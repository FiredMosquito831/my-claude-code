"""Unit and provider recovery tests for PR 6: cross-door semantic fidelity."""

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.config import settings as config_settings
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    build_base_request_body,
    effective_structured_output_spec,
)
from my_claude_code.core.anthropic.models import (
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockToolResult,
    MessagesRequest,
)
from my_claude_code.core.anthropic.tool_result_media import (
    TOOL_DOCUMENT_STRIPPED_TEXT,
    USER_DOCUMENT_STRIPPED_TEXT,
)
from my_claude_code.core.openai_chat_completions.errors import (
    ChatCompletionsConversionError,
)
from my_claude_code.core.openai_chat_completions.input import (
    convert_request_to_anthropic_payload as convert_chat_to_anthropic_payload,
)
from my_claude_code.core.openai_chat_completions.models import (
    OpenAIChatCompletionRequest,
)
from my_claude_code.core.openai_responses.errors import ResponsesConversionError
from my_claude_code.core.openai_responses.input import (
    convert_request_to_anthropic_payload as convert_responses_to_anthropic_payload,
)
from my_claude_code.core.openai_responses.models import OpenAIResponsesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_DEFAULT_BASE,
)
from my_claude_code.providers.deepseek.compat import build_deepseek_request_body
from my_claude_code.providers.openai_chat.profiles import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
)
from my_claude_code.providers.openai_chat.provider import OpenAIChatProvider
from my_claude_code.providers.openai_chat.request_policy import (
    OpenAIChatRequestPolicy,
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_passthrough_body,
    build_responses_request_body,
)
from my_claude_code.providers.recovery import (
    FACT_STRUCTURED_OUTPUT_UNSUPPORTED,
    RUNG_STRUCTURED_OUTPUT,
    LearnedFactStore,
    classify_structured_output_refusal,
    reset_learned_fact_store,
    set_learned_fact_store,
)
from my_claude_code.providers.runtime.models_dev import (
    model_accepts_pdf,
    write_models_dev_cache,
)
from tests.providers.support import passthrough_rate_limiter

_PNG_DATA_URL = "data:image/png;base64,AQ=="
_PDF_B64 = base64.b64encode(b"%PDF-1.4\n%test\n").decode("ascii")
_PDF_DATA_URL = f"data:application/pdf;base64,{_PDF_B64}"
_SAMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
_REASONING_OFF = ReasoningPolicy.off()


def _chat_to_ir(payload: dict[str, Any]) -> MessagesRequest:
    converted = convert_chat_to_anthropic_payload(
        OpenAIChatCompletionRequest.model_validate(payload)
    )
    return MessagesRequest.model_validate(converted)


def _responses_to_ir(payload: dict[str, Any]) -> MessagesRequest:
    converted = convert_responses_to_anthropic_payload(
        OpenAIResponsesRequest.model_validate(payload)
    )
    return MessagesRequest.model_validate(converted)


def _http_400(url: str, payload: dict[str, Any]) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    resp = httpx.Response(400, json=payload, request=req)
    return httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)


def _anthropic_sse_response() -> httpx.Response:
    body = (
        b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"{\\"answer\\":\\"ok\\"}"}}\n\n'
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
        request=httpx.Request("POST", "https://example.invalid/v1/messages"),
    )


def _chat_sse_response() -> httpx.Response:
    body = (
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"{\\"answer\\":\\"ok\\"}"},"finish_reason":null}]}\n\n'
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"),
    )


def _responses_sse_bytes() -> bytes:
    events = [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [],
            },
        },
        {"type": "response.output_text.delta", "delta": '{"answer":"ok"}'},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        },
    ]
    return b"".join(f"data: {json.dumps(ev)}\n\n".encode() for ev in events)


def _chat_sse_bytes() -> bytes:
    return (
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"{\\"answer\\":\\"ok\\"}"},"finish_reason":null}]}\n\n'
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n'
        b"data: [DONE]\n\n"
    )


