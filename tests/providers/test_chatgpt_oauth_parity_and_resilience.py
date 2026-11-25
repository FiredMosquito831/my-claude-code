"""Unit and integration tests for PR 4: ChatGPT OAuth parity, zstd, aliasing, and recovery."""

import json
from compression import zstd
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.application.catalogue_model import build_catalogue_models
from my_claude_code.application.catalogues.codex import (
    CODEX_BASE_INSTRUCTIONS,
    build_codex_catalogue,
)
from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ProviderModelInfo,
)
from my_claude_code.config import settings as config_settings
from my_claude_code.config.settings import (
    Settings,
    configured_chatgpt_oauth_service_tier,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import OpenAIToolNameCodec
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.openai_responses import (
    CARRIER_PROTOCOL_RESPONSES,
    ReplayOrigin,
    encode_carrier,
    responses_tool_name_to_anthropic_name,
)
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.codex_catalogue import (
    CodexCatalogue,
    CodexCatalogueEntry,
)
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_DEFAULT_BASE,
    CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES,
)
from my_claude_code.providers.chatgpt_oauth.streaming import (
    ChatGPTOAuthStreamConverter,
)
from my_claude_code.providers.openai_responses.conversion import (
    HISTORY_PLAINTEXT_REPLAYED,
    alias_responses_body_tool_names,
    build_responses_passthrough_body,
    build_responses_request_body,
)
from my_claude_code.providers.openai_responses.tool_schema_dialect import (
    RESPONSES_TOOL_SCHEMA_DIALECT,
    sweep_tool_catalogue,
)
from my_claude_code.providers.recovery import (
    forced_tool_names,
    prune_tool_catalogue,
    trim_tool_catalogue,
)
from my_claude_code.providers.recovery import store as learned_store
from my_claude_code.providers.recovery.tools_count_refusal import _tool_name
from tests.application.test_catalogue_model import FakeRuntime
from tests.providers.support import passthrough_rate_limiter


def _provider(base_url: str = CHATGPT_OAUTH_DEFAULT_BASE) -> ChatGPTOAuthProvider:
    return ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=base_url,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
        account_id="test_account",
    )


def _ok_sse_response() -> httpx.Response:
    async def _sse() -> Any:
        yield (
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
        )

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse(),
    )


