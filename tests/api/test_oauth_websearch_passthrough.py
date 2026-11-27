"""Tests for PR 33 (OAUTH-B): WEB_SEARCH_DOOR_MODE=passthrough|auto, cross-door search, and Codex SSE translation."""

import json
from collections.abc import AsyncIterator, Iterator
from json import loads as json_loads
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import (
    AnthropicStreamLedger,
    format_sse_event,
)
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)
from my_claude_code.providers.openai_responses.streaming import (
    ResponsesStreamConverter,
)
from my_claude_code.websearch.analytics import reset_analytics_state
from tests.api.support import create_test_app
from tests.providers.support import passthrough_rate_limiter


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    monkeypatch.setenv("MAX_OUTPUT_TOKENS_FLOOR", "0")
    set_request_log_path(tmp_path / "requests.db")
    reset_analytics_state()
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()


class _RecordingMessagesProvider:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.preflight_stream = MagicMock()
        self.requests: list[MessagesRequest] = []

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(
        self, request_data: MessagesRequest, **_kwargs: Any
    ) -> AsyncIterator[str]:
        self.requests.append(request_data)
        for frame in self._frames:
            yield frame


def _anthropic_native_search_frames() -> list[str]:
    citation = {
        "type": "web_search_result_location",
        "url": "https://docs.python.org/3.14/",
        "title": "Python 3.14 Docs",
        "cited_text": "Python 3.14 release notes.",
    }
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_native",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [],
                    "usage": {"input_tokens": 12, "output_tokens": 0},
                },
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "python 3.14"},
                },
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srvtoolu_1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://docs.python.org/3.14/",
                            "title": "Python 3.14 Docs",
                            "encrypted_content": "",
                            "page_age": None,
                        }
                    ],
                },
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 1}
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "text_delta", "text": "See Python 3.14 docs."},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "citations_delta", "citation": citation},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 2}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 10},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _chatgpt_oauth_provider_with_codex_sse(
    events: list[dict[str, Any]],
) -> tuple[ChatGPTOAuthProvider, list[dict[str, Any]]]:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url="https://chatgpt.com/backend-api",
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
        account_id="test_account_id",
    )
    sent_bodies: list[dict[str, Any]] = []
    payload = b"".join(f"data: {json.dumps(ev)}\n\n".encode() for ev in events)

    def _capture_build_request(
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        json: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> MagicMock:
        if json is not None:
            sent_bodies.append(json)
        elif content:
            sent_bodies.append(json_loads(content.decode("utf-8")))
        return MagicMock(method=method, url=url)

    async def _raw_stream() -> AsyncIterator[bytes]:
        yield payload

    response = MagicMock(status_code=200, headers={})
    response.aiter_raw = _raw_stream
    response.aclose = AsyncMock()
    provider._client.build_request = MagicMock(side_effect=_capture_build_request)
    provider._client.send = AsyncMock(return_value=response)
    return provider, sent_bodies


def _codex_search_events() -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "ws_abc",
                "type": "web_search_call",
                "status": "in_progress",
            },
        },
        {"type": "response.web_search_call.in_progress", "item_id": "ws_abc"},
        {"type": "response.web_search_call.searching", "item_id": "ws_abc"},
        {"type": "response.web_search_call.completed", "item_id": "ws_abc"},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "ws_abc",
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "python 3.14",
                    "sources": [
                        {
                            "type": "url",
                            "url": "https://docs.python.org/3.14/",
                            "title": "Python 3.14 Docs",
                        }
                    ],
                },
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "id": "msg_1",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 1,
            "content_index": 0,
            "delta": "Python 3.14 is released.",
        },
        {
            "type": "response.output_text.annotation.added",
            "item_id": "msg_1",
            "output_index": 1,
            "content_index": 0,
            "annotation_index": 0,
            "annotation": {
                "type": "url_citation",
                "url": "https://docs.python.org/3.14/",
                "title": "Python 3.14 Docs",
                "start_index": 0,
                "end_index": 11,
            },
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Python 3.14 is released.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://docs.python.org/3.14/",
                                "title": "Python 3.14 Docs",
                                "start_index": 0,
                                "end_index": 11,
                            }
                        ],
                    }
                ],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 15, "output_tokens": 9},
            },
        },
    ]