# ---------------------------------------------------------------------------
# Area 1: Structured output fidelity, Q1, Q4, classification & learned 400
# ---------------------------------------------------------------------------


def test_anthropic_output_config_format_translates_without_synthetic_strict() -> None:
    req = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {
                "format": {"type": "json_schema", "schema": _SAMPLE_SCHEMA}
            },
        }
    )

    spec = effective_structured_output_spec(req)
    assert spec == {"type": "json_schema", "schema": _SAMPLE_SCHEMA}

    chat_body = build_base_request_body(req)
    assert chat_body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_output",
            "schema": _SAMPLE_SCHEMA,
        },
    }
    assert "strict" not in chat_body["response_format"]["json_schema"]

    resp_body = build_responses_request_body(req, reasoning=_REASONING_OFF)
    assert resp_body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "structured_output",
            "schema": _SAMPLE_SCHEMA,
        }
    }
    assert "strict" not in resp_body["text"]["format"]


def test_chat_and_responses_structured_output_round_trip_and_prompt_fallback() -> None:
    ir = _chat_to_ir(
        {
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "reply",
                    "description": "structured reply",
                    "strict": True,
                    "schema": _SAMPLE_SCHEMA,
                },
            },
        }
    )
    assert ir.system is None
    assert ir.output_config == {
        "format": {"type": "json_schema", "schema": _SAMPLE_SCHEMA}
    }
    assert ir.structured_output_spec == {
        "type": "json_schema",
        "name": "reply",
        "strict": True,
        "description": "structured reply",
        "schema": _SAMPLE_SCHEMA,
    }

    # Native Anthropic strips extra metadata and keeps output_config.format.
    anth_body = build_anthropic_messages_body(ir, reasoning=_REASONING_OFF)
    assert anth_body["output_config"] == {
        "format": {"type": "json_schema", "schema": _SAMPLE_SCHEMA}
    }
    assert "system" not in anth_body

    # Chat outbound preserves name, strict, description verbatim.
    chat_out = build_openai_chat_request_body(
        ir,
        reasoning=_REASONING_OFF,
        policy=GENERIC_OPENAI_PROFILE.request_policy,
    )
    assert chat_out["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "reply",
            "strict": True,
            "description": "structured reply",
            "schema": _SAMPLE_SCHEMA,
        },
    }

    # Responses outbound preserves strict=True.
    resp_out = build_responses_request_body(ir, reasoning=_REASONING_OFF)
    assert resp_out["text"] == {
        "format": {
            "type": "json_schema",
            "name": "reply",
            "strict": True,
            "description": "structured reply",
            "schema": _SAMPLE_SCHEMA,
        }
    }

    # DeepSeek preserves structured_output_spec across dump_messages_request.
    ds_out = build_deepseek_request_body(
        ir, reasoning=_REASONING_OFF, provider_id="deepseek"
    )
    assert ds_out["response_format"] == chat_out["response_format"]

    # Q4: unsupported response_format injects prompt fallback before stripping key.
    no_rf_policy = OpenAIChatRequestPolicy(
        provider_name="NO_RF",
        reasoning_replay=GENERIC_OPENAI_PROFILE.request_policy.reasoning_replay,
        unsupported_body_keys=frozenset({"response_format"}),
    )
    no_rf_out = build_openai_chat_request_body(
        ir,
        reasoning=_REASONING_OFF,
        policy=no_rf_policy,
    )
    assert "response_format" not in no_rf_out
    system_msgs = [m for m in no_rf_out["messages"] if m["role"] == "system"]
    assert system_msgs
    assert "named 'reply'" in system_msgs[0]["content"]
    assert '"additionalProperties": false' in system_msgs[0]["content"]

    # json_object & Responses text.format round-trip + Anthropic prompt fallback.
    json_obj_ir = _responses_to_ir(
        {
            "model": "gpt-5",
            "input": "hi",
            "text": {"format": {"type": "json_object"}},
        }
    )
    assert json_obj_ir.structured_output_spec == {"type": "json_object"}
    assert "output_config" not in json_obj_ir.model_dump(exclude_none=True)
    anth_json_obj = build_anthropic_messages_body(json_obj_ir, reasoning=_REASONING_OFF)
    assert "output_config" not in anth_json_obj
    assert "single valid JSON object" in anth_json_obj["system"]
    assert build_base_request_body(json_obj_ir)["response_format"] == {
        "type": "json_object"
    }
    assert build_responses_request_body(json_obj_ir, reasoning=_REASONING_OFF)[
        "text"
    ] == {"format": {"type": "json_object"}}