# ---------------------------------------------------------------------------
# Item 1: Conditional Content-Encoding: zstd + reactive 413 retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_body_stays_uncompressed_without_content_encoding() -> None:
    captured: list[tuple[dict[str, str], bytes]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            ({k.lower(): v for k, v in request.headers.items()}, request.content)
        )
        return _ok_sse_response()

    provider = _provider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    req = MessagesRequest(
        model="gpt-6-luna",
        system="Be concise.",
        messages=[Message(role="user", content="hello")],
    )
    chunks = [c async for c in provider.stream_response(req)]
    assert any("ok" in c for c in chunks)
    assert len(captured) == 1
    headers, content = captured[0]
    assert len(content) < CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES
    assert "content-encoding" not in headers
    parsed = json.loads(content.decode("utf-8"))
    expected = json.dumps(
        parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert content == expected


@pytest.mark.asyncio
async def test_large_body_compresses_with_zstd() -> None:
    captured: list[tuple[dict[str, str], bytes]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            ({k.lower(): v for k, v in request.headers.items()}, request.content)
        )
        return _ok_sse_response()

    provider = _provider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    huge_text = "x" * CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES
    req = MessagesRequest(
        model="gpt-6-luna",
        system="Be concise.",
        messages=[Message(role="user", content=huge_text)],
    )
    trace = install_wire_trace()
    chunks = [c async for c in provider.stream_response(req)]

    assert any("ok" in c for c in chunks)
    assert len(captured) == 1
    headers, content = captured[0]
    assert headers.get("content-encoding") == "zstd"
    assert len(content) < CHATGPT_OAUTH_ZSTD_THRESHOLD_BYTES
    decompressed = zstd.decompress(content)
    parsed = json.loads(decompressed.decode("utf-8"))
    assert parsed["input"][0]["content"][0]["text"] == huge_text
    assert decompressed == json.dumps(
        parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert len(trace.requests) == 1
    assert trace.requests[0].params["model"] == "gpt-6-luna"


@pytest.mark.asyncio
async def test_413_retries_once_with_zstd() -> None:
    captured: list[tuple[dict[str, str], bytes]] = []
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        captured.append(
            ({k.lower(): v for k, v in request.headers.items()}, request.content)
        )
        if attempts == 1:
            return httpx.Response(413, json={"error": {"message": "Payload too large"}})
        return _ok_sse_response()

    provider = _provider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    req = MessagesRequest(
        model="gpt-6-luna",
        system="Be concise.",
        messages=[Message(role="user", content="hello")],
    )
    chunks = [c async for c in provider.stream_response(req)]
    assert any("ok" in c for c in chunks)
    assert len(captured) == 2
    assert "content-encoding" not in captured[0][0]
    assert captured[1][0].get("content-encoding") == "zstd"
    assert zstd.decompress(captured[1][1]) == captured[0][1]


# ---------------------------------------------------------------------------
# Item 2: Fallback CODEX_BASE_INSTRUCTIONS
# ---------------------------------------------------------------------------


def test_p5_empty_instructions_defaults_to_codex_base() -> None:
    no_system_msg = MessagesRequest(
        model="gpt-6-luna",
        system=None,
        messages=[Message(role="user", content="hi")],
    )
    blank_system_msg = MessagesRequest(
        model="gpt-6-luna",
        system="   ",
        messages=[Message(role="user", content="hi")],
    )
    explicit_system_msg = MessagesRequest(
        model="gpt-6-luna",
        system="Custom prompt",
        messages=[Message(role="user", content="hi")],
    )

    body_none = build_chatgpt_oauth_request_body(
        no_system_msg, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    )
    body_blank = build_chatgpt_oauth_request_body(
        blank_system_msg, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    )
    body_custom = build_chatgpt_oauth_request_body(
        explicit_system_msg, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    )
    assert body_none["instructions"] == CODEX_BASE_INSTRUCTIONS
    assert body_blank["instructions"] == CODEX_BASE_INSTRUCTIONS
    assert body_custom["instructions"] == "Custom prompt"

    generic_body = build_responses_request_body(
        no_system_msg,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        default_instructions=None,
    )
    assert "instructions" not in generic_body

    passthrough_blank = MessagesRequest.model_validate(
        {
            "model": "gpt-6-luna",
            "messages": [{"role": "user", "content": "hi"}],
            "responses_envelope": {
                "model": "gpt-6-luna",
                "instructions": "   ",
                "input": [{"role": "user", "content": "hi"}],
            },
        }
    )
    pt_oauth = build_chatgpt_oauth_request_body(
        passthrough_blank, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    )
    pt_generic = build_responses_passthrough_body(
        passthrough_blank,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        default_instructions=None,
    )
    assert pt_oauth["instructions"] == CODEX_BASE_INSTRUCTIONS
    assert pt_generic["instructions"] == "   "


# ---------------------------------------------------------------------------
# Item 3: Tool/choice aliasing, namespace/custom schema & count, wire notes
# ---------------------------------------------------------------------------


def test_custom_tool_choice_aliasing() -> None:
    long_custom = "mcp__very_long_server_namespace__" + ("c" * 45)
    long_ns_tool = "mcp__very_long_server_namespace__" + ("n" * 45)
    long_fn = "mcp__very_long_server_namespace__" + ("f" * 45)
    req = MessagesRequest.model_validate(
        {
            "model": "gpt-6-luna",
            "messages": [{"role": "user", "content": "hi"}],
            "responses_envelope": {
                "model": "gpt-6-luna",
                "instructions": "Run tools.",
                "input": [
                    {
                        "type": "custom_tool_call",
                        "call_id": "call_1",
                        "name": long_custom,
                        "input": "patch",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_2",
                        "namespace": "my_ns",
                        "name": long_ns_tool,
                        "arguments": "{}",
                    },
                ],
                "tools": [
                    {"type": "custom", "name": long_custom},
                    {
                        "type": "namespace",
                        "name": "my_ns",
                        "tools": [
                            {
                                "type": "function",
                                "name": long_ns_tool,
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": long_fn,
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                ],
                "tool_choice": {
                    "type": "allowed_tools",
                    "mode": "auto",
                    "tools": [
                        {"type": "custom", "name": long_custom},
                        {"type": "function", "name": long_fn},
                    ],
                },
            },
        }
    )
    codec = OpenAIToolNameCodec.from_request(req, max_length=64)
    alias_custom = codec.encode(long_custom)
    alias_ns = codec.encode(long_ns_tool)
    alias_fn = codec.encode(long_fn)
    assert alias_custom != long_custom and len(alias_custom) <= 64
    assert alias_ns != long_ns_tool and len(alias_ns) <= 64
    assert alias_fn != long_fn and len(alias_fn) <= 64

    body = build_responses_passthrough_body(
        req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        tool_name_max_length=64,
    )
    assert body["tools"][0] == {"type": "custom", "name": alias_custom}
    assert body["tools"][1]["name"] == "my_ns"
    assert body["tools"][1]["tools"][0]["name"] == alias_ns
    assert body["tools"][2]["function"]["name"] == alias_fn
    assert body["input"][0]["name"] == alias_custom
    assert body["input"][1]["name"] == alias_ns
    assert body["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [
            {"type": "custom", "name": alias_custom},
            {"type": "function", "name": alias_fn},
        ],
    }

    # Reactive body-level aliasing preserves type: "custom" and aliases namespace/nested.
    unaliased_body = build_responses_passthrough_body(
        req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        tool_name_max_length=None,
    )
    unaliased_body["tool_choice"] = {"type": "custom", "name": long_custom}
    retried = alias_responses_body_tool_names(unaliased_body, codec)
    assert retried is not None
    assert retried["tool_choice"] == {"type": "custom", "name": alias_custom}
    assert retried["tools"][1]["tools"][0]["name"] == alias_ns
    assert retried["input"][1]["name"] == alias_ns

    # Stream converter decodes aliased names inside namespace output items cleanly.
    ledger = AnthropicStreamLedger("msg_1", "gpt-6-luna", input_tokens=0)
    converter = ChatGPTOAuthStreamConverter(ledger, tool_names=codec)
    events = list(
        converter.feed(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "custom_tool_call",
                    "id": "item_1",
                    "call_id": "call_ns_1",
                    "namespace": "my_ns",
                    "name": alias_ns,
                },
            }
        )
    )
    joined = "".join(events)
    expected_ns_name = responses_tool_name_to_anthropic_name(
        long_ns_tool, namespace="my_ns"
    )
    assert f'"name": "{expected_ns_name}"' in joined


def test_namespace_schema_pruning() -> None:
    bad_pattern = r"^(?!forbidden)[a-z]+$"
    unicode_pattern = r"^[^\p{Cc}\p{Cf}]+$"
    tools = [
        {
            "type": "namespace",
            "name": "ns1",
            "tools": [
                {
                    "type": "function",
                    "name": "child_lookaround",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "pattern": bad_pattern}
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "child_unicode",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "pattern": unicode_pattern}
                        },
                    },
                },
            ],
        },
        {
            "type": "function",
            "function": {
                "name": "nested_chat_fn",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string", "pattern": bad_pattern}},
                },
            },
        },
    ]

    pruned, removals = prune_tool_catalogue(
        tools, RESPONSES_TOOL_SCHEMA_DIALECT.refused[0]
    )
    assert removals
    assert any(r.tool == "child_lookaround" for r in removals)
    assert any(r.tool == "nested_chat_fn" for r in removals)
    assert "pattern" not in pruned[0]["tools"][0]["parameters"]["properties"]["field"]
    assert "pattern" not in pruned[1]["function"]["parameters"]["properties"]["x"]

    swept = sweep_tool_catalogue(tools, RESPONSES_TOOL_SCHEMA_DIALECT)
    assert any(t.tool == "child_unicode" for t in swept.translations)
    assert (
        swept.tools[0]["tools"][1]["parameters"]["properties"]["label"]["pattern"]
        != unicode_pattern
    )


