"""End-to-end tests for native googleSearch / urlContext on Gemini /v1beta endpoints."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools.egress import WebFetchEgressViolation
from my_claude_code.api.web_tools.outbound import WebSearchRouteMeta
from my_claude_code.api.web_tools.tool_loop import (
    SYNTHETIC_WEB_FETCH_TOOL_NAME,
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
_GENERATE = f"/v1beta/models/{_MODEL}:generateContent"
_STREAM = f"/v1beta/models/{_MODEL}:streamGenerateContent?alt=sse"


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
                    "usage": {"input_tokens": 35, "output_tokens": 0},
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
                "usage": {"output_tokens": 11},
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
                    "usage": {"input_tokens": 55, "output_tokens": 0},
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
                    "usage": {"output_tokens": 17},
                },
            ),
            format_sse_event("message_stop", {"type": "message_stop"}),
        ]
    )
    return frames


class _ScriptedGeminiProvider:
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


def _fake_fetch_detail(url: str, *_args: Any, **_kw: Any) -> Any:
    return (
        {
            "url": url,
            "title": "Example Article",
            "media_type": "text/plain",
            "data": "Full article text from fetched page.",
        },
        "trafilatura",
        False,
    )


def _data_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        stripped = block.strip()
        if not stripped:
            continue
        assert stripped.startswith("data: "), stripped
        frames.append(json.loads(stripped[len("data: ") :]))
    return frames


def test_default_on_gemini_google_search_generate_content_grounded_and_logged(
    tmp_path: Path,
) -> None:
    """Default Settings() serves googleSearch on :generateContent with prefix-cache order & M11."""
    provider = _ScriptedGeminiProvider(
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
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ) as mocked_search,
        TestClient(app) as client,
    ):
        resp = client.post(
            _GENERATE,
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "What is new in 3.14?"}]}
                ],
                "tools": [
                    {
                        "functionDeclarations": [
                            {"name": "get_weather", "parameters": {"type": "OBJECT"}}
                        ]
                    },
                    {"googleSearch": {"excludeDomains": ["Spam.COM"]}},
                ],
            },
        )

    assert resp.status_code == 200
    assert mocked_search.call_count == 1
    assert mocked_search.call_args.kwargs["blocked_domains"] == ("spam.com",)

    # IV.7 prefix-cache invariant: caller functions FIRST, synthetic __mcc_web_search LAST!
    assert [t.name for t in provider.requests[0].tools] == [
        "get_weather",
        SYNTHETIC_WEB_SEARCH_TOOL_NAME,
    ]
    # Schema-on-error self-correction: step 1 got full schema after invalid step 0.
    step1_last = provider.requests[1].messages[-1].content[0]
    assert isinstance(step1_last, ContentBlockToolResult)
    assert json.dumps(WEB_SEARCH_TOOL_SCHEMA, sort_keys=True) in str(step1_last.content)

    body = resp.json()
    cand = body["candidates"][0]
    assert cand["finishReason"] == "STOP"
    assert "urlContextMetadata" not in cand
    gm = cand["groundingMetadata"]
    assert gm["webSearchQueries"] == ["python 3.14"]
    assert gm["groundingChunks"] == [
        {
            "web": {
                "uri": "https://docs.python.org/3.14/",
                "title": "Python 3.14 Docs",
            }
        }
    ]
    assert len(gm["groundingSupports"]) == 1
    assert gm["groundingSupports"][0]["groundingChunkIndices"] == [0]
    assert (
        gm["groundingSupports"][0]["segment"]["text"]
        == "[Python 3.14 Docs](https://docs.python.org/3.14/)"
    )
    # IV.16 anti-fabrication: never emit confidenceScores or searchEntryPoint.
    assert "confidenceScores" not in gm
    assert "searchEntryPoint" not in gm

    # requests.db logged is_local=0 with total loop tokens.
    store = get_request_log_store()
    assert store is not None
    store.close()
    with sqlite3.connect(tmp_path / "requests.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["protocol"] == "gemini"
    assert rows[0]["is_local"] == 0
    assert rows[0]["tokens_in"] == 125
    assert rows[0]["tokens_out"] == 39


def test_gemini_streaming_google_search_and_url_context_mixed_function_call() -> None:
    """Streaming ?alt=sse yields thoughts, text, functionCall, groundingMetadata, and urlContextMetadata without [DONE]."""
    provider = _ScriptedGeminiProvider(
        [
            _tool_use_stream(
                "toolu_s", SYNTHETIC_WEB_SEARCH_TOOL_NAME, {"query": "python 3.14"}
            ),
            _tool_use_stream(
                "toolu_f",
                SYNTHETIC_WEB_FETCH_TOOL_NAME,
                {"url": "https://example.com/article"},
            ),
            _final_stream(
                thinking="Comparing sources",
                text="Checked https://docs.python.org/3.14/ now.",
                client_tool=("toolu_fn", "save_note", {"title": "3.14"}),
            ),
        ]
    )
    app = create_test_app(providers={})
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
            side_effect=_fake_fetch_detail,
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            _STREAM,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Read and save"}]}],
                "generationConfig": {"thinkingConfig": {"includeThoughts": True}},
                "tools": [
                    {"functionDeclarations": [{"name": "save_note"}]},
                    {"googleSearch": {}},
                    {"urlContext": {}},
                ],
            },
        )

    assert resp.status_code == 200
    assert "[DONE]" not in resp.text
    assert [t.name for t in provider.requests[0].tools] == [
        "save_note",
        SYNTHETIC_WEB_SEARCH_TOOL_NAME,
        SYNTHETIC_WEB_FETCH_TOOL_NAME,
    ]

    frames = _data_frames(resp.text)
    parts = [
        part for frame in frames for part in frame["candidates"][0]["content"]["parts"]
    ]
    assert {"text": "Comparing sources", "thought": True} in parts
    assert {"text": "Checked https://docs.python.org/3.14/ now."} in parts
    assert {"functionCall": {"name": "save_note", "args": {"title": "3.14"}}} in parts

    last = frames[-1]
    cand = last["candidates"][0]
    assert cand["finishReason"] == "STOP"
    assert cand["groundingMetadata"]["webSearchQueries"] == ["python 3.14"]
    assert cand["urlContextMetadata"] == {
        "urlMetadata": [
            {
                "retrievedUrl": "https://example.com/article",
                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
            }
        ]
    }
    assert last["usageMetadata"]["thoughtsTokenCount"] > 0


def test_gemini_url_context_only_and_egress_unsafe_status() -> None:
    """urlContext-only appends only __mcc_web_fetch, omits groundingMetadata, and maps SSRF to UNSAFE."""
    provider = _ScriptedGeminiProvider(
        [
            _tool_use_stream(
                "toolu_f",
                SYNTHETIC_WEB_FETCH_TOOL_NAME,
                {"url": "http://127.0.0.1:8082/secret"},
            ),
            _final_stream(text="Cannot access private address."),
        ]
    )
    app = create_test_app(providers={})
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
            side_effect=WebFetchEgressViolation("private target blocked"),
        ),
        TestClient(app) as client,
    ):
        resp = client.post(
            _GENERATE,
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "Read http://127.0.0.1:8082/secret"}],
                    }
                ],
                "tools": [{"urlContext": {}}],
            },
        )

    assert resp.status_code == 200
    assert [t.name for t in provider.requests[0].tools] == [
        SYNTHETIC_WEB_FETCH_TOOL_NAME
    ]
    cand = resp.json()["candidates"][0]
    assert "groundingMetadata" not in cand
    assert cand["urlContextMetadata"] == {
        "urlMetadata": [
            {
                "retrievedUrl": "http://127.0.0.1:8082/secret",
                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_UNSAFE",
            }
        ]
    }


def test_gemini_trace_excludes_google_search_when_on_while_dropping_code_execution() -> (
    None
):
    """When enabled, googleSearch is NOT traced as dropped while codeExecution still is (M3)."""
    provider = _ScriptedGeminiProvider([_final_stream(text="Plain answer.")])
    app = create_test_app(providers={})
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        patch("my_claude_code.api.handlers.gemini.trace_event") as trace_mock,
        TestClient(app) as client,
    ):
        resp = client.post(
            _GENERATE,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                "tools": [{"googleSearch": {}}, {"codeExecution": {}}],
            },
        )

    assert resp.status_code == 200
    trace_mock.assert_called_once()
    assert trace_mock.call_args.kwargs["fields"] == ["codeExecution"]
    # No search ran -> groundingMetadata omitted (IV.16).
    assert "groundingMetadata" not in resp.json()["candidates"][0]


def test_gemini_tool_config_none_and_any_allowed_function_names() -> None:
    provider_none = _ScriptedGeminiProvider([_final_stream(text="No tools used.")])
    app = create_test_app(providers={})
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider",
            return_value=provider_none,
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ) as mocked_search,
        TestClient(app) as client,
    ):
        resp_none = client.post(
            _GENERATE,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Hi"}]}],
                "tools": [{"googleSearch": {}}],
                "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
            },
        )
    assert resp_none.status_code == 200
    assert mocked_search.call_count == 0
    assert provider_none.requests[0].tools is None

    provider_any = _ScriptedGeminiProvider(
        [_final_stream(client_tool=("t1", "get_weather", {"city": "NYC"}))]
    )
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider",
            return_value=provider_any,
        ),
        TestClient(app) as client,
    ):
        resp_any = client.post(
            _GENERATE,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Weather"}]}],
                "tools": [
                    {"functionDeclarations": [{"name": "get_weather"}]},
                    {"googleSearch": {}},
                ],
                "toolConfig": {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": ["get_weather"],
                    }
                },
            },
        )
    assert resp_any.status_code == 200
    assert provider_any.requests[0].tool_choice == {
        "type": "tool",
        "name": "get_weather",
    }


def test_gemini_off_toggle_passthrough_and_schema_on_error_400s() -> None:
    # Toggle OFF restores legacy drop behavior.
    provider_off = _ScriptedGeminiProvider([_final_stream(text="Legacy answer.")])
    off_app = create_test_app(
        Settings.model_validate({"WEB_SEARCH_GEMINI_ENABLED": False}),
        providers={},
    )
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider",
            return_value=provider_off,
        ),
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ) as mocked_search,
        patch("my_claude_code.api.handlers.gemini.trace_event") as trace_mock,
        TestClient(off_app) as off_client,
    ):
        off_resp = off_client.post(
            _GENERATE,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Search"}]}],
                "tools": [{"googleSearch": {}}],
            },
        )
    assert off_resp.status_code == 200
    assert mocked_search.call_count == 0
    trace_mock.assert_called_once()
    assert trace_mock.call_args.kwargs["fields"] == ["googleSearch"]
    assert "groundingMetadata" not in off_resp.json()["candidates"][0]

    # passthrough returns 400 INVALID_ARGUMENT in Gemini envelope.
    pt_app = create_test_app(
        Settings.model_validate({"WEB_SEARCH_DOOR_MODE": "passthrough"}),
        providers={},
    )
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider",
            return_value=provider_off,
        ),
        TestClient(pt_app) as pt_client,
    ):
        pt_resp = pt_client.post(
            _GENERATE,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Search"}]}],
                "tools": [{"googleSearch": {}}],
            },
        )
    assert pt_resp.status_code == 400
    assert pt_resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert "passthrough" in pt_resp.json()["error"]["message"].lower()

    # Malformed googleSearch / urlContext returns 400 quoting expected schema.
    on_app = create_test_app(providers={})
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider",
            return_value=provider_off,
        ),
        TestClient(on_app) as on_client,
    ):
        bad_resp = on_client.post(
            _GENERATE,
            json={
                "contents": [{"role": "user", "parts": [{"text": "Search"}]}],
                "tools": [{"googleSearch": {"excludeDomains": "bad.com"}}],
            },
        )
    assert bad_resp.status_code == 400
    assert bad_resp.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert "Expected Gemini web tools" in bad_resp.json()["error"]["message"]


def test_gemini_non_search_request_is_identical_whether_enabled_or_disabled() -> None:
    """IV.7 parity: non-search Gemini requests produce identical outbound payload & body."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Say hi"}]}],
        "tools": [{"functionDeclarations": [{"name": "echo"}]}],
    }
    p_on = _ScriptedGeminiProvider([_final_stream(text="Hello")])
    p_off = _ScriptedGeminiProvider([_final_stream(text="Hello")])
    with (
        patch("my_claude_code.api.gemini_routes.resolve_provider", return_value=p_on),
        TestClient(create_test_app(providers={})) as c_on,
    ):
        r_on = c_on.post(_GENERATE, json=payload)
    with (
        patch("my_claude_code.api.gemini_routes.resolve_provider", return_value=p_off),
        TestClient(
            create_test_app(
                Settings.model_validate({"WEB_SEARCH_GEMINI_ENABLED": False}),
                providers={},
            )
        ) as c_off,
    ):
        r_off = c_off.post(_GENERATE, json=payload)

    assert r_on.status_code == r_off.status_code == 200
    assert p_on.requests[0].model_dump() == p_off.requests[0].model_dump()
    b_on = r_on.json()
    b_off = r_off.json()
    b_on.pop("responseId", None)
    b_off.pop("responseId", None)
    assert b_on == b_off


def test_gemini_web_search_real_outbound_logs_websearch_db() -> None:
    """Verify full stack executes through outbound._run_web_search_response into websearch.db."""
    from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider

    provider = _ScriptedGeminiProvider(
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
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        patch.object(DdgsWebSearchProvider, "search", fake_ddgs_search),
        TestClient(app) as client,
    ):
        resp = client.post(
            _GENERATE,
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": "Search python 3.14"}]}
                ],
                "tools": [{"googleSearch": {}}],
            },
        )
    assert resp.status_code == 200
    reset_analytics_state()
    ws_db = default_websearch_db_path()
    assert ws_db.exists()
    with sqlite3.connect(ws_db) as ws_conn:
        rows = ws_conn.execute("SELECT provider, status FROM search_log").fetchall()
    assert rows == [("ddgs", "success")]
