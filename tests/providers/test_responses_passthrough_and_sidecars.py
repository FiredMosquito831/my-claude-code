"""Tests for lossless /v1/responses passthrough (P5), ResponsesTransport sidecars (PR C1), and reasoning.summary preservation (T2-Q5)."""

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.model_metadata import ResponseSurface
from my_claude_code.config import settings as config_settings
from my_claude_code.config.provider_catalog import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.client_fingerprint import install_fingerprint
from my_claude_code.core.openai_responses import (
    CARRIER_PROTOCOL_RESPONSES,
    OpenAIResponsesAdapter,
    OpenAIResponsesRequest,
    ReplayOrigin,
    connection_id,
    encode_carrier,
    normalise_endpoint,
)
from my_claude_code.core.openai_responses.anthropic_sse import AnthropicSseEvent
from my_claude_code.core.openai_responses.errors import ResponsesConversionError
from my_claude_code.core.openai_responses.streaming import ResponsesStreamAssembler
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    MISTRAL_CHAT_PROFILE,
    OPENAI_CHAT_PROFILES,
    OpenAIChatProvider,
    profile_with_declared_surfaces,
)
from my_claude_code.providers.openai_responses import ResponsesStreamConverter
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)
from tests.api.support import create_test_app
from tests.providers.support import passthrough_rate_limiter

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _chatgpt_config(**overrides: Any) -> ProviderConfig:
    return ProviderConfig(
        api_key=str(overrides.get("api_key", "test_token")),
        base_url=str(overrides.get("base_url", CHATGPT_OAUTH_DEFAULT_BASE)),
        rate_limit=int(overrides.get("rate_limit", 10)),
        rate_window=int(overrides.get("rate_window", 60)),
        max_concurrency=int(overrides.get("max_concurrency", 5)),
        http_read_timeout=float(overrides.get("http_read_timeout", 300.0)),
        http_write_timeout=float(overrides.get("http_write_timeout", 10.0)),
        http_connect_timeout=float(overrides.get("http_connect_timeout", 60.0)),
        proxy=str(overrides.get("proxy", "")),
    )


def _sample_codex_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "gpt-5.6-terra",
        "instructions": "Base Codex instructions.",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "AGENTS.md permissions"}],
            },
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "Inline system prompt"}],
            },
            {
                "type": "compaction",
                "id": "cmp_1",
                "encrypted_content": "cmp-opaque-123",
            },
            {
                "type": "reasoning",
                "id": "rs_native_1",
                "summary": [{"type": "summary_text", "text": "Checked repo layout"}],
                "content": [{"type": "reasoning_text", "text": "raw reasoning text"}],
                "encrypted_content": "gAAAAAB_native_cipher",
            },
            {
                "type": "message",
                "role": "assistant",
                "encrypted_content": "should-be-stripped",
                "content": [{"type": "output_text", "text": "Running patch"}],
            },
            {
                "type": "local_shell_call",
                "id": "lsc_1",
                "call_id": "call_shell_1",
                "action": {"type": "exec", "command": ["ls", "-la"]},
            },
            {
                "type": "local_shell_call_output",
                "call_id": "call_shell_1",
                "output": "README.md",
            },
            {
                "type": "custom_tool_call",
                "call_id": "call_patch_1",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** End Patch",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch_1",
                "output": "Applied.",
            },
            {
                "type": "function_call",
                "call_id": "call_fn_1",
                "name": "shell",
                "arguments": '{"cmd":"pwd"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_fn_1",
                "output": [
                    {"type": "input_text", "text": "screenshot"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_B64}",
                        "detail": "high",
                    },
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Inspect"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_B64}",
                        "detail": "high",
                    },
                ],
            },
        ],
        "tools": [
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Apply patch",
                "format": {
                    "type": "grammar",
                    "syntax": "lark",
                    "definition": "start: /.+/",
                },
            },
            {
                "type": "function",
                "name": "shell",
                "description": "Run shell",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string", "pattern": "^[^\\p{Cc}]+$"},
                        "mode": {
                            "type": "string",
                            "enum": ["fast", "safe"],
                            "oneOf": [{"const": "fast"}, {"const": "safe"}],
                        },
                        "guard": {"type": "string", "pattern": "^(?!bad).+$"},
                    },
                    "required": ["cmd"],
                },
            },
            {
                "type": "namespace",
                "name": "mcp__docs",
                "description": "Docs tools",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "q": {"type": "string", "pattern": "^(?!x).+$"}
                            },
                        },
                    }
                ],
            },
        ],
        "tool_choice": {"type": "custom", "name": "apply_patch"},
        "parallel_tool_calls": False,
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "codex_out",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                "strict": True,
            },
        },
        "reasoning": {
            "effort": "high",
            "summary": "detailed",
            "context": "all_turns",
        },
        "service_tier": "priority",
        "truncation": "auto",
        "include": ["message.output_text.logprobs"],
        "prompt_cache_key": "codex-thread-42",
        "client_metadata": {"codex_version": "0.169.0"},
        "max_output_tokens": 4096,
        "temperature": 0.2,
        "top_p": 0.9,
        "user": "user-123",
        "safety_identifier": "safe-123",
        "background": True,
        "prompt_cache_retention": "24h",
        "stream": True,
    }
    payload.update(overrides)
    return payload


