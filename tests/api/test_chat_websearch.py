"""End-to-end tests for native web_search_options / web_search* on POST /v1/chat/completions."""

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
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem
from my_claude_code.websearch.analytics import (
    default_websearch_db_path,
    reset_analytics_state,
)
from tests.api.support import create_test_app

_MODEL = "nvidia_nim/test-model"
_PATH = "/v1/chat/completions"


@pytest.fixture(autouse=True)
def _isolated_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    monkeypatch.setenv("MODEL", _MODEL)
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
                    "usage": {"input_tokens": 42, "output_tokens": 0},
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
                "usage": {"output_tokens": 13},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _final_stream(
    *,
    text: str = "",
    thinking: str = "",
    client_tool: tuple[str, str, dict[str, Any]] | None = None,
) -> list[str]:
    frames = [
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
                    "usage": {"input_tokens": 58, "output_tokens": 0},
                },
            },
        )
    ]
    idx = 0
    if thinking:
        frames.extend(
            [
                format_sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                ),
                format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "thinking_delta", "thinking": thinking},
                    },
                ),
                format_sse_event(
                    "content_block_stop", {"type": "content_block_stop", "index": idx}
                ),
            ]
        )
        idx += 1
    if text:
        frames.extend(
            [
                format_sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "text_delta", "text": text},
                    },
                ),
                format_sse_event(
                    "content_block_stop", {"type": "content_block_stop", "index": idx}
                ),
            ]
        )
        idx += 1
    if client_tool is not None:
        tid, tname, tinput = client_tool
        frames.extend(
            [
                format_sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tid,
                            "name": tname,
                            "input": {},
                        },
                    },
                ),
                format_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(tinput),
                        },
                    },
                ),
                format_sse_event(
                    "content_block_stop", {"type": "content_block_stop", "index": idx}
                ),
            ]
        )
    stop_reason = "tool_use" if client_tool is not None else "end_turn"
    frames.extend(
        [
            format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason},
                    "usage": {"output_tokens": 19},
                },
            ),
            format_sse_event("message_stop", {"type": "message_stop"}),
        ]
    )
    return frames


class _ScriptedChatProvider:
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


def _data_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        stripped = block.strip()
        if not stripped or stripped == "data: [DONE]":
            continue
        assert stripped.startswith("data: "), stripped
        frames.append(json.loads(stripped[len("data: ") :]))
    return frames