def test_structured_output_classifier_negative_controls() -> None:
    tool_schema_err = _http_400(
        "https://example.invalid/v1/responses",
        {
            "error": {
                "message": "Invalid JSON schema for tools[0]: regex lookaround unsupported",
                "param": "tools",
                "code": "invalid_json_schema",
            }
        },
    )
    assert (
        classify_structured_output_refusal(tool_schema_err, wire_field="text.format")
        is None
    )

    max_len_err = _http_400(
        "https://example.invalid/v1/responses",
        {
            "error": {
                "message": "tools array_above_max_length: maximum 128",
                "param": "text.format",
                "code": "array_above_max_length",
            }
        },
    )
    assert (
        classify_structured_output_refusal(max_len_err, wire_field="text.format")
        is None
    )

    effort_err = _http_400(
        "https://example.invalid/v1/messages",
        {
            "error": {
                "type": "invalid_request_error",
                "message": "output_config: Extra inputs are not permitted for effort",
            }
        },
    )
    assert (
        classify_structured_output_refusal(
            effort_err, wire_field="output_config.format"
        )
        is None
    )

    verbosity_err = _http_400(
        "https://example.invalid/v1/responses",
        {
            "error": {
                "message": "text.verbosity is not supported for this model",
                "param": "text",
            }
        },
    )
    assert (
        classify_structured_output_refusal(verbosity_err, wire_field="text.format")
        is None
    )

    valid_err = _http_400(
        "https://example.invalid/v1/chat/completions",
        {
            "error": {
                "message": "response_format type json_schema is not supported",
                "param": "response_format",
            }
        },
    )
    assert (
        classify_structured_output_refusal(valid_err, wire_field="response_format")
        is not None
    )