def _to_messages_request(payload: dict[str, Any]) -> MessagesRequest:
    req = OpenAIResponsesRequest.model_validate(payload)
    anthropic_payload = OpenAIResponsesAdapter().to_anthropic_payload(req)
    return MessagesRequest(**anthropic_payload)


def test_ingress_preserves_lossless_envelope_and_excludes_from_dumps() -> None:
    raw = _sample_codex_payload()
    msg_req = _to_messages_request(raw)

    assert msg_req.responses_envelope is not None
    assert msg_req.responses_envelope["instructions"] == "Base Codex instructions."
    assert msg_req.responses_envelope["service_tier"] == "priority"
    assert msg_req.responses_envelope["text"]["verbosity"] == "low"
    assert msg_req.responses_envelope["reasoning"]["summary"] == "detailed"
    assert msg_req.responses_envelope["reasoning"]["context"] == "all_turns"
    assert msg_req.responses_envelope["parallel_tool_calls"] is False
    assert msg_req.responses_envelope["tools"][0]["type"] == "custom"

    # Cross-door IR remains intact and excludes the sidecar on model_dump().
    dumped = msg_req.model_dump(mode="json", exclude_none=True)
    assert "responses_envelope" not in dumped
    assert any(m["role"] == "assistant" for m in dumped["messages"])

    copied = msg_req.model_copy(deep=True)
    assert copied.responses_envelope is not None
    assert copied.responses_envelope is not msg_req.responses_envelope


def test_ingress_still_rejects_arbitrary_unknown_input_item_types() -> None:
    with pytest.raises(
        ResponsesConversionError, match="Unsupported Responses input item type"
    ):
        _to_messages_request(
            {
                "model": "gpt-5.6-terra",
                "input": [{"type": "bogus_unknown_item", "data": 1}],
            }
        )


