"""The keyless DuckDuckGo hop as declared data (7.119.0 DDG-3, 7.119.1 DDG-3b).

Everything here runs over the DDG-1 corpus (``tests/fixtures/ddg``) or a page
written in the test, through an ``httpx.MockTransport``: nothing leaves the
process (and the suite's network gate would fail a test that tried).

What is pinned: the page profiles read the recorded pages -- titles, the
destination decoded once, ads skipped (by the row itself, whatever link an ad
row carries), snippets; a bot check with no result is a ``rate_limit`` failure
that names what was seen, never "no results"; a page that carries
DuckDuckGo's next-page form but yields no row is a failure, not an empty
answer; a genuinely empty page stays an honest empty answer; the pre-7.119.0
anchor scan is still the last fallback; ``kl`` / ``df`` reach the requests
exactly when ``DDGS_REGION`` / ``DDGS_TIMELIMIT`` are set;
``WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS`` spaces every request of this hop
and only this hop's, and 0 never waits; and with no chain set an HTTP error
status is worded through the shared status mapping.

7.119.1 (RESEARCH-DDG-WEBSEARCH.md 5 R2, USER DECISIONS Q12): the html form is
asked first, as declared -- ``POST`` with ``q`` and ``b``, the table's
``Referer``, ``Sec-Fetch-*`` and static browser-class User-Agent -- and the
lite page only as the second try, when the html page held no result and no
bot check. A bot check on the html page ends the hop with its rate limit and
no lite request; an HTTP error status or a transport failure ends it raw, as
before, with no lite request either. Request counts are asserted.
"""

