"""Tests for PR 2: /messages gateways reasoning, signing, and <think> alignment."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning import (
    ReasoningPreference,
    resolve_reasoning_policy,
)
from my_claude_code.application.reasoning_budget import reconcile_reasoning_budget
from my_claude_code.application.reasoning_gating import adapt_reasoning_policy
from my_claude_code.config.settings import get_settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
    text_content,
    thinking_content,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.reasoning import (
    ReasoningEffort,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.wire_capture import ResponseShape, install_wire_trace
from my_claude_code.providers.anthropic import AnthropicProvider
from my_claude_code.providers.anthropic_messages import (
    ANTHROPIC_GATEWAY_REASONING_DIALECT,
    ANTHROPIC_REASONING_DIALECT,
    build_anthropic_messages_body,
    iter_anthropic_sse_frames,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    profile_with_declared_surfaces,
)
from my_claude_code.providers.recovery import (
    FACT_HISTORY_THINKING_REJECTED,
    LearnedFactStore,
    reset_learned_fact_store,
    set_learned_fact_store,
)
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.providers.support import passthrough_rate_limiter

_SSE_OK = (
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","role":"assistant","content":[]}}\n\n'
    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _request(**extra: Any) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "deepseek-v4.1-flash",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(extra)
    return MessagesRequest.model_validate(payload)


def _custom_messages_provider(provider_id: str = "custom_gw") -> Any:
    profile = profile_with_declared_surfaces(GENERIC_OPENAI_PROFILE, ("messages",))
    return create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test", base_url="https://gw.invalid/v1"),
        passthrough_rate_limiter(),
        profile=profile,
    )


async def _chunks(*items: bytes) -> AsyncIterator[bytes]:
    for item in items:
        yield item


def _sse_bytes(*frames: tuple[str, dict[str, Any]]) -> bytes:
    return b"".join(
        f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        for event, data in frames
    )


def test_gateway_dialect_exported_and_wired_across_messages_gateways() -> None:
    assert ANTHROPIC_GATEWAY_REASONING_DIALECT.effort_field == "output_config.effort"
    assert ANTHROPIC_GATEWAY_REASONING_DIALECT.effort_values == frozenset(
        {
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        }
    )
    assert ANTHROPIC_REASONING_DIALECT.effort_values is None

    native = AnthropicProvider(
        ProviderConfig(api_key="sk-ant", base_url="https://api.anthropic.com"),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anthropic",
    )
    assert native.reasoning_dialect("claude-sonnet-5-5") == ANTHROPIC_REASONING_DIALECT
    assert native._messages._extract_think_tags is False
    assert native._messages._signs_thinking is True

    cc = CommandCodeProvider(
        ProviderConfig(api_key="sk-cc", base_url="https://api.commandcode.ai/provider"),
        rate_limiter=passthrough_rate_limiter(),
    )
    assert (
        cc.reasoning_dialect("claude-sonnet-5-5") == ANTHROPIC_GATEWAY_REASONING_DIALECT
    )
    assert cc._anthropic._signs_thinking is True
    assert cc._anthropic._extract_think_tags is False

    custom = _custom_messages_provider()
    assert custom.reasoning_dialect("deepseek-v4.1-flash") == (
        ANTHROPIC_GATEWAY_REASONING_DIALECT
    )
    assert custom._messages._provider._extract_think_tags is True
    assert custom._messages._provider._signs_thinking is False


@pytest.mark.parametrize(
    "capability",
    [
        None,
        ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supports_toggle_control=True,
            supports_budget_control=False,
            supported_efforts=frozenset(
                {ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX}
            ),
        ),
    ],
    ids=["unknown-cap", "deepseek-effort-only"],
)
def test_gateway_dialect_preserves_max_effort_and_output_config_format(
    monkeypatch: pytest.MonkeyPatch,
    capability: ModelReasoningCapability | None,
) -> None:
    monkeypatch.setenv("REASONING_ADAPTIVE", "client")
    get_settings.cache_clear()
    try:
        req = _request(output_config={"format": {"type": "json_schema"}})
        policy = ReasoningPolicy.on(effort=ReasoningEffort.MAX)
        gated, _ = adapt_reasoning_policy(
            policy, capability, dialect=ANTHROPIC_GATEWAY_REASONING_DIALECT
        )
        reconciled, _ = reconcile_reasoning_budget(gated, effective_output=131072)

        gw_body = build_anthropic_messages_body(
            req,
            reasoning=reconciled,
            adaptive_capable=True,
            effort_capable=True,
        )
        assert gw_body["thinking"] == {"type": "adaptive"}
        assert gw_body["output_config"] == {
            "format": {"type": "json_schema"},
            "effort": "max",
        }
    finally:
        get_settings.cache_clear()


def test_gateway_encoding_handles_budget_only_adaptive_off_and_small_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_cap = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=False,
        supports_toggle_control=True,
        supports_budget_control=True,
    )
    req = _request(output_config={"format": {"type": "json_schema"}})
    gated, _ = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        legacy_cap,
        dialect=ANTHROPIC_GATEWAY_REASONING_DIALECT,
    )
    reconciled, _ = reconcile_reasoning_budget(gated, effective_output=16384)
    legacy_body = build_anthropic_messages_body(
        req, reasoning=reconciled, effort_capable=True
    )
    assert legacy_body["thinking"]["type"] == "enabled"
    assert legacy_body["thinking"]["budget_tokens"] > 0
    assert legacy_body["output_config"] == {"format": {"type": "json_schema"}}

    # Gap C + Gap D: client output_config.effort only + max_tokens <= 1024 + adaptive=off
    monkeypatch.setenv("REASONING_ADAPTIVE", "off")
    get_settings.cache_clear()
    try:
        small_req = _request(max_tokens=512, output_config={"effort": "max"})
        client_policy = resolve_reasoning_policy(small_req, ReasoningPreference.CLIENT)
        small_body = build_anthropic_messages_body(
            small_req,
            reasoning=client_policy,
            adaptive_capable=True,
            effort_capable=True,
        )
        assert "thinking" not in small_body
        assert small_body["output_config"] == {"effort": "max"}

        # Normal max_tokens with adaptive=off keeps both manual budget and effort
        normal_req = _request(max_tokens=4096, output_config={"effort": "max"})
        normal_body = build_anthropic_messages_body(
            normal_req,
            reasoning=resolve_reasoning_policy(normal_req, ReasoningPreference.CLIENT),
            adaptive_capable=True,
            effort_capable=True,
        )
        assert normal_body["thinking"] == {"type": "enabled", "budget_tokens": 4095}
        assert normal_body["output_config"] == {"effort": "max"}

        # OFF strips effort while keeping format, and never leaves {}
        off_body = build_anthropic_messages_body(
            _request(output_config={"effort": "max"}),
            reasoning=ReasoningPolicy.off(),
            effort_capable=True,
        )
        assert "output_config" not in off_body
        assert "thinking" not in off_body
    finally:
        get_settings.cache_clear()


def test_native_anthropic_path_is_unchanged_for_budget_and_off() -> None:
    req = _request(output_config={"effort": "max", "format": {"type": "json_schema"}})
    body = build_anthropic_messages_body(
        req,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX, budget_tokens=4000),
        effort_capable=False,
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert body["output_config"] == {"format": {"type": "json_schema"}}


@pytest.mark.asyncio
async def test_output_config_effort_400_recovery_narrows_value_and_field_and_preserves_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REASONING_ADAPTIVE", "client")
    get_settings.cache_clear()
    try:
        # 1) Value-level 400 ("Invalid effort 'xhigh'") without naming output_config
        seen_bodies: list[dict[str, Any]] = []

        def value_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_bodies.append(body)
            effort = (body.get("output_config") or {}).get("effort")
            if effort == "xhigh":
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Invalid effort 'xhigh'",
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
            )

        provider = _custom_messages_provider("custom_val")
        inner = provider._messages._provider
        inner._client = httpx.AsyncClient(transport=httpx.MockTransport(value_handler))
        try:
            install_wire_trace()
            req = _request(output_config={"format": {"type": "json_schema"}})
            _ = [
                ev
                async for ev in provider._messages.stream(
                    req,
                    input_tokens=10,
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.XHIGH),
                )
            ]
            assert len(seen_bodies) == 2
            assert seen_bodies[0]["output_config"] == {
                "format": {"type": "json_schema"},
                "effort": "xhigh",
            }
            assert seen_bodies[1]["output_config"] == {
                "format": {"type": "json_schema"}
            }
            narrowed = provider.reasoning_dialect("deepseek-v4.1-flash")
            assert narrowed.effort_values is not None
            assert ReasoningEffort.XHIGH not in narrowed.effort_values
            assert ReasoningEffort.HIGH in narrowed.effort_values
        finally:
            await provider.cleanup()

        # 2) Whole-field 400 ("output_config.effort: Extra inputs are not permitted")
        seen_bodies.clear()

        def field_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_bodies.append(body)
            if "effort" in (body.get("output_config") or {}):
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "output_config.effort: Extra inputs are not permitted",
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
            )

        provider2 = _custom_messages_provider("custom_field")
        inner2 = provider2._messages._provider
        inner2._client = httpx.AsyncClient(transport=httpx.MockTransport(field_handler))
        try:
            install_wire_trace()
            _ = [
                ev
                async for ev in provider2._messages.stream(
                    _request(),
                    input_tokens=10,
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            ]
            assert len(seen_bodies) == 2
            assert seen_bodies[0]["output_config"] == {"effort": "max"}
            assert "output_config" not in seen_bodies[1]
            narrowed2 = provider2.reasoning_dialect("deepseek-v4.1-flash")
            assert narrowed2.effort_values is None
            assert narrowed2.budget is True
            assert narrowed2.adaptive is True
        finally:
            await provider2.cleanup()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_gateway_adaptive_400_fallback_preserves_output_config_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REASONING_ADAPTIVE", "client")
    get_settings.cache_clear()
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_bodies.append(body)
        if (body.get("thinking") or {}).get("type") == "adaptive":
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking.type: 'adaptive' is not supported",
                    },
                },
                request=request,
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
        )

    provider = _custom_messages_provider("custom_adaptive_fb")
    inner = provider._messages._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        install_wire_trace()
        _ = [
            ev
            async for ev in provider._messages.stream(
                _request(),
                input_tokens=10,
                reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
            )
        ]
        assert len(seen_bodies) == 2
        assert seen_bodies[0]["thinking"] == {"type": "adaptive"}
        assert seen_bodies[0]["output_config"] == {"effort": "max"}
        assert seen_bodies[1]["thinking"] == {"type": "enabled", "budget_tokens": 4095}
        assert seen_bodies[1]["output_config"] == {"effort": "max"}
        assert provider.reasoning_dialect("deepseek-v4.1-flash").adaptive is False
        assert (
            provider.reasoning_dialect("deepseek-v4.1-flash").effort_values is not None
        )
    finally:
        await provider.cleanup()
        get_settings.cache_clear()


def test_provider_manager_does_not_strip_output_config_effort_from_openai_params(
    tmp_path: Any,
) -> None:
    from my_claude_code.application.model_metadata import (
        ProviderModelInfo,
        ResponseSurface,
    )
    from my_claude_code.providers.openai_chat.reasoning import NamedEffortReasoning

    class _FakeCache:
        def cached_model_info_tiered(self, provider_id: str, model_id: str) -> Any:
            return (
                ProviderModelInfo(
                    model_id=model_id,
                    supported_parameters=frozenset({"temperature", "top_p", "tools"}),
                ),
                ResolutionTier.PROVIDER_EXACT,
            )

    class _FakeRuntime:
        def __init__(self, dialect: Any) -> None:
            self._dialect = dialect

        def resolve_provider(self, _pid: str) -> Any:
            dialect = self._dialect

            class _P:
                def reasoning_dialect(self, _mid: str) -> Any:
                    return dialect

            return _P()

    class _FakeState:
        def __init__(self, dialect: Any) -> None:
            self.runtime = _FakeRuntime(dialect)

    manager = ProviderRuntimeManager.__new__(ProviderRuntimeManager)
    manager.__dict__["_model_cache"] = _FakeCache()
    manager.__dict__["_current"] = _FakeState(ANTHROPIC_GATEWAY_REASONING_DIALECT)

    gw_dialect = manager.model_reasoning_dialect("custom_gw", "deepseek-v4.1-flash")
    assert gw_dialect == ANTHROPIC_GATEWAY_REASONING_DIALECT

    chat_dialect = NamedEffortReasoning(((ReasoningEffort.HIGH, "high"),)).dialect
    manager.__dict__["_current"] = _FakeState(chat_dialect)
    narrowed_chat = manager.model_reasoning_dialect("custom_chat", "m")
    assert narrowed_chat is not None
    assert narrowed_chat.effort_values is None
    assert ResponseSurface.MESSAGES.value == "messages"


def test_custom_messages_tool_followup_preserves_unsigned_thinking_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REASONING_ADAPTIVE", "off")
    get_settings.cache_clear()
    try:
        provider = _custom_messages_provider("custom_unsigned")
        req = _request(
            messages=[
                {"role": "user", "content": "Read file"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "unsigned plan"},
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "Read",
                            "input": {"p": "a.py"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_1",
                            "content": "ok",
                        }
                    ],
                },
            ]
        )
        dialect = provider.reasoning_dialect("deepseek-v4.1-flash")
        body = build_anthropic_messages_body(
            req,
            reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
            signs_thinking=provider._messages._provider._signs_thinking_for(req.model),
            adaptive_capable=dialect.adaptive,
            effort_capable=dialect.effort_values is not None,
        )
        assert body["messages"][1]["content"][0] == {
            "type": "thinking",
            "thinking": "unsigned plan",
        }
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4095}
        assert body["output_config"] == {"effort": "max"}
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_custom_messages_history_thinking_refusal_learns_without_extra_retry(
    tmp_path: Any,
) -> None:
    reset_learned_fact_store()
    store = LearnedFactStore(path=tmp_path / "facts.json")
    set_learned_fact_store(store)
    try:
        provider = _custom_messages_provider("custom_strict_claude")
        seen_bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_bodies.append(body)
            has_unsigned = any(
                b.get("type") == "thinking" and not b.get("signature")
                for m in body.get("messages", [])
                if m.get("role") == "assistant"
                for b in m.get("content", [])
                if isinstance(b, dict)
            )
            if has_unsigned:
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "messages.1.content.0: Invalid `signature` in `thinking` block",
                        },
                    },
                    request=request,
                )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
            )

        inner = provider._messages._provider
        inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        req = _request(
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "unsigned"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"p": "x"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "done"}
                    ],
                },
            ]
        )
        with pytest.raises(ExecutionFailure, match="400"):
            _ = [
                ev
                async for ev in provider._messages.stream(
                    req,
                    input_tokens=10,
                    reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
                )
            ]
        # 400 fails on first request (1 try + 0 extra retries since neither thinking nor output_config was named at top level)
        assert inner._recovery_memory.history_thinking_refused_for(req.model)
        assert FACT_HISTORY_THINKING_REJECTED

        seen_bodies.clear()
        _ = [
            ev
            async for ev in provider._messages.stream(
                req,
                input_tokens=10,
                reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
            )
        ]
        assert len(seen_bodies) == 1
        assert seen_bodies[0]["messages"][1]["content"] == [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": "x"}}
        ]
        await provider.cleanup()
    finally:
        reset_learned_fact_store()


@pytest.mark.asyncio
async def test_messages_think_tag_normalizer_handles_split_tools_and_hidden_mode() -> (
    None
):
    raw = _sse_bytes(
        ("message_start", {"type": "message_start", "message": {"id": "m1"}}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "text",
                    "text": "<thi",
                    "cache_control": {"type": "ephemeral"},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "nk>plan"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ning</thi"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "nk>answer"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Read",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"p":1}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 12},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )

    shape = ResponseShape(started_at=0.0)
    frames = [
        frame
        async for frame in iter_anthropic_sse_frames(
            _chunks(raw), shape, extract_think_tags=True, output_reasoning=True
        )
    ]
    events = parse_sse_text("".join(frames))
    assert_anthropic_stream_contract(events)
    assert thinking_content(events) == "planning"
    assert text_content(events) == "answer"
    starts = [e.data for e in events if e.event == "content_block_start"]
    assert [(s["index"], s["content_block"]["type"]) for s in starts] == [
        (0, "thinking"),
        (1, "text"),
        (2, "tool_use"),
    ]
    assert starts[1]["content_block"]["cache_control"] == {"type": "ephemeral"}
    payload = shape.payload()
    assert payload["fields"]["thinking"]["chars"] == len("planning")
    assert payload["fields"]["content"]["chars"] == len("answer")
    assert payload["reasoning"]["thinking_blocks"] == 1
    assert payload["tool_calls"]["calls"] == 1

    # output_reasoning=False suppresses <think> while keeping answer and tool_use
    hidden_frames = [
        frame
        async for frame in iter_anthropic_sse_frames(
            _chunks(raw), None, extract_think_tags=True, output_reasoning=False
        )
    ]
    hidden_events = parse_sse_text("".join(hidden_frames))
    assert_anthropic_stream_contract(hidden_events)
    assert thinking_content(hidden_events) == ""
    assert text_content(hidden_events) == "answer"

    # Native Anthropic (extract_think_tags=False) leaves literal <think> untouched
    literal_raw = _sse_bytes(
        ("message_start", {"type": "message_start", "message": {"id": "m2"}}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "<think>literal</think>"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    native_frames = [
        frame
        async for frame in iter_anthropic_sse_frames(
            _chunks(literal_raw), None, extract_think_tags=False
        )
    ]
    native_events = parse_sse_text("".join(native_frames))
    assert thinking_content(native_events) == ""
    assert text_content(native_events) == "<think>literal</think>"


@pytest.mark.asyncio
async def test_gateway_provider_stream_extracts_think_tags_and_native_does_not() -> (
    None
):
    think_sse = _sse_bytes(
        ("message_start", {"type": "message_start", "message": {"id": "m1"}}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "<think>plan</think>answer"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )

    def ok_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=think_sse
        )

    custom = _custom_messages_provider("custom_think_e2e")
    custom._messages._provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok_handler)
    )
    install_wire_trace()
    gw_frames = [
        ev
        async for ev in custom._messages.stream(
            _request(),
            input_tokens=5,
            reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        )
    ]
    gw_events = parse_sse_text("".join(gw_frames))
    assert_anthropic_stream_contract(gw_events)
    assert thinking_content(gw_events) == "plan"
    assert text_content(gw_events) == "answer"
    await custom.cleanup()

    native = AnthropicProvider(
        ProviderConfig(api_key="sk-ant", base_url="https://api.anthropic.com"),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anthropic",
    )
    native._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok_handler)
    )
    native_frames = [
        ev
        async for ev in native.stream_response(
            _request(model="claude-sonnet-5-5"),
            input_tokens=5,
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
        )
    ]
    native_events = parse_sse_text("".join(native_frames))
    assert thinking_content(native_events) == ""
    assert text_content(native_events) == "<think>plan</think>answer"
    await native.cleanup()

    assert (
        narrow_dialect_by_rejections(
            ANTHROPIC_GATEWAY_REASONING_DIALECT, {"output_config": "bad"}, {}
        ).effort_values
        is None
    )
    assert "inferhub" in OPENAI_CHAT_PROFILES