def test_p5_chatgpt_oauth_passthrough_preserves_codex_fidelity_and_sanitizes() -> None:
    msg_req = _to_messages_request(_sample_codex_payload())
    wire_notes: dict[str, str] = {}
    body = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        wire_notes=wire_notes,
    )

    assert body["model"] == "gpt-5.6-terra"
    assert body["store"] is False
    assert body["stream"] is True
    assert body["parallel_tool_calls"] is False
    assert body["instructions"] == "Base Codex instructions."
    assert body["service_tier"] == "priority"
    assert body["client_metadata"] == {"codex_version": "0.169.0"}
    assert body["text"]["verbosity"] == "low"
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["prompt_cache_key"] == "codex-thread-42"
    assert "reasoning.encrypted_content" in body["include"]
    assert "message.output_text.logprobs" in body["include"]

    # Stripped Codex-rejected parameters
    for forbidden in (
        "max_tokens",
        "max_output_tokens",
        "prompt_cache_retention",
        "safety_identifier",
        "user",
        "background",
        "temperature",
        "top_p",
        "truncation",
    ):
        assert forbidden not in body

    # Input item order + sanitizers
    input_items = body["input"]
    assert input_items[0]["role"] == "developer"
    assert input_items[1]["role"] == "developer"  # system -> developer in-place
    assert input_items[2]["type"] == "compaction"
    assert input_items[3]["type"] == "reasoning"
    assert input_items[3]["encrypted_content"] == "gAAAAAB_native_cipher"
    assert "content" not in input_items[3]  # reasoning_text stripped
    assert input_items[4]["type"] == "message"
    assert (
        "encrypted_content" not in input_items[4]
    )  # message encrypted_content stripped
    assert input_items[5]["type"] == "local_shell_call"
    assert input_items[6]["type"] == "local_shell_call_output"
    assert input_items[7]["type"] == "custom_tool_call"
    assert input_items[10]["output"][1]["detail"] == "high"
    assert input_items[11]["content"][1]["detail"] == "high"

    # Tools: custom + strict + namespace + schema dialect + redundant oneOf+enum cleanup
    assert body["tools"][0]["type"] == "custom"
    assert body["tools"][0]["format"]["syntax"] == "lark"
    shell_params = body["tools"][1]["parameters"]
    assert body["tools"][1]["strict"] is True
    assert "\\p{Cc}" not in shell_params["properties"]["cmd"]["pattern"]
    assert "oneOf" not in shell_params["properties"]["mode"]
    assert shell_params["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert "pattern" not in shell_params["properties"]["guard"]
    assert body["tools"][2]["type"] == "namespace"
    assert (
        "pattern" not in body["tools"][2]["tools"][0]["parameters"]["properties"]["q"]
    )
    assert "tool_schema_translated" in wire_notes
    assert "tool_schema_pruned" in wire_notes
    assert body["tool_choice"] == {"type": "custom", "name": "apply_patch"}

    # Reasoning policy + summary + context
    assert body["reasoning"] == {
        "effort": "high",
        "summary": "detailed",
        "context": "all_turns",
    }


def test_p5_respects_reasoning_policy_clamping_and_off() -> None:
    """M6 guard: passthrough MUST obey MCC ReasoningPolicy, not raw caller effort."""
    msg_req = _to_messages_request(_sample_codex_payload())

    clamped = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.LOW),
    )
    assert clamped["reasoning"]["effort"] == "low"
    assert clamped["reasoning"]["summary"] == "detailed"
    assert clamped["reasoning"]["context"] == "all_turns"

    disabled = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.off(),
    )
    assert "reasoning" not in disabled


def test_p5_carrier_provenance_on_chatgpt_oauth_passthrough() -> None:
    origin_ok = ReplayOrigin(
        provider="chatgpt_oauth",
        protocol=CARRIER_PROTOCOL_RESPONSES,
        endpoint=normalise_endpoint(CHATGPT_OAUTH_DEFAULT_BASE),
        connection=connection_id("acct-1"),
        model="gpt-5.6-terra",
    )
    origin_other = ReplayOrigin(
        provider="chatgpt_oauth",
        protocol=CARRIER_PROTOCOL_RESPONSES,
        endpoint=normalise_endpoint(CHATGPT_OAUTH_DEFAULT_BASE),
        connection=connection_id("acct-2"),
        model="gpt-5.6-terra",
    )
    native_item = {
        "id": "rs_minted_1",
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "Step 1"}],
        "content": [{"type": "reasoning_text", "text": "raw"}],
        "encrypted_content": "gAAAA_unwrapped",
    }
    carrier = encode_carrier(origin_ok, native_item)
    assert carrier is not None

    msg_req = _to_messages_request(
        {
            "model": "gpt-5.6-terra",
            "input": [
                {
                    "type": "reasoning",
                    "id": "rs_minted_1",
                    "summary": [{"type": "summary_text", "text": "Step 1"}],
                    "encrypted_content": carrier,
                },
                {"role": "user", "content": "Next"},
            ],
        }
    )

    accepted = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        origin=origin_ok,
    )
    assert accepted["input"][0]["encrypted_content"] == "gAAAA_unwrapped"
    assert "content" not in accepted["input"][0]

    # Cross-account carrier is rejected (never leaked raw mcc:reasoning:v1:).
    foreign = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        origin=origin_other,
        plaintext_replay=False,
    )
    assert all(item.get("type") != "reasoning" for item in foreign["input"])


