"""The two recovery nets are the same code for every provider (6.33.0).

Reject-and-remember and the learned output cap used to live on
``OpenAIChatProvider`` alone. These tests pin the shared implementation and
prove each net end-to-end on the Anthropic Messages dialect and on the
Responses dialect, including the part that makes it worth having: the memory
is consulted on the *next* request, so the 400 is paid once.

The 400 texts quoted here are real. The Anthropic ones are its published error
wording; the Command Code one was captured on 2026-09-02 against
``https://api.commandcode.ai/provider/v1/messages`` with a deliberately invalid
``thinking`` value, and is the reason an unrecognised 400 has to fail visibly:
the gateway answers ``{"type":"error","error":{"type":"invalid_request_error",
"message":"Invalid input"}}`` and names no field at all.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import (
    ReasoningAdaptationKind,
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.provider import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.providers.deepseek.tool_choice import (
    is_deepseek_tool_choice_rejection,
)
from my_claude_code.providers.mistral.reasoning import is_mistral_reasoning_rejection
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    ANTHROPIC_OUTPUT_FIELDS,
    OutputCapRecovery,
    ProviderRecovery,
    ReasoningStripRecovery,
    RecoveryLadder,
    RecoveryMemory,
    clone_body_without_reasoning_field,
    is_bad_request,
    parse_output_token_cap,
    rejected_reasoning_field,
    upstream_complaint,
    upstream_status_code,
)
from tests.providers.support import passthrough_rate_limiter

# --------------------------------------------------------------------------
# Real upstream wordings
# --------------------------------------------------------------------------

ANTHROPIC_CAP_400 = (
    "max_tokens: 100000 > 64000, which is the maximum allowed number of "
    "output tokens for claude-sonnet-4-5-20250929"
)
ANTHROPIC_THINKING_400 = "thinking: Extra inputs are not permitted"
ANTHROPIC_BUDGET_400 = (
    "thinking.budget_tokens: Input should be greater than or equal to 1024"
)
COMMANDCODE_INVALID_INPUT_400 = "Invalid input"


def _anthropic_error_body(message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }


def _httpx_bad_request(message: str, status: int = 400) -> httpx.HTTPStatusError:
    """An error shaped exactly like the Anthropic Messages sender raises."""
    request = httpx.Request("POST", "https://upstream.invalid/v1/messages")
    response = httpx.Response(
        status,
        request=request,
        json=_anthropic_error_body(message),
    )
    return httpx.HTTPStatusError(
        f"UPSTREAM Messages API error {status}", request=request, response=response
    )


class _OpenAIStyleError(Exception):
    """Stand-in for ``openai.BadRequestError``: status plus a parsed body."""

    def __init__(self, message: str, body: Any = None, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# --------------------------------------------------------------------------
# The matcher reads both error carriers
# --------------------------------------------------------------------------


def test_an_httpx_status_error_is_recognised_as_a_bad_request() -> None:
    """The Anthropic path raises httpx, which publishes no ``status_code``."""
    error = _httpx_bad_request(ANTHROPIC_THINKING_400)

    assert getattr(error, "status_code", None) is None
    assert upstream_status_code(error) == 400
    assert is_bad_request(error) is True


def test_an_httpx_5xx_is_not_a_bad_request() -> None:
    assert is_bad_request(_httpx_bad_request("upstream exploded", status=502)) is False


def test_the_anthropic_error_envelope_yields_the_hosts_words() -> None:
    complaint = upstream_complaint(_httpx_bad_request(ANTHROPIC_THINKING_400))

    assert ANTHROPIC_THINKING_400.lower() in complaint


def test_the_command_code_invalid_input_400_names_no_field() -> None:
    """Captured live. A 400 this vague must recover nothing at all."""
    error = _httpx_bad_request(COMMANDCODE_INVALID_INPUT_400)
    body = {"model": "claude-sonnet-5", "max_tokens": 64, "thinking": {"type": "off"}}

    assert is_bad_request(error) is True
    assert rejected_reasoning_field(error, body) is None
    assert parse_output_token_cap(error, fields=ANTHROPIC_OUTPUT_FIELDS) is None


# --------------------------------------------------------------------------
# The output cap, per dialect
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "fields", "expected"),
    [
        (ANTHROPIC_CAP_400, ANTHROPIC_OUTPUT_FIELDS, 64000),
        (
            "max_completion_tokens must be less than or equal to 40960",
            ("max_completion_tokens", "max_tokens"),
            40960,
        ),
        ("max_output_tokens must be at most 100000", ("max_output_tokens",), 100000),
    ],
)
def test_each_dialect_states_its_cap_differently_and_all_are_read(
    message: str, fields: tuple[str, ...], expected: int
) -> None:
    cap = parse_output_token_cap(_httpx_bad_request(message), fields=fields)

    assert cap == expected


def test_a_cap_for_another_dialects_field_is_not_read() -> None:
    """Anthropic never sends ``max_completion_tokens``; a 400 naming it is not ours."""
    error = _httpx_bad_request("max_completion_tokens must be at most 4096")

    assert parse_output_token_cap(error, fields=ANTHROPIC_OUTPUT_FIELDS) is None


def test_the_memory_keeps_the_smallest_cap_a_host_has_stated() -> None:
    memory = RecoveryMemory()

    assert memory.learn_cap("m", 64000) == 64000
    assert memory.learn_cap("m", 8192) == 8192
    assert memory.learn_cap("m", 100000) == 8192
    assert memory.cap_for("m") == 8192


# --------------------------------------------------------------------------
# The reasoning strip, per dialect
# --------------------------------------------------------------------------


def test_an_anthropic_thinking_object_is_a_strippable_reasoning_field() -> None:
    body = {"model": "claude-sonnet-5", "thinking": {"type": "adaptive"}}
    error = _httpx_bad_request(ANTHROPIC_THINKING_400)

    assert rejected_reasoning_field(error, body) == "thinking"
    assert clone_body_without_reasoning_field(body, "thinking") == {
        "model": "claude-sonnet-5"
    }


def test_a_budget_complaint_names_the_thinking_field_it_belongs_to() -> None:
    body = {"model": "claude-sonnet-5", "thinking": {"type": "enabled"}}

    assert rejected_reasoning_field(_httpx_bad_request(ANTHROPIC_BUDGET_400), body) == (
        "thinking"
    )


def test_a_responses_reasoning_block_is_a_strippable_reasoning_field() -> None:
    body = {"model": "gpt-5", "reasoning": {"effort": "high"}}
    error = _httpx_bad_request("reasoning: Extra inputs are not permitted")

    assert rejected_reasoning_field(error, body) == "reasoning"


def test_a_sampling_complaint_never_costs_an_anthropic_request_its_thinking() -> None:
    body = {"model": "claude-sonnet-5", "thinking": {"type": "adaptive"}}
    error = _httpx_bad_request("top_p: Input should be less than or equal to 1")

    assert rejected_reasoning_field(error, body) is None


def test_an_echoed_anthropic_request_is_not_evidence() -> None:
    """A validation error that quotes the body back names only what we sent."""
    request = httpx.Request("POST", "https://upstream.invalid/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Input validation failed",
                "input": {"thinking": {"type": "adaptive"}, "max_tokens": 100},
            },
        },
    )
    error = httpx.HTTPStatusError("400", request=request, response=response)
    body = {"model": "claude-sonnet-5", "thinking": {"type": "adaptive"}}

    assert rejected_reasoning_field(error, body) is None


# --------------------------------------------------------------------------
# The ladder itself
# --------------------------------------------------------------------------


def _rewrite(name: str):
    def rewrite(error: Exception, body: dict[str, Any]) -> dict[str, Any] | None:
        del error
        return {**body, name: True}

    return rewrite


def test_the_ladder_tries_rungs_in_order_and_fires_each_once() -> None:
    ladder = RecoveryLadder(
        (
            ProviderRecovery(kind="first", rewrite=_rewrite("first")).rung(),
            ProviderRecovery(kind="second", rewrite=_rewrite("second")).rung(),
        )
    )
    used: set[str] = set()
    error = _httpx_bad_request("anything")

    first = ladder.next_body(error, {"model": "m"}, used)
    assert first.kind == "first"
    second = ladder.next_body(error, first.body or {}, used)
    assert second.kind == "second"
    assert ladder.next_body(error, second.body or {}, used).retry is False


def test_the_output_cap_rung_may_fire_more_than_once() -> None:
    """Each firing is a new number the host stated, not a repeat of a guess."""
    memory = RecoveryMemory()
    cap = OutputCapRecovery(memory, log_tag="T", fields=ANTHROPIC_OUTPUT_FIELDS)
    ladder = RecoveryLadder((cap.rung(),))
    used: set[str] = set()

    first = ladder.next_body(
        _httpx_bad_request(ANTHROPIC_CAP_400),
        {"model": "m", "max_tokens": 100000},
        used,
    )
    assert first.body == {"model": "m", "max_tokens": 64000}
    second = ladder.next_body(
        _httpx_bad_request("max_tokens: 64000 > 8192, which is the maximum allowed "),
        first.body or {},
        used,
    )
    assert second.body is None  # phrase incomplete -- nothing stated
    third = ladder.next_body(
        _httpx_bad_request(
            "max_tokens: 64000 > 8192, which is the maximum allowed number of "
            "output tokens for claude-haiku-4-5"
        ),
        first.body or {},
        used,
    )
    assert third.body == {"model": "m", "max_tokens": 8192}
    assert memory.cap_for("m") == 8192


def test_the_reasoning_strip_is_the_last_rung() -> None:
    """A provider-specific recovery is strictly the better one; it goes first."""
    ladder = RecoveryLadder(
        (
            ProviderRecovery(kind="provider_specific", rewrite=_rewrite("mine")).rung(),
            ReasoningStripRecovery(log_tag="T").rung(),
        )
    )
    body = {"model": "m", "thinking": {"type": "adaptive"}}

    decision = ladder.next_body(_httpx_bad_request(ANTHROPIC_THINKING_400), body, set())

    assert decision.kind == "provider_specific"
    assert decision.stripped_reasoning_field is None


# --------------------------------------------------------------------------
# End to end: the Anthropic Messages dialect
# --------------------------------------------------------------------------

_SSE = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _messages_provider() -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(
        ProviderConfig(api_key="k", base_url="https://upstream.invalid/v1"),
        provider_name="UPSTREAM",
        rate_limiter=ProviderRateLimiter(
            rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
        ),
    )


def _request(**kwargs: Any) -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-5-20250929",
        max_tokens=100000,
        messages=[Message(role="user", content="ping")],
        stream=True,
        **kwargs,
    )


def _install(provider: AnthropicMessagesProvider, handler: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return handler(sent[-1], len(sent))

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return sent


async def _drain(stream: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in stream]


@pytest.mark.asyncio
async def test_anthropic_learns_its_cap_from_a_400_and_pays_it_only_once() -> None:
    provider = _messages_provider()

    def handler(body: dict[str, Any], _: int) -> httpx.Response:
        if body["max_tokens"] > 64000:
            return httpx.Response(400, json=_anthropic_error_body(ANTHROPIC_CAP_400))
        return httpx.Response(200, content=_SSE)

    sent = _install(provider, handler)

    frames = await _drain(provider.stream_response(_request()))
    assert any("message_stop" in frame for frame in frames)
    assert [item["max_tokens"] for item in sent] == [100000, 64000]

    # The second request never pays the 400: the cap is remembered.
    await _drain(provider.stream_response(_request()))
    assert [item["max_tokens"] for item in sent] == [100000, 64000, 64000]
    await provider.cleanup()


@pytest.mark.asyncio
async def test_anthropic_strips_the_thinking_field_its_host_named_and_remembers() -> (
    None
):
    provider = _messages_provider()
    trace = install_wire_trace()

    def handler(body: dict[str, Any], _: int) -> httpx.Response:
        if "thinking" in body:
            return httpx.Response(
                400, json=_anthropic_error_body(ANTHROPIC_THINKING_400)
            )
        return httpx.Response(200, content=_SSE)

    sent = _install(provider, handler)
    reasoning = ReasoningPolicy.on(budget_tokens=2048)

    frames = await _drain(provider.stream_response(_request(), reasoning=reasoning))

    assert any("message_stop" in frame for frame in frames)
    assert "thinking" in sent[0]
    assert "thinking" not in sent[1]

    # The Models page reads the dialect; it must now say the host refused it.
    dialect = provider.reasoning_dialect("claude-sonnet-4-5-20250929")
    assert dialect.origin is ReasoningDialectOrigin.LEARNED
    assert [field for field, _ in dialect.learned_rejections] == ["thinking"]
    assert dialect.budget is False and dialect.toggle is False

    # The request-detail modal reads the adaptation.
    assert [item.kind for item in trace.reasoning_adaptations] == [
        ReasoningAdaptationKind.SUPPRESSED
    ]
    assert "thinking" in (trace.reasoning_adaptations[0].message or "")
    await provider.cleanup()


@pytest.mark.asyncio
async def test_anthropic_failed_retry_teaches_nothing() -> None:
    """A strip that did not fix it is no evidence the field was the problem."""
    provider = _messages_provider()

    def handler(body: dict[str, Any], _: int) -> httpx.Response:
        del body
        return httpx.Response(400, json=_anthropic_error_body(ANTHROPIC_THINKING_400))

    _install(provider, handler)

    with pytest.raises(ExecutionFailure):
        await _drain(
            provider.stream_response(
                _request(), reasoning=ReasoningPolicy.on(budget_tokens=2048)
            )
        )

    dialect = provider.reasoning_dialect("claude-sonnet-4-5-20250929")
    assert dialect.origin is not ReasoningDialectOrigin.LEARNED
    await provider.cleanup()


@pytest.mark.asyncio
async def test_an_unrecognised_anthropic_400_fails_visibly() -> None:
    """Command Code's real ``Invalid input``. Nothing is guessed at."""
    provider = _messages_provider()

    def handler(body: dict[str, Any], _: int) -> httpx.Response:
        del body
        return httpx.Response(
            400, json=_anthropic_error_body(COMMANDCODE_INVALID_INPUT_400)
        )

    sent = _install(provider, handler)

    with pytest.raises(ExecutionFailure):
        await _drain(provider.stream_response(_request()))

    assert len(sent) == 1
    await provider.cleanup()


