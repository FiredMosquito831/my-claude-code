"""End-to-end tests for opt-in multi-tool /v1/messages and IV.18 fast path parity."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools.outbound import WebSearchRouteMeta
from my_claude_code.api.web_tools.tool_loop import (
    SYNTHETIC_WEB_FETCH_TOOL_NAME,
    SYNTHETIC_WEB_SEARCH_TOOL_NAME,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem
from my_claude_code.websearch.analytics import reset_analytics_state
from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _isolated_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/test-sonnet")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS_FLOOR", "0")
    set_request_log_path(tmp_path / "requests.db")
    reset_analytics_state()
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()


def _sse_tool_turn(tool_id: str, name: str, payload: dict[str, Any]) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_step",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-sonnet",
                    "content": [],
                    "usage": {"input_tokens": 30, "output_tokens": 0},
                },
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {},
                },
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(payload),
                },
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 10},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _sse_text_turn(text: str) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_end",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-sonnet",
                    "content": [],
                    "usage": {"input_tokens": 50, "output_tokens": 0},
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
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 20},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


class _ScriptedMessagesProvider:
    def __init__(self, turns: list[list[str]]) -> None:
        self._turns = turns
        self.preflight_stream = MagicMock()
        self.requests: list[Any] = []

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data: Any, **_kwargs: Any) -> Any:
        idx = len(self.requests)
        self.requests.append(request_data)
        for chunk in self._turns[idx]:
            yield chunk


def _fake_search_response(query: str, *_args: Any, **_kw: Any) -> Any:
    item = WebSearchResultItem(
        title="FastAPI Docs",
        url="https://fastapi.tiangolo.com/",
        snippet="Modern Python web framework",
        content=None,
        published=None,
    )
    resp = WebSearchResponse(
        provider="exa", query=query, results=(item,), key_index=0, cost_usd=None
    )
    items = [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "content": "",
            "published": "",
            "answer": "",
            "provider": "exa",
        }
    ]
    meta = WebSearchRouteMeta(
        route_id="route_1",
        primary_provider="exa",
        terminal_provider="exa",
        providers_tried=("exa",),
        fallback_used=False,
        supports_domain_filters=True,
    )
    return resp, items, meta


def test_single_tool_fast_path_stays_on_streaming_py_even_when_multi_tool_on() -> None:
    """IV.18: single-tool web_search stays on fast intercept and never hits LLM."""
    provider = _ScriptedMessagesProvider([_sse_text_turn("unexpected")])
    app = create_test_app(providers={})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "FastAPI docs"}],
                "tools": [{"name": "web_search", "type": "web_search_20250305"}],
                "tool_choice": {"type": "auto"},
            },
        )
    assert resp.status_code == 200
    assert provider.requests == []
    events = parse_sse_text(resp.text)
    assert events[0].data["message"]["model"] == "sonnet"


def test_mixed_multi_tool_messages_search_and_fetch_stream_and_non_stream(
    tmp_path: Path,
) -> None:
    provider = _ScriptedMessagesProvider(
        [
            _sse_tool_turn(
                "toolu_s", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "fastapi"}
            ),
            _sse_tool_turn(
                "toolu_f",
                SYNTHETIC_WEB_FETCH_TOOL_NAME,
                {"url": "https://fastapi.tiangolo.com/"},
            ),
            _sse_text_turn(
                "FastAPI is at [FastAPI Docs](https://fastapi.tiangolo.com/)."
            ),
        ]
    )
    app = create_test_app(providers={})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
            return_value=(
                {
                    "url": "https://fastapi.tiangolo.com/",
                    "title": "FastAPI Docs",
                    "media_type": "text/plain",
                    "data": "FastAPI guide text",
                },
                "basic",
                False,
            ),
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 128,
                "stream": False,
                "messages": [{"role": "user", "content": "Explain FastAPI"}],
                "tools": [
                    {
                        "name": "calc",
                        "description": "Calculator",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                    {"name": "web_search", "type": "web_search_20250305"},
                    {"name": "web_fetch", "type": "web_fetch_20250910"},
                ],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    # Requested alias preserved on response model (not rewritten downstream name)!
    assert body["model"] == "sonnet"
    block_types = [b["type"] for b in body["content"]]
    assert block_types == [
        "server_tool_use",
        "web_search_tool_result",
        "server_tool_use",
        "web_fetch_tool_result",
        "text",
    ]
    assert body["content"][0]["input"] == {"query": "fastapi"}
    assert body["content"][2]["input"] == {"url": "https://fastapi.tiangolo.com/"}
    assert body["content"][-1]["citations"][0]["type"] == "web_search_result_location"
    assert body["usage"]["server_tool_use"] == {
        "web_search_requests": 1,
        "web_fetch_requests": 1,
    }
    # Caller's custom tool remained first in upstream tools[].
    assert [t.name for t in provider.requests[0].tools] == [
        "calc",
        SYNTHETIC_WEB_SEARCH_TOOL_NAME,
        SYNTHETIC_WEB_FETCH_TOOL_NAME,
    ]
    # Logged cleanly as is_local=0.
    store = get_request_log_store()
    assert store is not None
    store.close()
    with sqlite3.connect(tmp_path / "requests.db") as conn:
        rows = conn.execute("SELECT is_local, requested_model FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 0
    assert rows[0][1] == "sonnet"


def test_mixed_multi_tool_messages_streaming_and_client_tool_stop() -> None:
    provider = _ScriptedMessagesProvider(
        [
            [
                *_sse_tool_turn(
                    "toolu_s", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "fastapi"}
                ),
            ],
            [
                *_sse_tool_turn("toolu_calc", "calc", {"expr": "1+1"}),
            ],
        ]
    )
    app = create_test_app(providers={})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 128,
                "stream": True,
                "messages": [{"role": "user", "content": "Search and calculate"}],
                "tools": [
                    {
                        "name": "calc",
                        "description": "Calculator",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                    {"name": "web_search", "type": "web_search_20250305"},
                ],
            },
        )
    assert resp.status_code == 200
    events = parse_sse_text(resp.text)
    delta_ev = next(e for e in events if e.event == "message_delta")
    assert delta_ev.data["delta"]["stop_reason"] == "tool_use"
    assert delta_ev.data["usage"]["server_tool_use"] == {"web_search_requests": 1}
    starts = [
        e.data["content_block"] for e in events if e.event == "content_block_start"
    ]
    assert [b["type"] for b in starts] == [
        "server_tool_use",
        "web_search_tool_result",
        "tool_use",
    ]
    assert starts[-1]["name"] == "calc"


def test_door_modes_passthrough_auto_opt_out_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "model": "sonnet",
        "max_tokens": 64,
        "stream": False,
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [
            {"name": "calc", "input_schema": {"type": "object"}},
            {"name": "web_search", "type": "web_search_20250305"},
        ],
    }

    # 1. Opt-out WEB_SEARCH_ANTHROPIC_MULTI_TOOL_ENABLED=false rejects cleanly with legacy 400.
    app_off = create_test_app(
        Settings.model_validate({"WEB_SEARCH_ANTHROPIC_MULTI_TOOL_ENABLED": False}),
        providers={},
    )
    with TestClient(app_off) as client:
        r_off = client.post("/v1/messages", json=payload)
    assert r_off.status_code == 400
    assert "cannot pass ambiguous Anthropic" in r_off.json()["error"]["message"]

    # 2. passthrough against non-Anthropic route -> 400.
    app_pt_bad = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "passthrough"}),
        providers={},
    )
    with TestClient(app_pt_bad) as client:
        r_pt_bad = client.post("/v1/messages", json=payload)
    assert r_pt_bad.status_code == 400
    assert "passthrough" in r_pt_bad.json()["error"]["message"].lower()

    # 3. passthrough & auto on all-Anthropic route pass native server tools straight upstream!
    monkeypatch.setenv("MODEL_SONNET", "anthropic/claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123")
    native_provider = _ScriptedMessagesProvider(
        [_sse_text_turn("Native Anthropic reply")]
    )
    app_auto = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "auto"}),
        providers={},
    )
    with (
        patch(
            "my_claude_code.api.routes.resolve_provider", return_value=native_provider
        ),
        TestClient(app_auto) as client,
    ):
        r_auto = client.post("/v1/messages", json=payload)
    assert r_auto.status_code == 200
    assert [t.name for t in native_provider.requests[0].tools] == [
        "calc",
        "web_search",
    ]

    # 4. Non-web server tool (code_execution*) and bad max_uses both return 400 with expected shape.
    app_on = create_test_app(providers={})
    with TestClient(app_on) as client:
        r_code = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [
                    {"name": "code_execution", "type": "code_execution_20250522"}
                ],
            },
        )
        r_bad_opt = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [
                    {"name": "calc", "input_schema": {"type": "object"}},
                    {
                        "name": "web_search",
                        "type": "web_search_20250305",
                        "max_uses": 0,
                    },
                ],
            },
        )
    assert r_code.status_code == 400
    assert "Expected Anthropic server tools" in r_code.json()["error"]["message"]
    assert r_bad_opt.status_code == 400
    assert "Expected Anthropic server tools" in r_bad_opt.json()["error"]["message"]