def test_pr_c1_responses_transport_preserves_sidecars_and_gates_codex_proprietary_fields() -> (
    None
):
    raw = _sample_codex_payload()
    msg_req = _to_messages_request(raw)

    # Non-OpenAI destination (OpenCode / Mistral / generic custom):
    non_openai_body = build_responses_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        parallel_tool_calls=False,
        apply_responses_sidecars=True,
        allow_openai_responses_sidecars=False,
    )
    assert non_openai_body["parallel_tool_calls"] is False
    assert non_openai_body["text"] == {"format": raw["text"]["format"]}
    assert "verbosity" not in non_openai_body["text"]
    assert non_openai_body["reasoning"] == {"effort": "high", "summary": "detailed"}
    assert "context" not in non_openai_body["reasoning"]
    assert non_openai_body["tools"][0]["type"] == "custom"
    assert non_openai_body["tools"][1]["strict"] is True
    assert non_openai_body["tool_choice"] == {"type": "custom", "name": "apply_patch"}
    assert "message.output_text.logprobs" in non_openai_body["include"]
    for leaked in ("service_tier", "client_metadata", "truncation"):
        assert leaked not in non_openai_body
    assert not any(
        item.get("type") in {"compaction", "local_shell_call"}
        for item in non_openai_body["input"]
    )

    # OpenAI / Azure destination:
    openai_body = build_responses_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        parallel_tool_calls=False,
        apply_responses_sidecars=True,
        allow_openai_responses_sidecars=True,
    )
    assert openai_body["text"]["verbosity"] == "low"
    assert openai_body["text"]["format"] == raw["text"]["format"]
    assert openai_body["truncation"] == "auto"
    assert openai_body["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_pr_c1_surface_fallback_to_chat_completions_drops_envelope_cleanly() -> (
    None
):
    captured: list[tuple[str, dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append((request.url.path, body))
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported endpoint /responses; use /chat/completions",
                        "type": "invalid_request_error",
                    }
                },
            )
        sse = (
            b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )

        async def _body() -> Any:
            yield sse

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_body()
        )

    profile = profile_with_declared_surfaces(
        GENERIC_OPENAI_PROFILE, ["chat_completions", "responses"]
    )
    provider = OpenAIChatProvider(
        ProviderConfig(
            api_key="sk-test-custom",
            base_url="https://custom-gateway.example/v1",
            rate_limit=10,
            rate_window=60,
        ),
        profile=profile,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="custom_resp",
    )
    provider._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)
    )
    provider._responses._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)
    )

    # Force RESPONSES first in ladder by remembering surface, then verify 400 falls back to Chat.
    from my_claude_code.providers.openai_chat.response_surface import (
        remember_response_surfaces,
    )

    remember_response_surfaces(
        "custom_resp",
        "custom-model",
        (ResponseSurface.RESPONSES, ResponseSurface.CHAT_COMPLETIONS),
        evidence="test setup",
    )

    msg_req = _to_messages_request(_sample_codex_payload(model="custom-model"))
    chunks = [
        chunk
        async for chunk in provider.stream_response(
            msg_req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
        )
    ]
    assert chunks
    assert len(captured) == 2
    assert captured[0][0].endswith("/responses")
    assert "service_tier" not in captured[0][1]
    assert captured[1][0].endswith("/chat/completions")
    chat_body = captured[1][1]
    assert "messages" in chat_body
    assert "responses_envelope" not in chat_body
    assert "service_tier" not in chat_body
    assert "text" not in chat_body