def _fake_local_search(query: str, *_args: Any, **_kwargs: Any) -> Any:
    item = WebSearchResultItem(
        title="Local Result",
        url="https://local.example/",
        snippet="Local search snippet",
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
    meta = MagicMock(
        route_id="r_local",
        primary_provider="exa",
        terminal_provider="exa",
        providers_tried=("exa",),
        fallback_used=False,
        supports_domain_filters=True,
    )
    return resp, items, meta


def test_build_chatgpt_oauth_translates_anthropic_web_search_without_leaking_keys() -> (
    None
):
    request = MessagesRequest.model_validate(
        {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "Find Python 3.14 docs"}],
            "tools": [
                {
                    "name": "calc",
                    "description": "Calculator",
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": "web_search",
                    "type": "web_search_20250305",
                    "allowed_domains": ["Docs.Python.Org."],
                    "blocked_domains": ["spam.example"],
                    "max_uses": 3,
                    "cache_control": {"type": "ephemeral"},
                    "external_web_access": True,
                    "search_context_size": "HIGH",
                    "user_location": {"type": "approximate", "country": "US"},
                },
            ],
            "tool_choice": {"type": "tool", "name": "web_search"},
        }
    )

    oauth_body = build_chatgpt_oauth_request_body(
        request, reasoning=DEFAULT_REASONING_POLICY
    )
    assert oauth_body["tools"] == [
        {
            "type": "function",
            "name": "calc",
            "description": "Calculator",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "web_search",
            "filters": {"allowed_domains": ["docs.python.org"]},
            "external_web_access": True,
            "search_context_size": "high",
            "user_location": {"type": "approximate", "country": "US"},
        },
    ]
    assert oauth_body["tool_choice"] == {"type": "web_search"}
    ws_tool = oauth_body["tools"][1]
    for forbidden in (
        "name",
        "parameters",
        "blocked_domains",
        "max_uses",
        "cache_control",
    ):
        assert forbidden not in ws_tool

    # Also accepts {"type": "web_search"} directly when allow_native_web_search=True.
    tc_request = request.model_copy(update={"tool_choice": {"type": "web_search"}})
    tc_body = build_chatgpt_oauth_request_body(
        tc_request, reasoning=DEFAULT_REASONING_POLICY
    )
    assert tc_body["tool_choice"] == {"type": "web_search"}

    # Non-OAuth default build_responses_request_body remains untouched (allow_native_web_search=False).
    default_body = build_responses_request_body(
        request, reasoning=DEFAULT_REASONING_POLICY
    )
    assert default_body["tools"][1]["type"] == "function"
    assert default_body["tools"][1]["name"] == "web_search"
    assert default_body["tool_choice"] == {"type": "function", "name": "web_search"}


def test_responses_stream_converter_translates_web_search_call_and_dedupes_citations() -> (
    None
):
    ledger = AnthropicStreamLedger("msg_ws", "gpt-5", input_tokens=15)
    converter = ResponsesStreamConverter(ledger)
    frames = [ledger.message_start()]
    for ev in _codex_search_events():
        frames.extend(converter.feed(ev))
    frames.extend(converter.finish())

    parsed = parse_sse_text("".join(frames))
    starts = [
        e.data["content_block"] for e in parsed if e.event == "content_block_start"
    ]
    assert [b["type"] for b in starts] == [
        "server_tool_use",
        "web_search_tool_result",
        "text",
    ]
    assert starts[0] == {
        "type": "server_tool_use",
        "id": "srvtoolu_abc",
        "name": "web_search",
        "input": {"query": "python 3.14"},
    }
    assert starts[1]["tool_use_id"] == "srvtoolu_abc"
    assert starts[1]["content"][0]["url"] == "https://docs.python.org/3.14/"

    citation_deltas = [
        e.data["delta"]
        for e in parsed
        if e.event == "content_block_delta"
        and e.data["delta"]["type"] == "citations_delta"
    ]
    # Emitted once on annotation.added and NOT duplicated on output_item.done.
    assert len(citation_deltas) == 1
    assert citation_deltas[0]["citation"] == {
        "type": "web_search_result_location",
        "url": "https://docs.python.org/3.14/",
        "title": "Python 3.14 Docs",
        "start_index": 0,
        "end_index": 11,
    }
    delta_ev = next(e for e in parsed if e.event == "message_delta")
    assert delta_ev.data["delta"]["stop_reason"] == "end_turn"