# --------------------------------------------------------------------------
# The sibling detectors stopped reading the echo
# --------------------------------------------------------------------------


def test_mistral_does_not_fire_on_a_body_echoed_back_to_it() -> None:
    error = _OpenAIStyleError(
        "422 Unprocessable Entity",
        body={
            "detail": [
                {
                    "type": "value_error",
                    "loc": ["body", "messages", 0, "content"],
                    "msg": "Invalid message content",
                    "input": {"reasoning_effort": "high", "thinking": True},
                }
            ]
        },
        status_code=422,
    )

    assert is_mistral_reasoning_rejection(error) is False


def test_mistral_still_fires_on_a_real_reasoning_rejection() -> None:
    error = _OpenAIStyleError(
        "400",
        body={
            "detail": [
                {
                    "type": "extra_forbidden",
                    "loc": ["body", "reasoning_effort"],
                    "msg": "Extra inputs are not permitted",
                }
            ]
        },
    )

    assert is_mistral_reasoning_rejection(error) is True


def test_deepseek_does_not_fire_on_a_body_echoed_back_to_it() -> None:
    error = _OpenAIStyleError(
        "400",
        body={
            "error": {
                "message": "Invalid schema for function 'Read'",
                "input": {
                    "tool_choice": {"type": "function", "function": {"name": "Read"}}
                },
            }
        },
    )

    assert is_deepseek_tool_choice_rejection(error) is False