def test_return_path_custom_tool_call_and_namespaced_tool_round_trip() -> None:
    req = OpenAIResponsesRequest.model_validate(
        {
            "model": "gpt-5.6-terra",
            "input": "Edit",
            "parallel_tool_calls": False,
            "text": {"verbosity": "low"},
            "truncation": "auto",
            "tools": [
                {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
                {
                    "type": "namespace",
                    "name": "mcp__docs",
                    "tools": [{"type": "function", "name": "lookup"}],
                },
            ],
        }
    )
    ledger = AnthropicStreamLedger("msg_1", "gpt-5.6-terra", 10)
    converter = ResponsesStreamConverter(ledger)

    upstream_events = [
        {
            "type": "response.output_item.added",
            "item": {
                "id": "ctc_1",
                "type": "custom_tool_call",
                "call_id": "call_patch_9",
                "name": "apply_patch",
            },
        },
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "ctc_1",
            "delta": "*** Begin Patch\n",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "ctc_1",
                "type": "custom_tool_call",
                "call_id": "call_patch_9",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** End Patch",
            },
        },
        {
            "type": "response.output_item.added",
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_ns_1",
                "namespace": "mcp__docs",
                "name": "lookup",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '{"q":"hi"}',
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_ns_1",
                "namespace": "mcp__docs",
                "name": "lookup",
                "arguments": '{"q":"hi"}',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 4},
            },
        },
    ]

    anthropic_sse = [ledger.message_start()]
    for ev in upstream_events:
        anthropic_sse.extend(converter.feed(ev))
    anthropic_sse.extend(converter.finish())

    assembler = ResponsesStreamAssembler(req)
    out_sse: list[str] = []
    for parsed in parse_sse_text("".join(anthropic_sse)):
        out_sse.extend(
            assembler.process_anthropic_event(
                AnthropicSseEvent(event=parsed.event, data=parsed.data)
            )
        )
    out_sse.extend(assembler.finish_if_needed())

    events = parse_sse_text("".join(out_sse))
    completed = events[-1].data["response"]
    assert completed["parallel_tool_calls"] is False
    assert completed["text"] == {"verbosity": "low"}
    assert completed["truncation"] == "auto"
    assert completed["output"][0]["type"] == "custom_tool_call"
    assert completed["output"][0]["input"] == "*** Begin Patch\n*** End Patch"
    assert completed["output"][1]["type"] == "function_call"
    assert completed["output"][1]["namespace"] == "mcp__docs"
    assert completed["output"][1]["name"] == "lookup"


def test_http_responses_to_chatgpt_oauth_and_messages_regression_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATGPT_OAUTH_SESSION_AFFINITY", "true")
    config_settings.get_settings.cache_clear()

    captured_requests: list[tuple[dict[str, str], dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(
            (dict(request.headers), json.loads(request.content.decode("utf-8")))
        )
        sse = (
            b'data: {"type":"response.output_text.delta","delta":"Done"}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\n\n'
        )

        async def _body() -> Any:
            yield sse

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-codex-turn-state": "ts-codex-1",
            },
            content=_body(),
        )

    provider = ChatGPTOAuthProvider(
        _chatgpt_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct-probe",
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    app = create_test_app()
    install_fingerprint({"session-id": "codex-sess-99"})
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/responses",
            headers={"session-id": "codex-sess-99"},
            json=_sample_codex_payload(model="chatgpt_oauth/gpt-5.6-terra"),
        )
        assert resp.status_code == 200

        # /v1/messages stays completely unaffected (responses_envelope is None).
        msg_resp = client.post(
            "/v1/messages",
            headers={"x-claude-code-session-id": "claude-sess-1"},
            json={
                "model": "chatgpt_oauth/gpt-5.6-terra",
                "max_tokens": 256,
                "system": "Be brief",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert msg_resp.status_code == 200

    assert len(captured_requests) == 2
    codex_headers, codex_body = captured_requests[0]
    assert codex_body["instructions"] == "Base Codex instructions."
    assert codex_body["input"][0]["role"] == "developer"
    assert codex_body["tools"][0]["type"] == "custom"
    assert codex_body["service_tier"] == "priority"
    assert "max_output_tokens" not in codex_body
    assert "session-id" in codex_headers

    _, msg_body = captured_requests[1]
    assert msg_body["instructions"] == "Be brief"
    assert "service_tier" not in msg_body


@pytest.mark.asyncio
async def test_responses_transport_profiles_gate_destination_end_to_end() -> None:
    """Verify real ResponsesTransport on opencode, mistral, and custom OpenAI URL."""
    captured: list[tuple[str, dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), json.loads(request.content.decode("utf-8"))))
        sse = (
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
        )

        async def _body() -> Any:
            yield sse

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_body()
        )

    from my_claude_code.providers.openai_chat.response_surface import (
        remember_response_surfaces,
    )

    msg_req = _to_messages_request(_sample_codex_payload(model="gpt-5.6-terra"))

    for provider_id, profile, base_url, expect_openai in (
        (
            "opencode",
            OPENAI_CHAT_PROFILES["opencode"],
            "https://opencode.ai/zen/v1",
            False,
        ),
        ("mistral", MISTRAL_CHAT_PROFILE, "https://api.mistral.ai/v1", False),
        (
            "custom_openai_resp",
            profile_with_declared_surfaces(GENERIC_OPENAI_PROFILE, ["responses"]),
            "https://api.openai.com/v1",
            True,
        ),
    ):
        remember_response_surfaces(
            provider_id,
            "gpt-5.6-terra",
            (ResponseSurface.RESPONSES,),
            evidence="test",
        )
        provider = OpenAIChatProvider(
            ProviderConfig(
                api_key="sk-test",
                base_url=base_url,
                rate_limit=10,
                rate_window=60,
            ),
            profile=profile,
            rate_limiter=passthrough_rate_limiter(),
            provider_id=provider_id,
        )
        provider._responses._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_handler)
        )
        chunks = [
            c
            async for c in provider.stream_response(
                msg_req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
            )
        ]
        assert chunks

        _url, body = captured[-1]
        assert body["parallel_tool_calls"] is False
        assert body["reasoning"]["summary"] == "detailed"
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["tools"][0]["type"] == "custom"
        assert body["tools"][1]["strict"] is True
        if expect_openai:
            assert body["client_metadata"] == {"codex_version": "0.169.0"}
            assert body["input"][0]["role"] == "developer"
            assert body["input"][2]["type"] == "compaction"
            assert body["text"]["verbosity"] == "low"
            assert body["service_tier"] == "priority"
            assert body["truncation"] == "auto"
        else:
            assert "client_metadata" not in body
            assert "verbosity" not in body["text"]
            assert "service_tier" not in body
            assert "truncation" not in body


