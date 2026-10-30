"""DuckDuckGo's no-JavaScript result pages, declared as data (7.119.0, 7.119.1).

The keyless last hop of the web search route (the ``legacy`` entry,
``api/web_tools/duckduckgo_lite.py``) asks DuckDuckGo for one page of results
and reads the answer. Everything it needs to know about DuckDuckGo lives in
this table, so a markup change is an edit to one row here and a recorded page
under ``tests/fixtures/ddg``, never a change of code:

* :data:`REQUESTS` -- the requests the hop makes, in the order it makes them
  (7.119.1; RESEARCH-DDG-WEBSEARCH.md 5 R2): :data:`HTML_REQUEST` first, the
  html page's own search form posted the way a browser submits it, then
  :data:`LITE_REQUEST`, the lite page, asked only when the html page held no
  result and no bot check. Each names its endpoint, its method, its fields,
  its headers, and which of MCC's existing options travel as which of the
  page's own form fields (region ``kl`` from ``DDGS_REGION``, time ``df`` from
  ``DDGS_TIMELIMIT``; USER DECISIONS 2026-10-25 Q10). An option that is not
  set is not sent.
* :data:`BROWSER_USER_AGENT` -- the one static, browser-class User-Agent both
  requests send (USER DECISIONS Q12: "a browser-class UA only as declared data
  for the DuckDuckGo profile"). Headers only: no TLS or HTTP/2 fingerprint is
  imitated (Q3), and certificate verification stays the HTTP library's own
  default. ``web_fetch`` keeps its own identity (``api/web_tools/
  constants.py``); this one is the DuckDuckGo hop's alone.
* :data:`PAGE_PROFILES` -- one row per page kind: the result row (alternatives
  in order), the title link, the snippet, the rows that are never results
  (advertisements) and the form that asks for the next page. An answer is
  read first with the row of the page kind its request asked for, then with
  the others; a new page shape is a new row.
* :data:`REDIRECT` and :data:`AD_LINK` -- DuckDuckGo's own click-through
  links: a result's link is a redirect that names the destination, an ad's
  link is a ``y.js`` click link that is never a result.
* :data:`CHALLENGE_STATUSES` and :data:`CHALLENGE_MARKERS` -- what its bot
  check looks like, read only when a page held no result at all (a page of
  results about bot checks carries the same words).

A selector is one element, written ``tag``, ``tag.class``, ``.class``,
``tag#id`` or ``tag[attribute="value"]`` (classes and attributes combine:
``div.result.result--ad``). There is no descendant combinator on purpose: a
row's link and snippet are looked for inside the row, and a row-less page kind
pairs each snippet with the link before it. ``tests/config/
test_duckduckgo_profiles.py`` parses every selector here, so a typo fails CI.

Provenance (USER DECISIONS Q13, the standing licence rule): every value is a
fact about DuckDuckGo's public pages -- a class name, a form field, a status
code, a sentence the page prints -- or about how a browser submits a form, as
recorded in RESEARCH-DDG-WEBSEARCH.md §2.1/§2.4/§5 R2, in the W3C Fetch
Metadata specification and Mozilla's documented User-Agent form, and in the
DDG-1 corpus: the live DuckDuckGo html page octos recorded on 2026-09-28
(Apache-2.0) and MCC's own synthetic lite, html and challenge pages. No code,
selector table or fixture of another project was copied. Leaf module: imports
nothing first-party.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DuckDuckGoRequest:
    """One request the hop can make."""

    #: The page kind its answer is read as first (a :data:`PAGE_PROFILES` row).
    kind: str
    endpoint: str
    #: ``POST`` sends the fields as a form body, ``GET`` as the query string.
    method: str
    #: The form field that carries the search text.
    query_field: str
    #: Fields sent with every request of this kind, as the page's own form
    #: sends them, after the query field.
    fixed_fields: tuple[tuple[str, str], ...]
    #: ``(MCC option, page field)``: the option's value, when it is set, is
    #: sent as that field. The options are the ``ddgs`` provider's own
    #: (``config/websearch_catalog.py``), read the way its adapter reads them.
    option_fields: tuple[tuple[str, str], ...]
    #: The request's own headers, sent as written.
    headers: tuple[tuple[str, str], ...]
    #: Where the request's shape was read.
    source: str


#: Firefox 140 ESR on 64-bit Windows, in the form Mozilla documents for
#: Firefox's User-Agent (``Mozilla/5.0 (<platform>; rv:<version>)
#: Gecko/20100101 Firefox/<version>``). Static on purpose: one identity for
#: every request, never rotated or randomised. A browser release that has
#: moved on is an edit to this line.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
)

#: The ``ddgs`` provider's options each request forwards, as the page's own
#: form fields (USER DECISIONS Q10).
_OPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("DDGS_REGION", "kl"),
    ("DDGS_TIMELIMIT", "df"),
)

HTML_REQUEST = DuckDuckGoRequest(
    kind="html",
    endpoint="https://html.duckduckgo.com/html/",
    method="POST",
    query_field="q",
    # The form's submit button is named ``b`` and its value is empty.
    fixed_fields=(("b", ""),),
    option_fields=_OPTION_FIELDS,
    headers=(
        ("User-Agent", BROWSER_USER_AGENT),
        ("Referer", "https://html.duckduckgo.com/"),
        # A same-origin form submission the user made, navigating to a
        # document (W3C Fetch Metadata Request Headers).
        ("Sec-Fetch-Dest", "document"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Sec-Fetch-Site", "same-origin"),
        ("Sec-Fetch-User", "?1"),
    ),
    source=(
        "RESEARCH-DDG-WEBSEARCH.md 5 R2 (POST html.duckduckgo.com/html/ with "
        "q, b, kl, df; headers Referer, Sec-Fetch-*; a static UA) and the html "
        "page's own search form, posted to /html/ with its q text field, its "
        "b submit button whose value is empty and its kl field "
        "(tests/fixtures/ddg/octos_html_live_2026-09-28.html, recorded live "
        "by octos, Apache-2.0); whether the live form honours kl and df is "
        "UNVERIFIED (no live call)"
    ),
)

LITE_REQUEST = DuckDuckGoRequest(
    kind="lite",
    endpoint="https://lite.duckduckgo.com/lite/",
    method="GET",
    query_field="q",
    fixed_fields=(),
    option_fields=_OPTION_FIELDS,
    headers=(("User-Agent", BROWSER_USER_AGENT),),
    source=(
        "RESEARCH-DDG-WEBSEARCH.md 5 R2 (GET lite.duckduckgo.com/lite/, the "
        "second try when the html page yields nothing and no challenge); the "
        "query string every release before 7.119.1 sent, with the same "
        "static UA as the html request"
    ),
)

#: The hop's requests in the order it makes them: the html form first, the
#: lite page only when the html page held no result and no bot check.
REQUESTS: tuple[DuckDuckGoRequest, ...] = (HTML_REQUEST, LITE_REQUEST)


@dataclass(frozen=True, slots=True)
class DuckDuckGoPageProfile:
    """How one kind of DuckDuckGo results page lays out a result."""

    kind: str
    #: The elements that each hold one whole result, as alternatives tried in
    #: order (the first that reads a result wins), or ``()`` for a page whose
    #: results are not wrapped: there each title link opens a result and the
    #: next snippet in the page belongs to it.
    rows: tuple[str, ...]
    #: The result's title link (its text is the title, its ``href`` the link).
    link: str
    #: The result's snippet (its text); a result without one keeps an empty one.
    snippet: str
    #: Rows that are never results -- advertisements -- skipped whole.
    skip: tuple[str, ...]
    #: The form that asks for the next page of results, and the field that
    #: makes it that form (its hidden result offset). DuckDuckGo shows it only
    #: under results, so a page that carries it but yields no row is a results
    #: page this table can no longer read -- never an empty answer.
    next_page_form: str
    next_page_field: str
    #: Where the structure was read.
    source: str


PAGE_PROFILES: tuple[DuckDuckGoPageProfile, ...] = (
    DuckDuckGoPageProfile(
        kind="html",
        # R2's chain: div.result:not(.result--ad) -- the div.result rows less
        # the div.result--ad rows ``skip`` names -- else div.web-result, a
        # class no ad row carries. Ads stay out by construction either way.
        rows=("div.result", "div.web-result"),
        link="a.result__a",
        snippet=".result__snippet",
        skip=("div.result--ad",),
        next_page_form='form[action="/html/"]',
        next_page_field='input[name="s"]',
        source=(
            "the html page's div.result rows (div.result--ad rows are ads; "
            "organic rows also carry web-result), a.result__a titles, "
            ".result__snippet snippets, and the nav-link form's hidden s "
            "offset (RESEARCH-DDG-WEBSEARCH.md 5 R2; tests/fixtures/ddg/"
            "octos_html_live_2026-09-28.html, recorded live by octos, "
            "Apache-2.0; mcc_html_ads_and_edge_cases.html)"
        ),
    ),
    DuckDuckGoPageProfile(
        kind="lite",
        rows=(),
        link="a.result-link",
        snippet="td.result-snippet",
        skip=(),
        next_page_form='form[action="/lite/"]',
        next_page_field='input[name="s"]',
        source=(
            "the lite page's table rows: a.result-link titles over "
            "//duckduckgo.com/l/?uddg= redirects, td.result-snippet snippets "
            "(RESEARCH-DDG-WEBSEARCH.md 2.1; tests/fixtures/ddg/"
            "mcc_lite_results.html); the next-page form is UNVERIFIED on a "
            "recorded lite page (none in the corpus carries a next page)"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class DuckDuckGoRedirect:
    """A result link that names its destination in a query parameter."""

    host: str
    path: str
    parameter: str


#: ``//duckduckgo.com/l/?uddg=<destination>&rut=<token>``: the destination is
#: the ``uddg`` value, percent-decoded once.
REDIRECT = DuckDuckGoRedirect(host="duckduckgo.com", path="/l/", parameter="uddg")


@dataclass(frozen=True, slots=True)
class DuckDuckGoAdLink:
    """An advertisement's click link: never a result, whatever row holds it."""

    host: str
    path: str


#: ``//duckduckgo.com/y.js?ad_domain=...`` (either scheme form).
AD_LINK = DuckDuckGoAdLink(host="duckduckgo.com", path="/y.js")

#: The status DuckDuckGo serves its bot check with ("accepted", not served).
CHALLENGE_STATUSES: frozenset[int] = frozenset({202})

#: Strings DuckDuckGo's bot check carries (its modal, its script, its form and
#: its heading "Unfortunately, bots use DuckDuckGo too."), matched without
#: regard to case.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "anomaly-modal",
    "anomaly.js",
    "challenge-form",
    "bots use DuckDuckGo",
)
