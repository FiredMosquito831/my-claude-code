"""The keyless DuckDuckGo hop of the web search route (``legacy``), 7.119.0.

``outbound._legacy_web_search_scrape`` makes the request; this module decides
what is asked and what the answer means, from the declared table in
``config/duckduckgo_profiles.py``:

1. **What is asked** (:func:`request_params`): the lite endpoint's query
   field, plus ``kl`` / ``df`` when ``DDGS_REGION`` / ``DDGS_TIMELIMIT`` are
   set -- the ``ddgs`` provider's own options, read the way its adapter reads
   them. With neither set the request is byte for byte the one every earlier
   release made. :func:`pace` spaces consecutive requests of this hop by
   ``WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS``; 0, the default, never waits.
2. **What the page says** (:func:`read_answer`): the first page profile that
   finds a result row gives each result's title, destination and snippet. A
   destination behind DuckDuckGo's ``/l/?uddg=`` redirect is decoded once; an
   ad's ``y.js`` link and an ad row are never results. When no profile finds a
   row, the pre-7.119.0 anchor scan (``parsers.SearchResultParser``) runs as
   the last fallback, so a layout change degrades to the old answer and never
   below it.
3. **What a page with no result is** -- never "no results" when the page says
   otherwise: HTTP 202 or one of the bot check's own markers is a
   :class:`WebSearchRateLimitError` naming what was seen
   (``KIND_DETAIL_CHALLENGE``), reported and never solved or worked around; a
   page that carries DuckDuckGo's next-page form yet has no row any profile
   can read is a :class:`WebSearchUpstreamError`
   (``KIND_DETAIL_UNREADABLE_PAGE``): its results exist and MCC cannot read
   them; anything else is an honest empty answer.

An HTTP error status and a transport failure are NOT read here: the hop raises
them exactly as before 7.119.0, so the search chain's ``legacy`` entry
classifies them with the model path's own classifier and decides exactly as it
did. The route used with no chain set words them through
:func:`status_failure` -- the adapters' shared status mapping -- instead of
passing a raw ``httpx`` error on.

The page is read with the standard library's ``html.parser`` into a small
tree, the parser technology of the pre-7.119.0 scraper: nothing new is
imported and no dependency is added (USER DECISIONS 2026-10-25 Q1).
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from my_claude_code.config.duckduckgo_profiles import (
    AD_LINK,
    CHALLENGE_MARKERS,
    CHALLENGE_STATUSES,
    LITE_REQUEST,
    PAGE_PROFILES,
    REDIRECT,
    DuckDuckGoPageProfile,
    DuckDuckGoRequest,
)
from my_claude_code.config.settings import WEB_SEARCH_CHAIN_LEGACY_ENTRY
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG
from my_claude_code.core.rate_limit import retry_after_seconds
from my_claude_code.websearch.adapters.http import map_status_error
from my_claude_code.websearch.errors import (
    KIND_DETAIL_CHALLENGE,
    KIND_DETAIL_UNREADABLE_PAGE,
    WebSearchError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.options import read_websearch_options

from .constants import _MAX_SEARCH_RESULTS
from .parsers import SearchResultParser

#: The provider id the hop's results and failures carry (route rows, digest).
PROVIDER_ID = WEB_SEARCH_CHAIN_LEGACY_ENTRY

#: The one request the hop makes (``config/duckduckgo_profiles.py``).
LITE: DuckDuckGoRequest = LITE_REQUEST


# --------------------------------------------------------------------------- #
# What is asked


def request_params(
    query: str, options: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The query string the lite endpoint is asked with.

    ``options`` are the ``ddgs`` provider's set options; when omitted they are
    read here, per request (process environment, else the dotenv files), so a
    dashboard save applies to the next search. An option that is not set is
    not sent.
    """

    if options is None:
        options = read_websearch_options("ddgs", WEBSEARCH_CATALOG["ddgs"])
    params = {LITE.query_field: query}
    for option, page_field in LITE.option_fields:
        value = options.get(option, "").strip()
        if value:
            params[page_field] = value
    return params


