"""End-to-end tests for native web_search* on POST /v1/responses."""

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
    SYNTHETIC_WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_SCHEMA,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import ContentBlockToolResult
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem
from my_claude_code.websearch.analytics import (
    default_websearch_db_path,
    reset_analytics_state,
)
from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _isolated_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    monkeypatch.setenv("MODEL", "nvidia_nim/test-model")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS_FLOOR", "0")
    set_request_log_path(tmp_path / "requests.db")
    reset_analytics_state()
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()


def _tool_use_stream(tool_id: str, name: str, tool_input: dict[str, Any]) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_step",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "usage": {"input_tokens": 40, "output_tokens": 0},
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
                    "partial_json": json.dumps(tool_input),
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
                "usage": {"output_tokens": 12},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _text_stream(text: str) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_final",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "usage": {"input_tokens": 60, "output_tokens": 0},
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
                "usage": {"output_tokens": 18},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


class _ScriptedResponsesProvider:
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
        title="Python 3.14 Docs",
        url="https://docs.python.org/3.14/",
        snippet="What's New in Python 3.14",
        content=None,
        published="2026-10-01",
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
            "published": item.published or "",
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


def test_default_on_responses_web_search_sse_grounded_and_logged(
    tmp_path: Path,
) -> None:
    """Default Settings() works openly on /v1/responses without env overrides."""
    provider = _ScriptedResponsesProvider(
        [
            # First step has bad schema -> verifies M11 self-correction inside live HTTP request!
            _tool_use_stream("toolu_bad", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {}),
            _tool_use_stream(
                "toolu_ok", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _text_stream(
                "Python 3.14 notes live at [Python 3.14 Docs](https://docs.python.org/3.14/)."
            ),
        ]
    )
    app = create_test_app(providers={})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ) as mocked_search,
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/responses",
            json={
                "model": "nvidia_nim/test-model",
                "input": "What is new in Python 3.14?",
                "stream": True,
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [
                            {
                                "type": "function",
                                "name": "js",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    },
                    {
                        "type": "web_search",
                        "filters": {"allowed_domains": ["docs.python.org"]},
                    },
                ],
            },
        )

    assert resp.status_code == 200
    assert mocked_search.call_count == 1
    assert mocked_search.call_args.kwargs["allowed_domains"] == ("docs.python.org",)

    # Tool order cache-prefix check: namespace custom tool FIRST, synthetic web_search at END!
    assert [t.name for t in provider.requests[0].tools] == [
        "mcp__node_repl__js",
        SYNTHETIC_WEB_SEARCH_TOOL_NAME,
    ]
    # Self-correction check: step 1 received full JSON schema from invalid step 0!
    step1_last = provider.requests[1].messages[-1].content[0]
    assert isinstance(step1_last, ContentBlockToolResult)
    assert json.dumps(WEB_SEARCH_TOOL_SCHEMA, sort_keys=True) in str(step1_last.content)

    events = parse_sse_text(resp.text)
    event_types = [e.event for e in events]
    assert "response.web_search_call.in_progress" in event_types
    assert "response.web_search_call.searching" in event_types
    assert "response.web_search_call.completed" in event_types
    assert "response.output_text.annotation.added" in event_types
    completed = events[-1].data["response"]
    assert completed["status"] == "completed"
    msg_item = completed["output"][-1]
    anns = msg_item["content"][0]["annotations"]
    assert len(anns) == 1
    assert anns[0]["type"] == "url_citation"
    assert anns[0]["url"] == "https://docs.python.org/3.14/"

    # Verify requests.db logged is_local=0 with total loop tokens.
    store = get_request_log_store()
    assert store is not None
    store.close()
    with sqlite3.connect(tmp_path / "requests.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["is_local"] == 0
    assert rows[0]["tokens_in"] == 140
    assert rows[0]["tokens_out"] == 42


def test_responses_web_search_non_streaming_json_and_follow_up_turn() -> None:
    provider = _ScriptedResponsesProvider(
        [
            _tool_use_stream(
                "toolu_1", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _text_stream("See https://docs.python.org/3.14/ now."),
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
            "/v1/responses",
            json={
                "model": "nvidia_nim/test-model",
                "input": [
                    {
                        "type": "web_search_call",
                        "id": "ws_old",
                        "status": "completed",
                        "action": {"type": "search", "query": "python 3.13"},
                    },
                    {"role": "user", "content": "Compare with 3.14"},
                ],
                "stream": False,
                "tools": [{"type": "web_search_2025_08_26"}],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["type"] == "web_search_call"
    assert body["output"][1]["type"] == "message"
    assert (
        body["output"][1]["content"][0]["annotations"][0]["url"]
        == "https://docs.python.org/3.14/"
    )


def test_responses_passthrough_mode_and_invalid_tool_choice_return_400() -> None:
    provider = _ScriptedResponsesProvider([_text_stream("unused")])
    # WEB_SEARCH_DOOR_MODE=passthrough raises crisp 400 on /v1/responses.
    app_pt = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "passthrough"}),
        providers={},
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app_pt) as client,
    ):
        r_pt = client.post(
            "/v1/responses",
            json={
                "model": "nvidia_nim/test-model",
                "input": "Search",
                "tools": [{"type": "web_search"}],
            },
        )
    assert r_pt.status_code == 400
    assert "passthrough" in r_pt.json()["error"]["message"].lower()

    # Invalid tool_choice referencing undeclared web_search includes expected schema!
    app_ok = create_test_app(providers={})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app_ok) as client,
    ):
        r_tc = client.post(
            "/v1/responses",
            json={
                "model": "nvidia_nim/test-model",
                "input": "Search",
                "tools": [],
                "tool_choice": {"type": "web_search"},
            },
        )
    assert r_tc.status_code == 400
    assert "Expected Responses web_search tool" in r_tc.json()["error"]["message"]


def test_responses_web_search_real_outbound_logs_websearch_db() -> None:
    """Verify full stack executes through outbound._run_web_search_response into websearch.db."""
    from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider

    provider = _ScriptedResponsesProvider(
        [
            _tool_use_stream(
                "toolu_1", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _text_stream("Found https://docs.python.org/3.14/"),
        ]
    )
    app = create_test_app(
        Settings.model_validate(
            {"WEB_SEARCH_PROVIDER": "ddgs", "WEB_SEARCH_FALLBACK_POLICY": "none"}
        ),
        providers={},
    )

    async def fake_ddgs_search(self: Any, query: str, **_kw: Any) -> WebSearchResponse:
        return WebSearchResponse(
            provider="ddgs",
            query=query,
            key_index=0,
            cost_usd=None,
            results=(
                WebSearchResultItem(
                    title="Python 3.14",
                    url="https://docs.python.org/3.14/",
                    snippet="Release info",
                    content=None,
                    published=None,
                ),
            ),
        )

    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch.object(DdgsWebSearchProvider, "search", fake_ddgs_search),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/responses",
            json={
                "model": "nvidia_nim/test-model",
                "input": "Search python 3.14",
                "stream": False,
                "tools": [{"type": "web_search"}],
            },
        )
    assert resp.status_code == 200
    reset_analytics_state()
    ws_db = default_websearch_db_path()
    assert ws_db.exists()
    with sqlite3.connect(ws_db) as ws_conn:
        rows = ws_conn.execute("SELECT provider, status FROM search_log").fetchall()
    assert rows == [("ddgs", "success")]
