"""Track 5 PR2 resilience tests: chain safety, fast rotation, empty-200, quota cooldown, auto cascade, tries_json."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from ddgs.exceptions import RatelimitException

from my_claude_code.api.web_tools import (
    outbound,
    search_books,
    search_chain,
    search_key_health,
    search_providers,
)
from my_claude_code.config.constants import (
    WEB_SEARCH_CHAIN_SKIP_KINDS_DEFAULT,
    WEB_SEARCH_FALLBACK_ON_EMPTY_DEFAULT,
    WEB_SEARCH_QUOTA_COOLDOWN_SECONDS_DEFAULT,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem
from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from my_claude_code.websearch.base import BaseWebSearchProvider, WebSearchProviderConfig
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
    resolve_search_route,
    search_with_logging,
)
from my_claude_code.websearch.rotation import KeyHealthState, KeyPool
from tests.contracts.search_chat_parity_world import fake_limiter_time
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS

EXA_KEY_1 = "exa-resilience-one-1111"
EXA_KEY_2 = "exa-resilience-two-2222"
TAVILY_KEY = "tvly-resilience-aaaa1111"


@dataclass
class ScriptedUpstream:
    """In-process HTTP responder for Exa and Tavily with per-(provider, key) queues."""

    script: dict[
        tuple[str, str], list[tuple[int, dict[str, str], list[dict[str, Any]] | None]]
    ] = field(default_factory=dict)
    calls: list[tuple[str, str, int]] = field(default_factory=list)

    def answer(
        self,
        provider: str,
        key: str,
        *answers: Any,
    ) -> None:
        queued: list[tuple[int, dict[str, str], list[dict[str, Any]] | None]] = []
        for item in answers:
            if isinstance(item, tuple):
                if len(item) == 2:
                    queued.append((item[0], item[1], None))
                else:
                    queued.append((item[0], item[1], item[2]))
            else:
                queued.append((item, {}, None))
        self.script[(provider, key)] = queued

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "exa" in host:
            provider, key = "exa", request.headers.get("x-api-key", "")
        else:
            provider = "tavily"
            key = request.headers.get("authorization", "").removeprefix("Bearer ")
        queue = self.script.get((provider, key), [(200, {}, None)])
        status, headers, custom_results = queue[0] if len(queue) == 1 else queue.pop(0)
        self.calls.append((provider, key, status))
        if status == 200:
            results = (
                custom_results
                if custom_results is not None
                else [
                    {
                        "title": f"{provider} result",
                        "url": f"https://example.invalid/{provider}",
                        "highlights": ["found"],
                        "content": "found",
                    }
                ]
            )
            body: dict[str, Any] = {"results": results}
        else:
            body = {"error": {"message": f"upstream said {status}"}}
        return httpx.Response(status, headers=headers, json=body, request=request)


@dataclass
class RecordedLog:
    rows: list[SearchOutcome] = field(default_factory=list)
    routes: list[SearchRouteOutcome] = field(default_factory=list)

    def outcomes(self) -> list[str | None]:
        return [row.outcome for row in self.rows]

    def providers_and_outcomes(self) -> list[tuple[str, str | None]]:
        return [(row.provider, row.outcome) for row in self.rows]


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[ScriptedUpstream]:
    world = ScriptedUpstream()

    def patched_build(settings: Settings, provider_id: str) -> Any:
        provider = build_provider(settings, provider_id)
        if "_client" in provider.__dict__:
            provider.__dict__["_client"] = httpx.AsyncClient(
                transport=httpx.MockTransport(world.handler)
            )
        return provider

    async def no_legacy(
        query: str, *_args: Any, **_kwargs: Any
    ) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.search_providers.build_provider", patched_build
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", no_legacy
    )
    asyncio.run(search_providers.reset_runtime_providers())
    search_books.reset_search_books()
    with fake_limiter_time():
        yield world
    asyncio.run(search_providers.reset_runtime_providers())
    search_books.reset_search_books()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordedLog:
    log = RecordedLog()
    monkeypatch.setattr(outbound, "emit_search_outcome", log.rows.append)
    monkeypatch.setattr(outbound, "emit_route_outcome", log.routes.append)
    monkeypatch.setattr(search_chain, "emit_search_outcome", log.rows.append)
    monkeypatch.setattr(
        "my_claude_code.websearch.registry._default_recorder",
        lambda: log.rows.append,
    )
    return log


def _settings(**overrides: Any) -> Settings:
    payload: dict[str, Any] = {
        **NO_WEB_SEARCH_CREDENTIALS,
        "EXA_API_KEY": f"{EXA_KEY_1},{EXA_KEY_2}",
        "EXA_API_KEY_ROTATION": "failover",
        "TAVILY_API_KEY": TAVILY_KEY,
        "WEB_SEARCH_CHAIN": "exa,tavily",
        "WEB_SEARCH_PROVIDER": "auto",
        "WEB_SEARCH_FALLBACK_POLICY": "auto",
        "RATE_LIMIT_COOLDOWN_MODE": "fixed",
        "RATE_LIMIT_COOLDOWN_SECONDS": "60",
        "FALLBACK_TOTAL_TIMEOUT": "30",
        "FALLBACK_COOLDOWN_STEP_OVER_FLOOR": "5",
        "PROVIDER_RETRY_ATTEMPTS": "3",
        "PROVIDER_RETRY_BACKOFF_BASE_SECONDS": "2",
        "PROVIDER_RETRY_BACKOFF_MAX_SECONDS": "20",
        "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS": "0",
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def test_settings_defaults_and_limits() -> None:
    assert WEB_SEARCH_CHAIN_SKIP_KINDS_DEFAULT == ""
    assert WEB_SEARCH_QUOTA_COOLDOWN_SECONDS_DEFAULT == 1800.0
    assert WEB_SEARCH_FALLBACK_ON_EMPTY_DEFAULT is True
    defaults = Settings.model_validate(NO_WEB_SEARCH_CREDENTIALS)
    assert defaults.web_search_chain_skip_kinds == ""
    assert defaults.web_search_quota_cooldown_seconds == 1800.0
    assert defaults.web_search_fallback_on_empty is True

    clamped = Settings.model_validate({"WEB_SEARCH_QUOTA_COOLDOWN_SECONDS": "-1"})
    assert clamped.web_search_quota_cooldown_seconds == 0.0


@pytest.mark.asyncio
async def test_empty_web_search_chain_raises_clean_config_error(
    upstream: ScriptedUpstream, recorded: RecordedLog
) -> None:
    """Empty WEB_SEARCH_CHAIN must raise 'has no entries configured', not paused."""
    trace = outbound._SearchRouteTrace(
        route_id="empty-chain",
        ts_epoch=0.0,
        query="q",
        started=0.0,
    )
    with pytest.raises(
        WebSearchConfigError, match="has no entries configured"
    ) as exc_info:
        await search_chain.run_search_chain(
            "q",
            _settings(WEB_SEARCH_CHAIN=" , "),
            trace,
            legacy_scrape=lambda _q: asyncio.sleep(0, result=[]),
            response_items=lambda _r: [],
        )
    assert "paused" not in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_trailing_unconfigured_entry_does_not_starve_cooled_down_provider(
    upstream: ScriptedUpstream, recorded: RecordedLog
) -> None:
    """Item 1: trailing unconfigured entry must never make cooled-down earlier entry step over."""
    settings = _settings(
        WEB_SEARCH_CHAIN="exa,brave",
        EXA_API_KEY=EXA_KEY_1,
        TAVILY_API_KEY="",
        BRAVE_API_KEY="",
        PROVIDER_RETRY_ATTEMPTS="1",
    )
    upstream.answer("exa", EXA_KEY_1, (429, {"Retry-After": "12"}), 200)

    with pytest.raises(WebSearchRateLimitError):
        await outbound._run_web_search("first", settings)

    # Second search: exa is cooling down (12s >= floor 5s), brave is unconfigured.
    # Exa must not step over into a skip row or total starvation; it runs directly.
    recorded.rows.clear()
    results = await outbound._run_web_search("second", settings)
    assert [item["provider"] for item in results] == ["exa"]
    assert ("exa", "skipped") not in recorded.providers_and_outcomes()


@pytest.mark.asyncio
async def test_runtime_config_error_retries_stepped_over_entry(
    upstream: ScriptedUpstream, recorded: RecordedLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a configured trailing entry skips dynamically at call time, retry stepped-over entry."""
    bench_settings = _settings(
        WEB_SEARCH_CHAIN="exa",
        EXA_API_KEY=EXA_KEY_1,
        PROVIDER_RETRY_ATTEMPTS="1",
    )
    upstream.answer("exa", EXA_KEY_1, (429, {"Retry-After": "12"}), 200)

    with pytest.raises(WebSearchRateLimitError):
        await outbound._run_web_search("first", bench_settings)

    chain_settings = _settings(
        WEB_SEARCH_CHAIN="exa,tavily",
        EXA_API_KEY=EXA_KEY_1,
        PROVIDER_RETRY_ATTEMPTS="1",
    )
    orig_run_provider = search_chain._run_provider

    async def dynamic_config_skip(
        books: Any, provider: Any, query: str, **kwargs: Any
    ) -> search_chain.EntryResult:
        if books.provider_id == "tavily":
            return search_chain.EntryResult(skipped="not configured: runtime check")
        return await orig_run_provider(books, provider, query, **kwargs)

    monkeypatch.setattr(search_chain, "_run_provider", dynamic_config_skip)
    results = await outbound._run_web_search("second", chain_settings)
    assert [item["provider"] for item in results] == ["exa"]