class LitePacing:
    """The least time between two requests of the hop, when one is set.

    A request reserves its start before it waits, so requests made together
    are spaced one interval apart in the order they asked. An interval of 0
    (the default) returns at once and records nothing: the hop sends back to
    back, as every earlier release did.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last_start: float | None = None

    async def wait_turn(self, interval: float) -> float:
        """Wait until this request may start; the seconds waited."""

        if interval <= 0:
            return 0.0
        now = self._clock()
        start = (
            now if self._last_start is None else max(now, self._last_start + interval)
        )
        self._last_start = start
        wait = start - now
        if wait > 0:
            await self._sleep(wait)
        return wait

    def forget(self) -> None:
        """Forget the previous request's start (tests)."""

        self._last_start = None


_PACING = LitePacing()


async def pace(interval: float) -> float:
    """Space this request from the hop's previous one (``0``: no wait at all)."""

    return await _PACING.wait_turn(interval)


def reset_pacing() -> None:
    """Forget the previous request's start (tests)."""

    _PACING.forget()


# --------------------------------------------------------------------------- #
# The page as a tree

#: Elements HTML never closes.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
#: Elements whose text is never page text.
_NO_TEXT = frozenset({"script", "style", "noscript", "template"})
#: The two closings HTML itself implies in a table: a new row closes the open
#: row, a new cell the open cell -- never across the table that holds them.
_IMPLIED_CLOSE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "tr": (frozenset({"tr"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
}


@dataclass(slots=True, eq=False)
class Node:
    """One element of the page: its tag, attributes and children in order."""

    tag: str
    attrs: dict[str, str]
    parent: Node | None
    children: list[Node | str] = field(default_factory=list)

    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    def walk(self) -> Iterator[Node]:
        """This element and every element under it, in document order."""

        stack: list[Node] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(
                child for child in reversed(node.children) if isinstance(child, Node)
            )

    def text(self) -> str:
        """The element's text, whitespace collapsed (scripts and styles left out)."""

        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in _NO_TEXT:
                stack.extend(reversed(item.children))
        return " ".join("".join(parts).split())


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self._open: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        node = self._append(tag, attrs)
        if tag not in _VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implied(tag)
        self._append(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        # An end tag closes the nearest open element of its name and whatever
        # was left open inside it; a stray one closes nothing.
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)

    def _append(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        parent = self._open[-1]
        node = Node(tag, {name: value or "" for name, value in attrs}, parent)
        parent.children.append(node)
        return node

    def _close_implied(self, tag: str) -> None:
        rule = _IMPLIED_CLOSE.get(tag)
        if rule is None:
            return
        targets, fences = rule
        for depth in range(len(self._open) - 1, 0, -1):
            open_tag = self._open[depth].tag
            if open_tag in fences:
                return
            if open_tag in targets:
                del self._open[depth:]
                return


def parse_page(text: str) -> Node:
    """The page as a tree (malformed markup is read, never refused)."""

    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------- #
# Selectors

_SELECTOR = re.compile(
    r"(?P<tag>[a-z][a-z0-9]*)?"
    r"(?P<parts>(?:[.#][A-Za-z0-9_-]+|\[[a-z][a-z0-9_-]*(?:=\"[^\"]*\")?\])*)"
)
_PART = re.compile(
    r"\.(?P<cls>[A-Za-z0-9_-]+)"
    r"|#(?P<id>[A-Za-z0-9_-]+)"
    r"|\[(?P<attr>[a-z][a-z0-9_-]*)(?:=\"(?P<value>[^\"]*)\")?\]"
)


@dataclass(frozen=True, slots=True)
class Selector:
    """One element: its tag, classes, id and attributes (any may be absent)."""

    tag: str | None
    classes: frozenset[str]
    element_id: str | None
    attributes: tuple[tuple[str, str | None], ...]

    def matches(self, node: Node) -> bool:
        if self.tag is not None and node.tag != self.tag:
            return False
        if self.classes and not self.classes <= node.classes():
            return False
        if self.element_id is not None and node.attrs.get("id") != self.element_id:
            return False
        for name, value in self.attributes:
            if name not in node.attrs:
                return False
            if value is not None and node.attrs[name] != value:
                return False
        return True

    def first_under(self, node: Node) -> Node | None:
        """The first element below ``node`` (not ``node`` itself) that matches."""

        for candidate in node.walk():
            if candidate is not node and self.matches(candidate):
                return candidate
        return None

    def holds(self, node: Node) -> bool:
        """Whether ``node`` or an element that contains it matches."""

        current: Node | None = node
        while current is not None:
            if self.matches(current):
                return True
            current = current.parent
        return False


def parse_selector(text: str) -> Selector:
    """Read one declared selector; anything outside the small form is refused."""

    stripped = text.strip()
    match = _SELECTOR.fullmatch(stripped)
    if match is None or not stripped:
        raise ValueError(f"not a page-profile selector: {text!r}")
    classes: set[str] = set()
    element_id: str | None = None
    attributes: list[tuple[str, str | None]] = []
    for part in _PART.finditer(match.group("parts")):
        if part.group("cls"):
            classes.add(part.group("cls"))
        elif part.group("id"):
            element_id = part.group("id")
        else:
            attributes.append((part.group("attr"), part.group("value")))
    return Selector(
        tag=match.group("tag"),
        classes=frozenset(classes),
        element_id=element_id,
        attributes=tuple(attributes),
    )


# --------------------------------------------------------------------------- #
# What the page says


@dataclass(frozen=True, slots=True)
class PageProfile:
    """A declared page profile with every selector read."""

    kind: str
    row: Selector | None
    link: Selector
    snippet: Selector
    skip: tuple[Selector, ...]
    next_page_form: Selector
    next_page_field: Selector

    def skipped(self, node: Node) -> bool:
        return any(selector.holds(node) for selector in self.skip)


def compile_profile(profile: DuckDuckGoPageProfile) -> PageProfile:
    """Read every selector of a declared row (a malformed one raises)."""

    return PageProfile(
        kind=profile.kind,
        row=None if profile.row is None else parse_selector(profile.row),
        link=parse_selector(profile.link),
        snippet=parse_selector(profile.snippet),
        skip=tuple(parse_selector(text) for text in profile.skip),
        next_page_form=parse_selector(profile.next_page_form),
        next_page_field=parse_selector(profile.next_page_field),
    )


PROFILES: tuple[PageProfile, ...] = tuple(
    compile_profile(profile) for profile in PAGE_PROFILES
)


def _is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def destination(href: str, page_url: str) -> str | None:
    """Where a result link leads, or ``None`` for an ad or an unusable link.

    A DuckDuckGo redirect names the destination in its ``uddg`` parameter,
    which the page percent-encodes once and which is decoded exactly once
    here. Any other link is made absolute against the page's own URL.
    """

    href = href.strip()
    if not href:
        return None
    try:
        absolute = urljoin(page_url, href)
        parts = urlsplit(absolute)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host == AD_LINK.host and parts.path == AD_LINK.path:
        return None
    if host == REDIRECT.host and parts.path == REDIRECT.path:
        values = [
            value
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name == REDIRECT.parameter
        ]
        target = values[0].strip() if values else ""
        return target if _is_http_url(target) else None
    return absolute if _is_http_url(absolute) else None


def _result(link: Node, page_url: str) -> dict[str, str] | None:
    url = destination(link.attrs.get("href", ""), page_url)
    title = link.text()
    if url is None or not title:
        return None
    return {"title": title, "url": url, "snippet": ""}


def profile_results(
    tree: Node, profile: PageProfile, page_url: str
) -> list[dict[str, str]]:
    """Every result one profile reads off the page, in page order, each URL once."""

    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def keep(result: dict[str, str] | None) -> dict[str, str] | None:
        if result is None or result["url"] in seen:
            return None
        seen.add(result["url"])
        found.append(result)
        return result

    if profile.row is None:
        # No row element: a title link opens a result and the next snippet
        # belongs to it (a link that is not kept -- an ad, a repeat -- takes
        # the snippet after it out of play too).
        current: dict[str, str] | None = None
        for node in tree.walk():
            if profile.skip and profile.skipped(node):
                continue
            if profile.link.matches(node):
                current = keep(_result(node, page_url))
            elif (
                current is not None
                and not current["snippet"]
                and profile.snippet.matches(node)
            ):
                current["snippet"] = node.text()
        return found
    for row in tree.walk():
        if not profile.row.matches(row) or profile.skipped(row):
            continue
        link = profile.link.first_under(row)
        if link is None:
            continue
        result = keep(_result(link, page_url))
        if result is None:
            continue
        snippet = profile.snippet.first_under(row)
        if snippet is not None:
            result["snippet"] = snippet.text()
    return found


def read_results(tree: Node, page_url: str) -> list[dict[str, str]]:
    """The results of the first profile that finds any (capped at MCC's ten)."""

    for profile in PROFILES:
        rows = profile_results(tree, profile, page_url)
        if rows:
            return rows[:_MAX_SEARCH_RESULTS]
    return []


def challenge_markers(text: str) -> tuple[str, ...]:
    """The bot check's markers this page carries, as they are declared."""

    folded = text.casefold()
    return tuple(marker for marker in CHALLENGE_MARKERS if marker.casefold() in folded)


def offers_next_page(tree: Node) -> bool:
    """Whether the page carries a profile's next-page form (with its field)."""

    for node in tree.walk():
        for profile in PROFILES:
            if profile.next_page_form.matches(node) and (
                profile.next_page_field.first_under(node) is not None
            ):
                return True
    return False


def _challenge_message(status: int, markers: tuple[str, ...]) -> str:
    seen: list[str] = []
    if status in CHALLENGE_STATUSES:
        seen.append(f"HTTP {status}")
    if markers:
        seen.append("the page carries " + ", ".join(f"'{m}'" for m in markers))
    return (
        "DuckDuckGo answered with its bot check instead of results "
        f"({'; '.join(seen)}). MCC reports the check and never solves or "
        "works around it."
    )


def read_answer(
    *, status: int, text: str, page_url: str, headers: Mapping[str, str]
) -> list[dict[str, str]]:
    """The results of one 2xx answer, or the failure it really is.

    Results win: a page that has results is read as results, whatever else it
    says. With none, the old anchor scan has the last word; then HTTP 202 or a
    marker of the bot check is a rate limit naming what was seen, a page that
    carries the next-page form is one MCC cannot read, and anything else is an
    honest empty answer.
    """

    tree = parse_page(text)
    results = read_results(tree, page_url)
    if results:
        return results
    scan = SearchResultParser()
    scan.feed(text)
    if scan.results:
        return scan.results[:_MAX_SEARCH_RESULTS]
    markers = challenge_markers(text)
    if status in CHALLENGE_STATUSES or markers:
        raise WebSearchRateLimitError(
            PROVIDER_ID,
            _challenge_message(status, markers),
            status_code=status,
            retry_after_seconds=retry_after_seconds(headers),
            kind_detail=KIND_DETAIL_CHALLENGE,
        )
    if offers_next_page(tree):
        raise WebSearchUpstreamError(
            PROVIDER_ID,
            "DuckDuckGo's page carries its next-page form but none of MCC's "
            "page profiles found a result row in it: its layout has changed "
            "(config/duckduckgo_profiles.py)",
            kind_detail=KIND_DETAIL_UNREADABLE_PAGE,
        )
    return []


# --------------------------------------------------------------------------- #
# An HTTP error status, worded for the route with no chain

#: Headers that describe the body the hop never read; a copy without the body
#: must not carry them.
_BODY_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def status_failure(error: httpx.HTTPStatusError) -> WebSearchError:
    """The hop's HTTP error status through the adapters' shared status mapping.

    401/403 refused access, 402 a quota, 429 a rate limit carrying the wait
    the host published (``Retry-After``), 400/404/405/422 a refused request,
    anything else an upstream failure with its status. The hop raises before
    it reads the body, so the message is ``HTTP <status>``.
    """

    response = error.response
    bodiless = httpx.Response(
        response.status_code,
        headers=[
            (name, value)
            for name, value in response.headers.items()
            if name.lower() not in _BODY_HEADERS
        ],
        content=b"",
    )
    return map_status_error(PROVIDER_ID, bodiless)