@pytest.mark.asyncio
async def test_structured_output_refusal_recovery_across_all_four_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_settings, "get_settings", Settings)

    store = LearnedFactStore(path=tmp_path / "learned_facts.json")
    set_learned_fact_store(store)
    try:
        req = MessagesRequest.model_validate(
            {
                "model": "test-model",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "output_config": {
                    "format": {"type": "json_schema", "schema": _SAMPLE_SCHEMA}
                },
            }
        )

        # 1. AnthropicMessagesProvider
        anth_bodies: list[dict[str, Any]] = []
        anth_provider = AnthropicMessagesProvider(
            ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
            rate_limiter=passthrough_rate_limiter(),
            provider_id="anth_test",
            provider_name="ANTH_TEST",
        )

        async def _fake_anth_post(
            body: dict[str, Any],
        ) -> tuple[httpx.Response, httpx.Request]:
            anth_bodies.append(body)
            if len(anth_bodies) == 1:
                raise _http_400(
                    "https://example.invalid/v1/messages",
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "output_config.format is not supported",
                        },
                    },
                )
            resp = _anthropic_sse_response()
            return resp, resp.request

        monkeypatch.setattr(anth_provider, "_open_stream", _fake_anth_post)
        install_wire_trace()
        async for _ in anth_provider.stream_response(req):
            pass
        assert len(anth_bodies) == 2
        assert "output_config" in anth_bodies[0]
        assert "output_config" not in anth_bodies[1]
        assert "valid JSON" in anth_bodies[1]["system"]
        assert any(
            f.fact_kind == FACT_STRUCTURED_OUTPUT_UNSUPPORTED
            and f.provider_id == "anth_test"
            for f in store.all_facts()
        )
        # Turn 2 skips native output_config from the first attempt.
        install_wire_trace()
        async for _ in anth_provider.stream_response(req):
            pass
        assert len(anth_bodies) == 3
        assert "output_config" not in anth_bodies[2]
        assert "valid JSON" in anth_bodies[2]["system"]

        # 2. OpenAIChatProvider
        chat_bodies: list[dict[str, Any]] = []
        chat_provider = OpenAIChatProvider(
            ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
            profile=GENERIC_OPENAI_PROFILE,
            rate_limiter=passthrough_rate_limiter(),
            provider_id="chat_test",
        )

        def _chat_transport_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            chat_bodies.append(body)
            if len(chat_bodies) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "response_format is not supported for this model",
                            "param": "response_format",
                        }
                    },
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_chat_sse_bytes(),
            )

        chat_provider._client = AsyncOpenAI(
            api_key="sk-test",
            base_url="https://example.invalid/v1",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(_chat_transport_handler)
            ),
        )
        install_wire_trace()
        async for _ in chat_provider.stream_response(req):
            pass
        assert len(chat_bodies) == 2
        assert "response_format" in chat_bodies[0]
        assert "response_format" not in chat_bodies[1]
        assert any(
            m["role"] == "system" and "valid JSON" in m["content"]
            for m in chat_bodies[1]["messages"]
        )
        assert any(
            f.fact_kind == FACT_STRUCTURED_OUTPUT_UNSUPPORTED
            and f.provider_id == "chat_test"
            for f in store.all_facts()
        )
        install_wire_trace()
        async for _ in chat_provider.stream_response(req):
            pass
        assert len(chat_bodies) == 3
        assert "response_format" not in chat_bodies[2]

        # 3. ResponsesTransport
        resp_bodies: list[dict[str, Any]] = []
        transport = ResponsesTransport(
            ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
            base_url="https://example.invalid/v1",
            provider_name="RESP_TEST",
            identity=None,
            api_key="sk-test",
            rate_limiter=passthrough_rate_limiter(),
            memory=store.memory_for("resp_test"),
            provider_id="resp_test",
        )

        def _resp_transport_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            resp_bodies.append(body)
            if len(resp_bodies) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "text.format json_schema is not supported",
                            "param": "text.format",
                        }
                    },
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(_responses_sse_bytes()),
            )

        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_resp_transport_handler)
        )
        built_body, built_headers = transport.build_body(
            req, reasoning=_REASONING_OFF, max_output_tokens=req.max_tokens
        )
        install_wire_trace()
        async for _ in transport.stream(
            req,
            input_tokens=1,
            reasoning=_REASONING_OFF,
            body=built_body,
            headers=built_headers,
            surface_label="responses",
        ):
            pass
        assert len(resp_bodies) == 2
        assert "text" in resp_bodies[0] and "format" in resp_bodies[0]["text"]
        assert "format" not in (resp_bodies[1].get("text") or {})
        assert "valid JSON" in resp_bodies[1]["instructions"]
        assert any(
            f.fact_kind == FACT_STRUCTURED_OUTPUT_UNSUPPORTED
            and f.provider_id == "resp_test"
            for f in store.all_facts()
        )
        turn2_body, turn2_headers = transport.build_body(
            req, reasoning=_REASONING_OFF, max_output_tokens=req.max_tokens
        )
        install_wire_trace()
        async for _ in transport.stream(
            req,
            input_tokens=1,
            reasoning=_REASONING_OFF,
            body=turn2_body,
            headers=turn2_headers,
            surface_label="responses",
        ):
            pass
        assert len(resp_bodies) == 3
        assert "format" not in (resp_bodies[2].get("text") or {})
        assert "valid JSON" in resp_bodies[2]["instructions"]

        # 4. ChatGPTOAuthProvider
        oauth_bodies: list[dict[str, Any]] = []
        oauth_provider = ChatGPTOAuthProvider(
            ProviderConfig(api_key="test_token", base_url=CHATGPT_OAUTH_DEFAULT_BASE),
            rate_limiter=passthrough_rate_limiter(),
            account_id="test_account",
        )

        def _oauth_transport_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            oauth_bodies.append(body)
            if len(oauth_bodies) == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Invalid schema in text.format: not supported",
                            "param": "text.format",
                        }
                    },
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(_responses_sse_bytes()),
            )

        oauth_provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_oauth_transport_handler)
        )
        oauth_req = req.model_copy(update={"model": "gpt-5.4"})
        install_wire_trace()
        async for _ in oauth_provider.stream_response(oauth_req):
            pass
        assert len(oauth_bodies) == 2
        assert "format" in (oauth_bodies[0].get("text") or {})
        assert "format" not in (oauth_bodies[1].get("text") or {})
        assert "valid JSON" in oauth_bodies[1]["instructions"]
        assert any(
            f.fact_kind == FACT_STRUCTURED_OUTPUT_UNSUPPORTED
            and f.provider_id == "chatgpt_oauth"
            for f in store.all_facts()
        )
        install_wire_trace()
        async for _ in oauth_provider.stream_response(oauth_req):
            pass
        assert len(oauth_bodies) == 3
        assert "format" not in (oauth_bodies[2].get("text") or {})
        assert "valid JSON" in oauth_bodies[2]["instructions"]
        assert RUNG_STRUCTURED_OUTPUT == "structured_output"
    finally:
        reset_learned_fact_store()