def test_default_on_chat_web_search_options_nested_citation_and_logged(
    tmp_path: Path,
) -> None:
    """Default Settings() serves web_search_options openly with nested url_citation and M11."""
    provider = _ScriptedChatProvider(
        [
            _tool_use_stream("toolu_bad", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {}),
            _tool_use_stream(
                "toolu_ok", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _final_stream(
                text="Python 3.14 notes live at [Python 3.14 Docs](https://docs.python.org/3.14/)."
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
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "What is new in 3.14?"}],
                "web_search_options": {
                    "search_context_size": "low",
                    "user_location": {
                        "type": "approximate",
                        "approximate": {"country": "US", "city": "NYC"},
                    },
                    "filters": {"allowed_domains": ["docs.python.org"]},
                },
            },
        )

    assert resp.status_code == 200
    assert mocked_search.call_count == 1
    assert mocked_search.call_args.kwargs["max_results"] == 5
    assert mocked_search.call_args.kwargs["allowed_domains"] == ("docs.python.org",)

    # Schema-on-error self-correction check in live loop.
    step1_last = provider.requests[1].messages[-1].content[0]
    assert isinstance(step1_last, ContentBlockToolResult)
    assert json.dumps(WEB_SEARCH_TOOL_SCHEMA, sort_keys=True) in str(step1_last.content)

    body = resp.json()
    assert body["object"] == "chat.completion"
    msg = body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "stop"
    assert len(msg["annotations"]) == 1
    ann = msg["annotations"][0]
    # Nested url_citation shape only (M6 guard: no top-level url).
    assert ann["type"] == "url_citation"
    assert "url" not in ann
    assert ann["url_citation"] == {
        "url": "https://docs.python.org/3.14/",
        "title": "Python 3.14 Docs",
        "start_index": 26,
        "end_index": 75,
    }

    store = get_request_log_store()
    assert store is not None
    store.close()
    with sqlite3.connect(tmp_path / "requests.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["protocol"] == "openai_chat"
    assert rows[0]["is_local"] == 0
    assert rows[0]["tokens_in"] == 142
    assert rows[0]["tokens_out"] == 45


def test_chat_streaming_mixed_function_tools_prefix_cache_and_annotations() -> None:
    """Streaming chat completions emits role, thinking, text, tool_calls, annotations, usage, and [DONE]."""
    provider = _ScriptedChatProvider(
        [
            _tool_use_stream(
                "toolu_1", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _final_stream(
                thinking="Checking source",
                text="See https://docs.python.org/3.14/ now.",
                client_tool=("call_save", "save_note", {"title": "3.14"}),
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
        TestClient(app) as client,
    ):
        resp = client.post(
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Find and save"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "save_note",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    {"type": "web_search"},
                ],
                "tool_choice": {"type": "web_search"},
            },
        )

    assert resp.status_code == 200
    assert resp.text.endswith("data: [DONE]\n\n")
    # IV.7 prefix cache order: caller tool FIRST, __mcc_web_search LAST!
    assert [t.name for t in provider.requests[0].tools] == [
        "save_note",
        SYNTHETIC_WEB_SEARCH_TOOL_NAME,
    ]
    # tool_choice={"type": "web_search"} forced __mcc_web_search on step 0!
    assert provider.requests[0].tool_choice == {
        "type": "tool",
        "name": SYNTHETIC_WEB_SEARCH_TOOL_NAME,
    }

    frames = _data_frames(resp.text)
    deltas = [f["choices"][0]["delta"] for f in frames if f["choices"]]
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert any(d.get("reasoning_content") == "Checking source" for d in deltas)
    assert any(
        d.get("content") == "See https://docs.python.org/3.14/ now." for d in deltas
    )
    tool_deltas = [d["tool_calls"][0] for d in deltas if "tool_calls" in d]
    assert tool_deltas[0]["function"]["name"] == "save_note"
    assert json.loads("".join(tc["function"]["arguments"] for tc in tool_deltas)) == {
        "title": "3.14"
    }
    ann_deltas = [d["annotations"] for d in deltas if "annotations" in d]
    assert len(ann_deltas) == 1
    assert ann_deltas[0][0]["type"] == "url_citation"
    assert ann_deltas[0][0]["url_citation"]["url"] == "https://docs.python.org/3.14/"
    assert frames[-1]["choices"] == []
    assert frames[-1]["usage"]["prompt_tokens"] == 100


def test_chat_ungrounded_reply_omits_annotations_and_supports_all_search_types() -> (
    None
):
    """IV.16: when no gathered URL is cited in text, omit annotations completely."""
    for tool_type in (
        "web_search",
        "web_search_2025_08_26",
        "web_search_preview",
        "web_search_preview_2025_03_11",
    ):
        provider = _ScriptedChatProvider(
            [
                _tool_use_stream(
                    "toolu_1", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
                ),
                _final_stream(text="Python 3.14 is released."),
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
                _PATH,
                json={
                    "model": _MODEL,
                    "messages": [{"role": "user", "content": "Summary"}],
                    "tools": [{"type": tool_type}],
                },
            )
        assert resp.status_code == 200
        assert "annotations" not in resp.json()["choices"][0]["message"]


def test_chat_off_toggle_passthrough_and_schema_on_error_400s() -> None:
    provider = _ScriptedChatProvider([_final_stream(text="Legacy answer.")])
    # Toggle OFF ignores web_search_options without invoking search loop.
    off_app = create_test_app(
        Settings.model_validate({"WEB_SEARCH_CHAT_ENABLED": False}),
        providers={},
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ) as mocked_search,
        TestClient(off_app) as off_client,
    ):
        off_resp = off_client.post(
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Hello"}],
                "web_search_options": {"search_context_size": "low"},
            },
        )
    assert off_resp.status_code == 200
    assert mocked_search.call_count == 0

    # passthrough raises crisp 400.
    pt_app = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "passthrough"}),
        providers={},
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(pt_app) as pt_client,
    ):
        pt_resp = pt_client.post(
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Hello"}],
                "web_search_options": {},
            },
        )
    assert pt_resp.status_code == 400
    assert "passthrough" in pt_resp.json()["error"]["message"].lower()

    # Invalid web_search_options & orphan tool_choice raise 400 quoting schema.
    on_app = create_test_app(providers={})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(on_app) as on_client,
    ):
        r_bad_opt = on_client.post(
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Hello"}],
                "web_search_options": "bad-string",
            },
        )
        r_orphan = on_client.post(
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Hello"}],
                "tool_choice": {"type": "web_search"},
            },
        )
    for r_err in (r_bad_opt, r_orphan):
        assert r_err.status_code == 400
        assert r_err.json()["error"]["type"] == "invalid_request_error"
        assert (
            "Expected Chat Completions web search" in r_err.json()["error"]["message"]
        )


def test_chat_non_search_request_is_identical_whether_enabled_or_disabled() -> None:
    """IV.7 parity: non-search Chat Completions requests stay byte-identical."""
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "Say hi"}],
        "tools": [{"type": "function", "function": {"name": "echo"}}],
    }
    p_on = _ScriptedChatProvider([_final_stream(text="Hello")])
    p_off = _ScriptedChatProvider([_final_stream(text="Hello")])
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=p_on),
        TestClient(create_test_app(providers={})) as c_on,
    ):
        r_on = c_on.post(_PATH, json=payload)
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=p_off),
        TestClient(
            create_test_app(
                Settings.model_validate({"WEB_SEARCH_CHAT_ENABLED": False}),
                providers={},
            )
        ) as c_off,
    ):
        r_off = c_off.post(_PATH, json=payload)

    assert r_on.status_code == r_off.status_code == 200
    assert p_on.requests[0].model_dump() == p_off.requests[0].model_dump()
    b_on = r_on.json()
    b_off = r_off.json()
    for b in (b_on, b_off):
        b.pop("id", None)
        b.pop("created", None)
    assert b_on == b_off


def test_chat_web_search_real_outbound_logs_websearch_db() -> None:
    """Verify full stack executes through outbound._run_web_search_response into websearch.db."""
    from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider

    provider = _ScriptedChatProvider(
        [
            _tool_use_stream(
                "toolu_1", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _final_stream(text="Found https://docs.python.org/3.14/"),
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
            _PATH,
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Search python 3.14"}],
                "web_search_options": {},
            },
        )
    assert resp.status_code == 200
    assert (
        resp.json()["choices"][0]["message"]["annotations"][0]["url_citation"]["url"]
        == "https://docs.python.org/3.14/"
    )
    reset_analytics_state()
    ws_db = default_websearch_db_path()
    assert ws_db.exists()
    with sqlite3.connect(ws_db) as ws_conn:
        rows = ws_conn.execute("SELECT provider, status FROM search_log").fetchall()
    assert rows == [("ddgs", "success")]