def test_deepseek_still_fires_on_a_real_tool_choice_rejection() -> None:
    error = _OpenAIStyleError(
        "400",
        body={
            "error": {"message": "deepseek-reasoner does not support this tool_choice"}
        },
    )

    assert is_deepseek_tool_choice_rejection(error) is True


def test_the_openai_sdk_error_shape_is_still_read_the_way_it_always_was() -> None:
    """The family that owned these nets sees no change in what a 400 means."""
    error = openai.BadRequestError(
        message="reasoning_effort is not supported",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://x.invalid/v1/chat/completions")
        ),
        body={"error": {"message": "reasoning_effort is not supported"}},
    )

    assert is_bad_request(error) is True
    assert (
        rejected_reasoning_field(error, {"model": "m", "reasoning_effort": "high"})
        == "reasoning_effort"
    )


# --------------------------------------------------------------------------
# End to end: the Responses dialect (ChatGPT OAuth)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chatgpt_oauth_strips_the_reasoning_block_its_host_named() -> None:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=CHATGPT_OAUTH_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    trace = install_wire_trace()

    async def _raw_stream():
        yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        yield b'data: {"type":"response.completed","response":{}}\n\n'

    success = MagicMock(status_code=200)
    success.aiter_raw = _raw_stream
    success.aclose = AsyncMock()
    provider._send_stream_request = AsyncMock(
        side_effect=[
            _httpx_bad_request("reasoning: Extra inputs are not permitted"),
            success,
        ]
    )

    chunks = [
        chunk
        async for chunk in provider.stream_response(
            MessagesRequest(
                model="gpt-5",
                max_tokens=64,
                messages=[Message(role="user", content="hi")],
            ),
            reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        )
    ]

    assert any("text_delta" in chunk and "ok" in chunk for chunk in chunks)
    calls = provider._send_stream_request.await_args_list
    assert "reasoning" in calls[0].kwargs["body"]
    assert "reasoning" not in calls[1].kwargs["body"]

    dialect = provider.reasoning_dialect("gpt-5")
    assert dialect.origin is ReasoningDialectOrigin.LEARNED
    assert [field for field, _ in dialect.learned_rejections] == ["reasoning"]
    assert [item.kind for item in trace.reasoning_adaptations] == [
        ReasoningAdaptationKind.SUPPRESSED
    ]
    await provider.cleanup()