@pytest.mark.parametrize("door_mode", ["passthrough", "auto"])
@pytest.mark.parametrize("tool_name", ["web_search", "web_fetch"])
def test_messages_single_tool_native_anthropic_oauth_passthrough_and_auto(
    monkeypatch: pytest.MonkeyPatch, door_mode: str, tool_name: str
) -> None:
    monkeypatch.setenv("MODEL_SONNET", "anthropic_oauth/claude-sonnet-4-5")
    provider = _RecordingMessagesProvider(_anthropic_native_search_frames())
    app = create_test_app(
        Settings.model_validate(
            {
                "WEB_SEARCH_DOOR_MODE": door_mode,
                "ENABLE_WEB_SERVER_TOOLS": False,
            }
        ),
        providers={},
    )
    tool_type = (
        "web_search_20250305" if tool_name == "web_search" else "web_fetch_20250910"
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "stream": False,
                "messages": [{"role": "user", "content": "Search Python 3.14"}],
                "tools": [{"name": tool_name, "type": tool_type}],
                "tool_choice": {"type": "auto"},
            },
        )
    assert resp.status_code == 200
    assert len(provider.requests) == 1
    assert provider.requests[0].tools is not None
    assert provider.requests[0].tools[0].name == tool_name
    body = resp.json()
    assert [b["type"] for b in body["content"]] == [
        "server_tool_use",
        "web_search_tool_result",
        "text",
    ]
    assert body["content"][2]["citations"][0]["url"] == "https://docs.python.org/3.14/"


@pytest.mark.parametrize("door_mode", ["passthrough", "auto"])
def test_messages_single_tool_cross_door_chatgpt_oauth_web_search_e2e(
    monkeypatch: pytest.MonkeyPatch, door_mode: str
) -> None:
    monkeypatch.setenv("MODEL_SONNET", "chatgpt_oauth/gpt-5")
    provider, sent_bodies = _chatgpt_oauth_provider_with_codex_sse(
        _codex_search_events()
    )
    app = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": door_mode}),
        providers={},
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "stream": False,
                "messages": [{"role": "user", "content": "Python 3.14 release"}],
                "tools": [
                    {
                        "name": "web_search",
                        "type": "web_search_20250305",
                        "allowed_domains": ["docs.python.org"],
                        "blocked_domains": ["spam.example"],
                        "max_uses": 2,
                    }
                ],
                "tool_choice": {"type": "tool", "name": "web_search"},
            },
        )

    assert resp.status_code == 200
    assert len(sent_bodies) == 1
    assert sent_bodies[0]["tools"] == [
        {
            "type": "web_search",
            "filters": {"allowed_domains": ["docs.python.org"]},
        }
    ]
    assert sent_bodies[0]["tool_choice"] == {"type": "web_search"}
    body = resp.json()
    assert [b["type"] for b in body["content"]] == [
        "server_tool_use",
        "web_search_tool_result",
        "text",
    ]
    assert body["content"][0]["input"] == {"query": "python 3.14"}
    assert body["content"][2]["text"] == "Python 3.14 is released."
    assert body["content"][2]["citations"][0]["url"] == "https://docs.python.org/3.14/"


