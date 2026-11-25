"""Comprehensive tests for PR 7: PROTOCOL-STREAMING-SIDECARS-AND-PARITY."""

import json
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from my_claude_code.api.handler_policies import (
    fold_request_tool_changes_with_envelope_sync,
    try_store_from_settings,
)
from my_claude_code.api.handlers import ChatCompletionsHandler, ResponsesHandler
from my_claude_code.application.routing import ModelRouter
from my_claude_code.application.vision_describe import DESCRIBE_FAILED_TEXT
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_chat_completions.input import (
    convert_request_to_anthropic_payload as chat_to_anthropic,
)
from my_claude_code.core.openai_chat_completions.models import (
    OpenAIChatCompletionRequest,
)
from my_claude_code.core.openai_common.errors import (
    openai_error_code_for_failure,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from my_claude_code.core.openai_responses.anthropic_sse import AnthropicSseEvent
from my_claude_code.core.openai_responses.input import (
    convert_request_to_anthropic_payload as responses_to_anthropic,
)
from my_claude_code.core.openai_responses.models import OpenAIResponsesRequest
from my_claude_code.core.openai_responses.streaming import ResponsesStreamAssembler
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.profiles import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
)
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile
from tests.api.support import create_test_app
from tests.providers.support import passthrough_rate_limiter

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
_CLASSIFIER_SYSTEM = (
    "You are a security monitor. Respond with <block>yes</block> or <block>no</block>."
)
_CLASSIFIER_USER = (
    "<transcript>\nUser: review repo\n</transcript>\n<block> immediately."
)


class _RecordingProvider:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events or [
            format_sse_event("message_start", {"type": "message_start", "message": {}}),
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
                    "delta": {"type": "text_delta", "text": "ok"},
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
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            ),
            format_sse_event("message_stop", {"type": "message_stop"}),
        ]
        self.requests: list[MessagesRequest] = []
        self.reasonings: list[ReasoningPolicy] = []

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        return None

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ):
        _ = (input_tokens, request_id)
        self.requests.append(request)
        self.reasonings.append(reasoning)
        for chunk in self.events:
            yield chunk


def test_responses_stream_false_incomplete_on_max_tokens() -> None:
    provider = _RecordingProvider(
        [
            format_sse_event("message_start", {"type": "message_start", "message": {}}),
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
                    "delta": {"type": "text_delta", "text": "cut off"},
                },
            ),
            format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": 0}
            ),
            format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "max_tokens"},
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            ),
            format_sse_event("message_stop", {"type": "message_stop"}),
        ]
    )
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/responses",
            json={"model": "nvidia_nim/test-model", "input": "Hi", "stream": False},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}
    assert body["output"][0]["content"][0]["text"] == "cut off"