def test_p5_empty_instructions_omitted_and_compact_omits_store_and_include() -> None:
    from my_claude_code.application.catalogues.codex import CODEX_BASE_INSTRUCTIONS
    from my_claude_code.providers.openai_responses.conversion import (
        build_responses_passthrough_body,
    )

    msg_req = _to_messages_request(
        {
            "model": "gpt-5.6-terra",
            "instructions": "",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Dev rule"}],
                },
                {"role": "user", "content": "Hello"},
            ],
        }
    )
    normal = build_chatgpt_oauth_request_body(
        msg_req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    )
    assert normal["instructions"] == CODEX_BASE_INSTRUCTIONS
    assert normal["store"] is False
    assert normal["include"] == ["reasoning.encrypted_content"]
    assert normal["input"][0]["role"] == "developer"

    generic = build_responses_passthrough_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        default_instructions=None,
    )
    assert "instructions" not in generic

    compacted = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        compact=True,
    )
    assert "store" not in compacted
    assert "include" not in compacted


def test_p5_tool_name_max_length_aliases_function_tools_preserving_custom() -> None:
    long_name = "mcp__very_long_server_namespace__" + ("a" * 50)
    msg_req = _to_messages_request(
        {
            "model": "gpt-5.6-terra",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_long_1",
                    "name": long_name,
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_long_1",
                    "output": "ok",
                },
            ],
            "tools": [
                {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
                {
                    "type": "function",
                    "name": long_name,
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
            "tool_choice": {"type": "function", "name": long_name},
        }
    )
    body = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        tool_name_max_length=64,
    )
    assert body["tools"][0]["type"] == "custom"
    assert body["tools"][0]["name"] == "apply_patch"
    aliased = body["tools"][1]["name"]
    assert aliased != long_name
    assert len(aliased) <= 64
    assert body["tool_choice"]["name"] == aliased
    assert body["input"][0]["name"] == aliased


