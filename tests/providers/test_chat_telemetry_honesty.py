"""Telemetry honesty and bidirectional delta extraction for OpenAI Chat streams (8.40.0)."""

from collections.abc import AsyncIterator
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.core import wire_capture
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.cloudflare import CloudflareProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
)
from my_claude_code.providers.openai_chat.reasoning import NoReasoning
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile
from tests.providers.support import passthrough_rate_limiter


@pytest.fixture(autouse=True)
def _untracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the wire trace context var per test."""
    monkeypatch.setattr(
        wire_capture, "_WIRE_TRACE", ContextVar("mcc_chat_telemetry_test", default=None)
    )


def _config() -> ProviderConfig:
    return ProviderConfig(
        api_key="sk-test-telemetry",
        base_url="https://telemetry.invalid/v1",
        rate_limit=100,
        rate_window=60,
        commit_holdback_seconds=0,
    )


def _chunk(
    *,
    content: object = None,
    reasoning_content: object = None,
    reasoning: object = None,
    reasoning_details: object = None,
    finish_reason: str | None = None,
    usage: object = None,
    model_extra: dict | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=reasoning,
        reasoning_details=reasoning_details,
        tool_calls=None,
    )
    if model_extra is not None:
        delta.model_extra = model_extra
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


async def _async_stream(*chunks: SimpleNamespace) -> AsyncIterator[SimpleNamespace]:
    for chunk in chunks:
        yield chunk


def _request(model: str = "test-model") -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=128,
        messages=[Message(role="user", content="hello")],
    )


def test_reasoning_delta_with_field_covers_primary_fallback_bidirectional_and_empty() -> (
    None
):
    primary_rc = OpenAIChatProfile(
        OpenAIChatRequestPolicy("TEST", ReasoningReplayMode.REASONING_CONTENT),
        NoReasoning(),
        reasoning_delta_field="reasoning_content",
        reasoning_delta_fallback_field=None,
    )
    assert primary_rc.reasoning_delta_with_field(
        SimpleNamespace(reasoning_content="rc", reasoning="r")
    ) == ("reasoning_content", "rc")
    assert primary_rc.reasoning_delta_with_field(
        SimpleNamespace(reasoning_content=None, reasoning="r")
    ) == ("reasoning", "r")
    assert primary_rc.reasoning_delta_with_field(
        SimpleNamespace(reasoning_content="", reasoning="r")
    ) == ("reasoning", "r")
    assert primary_rc.reasoning_delta_with_field(
        SimpleNamespace(reasoning_content="", reasoning=None)
    ) == ("reasoning_content", "")
    assert primary_rc.reasoning_delta_with_field(
        SimpleNamespace(reasoning_content=None, reasoning="")
    ) == (None, None)
    assert primary_rc.reasoning_delta_with_field(
        SimpleNamespace(reasoning_content=None, model_extra={"reasoning": "extra-r"})
    ) == ("reasoning", "extra-r")

    primary_r = OpenAIChatProfile(
        OpenAIChatRequestPolicy("TEST", ReasoningReplayMode.REASONING),
        NoReasoning(),
        reasoning_delta_field="reasoning",
        reasoning_delta_fallback_field="reasoning_content",
    )
    assert primary_r.reasoning_delta_with_field(
        SimpleNamespace(reasoning=None, reasoning_content="rc")
    ) == ("reasoning_content", "rc")
    assert primary_r.reasoning_delta_with_field(
        SimpleNamespace(reasoning=None, reasoning_content="")
    ) == ("reasoning_content", "")
    assert (
        primary_r.reasoning_delta(
            SimpleNamespace(reasoning=None, reasoning_content="rc")
        )
        == "rc"
    )


@pytest.mark.asyncio
async def test_response_shape_records_actual_matched_wire_field_not_profile_default() -> (
    None
):
    """M1: profile declaring reasoning_delta_field='reasoning' logs 'reasoning_content' when that arrives."""
    profile = OpenAIChatProfile(
        OpenAIChatRequestPolicy("ZENMUX_LIKE", ReasoningReplayMode.REASONING),
        NoReasoning(),
        reasoning_delta_field="reasoning",
        reasoning_delta_fallback_field="reasoning_content",
    )
    provider = OpenAIChatProvider(
        _config(),
        profile=profile,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="zenmux_like",
    )
    stream = _async_stream(
        _chunk(reasoning_content="step 1"),
        _chunk(reasoning_content=" step 2"),
        _chunk(
            content="answer",
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        ),
    )
    trace = install_wire_trace()
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        _ = [event async for event in provider.stream_response(_request())]
    shape = trace.responses[0]
    assert shape["fields"]["reasoning_content"] == {"deltas": 2, "chars": 13}
    assert "reasoning" not in shape["fields"]
    assert "reasoning" not in shape


@pytest.mark.asyncio
async def test_custom_generic_profile_reads_and_tallies_alternate_reasoning_field() -> (
    None
):
    """M2: custom_* (GENERIC_OPENAI_PROFILE) captures delta.reasoning instead of dropping it."""
    provider = OpenAIChatProvider(
        _config(),
        profile=GENERIC_OPENAI_PROFILE,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="custom_alt",
    )
    stream = _async_stream(
        _chunk(reasoning="thought from alternate field"),
        _chunk(
            content="final",
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        ),
    )
    trace = install_wire_trace()
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        events = [event async for event in provider.stream_response(_request())]
    parsed = parse_sse_text("".join(events))
    thinking_text = "".join(
        ev.data.get("delta", {}).get("thinking", "")
        for ev in parsed
        if ev.event == "content_block_delta"
        and ev.data.get("delta", {}).get("type") == "thinking_delta"
    )
    assert thinking_text == "thought from alternate field"
    shape = trace.responses[0]
    assert shape["fields"]["reasoning"] == {"deltas": 1, "chars": 28}
    assert "reasoning_content" not in shape["fields"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output_reasoning", [True, False])
async def test_response_shape_records_think_tags_and_reasoning_details_without_double_counting(
    output_reasoning: bool,
) -> None:
    """M3 & Q4: <think> tags tally in shape['reasoning'] and reasoning_details in shape['fields']."""
    provider = OpenAIChatProvider(
        _config(),
        profile=openrouter_gateway_profile("OPEN_ROUTER"),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="open_router",
    )
    details = [{"type": "reasoning.text", "text": "hidden detail"}]
    stream = _async_stream(
        _chunk(
            content="<think>plan one</think>visible",
            reasoning_details=details,
        ),
        _chunk(
            content="<think>unclosed",
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        ),
    )
    policy = ReasoningPolicy.on() if output_reasoning else ReasoningPolicy.off()
    trace = install_wire_trace()
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        _ = [
            event
            async for event in provider.stream_response(_request(), reasoning=policy)
        ]
    shape = trace.responses[0]
    # Raw JSON delta.content length stays exact (30 + 15 = 45)
    assert shape["fields"]["content"] == {"deltas": 2, "chars": 45}
    assert shape["fields"]["reasoning_details"]["deltas"] == 1
    assert shape["fields"]["reasoning_details"]["chars"] > 0
    # <think> tags counted under shape["reasoning"] even when output_reasoning=False
    assert shape["reasoning"]["think_tag_deltas"] == 2
    assert shape["reasoning"]["think_tag_chars"] == len("plan one") + len("unclosed")


@pytest.mark.asyncio
async def test_cloudflare_reasoning_delta_not_double_emitted_and_tallied_honestly() -> (
    None
):
    """Cloudflare reading delta.reasoning emits thinking once and records fields['reasoning']."""
    provider = CloudflareProvider(
        _config(),
        account_id="acct-123",
        rate_limiter=passthrough_rate_limiter(),
    )
    stream = _async_stream(
        _chunk(reasoning="cf thought"),
        _chunk(
            content="cf answer",
            finish_reason="stop",
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4),
        ),
    )
    trace = install_wire_trace()
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        events = [
            event
            async for event in provider.stream_response(
                _request("@cf/moonshotai/kimi-k2.6")
            )
        ]
    parsed = parse_sse_text("".join(events))
    thinking_deltas = [
        ev.data.get("delta", {}).get("thinking")
        for ev in parsed
        if ev.event == "content_block_delta"
        and ev.data.get("delta", {}).get("type") == "thinking_delta"
    ]
    assert thinking_deltas == ["cf thought"]
    shape = trace.responses[0]
    assert shape["fields"]["reasoning"] == {"deltas": 1, "chars": len("cf thought")}
    assert "reasoning_content" not in shape["fields"]