def test_responses_streaming_incremental_tool_deltas_and_summary_and_server_tool() -> (
    None
):
    req = OpenAIResponsesRequest.model_validate(
        {"model": "nvidia_nim/test-model", "stream": True}
    )
    assembler = ResponsesStreamAssembler(req)

    start_chunks = assembler.process_anthropic_event(
        AnthropicSseEvent(event="message_start", data={"type": "message_start"})
    )
    assert parse_sse_text("".join(start_chunks))[0].event == "response.created"

    # Incremental function_call_arguments.delta on each input_json_delta (never duplicated on stop).
    assembler.process_anthropic_event(
        AnthropicSseEvent(
            event="content_block_start",
            data={
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "echo",
                    "input": {},
                },
            },
        )
    )
    delta1 = parse_sse_text(
        "".join(
            assembler.process_anthropic_event(
                AnthropicSseEvent(
                    event="content_block_delta",
                    data={
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '{"val":',
                        },
                    },
                )
            )
        )
    )
    delta2 = parse_sse_text(
        "".join(
            assembler.process_anthropic_event(
                AnthropicSseEvent(
                    event="content_block_delta",
                    data={
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
                    },
                )
            )
        )
    )
    stop_events = parse_sse_text(
        "".join(
            assembler.process_anthropic_event(
                AnthropicSseEvent(event="content_block_stop", data={"index": 0})
            )
        )
    )
    assert [e.event for e in delta1] == ["response.function_call_arguments.delta"]
    assert delta1[0].data["delta"] == '{"val":'
    assert [e.event for e in delta2] == ["response.function_call_arguments.delta"]
    assert delta2[0].data["delta"] == '"hi"}'
    assert [e.event for e in stop_events] == [
        "response.function_call_arguments.done",
        "response.output_item.done",
    ]

    # server_tool_use (web_search) + web_search_tool_result + compaction do not break stream.
    ws_start = parse_sse_text(
        "".join(
            assembler.process_anthropic_event(
                AnthropicSseEvent(
                    event="content_block_start",
                    data={
                        "index": 1,
                        "content_block": {
                            "type": "server_tool_use",
                            "id": "srvtoolu_1",
                            "name": "web_search",
                            "input": {"query": "python 3.14"},
                        },
                    },
                )
            )
        )
    )
    assert [e.event for e in ws_start] == [
        "response.output_item.added",
        "response.web_search_call.in_progress",
    ]
    ws_stop = parse_sse_text(
        "".join(
            assembler.process_anthropic_event(
                AnthropicSseEvent(event="content_block_stop", data={"index": 1})
            )
        )
    )
    assert [e.event for e in ws_stop] == [
        "response.web_search_call.searching",
        "response.web_search_call.completed",
        "response.output_item.done",
    ]
    assert (
        assembler.process_anthropic_event(
            AnthropicSseEvent(
                event="content_block_start",
                data={"index": 2, "content_block": {"type": "compaction"}},
            )
        )
        == []
    )
    assembler.process_anthropic_event(
        AnthropicSseEvent(
            event="message_delta",
            data={
                "delta": {"stop_reason": "content_filter"},
                "usage": {"output_tokens": 5},
            },
        )
    )
    terminal = parse_sse_text(
        "".join(
            assembler.process_anthropic_event(
                AnthropicSseEvent(event="message_stop", data={"type": "message_stop"})
            )
        )
    )
    assert terminal[-1].event == "response.incomplete"
    assert terminal[-1].data["response"]["incomplete_details"] == {
        "reason": "content_filter"
    }
    assert terminal[-1].data["response"]["output"][1]["type"] == "web_search_call"
    assert terminal[-1].data["response"]["output"][1]["action"] == {
        "type": "search",
        "query": "python 3.14",
    }


