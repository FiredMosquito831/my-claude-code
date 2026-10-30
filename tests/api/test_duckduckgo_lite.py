"""The keyless DuckDuckGo hop re-implemented as declared data (7.119.0, DDG-3).

Everything here runs over the DDG-1 corpus (``tests/fixtures/ddg``) or a page
written in the test, through an ``httpx.MockTransport``: nothing leaves the
process (and the suite's network gate would fail a test that tried).

What is pinned: the page profiles read the recorded pages -- titles, the
destination decoded once, ads skipped, snippets; a bot check with no result is
a ``rate_limit`` failure that names what was seen, never "no results"; a page
that carries DuckDuckGo's next-page form but yields no row is a failure, not
an empty answer; a genuinely empty page stays an honest empty answer; the
pre-7.119.0 anchor scan is still the last fallback; ``kl`` / ``df`` reach the
request exactly when ``DDGS_REGION`` / ``DDGS_TIMELIMIT`` are set; the request
carries MCC's product User-Agent; ``WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS``
spaces only this hop's requests and 0 never waits; and with no chain set an
HTTP error status is worded through the shared status mapping.
"""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from my_claude_code.api.web_tools import duckduckgo_lite, outbound
from my_claude_code.api.web_tools import streaming as web_streaming
from my_claude_code.api.web_tools.constants import (
    _SEARCH_HOP_HTTP_HEADERS,
    _WEB_TOOL_HTTP_HEADERS,
)
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

PAGE_URL = "https://lite.duckduckgo.com/lite/?q=omarchy+linux"
QUERY = "omarchy linux"


def _page(name: str) -> str:
    return corpus.body_after_source_line(corpus.DDG_DIR / name).decode("utf-8")


def _answer(
    text: str, *, status: int = 200, headers: dict[str, str] | None = None
) -> list[dict[str, str]]:
    return duckduckgo_lite.read_answer(
        status=status,
        text=text,
        page_url=PAGE_URL,
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
    assert all(row["snippet"] for row in results)
    assert results[2] == {
        "title": "Omarchy - Wikipedia",
        "url": "https://en.wikipedia.org/wiki/Omarchy",
        "snippet": (
            "Omarchy is an open-source Linux distribution created by David "
            "Heinemeier Hansson. It is based on Arch Linux and uses the Hyprland "
            "tiling Wayland compositor and Quickshell desktop shell. [3][4][5]"
        ),
    }


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
    results = _answer(_page("mcc_lite_results.html"))

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
        duckduckgo_lite.destination(href, PAGE_URL) == "https://example.org/caf%C3%A9"
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
    assert duckduckgo_lite.destination(href, PAGE_URL) is None


def test_a_relative_link_is_made_absolute_against_the_page() -> None:
    assert (
        duckduckgo_lite.destination("//example.org/a?b=1", PAGE_URL)
        == "https://example.org/a?b=1"
    )


# ---------------------------------------------------------- classification


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
    name: str, status: int
) -> None:
    with pytest.raises(WebSearchRateLimitError) as raised:
        _answer(_page(name), status=status)

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
    assert len(_answer(page, status=200)) == 3
    assert len(_answer(page, status=202)) == 3


@pytest.mark.parametrize(
    "text",
    ["", "<html><body>nothing here</body></html>", "\x00\x01 not html at all"],
)
def test_a_page_with_no_result_and_no_check_is_an_honest_empty_answer(
    text: str,
) -> None:
    assert _answer(text) == []


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


def test_the_old_anchor_scan_is_still_the_last_fallback() -> None:
    """A layout no profile knows degrades to the pre-7.119.0 answer, never below."""

    page = (
        "<html><body><div class='renamed-row'>"
        "<a class='renamed-link' href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fx"
        "&amp;rut=1'>Renamed layout</a></div></body></html>"
    )
    assert _answer(page) == [
        {"title": "Renamed layout", "url": "https://example.org/x"}
    ]


# ------------------------------------------------------------- the request


def test_with_no_option_set_the_request_is_the_old_one() -> None:
    assert duckduckgo_lite.request_params(QUERY, {}) == {"q": QUERY}


def test_region_and_time_travel_as_kl_and_df() -> None:
    options = {"DDGS_REGION": "uk-en", "DDGS_TIMELIMIT": "w", "DDGS_BACKEND": "bing"}
    assert duckduckgo_lite.request_params(QUERY, options) == {
        "q": QUERY,
        "kl": "uk-en",
        "df": "w",
    }
    assert duckduckgo_lite.request_params(QUERY, {"DDGS_REGION": "  "}) == {"q": QUERY}