def test_t2_q5_responses_sidecars_handle_explicit_null_and_concise_summary() -> None:
    concise_req = _to_messages_request(
        {
            "model": "gpt-5.6-terra",
            "input": "Hi",
            "reasoning": {"effort": "high", "summary": "concise"},
        }
    )
    concise_body = build_responses_request_body(
        concise_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        apply_responses_sidecars=True,
    )
    assert concise_body["reasoning"] == {"effort": "high", "summary": "concise"}

    null_summary_req = _to_messages_request(
        {
            "model": "gpt-5.6-terra",
            "input": "Hi",
            "reasoning": {"effort": "high", "summary": None},
        }
    )
    null_body = build_responses_request_body(
        null_summary_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        apply_responses_sidecars=True,
    )
    assert null_body["reasoning"] == {"effort": "high"}


def test_cross_door_anthropic_messages_drops_responses_envelope() -> None:
    from my_claude_code.providers.anthropic_messages.request import (
        build_anthropic_messages_body,
    )

    msg_req = _to_messages_request(_sample_codex_payload(model="claude-sonnet-4-6"))
    body = build_anthropic_messages_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
    )
    assert "responses_envelope" not in body
    assert "service_tier" not in body
    assert "truncation" not in body
    assert "client_metadata" not in body


def test_web_search_tool_loop_clears_stale_envelope_tools_and_input() -> None:
    captured_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content.decode("utf-8")))
        sse = (
            b'data: {"type":"response.output_text.delta","delta":"Searched answer"}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":4,"output_tokens":3}}}\n\n'
        )

        async def _body() -> Any:
            yield sse

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_body()
        )

    provider = ChatGPTOAuthProvider(
        _chatgpt_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="acct-ws",
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/v1/responses",
            json={
                "model": "chatgpt_oauth/gpt-5.6-terra",
                "input": "Search python 3.14",
                "tools": [{"type": "web_search"}],
                "text": {"verbosity": "low"},
                "reasoning": {"effort": "medium", "summary": "concise"},
            },
        )
        assert resp.status_code == 200

    assert len(captured_bodies) == 1
    sent = captured_bodies[0]
    tool_names = [t["name"] for t in sent.get("tools", [])]
    assert "__mcc_web_search" in tool_names
    assert sent["text"] == {"verbosity": "low"}
    assert sent["reasoning"]["summary"] == "concise"


def test_passthrough_preserves_image_generation_call_and_built_in_tools() -> None:
    from my_claude_code.providers.openai_responses.conversion import (
        _sanitize_passthrough_input,
        _sanitize_passthrough_tool,
    )

    raw_items = [
        {
            "type": "image_generation_call",
            "id": "ig_1",
            "role": "assistant",
            "status": "completed",
            "revised_prompt": "A fox in snow",
            "result": "iVBORw0KGgo=",
            "encrypted_content": "ig_opaque",
        },
        {
            "type": "web_search_call",
            "id": "ws_1",
            "status": "completed",
            "action": {"type": "search", "query": "foxes"},
        },
    ]
    sanitized = _sanitize_passthrough_input(
        raw_items,
        tool_names=None,
        origin=None,
        plaintext_replay=False,
        drop_reasoning_items=False,
        history_counts={},
    )
    assert sanitized == raw_items

    img_tool = {"type": "image_generation", "output_format": "png", "partial_images": 1}
    ws_tool = {"type": "web_search", "external_web_access": True}
    assert _sanitize_passthrough_tool(img_tool, tool_names=None) == img_tool
    assert _sanitize_passthrough_tool(ws_tool, tool_names=None) == ws_tool

    msg_req = _to_messages_request(
        {
            "model": "gpt-5.6-terra",
            "instructions": "Base instructions.",
            "input": [
                {
                    "type": "image_generation_call",
                    "id": "ig_prev",
                    "status": "completed",
                    "revised_prompt": "A red lantern",
                    "result": "iVBORw0KGgo=",
                },
                {"role": "user", "content": "Add snow"},
            ],
            "tools": [img_tool, ws_tool],
        }
    )
    body = build_chatgpt_oauth_request_body(
        msg_req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
    )
    assert body["input"][0]["type"] == "image_generation_call"
    assert body["input"][0]["result"] == "iVBORw0KGgo="
    assert body["tools"] == [img_tool, ws_tool]