def test_responses_streaming_raw_reasoning_when_summary_suppressed() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "nvidia_nim/test-model",
            "reasoning": {"effort": "high", "summary": "none"},
            "stream": True,
        }
    )
    assembler = ResponsesStreamAssembler(req)
    events_in = [
        AnthropicSseEvent(event="message_start", data={"type": "message_start"}),
        AnthropicSseEvent(
            event="content_block_start",
            data={"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        ),
        AnthropicSseEvent(
            event="content_block_delta",
            data={
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "raw step"},
            },
        ),
        AnthropicSseEvent(event="content_block_stop", data={"index": 0}),
        AnthropicSseEvent(event="message_stop", data={"type": "message_stop"}),
    ]
    out_events = []
    for ev in events_in:
        out_events.extend(
            parse_sse_text("".join(assembler.process_anthropic_event(ev)))
        )
    names = [e.event for e in out_events]
    assert "response.reasoning_text.delta" in names
    assert "response.reasoning_summary_text.delta" not in names
    assert out_events[-1].data["response"]["output"][0]["content"] == [
        {"type": "reasoning_text", "text": "raw step"}
    ]


def test_responses_transport_passthrough_for_openai_vs_gateway() -> None:
    raw = OpenAIResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-terra",
            "instructions": "Top instructions",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "mid dev"}],
                },
                {
                    "type": "compaction",
                    "id": "cmp_1",
                    "encrypted_content": "cmp-secret",
                },
                {"type": "message", "role": "user", "content": "Hello"},
            ],
            "prompt_cache_key": "pcache-123",
            "service_tier": "priority",
            "truncation": "auto",
        }
    )
    ir = MessagesRequest(**responses_to_anthropic(raw))
    reasoning = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)

    openai_transport = ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://api.openai.com/v1"),
        base_url="https://api.openai.com/v1",
        provider_name="OPENAI",
        identity=None,
        api_key="sk-test",
        rate_limiter=passthrough_rate_limiter(),
        provider_id="openai",
    )
    openai_body, _ = openai_transport.build_body(
        ir, reasoning=reasoning, max_output_tokens=1024
    )
    assert openai_body["instructions"] == "Top instructions"
    assert [item["type"] for item in openai_body["input"]] == [
        "message",
        "compaction",
        "message",
    ]
    assert openai_body["input"][0]["role"] == "developer"
    assert openai_body["prompt_cache_key"] == "pcache-123"
    assert openai_body["service_tier"] == "priority"
    assert openai_body["truncation"] == "auto"

    opencode_transport = ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        base_url="https://opencode.ai/zen/v1",
        provider_name="OPENCODE",
        identity=None,
        api_key="sk-test",
        rate_limiter=passthrough_rate_limiter(),
        provider_id="opencode",
    )
    opencode_body, _ = opencode_transport.build_body(
        ir, reasoning=reasoning, max_output_tokens=1024
    )
    assert not any(item.get("type") == "compaction" for item in opencode_body["input"])
    assert "service_tier" not in opencode_body
    assert "truncation" not in opencode_body


def test_chat_envelope_replay_overrides_and_unsupported_keys_and_cross_door() -> None:
    raw = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "Be brief"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_PNG_B64}",
                                "detail": "high",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": "thinking done",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "abc"}
                    ],
                },
                {"role": "user", "content": "Continue"},
            ],
            "seed": 42,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.25,
            "logprobs": True,
            "top_logprobs": 2,
            "service_tier": "priority",
            "store": True,
            "prompt_cache_key": "chat-key-1",
            "parallel_tool_calls": True,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                    },
                },
            },
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "strict": True,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
    )
    ir = MessagesRequest(**chat_to_anthropic(raw))
    assert ir.chat_envelope is not None
    assert "chat_envelope" not in ir.model_dump(mode="json", exclude_none=True)
    copied = ir.model_copy(deep=True)
    assert copied.chat_envelope == ir.chat_envelope
    assert copied.chat_envelope is not ir.chat_envelope

    # OpenRouter profile replays scalars + reasoning_details + strict + detail + response_format.
    or_body = build_openai_chat_request_body(
        ir,
        reasoning=ReasoningPolicy.off(),
        policy=openrouter_gateway_profile("OPEN_ROUTER").request_policy,
        provider_id="open_router",
        overrides=ModelParameterOverrides(),
    )
    assert or_body["seed"] == 42
    assert or_body["frequency_penalty"] == 0.5
    assert or_body["presence_penalty"] == 0.25
    assert or_body["logprobs"] is True
    assert or_body["top_logprobs"] == 2
    assert or_body["service_tier"] == "priority"
    assert or_body["store"] is True
    assert or_body["prompt_cache_key"] == "chat-key-1"
    assert or_body["parallel_tool_calls"] is True
    assert or_body["response_format"]["type"] == "json_schema"
    assert or_body["tools"][0]["function"]["strict"] is True
    assert or_body["messages"][1]["content"][1]["image_url"]["detail"] == "high"
    assert or_body["messages"][2]["reasoning_details"] == [
        {"type": "reasoning.encrypted", "data": "abc"}
    ]
    # No duplicate JSON instruction in system prompt when response_format is native.
    assert or_body["messages"][0]["content"] == "Be brief"

    # Cohere strips unsupported keys cleanly.
    cohere_body = build_openai_chat_request_body(
        ir,
        reasoning=ReasoningPolicy.off(),
        policy=OPENAI_CHAT_PROFILES["cohere"].request_policy,
        provider_id="cohere",
        overrides=ModelParameterOverrides(),
    )
    for forbidden in ("service_tier", "store", "top_logprobs", "parallel_tool_calls"):
        assert forbidden not in cohere_body
    assert "reasoning_details" not in cohere_body["messages"][2]

    # Operator model overrides still win over chat_envelope.
    overrides = ModelParameterOverrides(
        providers={"openai": {"seed": 999, "service_tier": None}}
    )
    overridden = build_openai_chat_request_body(
        ir,
        reasoning=ReasoningPolicy.off(),
        policy=GENERIC_OPENAI_PROFILE.request_policy,
        provider_id="openai",
        overrides=overrides,
    )
    assert overridden["seed"] == 999
    assert "service_tier" not in overridden