# ---------------------------------------------------------------------------
# Area 2 & 3: tool_choice="none", parallel_tool_calls, strict, max_tokens=0
# ---------------------------------------------------------------------------


def test_tool_choice_none_with_history_and_max_completion_tokens_zero() -> None:
    ir = _chat_to_ir(
        {
            "model": "gpt-5",
            "max_completion_tokens": 0,
            "max_tokens": 999,
            "tool_choice": "none",
            "messages": [
                {"role": "user", "content": "call"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"q":"x"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ],
        }
    )
    assert ir.max_tokens == 0
    assert ir.tool_choice == {"type": "none"}
    assert ir.tools and [t.name for t in ir.tools] == ["search"]

    ir_no_history = _chat_to_ir(
        {
            "model": "gpt-5",
            "tool_choice": "none",
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search", "parameters": {"type": "object"}},
                }
            ],
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    assert ir_no_history.tools is None
    assert ir_no_history.tool_choice is None

    resp_ir = _responses_to_ir(
        {
            "model": "gpt-5",
            "tool_choice": {"type": "none"},
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "done"},
            ],
        }
    )
    assert resp_ir.tool_choice == {"type": "none"}
    assert resp_ir.tools and [t.name for t in resp_ir.tools] == ["lookup"]

    req_ir = _responses_to_ir(
        {
            "model": "gpt-5",
            "tool_choice": {"type": "required"},
            "parallel_tool_calls": False,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "strict": True,
                    "parameters": {"type": "object"},
                }
            ],
            "input": "hi",
        }
    )
    assert req_ir.tool_choice == {"type": "any", "disable_parallel_tool_use": True}
    assert req_ir.tools and req_ir.tools[0].strict is True


