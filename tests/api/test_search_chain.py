"""The web search chain door (``WEB_SEARCH_CHAIN``, 7.112.0), end to end in process.

Real adapters (Exa, Tavily) over an ``httpx.MockTransport`` -- the adapters'
own request building and status mapping run, nothing leaves the process --
driven through ``outbound._run_web_search`` exactly as the server tool calls
it. The limiter's clock is the parity world's fake one, so a backoff or a
reactive block is recorded instead of slept.

What is pinned here is the walk (spec §3.2) and the decisions the user named
(E3 in the brief): a 401 on key 1 rotates to key 2; a 5xx moves to the next
ENTRY without charging the key; a one-key provider is never benched into an
outage; a 429 benches the key for its Retry-After and the next entry answers;
``FALLBACK_SKIP_KINDS`` ends the route; an unconfigured entry is skipped with
its variable named; a paused entry is skipped; a repeated entry is tried
again; the step-over never skips the last entry; nothing reaches the request's
own ladder; and with no chain set the door is never entered.
"""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from ddgs.exceptions import RatelimitException

from my_claude_code.api.web_tools import outbound, search_books, search_chain
from my_claude_code.api.web_tools import streaming as web_streaming
from my_claude_code.config.settings import Settings
from my_claude_code.core.upstream_ladder import current_ladder, install_ladder_trace
from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from my_claude_code.websearch.errors import (
    WebSearchConfigError,
    WebSearchInvalidRequestError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import (
    SearchOutcome,
    SearchRouteOutcome,
    build_provider,
)
from tests.contracts.search_chat_parity_world import fake_limiter_time
from tests.support.ddg_harness import corpus
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS

EXA_KEY_1 = "exa-chain-key-one-1111"
EXA_KEY_2 = "exa-chain-key-two-2222"
TAVILY_KEY = "tvly-chain-key-aaaa1111"


@dataclass
class Upstream:
    """Scripted answers per (provider, key); every call is recorded."""

    script: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = field(
        default_factory=dict
    )
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def answer(self, provider: str, key: str, *answers: Any) -> None:
        self.script[(provider, key)] = [
            item if isinstance(item, tuple) else (item, {}) for item in answers
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "exa" in host:
            provider, key = "exa", request.headers.get("x-api-key", "")
        else:
            provider = "tavily"
            key = request.headers.get("authorization", "").removeprefix("Bearer ")
        queue = self.script.get((provider, key), [(200, {})])
        status, headers = queue[0] if len(queue) == 1 else queue.pop(0)
        self.calls.append((provider, key, status))
        if status == 200:
            body: dict[str, Any] = {
                "results": [
                    {
                        "title": f"{provider} result",
                        "url": f"https://example.invalid/{provider}",
                        "highlights": ["found"],
                        "content": "found",
                    }
                ]
            }
        else:
            body = {"error": {"message": f"upstream said {status}"}}
        return httpx.Response(
            status, headers=headers, content=json.dumps(body).encode(), request=request
        )


@dataclass
class Recorded:
    rows: list[SearchOutcome] = field(default_factory=list)
    routes: list[SearchRouteOutcome] = field(default_factory=list)

    def chain_rows(self) -> list[tuple[str, int | None, str, str | None]]:
        return [
            (row.provider, row.chain_position, row.status, row.outcome)
            for row in self.rows
        ]

    def tries(self, index: int) -> list[dict[str, Any]]:
        raw = self.rows[index].tries_json
        assert raw is not None
        return json.loads(raw)


@pytest.fixture
def upstream(monkeypatch) -> Upstream:
    world = Upstream()

    async def fake_runtime_provider(settings: Settings, provider_id: str):
        provider = build_provider(settings, provider_id)
        original = provider.__dict__.get("_client")
        if original is not None:
            await original.aclose()
        provider.__dict__["_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(world.handler)
        )
        return provider

    monkeypatch.setattr(search_chain, "runtime_provider", fake_runtime_provider)
    return world


@pytest.fixture
def recorded(monkeypatch) -> Recorded:
    record = Recorded()
    monkeypatch.setattr(search_chain, "emit_search_outcome", record.rows.append)
    monkeypatch.setattr(outbound, "emit_route_outcome", record.routes.append)
    return record


@pytest.fixture(autouse=True)
def _limiter_clock() -> Iterator[None]:
    with fake_limiter_time():
        yield


def _settings(**values: str) -> Settings:
    base = {
        **NO_WEB_SEARCH_CREDENTIALS,
        "EXA_API_KEY": EXA_KEY_1,
        "TAVILY_API_KEY": TAVILY_KEY,
        "WEB_SEARCH_CHAIN": "exa,tavily",
        "PROVIDER_RETRY_ATTEMPTS": "3",
    }
    base.update(values)
    return Settings.model_validate(base)


async def _search(settings: Settings, query: str = "chain query") -> Any:
    return await outbound._run_web_search(query, settings)


# ------------------------------------------------------------------ E3 cases


@pytest.mark.asyncio
async def test_a_401_on_key_one_rotates_to_key_two_and_the_search_succeeds(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(EXA_API_KEY=f"{EXA_KEY_1},{EXA_KEY_2}")
    upstream.answer("exa", EXA_KEY_1, 401)

    results = await _search(settings)

    assert [item["provider"] for item in results] == ["exa"]
    assert upstream.calls == [("exa", EXA_KEY_1, 401), ("exa", EXA_KEY_2, 200)]
    assert recorded.chain_rows() == [("exa", 0, "success", "succeeded")]
    tries = recorded.tries(0)
    assert [(t["key_index"], t["status"], t.get("charged")) for t in tries] == [
        (0, 401, "auth"),
        (1, None, None),
    ]
    assert tries[0]["rotated"] is True
    assert tries[1]["ok"] is True
    books = search_books.cached_search_key_books("exa")
    assert books is not None
    states = [entry["state"] for entry in books.state.get_metrics()]
    assert states == ["LOCKED_OUT", "HEALTHY"]


@pytest.mark.asyncio
async def test_a_5xx_moves_to_the_next_entry_without_charging_the_key(
    upstream: Upstream, recorded: Recorded
) -> None:
    upstream.answer("exa", EXA_KEY_1, 500)

    results = await _search(_settings())

    assert [item["provider"] for item in results] == ["tavily"]
    # PROVIDER_RETRY_ATTEMPTS=3 on the one key, then the next entry.
    assert upstream.calls == [
        ("exa", EXA_KEY_1, 500),
        ("exa", EXA_KEY_1, 500),
        ("exa", EXA_KEY_1, 500),
        ("tavily", TAVILY_KEY, 200),
    ]
    assert recorded.chain_rows() == [
        ("exa", 0, "error", "failed"),
        ("tavily", 1, "success", "succeeded"),
    ]
    assert recorded.rows[0].failure_kind == "upstream"
    tries = recorded.tries(0)
    assert [t["status"] for t in tries] == [500, 500, 500]
    assert tries[-1]["charged"] is None
    assert tries[-1]["rotated"] is False
    books = search_books.cached_search_key_books("exa")
    assert books is not None
    assert books.state.get_metrics()[0]["state"] == "HEALTHY"
    route = recorded.routes[-1]
    assert route.provider_path == ("exa", "tavily")
    assert route.fallback_used is True
    assert route.status == "success"


@pytest.mark.asyncio
async def test_a_one_key_provider_is_not_benched_into_an_outage(
    upstream: Upstream, recorded: Recorded
) -> None:
    """F1: today every later search fails with no call made for 300 s."""
    settings = _settings(WEB_SEARCH_CHAIN="exa")
    upstream.answer("exa", EXA_KEY_1, 401, 200)

    with pytest.raises(Exception, match="upstream said 401"):
        await _search(settings)
    results = await _search(settings, "after the 401")

    assert [item["provider"] for item in results] == ["exa"]
    assert upstream.calls == [("exa", EXA_KEY_1, 401), ("exa", EXA_KEY_1, 200)]


@pytest.mark.asyncio
async def test_a_429_benches_the_key_for_retry_after_and_the_next_entry_answers(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(PROVIDER_RETRY_ATTEMPTS="1")
    upstream.answer("exa", EXA_KEY_1, (429, {"Retry-After": "7"}))

    first = await _search(settings)
    second = await _search(settings, "while exa cools down")

    assert [item["provider"] for item in first] == ["tavily"]
    assert [item["provider"] for item in second] == ["tavily"]
    tries = recorded.tries(0)
    assert tries[0]["status"] == 429
    assert tries[0]["retry_after"] == 7.0
    assert tries[0]["charged"] == "rate_limit"
    assert tries[0]["benched_for_s"] == pytest.approx(7.0, abs=0.5)
    # The second search steps over exa (7 s >= FALLBACK_COOLDOWN_STEP_OVER_FLOOR
    # and it is not the last entry): no call, a skipped row naming the wait.
    assert upstream.calls == [
        ("exa", EXA_KEY_1, 429),
        ("tavily", TAVILY_KEY, 200),
        ("tavily", TAVILY_KEY, 200),
    ]
    skipped = recorded.rows[2]
    assert (skipped.provider, skipped.status, skipped.outcome) == (
        "exa",
        "skipped",
        "skipped",
    )
    assert skipped.error_message is not None
    assert skipped.error_message.startswith("skipped: cooldown ")


@pytest.mark.asyncio
async def test_the_step_over_never_skips_the_last_entry(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(WEB_SEARCH_CHAIN="tavily,exa", PROVIDER_RETRY_ATTEMPTS="1")
    upstream.answer("tavily", TAVILY_KEY, 500)
    upstream.answer("exa", EXA_KEY_1, (429, {"Retry-After": "7"}), 200)

    with pytest.raises(WebSearchRateLimitError):
        await _search(settings)
    results = await _search(settings, "exa is last")

    # exa is benched 7 s but is the last entry: it is tried (after its own
    # limiter window), never skipped into an outage.
    assert [item["provider"] for item in results] == ["exa"]
    assert upstream.calls[-1] == ("exa", EXA_KEY_1, 200)


@pytest.mark.asyncio
async def test_fallback_skip_kinds_end_the_route(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(FALLBACK_SKIP_KINDS="invalid_request,model_rejected")
    upstream.answer("exa", EXA_KEY_1, 400)

    with pytest.raises(WebSearchInvalidRequestError):
        await _search(settings)

    assert upstream.calls == [("exa", EXA_KEY_1, 400)]
    assert recorded.chain_rows() == [("exa", 0, "error", "failed")]
    assert recorded.rows[0].failure_kind == "model_rejected"


@pytest.mark.asyncio
async def test_a_400_moves_on_by_default(
    upstream: Upstream, recorded: Recorded
) -> None:
    """User decision Q5a: each service builds its own request."""
    upstream.answer("exa", EXA_KEY_1, 400)

    results = await _search(_settings())

    assert [item["provider"] for item in results] == ["tavily"]
    assert upstream.calls == [("exa", EXA_KEY_1, 400), ("tavily", TAVILY_KEY, 200)]


@pytest.mark.asyncio
async def test_an_unconfigured_entry_is_skipped_and_names_its_variable(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(WEB_SEARCH_CHAIN="brave,exa")

    results = await _search(settings)

    assert [item["provider"] for item in results] == ["exa"]
    assert recorded.chain_rows() == [
        ("brave", 0, "skipped", "skipped"),
        ("exa", 1, "success", "succeeded"),
    ]
    message = recorded.rows[0].error_message or ""
    assert "BRAVE_SEARCH_API_KEY" in message
    assert message.startswith("skipped: not configured")
    # A skipped entry is not an attempt.
    assert recorded.routes[-1].provider_path == ("exa",)
    assert recorded.routes[-1].attempt_count == 1


@pytest.mark.asyncio
async def test_a_paused_entry_is_skipped(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(WEB_SEARCH_CHAIN_PAUSED="exa")

    results = await _search(settings)

    assert [item["provider"] for item in results] == ["tavily"]
    assert upstream.calls == [("tavily", TAVILY_KEY, 200)]
    assert recorded.chain_rows() == [
        ("exa", 0, "skipped", "skipped"),
        ("tavily", 1, "success", "succeeded"),
    ]
    assert recorded.rows[0].error_message == "skipped: paused"


@pytest.mark.asyncio
async def test_an_all_paused_chain_is_an_error_naming_the_setting(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(WEB_SEARCH_CHAIN_PAUSED="exa,tavily")

    with pytest.raises(WebSearchConfigError, match="WEB_SEARCH_CHAIN_PAUSED"):
        await _search(settings)

    assert upstream.calls == []
    assert [row.status for row in recorded.rows] == ["skipped", "skipped"]


@pytest.mark.asyncio
async def test_a_repeated_entry_is_tried_again_at_its_place(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(WEB_SEARCH_CHAIN="exa,tavily,exa", PROVIDER_RETRY_ATTEMPTS="1")
    upstream.answer("exa", EXA_KEY_1, 500, 200)
    upstream.answer("tavily", TAVILY_KEY, 503)

    results = await _search(settings)

    assert [item["provider"] for item in results] == ["exa"]
    assert upstream.calls == [
        ("exa", EXA_KEY_1, 500),
        ("tavily", TAVILY_KEY, 503),
        ("exa", EXA_KEY_1, 200),
    ]
    assert [row.chain_position for row in recorded.rows] == [0, 1, 2]
    assert recorded.routes[-1].provider_path == ("exa", "tavily", "exa")


@pytest.mark.asyncio
async def test_a_keyless_provider_429_is_not_an_outage(
    monkeypatch, upstream: Upstream, recorded: Recorded
) -> None:
    calls: list[str] = []

    def fake_text_search(self, query: str, max_results: int):
        calls.append(query)
        if len(calls) == 1:
            raise RatelimitException("202 Ratelimit")
        return [{"title": "ddgs", "href": "https://example.invalid/ddgs", "body": "x"}]

    monkeypatch.setattr(DdgsWebSearchProvider, "_run_text_search", fake_text_search)
    settings = _settings(WEB_SEARCH_CHAIN="ddgs", PROVIDER_RETRY_ATTEMPTS="1")

    with pytest.raises(WebSearchRateLimitError):
        await _search(settings)
    results = await _search(settings, "after the ddgs 429")

    assert [item["provider"] for item in results] == ["ddgs"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_the_legacy_entry_answers_after_a_failing_provider(
    monkeypatch, upstream: Upstream, recorded: Recorded
) -> None:
    async def fake_scrape(query: str) -> list[dict[str, str]]:
        return [{"title": "legacy", "url": "https://example.invalid/legacy"}]

    monkeypatch.setattr(outbound, "_legacy_web_search_scrape", fake_scrape)
    upstream.answer("exa", EXA_KEY_1, 500)
    settings = _settings(WEB_SEARCH_CHAIN="exa,legacy", PROVIDER_RETRY_ATTEMPTS="1")

    results = await _search(settings)

    assert results == [
        {
            "title": "legacy",
            "url": "https://example.invalid/legacy",
            "provider": "legacy",
        }
    ]
    assert recorded.chain_rows() == [
        ("exa", 0, "error", "failed"),
        ("legacy", 1, "success", "succeeded"),
    ]


#: DuckDuckGo's lite bot-check page (MCC-own synthetic fixture, DDG-1 corpus).
_LITE_CHALLENGE = corpus.body_after_source_line(
    corpus.DDG_DIR / "mcc_lite_challenge.html"
)


def _real_legacy_hop(
    monkeypatch, upstream: Upstream, lite: httpx.Response | Exception
) -> list[httpx.Request]:
    """The real legacy hop (7.119.0) answered by ``lite``; Exa/Tavily as scripted."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "lite.duckduckgo.com":
            return upstream.handler(request)
        seen.append(request)
        if isinstance(lite, httpx.RequestError):
            raise type(lite)(str(lite), request=request)
        if isinstance(lite, Exception):
            raise lite
        return httpx.Response(
            lite.status_code,
            headers=lite.headers,
            content=lite.content,
            request=request,
        )

    real = httpx.AsyncClient

    class _Routed(real):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(outbound.httpx, "AsyncClient", _Routed)
    return seen


@pytest.mark.asyncio
async def test_the_legacy_hops_bot_check_falls_through_like_its_429(
    monkeypatch, upstream: Upstream, recorded: Recorded
) -> None:
    """7.119.0 (DDG-3): a bot check is no longer a successful empty answer that
    ends the chain; it is the hop's ``rate_limit`` and the next entry answers,
    with exactly the decisions a 429 from the same hop gets (one try per
    ``PROVIDER_RETRY_ATTEMPTS``, the keyless slot cooling down, the next
    search stepping over it)."""

    seen = _real_legacy_hop(
        monkeypatch, upstream, httpx.Response(202, content=_LITE_CHALLENGE)
    )
    settings = _settings(WEB_SEARCH_CHAIN="legacy,exa", PROVIDER_RETRY_ATTEMPTS="1")

    results = await _search(settings)

    assert [item["provider"] for item in results] == ["exa"]
    assert recorded.chain_rows() == [
        ("legacy", 0, "error", "failed"),
        ("exa", 1, "success", "succeeded"),
    ]
    assert recorded.rows[0].failure_kind == "rate_limit"
    assert recorded.rows[0].error_kind == "rate_limit"
    assert "bot check" in (recorded.rows[0].error_message or "")
    assert [(t["status"], t.get("charged")) for t in recorded.tries(0)] == [
        (429, "rate_limit")
    ]
    assert len(seen) == 1

    await _search(settings, "the next search")

    assert recorded.chain_rows()[2:] == [
        ("legacy", 0, "skipped", "skipped"),
        ("exa", 1, "success", "succeeded"),
    ]
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lite", "failure_kind", "try_kind"),
    [
        (
            httpx.Response(403, content=b"Forbidden"),
            "authentication",
            "HTTPStatusError",
        ),
        (httpx.Response(429, content=b"Too Many"), "rate_limit", "HTTPStatusError"),
        (httpx.Response(503, content=b"Unavailable"), "overloaded", "HTTPStatusError"),
        (httpx.ReadTimeout("timed out"), "timeout", "ReadTimeout"),
    ],
)
async def test_the_legacy_hops_http_and_transport_failures_still_reach_the_chain_raw(
    monkeypatch,
    upstream: Upstream,
    recorded: Recorded,
    lite: httpx.Response | Exception,
    failure_kind: str,
    try_kind: str,
) -> None:
    """7.119.0 classifies these only on the route with no chain set: the chain
    keeps reading the hop's own httpx error with the model path's classifier,
    so every decision it made in 7.118.2 is the decision it makes now
    (measured on all sixteen DDG-1 scenarios, both retry settings)."""

    _real_legacy_hop(monkeypatch, upstream, lite)
    settings = _settings(WEB_SEARCH_CHAIN="legacy,exa", PROVIDER_RETRY_ATTEMPTS="1")

    results = await _search(settings)

    assert [item["provider"] for item in results] == ["exa"]
    assert recorded.rows[0].failure_kind == failure_kind
    assert recorded.rows[0].error_kind == "upstream"
    assert [t["kind"] for t in recorded.tries(0)] == [try_kind]


@pytest.mark.asyncio
async def test_the_total_budget_ends_the_route(monkeypatch, recorded: Recorded) -> None:
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"results": []}, request=request)

    async def fake_runtime_provider(settings: Settings, provider_id: str):
        provider = build_provider(settings, provider_id)
        provider.__dict__["_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(slow_handler)
        )
        return provider

    monkeypatch.setattr(search_chain, "runtime_provider", fake_runtime_provider)
    settings = _settings(FALLBACK_TOTAL_TIMEOUT="0.2")

    with pytest.raises(WebSearchUpstreamError, match="FALLBACK_TOTAL_TIMEOUT"):
        await _search(settings)

    statuses = [(row.provider, row.status) for row in recorded.rows]
    assert statuses == [("exa", "error"), ("tavily", "skipped")]


@pytest.mark.asyncio
async def test_disabled_still_disables(upstream: Upstream, recorded: Recorded) -> None:
    settings = _settings(WEB_SEARCH_PROVIDER="disabled")

    with pytest.raises(WebSearchConfigError, match="disabled"):
        await _search(settings)

    assert upstream.calls == []


@pytest.mark.asyncio
async def test_the_provider_and_policy_settings_are_not_read_while_a_chain_is_set(
    upstream: Upstream, recorded: Recorded
) -> None:
    settings = _settings(
        WEB_SEARCH_PROVIDER="tavily", WEB_SEARCH_FALLBACK_POLICY="none"
    )

    await _search(settings)

    assert upstream.calls == [("exa", EXA_KEY_1, 200)]


@pytest.mark.asyncio
async def test_nothing_reaches_the_requests_own_ladder(
    upstream: Upstream, recorded: Recorded
) -> None:
    ladder = install_ladder_trace()
    settings = _settings(EXA_API_KEY=f"{EXA_KEY_1},{EXA_KEY_2}")
    upstream.answer("exa", EXA_KEY_1, 401)

    await _search(settings)

    # The request's own collector is back in place and holds nothing the
    # chain's key tries wrote: they live on the search's attempt row.
    assert current_ladder() is ladder
    assert sum(len(attempt.tries) for attempt in ladder.ladders.values()) == 0
    assert sum(len(attempt.decisions) for attempt in ladder.ladders.values()) == 0
    assert len(recorded.tries(0)) == 2


@pytest.mark.asyncio
async def test_with_no_chain_the_door_is_never_entered(monkeypatch) -> None:
    async def must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the chain door ran with WEB_SEARCH_CHAIN unset")

    async def fake_legacy(query: str) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(outbound, "run_search_chain", must_not_run)
    monkeypatch.setattr(outbound, "_legacy_web_search_scrape", fake_legacy)
    settings = Settings.model_validate(
        {**NO_WEB_SEARCH_CREDENTIALS, "WEB_SEARCH_PROVIDER": "off"}
    )

    assert await outbound._run_web_search("plain", settings) == []


def test_the_pool_sentence_maps_to_the_cause_for_the_client() -> None:
    """F4: a pool benched by a 429 says too_many_requests, not unavailable."""
    error = WebSearchRateLimitError(
        "exa", "All API keys for this provider are in cooldown. Retry in 7s."
    )
    assert web_streaming._web_search_error_code(error) == "too_many_requests"