import asyncio
import dataclasses
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from my_claude_code.api.web_tools import duckduckgo_lite, outbound
from my_claude_code.api.web_tools import streaming as web_streaming
from my_claude_code.api.web_tools.constants import _WEB_TOOL_HTTP_HEADERS
from my_claude_code.config import duckduckgo_profiles as profiles
from my_claude_code.config.settings import Settings
from my_claude_code.core.version import package_version
from my_claude_code.websearch.errors import (
    KIND_DETAIL_CHALLENGE,
    KIND_DETAIL_UNREADABLE_PAGE,
    WebSearchAuthError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from tests.support.ddg_harness import corpus, methods, scoring
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS

#: The URL each request kind's answer is read at (what httpx reports).
PAGE_URLS = {
    "html": "https://html.duckduckgo.com/html/",
    "lite": "https://lite.duckduckgo.com/lite/?q=omarchy+linux",
}
QUERY = "omarchy linux"
HTML_HOST = "html.duckduckgo.com"
LITE_HOST = "lite.duckduckgo.com"


def _page(name: str) -> str:
    return corpus.body_after_source_line(corpus.DDG_DIR / name).decode("utf-8")


def _answer(
    text: str,
    *,
    kind: str = "html",
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    return duckduckgo_lite.read_answer(
        kind=kind,
        status=status,
        text=text,
        page_url=PAGE_URLS[kind],
        headers=httpx.Headers(headers or {}),
    )


@pytest.fixture(autouse=True)
def _no_search_analytics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route's attempt and route rows are not this file's subject."""

    monkeypatch.setattr(outbound, "emit_search_outcome", lambda _outcome: None)
    monkeypatch.setattr(outbound, "emit_route_outcome", lambda _outcome: None)


# ---------------------------------------------------------------- the pages


def test_the_live_recorded_html_page_gives_ten_results_with_snippets() -> None:
    results = _answer(_page("octos_html_live_2026-09-28.html"))

    assert [row["url"] for row in results] == [
        "https://omarchy.org/",
        "https://omarchy.us/",
        "https://en.wikipedia.org/wiki/Omarchy",
        "https://omarchy-official.org/",
        "https://omarchy.org/manual/",
        "https://github.com/omacom/omarchy",
        "https://omarchy.archi/",
        "https://tedium.co/2025/10/13/omarchy-linux-distro-commentary/",
        "https://distrowatch.com/omarchy",
        "https://tbreak.com/omarchy-linux-distro-agentic-ai/",
    ]
    assert all(row["title"] and row["snippet"] for row in results)
    assert not any("duckduckgo.com" in row["url"] for row in results)
    assert results[2] == {
        "title": "Omarchy - Wikipedia",
        "url": "https://en.wikipedia.org/wiki/Omarchy",
        "snippet": (
            "Omarchy is an open-source Linux distribution created by David "
            "Heinemeier Hansson. It is based on Arch Linux and uses the Hyprland "
            "tiling Wayland compositor and Quickshell desktop shell. [3][4][5]"
        ),
    }


def _profile(kind: str) -> duckduckgo_lite.PageProfile:
    return next(item for item in duckduckgo_lite.PROFILES if item.kind == kind)


def test_r2s_html_chain_reads_the_live_recorded_page_on_the_html_profile() -> None:
    """The html row chain itself answers the recorded page, each alternative alone.

    R2: ``div.result:not(.result--ad)`` / ``div.web-result``, ``a.result__a``,
    ``.result__snippet``. The standard-library parser reads the page octos
    recorded live on 2026-09-28 with the first alternative and with the second
    on its own, to the same ten results; the lite row finds nothing there, so
    no other reading stood in for the html profile.
    """

    tree = duckduckgo_lite.parse_page(_page("octos_html_live_2026-09-28.html"))
    html = _profile("html")
    url = PAGE_URLS["html"]

    first = duckduckgo_lite.profile_results(tree, html, url)
    assert len(first) == 10
    assert all(row["title"] and row["snippet"] for row in first)
    result_row, web_result_row = html.rows
    for row in (result_row, web_result_row):
        alone = dataclasses.replace(html, rows=(row,))
        assert duckduckgo_lite.profile_results(tree, alone, url) == first, row
    assert duckduckgo_lite.profile_results(tree, _profile("lite"), url) == []
    assert duckduckgo_lite.read_results(tree, url, "html") == first


def test_each_answer_is_read_by_its_own_page_kind_first() -> None:
    """The lite answer keeps 7.119.0's reading order; the html answer reads html first."""

    assert [item.kind for item in duckduckgo_lite.profiles_for("lite")] == [
        "lite",
        "html",
    ]
    assert [item.kind for item in duckduckgo_lite.profiles_for("html")] == [
        "html",
        "lite",
    ]


def test_a_page_whose_rows_carry_only_web_result_still_reads() -> None:
    """R2's second alternative: rows that no longer carry ``result`` still read."""

    page = (
        "<html><body><div id='links'>"
        "<div class='web-result'><h2><a class='result__a' "
        "href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone&amp;rut=1'>"
        "One</a></h2><a class='result__snippet' href='#'>Snippet one.</a></div>"
        "<div class='sponsored result--ad'><h2><a class='result__a' "
        "href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fads.example.com%2F&amp;rut=2'>"
        "An ad</a></h2></div>"
        "</div></body></html>"
    )

    assert _answer(page) == [
        {"title": "One", "url": "https://example.org/one", "snippet": "Snippet one."}
    ]


def test_an_ad_row_is_never_a_result_whatever_link_it_carries() -> None:
    """Ads stay out by the row (``div.result--ad``), not only by their ``y.js`` link.

    The edge page's first ad is given an ordinary redirect link to its
    advertiser: the html row chain still leaves the row out.
    """

    original = _page("mcc_html_ads_and_edge_cases.html")
    page = original.replace(
        "//duckduckgo.com/y.js?ad_domain=ads.example.com&amp;ad_provider=bingv7aa"
        "&amp;u3=https%3A%2F%2Fads.example.com%2Fpromo",
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fads.example.com%2Fpromo&amp;rut=ad",
    )
    assert page != original

    results = _answer(page)

    assert [row["url"] for row in results] == [
        "https://example.org/guide",
        "https://example.net/direct",
        "https://example.edu/no-snippet",
        "https://example.com/search?q=a+b&page=2",
    ]
    assert not any(
        scoring.is_ad(row["url"], ("ads.example.com", "shop.example.com"))
        for row in results
    )


def test_the_edge_page_skips_both_ads_and_keeps_every_real_result() -> None:
    results = _answer(_page("mcc_html_ads_and_edge_cases.html"))

    assert results == [
        {
            "title": "Example guide to harness testing",
            "url": "https://example.org/guide",
            "snippet": "A step-by-step guide to harness testing with recorded pages.",
        },
        {
            # Missed before 7.119.0: its title link is direct, with no uddg=.
            "title": "A result whose title link is direct",
            "url": "https://example.net/direct",
            "snippet": "This result links straight to its destination, with no redirect.",
        },
        {
            "title": "A result with no snippet",
            "url": "https://example.edu/no-snippet",
            "snippet": "",
        },
        {
            "title": "Tom & Jerry: a destination with its own query",
            "url": "https://example.com/search?q=a+b&page=2",
            "snippet": "The destination carries q=a+b&page=2 of its own.",
        },
    ]
    assert not any(
        scoring.is_ad(row["url"], ("ads.example.com", "shop.example.com"))
        for row in results
    )


def test_the_lite_page_pairs_each_snippet_with_its_link() -> None:
    results = _answer(_page("mcc_lite_results.html"), kind="lite")

    assert results == [
        {
            "title": "First lite result",
            "url": "https://example.org/lite/one",
            "snippet": "The first lite result's snippet.",
        },
        {
            "title": "Second lite result",
            "url": "https://example.net/lite/two",
            "snippet": "The second result, about harness pages.",
        },
        {
            "title": "Third lite result & more",
            "url": "https://example.com/lite/three?x=1",
            "snippet": "A third snippet.",
        },
    ]


def test_a_destination_is_decoded_exactly_once() -> None:
    # The page percent-encodes the destination once; a destination carrying
    # its own escape (%25 for "%") must keep it.
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fcaf%25C3%25A9&rut=ab"
    assert (
        duckduckgo_lite.destination(href, PAGE_URLS["html"])
        == "https://example.org/caf%C3%A9"
    )


@pytest.mark.parametrize(
    "href",
    [
        "//duckduckgo.com/y.js?ad_domain=ads.example.com&u3=https%3A%2F%2Fads.example.com",
        "https://duckduckgo.com/y.js?ad_domain=ads.example.com",
        "//duckduckgo.com/l/?rut=only-a-token",
        "//duckduckgo.com/l/?uddg=&rut=ab",
        "//duckduckgo.com/l/?uddg=not-a-url",
        "javascript:alert(1)",
        "",
    ],
)
def test_an_ad_or_an_unusable_link_is_never_a_destination(href: str) -> None:
    for url in PAGE_URLS.values():
        assert duckduckgo_lite.destination(href, url) is None


def test_a_relative_link_is_made_absolute_against_the_page() -> None:
    for url in PAGE_URLS.values():
        assert (
            duckduckgo_lite.destination("//example.org/a?b=1", url)
            == "https://example.org/a?b=1"
        )


# ---------------------------------------------------------- classification


@pytest.mark.parametrize("kind", ["html", "lite"])
@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("mcc_lite_challenge.html", 202),
        ("mcc_lite_challenge.html", 200),
        ("octos_html_challenge_reconstructed.html", 202),
        ("octos_html_challenge_reconstructed.html", 200),
    ],
)
def test_a_bot_check_is_a_rate_limit_that_names_what_was_seen(
    name: str, status: int, kind: str
) -> None:
    with pytest.raises(WebSearchRateLimitError) as raised:
        _answer(_page(name), kind=kind, status=status)

    error = raised.value
    assert error.kind == "rate_limit"
    assert error.kind_detail == KIND_DETAIL_CHALLENGE
    assert error.provider == "legacy"
    assert error.status_code == status
    assert "bot check" in error.message
    assert ("HTTP 202" in error.message) is (status == 202)
    for marker in ("anomaly-modal", "anomaly.js", "challenge-form"):
        assert marker in error.message
    assert "no results" not in str(error).lower()