def test_parallel_tool_calls_and_strict_round_trip_and_oauth_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_settings,
        "get_settings",
        lambda: Settings(responses_parallel_tool_calls=True),
    )
    req = MessagesRequest.model_validate(
        {
            "model": "gpt-5.4",
            "max_tokens": 128,
            "tools": [
                {
                    "name": "strict_fn",
                    "input_schema": {"type": "object"},
                    "strict": True,
                },
                {"name": "lax_fn", "input_schema": {"type": "object"}, "strict": False},
                {"name": "plain_fn", "input_schema": {"type": "object"}},
            ],
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "messages": [{"role": "user", "content": "hi"}],
        }
    )

    chat_body = build_base_request_body(req)
    assert chat_body["parallel_tool_calls"] is False
    assert chat_body["tools"][0]["function"]["strict"] is True
    assert chat_body["tools"][1]["function"]["strict"] is False
    assert "strict" not in chat_body["tools"][2]["function"]

    cohere_body = build_openai_chat_request_body(
        req,
        reasoning=_REASONING_OFF,
        policy=OPENAI_CHAT_PROFILES["cohere"].request_policy,
    )
    assert "parallel_tool_calls" not in cohere_body

    resp_body = build_responses_request_body(
        req, reasoning=_REASONING_OFF, parallel_tool_calls=True
    )
    assert resp_body["parallel_tool_calls"] is False
    assert resp_body["tools"][0]["strict"] is True
    assert resp_body["tools"][1]["strict"] is False
    assert "strict" not in resp_body["tools"][2]

    oauth_body = build_chatgpt_oauth_request_body(req, reasoning=_REASONING_OFF)
    assert oauth_body["parallel_tool_calls"] is False

    # Passthrough preserves disable_parallel_tool_use override -> parallel_tool_calls=False.
    passthru_ir = _responses_to_ir(
        {
            "model": "gpt-5.4",
            "parallel_tool_calls": False,
            "tools": [
                {
                    "type": "function",
                    "name": "strict_fn",
                    "strict": True,
                    "parameters": {"type": "object"},
                }
            ],
            "input": "hi",
        }
    )
    passthru_body = build_responses_passthrough_body(
        passthru_ir,
        reasoning=_REASONING_OFF,
        parallel_tool_calls=True,
    )
    assert passthru_body["parallel_tool_calls"] is False
    assert passthru_body["tools"][0]["strict"] is True