def test_tools_count_recovery_nested_and_namespace() -> None:
    assert (
        _tool_name({"type": "function", "function": {"name": "chat_fn"}}) == "chat_fn"
    )
    assert _tool_name({"type": "custom", "custom": {"name": "cust_fn"}}) == "cust_fn"
    assert forced_tool_names(
        {"type": "custom", "custom": {"name": "cust_fn"}}
    ) == frozenset({"cust_fn"})

    tools = [
        {
            "type": "namespace",
            "name": "ns_keep",
            "tools": [{"type": "function", "name": "used_child"}],
        },
        {"type": "function", "function": {"name": "forced_chat"}},
        {"type": "custom", "custom": {"name": "drop_custom"}},
    ]
    body = {
        "input": [{"type": "function_call", "name": "used_child", "arguments": "{}"}],
        "tool_choice": {"type": "function", "function": {"name": "forced_chat"}},
        "tools": tools,
    }
    trimmed = trim_tool_catalogue(tools, max_count=2, body=body)
    assert trimmed is not None
    assert trimmed.dropped == ("drop_custom",)
    assert len(trimmed.tools) == 2
    assert trimmed.tools[0]["name"] == "ns_keep"


def test_passthrough_plaintext_replay_records_wire_note() -> None:
    foreign_origin = ReplayOrigin(
        provider="chatgpt_oauth",
        protocol=CARRIER_PROTOCOL_RESPONSES,
        endpoint="https://other.example/backend-api",
        connection="acct_other",
        model="gpt-6-luna",
    )
    carrier = encode_carrier(
        foreign_origin,
        {"type": "reasoning", "encrypted_content": "secret_blob"},
    )
    req = MessagesRequest.model_validate(
        {
            "model": "gpt-6-luna",
            "messages": [{"role": "user", "content": "hi"}],
            "responses_envelope": {
                "model": "gpt-6-luna",
                "instructions": "Run.",
                "input": [
                    {
                        "type": "reasoning",
                        "encrypted_content": carrier,
                        "summary": [{"type": "summary_text", "text": "Thought A"}],
                    },
                    {"role": "user", "content": "hi"},
                ],
            },
        }
    )
    wire_notes: dict[str, str] = {}
    my_origin = ReplayOrigin(
        provider="chatgpt_oauth",
        protocol=CARRIER_PROTOCOL_RESPONSES,
        endpoint="https://chatgpt.com/backend-api",
        connection="acct_mine",
        model="gpt-6-luna",
    )
    body = build_responses_passthrough_body(
        req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        origin=my_origin,
        plaintext_replay=True,
        wire_notes=wire_notes,
    )
    assert body["input"][0] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "Thought A"}],
    }
    assert HISTORY_PLAINTEXT_REPLAYED in wire_notes
    assert (
        "replayed 1 readable reasoning block" in wire_notes[HISTORY_PLAINTEXT_REPLAYED]
    )