def test_openai_error_code_and_type_parity() -> None:
    cases = [
        (FailureKind.RATE_LIMIT, 429, "rate_limit_error", "rate_limit_exceeded"),
        (FailureKind.QUOTA, 402, "insufficient_quota", "insufficient_quota"),
        (FailureKind.PERMISSION, 402, "insufficient_quota", "insufficient_quota"),
        (
            FailureKind.CONTEXT_LENGTH,
            400,
            "invalid_request_error",
            "context_length_exceeded",
        ),
        (FailureKind.AUTHENTICATION, 401, "invalid_request_error", "invalid_api_key"),
        (FailureKind.INVALID_REQUEST, 404, "invalid_request_error", "model_not_found"),
        (FailureKind.OVERLOADED, 529, "server_error", "server_error"),
        (FailureKind.TIMEOUT, 504, "server_error", "timeout"),
    ]
    for kind, status, expected_type, expected_code in cases:
        failure = ExecutionFailure(
            kind=kind, status_code=status, message="boom", retryable=False
        )
        assert openai_error_type_for_failure(failure) == expected_type
        assert openai_error_code_for_failure(failure) == expected_code
        payload = openai_failure_payload(failure)
        assert payload["error"]["type"] == expected_type
        assert payload["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_handler_policies_parity_describe_blind_fallback_and_safety_and_store_guard() -> (
    None
):
    # try_store_from_settings must never raise when set_request_log_path() wasn't called.
    with patch(
        "my_claude_code.api.handler_policies.store_from_settings",
        side_effect=RuntimeError("unregistered"),
    ):
        assert try_store_from_settings(Settings()) is None

    # fold_request_tool_changes_with_envelope_sync invalidates stale envelope tools/input.
    req_with_tool_change = MessagesRequest.model_validate(
        {
            "model": "gpt-4o",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "tool_addition",
                            "tool": {
                                "type": "tool_definition",
                                "definition": {
                                    "name": "added_tool",
                                    "input_schema": {
                                        "type": "object",
                                        "properties": {},
                                    },
                                },
                            },
                        }
                    ],
                },
                {"role": "user", "content": "hi"},
            ],
            "responses_envelope": {
                "input": [{"role": "user", "content": "hi"}],
                "service_tier": "priority",
            },
            "chat_envelope": {"messages": [], "tools": [], "seed": 7},
        }
    )
    folded = fold_request_tool_changes_with_envelope_sync(
        req_with_tool_change, request_id="req_fold"
    )
    assert folded.responses_envelope == {"service_tier": "priority"}
    assert folded.chat_envelope == {"seed": 7}

    # Blind-fallback describe failure on /v1/responses MUST invalidate responses_envelope["input"]
    # so the blind fallback model receives placeholder text instead of raw input_image.
    settings = Settings.model_validate(
        {
            "MODEL_SONNET": "nvidia_nim/dead-vision",
            "MODEL_VISION": "nvidia_nim/dead-vision",
            "VISION_ADAPTER_MODE": "describe",
            "VISION_DESCRIBE_MODEL": "nvidia_nim/dead-vision",
            "REQUEST_LOG_ENABLED": False,
        }
    )

    captured_bodies: list[dict[str, Any]] = []

    class _MockTransportProvider:
        def __init__(self) -> None:
            self.transport = ResponsesTransport(
                ProviderConfig(api_key="sk-test", base_url="https://api.openai.com/v1"),
                base_url="https://api.openai.com/v1",
                provider_name="OPENAI",
                identity=None,
                api_key="sk-test",
                rate_limiter=passthrough_rate_limiter(),
                provider_id="openai",
            )

        def throttle_remaining(self, model: str | None = None) -> float:
            return 0.0

        @property
        def credential_label(self) -> str | None:
            return None

        def preflight_stream(
            self, request: MessagesRequest, *, reasoning: ReasoningPolicy
        ) -> None:
            return None

        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ):
            _ = (input_tokens, request_id)
            if request.model == "dead-vision":
                raise ExecutionFailure(
                    kind=FailureKind.UNAVAILABLE,
                    status_code=503,
                    message="vision model offline",
                    retryable=False,
                )
            body, _ = self.transport.build_body(
                request, reasoning=reasoning, max_output_tokens=256
            )
            captured_bodies.append(body)
            yield format_sse_event(
                "message_start", {"type": "message_start", "message": {}}
            )
            yield format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield format_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "handled"},
                },
            )
            yield format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": 0}
            )
            yield format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
            yield format_sse_event("message_stop", {"type": "message_stop"})

    provider = _MockTransportProvider()
    settings = Settings.model_validate(
        {
            "MODEL_SONNET": "nvidia_nim/blind-text-model",
            "MODEL_VISION": "nvidia_nim/dead-vision",
            "VISION_ADAPTER_MODE": "describe",
            "VISION_DESCRIBE_MODEL": "nvidia_nim/dead-vision",
            "REQUEST_LOG_ENABLED": False,
        }
    )
    router = ModelRouter(
        settings,
        vision_lookup=lambda _p, m: {
            "blind-text-model": False,
            "dead-vision": True,
        }.get(m),
    )
    handler = ResponsesHandler(
        settings,
        provider_resolver=lambda _: provider,
        model_router=router,
    )
    resp = await handler.create(
        OpenAIResponsesRequest.model_validate(
            {
                "model": "sonnet",
                "stream": False,
                "service_tier": "priority",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe"},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/png;base64,{_PNG_B64}",
                            },
                        ],
                    }
                ],
            }
        )
    )
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 200
    assert len(captured_bodies) == 1
    sent_body = captured_bodies[0]
    assert sent_body["service_tier"] == "priority"
    serialized = json.dumps(sent_body)
    assert "input_image" not in serialized
    assert DESCRIBE_FAILED_TEXT in serialized

    # ChatCompletionsHandler applies safety classifier no-thinking policy.
    chat_provider = _RecordingProvider()
    chat_handler = ChatCompletionsHandler(
        Settings.model_validate({"REQUEST_LOG_ENABLED": False}),
        provider_resolver=lambda _: chat_provider,
    )
    chat_resp = await chat_handler.create(
        OpenAIChatCompletionRequest.model_validate(
            {
                "model": "nvidia_nim/test-model",
                "stream": False,
                "messages": [
                    {"role": "system", "content": _CLASSIFIER_SYSTEM},
                    {"role": "user", "content": _CLASSIFIER_USER},
                ],
            }
        )
    )
    assert isinstance(chat_resp, JSONResponse)
    assert chat_resp.status_code == 200
    assert chat_provider.reasonings == [ReasoningPolicy.off()]
