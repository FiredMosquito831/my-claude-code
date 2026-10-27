"""Go preserves native history and the max rung without widening other hosts."""

import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_gating import adapt_reasoning_policy
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.reasoning import (
    ReasoningEffort,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.providers.openai_chat.profiles import (
    OPENAI_CHAT_PROFILES,
    OPENAI_STANDARD_REASONING,
)
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from tests.providers.sse_replay import _build_provider


def _request() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "fixture-go-model",
            "max_tokens": 1024,
            "messages": [
                {"role": "user", "content": "Inspect the fixture."},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Earlier thought.",
                            "signature": "sig",
                        },
                        {"type": "text", "text": "Earlier answer."},
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "Read",
                            "input": {"path": "a"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": "fixture result",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read fixture.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
        }
    )


def _body(
    policy: ReasoningPolicy, request: MessagesRequest | None = None
) -> dict[str, Any]:
    profile = OPENAI_CHAT_PROFILES["opencode_go"]
    return build_openai_chat_request_body(
        request or _request(),
        reasoning=policy,
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id="opencode_go",
        overrides=ModelParameterOverrides(),
    )


@pytest.mark.parametrize(
    ("effort", "wire"),
    [
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("max", "max"),
    ],
)
def test_go_effort_mapping_retains_every_other_rung(effort: str, wire: str) -> None:
    assert (
        _body(ReasoningPolicy.on(effort=ReasoningEffort(effort)))["reasoning_effort"]
        == wire
    )


@pytest.mark.parametrize(
    "policy",
    [
        ReasoningPolicy.off(),
        ReasoningPolicy.on(),
        ReasoningPolicy.provider_default(),
        ReasoningPolicy.adaptive(),
    ],
)
def test_go_does_not_invent_an_off_or_default_rung(policy: ReasoningPolicy) -> None:
    assert "reasoning_effort" not in _body(policy)


@pytest.mark.parametrize(
    "policy", [ReasoningPolicy.off(), ReasoningPolicy.on(effort=ReasoningEffort.MAX)]
)
def test_go_tool_history_uses_native_thinking_even_when_output_is_hidden(
    policy: ReasoningPolicy,
) -> None:
    body = _body(policy)
    assert (
        OPENAI_CHAT_PROFILES["opencode_go"].request_policy.reasoning_replay
        is ReasoningReplayMode.REASONING_CONTENT
    )
    assistant = body["messages"][1]
    assert assistant["content"] == "Earlier answer."
    assert assistant["reasoning_content"] == "Earlier thought."
    assert assistant["tool_calls"][0]["function"]["name"] == "Read"
    assert body["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "fixture result",
    }
    assert body["tools"][0]["function"]["name"] == "Read"


def test_go_keeps_explicit_empty_native_reasoning_and_literal_answer_tags() -> None:
    request = MessagesRequest.model_validate(
        {
            "model": "fixture",
            "messages": [
                {
                    "role": "assistant",
                    "content": "Quote <think>literally</think>.",
                    "reasoning_content": "",
                }
            ],
        }
    )
    body = _body(ReasoningPolicy.provider_default(), request)
    assert body["messages"] == [
        {
            "role": "assistant",
            "content": "Quote <think>literally</think>.",
            "reasoning_content": "",
        }
    ]


@pytest.mark.parametrize(
    ("efforts", "expected"),
    [
        (None, "max"),
        (frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}), "high"),
        (
            frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX}),
            "max",
        ),
    ],
)
def test_go_model_intersection_still_limits_max(
    efforts: frozenset[ReasoningEffort] | None, expected: str
) -> None:
    capability = (
        None
        if efforts is None
        else ModelReasoningCapability(
            can_reason=True, supports_effort_control=True, supported_efforts=efforts
        )
    )
    policy, _ = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        capability,
        dialect=OPENAI_CHAT_PROFILES["opencode_go"].reasoning.dialect,
    )
    assert _body(policy)["reasoning_effort"] == expected


@pytest.mark.parametrize("field_refused", [False, True])
def test_go_learned_rejections_still_override_the_declaration(
    field_refused: bool,
) -> None:
    dialect = narrow_dialect_by_rejections(
        OPENAI_CHAT_PROFILES["opencode_go"].reasoning.dialect,
        {"reasoning_effort": "2026-10-27"} if field_refused else {},
        None if field_refused else {"reasoning_effort": {"max"}},
    )
    if field_refused:
        # Pin the declaration/refusal intersection here. The frozen gate's
        # unknown-model fall-through is a separate pre-existing behavior;
        # do not claim that this profile-only change rewrites that policy.
        assert dialect.effort_values is None
        assert dialect.effort_field == ""
        return
    policy, _ = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX), None, dialect=dialect
    )
    body = _body(policy)
    assert body["reasoning_effort"] == "high"


def test_go_does_not_enable_reasoning_for_a_model_that_cannot_reason() -> None:
    policy, _ = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        ModelReasoningCapability(can_reason=False),
        dialect=OPENAI_CHAT_PROFILES["opencode_go"].reasoning.dialect,
    )
    assert "reasoning_effort" not in _body(policy)
    default_efforts = OPENAI_STANDARD_REASONING.dialect.effort_values
    assert default_efforts is not None
    assert ReasoningEffort.MAX not in default_efforts


def _frame(delta: dict[str, Any], finish: str | None = None) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "fixture-go-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )
        + "\n\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [False, True])
async def test_go_real_stream_sends_max_and_keeps_native_output_semantics(
    hidden: bool,
) -> None:
    provider = _build_provider("opencode_go")
    seen: list[dict[str, Any]] = []
    native = "Thought.</think>Tail remains thinking."

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        # Split the malformed delimiter: native fields remain native; the
        # unrelated ordinary content parser retains its established behavior.
        raw = (
            _frame({"reasoning_content": "Thought.</thi"})
            + _frame({"reasoning_content": "nk>Tail remains thinking."})
            + _frame({"content": "Visible answer."})
            + _frame({}, "stop")
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=raw.encode(),
            request=request,
        )

    provider._client = AsyncOpenAI(
        api_key="fixture-key",
        base_url="http://fixture.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        frames = [
            part
            async for part in provider.stream_response(
                _request(),
                10,
                reasoning=ReasoningPolicy.off()
                if hidden
                else ReasoningPolicy.on(effort=ReasoningEffort.MAX),
            )
        ]
    finally:
        await provider.cleanup()
    assert len(seen) == 1
    assert seen[0]["messages"][1]["reasoning_content"] == "Earlier thought."
    if hidden:
        assert "reasoning_effort" not in seen[0]
    else:
        assert seen[0]["reasoning_effort"] == "max"
    deltas = [
        event.data["delta"]
        for event in parse_sse_text("".join(frames))
        if event.event == "content_block_delta"
    ]
    assert "".join(d.get("text", "") for d in deltas) == "Visible answer."
    assert "".join(d.get("thinking", "") for d in deltas) == ("" if hidden else native)