def test_http_202_with_no_page_at_all_is_still_the_bot_check() -> None:
    with pytest.raises(WebSearchRateLimitError, match=r"\(HTTP 202\)"):
        _answer("", status=202)


def test_a_retry_after_the_check_carries_is_kept() -> None:
    with pytest.raises(WebSearchRateLimitError) as raised:
        _answer(
            _page("mcc_lite_challenge.html"), status=202, headers={"Retry-After": "7"}
        )

    assert raised.value.retry_after_seconds == 7.0


def test_results_win_over_the_bot_check_words_and_over_202() -> None:
    """A page of results about bot checks carries the same words (octos)."""

    page = _page("mcc_lite_results.html").replace(
        "A third snippet.", "Unfortunately, bots use DuckDuckGo too: anomaly-modal."
    )
    assert len(_answer(page, kind="lite", status=200)) == 3
    assert len(_answer(page, kind="lite", status=202)) == 3


@pytest.mark.parametrize("kind", ["html", "lite"])
@pytest.mark.parametrize(
    "text",
    ["", "<html><body>nothing here</body></html>", "\x00\x01 not html at all"],
)
def test_a_page_with_no_result_and_no_check_is_an_honest_empty_answer(
    text: str, kind: str
) -> None:
    assert _answer(text, kind=kind) == []