# ---------------------------------------------------------------------------
# Item 4: Exhausted RecoveryMemory reasoning guard & inbound "ultra" -> "max"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_recovery_memory_omits_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = learned_store.LearnedFactStore(path=tmp_path / "facts.json")
    monkeypatch.setattr(learned_store, "_STORE", store)
    memory = store.memory_for("chatgpt_oauth")
    memory.remember_rejection("gpt-5-nano", "reasoning.effort", evidence="400")

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return _ok_sse_response()

    provider = _provider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    req = MessagesRequest(
        model="gpt-5-nano",
        system="Be helpful.",
        messages=[Message(role="user", content="hello")],
    )
    _ = [
        c
        async for c in provider.stream_response(
            req, reasoning=ReasoningPolicy.adaptive()
        )
    ]
    assert len(captured) == 1
    assert "reasoning" not in captured[0]


@pytest.mark.asyncio
async def test_recovery_memory_summary_and_effort_clamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = learned_store.LearnedFactStore(path=tmp_path / "facts.json")
    monkeypatch.setattr(learned_store, "_STORE", store)
    memory = store.memory_for("chatgpt_oauth")
    memory.remember_rejection("gpt-6-luna", "reasoning.summary", evidence="400")
    memory.remember_rejected_values(
        "gpt-6-luna", "reasoning.effort", {"max"}, evidence="400"
    )

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return _ok_sse_response()

    provider = _provider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    req = MessagesRequest.model_validate(
        {
            "model": "gpt-6-luna",
            "system": "Be helpful.",
            "messages": [{"role": "user", "content": "hello"}],
            "output_config": {"effort": "ultra"},
        }
    )
    _ = [
        c
        async for c in provider.stream_response(
            req, reasoning=ReasoningPolicy.on(effort=None)
        )
    ]
    assert len(captured) == 1
    assert captured[0]["reasoning"]["effort"] != "medium"
    assert "summary" not in captured[0]["reasoning"]


@pytest.mark.asyncio
async def test_inbound_ultra_effort_maps_to_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = learned_store.LearnedFactStore(path=tmp_path / "facts.json")
    monkeypatch.setattr(learned_store, "_STORE", store)
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    monkeypatch.setattr(
        provider_module,
        "model_reasoning_capability_from_models_dev",
        lambda _p, _m: None,
    )

    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return _ok_sse_response()

    provider = _provider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    req = MessagesRequest.model_validate(
        {
            "model": "gpt-6.2-sol",
            "messages": [{"role": "user", "content": "hello"}],
            "responses_envelope": {
                "model": "gpt-6.2-sol",
                "instructions": "Run.",
                "input": [{"role": "user", "content": "hello"}],
                "reasoning": {"effort": "ultra"},
            },
        }
    )
    _ = [c async for c in provider.stream_response(req)]
    assert len(captured) == 1
    assert captured[0]["reasoning"]["effort"] == "max"


# ---------------------------------------------------------------------------
# Item 5A & 5B: CHATGPT_OAUTH_SERVICE_TIER & Codex vendor service_tiers
# ---------------------------------------------------------------------------