@pytest.mark.asyncio
async def test_anthropic_tool_strict_400_strips_and_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_settings, "get_settings", Settings)
    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anth_strict",
        provider_name="ANTH_STRICT",
    )
    seen: list[dict[str, Any]] = []

    async def _fake_open(
        body: dict[str, Any],
    ) -> tuple[httpx.Response, httpx.Request]:
        seen.append(body)
        if len(seen) == 1:
            raise _http_400(
                "https://example.invalid/v1/messages",
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "tools.0.strict: Extra inputs are not permitted",
                    },
                },
            )
        resp = _anthropic_sse_response()
        return resp, resp.request

    monkeypatch.setattr(provider, "_open_stream", _fake_open)
    req = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 128,
            "tools": [
                {
                    "name": "strict_fn",
                    "input_schema": {"type": "object"},
                    "strict": True,
                }
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    install_wire_trace()
    async for _ in provider.stream_response(req):
        pass
    assert len(seen) == 2
    assert seen[0]["tools"][0]["strict"] is True
    assert "strict" not in seen[1]["tools"][0]


# ---------------------------------------------------------------------------
# Area 4: Multimodal tool_result, PDFs, image detail
# ---------------------------------------------------------------------------


def test_multimodal_tool_result_pdfs_and_image_detail(tmp_path: Path) -> None:
    resp_req = _responses_to_ir(
        {
            "model": "gpt-5",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "inspect"},
                        {
                            "type": "input_image",
                            "image_url": _PNG_DATA_URL,
                            "detail": "high",
                        },
                        {
                            "type": "input_file",
                            "filename": "spec.pdf",
                            "file_data": _PDF_DATA_URL,
                        },
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call_img",
                    "name": "snap",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_img",
                    "output": [
                        {"type": "input_text", "text": "captured"},
                        {
                            "type": "input_image",
                            "image_url": _PNG_DATA_URL,
                            "detail": "low",
                        },
                    ],
                },
            ],
        }
    )
    user_blocks = resp_req.messages[0].content
    assert isinstance(user_blocks, list)
    assert isinstance(user_blocks[1], ContentBlockImage)
    assert user_blocks[1].detail == "high"
    assert isinstance(user_blocks[2], ContentBlockDocument)
    assert user_blocks[2].source["media_type"] == "application/pdf"
    tool_msg_blocks = resp_req.messages[2].content
    assert isinstance(tool_msg_blocks, list)
    first_tool_result = tool_msg_blocks[0]
    assert isinstance(first_tool_result, ContentBlockToolResult)
    assert isinstance(first_tool_result.content, list)
    assert first_tool_result.content[1]["detail"] == "low"

    # Without accepts_documents: PDF stripped to marker text.
    chat_no_pdf = build_base_request_body(resp_req, accepts_documents=False)
    assert any(
        isinstance(p, dict) and p.get("text") == USER_DOCUMENT_STRIPPED_TEXT
        for p in chat_no_pdf["messages"][0]["content"]
    )
    assert chat_no_pdf["messages"][0]["content"][1]["image_url"]["detail"] == "high"
    # Tool result image hoisted into synthetic user message with detail="low".
    assert chat_no_pdf["messages"][-1]["role"] == "user"
    assert chat_no_pdf["messages"][-1]["content"][1]["image_url"]["detail"] == "low"

    # With accepts_documents=True: Chat emits type="file", Responses emits type="input_file".
    chat_pdf = build_base_request_body(resp_req, accepts_documents=True)
    assert chat_pdf["messages"][0]["content"][2] == {
        "type": "file",
        "file": {"filename": "spec.pdf", "file_data": _PDF_DATA_URL},
    }
    resp_pdf = build_responses_request_body(
        resp_req, reasoning=_REASONING_OFF, accepts_documents=True
    )
    user_input = resp_pdf["input"][0]["content"]
    assert {
        "type": "input_file",
        "filename": "spec.pdf",
        "file_data": _PDF_DATA_URL,
    } in user_input

    # ChatGPTOAuth always enables accepts_documents=True.
    oauth_pdf = build_chatgpt_oauth_request_body(resp_req, reasoning=_REASONING_OFF)
    assert any(
        isinstance(part, dict) and part.get("type") == "input_file"
        for part in oauth_pdf["input"][0]["content"]
    )

    # Chat inbound image_url inside role:tool & file block & text-only list stays str.
    chat_req = _chat_to_ir(
        {
            "model": "gpt-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read"},
                        {
                            "type": "file",
                            "file": {
                                "filename": "report.pdf",
                                "file_data": _PDF_DATA_URL,
                            },
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": _PNG_DATA_URL, "detail": "auto"},
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "fetch", "arguments": "{}"},
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "echo", "arguments": "{}"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": [
                        {"type": "text", "text": "shot"},
                        {"type": "image_url", "image_url": {"url": _PNG_DATA_URL}},
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_2",
                    "content": [{"type": "text", "text": "plain text only"}],
                },
            ],
        }
    )
    # detail="auto" is omitted on Anthropic wire body.
    anth_body = build_anthropic_messages_body(chat_req, reasoning=_REASONING_OFF)
    assert "detail" not in anth_body["messages"][0]["content"][2]
    assert anth_body["messages"][0]["content"][1]["type"] == "document"
    chat_tool_blocks = chat_req.messages[2].content
    assert isinstance(chat_tool_blocks, list)
    second_result = chat_tool_blocks[1]
    assert isinstance(second_result, ContentBlockToolResult)
    assert second_result.content == "plain text only"

    # Opaque file_id stays an explicit 400 per Q2.
    with pytest.raises(ChatCompletionsConversionError):
        _chat_to_ir(
            {
                "model": "gpt-5",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "file", "file": {"file_id": "file-123"}}],
                    }
                ],
            }
        )
    with pytest.raises(ResponsesConversionError):
        _responses_to_ir(
            {
                "model": "gpt-5",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": "file-123"}],
                    }
                ],
            }
        )

    # HTTP(S) file_url parses into Anthropic URL document block; stripped without document support.
    url_req = _responses_to_ir(
        {
            "model": "gpt-5",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_doc",
                    "name": "get_pdf",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_doc",
                    "output": [
                        {
                            "type": "input_file",
                            "file_url": "https://example.invalid/doc.pdf",
                        }
                    ],
                },
            ],
        }
    )
    assert (
        build_base_request_body(url_req, accepts_documents=False)["messages"][1][
            "content"
        ]
        == TOOL_DOCUMENT_STRIPPED_TEXT
    )

    # model_accepts_pdf reads cached models.dev modalities.input.
    cache_path = tmp_path / "models_dev.json"
    write_models_dev_cache(
        {
            "openai": {
                "models": {
                    "gpt-pdf": {
                        "modalities": {
                            "input": ["text", "image", "pdf"],
                            "output": ["text"],
                        }
                    },
                    "gpt-nopdf": {
                        "modalities": {
                            "input": ["text", "image"],
                            "output": ["text"],
                        }
                    },
                }
            }
        },
        path=cache_path,
    )
    assert model_accepts_pdf("openai", "gpt-pdf", path=cache_path) is True
    assert model_accepts_pdf("openai", "gpt-nopdf", path=cache_path) is False