def test_a_wordpress_search_box_is_not_duckduckgos_next_page_form() -> None:
    page = '<form action="/search/"><input name="s" value=""></form>'
    assert _answer(page) == []


@pytest.mark.parametrize(
    "form",
    [
        '<form action="/lite/" method="post"><input type="hidden" name="s" value="10"></form>',
        '<div class="nav-link"><form action="/html/" method="post">'
        '<input type="hidden" name="s" value="10"></form></div>',
    ],
)
def test_a_next_page_with_no_readable_row_is_a_layout_change_not_empty(
    form: str,
) -> None:
    page = f"<html><body><table><tr><td class='renamed'>x</td></tr></table>{form}</body></html>"
    with pytest.raises(WebSearchUpstreamError) as raised:
        _answer(page)

    assert raised.value.kind == "upstream"
    assert raised.value.kind_detail == KIND_DETAIL_UNREADABLE_PAGE
    assert "config/duckduckgo_profiles.py" in raised.value.message


@pytest.mark.parametrize("kind", ["html", "lite"])
def test_the_old_anchor_scan_is_still_the_last_fallback(kind: str) -> None:
    """A layout no profile knows degrades to the pre-7.119.0 answer, never below."""

    page = (
        "<html><body><div class='renamed-row'>"
        "<a class='renamed-link' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fx"
        "&amp;rut=1'>Renamed layout</a></div></body></html>"
    )
    assert _answer(page, kind=kind) == [
        {"title": "Renamed layout", "url": "https://example.org/x"}
    ]


# ------------------------------------------------------------- the requests


def test_with_no_option_set_each_request_sends_only_its_declared_fields() -> None:
    html, lite = profiles.REQUESTS
    assert duckduckgo_lite.request_fields(html, QUERY, {}) == {"q": QUERY, "b": ""}
    # The lite request is the one every release before 7.119.1 sent.
    assert duckduckgo_lite.request_fields(lite, QUERY, {}) == {"q": QUERY}


def test_region_and_time_travel_as_kl_and_df() -> None:
    options = {"DDGS_REGION": "uk-en", "DDGS_TIMELIMIT": "w", "DDGS_BACKEND": "bing"}
    html, lite = profiles.REQUESTS
    fields = duckduckgo_lite.request_fields(html, QUERY, options)
    # R2's order: q, b, kl, df.
    assert list(fields.items()) == [
        ("q", QUERY),
        ("b", ""),
        ("kl", "uk-en"),
        ("df", "w"),
    ]
    assert duckduckgo_lite.request_fields(lite, QUERY, options) == {
        "q": QUERY,
        "kl": "uk-en",
        "df": "w",
    }
    for request in profiles.REQUESTS:
        assert "kl" not in duckduckgo_lite.request_fields(
            request, QUERY, {"DDGS_REGION": "  "}
        )


type _Answer = httpx.Response | Exception


