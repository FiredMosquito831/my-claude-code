"""DuckDuckGo's no-JavaScript result pages, declared as data (7.119.0).

The keyless last hop of the web search route (the ``legacy`` entry,
``api/web_tools/duckduckgo_lite.py``) asks DuckDuckGo's lite endpoint for one
page of results and reads the answer. Everything it needs to know about
DuckDuckGo lives in this table, so a markup change is an edit to one row here
and a recorded page under ``tests/fixtures/ddg``, never a change of code:

* :data:`LITE_REQUEST` -- the endpoint, the method, the query field, and which
  of MCC's existing options travel as which of the page's own form fields
  (region ``kl`` from ``DDGS_REGION``, time ``df`` from ``DDGS_TIMELIMIT``;
  RESEARCH-DDG-WEBSEARCH.md USER DECISIONS 2026-10-25 Q10). An option that is
  not set is not sent, so with none set the request is the one every release
  before 7.119.0 made.
* :data:`PAGE_PROFILES` -- one row per page kind: the result row, the title
  link, the snippet, the rows that are never results (advertisements) and the
  form that asks for the next page. A page is read with the first row that
  finds a result in it; a new page shape is a new row.
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
code, a sentence the page prints -- as recorded in RESEARCH-DDG-WEBSEARCH.md
§2.1/§2.4 and in the DDG-1 corpus: the live DuckDuckGo html page octos
recorded on 2026-09-28 (Apache-2.0) and MCC's own synthetic lite, html and
challenge pages. No code, selector table or fixture of another project was
copied. Leaf module: imports nothing first-party.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DuckDuckGoRequest:
    """The one request the hop makes."""

    endpoint: str
    method: str
    #: The form field that carries the search text.
    query_field: str
    #: ``(MCC option, page field)``: the option's value, when it is set, is
    #: sent as that field. The options are the ``ddgs`` provider's own
    #: (``config/websearch_catalog.py``), read the way its adapter reads them.
    option_fields: tuple[tuple[str, str], ...]


LITE_REQUEST = DuckDuckGoRequest(
    endpoint="https://lite.duckduckgo.com/lite/",
    method="GET",
    query_field="q",
    option_fields=(("DDGS_REGION", "kl"), ("DDGS_TIMELIMIT", "df")),
)


@dataclass(frozen=True, slots=True)
class DuckDuckGoPageProfile:
    """How one kind of DuckDuckGo results page lays out a result."""

    kind: str
    #: The element that holds one whole result, or ``None`` for a page whose
    #: results are not wrapped: there each title link opens a result and the
    #: next snippet in the page belongs to it.
    row: str | None
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
        kind="lite",
        row=None,
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
    DuckDuckGoPageProfile(
        kind="html",
        row="div.result",
        link="a.result__a",
        snippet=".result__snippet",
        skip=("div.result--ad",),
        next_page_form='form[action="/html/"]',
        next_page_field='input[name="s"]',
        source=(
            "the html page's div.result rows (div.result--ad rows are ads), "
            "a.result__a titles, .result__snippet snippets, and the nav-link "
            "form's hidden s offset (tests/fixtures/ddg/"
            "octos_html_live_2026-09-28.html, recorded live by octos, "
            "Apache-2.0; mcc_html_ads_and_edge_cases.html)"
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