def test_messages_chatgpt_oauth_rejects_web_fetch_in_passthrough_and_falls_back_in_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_SONNET", "chatgpt_oauth/gpt-5")

    # Single-tool web_fetch -> chatgpt_oauth under passthrough => 400
    app_pt = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "passthrough"}),
        providers={},
    )
    with TestClient(app_pt) as client:
        r_single = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Read https://example.com"}],
                "tools": [{"name": "web_fetch", "type": "web_fetch_20250910"}],
                "tool_choice": {"type": "auto"},
            },
        )
        r_multi = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Search and fetch"}],
                "tools": [
                    {"name": "calc", "input_schema": {"type": "object"}},
                    {"name": "web_search", "type": "web_search_20250305"},
                    {"name": "web_fetch", "type": "web_fetch_20250910"},
                ],
            },
        )
    assert r_single.status_code == 400
    assert "passthrough" in r_single.json()["error"]["message"].lower()
    assert "anthropic_oauth" in r_single.json()["error"]["message"]
    assert r_multi.status_code == 400
    assert "passthrough" in r_multi.json()["error"]["message"].lower()

    # Multi-tool web_search-only -> chatgpt_oauth under passthrough => 200 native
    provider, sent_bodies = _chatgpt_oauth_provider_with_codex_sse(
        _codex_search_events()
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app_pt) as client,
    ):
        r_multi_ws = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "Search Python 3.14"}],
                "tools": [
                    {"name": "calc", "input_schema": {"type": "object"}},
                    {"name": "web_search", "type": "web_search_20250305"},
                ],
            },
        )
    assert r_multi_ws.status_code == 200
    assert [t["type"] for t in sent_bodies[0]["tools"]] == ["function", "web_search"]

    # Non-native single-tool in auto mode falls back locally and respects ENABLE_WEB_SERVER_TOOLS
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/test-sonnet")
    app_auto_disabled = create_test_app(
        Settings.model_validate(
            {
                "WEB_SEARCH_DOOR_MODE": "auto",
                "ENABLE_WEB_SERVER_TOOLS": False,
            }
        ),
        providers={},
    )
    with TestClient(app_auto_disabled) as client:
        r_disabled = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Search"}],
                "tools": [{"name": "web_search", "type": "web_search_20250305"}],
                "tool_choice": {"type": "auto"},
            },
        )
    assert r_disabled.status_code == 400
    assert "ENABLE_WEB_SERVER_TOOLS" in r_disabled.json()["error"]["message"]

    app_auto_local = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "auto"}),
        providers={},
    )
    with (
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_local_search,
        ) as mocked_local,
        TestClient(app_auto_local) as client,
    ):
        r_local = client.post(
            "/v1/messages",
            json={
                "model": "sonnet",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "Search local"}],
                "tools": [{"name": "web_search", "type": "web_search_20250305"}],
                "tool_choice": {"type": "auto"},
            },
        )
    assert r_local.status_code == 200
    assert mocked_local.call_count == 1


@pytest.mark.parametrize("door_mode", ["passthrough", "auto"])
@pytest.mark.parametrize("stream", [True, False])
def test_responses_web_search_passthrough_and_auto_to_chatgpt_oauth(
    door_mode: str, stream: bool
) -> None:
    provider, sent_bodies = _chatgpt_oauth_provider_with_codex_sse(
        _codex_search_events()
    )
    app = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": door_mode}),
        providers={},
    )
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=AssertionError("native passthrough must not run local search"),
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/responses",
            json={
                "model": "chatgpt_oauth/gpt-5",
                "input": "What is new in Python 3.14?",
                "stream": stream,
                "tools": [
                    {
                        "type": "web_search",
                        "filters": {"allowed_domains": ["docs.python.org"]},
                    }
                ],
                "tool_choice": {"type": "web_search"},
            },
        )

    assert resp.status_code == 200
    assert len(sent_bodies) == 1
    assert sent_bodies[0]["tools"] == [
        {
            "type": "web_search",
            "filters": {"allowed_domains": ["docs.python.org"]},
        }
    ]
    assert sent_bodies[0]["tool_choice"] == {"type": "web_search"}

    if stream:
        events = parse_sse_text(resp.text)
        event_names = [e.event for e in events]
        assert "response.web_search_call.in_progress" in event_names
        assert "response.web_search_call.completed" in event_names
        assert "response.output_text.annotation.added" in event_names
        completed = events[-1].data["response"]
    else:
        completed = resp.json()

    assert completed["status"] == "completed"
    assert [item["type"] for item in completed["output"]] == [
        "web_search_call",
        "message",
    ]
    assert completed["output"][0]["action"] == {
        "type": "search",
        "query": "python 3.14",
    }
    msg = completed["output"][1]
    assert msg["content"][0]["text"] == "Python 3.14 is released."
    assert msg["content"][0]["annotations"][0]["type"] == "url_citation"
    assert msg["content"][0]["annotations"][0]["url"] == "https://docs.python.org/3.14/"