@pytest.mark.asyncio
async def test_t5_q1_decoupled_skip_kinds_from_llm(
    upstream: ScriptedUpstream, recorded: RecordedLog
) -> None:
    """T5-Q1: FALLBACK_SKIP_KINDS never aborts WEB_SEARCH_CHAIN; WEB_SEARCH_CHAIN_SKIP_KINDS does."""
    upstream.answer("exa", EXA_KEY_1, 400)
    results = await outbound._run_web_search(
        "q",
        _settings(
            EXA_API_KEY=EXA_KEY_1, FALLBACK_SKIP_KINDS="invalid_request,model_rejected"
        ),
    )
    assert [item["provider"] for item in results] == ["tavily"]

    with pytest.raises(WebSearchInvalidRequestError):
        await outbound._run_web_search(
            "q",
            _settings(
                EXA_API_KEY=EXA_KEY_1, WEB_SEARCH_CHAIN_SKIP_KINDS="model_rejected"
            ),
        )


@pytest.mark.asyncio
async def test_t5_q2_multi_key_fast_rotation_and_unexpected_exception(
    upstream: ScriptedUpstream, recorded: RecordedLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5-Q2: non-last key tries once on 429/503/504 before rotating; unexpected Exception rotates."""
    # Case 1: key 1 returns 429, rotates after 1 try to key 2 without sleeping on key 1.
    settings = _settings(WEB_SEARCH_CHAIN="exa,tavily", PROVIDER_RETRY_ATTEMPTS="3")
    upstream.answer("exa", EXA_KEY_1, (429, {"Retry-After": "9"}))
    upstream.answer("exa", EXA_KEY_2, 200)
    results = await outbound._run_web_search("fast429", settings)
    assert [item["provider"] for item in results] == ["exa"]
    assert upstream.calls == [("exa", EXA_KEY_1, 429), ("exa", EXA_KEY_2, 200)]

    # Case 2: fresh pool — key 1 hits 503, rotates after 1 try to key 2 (504 -> 500 -> 200 retries on last key).
    upstream.calls.clear()
    search_books.reset_search_books()
    upstream.answer("exa", EXA_KEY_1, 503)
    upstream.answer("exa", EXA_KEY_2, 504, 200)
    results = await outbound._run_web_search("fast503", settings)
    assert [item["provider"] for item in results] == ["exa"]
    assert upstream.calls == [
        ("exa", EXA_KEY_1, 503),
        ("exa", EXA_KEY_2, 504),
        ("exa", EXA_KEY_2, 200),
    ]

    # Case 3: unexpected non-WebSearchError Exception rotates from key 0 to key 1; CancelledError propagates.
    books = await search_books.search_key_books(
        settings, build_provider(settings, "exa")
    )

    async def boom_on_key0(index: int, _key: str) -> str:
        if index == 0:
            raise RuntimeError("unexpected decoder fault")
        return "ok-from-key1"

    with fake_limiter_time():
        res = await search_chain.drive_keys(books, boom_on_key0)
    assert res.ok and res.value == "ok-from-key1"
    assert res.key_index == 1

    async def cancel_immediately(_index: int, _key: str) -> str:
        raise asyncio.CancelledError("cancelled")

    with pytest.raises(asyncio.CancelledError):
        await search_chain.drive_keys(books, cancel_immediately)


@pytest.mark.asyncio
async def test_t5_q2_rate_limited_ddgs_steps_over_without_sleeping(
    upstream: ScriptedUpstream, recorded: RecordedLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ddgs throttle_remaining() > 0 steps over immediately without wait_if_blocked() sleep."""
    monkeypatch.setattr(
        DdgsWebSearchProvider,
        "_run_text_search",
        lambda *_a, **_kw: (_ for _ in ()).throw(RatelimitException("ddgs blocked")),
    )
    settings = _settings(
        WEB_SEARCH_CHAIN="ddgs,exa",
        EXA_API_KEY=EXA_KEY_1,
        RATE_LIMIT_COOLDOWN_SECONDS="2",  # < default floor (5.0s)
        FALLBACK_COOLDOWN_STEP_OVER_FLOOR="5",
        PROVIDER_RETRY_ATTEMPTS="1",
    )
    upstream.answer("exa", EXA_KEY_1, 200)

    # First query: ddgs raises rate limit -> blocked 2s; exa answers.
    res1 = await outbound._run_web_search("first", settings)
    assert [item["provider"] for item in res1] == ["exa"]

    # Second query: ddgs limiter has 2s (< floor 5s). Keyless entry still steps over cleanly
    # without waiting in wait_if_blocked() when exa follows!
    res2 = await outbound._run_web_search("second", settings)
    assert [item["provider"] for item in res2] == ["exa"]
    assert any(
        row.provider == "ddgs"
        and row.outcome == "skipped"
        and (row.error_message or "").startswith("skipped: cooldown ")
        for row in recorded.rows
    )


@pytest.mark.asyncio
async def test_t5_q5_quota_cooldown_seconds_respected_and_resettable(
    upstream: ScriptedUpstream, recorded: RecordedLog
) -> None:
    """T5-Q5: quota 402/432/433 benches for WEB_SEARCH_QUOTA_COOLDOWN_SECONDS (default 1800s), resettable."""
    settings = _settings(
        WEB_SEARCH_CHAIN="exa,tavily",
        WEB_SEARCH_QUOTA_COOLDOWN_SECONDS="1800",
        RATE_LIMIT_COOLDOWN_SECONDS="15",
        PROVIDER_RETRY_ATTEMPTS="1",
    )
    upstream.answer("exa", EXA_KEY_1, 402)
    upstream.answer("exa", EXA_KEY_2, 200)
    results = await outbound._run_web_search("quota", settings)
    assert [item["provider"] for item in results] == ["exa"]

    provider = build_provider(settings, "exa")
    books = await search_books.search_key_books(settings, provider)
    assert books.state.get_metrics()[0]["cooldown_remaining"] == pytest.approx(
        1800.0, abs=5.0
    )
    _, facts = search_key_health.books_health(books)
    assert facts[0]["reason"] == "out_of_credits"
    # 0 disables quota cooldown on non-chain KeyPool too:
    zero_pool = KeyPool(("k0",), policy="failover", quota_cooldown_seconds=0.0)
    zero_pool.report_failure(0, kind="quota")
    assert zero_pool.health_at(0).state is KeyHealthState.HEALTHY

    # Reset restores key 0 cleanly.
    await books.state.reset_key(0)
    assert books.state.get_metrics()[0]["cooldown_remaining"] == 0.0

    # Custom quota cooldown = 90s while 429 stays on rate_limit_cooldown_seconds = 15s
    custom = _settings(
        WEB_SEARCH_QUOTA_COOLDOWN_SECONDS="90",
        RATE_LIMIT_COOLDOWN_SECONDS="15",
        EXA_API_KEY=f"{EXA_KEY_1},{EXA_KEY_2}",
    )
    search_books.reset_search_books()
    upstream.answer("exa", EXA_KEY_1, 402)
    upstream.answer("exa", EXA_KEY_2, (429, {}))
    upstream.answer("tavily", TAVILY_KEY, 200)
    res2 = await outbound._run_web_search("custom", custom)
    assert [item["provider"] for item in res2] == ["tavily"]
    custom_books = await search_books.search_key_books(
        custom, build_provider(custom, "exa")
    )
    assert custom_books.state.get_metrics()[0]["cooldown_remaining"] == pytest.approx(
        90.0, abs=5.0
    )
    assert custom_books.state.get_metrics()[1]["cooldown_remaining"] == pytest.approx(
        15.0, abs=5.0
    )


@pytest.mark.asyncio
async def test_t5_q3_fallback_on_empty_chain_and_clean_empty_return(
    upstream: ScriptedUpstream, recorded: RecordedLog
) -> None:
    """T5-Q3: empty 200 falls back without charging health, emits outcome='empty', returns clean [] if all fail/empty."""
    settings = _settings(WEB_SEARCH_CHAIN="exa,tavily", EXA_API_KEY=EXA_KEY_1)
    upstream.answer("exa", EXA_KEY_1, (200, {}, []))
    upstream.answer("tavily", TAVILY_KEY, 200)

    res = await outbound._run_web_search("empty_then_ok", settings)
    assert [item["provider"] for item in res] == ["tavily"]
    assert recorded.providers_and_outcomes() == [
        ("exa", "empty"),
        ("tavily", "succeeded"),
    ]
    # Health unpenalized: exa key 0 remains healthy.
    books = await search_books.search_key_books(
        settings, build_provider(settings, "exa")
    )
    assert books.state.get_metrics()[0]["cooldown_remaining"] == 0.0

    # If all remaining entries fail with upstream 503 after an empty 200, return clean [].
    recorded.rows.clear()
    upstream.answer("exa", EXA_KEY_1, (200, {}, []))
    upstream.answer("tavily", TAVILY_KEY, 503)
    res_clean = await outbound._run_web_search("empty_then_fail", settings)
    assert res_clean == []
    assert recorded.providers_and_outcomes() == [("exa", "empty"), ("tavily", "failed")]

    # With WEB_SEARCH_FALLBACK_ON_EMPTY=false, stops on first empty 200 without calling tavily.
    recorded.rows.clear()
    upstream.calls.clear()
    upstream.answer("exa", EXA_KEY_1, (200, {}, []))
    res_off = await outbound._run_web_search(
        "empty_off",
        _settings(
            WEB_SEARCH_CHAIN="exa,tavily",
            EXA_API_KEY=EXA_KEY_1,
            WEB_SEARCH_FALLBACK_ON_EMPTY="false",
        ),
    )
    assert res_off == []
    assert upstream.calls == [("exa", EXA_KEY_1, 200)]


@pytest.mark.asyncio
async def test_t5_q6_q7_auto_cascade_order_and_empty_non_chain_vs_strict_override(
    upstream: ScriptedUpstream, recorded: RecordedLog
) -> None:
    """T5-Q6/Q7: auto cascades configured auto_select=True providers -> ddgs -> legacy; explicit overrides stay strict."""
    settings = _settings(
        WEB_SEARCH_CHAIN="",
        WEB_SEARCH_PROVIDER="auto",
        WEB_SEARCH_FALLBACK_POLICY="auto",
        CONTEXT7_API_KEY="ctx7-should-skip",
        DEEPSEEK_SEARCH_API_KEY="ds-should-skip",
    )
    route = resolve_search_route(settings)
    assert "exa" in route.provider_ids and "tavily" in route.provider_ids
    assert "ddgs" in route.provider_ids and route.use_legacy_scrape is True
    assert route.provider_ids.index("exa") < route.provider_ids.index("tavily")
    assert "context7" not in route.provider_ids
    assert "deepseek" not in route.provider_ids

    # Named + auto remains strict, explicit override stays strict.
    strict_named = resolve_search_route(
        _settings(WEB_SEARCH_CHAIN="", WEB_SEARCH_PROVIDER="exa")
    )
    assert (
        strict_named.provider_ids == ("exa",)
        and strict_named.use_legacy_scrape is False
    )
    explicit = resolve_search_route(
        settings.model_copy(update={"web_search_provider": "tavily"})
    )
    assert explicit.provider_ids == ("tavily",) and explicit.use_legacy_scrape is False

    # Non-chain auto fallback on empty 200 -> cascades exa empty -> tavily ok!
    upstream.answer("exa", EXA_KEY_1, (200, {}, []))
    upstream.answer("tavily", TAVILY_KEY, 200)
    res_auto = await outbound._run_web_search("non_chain_empty", settings)
    assert [item["provider"] for item in res_auto] == ["tavily"]
    assert ("exa", "empty") in recorded.providers_and_outcomes()

    # Explicit ?provider=exa with empty 200 NEVER falls back!
    upstream.calls.clear()
    upstream.answer("exa", EXA_KEY_1, (200, {}, []))
    _resp, items, _meta = await outbound._run_web_search_response(
        "strict_override_empty",
        settings,
        provider_override="exa",
    )
    assert items == []
    assert [p for p, _k, _s in upstream.calls] == ["exa"]


@pytest.mark.asyncio
async def test_outbound_unexpected_exception_falls_back_in_non_chain(
    upstream: ScriptedUpstream, recorded: RecordedLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 7: _provider_web_search catches unexpected non-WebSearchError Exception and falls back."""
    settings = _settings(
        WEB_SEARCH_CHAIN="",
        WEB_SEARCH_PROVIDER="auto",
        WEB_SEARCH_FALLBACK_POLICY="auto",
    )
    call_count = 0
    real_swl = outbound.search_with_logging

    async def flaky_swl(provider: Any, query: str, **kwargs: Any) -> Any:
        nonlocal call_count
        if provider.PROVIDER_ID == "exa":
            call_count += 1
            raise RuntimeError("unexpected non-WebSearchError")
        return await real_swl(provider, query, **kwargs)

    monkeypatch.setattr(outbound, "search_with_logging", flaky_swl)
    upstream.answer("tavily", TAVILY_KEY, 200)
    res = await outbound._run_web_search("unexpected_exc", settings)
    assert call_count == 1
    assert [item["provider"] for item in res] == ["tavily"]


@pytest.mark.asyncio
async def test_no_chain_multi_key_tries_json_logged() -> None:
    """§5.2: no-chain multi-key search_with_logging emits tries_json on search_log rows."""

    class _TwoKeyFlakyProvider(BaseWebSearchProvider):
        PROVIDER_ID = "exa"
        SUPPORTS_DOMAINS = False

        async def _search_with_key(
            self,
            query: str,
            key: str,
            key_index: int,
            *,
            max_results: int,
            allowed_domains: tuple[str, ...],
            blocked_domains: tuple[str, ...],
        ) -> WebSearchResponse:
            if key_index == 0:
                raise WebSearchUpstreamError("exa", "server down", status_code=503)
            return WebSearchResponse(
                provider="exa",
                query=query,
                key_index=key_index,
                cost_usd=None,
                results=(
                    WebSearchResultItem(
                        title="ok",
                        url="https://example.invalid/ok",
                        snippet="hi",
                        content="hi",
                        published=None,
                    ),
                ),
            )

    provider = _TwoKeyFlakyProvider(
        WebSearchProviderConfig(
            api_keys=(EXA_KEY_1, EXA_KEY_2),
            credential_rotation="failover",
            base_url=None,
            proxy=None,
            http_timeout=20.0,
        )
    )
    outcomes: list[SearchOutcome] = []
    resp = await search_with_logging(provider, "multi_key", recorder=outcomes.append)
    assert len(resp.results) == 1
    assert len(outcomes) == 1
    assert outcomes[0].tries_json is not None
    tries = json.loads(outcomes[0].tries_json)
    assert len(tries) == 2
    assert (
        tries[0]["key_index"] == 0
        and tries[0]["ok"] is False
        and tries[0]["rotated"] is True
    )
    assert tries[1]["key_index"] == 1 and tries[1]["ok"] is True