# ---------------------------------------------------------------------------
# Area 5: Deterministic orphan repair
# ---------------------------------------------------------------------------


def test_deterministic_orphan_tool_repair_across_doors() -> None:
    # Responses: drop orphan without previous_response_id, keep with previous_response_id.
    no_prev = _responses_to_ir(
        {
            "model": "gpt-5",
            "input": [
                {"role": "user", "content": "hi"},
                {
                    "type": "function_call_output",
                    "call_id": "call_orphan",
                    "output": "stale",
                },
            ],
        }
    )
    assert len(no_prev.messages) == 1

    with_prev = _responses_to_ir(
        {
            "model": "gpt-5",
            "previous_response_id": "resp_prev",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call_prev",
                    "output": "continued",
                }
            ],
        }
    )
    assert with_prev.responses_envelope is not None
    assert any(
        item.get("type") == "function_call_output"
        for item in with_prev.responses_envelope["input"]
    )

    # Chat ingress: drops orphan role="tool" while missing tool_call_id still raises 400.
    chat_orphan = _chat_to_ir(
        {
            "model": "gpt-5",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "call_ghost", "content": "x"},
            ],
        }
    )
    assert len(chat_orphan.messages) == 1

    with pytest.raises(ChatCompletionsConversionError):
        _chat_to_ir(
            {
                "model": "gpt-5",
                "messages": [{"role": "tool", "content": "missing id"}],
            }
        )

    # Outbound Chat drops orphan tool_result (no standalone role="tool").
    anth_orphan_ir = MessagesRequest.model_validate(
        {
            "model": "gpt-5",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_ghost",
                            "content": "ghost",
                        },
                        {"type": "text", "text": "continue"},
                    ],
                },
            ],
        }
    )
    chat_body = build_base_request_body(anth_orphan_ir)
    assert all(m["role"] != "tool" for m in chat_body["messages"])

    # Outbound Anthropic strips true orphan tool_result while keeping consecutive_same_role.
    mixed_ir = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "start"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_real",
                            "name": "f",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "note"}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_real",
                            "content": "valid",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_orphan",
                            "content": "orphan",
                        },
                    ],
                },
            ],
        }
    )
    anth_clean = build_anthropic_messages_body(mixed_ir, reasoning=_REASONING_OFF)
    assert anth_clean["messages"][-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_real", "content": "valid"}
    ]
