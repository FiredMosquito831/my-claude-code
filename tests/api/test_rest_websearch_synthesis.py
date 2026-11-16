"""Integration tests for opt-in synthesize=true grounded LLM answers & telemetry."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools import outbound, search_books, search_chain
from my_claude_code.api.web_tools.search_providers import reset_runtime_providers
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.websearch.analytics import (
    default_websearch_db_path,
    reset_analytics_state,
)
from my_claude_code.websearch.registry import build_provider
from tests.api.support import attempts_as_read, create_test_app
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS


class _FakeSynthProvider(BaseProvider):
    """Fake LLM provider emitting deterministic Anthropic SSE frames or failures."""

    def __init__(
        self,
        chunks: list[str] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(
            ProviderConfig(api_key="synth-test-key", base_url="https://synth.test/v1")
        )
        self._chunks = chunks or []
        self._error = error
        self.calls = 0
        self.requests: list[MessagesRequest] = []

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        return None

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        _ = input_tokens, request_id, reasoning
        self.calls += 1
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        for chunk in self._chunks:
            yield chunk

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"test-synth", "custom-synth"})


def _text_stream(
    answer_text: str,
    *,
    model: str = "test-synth",
    tokens_in: int = 42,
    tokens_out: int = 19,
) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_synth_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": tokens_in, "output_tokens": 0},
                },
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": answer_text},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        **NO_WEB_SEARCH_CREDENTIALS,
        "HOME": str(tmp_path),
        "ENABLE_WEB_SERVER_TOOLS": True,
        "WEB_SEARCH_PROVIDER": "exa",
        "EXA_API_KEY": "exa-test-key-1111",
        "MODEL": "opencode/test-synth",
        "MAX_OUTPUT_TOKENS_FLOOR": 0,
        "REQUEST_LOG_ENABLED": True,
        "WEBSEARCH_LOG_ENABLED": True,
    }
    data.update(overrides)
    return Settings.model_validate(data)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MODEL", "opencode/test-synth")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS_FLOOR", "0")
    set_request_log_path(tmp_path / "requests.db")
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()
    search_books.reset_search_books()
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(reset_runtime_providers())
        loop.close()
    except Exception:
        pass


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    search_handler: Callable[[httpx.Request], httpx.Response],
    synth_provider: _FakeSynthProvider,
) -> Any:
    providers: dict[str, Any] = {}

    async def fake_runtime_provider(cfg: Settings, provider_id: str) -> Any:
        cached = providers.get(provider_id)
        if cached is not None:
            return cached
        prov = build_provider(cfg, provider_id)
        original = prov.__dict__.get("_client")
        if original is not None:
            await original.aclose()
        prov.__dict__["_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(search_handler)
        )
        providers[provider_id] = prov
        return prov

    monkeypatch.setattr(outbound, "runtime_provider", fake_runtime_provider)
    monkeypatch.setattr(search_chain, "runtime_provider", fake_runtime_provider)
    app = create_test_app(settings, providers={"opencode": synth_provider})
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _three_search_results() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "requestId": "exa-synth-1",
            "results": [
                {
                    "title": "Source One",
                    "url": "https://one.example.org/doc",
                    "highlights": ["Fact one from doc one."],
                },
                {
                    "title": "Source Two Uncited",
                    "url": "https://two.example.org/doc",
                    "highlights": ["Distractor fact two."],
                },
                {
                    "title": "Source Three",
                    "url": "https://three.example.org/doc",
                    "highlights": ["Fact three from doc three."],
                },
            ],
        },
    )


def test_synthesize_false_leaves_llm_uncalled_and_logs_local_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    synth = _FakeSynthProvider(_text_stream("Should not run [1]."))
    app = _build_app(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: _three_search_results(),
        synth,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.post("/v1/websearch", json={"query": "no llm"})
    assert resp.status_code == 200
    body = resp.json()
    assert synth.calls == 0
    assert body["synthesis"] is None
    assert body["usage"]["model_tokens_in"] is None
    assert body["usage"]["model_tokens_out"] is None
    assert body["usage"]["model_cost_usd"] is None

    req_id = resp.headers["request-id"]
    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request(req_id)
    assert row is not None
    assert row["is_local"] == 1
    assert row["optimization"] == "web_search"
    assert row["provider"] is None


def test_synthesize_true_grounds_citations_and_logs_single_model_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M1 & M2 guard: exactly one is_local=0 row in requests.db, only cited [N] returned."""
    synth = _FakeSynthProvider(
        _text_stream(
            "Confirmed by [1], repeated [1], and also [3], ignoring out-of-range [9].",
            model="custom-synth",
            tokens_in=85,
            tokens_out=27,
        )
    )
    app = _build_app(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: _three_search_results(),
        synth,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.post(
        "/v1/websearch",
        json={
            "query": "grounded synthesis",
            "synthesize": True,
            "model": "opencode/custom-synth",
            "synthesis_max_tokens": 512,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert synth.calls == 1
    assert synth.requests[0].max_tokens == 512
    assert synth.requests[0].model == "custom-synth"

    synthesis = body["synthesis"]
    assert synthesis["model"] == "custom-synth"
    assert "[1]" in synthesis["answer"] and "[3]" in synthesis["answer"]
    # M2: only indices [1] and [3] actually cited in answer appear; [2] and [9] must NOT!
    assert synthesis["citations"] == [
        {"index": 1, "url": "https://one.example.org/doc", "title": "Source One"},
        {"index": 3, "url": "https://three.example.org/doc", "title": "Source Three"},
    ]

    assert body["usage"]["model_tokens_in"] == 85
    assert body["usage"]["model_tokens_out"] == 27

    # M1: verify exactly 1 row in requests.db with is_local=0 and optimization=None
    req_id = resp.headers["request-id"]
    store = get_request_log_store()
    assert store is not None
    store.close()
    db_path = tmp_path / "requests.db"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests").fetchall()
        attempts = attempts_as_read(conn, req_id)
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["id"] == req_id
    assert row["endpoint"] == "/v1/websearch"
    assert row["protocol"] == "rest_websearch"
    assert row["is_local"] == 0
    assert row["optimization"] is None
    assert row["provider"] == "opencode"
    assert row["resolved_model"] == "custom-synth"
    assert row["tokens_in"] == 85
    assert row["tokens_out"] == 27
    assert row["status"] == "success"
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "succeeded"

    # Search step also logged into websearch.db
    reset_analytics_state()
    ws_db = default_websearch_db_path()
    assert ws_db.exists()
    with sqlite3.connect(ws_db) as ws_conn:
        ws_rows = ws_conn.execute("SELECT provider, status FROM search_log").fetchall()
    assert ws_rows == [("exa", "success")]


def test_synthesize_with_dialect_format_and_error_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    synth_ok = _FakeSynthProvider(
        _text_stream("Tavily grounded [1].", tokens_in=30, tokens_out=12)
    )
    app = _build_app(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: _three_search_results(),
        synth_ok,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    tv = client.post(
        "/v1/websearch",
        json={"query": "tavily synth", "format": "tavily", "synthesize": True},
    )
    assert tv.status_code == 200
    assert tv.json()["answer"] == "Tavily grounded [1]."

    # Search failure when synthesize=True does NOT invoke LLM and logs local error
    synth_unused = _FakeSynthProvider(_text_stream("never"))
    app_search_fail = _build_app(
        monkeypatch,
        _make_settings(tmp_path, ENABLE_WEB_SERVER_TOOLS=False),
        lambda _req: _three_search_results(),
        synth_unused,
    )
    client_search_fail = TestClient(app_search_fail, client=("127.0.0.1", 50000))
    off_resp = client_search_fail.post(
        "/v1/websearch", json={"query": "disabled", "synthesize": True}
    )
    assert off_resp.status_code == 503
    assert synth_unused.calls == 0

    # Synthesis ExecutionFailure returns OpenAI JSON error and records 1 error row
    prev_store = get_request_log_store()
    if prev_store is not None:
        prev_store.close()
    set_request_log_path(tmp_path / "requests_err.db")
    synth_fail = _FakeSynthProvider(
        error=ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            message="Synth model rate limited",
            status_code=429,
            retryable=False,
        )
    )
    app_synth_fail = _build_app(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: _three_search_results(),
        synth_fail,
    )
    client_synth_fail = TestClient(app_synth_fail, client=("127.0.0.1", 50000))
    err_resp = client_synth_fail.post(
        "/v1/websearch", json={"query": "fail synth", "synthesize": True}
    )
    assert err_resp.status_code == 429
    assert err_resp.json()["error"]["type"] == "rate_limit_error"
    err_id = err_resp.headers["request-id"]
    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request(err_id)
    assert row is not None
    assert row["status"] == "error"
    assert row["is_local"] == 0
    assert row["optimization"] is None