class _DuckDuckGo:
    """DuckDuckGo's two endpoints, scripted: every request recorded in order.

    Each endpoint answers every request with its one scripted answer. An
    endpoint the test scripted no answer for fails the test when it is asked.
    """

    def __init__(self, html: _Answer, lite: _Answer | None) -> None:
        self.answers: dict[str, _Answer] = {HTML_HOST: html}
        if lite is not None:
            self.answers[LITE_HOST] = lite
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.get(request.url.host)
        if answer is None:
            raise AssertionError(f"the hop asked {request.url}; nothing was scripted")
        if isinstance(answer, httpx.RequestError):
            raise type(answer)(str(answer), request=request)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(
            answer.status_code,
            headers=answer.headers,
            content=answer.content,
            request=request,
        )

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@pytest.fixture
def ddg(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _DuckDuckGo]:
    real = httpx.AsyncClient

    def install(*, html: _Answer, lite: _Answer | None = None) -> _DuckDuckGo:
        upstream = _DuckDuckGo(html, lite)

        class _MockedClient(real):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(upstream.handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(outbound.httpx, "AsyncClient", _MockedClient)
        return upstream

    return install


def _ok(
    name: str = "octos_html_live_2026-09-28.html", status: int = 200
) -> httpx.Response:
    return httpx.Response(status, content=_page(name).encode("utf-8"))


#: A page with no result and no sign of a bot check.
_NOTHING = httpx.Response(200, content=b"<html><body>nothing here</body></html>")

#: A results page none of the profiles reads: DuckDuckGo's next-page form, no row.
_UNREADABLE = httpx.Response(
    200,
    content=(
        b"<html><body><table><tr><td class='renamed'>x</td></tr></table>"
        b'<div class="nav-link"><form action="/html/" method="post">'
        b'<input type="hidden" name="s" value="10"></form></div></body></html>'
    ),
)


def _form(request: httpx.Request) -> list[tuple[str, str]]:
    return parse_qsl(request.content.decode("utf-8"), keep_blank_values=True)


@pytest.fixture
def clean_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite reads no dotenv file (tests/conftest.py); the process
    # environment is the only place these could come from.
    for name in (
        "DDGS_REGION",
        "DDGS_TIMELIMIT",
        "WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_the_hop_asks_the_html_form_first_exactly_as_declared(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    """One POST of the html form, ``q`` and ``b`` in R2's order; it answers."""

    upstream = ddg(html=_ok(), lite=_ok("mcc_lite_results.html"))

    results = await outbound._legacy_web_search_scrape(QUERY)

    assert len(results) == 10
    assert results[0]["url"] == "https://omarchy.org/"
    (request,) = upstream.requests
    assert request.method == "POST"
    assert str(request.url) == "https://html.duckduckgo.com/html/"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.content == b"q=omarchy+linux&b="


@pytest.mark.asyncio
async def test_the_html_request_carries_the_tables_headers(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    """``Referer``, ``Sec-Fetch-*`` and the static browser-class User-Agent."""

    upstream = ddg(html=_ok())

    await outbound._legacy_web_search_scrape(QUERY)

    (request,) = upstream.requests
    declared = profiles.HTML_REQUEST.headers
    assert {name.lower() for name, _ in declared} == {
        "user-agent",
        "referer",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
    }
    for name, value in declared:
        assert request.headers[name] == value, name
    assert request.headers["user-agent"] == profiles.BROWSER_USER_AGENT
    assert request.headers["referer"] == "https://html.duckduckgo.com/"
    assert f"my-claude-code/{package_version()}" not in request.headers["user-agent"]
    # Since 7.120.0 (DDG-4) the fetch path has its own identity, MCC's product
    # User-Agent: this hop keeps the browser-class one and the two never mix.
    assert _WEB_TOOL_HTTP_HEADERS["User-Agent"] == (
        f"my-claude-code/{package_version()}"
    )
    assert _WEB_TOOL_HTTP_HEADERS["User-Agent"] != profiles.BROWSER_USER_AGENT


@pytest.mark.asyncio
async def test_the_requests_follow_the_table_and_nothing_else(
    ddg: Callable[..., _DuckDuckGo],
    clean_options: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edited row is the request sent: no header or field lives in code."""

    edited = dataclasses.replace(
        profiles.HTML_REQUEST,
        fixed_fields=(("b", "edited"),),
        headers=(
            ("User-Agent", "Edited/1.0"),
            ("Referer", "https://example.test/"),
            ("Sec-Fetch-Site", "none"),
        ),
    )
    monkeypatch.setattr(duckduckgo_lite, "REQUESTS", (edited, profiles.LITE_REQUEST))
    upstream = ddg(html=_ok())

    await outbound._legacy_web_search_scrape(QUERY)

    (request,) = upstream.requests
    assert request.headers["user-agent"] == "Edited/1.0"
    assert request.headers["referer"] == "https://example.test/"
    assert request.headers["sec-fetch-site"] == "none"
    assert "sec-fetch-dest" not in request.headers
    assert _form(request) == [("q", QUERY), ("b", "edited")]


@pytest.mark.asyncio
async def test_an_html_page_with_no_result_and_no_check_asks_lite_exactly_once(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    """R2: the lite page is the second try when the html page yields nothing."""

    upstream = ddg(html=_NOTHING, lite=_ok("mcc_lite_results.html"))

    results = await outbound._legacy_web_search_scrape(QUERY)

    assert [row["url"] for row in results] == [
        "https://example.org/lite/one",
        "https://example.net/lite/two",
        "https://example.com/lite/three?x=1",
    ]
    assert all(row["snippet"] for row in results)
    assert upstream.hosts == [HTML_HOST, LITE_HOST]
    lite = upstream.requests[1]
    # The query string every release before 7.119.1 sent, under the table's UA.
    assert lite.method == "GET"
    assert str(lite.url) == "https://lite.duckduckgo.com/lite/?q=omarchy+linux"
    assert lite.content == b""
    for name, value in profiles.LITE_REQUEST.headers:
        assert lite.headers[name] == value, name
    assert "referer" not in lite.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [202, 200])
async def test_a_bot_check_on_the_html_page_ends_the_hop_and_lite_is_never_asked(
    ddg: Callable[..., _DuckDuckGo], clean_options: None, status: int
) -> None:
    """Never solved or worked around: the check is reported, no second try."""

    upstream = ddg(
        html=_ok("octos_html_challenge_reconstructed.html", status=status),
        lite=_ok("mcc_lite_results.html"),
    )

    with pytest.raises(WebSearchRateLimitError) as raised:
        await outbound._legacy_web_search_scrape(QUERY)

    assert raised.value.kind_detail == KIND_DETAIL_CHALLENGE
    assert raised.value.status_code == status
    assert upstream.hosts == [HTML_HOST]


@pytest.mark.asyncio
async def test_a_bot_check_on_the_lite_page_after_an_empty_html_page_is_reported(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    upstream = ddg(html=_NOTHING, lite=_ok("mcc_lite_challenge.html", status=202))

    with pytest.raises(WebSearchRateLimitError) as raised:
        await outbound._legacy_web_search_scrape(QUERY)

    assert raised.value.kind_detail == KIND_DETAIL_CHALLENGE
    assert upstream.hosts == [HTML_HOST, LITE_HOST]


@pytest.mark.asyncio
async def test_two_pages_with_nothing_are_an_honest_empty_answer(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    upstream = ddg(html=_NOTHING, lite=_NOTHING)

    assert await outbound._legacy_web_search_scrape(QUERY) == []
    assert upstream.hosts == [HTML_HOST, LITE_HOST]


@pytest.mark.asyncio
async def test_an_html_page_mcc_cannot_read_lets_the_lite_page_answer(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    """A layout change on the html page never answers below the lite page."""

    upstream = ddg(html=_UNREADABLE, lite=_ok("mcc_lite_results.html"))

    assert len(await outbound._legacy_web_search_scrape(QUERY)) == 3
    assert upstream.hosts == [HTML_HOST, LITE_HOST]


@pytest.mark.asyncio
async def test_an_unreadable_html_page_is_the_failure_when_lite_has_nothing(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    """Its results exist: an empty lite page does not turn them into "none"."""

    upstream = ddg(html=_UNREADABLE, lite=_NOTHING)

    with pytest.raises(WebSearchUpstreamError) as raised:
        await outbound._legacy_web_search_scrape(QUERY)

    assert raised.value.kind_detail == KIND_DETAIL_UNREADABLE_PAGE
    assert upstream.hosts == [HTML_HOST, LITE_HOST]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403, content=b"Forbidden"),
        httpx.Response(429, content=b"Too Many Requests"),
        httpx.Response(503, content=b"Service Unavailable"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
async def test_an_http_error_or_transport_failure_ends_the_hop_raw(
    ddg: Callable[..., _DuckDuckGo], clean_options: None, answer: _Answer
) -> None:
    """Raised as httpx raised it, exactly as before 7.119.0 -- the chain's
    ``legacy`` entry classifies it itself -- and no lite request follows."""

    upstream = ddg(html=answer, lite=_ok("mcc_lite_results.html"))

    with pytest.raises(httpx.HTTPError) as raised:
        await outbound._legacy_web_search_scrape(QUERY)

    expected = (
        httpx.HTTPStatusError if isinstance(answer, httpx.Response) else type(answer)
    )
    assert type(raised.value) is expected
    assert upstream.hosts == [HTML_HOST]


@pytest.mark.asyncio
async def test_kl_and_df_reach_both_requests_exactly_as_declared(
    ddg: Callable[..., _DuckDuckGo],
    clean_options: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DDGS_REGION", "uk-en")
    monkeypatch.setenv("DDGS_TIMELIMIT", "w")
    upstream = ddg(html=_NOTHING, lite=_ok("mcc_lite_results.html"))

    await outbound._legacy_web_search_scrape(QUERY)

    html, lite = upstream.requests
    assert _form(html) == [("q", QUERY), ("b", ""), ("kl", "uk-en"), ("df", "w")]
    assert str(lite.url) == (
        "https://lite.duckduckgo.com/lite/?q=omarchy+linux&kl=uk-en&df=w"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "error_type", "summary"),
    [
        (
            403,
            {},
            WebSearchAuthError,
            "web_search unavailable: legacy refused access (HTTP 403)",
        ),
        (
            429,
            {"Retry-After": "7"},
            WebSearchRateLimitError,
            "web_search rate limited by legacy: retry after 7s",
        ),
        (
            503,
            {},
            WebSearchUpstreamError,
            "web_search unavailable: legacy answered with a server error (HTTP 503)",
        ),
    ],
)
async def test_with_no_chain_an_http_error_status_is_worded_through_the_shared_mapping(
    ddg: Callable[..., _DuckDuckGo],
    clean_options: None,
    status: int,
    headers: dict[str, str],
    error_type: type[Exception],
    summary: str,
) -> None:
    upstream = ddg(
        html=httpx.Response(status, headers=headers, content=b"<html>error</html>")
    )
    settings = Settings.model_validate(
        {**NO_WEB_SEARCH_CREDENTIALS, "WEB_SEARCH_PROVIDER": "off"}
    )

    with pytest.raises(error_type) as raised:
        await outbound._run_web_search(QUERY, settings)

    error = raised.value
    assert isinstance(error.__cause__, httpx.HTTPStatusError)
    assert (
        outbound._web_tool_client_error_summary("web_search", error, verbose=False)
        == summary
    )
    assert upstream.hosts == [HTML_HOST]


@pytest.mark.asyncio
async def test_with_no_chain_the_bot_check_is_never_no_results(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    ddg(html=_ok("octos_html_challenge_reconstructed.html", status=202))
    settings = Settings.model_validate(
        {**NO_WEB_SEARCH_CREDENTIALS, "WEB_SEARCH_PROVIDER": "off"}
    )

    with pytest.raises(WebSearchRateLimitError) as raised:
        await outbound._run_web_search(QUERY, settings)

    text = outbound._web_tool_client_error_summary(
        "web_search", raised.value, verbose=False
    )
    assert text.startswith(
        "web_search rate limited by legacy: DuckDuckGo answered with its bot "
        "check instead of results (HTTP 202; the page carries 'anomaly-modal'"
    )
    assert "No web search results" not in text
    assert web_streaming._web_search_error_code(raised.value) == "too_many_requests"


@pytest.mark.asyncio
async def test_with_no_chain_a_genuinely_empty_page_says_so(
    ddg: Callable[..., _DuckDuckGo], clean_options: None
) -> None:
    ddg(html=_NOTHING, lite=_NOTHING)
    settings = Settings.model_validate(
        {**NO_WEB_SEARCH_CREDENTIALS, "WEB_SEARCH_PROVIDER": "off"}
    )

    results = await outbound._run_web_search(QUERY, settings)

    assert results == []
    assert (
        web_streaming._search_summary(QUERY, results, settings)
        == "No web search results found for: omarchy linux"
    )


# ---------------------------------------------------------------- pacing


class _FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


@pytest.fixture
def fake_pacing(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeTime]:
    clock = _FakeTime()
    monkeypatch.setattr(
        duckduckgo_lite,
        "_PACING",
        duckduckgo_lite.LitePacing(clock=clock.monotonic, sleep=clock.sleep),
    )
    yield clock


def test_an_interval_of_zero_never_waits_and_records_nothing(
    fake_pacing: _FakeTime,
) -> None:
    async def run() -> list[float]:
        return [await duckduckgo_lite.pace(0) for _ in range(5)]

    assert asyncio.run(run()) == [0.0] * 5
    assert fake_pacing.slept == []


def test_a_set_interval_spaces_requests_in_the_order_they_asked(
    fake_pacing: _FakeTime,
) -> None:
    async def run() -> list[float]:
        first = await duckduckgo_lite.pace(2.5)
        second = await duckduckgo_lite.pace(2.5)
        fake_pacing.now += 10.0  # long after: no wait
        third = await duckduckgo_lite.pace(2.5)
        return [first, second, third]

    assert asyncio.run(run()) == [0.0, 2.5, 0.0]
    assert fake_pacing.slept == [2.5]


def test_requests_made_together_queue_one_interval_apart() -> None:
    real_sleeps: list[float] = []

    async def record(seconds: float) -> None:
        real_sleeps.append(round(seconds, 3))

    pacing = duckduckgo_lite.LitePacing(clock=lambda: 50.0, sleep=record)

    async def run() -> list[float]:
        return list(await asyncio.gather(*(pacing.wait_turn(3.0) for _ in range(3))))

    assert asyncio.run(run()) == [0.0, 3.0, 6.0]
    assert real_sleeps == [3.0, 6.0]


@pytest.mark.asyncio
async def test_the_setting_spaces_consecutive_hop_requests(
    ddg: Callable[..., _DuckDuckGo],
    clean_options: None,
    fake_pacing: _FakeTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS", "2.5")
    upstream = ddg(html=_ok())

    await outbound._legacy_web_search_scrape(QUERY)
    await outbound._legacy_web_search_scrape(QUERY)

    assert upstream.hosts == [HTML_HOST, HTML_HOST]
    assert fake_pacing.slept == [2.5]


@pytest.mark.asyncio
async def test_the_html_and_lite_requests_of_one_search_are_two_turns(
    ddg: Callable[..., _DuckDuckGo],
    clean_options: None,
    fake_pacing: _FakeTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request takes its turn: the lite try waits the interval after the html
    request, and the next search's html request the interval after that."""

    monkeypatch.setenv("WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS", "2.5")
    upstream = ddg(html=_NOTHING, lite=_ok("mcc_lite_results.html"))

    await outbound._legacy_web_search_scrape(QUERY)

    assert upstream.hosts == [HTML_HOST, LITE_HOST]
    assert fake_pacing.slept == [2.5]

    await outbound._legacy_web_search_scrape(QUERY)

    assert upstream.hosts == [HTML_HOST, LITE_HOST, HTML_HOST, LITE_HOST]
    assert fake_pacing.slept == [2.5, 2.5, 2.5]


@pytest.mark.asyncio
async def test_by_default_the_hop_sends_back_to_back(
    ddg: Callable[..., _DuckDuckGo], clean_options: None, fake_pacing: _FakeTime
) -> None:
    upstream = ddg(html=_NOTHING, lite=_ok("mcc_lite_results.html"))

    for _ in range(3):
        await outbound._legacy_web_search_scrape(QUERY)

    assert upstream.hosts == [HTML_HOST, LITE_HOST] * 3
    assert fake_pacing.slept == []


def test_the_interval_delays_only_the_duckduckgo_hop(
    monkeypatch: pytest.MonkeyPatch, clean_options: None
) -> None:
    """``ddgs`` and ``web_fetch`` never ask the hop's pacing for a turn."""

    monkeypatch.setenv("WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS", "30")
    asked: list[float] = []

    async def recording_pace(interval: float) -> float:
        asked.append(interval)
        return 0.0

    monkeypatch.setattr(duckduckgo_lite, "pace", recording_pace)
    scenario = next(
        item for item in corpus.search_corpus().scenarios if item.id == "live_html_200"
    )
    page = corpus.ddg_page_fetches()[0]

    async def run() -> None:
        await methods.run_ddgs(scenario, QUERY, 10)
        async with methods.fetch_server((page,)) as server:
            await methods.run_web_fetch(page, server)
        assert asked == []
        await methods.run_legacy(scenario, QUERY, 10)

    asyncio.run(run())
    assert asked == [30.0]


# ------------------------------------------- the harness's sixteen scenarios


def test_every_scenario_ends_in_the_outcome_its_corpus_declares() -> None:
    """The DDG-1 corpus's own targets: 16 of 16 (10 of 16 before 7.119.0)."""

    runs = methods.run_sync(methods.run_search_method("legacy"))
    cells = [
        scoring.score_search_cell(scenario, runs[scenario.id])
        for scenario in corpus.search_corpus().scenarios
    ]

    assert [(cell.scenario, cell.observed) for cell in cells] == [
        (cell.scenario, cell.expect) for cell in cells
    ]
    summary = scoring.summarise_search(cells)
    assert summary.challenge_detected == summary.challenge_pages == 4
    assert summary.ads_leaked == 0
    assert summary.url_ok == summary.returned == summary.real_results == 17
    # Snippets wherever the page carries one: all but the edge page's one row
    # that has none.
    assert summary.snippets == 16