def test_chatgpt_oauth_service_tier_validation_and_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s_default = Settings.model_validate({})
    assert s_default.chatgpt_oauth_service_tier is None

    s_blank = Settings.model_validate({"CHATGPT_OAUTH_SERVICE_TIER": "  "})
    assert s_blank.chatgpt_oauth_service_tier is None

    s_fast = Settings.model_validate({"CHATGPT_OAUTH_SERVICE_TIER": "FAST"})
    assert s_fast.chatgpt_oauth_service_tier == "priority"

    s_prio = Settings.model_validate({"CHATGPT_OAUTH_SERVICE_TIER": "Priority"})
    assert s_prio.chatgpt_oauth_service_tier == "priority"

    with pytest.raises(ValueError, match="CHATGPT_OAUTH_SERVICE_TIER"):
        Settings.model_validate({"CHATGPT_OAUTH_SERVICE_TIER": "invalid_tier"})

    monkeypatch.setenv("CHATGPT_OAUTH_SERVICE_TIER", "fast")
    config_settings.get_settings.cache_clear()
    try:
        assert configured_chatgpt_oauth_service_tier() == "priority"
        msg_req = MessagesRequest(
            model="gpt-6-luna",
            system="Hi.",
            messages=[Message(role="user", content="hello")],
        )
        body_msg = build_chatgpt_oauth_request_body(
            msg_req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
        )
        assert body_msg["service_tier"] == "priority"

        pt_inbound_wins = MessagesRequest.model_validate(
            {
                "model": "gpt-6-luna",
                "messages": [{"role": "user", "content": "hi"}],
                "responses_envelope": {
                    "model": "gpt-6-luna",
                    "instructions": "Hi.",
                    "input": [{"role": "user", "content": "hi"}],
                    "service_tier": "auto",
                },
            }
        )
        body_pt = build_chatgpt_oauth_request_body(
            pt_inbound_wins, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
        )
        assert body_pt["service_tier"] == "auto"
    finally:
        monkeypatch.delenv("CHATGPT_OAUTH_SERVICE_TIER", raising=False)
        config_settings.get_settings.cache_clear()


@pytest.mark.asyncio
async def test_codex_serialiser_emits_vendor_service_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    vendor_row = {
        "slug": "gpt-6-luna",
        "visibility": "list",
        "additional_speed_tiers": ["fast"],
        "service_tiers": [
            {"id": "priority", "name": "Fast", "description": "Priority tier"}
        ],
        "default_service_tier": "priority",
    }
    fake_catalogue = CodexCatalogue(
        version="0.172.0",
        source_path="/fake/codex",
        entries=(
            CodexCatalogueEntry(
                slug="gpt-6-luna",
                visibility="list",
                raw=vendor_row,
            ),
        ),
    )
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        provider_module.ChatGPTOAuthProvider,
        "_gateway_catalogue_bundle",
        AsyncMock(return_value=({}, {}, None)),
    )
    monkeypatch.setattr(
        provider_module,
        "vendor_client_evidence",
        lambda: {
            "gpt-6-luna": ModelListingEvidence(
                provenance=ModelListingProvenance.VENDOR_CLIENT,
                detail="Codex CLI 0.172.0",
            )
        },
    )
    monkeypatch.setattr(provider_module, "observed_evidence", dict)
    monkeypatch.setattr(provider_module, "load_codex_catalogue", lambda: fake_catalogue)

    provider = _provider()
    infos = await provider.list_model_infos()
    (info,) = infos
    assert info.context_length is None
    assert info.published_row is not None

    runtime = FakeRuntime(
        settings=Settings.model_validate(
            {
                "MODEL": "chatgpt_oauth/gpt-6-luna",
                "MODEL_FALLBACKS": "open_router/plain",
            }
        ),
        cached_infos=(
            replace(info, model_id="chatgpt_oauth/gpt-6-luna"),
            ProviderModelInfo(model_id="open_router/plain"),
        ),
    )
    models = build_catalogue_models(runtime.current_settings(), runtime)
    document, _ = build_codex_catalogue(models)
    by_slug = {entry["slug"]: entry for entry in document["models"]}

    codex_entry = by_slug["chatgpt_oauth/gpt-6-luna"]
    assert codex_entry["additional_speed_tiers"] == ["fast"]
    assert codex_entry["service_tiers"] == [
        {"id": "priority", "name": "Fast", "description": "Priority tier"}
    ]
    assert codex_entry["default_service_tier"] == "priority"

    generic_entry = by_slug["open_router/plain"]
    assert generic_entry["additional_speed_tiers"] == []
    assert generic_entry["service_tiers"] == []
    assert "default_service_tier" not in generic_entry