class _Upstream:
    """The lite endpoint, scripted: every request recorded, one answer each."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(
            answer.status_code,
            headers=answer.headers,
            content=answer.content,
            request=request,
        )


@pytest.fixture
def lite(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Upstream]:
    real = httpx.AsyncClient

    def install(*answers: httpx.Response | Exception) -> _Upstream:
        upstream = _Upstream(*answers)

        class _MockedClient(real):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(upstream.handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(outbound.httpx, "AsyncClient", _MockedClient)
        return upstream

    return install


def _ok(name: str = "mcc_lite_results.html", status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=_page(name).encode("utf-8"))


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
async def test_the_hop_asks_exactly_what_it_always_asked_when_nothing_is_set(
    lite: Callable[..., _Upstream], clean_options: None
) -> None:
    upstream = lite(_ok())

    results = await outbound._legacy_web_search_scrape(QUERY)

    assert len(results) == 3
    (request,) = upstream.requests
    assert request.method == "GET"
    assert str(request.url) == "https://lite.duckduckgo.com/lite/?q=omarchy+linux"


@pytest.mark.asyncio
async def test_kl_and_df_reach_the_request_exactly_as_declared(
    lite: Callable[..., _Upstream],
    clean_options: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DDGS_REGION", "uk-en")
    monkeypatch.setenv("DDGS_TIMELIMIT", "w")
    upstream = lite(_ok())

    await outbound._legacy_web_search_scrape(QUERY)

    (request,) = upstream.requests
    assert str(request.url) == (
        "https://lite.duckduckgo.com/lite/?q=omarchy+linux&kl=uk-en&df=w"
    )


@pytest.mark.asyncio
async def test_the_hop_says_it_is_mcc_never_a_browser(
    lite: Callable[..., _Upstream], clean_options: None
) -> None:
    upstream = lite(_ok())

    await outbound._legacy_web_search_scrape(QUERY)

    agent = upstream.requests[0].headers["user-agent"]
    assert agent == f"my-claude-code/{package_version()}"
    assert agent == _SEARCH_HOP_HTTP_HEADERS["User-Agent"]
    assert "Mozilla" not in agent
    # web_fetch keeps its own headers until its own release (DDG-4).
    assert _WEB_TOOL_HTTP_HEADERS["User-Agent"].startswith("Mozilla/5.0 compatible;")


@pytest.mark.asyncio
async def test_an_http_error_status_leaves_the_hop_raw_as_before(
    lite: Callable[..., _Upstream], clean_options: None
) -> None:
    """The chain's legacy entry classifies these itself; their shape is unchanged."""

    lite(httpx.Response(403, content=b"Forbidden"))

    with pytest.raises(httpx.HTTPStatusError, match="403 Forbidden"):
        await outbound._legacy_web_search_scrape(QUERY)


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
    lite: Callable[..., _Upstream],
    clean_options: None,
    status: int,
    headers: dict[str, str],
    error_type: type[Exception],
    summary: str,
) -> None:
    lite(httpx.Response(status, headers=headers, content=b"<html>error page</html>"))
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


@pytest.mark.asyncio
async def test_with_no_chain_the_bot_check_is_never_no_results(
    lite: Callable[..., _Upstream], clean_options: None
) -> None:
    lite(_ok("mcc_lite_challenge.html", status=202))
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
    lite: Callable[..., _Upstream], clean_options: None
) -> None:
    lite(httpx.Response(200, content=b"<html><body>nothing here</body></html>"))
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
    lite: Callable[..., _Upstream],
    clean_options: None,
    fake_pacing: _FakeTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBSEARCH_LEGACY_MIN_INTERVAL_SECONDS", "2.5")
    upstream = lite(_ok())

    await outbound._legacy_web_search_scrape(QUERY)
    await outbound._legacy_web_search_scrape(QUERY)

    assert len(upstream.requests) == 2
    assert fake_pacing.slept == [2.5]


@pytest.mark.asyncio
async def test_by_default_the_hop_sends_back_to_back(
    lite: Callable[..., _Upstream], clean_options: None, fake_pacing: _FakeTime
) -> None:
    upstream = lite(_ok())

    for _ in range(3):
        await outbound._legacy_web_search_scrape(QUERY)

    assert len(upstream.requests) == 3
    assert fake_pacing.slept == []


def test_the_interval_delays_only_the_lite_hop(
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
