"""Application-owned provider execution contracts."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from unittest.mock import MagicMock

import pytest
from loguru import logger

from my_claude_code.application.execution import (
    AttemptResultObserver,
    ProviderExecutor,
    RouteAttemptRecord,
    RouteExecutionPolicy,
    route_health_registry,
)
from my_claude_code.application.ports import ProviderPort
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.constants import (
    FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT,
    FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT,
    FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT,
    FALLBACK_STALL_TIMEOUT_DEFAULT,
    FALLBACK_TOTAL_TIMEOUT_DEFAULT,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import (
    REASONING_HEARTBEAT,
    assert_anthropic_stream_contract,
    parse_sse_text,
    sse_carries_content,
)
from my_claude_code.core.anthropic.streaming.ledger import AnthropicStreamLedger
from my_claude_code.core.async_iterators import AsyncCloseable
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    parse_failure_kinds,
)
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.providers.openai_chat.tool_calls import OpenAIToolCallAssembler


class FakeProvider:
    def __init__(self) -> None:
        self.preflight_calls: list[tuple[MessagesRequest, ReasoningPolicy]] = []
        self.stream_calls: list[dict[str, object]] = []
        self.stream_close_calls = 0
        self.cooldown_seconds = 0.0

    def throttle_remaining(self) -> float:
        return self.cooldown_seconds

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy,
    ) -> None:
        self.preflight_calls.append((request, reasoning))

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append(
            {
                "request": request,
                "input_tokens": input_tokens,
                "request_id": request_id,
                "reasoning": reasoning,
            }
        )
        try:
            yield "event: message_stop\ndata: {}\n\n"
        finally:
            self.stream_close_calls += 1


class FailingPreflightProvider(FakeProvider):
    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy,
    ) -> None:
        raise ValueError("invalid provider request")


class FailingStreamConstructionProvider(FakeProvider):
    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        raise RuntimeError("stream construction failed")


def _routed_request(
    provider_id: str = "provider",
    provider_model: str = "provider-model",
    *,
    stream: bool = True,
) -> RoutedMessagesRequest:
    request = MessagesRequest(
        model=provider_model,
        messages=[Message(role="user", content="hello")],
        stream=stream,
    )
    return RoutedMessagesRequest(
        request=request,
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id=provider_id,
            provider_model=provider_model,
            provider_model_ref=f"{provider_id}/{provider_model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


def _plan(*routed: RoutedMessagesRequest) -> RoutedMessagesPlan:
    return RoutedMessagesPlan(routed or (_routed_request(),))


@pytest.mark.asyncio
async def test_executor_uses_structural_provider_port_and_preflights_eagerly() -> None:
    provider = FakeProvider()
    routed = _routed_request()
    request = routed.request
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 17,
    )

    stream = executor.stream(
        _plan(routed),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload=request.model_dump(),
        request_id="req_application",
    )

    assert provider.preflight_calls == [(request, ReasoningPolicy.on())]
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert provider.stream_calls == [
        {
            "request": request,
            "input_tokens": 17,
            "request_id": "req_application",
            "reasoning": ReasoningPolicy.on(),
        }
    ]
    assert provider.stream_close_calls == 1


@pytest.mark.asyncio
async def test_closing_executor_stream_closes_provider_stream_once() -> None:
    provider = FakeProvider()
    routed = _routed_request()
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 17,
    )
    stream = executor.stream(
        _plan(routed),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_early_close",
    )

    assert await anext(stream) == "event: message_stop\ndata: {}\n\n"
    assert isinstance(stream, AsyncCloseable)
    await stream.aclose()

    assert provider.stream_close_calls == 1


@pytest.mark.asyncio
async def test_stream_construction_failure_remains_deferred_to_iteration() -> None:
    provider = FailingStreamConstructionProvider()
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _messages, _system, _tools: 17,
    )

    stream = executor.stream(
        _plan(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_deferred_construction",
    )

    with pytest.raises(RuntimeError, match="stream construction failed"):
        await anext(stream)


def test_executor_preflight_failure_stays_before_token_count_and_stream() -> None:
    provider = FailingPreflightProvider()
    token_counter = MagicMock(return_value=17)
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=token_counter,
    )

    with pytest.raises(ValueError, match="invalid provider request"):
        executor.stream(
            _plan(),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_application",
        )

    token_counter.assert_not_called()
    assert provider.stream_calls == []


class ScriptedProvider(FakeProvider):
    """Provider whose stream fails after a set number of emitted chunks."""

    def __init__(self, *, chunks: tuple[str, ...], error: Exception | None) -> None:
        super().__init__()
        self._chunks = chunks
        self._error = error

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request, "request_id": request_id})
        try:
            for chunk in self._chunks:
                yield chunk
            if self._error is not None:
                raise self._error
        finally:
            self.stream_close_calls += 1


def _executor(providers: Mapping[str, ProviderPort]) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
    )


@pytest.mark.asyncio
async def test_fallback_runs_when_primary_fails_before_the_first_chunk() -> None:
    primary = ScriptedProvider(chunks=(), error=RuntimeError("upstream 503"))
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})
    attempts: list[tuple[int, str]] = []

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_fallback",
        on_attempt=lambda routed, index: attempts.append(
            (index, routed.resolved.provider_model_ref)
        ),
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert attempts == [(0, "primary/big"), (1, "secondary/small")]
    assert primary.stream_close_calls == 1
    assert secondary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_failure_after_the_first_chunk_is_never_retried_on_a_fallback() -> None:
    """A streaming client has already seen the chunk; a second model would splice."""
    primary = ScriptedProvider(
        chunks=("event: a\n\n",), error=RuntimeError("mid-stream")
    )
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_committed",
    )

    assert await anext(stream) == "event: a\n\n"
    with pytest.raises(RuntimeError, match="mid-stream"):
        await anext(stream)
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_non_streaming_request_falls_back_after_the_first_chunk() -> None:
    """Nothing reached the client, so a mid-stream failure is still recoverable.

    A non-streaming client is served one aggregated message at the end. Treating
    the provider's first chunk as a commit made a fallback chain useless for
    every failure past time-to-first-token, which is where they mostly happen.
    """
    primary = ScriptedProvider(
        chunks=("event: partial\n\n",), error=RuntimeError("mid-stream")
    )
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})
    attempts: list[tuple[int, str]] = []

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big", stream=False),
            _routed_request("secondary", "small", stream=False),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_non_streaming_fallback",
        on_attempt=lambda routed, index: attempts.append(
            (index, routed.resolved.provider_model_ref)
        ),
    )

    # The failed attempt's partial output is dropped with it: the aggregator
    # must never see two openings spliced into one message.
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert attempts == [(0, "primary/big"), (1, "secondary/small")]
    assert primary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_every_attempt_is_announced_even_when_the_chain_is_exhausted() -> None:
    """The request log must name the last model tried, not the first."""
    providers = {
        "primary": FailingPreflightProvider(),
        "secondary": FailingPreflightProvider(),
    }
    executor = _executor(providers)
    attempts: list[tuple[int, str]] = []

    with pytest.raises(ValueError, match="invalid provider request"):
        executor.stream(
            _plan(
                _routed_request("primary", "big"),
                _routed_request("secondary", "small"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_exhausted",
            on_attempt=lambda routed, index: attempts.append(
                (index, routed.resolved.provider_model_ref)
            ),
        )

    assert attempts == [(0, "primary/big"), (1, "secondary/small")]


@pytest.mark.asyncio
async def test_preflight_failure_moves_to_the_next_attempt() -> None:
    primary = FailingPreflightProvider()
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_preflight_fallback",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_calls == []
    assert secondary.stream_calls != []


def test_every_attempt_failing_preflight_raises_the_last_error_synchronously() -> None:
    providers = {
        "primary": FailingPreflightProvider(),
        "secondary": FailingPreflightProvider(),
    }
    executor = _executor(providers)

    with pytest.raises(ValueError, match="invalid provider request"):
        executor.stream(
            _plan(
                _routed_request("primary", "big"),
                _routed_request("secondary", "small"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_all_preflight_fail",
        )


@pytest.mark.asyncio
async def test_last_attempt_failure_propagates_its_own_error() -> None:
    primary = ScriptedProvider(chunks=(), error=RuntimeError("first down"))
    secondary = ScriptedProvider(chunks=(), error=RuntimeError("second down"))
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_all_fail",
    )

    with pytest.raises(RuntimeError, match="second down"):
        await anext(stream)


def test_a_plan_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        RoutedMessagesPlan(())


class StallingProvider(FakeProvider):
    """Opens a stream, then produces nothing -- the shape a deadline exists for."""

    def __init__(self, *, stall_seconds: float = 3600.0, before: tuple[str, ...] = ()):
        super().__init__()
        self._stall_seconds = stall_seconds
        self._before = before

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request, "request_id": request_id})
        try:
            for chunk in self._before:
                yield chunk
            await asyncio.sleep(self._stall_seconds)
            yield "event: never\n\n"
        finally:
            self.stream_close_calls += 1


def _deadline_executor(
    providers: Mapping[str, ProviderPort],
    *,
    first_token_timeout: float = 0.05,
    total_timeout: float = 0.0,
    attempt_share_floor: float = 0.0,
    health: RouteHealthRegistry | None = None,
) -> ProviderExecutor:
    """An executor for the sub-second deadlines these tests run on.

    The share floor defaults to 0 here, not to the shipped 180s: every budget
    below is a fraction of a second, so the real default would swallow all of
    them and every one of these tests would be measuring the floor instead of
    the thing it names. The floor's own behaviour is asserted explicitly by the
    tests that pass it.
    """
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
        policy=RouteExecutionPolicy(
            first_token_timeout=first_token_timeout,
            total_timeout=total_timeout,
            attempt_share_floor=attempt_share_floor,
        ),
        health=health or RouteHealthRegistry(eject_after_failures=0),
    )


@pytest.mark.asyncio
async def test_a_model_that_sends_no_first_token_hands_over_to_the_fallback() -> None:
    """Nothing reached the client, so swapping models is invisible to it."""
    primary = StallingProvider()
    secondary = FakeProvider()
    executor = _deadline_executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_ttft",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_the_first_token_deadline_stops_applying_once_output_started() -> None:
    """A slow generation is not a stalled one; only the total budget bounds it."""
    primary = StallingProvider(stall_seconds=0.2, before=("event: a\n\n",))
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        first_token_timeout=0.05,
        total_timeout=0.0,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_slow",
    )

    chunks = [chunk async for chunk in stream]
    assert chunks[0] == "event: a\n\n"
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_a_committed_stall_ends_at_the_total_budget() -> None:
    """No chain can rescue a committed stream, but it must still stop."""
    primary = StallingProvider(before=("event: a\n\n",))
    executor = _deadline_executor(
        {"primary": primary},
        first_token_timeout=0.0,
        total_timeout=0.05,
    )

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_budget",
    )

    # Drained by hand: what reached the client *before* the failure is the
    # assertion, and a comprehension would discard it along with the exception.
    chunks = stream.__aiter__()
    received: list[str] = []
    with pytest.raises(ExecutionFailure) as failure:
        while True:
            received.append(await anext(chunks))

    assert received == ["event: a\n\n"]
    assert failure.value.kind is FailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_the_chain_is_not_extended_once_the_budget_is_spent() -> None:
    """Starting another model with no time left only delays the same error.

    The budget can only be spent this way once an attempt is past its share:
    a stream that has produced content owns the rest of the request, because
    truncating a working answer to preserve a fallback is the wrong trade. The
    non-streaming client is what keeps the fallback theoretically available
    here, so the reason the second model is never tried is the budget itself.
    """
    primary = StallingProvider(before=("event: content\n\n",))
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        first_token_timeout=0.05,
        total_timeout=0.05,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big", stream=False),
            _routed_request("secondary", "small", stream=False),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_spent",
    )

    with pytest.raises(ExecutionFailure):
        async for _chunk in stream:
            pass
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_a_silent_primary_cannot_spend_the_whole_budget() -> None:
    """The chain is guaranteed a turn, which is the entire point of the share.

    One shared pool meant the first model could drain it and every model behind
    it was skipped for lack of time. Measured on 21 days of real traffic, 393
    requests ran the full 600s budget having produced only scaffolding, with a
    configured chain sitting unused -- the primary was never abandoned early
    enough for the fallback to be reachable.
    """
    primary = StallingProvider()
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        # Disabled, so the share is provably the thing that ends the attempt.
        first_token_timeout=0.0,
        total_timeout=0.2,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_share",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_calls, "the primary must still be tried first"
    assert secondary.stream_calls, "the fallback must get its turn"


@pytest.mark.asyncio
async def test_a_producing_stream_is_not_cut_short_by_the_share() -> None:
    """A share bounds silence, never a working answer.

    The share applies only until the first content chunk. After that the
    attempt owns the remaining budget: cutting a stream that is producing, to
    hand over to a model that may not be, trades a real answer for nothing.
    """
    slow = StallingProvider(stall_seconds=0.08, before=("event: content\n\n",))
    executor = _deadline_executor(
        {"primary": slow, "secondary": FakeProvider()},
        first_token_timeout=0.0,
        total_timeout=0.4,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_producing",
    )

    # 0.08s of silence after content is longer than the 0.2s share would have
    # been generous about had it still applied, and well inside the total.
    chunks = [chunk async for chunk in stream]
    assert chunks[0] == "event: content\n\n"


@pytest.mark.asyncio
async def test_deadlines_disabled_never_abandon_an_attempt() -> None:
    primary = StallingProvider(stall_seconds=0.05, before=("event: a\n\n",))
    executor = _deadline_executor(
        {"primary": primary}, first_token_timeout=0.0, total_timeout=0.0
    )

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_off",
    )

    assert [chunk async for chunk in stream] == ["event: a\n\n", "event: never\n\n"]


@pytest.mark.asyncio
async def test_an_upstream_timeout_is_not_reported_as_a_routing_deadline() -> None:
    """The upstream giving up and us declining to wait are different facts."""
    primary = ScriptedProvider(chunks=(), error=TimeoutError("upstream read timeout"))
    secondary = FakeProvider()
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary},
        first_token_timeout=30.0,
        total_timeout=30.0,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_upstream_timeout",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]


@pytest.mark.asyncio
async def test_a_model_benched_by_earlier_failures_is_skipped_entirely() -> None:
    """The point of ejection: the fallback answers without re-paying the timeout."""
    primary = FakeProvider()
    secondary = FakeProvider()
    health = RouteHealthRegistry(
        bench_enabled=True,
        mode="consecutive",
        eject_after_failures=1,
        eject_seconds=300.0,
    )
    health.record_failure("primary/big", failure_kind="upstream")
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary}, health=health
    )
    attempts: list[tuple[int, str]] = []

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_ejected",
        on_attempt=lambda routed, index: attempts.append(
            (index, routed.resolved.provider_model_ref)
        ),
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.stream_calls == []
    assert attempts == [(1, "secondary/small")]


@pytest.mark.asyncio
async def test_a_served_request_clears_the_models_failure_streak() -> None:
    provider = FakeProvider()
    health = RouteHealthRegistry(eject_after_failures=2, eject_seconds=300.0)
    health.record_failure("primary/big", failure_kind="upstream")
    executor = _deadline_executor({"primary": provider}, health=health)

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_recovered",
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    health.record_failure("primary/big", failure_kind="upstream")
    assert not health.is_ejected("primary/big")


# ---------------------------------------------------------------- attempts --
#
# The chain's own account of itself. ``requests`` holds one row per request, so
# it can only ever name the model that answered: when a primary failed and a
# fallback succeeded the row said "success" and the reason the primary failed
# lived only in a log line. Measured over 21 days of real traffic, 1,144
# fallbacks succeeded and the largest cohort of 319 carried no recoverable
# reason at all.


def _attempt_log() -> tuple[list[RouteAttemptRecord], AttemptResultObserver]:
    seen: list[RouteAttemptRecord] = []
    return seen, seen.append


@pytest.mark.asyncio
async def test_a_rescued_request_records_why_the_primary_was_abandoned() -> None:
    """The fallback's success must not erase the primary's failure."""
    primary = FailingPreflightProvider()
    backup = FakeProvider()
    providers = {"broken": primary, "healthy": backup}
    first = _routed_request(provider_id="broken")
    second = _routed_request(provider_id="healthy")
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(first, second),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_rescued",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert [(a.attempt, a.model_ref, a.outcome) for a in attempts] == [
        (0, "broken/provider-model", "failed"),
        (1, "healthy/provider-model", "succeeded"),
    ]
    # The reason, which is the whole point: the request succeeded, and the log
    # still says what it had to survive to do so.
    assert attempts[0].error_kind == "ValueError"
    assert "invalid provider request" in (attempts[0].error_message or "")
    assert attempts[1].error_kind is None


@pytest.mark.asyncio
async def test_a_model_the_chain_never_reached_says_so() -> None:
    """ "Not tried" and "tried and failed" are different facts about a route."""
    provider = FakeProvider()
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="first"),
            _routed_request(provider_id="second"),
            _routed_request(provider_id="third"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_untouched",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert [(a.attempt, a.outcome, a.error_message) for a in attempts] == [
        (0, "succeeded", None),
        (1, "skipped", "never reached"),
        (2, "skipped", "never reached"),
    ]


@pytest.mark.asyncio
async def test_a_model_benched_by_recent_failures_is_recorded_as_benched() -> None:
    """A three-model chain that only ran one must not look like a one-model route.

    Health ejection removes a model from the route before the request starts,
    so nothing else in the log distinguishes it from a chain that was never
    configured -- which is exactly the confusion this row exists to prevent.
    """
    health = RouteHealthRegistry(
        bench_enabled=True,
        mode="consecutive",
        eject_after_failures=1,
        eject_seconds=300.0,
    )
    health.record_failure("sick/provider-model", failure_kind="upstream")
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda _provider_id: FakeProvider(),
        token_counter=lambda _m, _s, _t: 1,
        health=health,
    ).stream(
        _plan(
            _routed_request(provider_id="sick"),
            _routed_request(provider_id="healthy"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_benched",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    benched = attempts[0]
    assert benched.outcome == "skipped"
    assert benched.error_kind == "ejected"
    assert benched.error_message == (
        "benched: 1 consecutive failure (last: upstream), 300 s left"
    )
    assert benched.bench is not None
    assert benched.bench["mode"] == "consecutive"
    assert benched.bench["last_kind"] == "upstream"
    assert attempts[1].outcome == "succeeded"


@pytest.mark.asyncio
async def test_an_exhausted_chain_records_every_failure_not_just_the_last() -> None:
    """When everything fails, the log must name what each model did."""
    attempts, observer = _attempt_log()
    executor = ProviderExecutor(
        lambda _provider_id: FailingPreflightProvider(),
        token_counter=lambda _m, _s, _t: 1,
    )

    with pytest.raises(ValueError):
        executor.stream(
            _plan(
                _routed_request(provider_id="a"),
                _routed_request(provider_id="b"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_exhausted",
            on_attempt_result=observer,
        )

    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "ValueError"),
        (1, "failed", "ValueError"),
    ]


@pytest.mark.asyncio
async def test_an_execution_failure_is_named_by_its_kind_not_its_class() -> None:
    """One vocabulary for the attempt log.

    ``error_kind`` on the request row mixes ``FailureKind`` values with Python
    class names, which makes it awkward to group by. The attempt log prefers the
    kind wherever the failure carries one.
    """

    class RateLimited(FakeProvider):
        def preflight_stream(
            self, request: MessagesRequest, *, reasoning: ReasoningPolicy
        ) -> None:
            raise ExecutionFailure(
                kind=FailureKind.RATE_LIMIT,
                status_code=429,
                message="slow down",
                retryable=True,
            )

    attempts, observer = _attempt_log()
    providers: dict[str, ProviderPort] = {
        "limited": RateLimited(),
        "ok": FakeProvider(),
    }
    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="limited"), _routed_request(provider_id="ok")
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_kind",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert attempts[0].error_kind == "rate_limit"
    assert attempts[0].outcome == "failed"


@pytest.mark.asyncio
async def test_a_stream_that_dies_after_preflight_is_recorded_as_failed() -> None:
    """Preflight and streaming fail on different code paths.

    A preflight failure never opens a stream and is recorded where the chain
    picks the next candidate; a stream that dies afterwards is recorded in the
    execution loop. Covering only the first left the second free to report a
    failed attempt as a success -- verified by mutation, which the preflight
    test alone did not catch.
    """
    broken = FailingStreamConstructionProvider()
    healthy = FakeProvider()
    providers: dict[str, ProviderPort] = {"broken": broken, "healthy": healthy}
    attempts, observer = _attempt_log()

    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="broken"),
            _routed_request(provider_id="healthy"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_midstream",
        on_attempt_result=observer,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    # Preflight passed, so this attempt really did start; it failed opening the
    # stream, and that is what the log has to say.
    assert broken.preflight_calls
    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "RuntimeError"),
        (1, "succeeded", None),
    ]
    assert "stream construction failed" in (attempts[0].error_message or "")
    # And the attempt that ran was timed, which is what makes a slow failure
    # legible next to a fast one. Asserted as "present", not ">= 0": the latter
    # is true of None too, and passed against a mutation that dropped timing
    # altogether.
    assert attempts[0].duration_ms is not None
    assert attempts[1].duration_ms is not None
    # A model that never ran has nothing to time.
    assert all(a.duration_ms is None for a in attempts if a.outcome == "skipped")


def _ejection_settings(
    *,
    after_failures: int = 3,
    behavior: str = "consecutive",
    eject_window: int = 10,
    eject_failure_rate: float = 0.5,
    eject_min_samples: int = 8,
    bench_enabled: bool = True,
) -> Settings:
    """Real Settings, because that is what the factory keys itself on."""
    settings = Settings()
    settings.fallback_behavior = behavior
    settings.fallback_eject_after_failures = after_failures
    settings.fallback_eject_seconds = 300.0
    settings.fallback_eject_window = eject_window
    settings.fallback_eject_failure_rate = eject_failure_rate
    settings.fallback_eject_min_samples = eject_min_samples
    settings.fallback_bench_enabled = bench_enabled
    return settings


@pytest.mark.asyncio
async def test_a_model_is_benched_across_requests_not_within_one() -> None:
    """Three consecutive failures cannot be seen by three fresh registries.

    `MessagesHandler`, the executor and its registry are all constructed per
    request, so a registry owned by the executor reset its counter every time
    and the ejection threshold was never reached. Confirmed against a live
    server before the fix: four consecutive failures of the same model produced
    zero "MODEL CHAIN: skipping" lines.
    """
    settings = _ejection_settings(after_failures=2, bench_enabled=True)

    # Two separate "requests", each resolving its registry the way a handler
    # does. Sharing is the whole contract being tested.
    first = route_health_registry(settings)
    second = route_health_registry(settings)
    assert first is second

    for _ in range(2):
        first.record_failure("sick/model", failure_kind="upstream")
    assert second.usable_indexes(("sick/model", "healthy/model")) == (1,)


def test_changing_the_ejection_policy_starts_a_clean_registry() -> None:
    """A bench made under one policy must not be inherited by another.

    Two models on the route, because ejecting every candidate is deliberately
    bypassed -- skipping a bad model is an optimisation, refusing to try
    anything is an outage -- so a single-model route can never show a bench.
    """
    strict = _ejection_settings(after_failures=1, bench_enabled=True)
    lenient = _ejection_settings(after_failures=9, bench_enabled=True)
    route = ("sick/model", "healthy/model")

    route_health_registry(strict).record_failure("sick/model", failure_kind="upstream")

    assert route_health_registry(strict).usable_indexes(route) == (1,)
    assert route_health_registry(lenient).usable_indexes(route) == (0, 1)


# --------------------------------------------------------------- taxonomy --


class _MalformedRequestProvider(FakeProvider):
    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        raise ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=400,
            message="messages: field required",
            retryable=False,
        )


def _taxonomy_executor(providers, *, skip_kinds):
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
        policy=RouteExecutionPolicy(skip_kinds=skip_kinds),
    )


@pytest.mark.asyncio
async def test_a_malformed_request_does_not_walk_the_whole_chain() -> None:
    """The same body fails identically on every model.

    Retrying it buys three round trips to the same 400, and three entries in
    the request log that all say the caller sent something invalid.
    """
    broken = _MalformedRequestProvider()
    healthy = FakeProvider()
    attempts, observer = _attempt_log()

    with pytest.raises(ExecutionFailure):
        _taxonomy_executor(
            {"first": broken, "second": healthy},
            skip_kinds=frozenset({FailureKind.INVALID_REQUEST}),
        ).stream(
            _plan(
                _routed_request(provider_id="first"),
                _routed_request(provider_id="second"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_malformed",
            on_attempt_result=observer,
        )

    assert healthy.preflight_calls == [], "the second model must never be tried"
    # And the log says *why* it was not tried, which is the difference between
    # "your chain did not help" and "your chain was correctly not used".
    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "invalid_request"),
        (1, "skipped", "route_ended"),
    ]
    assert "invalid_request failure ends the route" in (attempts[1].error_message or "")


@pytest.mark.asyncio
async def test_every_other_failure_still_walks_the_chain() -> None:
    """Timeout, upstream, rate limit and the rest are what a chain is for."""
    for kind in (
        FailureKind.TIMEOUT,
        FailureKind.UPSTREAM,
        FailureKind.RATE_LIMIT,
        FailureKind.OVERLOADED,
        FailureKind.AUTHENTICATION,
        FailureKind.UNAVAILABLE,
    ):

        class _Failing(FakeProvider):
            def preflight_stream(self, request, *, reasoning, _kind=kind):
                raise ExecutionFailure(
                    kind=_kind, status_code=500, message="nope", retryable=True
                )

        healthy = FakeProvider()
        stream = _taxonomy_executor(
            {"first": _Failing(), "second": healthy},
            skip_kinds=frozenset({FailureKind.INVALID_REQUEST}),
        ).stream(
            _plan(
                _routed_request(provider_id="first"),
                _routed_request(provider_id="second"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id=f"req_{kind.value}",
        )
        assert [chunk async for chunk in stream] == [
            "event: message_stop\ndata: {}\n\n"
        ], kind
        assert healthy.stream_calls, kind


@pytest.mark.asyncio
async def test_an_empty_skip_list_falls_back_on_absolutely_everything() -> None:
    """The literal reading of "a chain is for every error", if you want it."""
    healthy = FakeProvider()
    stream = _taxonomy_executor(
        {"first": _MalformedRequestProvider(), "second": healthy},
        skip_kinds=frozenset(),
    ).stream(
        _plan(
            _routed_request(provider_id="first"),
            _routed_request(provider_id="second"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_everything",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert healthy.stream_calls


# ------------------------------------------------------------ stall guard --
#
# A stream that produced real output and then went quiet cannot fall back --
# the reader has already seen part of an answer, and switching models would
# splice two of them together. It can still stop. Measured on 21 days of real
# traffic, 106 requests held one open for the full 600s budget after producing
# output; nothing bounded them except that budget.

_TEXT = (
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hi"}}\n\n'
)
_PING = 'event: ping\ndata: {"type":"ping"}\n\n'


class _ThenSilentProvider(FakeProvider):
    """Produces some output, then stops -- the shape of the 106."""

    def __init__(self, *, before: tuple[str, ...]) -> None:
        super().__init__()
        self._before = before

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request, "request_id": request_id})
        try:
            for chunk in self._before:
                yield chunk
            await asyncio.sleep(3600)
            yield "event: never\n\n"
        finally:
            self.stream_close_calls += 1


def _stall_executor(providers, *, stall_timeout: float, total_timeout: float = 0.0):
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
        policy=RouteExecutionPolicy(
            first_token_timeout=0.0,
            total_timeout=total_timeout,
            stall_timeout=stall_timeout,
        ),
    )


@pytest.mark.asyncio
async def test_a_stream_that_goes_quiet_after_producing_is_stopped() -> None:
    """The whole point: output started, then silence, and it still ends.

    The total budget is disabled here so the stall limit is provably the only
    thing that can end this attempt -- without it the request would wait for
    the transport read timeout, minutes later, or forever.
    """
    provider = _ThenSilentProvider(before=(_TEXT,))
    stream = _stall_executor({"primary": provider}, stall_timeout=0.05).stream(
        _plan(_routed_request(provider_id="primary")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_stall",
    )

    seen: list[str] = []
    with pytest.raises(ExecutionFailure) as caught:
        async for chunk in stream:
            # Appended as it arrives, deliberately. A comprehension builds the
            # list only on success, so it would discard the partial output --
            # which is the exact thing this test asserts survives.
            seen.append(chunk)  # noqa: PERF401

    # What the reader already saw is still delivered; only the silence ends.
    assert seen == [_TEXT]
    assert caught.value.kind is FailureKind.TIMEOUT
    assert "stopped producing output" in caught.value.message


@pytest.mark.asyncio
async def test_a_keepalive_does_not_count_as_progress() -> None:
    """A ping resetting the clock is how a dead stream holds a request forever.

    The pings here keep coming, spaced well inside the stall limit, which is
    exactly the shape that defeats a guard measuring "time since any frame".
    If they counted as progress this stream would run to completion; the guard
    has to end it while they are still arriving.
    """

    class _PingsForever(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.pings_sent = 0

        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.stream_calls.append({"request": request})
            yield _TEXT
            for _ in range(200):
                await asyncio.sleep(0.005)
                self.pings_sent += 1
                yield _PING
            yield "event: message_stop\ndata: {}\n\n"

    provider = _PingsForever()
    stream = _stall_executor({"primary": provider}, stall_timeout=0.1).stream(
        _plan(_routed_request(provider_id="primary")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_keepalive",
    )

    with pytest.raises(ExecutionFailure) as caught:
        async for _chunk in stream:
            pass
    assert "stopped producing output" in caught.value.message
    # It gave up while the connection was still visibly alive, which is the
    # whole distinction: liveness is not progress.
    assert 0 < provider.pings_sent < 200


@pytest.mark.asyncio
async def test_a_stream_that_keeps_producing_is_never_cut() -> None:
    """The clock resets on every chunk that moves the answer forward.

    A guard that fired on total elapsed time rather than time-since-progress
    would truncate exactly the long answers it must not touch: on the measured
    traffic 104 successful requests ran more than 300 seconds.
    """

    class _SlowButSteady(FakeProvider):
        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.stream_calls.append({"request": request})
            # Ten gaps, each most of the stall limit: far longer in total than
            # the limit, never once silent for the length of it.
            for _ in range(10):
                await asyncio.sleep(0.03)
                yield _TEXT
            yield "event: message_stop\ndata: {}\n\n"

    stream = _stall_executor({"primary": _SlowButSteady()}, stall_timeout=0.1).stream(
        _plan(_routed_request(provider_id="primary")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_steady",
    )

    chunks = [chunk async for chunk in stream]
    assert chunks.count(_TEXT) == 10, "a producing stream must not be truncated"
    assert chunks[-1] == "event: message_stop\ndata: {}\n\n"


@pytest.mark.asyncio
async def test_a_buffered_request_that_stalls_falls_back_instead() -> None:
    """A non-streaming client has seen nothing, so a stall can still hand over.

    Nothing has been forwarded, so replacing the model is invisible -- the
    stall guard becomes a fallback trigger rather than a way to end a request.
    """
    stalled = _ThenSilentProvider(before=(_TEXT,))
    healthy = FakeProvider()
    stream = _stall_executor(
        {"primary": stalled, "secondary": healthy}, stall_timeout=0.05
    ).stream(
        _plan(
            _routed_request(provider_id="primary", stream=False),
            _routed_request(provider_id="secondary", stream=False),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_buffered_stall",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert healthy.stream_calls, "the fallback must answer"


@pytest.mark.asyncio
async def test_disabling_the_stall_guard_tolerates_a_long_pause() -> None:
    """0 means off, and off has to mean a long silence is allowed again.

    This is the escape hatch for a provider whose streams legitimately pause
    longer than the limit, so it is asserted as behaviour -- the pause is
    survived and the answer completes -- rather than as a message.
    """

    class _PausesThenFinishes(FakeProvider):
        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.stream_calls.append({"request": request})
            yield _TEXT
            await asyncio.sleep(0.15)
            yield _TEXT
            yield "event: message_stop\ndata: {}\n\n"

    stream = _stall_executor(
        {"primary": _PausesThenFinishes()}, stall_timeout=0.0, total_timeout=5.0
    ).stream(
        _plan(_routed_request(provider_id="primary")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_stall_off",
    )

    chunks = [chunk async for chunk in stream]
    assert chunks.count(_TEXT) == 2
    assert chunks[-1] == "event: message_stop\ndata: {}\n\n"


# The stall clock measures time since the last chunk that carried content, but
# the openai-chat translator holds tool arguments back until the JSON parses --
# for a ``Task`` call and for any tool whose argument names were aliased for the
# wire. While it buffers, an actively streaming model produces no
# ``content_block_delta`` at all, so the clock runs against a working stream.
# This is what killed req_e49840e7 after 120s with no tool call ever completed.

_TASK_ARGUMENTS = json.dumps({"description": "review", "prompt": "look at the file"})
# Grep's ``type`` parameter cannot travel under that name, so it is sent as
# ``_fcc_arg_type`` and renamed back on the way out -- the real alias on the
# incident's own tool list, not an invented one.
_ALIASED_ARGUMENTS = json.dumps({"pattern": "todo", "_fcc_arg_type": "py"})
_GREP_ALIASES = {"Grep": {"_fcc_arg_type": "type"}}


def _fragments(payload: str, count: int) -> tuple[str, ...]:
    """Split one arguments JSON into ``count`` wire fragments."""
    size = max(1, -(-len(payload) // count))
    return tuple(
        payload[start : start + size] for start in range(0, len(payload), size)
    )


class _BufferedToolArgsProvider(FakeProvider):
    """A model streaming tool arguments through the real translator.

    The buffering under test is the shipped one: this drives
    ``OpenAIToolCallAssembler`` rather than imitating it, so the test cannot
    pass by disagreeing with the code about when a fragment is held.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        arguments: str,
        aliases: dict[str, dict[str, str]] | None = None,
        pause: float = 0.1,
        count: int = 8,
    ) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._fragment_list = _fragments(arguments, count)
        self.fragment_count = len(self._fragment_list)
        self._aliases = aliases or {}
        self._pause = pause
        self.fragments_sent = 0

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request})
        ledger = AnthropicStreamLedger("msg_buffered", request.model)
        assembler = OpenAIToolCallAssembler()
        buffers: dict[int, str] = {}
        for event in assembler.process_tool_call(
            {
                "index": 0,
                "id": "toolu_buffered",
                "function": {"name": self._tool_name, "arguments": ""},
            },
            ledger,
            tool_argument_aliases=self._aliases,
            tool_argument_alias_buffers=buffers,
        ):
            yield event
        for fragment in self._fragment_list:
            await asyncio.sleep(self._pause)
            self.fragments_sent += 1
            for event in assembler.process_tool_call(
                {"index": 0, "function": {"arguments": fragment}},
                ledger,
                tool_argument_aliases=self._aliases,
                tool_argument_alias_buffers=buffers,
            ):
                yield event
        yield "event: message_stop\ndata: {}\n\n"


async def _run_buffered(provider: FakeProvider, request_id: str) -> list[str]:
    """Drive one attempt through the stall guard and return what reached the client."""
    stream = _stall_executor({"primary": provider}, stall_timeout=0.3).stream(
        _plan(_routed_request(provider_id="primary")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id=request_id,
    )
    seen: list[str] = []
    async for chunk in stream:
        seen.append(chunk)  # noqa: PERF401
    return seen


@pytest.mark.asyncio
async def test_buffered_task_arguments_do_not_starve_the_stall_clock() -> None:
    """A Task call whose arguments are still being buffered is not a stall.

    Upstream never pauses longer than one fragment interval -- a tenth of the
    limit -- but until the JSON parses the translator has nothing to forward.
    Counting only forwarded content made the clock measure the buffer instead
    of the model.
    """
    provider = _BufferedToolArgsProvider(
        tool_name="Task", arguments=_TASK_ARGUMENTS, count=8
    )

    seen = await _run_buffered(provider, "req_task_buffer")

    assert provider.fragments_sent == provider.fragment_count, (
        "upstream must have streamed throughout"
    )
    assert seen[-1] == "event: message_stop\ndata: {}\n\n"
    assert any(sse_carries_content(chunk) for chunk in seen), (
        "the completed arguments must still reach the client"
    )


@pytest.mark.asyncio
async def test_buffered_aliased_arguments_do_not_starve_the_stall_clock() -> None:
    """The same holds for a tool whose argument names were aliased for the wire.

    An aliased name can only be renamed back once the whole object parses, so
    the arguments accumulate until then. That buffer is silent for exactly as
    long as the model takes to finish the call.
    """
    provider = _BufferedToolArgsProvider(
        tool_name="Grep",
        arguments=_ALIASED_ARGUMENTS,
        aliases=_GREP_ALIASES,
        count=8,
    )

    seen = await _run_buffered(provider, "req_alias_buffer")

    assert provider.fragments_sent == provider.fragment_count
    assert seen[-1] == "event: message_stop\ndata: {}\n\n"
    restored = [
        chunk for chunk in seen if "_fcc_arg_type" not in chunk and "type" in chunk
    ]
    assert restored, "the aliased argument must be restored before the client sees it"


@pytest.mark.asyncio
async def test_a_buffering_stream_that_goes_silent_is_still_stopped() -> None:
    """The inverse guard: buffering earns time only while upstream delivers.

    The heartbeat says "a fragment just arrived", not "a call is open". Once
    upstream stops mid-arguments the heartbeats stop with it and the stall
    guard ends the attempt exactly as it did before -- the 5.41.0 behaviour
    this fix must not undo.
    """

    class _BuffersThenSilent(FakeProvider):
        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.stream_calls.append({"request": request})
            ledger = AnthropicStreamLedger("msg_silent", request.model)
            assembler = OpenAIToolCallAssembler()
            for event in assembler.process_tool_call(
                {
                    "index": 0,
                    "id": "toolu_silent",
                    "function": {"name": "Task", "arguments": '{"desc'},
                },
                ledger,
            ):
                yield event
            await asyncio.sleep(3600)
            yield "event: never\n\n"

    stream = _stall_executor(
        {"primary": _BuffersThenSilent()}, stall_timeout=0.3
    ).stream(
        _plan(_routed_request(provider_id="primary")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_buffer_then_silent",
    )

    with pytest.raises(ExecutionFailure) as caught:
        async for _chunk in stream:
            pass
    assert caught.value.kind is FailureKind.TIMEOUT
    assert "stopped producing output" in caught.value.message


@pytest.mark.asyncio
async def test_text_at_the_same_cadence_is_never_cut() -> None:
    """The control: identical timing, nothing buffered, and it survives today.

    It isolates the buffering as the cause -- the cadence alone is not what
    ends the attempt.
    """

    class _SteadyText(FakeProvider):
        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.stream_calls.append({"request": request})
            for _ in range(8):
                await asyncio.sleep(0.1)
                yield _TEXT
            yield "event: message_stop\ndata: {}\n\n"

    seen = await _run_buffered(_SteadyText(), "req_text_cadence")

    assert seen.count(_TEXT) == 8
    assert seen[-1] == "event: message_stop\ndata: {}\n\n"


class _ContextOverflowProvider(FakeProvider):
    """The user's bug: OpenRouter rejecting a 256487-token body at 256000."""

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        raise ExecutionFailure(
            kind=FailureKind.CONTEXT_LENGTH,
            status_code=400,
            message=(
                "Request exceeds this model's context window. Needed about "
                "256487 tokens; this model holds 256000."
            ),
            retryable=False,
        )


@pytest.mark.asyncio
async def test_a_context_overflow_moves_on_to_the_next_model() -> None:
    """A body too big for a 256k window is not too big for a 1M one.

    This is the reported bug: a seven-model chain went entirely unused because
    the overflow classified as `invalid_request` and hit the default skip list.
    """
    attempts, observer = _attempt_log()
    healthy = FakeProvider()

    stream = _taxonomy_executor(
        {"first": _ContextOverflowProvider(), "second": healthy},
        skip_kinds=frozenset({FailureKind.INVALID_REQUEST}),
    ).stream(
        _plan(
            _routed_request(provider_id="first"),
            _routed_request(provider_id="second"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_context_overflow",
        on_attempt_result=observer,
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert healthy.stream_calls, "the wider model must actually be tried"
    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "context_length"),
        (1, "succeeded", None),
    ]


@pytest.mark.asyncio
async def test_a_malformed_request_still_ends_the_route_beside_it() -> None:
    """The narrowing must not have widened the chain for real 400s."""
    healthy = FakeProvider()

    with pytest.raises(ExecutionFailure):
        _taxonomy_executor(
            {"first": _MalformedRequestProvider(), "second": healthy},
            skip_kinds=frozenset({FailureKind.INVALID_REQUEST}),
        ).stream(
            _plan(
                _routed_request(provider_id="first"),
                _routed_request(provider_id="second"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_still_malformed",
        )

    assert healthy.preflight_calls == []


@pytest.mark.asyncio
async def test_an_operator_can_opt_back_into_aborting_on_a_context_overflow() -> None:
    """FALLBACK_SKIP_KINDS=context_length restores the pre-fix behaviour."""
    settings = Settings()
    settings.fallback_skip_kinds = "context_length"
    assert settings.fallback_skip_kinds == "context_length"

    attempts, observer = _attempt_log()
    healthy = FakeProvider()

    with pytest.raises(ExecutionFailure):
        _taxonomy_executor(
            {"first": _ContextOverflowProvider(), "second": healthy},
            skip_kinds=parse_failure_kinds(settings.fallback_skip_kinds),
        ).stream(
            _plan(
                _routed_request(provider_id="first"),
                _routed_request(provider_id="second"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_opt_out",
            on_attempt_result=observer,
        )

    assert healthy.preflight_calls == []
    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "context_length"),
        (1, "skipped", "route_ended"),
    ]
    assert "context_length failure ends the route" in (attempts[1].error_message or "")


@pytest.mark.asyncio
async def test_the_default_policy_is_what_lets_a_context_overflow_fall_back() -> None:
    """The shipped default, not a hand-built one, is what the user's route used.

    Pinned separately because every other test here constructs its own
    `skip_kinds`: adding CONTEXT_LENGTH back to the default would reinstate the
    reported bug without reddening any of them.
    """
    assert RouteExecutionPolicy().skip_kinds == frozenset({FailureKind.INVALID_REQUEST})

    healthy = FakeProvider()
    providers = {"first": _ContextOverflowProvider(), "second": healthy}
    stream = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
    ).stream(
        _plan(
            _routed_request(provider_id="first"),
            _routed_request(provider_id="second"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_default_policy",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert healthy.stream_calls, "the default policy must let the chain continue"


@pytest.mark.asyncio
async def test_a_rate_limited_provider_is_stepped_over_rather_than_waited_on() -> None:
    """A cooldown is a sleep, and a chain exists so the sleep is not taken."""
    primary = FakeProvider()
    primary.cooldown_seconds = 42.0
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "limited"),
            _routed_request("secondary", "free"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cooldown_skip",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    # Not merely un-streamed: the limited provider was never even preflighted,
    # which is the round trip the old path spent inside its own limiter.
    assert primary.preflight_calls == []
    assert primary.stream_calls == []
    assert secondary.stream_calls != []


@pytest.mark.asyncio
async def test_the_last_model_is_tried_even_while_it_is_rate_limited() -> None:
    """Skipping a bad model is an optimisation; refusing to try one is an outage."""
    only = FakeProvider()
    only.cooldown_seconds = 900.0
    executor = _executor({"only": only})

    stream = executor.stream(
        _plan(_routed_request("only", "limited")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cooldown_last",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert only.stream_calls != []


@pytest.mark.asyncio
async def test_a_cooldown_skip_is_recorded_as_its_own_verdict() -> None:
    """The ledger must say "skipped, limited", not "failed" or "never reached"."""
    primary = FakeProvider()
    primary.cooldown_seconds = 42.0
    secondary = FakeProvider()
    records: list[RouteAttemptRecord] = []
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request("primary", "limited"),
            _routed_request("secondary", "free"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cooldown_ledger",
        on_attempt_result=records.append,
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert [(r.outcome, r.error_kind) for r in records] == [
        ("skipped", "cooldown"),
        ("succeeded", None),
    ]
    assert "42s" in (records[0].error_message or "")


@pytest.mark.asyncio
async def test_a_cooldown_skip_does_not_bench_the_model_it_skipped() -> None:
    """The model did not fail; the chain declined to wait for it.

    Counting it as a failure would eject a perfectly healthy model after a few
    busy minutes, which is the opposite of what a cooldown means.
    """
    primary = FakeProvider()
    primary.cooldown_seconds = 42.0
    secondary = FakeProvider()
    health = RouteHealthRegistry(eject_after_failures=1, eject_seconds=60.0)
    executor = _deadline_executor(
        {"primary": primary, "secondary": secondary}, health=health
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "limited"),
            _routed_request("secondary", "free"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cooldown_health",
    )
    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]

    assert not health.is_ejected("primary/limited")


class ThinkingProvider(FakeProvider):
    """Holds reasoning back: alive and working, with nothing to show for it.

    Emits the heartbeat the provider layer sends while its holdback buffer is
    withholding thought frames, then -- if it is allowed to get that far --
    the answer it was building up to.
    """

    def __init__(self, *, think_seconds: float, answers: bool = True):
        super().__init__()
        self._think_seconds = think_seconds
        self._answers = answers

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"request": request, "request_id": request_id})
        deadline = asyncio.get_running_loop().time() + self._think_seconds
        while asyncio.get_running_loop().time() < deadline:
            yield REASONING_HEARTBEAT
            await asyncio.sleep(0.01)
        if not self._answers:
            await asyncio.sleep(3600.0)
        yield "event: message_stop\ndata: {}\n\n"


def _thinking_executor(
    providers: Mapping[str, ProviderPort],
    *,
    reasoning_answer_timeout: float,
    total_timeout: float = 0.0,
) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
        policy=RouteExecutionPolicy(
            first_token_timeout=0.05,
            total_timeout=total_timeout,
            reasoning_answer_timeout=reasoning_answer_timeout,
        ),
        health=RouteHealthRegistry(eject_after_failures=0),
    )


@pytest.mark.asyncio
async def test_a_thinking_model_outlives_the_first_token_deadline() -> None:
    """The regression this deadline exists to prevent.

    Held reasoning makes an attempt look silent, so without a deadline of its
    own it inherits the first-token share -- 600s over an eleven-model chain
    is 54s. Measured against real traffic that would have diverted 1,387
    successful reasoning requests. A model that is visibly thinking must not
    be judged by the clock for a model that is doing nothing.
    """
    primary = ThinkingProvider(think_seconds=0.3)
    secondary = FakeProvider()
    executor = _thinking_executor(
        {"primary": primary, "secondary": secondary}, reasoning_answer_timeout=5.0
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "thinker"),
            _routed_request("secondary", "other"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_thinking_survives",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert secondary.stream_calls == []


@pytest.mark.asyncio
async def test_a_model_that_only_ever_thinks_hands_over_to_the_fallback() -> None:
    """The failure this whole boundary exists for, now bounded rather than endless."""
    primary = ThinkingProvider(think_seconds=3600.0, answers=False)
    secondary = FakeProvider()
    executor = _thinking_executor(
        {"primary": primary, "secondary": secondary}, reasoning_answer_timeout=0.2
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "loops"),
            _routed_request("secondary", "answers"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_thinking_forever",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert secondary.stream_calls != []


@pytest.mark.asyncio
async def test_the_heartbeat_never_reaches_the_client() -> None:
    """It is a routing signal, not output. An empty SSE frame is still a frame."""
    primary = ThinkingProvider(think_seconds=0.1)
    executor = _thinking_executor({"primary": primary}, reasoning_answer_timeout=5.0)

    stream = executor.stream(
        _plan(_routed_request("primary", "thinker")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_heartbeat_private",
    )

    chunks = [chunk async for chunk in stream]
    assert REASONING_HEARTBEAT not in chunks
    assert chunks == ["event: message_stop\ndata: {}\n\n"]


@pytest.mark.asyncio
async def test_a_silent_model_is_still_judged_by_the_first_token_deadline() -> None:
    """The 393-hang fix must survive this one.

    A stream that produces nothing at all sends no heartbeat, so it keeps the
    tight share. Widening that to the thinking allowance would quietly undo
    the fix that took hangs from 150 a day to roughly one.
    """
    primary = StallingProvider()
    secondary = FakeProvider()
    executor = _thinking_executor(
        {"primary": primary, "secondary": secondary}, reasoning_answer_timeout=3600.0
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "silent"),
            _routed_request("secondary", "answers"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_silent_still_bounded",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert secondary.stream_calls != []


def test_the_thinking_allowance_has_a_shipped_default() -> None:
    """A default is a separate contract from the parameter (§173)."""
    assert RouteExecutionPolicy().reasoning_answer_timeout == 0.0


def test_the_shipped_deadlines_are_the_numbers_operators_read() -> None:
    """The five deadline defaults, pinned as the literals they ship as.

    All zero since 6.16.0. They were 120/120/300 through 6.9.x and
    180/180/450 with a 600s budget through 6.15.0, chosen off measured
    traffic; the reason they are gone is not that the measurements were wrong
    but that the failure they produce is worse than the one they prevent -- a
    model legitimately thinking for half an hour was killed mid-work, and the
    error never said which knob did it. MCC is configured by the operator who
    runs it, so the shipped value decides nothing.

    The consequence is deliberate and is what the docs say: with these zeros
    MCC never ends a silent or stalled upstream on its own, and the chain
    moves only on an error the provider returns.
    """
    policy = RouteExecutionPolicy()

    assert policy.first_token_timeout == 0.0
    assert policy.attempt_share_floor == 0.0
    assert policy.stall_timeout == 0.0
    assert policy.reasoning_answer_timeout == 0.0
    assert policy.total_timeout == 0.0


def _share(*, total: float, attempts_remaining: int, floor: float) -> float:
    """Seconds ``_attempt_deadline`` grants one attempt, as a duration.

    The method answers in monotonic timestamps because that is what the read
    loop compares against; every assertion here is about the size of the slice,
    so convert once rather than in each test.
    """
    executor = _deadline_executor({}, attempt_share_floor=floor)
    deadline = time.monotonic() + total
    granted = executor._attempt_deadline(deadline, attempts_remaining)
    assert granted is not None
    return granted - time.monotonic()


def test_the_share_floor_raises_a_share_the_chain_had_cut_too_small() -> None:
    """The live defect: 600s over eight models is 75s, whatever the box said.

    ``MODEL DEADLINE: 'nvidia_nim/moonshotai/kimi-k3' produced no first token
    after 74.9494s`` was logged with FALLBACK_FIRST_TOKEN_TIMEOUT=120 and
    nothing anywhere set to 75. The floor is what stops the division from
    quietly replacing the operator's number.
    """
    assert _share(total=600.0, attempts_remaining=8, floor=0.0) == pytest.approx(
        75.0, abs=0.5
    )
    assert _share(total=600.0, attempts_remaining=8, floor=180.0) == pytest.approx(
        180.0, abs=0.5
    )


def test_the_share_floor_never_hands_out_budget_the_request_has_not_got() -> None:
    """A floor raises a share; it cannot extend the total.

    Without the clamp a 180s floor on a 60s budget would let one attempt run
    three times the whole request budget -- an invented limit in the other
    direction, and the exact shape Part IV rules out.
    """
    assert _share(total=60.0, attempts_remaining=8, floor=180.0) == pytest.approx(
        60.0, abs=0.5
    )


def test_a_zero_share_floor_reproduces_the_pure_equal_share() -> None:
    """The escape hatch has to be an exact restoration, not an approximation.

    An operator who preferred the old behaviour gets it back byte for byte:
    every chain length divides the remaining budget as it always did.
    """
    for models in (2, 3, 8, 11):
        assert _share(
            total=600.0, attempts_remaining=models, floor=0.0
        ) == pytest.approx(600.0 / models, abs=0.5)


def test_the_share_floor_does_not_shorten_a_share_that_was_already_generous() -> None:
    """A two-model chain already beats the floor, so the floor is inert."""
    assert _share(total=600.0, attempts_remaining=2, floor=180.0) == pytest.approx(
        300.0, abs=0.5
    )


def test_the_reported_deadline_names_the_floor_raised_value() -> None:
    """ "after Ns" has to be the limit that actually fired, floor included.

    The message exists to send the reader to the setting that ended their
    request. Reporting 75s when the attempt was allowed 180 and cut at the
    120s first-token deadline sends them to the wrong one -- which is precisely
    how the original defect stayed unexplained.
    """
    executor = _deadline_executor(
        {}, first_token_timeout=120.0, total_timeout=600.0, attempt_share_floor=180.0
    )
    deadline = time.monotonic() + 600.0
    attempt_deadline = executor._attempt_deadline(deadline, 8)
    assert attempt_deadline is not None
    attempt_budget = attempt_deadline - time.monotonic()

    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING")
    try:
        executor._deadline_reached(
            "nvidia_nim/moonshotai/kimi-k3",
            seen_chunk=False,
            request_id="req_floor",
            attempt_budget=attempt_budget,
        )
    finally:
        logger.remove(sink)

    reported = "".join(lines)
    assert "produced no first token after 120s" in reported
    assert "74.9" not in reported and "75s" not in reported


def test_the_heartbeat_is_empty_and_is_not_a_frame() -> None:
    """A protocol invariant, pinned rather than mutated.

    Mutating the sentinel into a real SSE frame cannot fail any behavioural
    test, because routing compares chunks against this same constant -- it is
    unkillable by construction (§175). What actually matters is the property
    the constant carries: any consumer that forwards it writes nothing, and it
    can never be mistaken for provider output. Assert that directly.
    """
    assert REASONING_HEARTBEAT == ""
    assert parse_sse_text(REASONING_HEARTBEAT) == []


def test_route_execution_policy_defaults_match_fallback_constants() -> None:
    """The dataclass defaults and the env-default constants move together."""
    policy = RouteExecutionPolicy()

    assert policy.first_token_timeout == FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT
    assert policy.total_timeout == FALLBACK_TOTAL_TIMEOUT_DEFAULT
    assert policy.stall_timeout == FALLBACK_STALL_TIMEOUT_DEFAULT
    assert policy.reasoning_answer_timeout == FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT
    assert policy.attempt_share_floor == FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT


@pytest.mark.asyncio
async def test_client_cancellation_publishes_an_interrupted_attempt_verdict() -> None:
    """A disconnect must not erase every verdict the chain had reached."""
    provider = FakeProvider()
    attempts, observer = _attempt_log()
    executor = ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _m, _s, _t: 1,
    )
    stream = executor.stream(
        _plan(_routed_request(provider_id="solo")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cancelled",
        on_attempt_result=observer,
    )

    assert await anext(stream) == "event: message_stop\ndata: {}\n\n"
    assert isinstance(stream, AsyncCloseable)
    await stream.aclose()

    assert len(attempts) == 1
    verdict = attempts[0]
    assert verdict.outcome == "failed"
    assert verdict.error_kind == "interrupted"
    assert "cancelled" in (verdict.error_message or "")


@pytest.mark.asyncio
async def test_a_post_commit_route_end_marks_downstream_models_unreachable() -> None:
    """Behind a committed failure nothing was skipped for time or health."""
    primary = ScriptedProvider(
        chunks=("event: a\n\n",),
        error=ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=400,
            message="bad body",
            retryable=False,
        ),
    )
    secondary = FakeProvider()
    attempts, observer = _attempt_log()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request(provider_id="primary"),
            _routed_request(provider_id="secondary"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_post_commit_end",
        on_attempt_result=observer,
    )
    chunks = stream.__aiter__()
    received: list[str] = []
    with pytest.raises(ExecutionFailure):
        while True:
            received.append(await anext(chunks))

    assert received == ["event: a\n\n"]
    assert attempts[0].outcome == "failed"
    assert attempts[1].outcome == "skipped"
    assert attempts[1].error_kind == "route_ended"


@pytest.mark.asyncio
async def test_sub_second_cooldowns_do_not_cost_the_chain_a_slot() -> None:
    """Below the step-over floor the wait is cheaper paid than routed around."""
    primary = FakeProvider()
    primary.cooldown_seconds = 4.9
    secondary = FakeProvider()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request(provider_id="primary"),
            _routed_request(provider_id="secondary"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_small_cooldown",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.preflight_calls, "a sub-floor cooldown must still be tried"


@pytest.mark.asyncio
async def test_full_cooldowns_still_step_over_to_the_next_model() -> None:
    primary = FakeProvider()
    primary.cooldown_seconds = 5.0
    secondary = FakeProvider()
    attempts, observer = _attempt_log()
    executor = _executor({"primary": primary, "secondary": secondary})

    stream = executor.stream(
        _plan(
            _routed_request(provider_id="primary"),
            _routed_request(provider_id="secondary"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cooldown_skip",
        on_attempt_result=observer,
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert primary.preflight_calls == []
    assert attempts[0].error_kind == "cooldown"
    assert attempts[1].outcome == "succeeded"


@pytest.mark.asyncio
async def test_the_last_candidate_is_never_stepped_over_for_cooldown() -> None:
    provider = FakeProvider()
    provider.cooldown_seconds = 30.0
    executor = _executor({"solo": provider})

    stream = executor.stream(
        _plan(_routed_request(provider_id="solo")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_last_candidate",
    )

    assert [chunk async for chunk in stream] == ["event: message_stop\ndata: {}\n\n"]
    assert provider.preflight_calls


# --------------------------------------------------------------- retry-once --


def _retry_settings() -> Settings:
    """Settings that enable retry_once on a synthetic 2-model chain."""
    settings = Settings()
    settings.fallback_retry_first = "retry_once"
    settings.fallback_behavior = "rate_based"
    return settings


# --------------------------------------------------------------- retry-once --


class _static_resolver:
    """Test resolver that returns a stand-in provider; enough to construct a
    ProviderExecutor without exercising the stream path.
    """

    def __call__(self, provider_id: str) -> ProviderPort:
        return FakeProvider()


def test_error_is_retryable_classifies_failures() -> None:
    """Transient failure kinds are retryable; auth and invalid-request are not.

    The chain retries a failed primary once only when the failure is one the
    same model could plausibly recover from on a second attempt. Timeouts,
    5xx, 429 and upstream errors qualify; auth and malformed-request do not,
    because a second try will produce the same answer.
    """
    from my_claude_code.core.failures import ExecutionFailure, FailureKind

    executor = ProviderExecutor(
        _static_resolver(),
        token_counter=lambda _m, _s, _t: 1,
        retry_first="retry_once",
    )

    for kind, retryable in [
        (FailureKind.TIMEOUT, True),
        (FailureKind.UPSTREAM, True),
        (FailureKind.RATE_LIMIT, True),
        (FailureKind.OVERLOADED, True),
        (FailureKind.UNAVAILABLE, True),
        (FailureKind.AUTHENTICATION, False),
        (FailureKind.INVALID_REQUEST, False),
        (FailureKind.PERMISSION, False),
        (FailureKind.CONTEXT_LENGTH, False),
    ]:
        failure = ExecutionFailure(
            kind=kind, status_code=500, message="x", retryable=False
        )
        assert executor._error_is_retryable(failure) is retryable, kind

    # An unclassified exception is treated as transient: it might be a raw
    # httpx.TimeoutError raised before the failure policy mapped it.
    assert executor._error_is_retryable(TimeoutError("slow")) is True


def test_benching_is_off_by_default() -> None:
    """Shipped off in 6.14.0: the guard removes capacity, so it is opt-in.

    It shipped off in 5.58.0-5.60.0 by accident and on from 5.61.0 on purpose.
    What changed the answer is what fed it: every failure counted, so a prompt
    larger than any model's context ejected the whole chain and the request
    was answered by whichever model was left holding the 400.
    """
    settings = Settings()

    assert settings.fallback_bench_enabled is False

    registry = route_health_registry(settings)
    assert registry.bench_enabled is False
    assert registry.enabled is False


def test_a_failing_model_is_retried_every_request_under_the_default_settings() -> None:
    """The consequence of the default, stated as behaviour rather than a flag."""
    registry = route_health_registry(Settings())

    for _ in range(20):
        registry.record_failure("sick/model", failure_kind="upstream")

    assert registry.usable_indexes(("sick/model", "healthy/model")) == (0, 1)


def test_the_eject_knobs_are_live_once_benching_is_turned_on() -> None:
    """Turning the switch on must actually bench, not just be configured to."""
    settings = _ejection_settings(after_failures=2)
    registry = route_health_registry(settings)

    for _ in range(2):
        registry.record_failure("sick/model", failure_kind="upstream")

    assert registry.usable_indexes(("sick/model", "healthy/model")) == (1,)


class _RecordingRegistry(RouteHealthRegistry):
    """Registry that captures what the executor tells it about each failure."""

    def __init__(self) -> None:
        super().__init__(bench_enabled=False)
        self.calls: list[tuple[str, str | None, int | None]] = []

    def record_failure(
        self,
        model_ref: str,
        *,
        failure_kind: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.calls.append((model_ref, failure_kind, status_code))
        super().record_failure(
            model_ref,
            failure_kind=failure_kind,
            status_code=status_code,
        )


@pytest.mark.asyncio
async def test_the_failure_kind_reaches_the_health_registry() -> None:
    """Without the kind, every ejection used the full eject_seconds.

    The kind is what decides whether the failure counts against the model at
    all, and the status is what the skipped row quotes back. The executor
    passed neither, so the registry could not tell a 502 from a 400.
    """
    failure = ExecutionFailure(
        kind=FailureKind.UPSTREAM, status_code=502, message="boom", retryable=True
    )
    primary = ScriptedProvider(chunks=(), error=failure)
    registry = _RecordingRegistry()
    executor = ProviderExecutor(
        lambda provider_id: {"primary": primary, "secondary": FakeProvider()}[
            provider_id
        ],
        token_counter=lambda _m, _s, _t: 17,
        health=registry,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_kind",
    )
    [chunk async for chunk in stream]

    assert registry.calls == [("primary/big", "upstream", 502)]


@pytest.mark.asyncio
async def test_an_unclassified_failure_reports_no_kind_rather_than_raising() -> None:
    """A raw exception never reached provider classification and has no .kind."""
    primary = ScriptedProvider(chunks=(), error=RuntimeError("upstream 503"))
    registry = _RecordingRegistry()
    executor = ProviderExecutor(
        lambda provider_id: {"primary": primary, "secondary": FakeProvider()}[
            provider_id
        ],
        token_counter=lambda _m, _s, _t: 17,
        health=registry,
    )

    stream = executor.stream(
        _plan(
            _routed_request("primary", "big"),
            _routed_request("secondary", "small"),
        ),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_unclassified",
    )
    [chunk async for chunk in stream]

    assert registry.calls == [("primary/big", None, None)]


@pytest.mark.asyncio
async def test_retry_once_applies_to_every_request_not_once_per_process() -> None:
    """``_retried_positions`` used to live on the executor.

    ProviderExecutor is built once per handler and shared by every request in
    the process, so the first request to retry position 0 consumed the budget
    for the whole process lifetime and no later request retried its primary.
    """
    executor = ProviderExecutor(
        lambda provider_id: {
            "primary": ScriptedProvider(chunks=(), error=RuntimeError("upstream 503")),
            "secondary": FakeProvider(),
        }[provider_id],
        token_counter=lambda _m, _s, _t: 17,
        retry_first="retry_once",
        health=RouteHealthRegistry(bench_enabled=False),
    )

    async def run(request_id: str) -> list[tuple[int, str]]:
        seen: list[tuple[int, str]] = []
        stream = executor.stream(
            _plan(
                _routed_request("primary", "big"),
                _routed_request("secondary", "small"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id=request_id,
            on_attempt=lambda routed, index: seen.append(
                (index, routed.resolved.provider_model_ref)
            ),
        )
        [chunk async for chunk in stream]
        return seen

    first = await run("req_retry_1")
    second = await run("req_retry_2")

    # The primary is attempted twice (original + one retry) in both requests.
    assert [ref for _, ref in first].count("primary/big") == 2
    assert [ref for _, ref in second].count("primary/big") == 2, (
        "the second request did not retry its primary: retry state leaked "
        "across requests"
    )


def test_the_executor_holds_no_cross_request_retry_state() -> None:
    executor = ProviderExecutor(
        _static_resolver(),
        token_counter=lambda _m, _s, _t: 1,
        retry_first="retry_once",
    )

    assert not hasattr(executor, "_retried_positions")


def test_the_cooldown_step_over_floor_comes_from_settings() -> None:
    """The floor was a module constant, so the knob beside it was unreachable."""
    from my_claude_code.application.execution import route_execution_policy

    def _settings(**overrides) -> Settings:
        return Settings(**overrides)

    policy = route_execution_policy(_settings(FALLBACK_COOLDOWN_STEP_OVER_FLOOR=12.0))
    assert policy.cooldown_step_over_floor == 12.0
    assert route_execution_policy(_settings()).cooldown_step_over_floor == 5.0


@pytest.mark.asyncio
async def test_a_configured_step_over_floor_decides_which_cooldowns_are_routed() -> (
    None
):
    """A 10s cooldown is waited out under a 12s floor and stepped over under 5s."""

    async def _run(floor: float, cooldown: float) -> bool:
        primary = FakeProvider()
        primary.cooldown_seconds = cooldown
        secondary = FakeProvider()
        executor = ProviderExecutor(
            lambda provider_id: {"primary": primary, "secondary": secondary}[
                provider_id
            ],
            token_counter=lambda _messages, _system, _tools: 17,
            policy=RouteExecutionPolicy(cooldown_step_over_floor=floor),
        )
        stream = executor.stream(
            _plan(
                _routed_request(provider_id="primary"),
                _routed_request(provider_id="secondary"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_floor",
        )
        [chunk async for chunk in stream]
        return bool(primary.preflight_calls)

    assert await _run(12.0, 10.0) is True, "under the floor: wait rather than route"
    assert await _run(5.0, 10.0) is False, "over the floor: step the model over"


# --- A committed stream that dies ends as a message, not an error -----------
#
# Once real text has reached the client no other model can take over, so these
# do not test fallback. They test what the client is handed instead of the API
# error it used to get printed underneath a half-written answer.

_COMMITTED_START = (
    'event: message_start\ndata: {"type": "message_start", "message": '
    '{"id": "msg_1", "type": "message", "role": "assistant", "content": [], '
    '"model": "big", "stop_reason": null, "stop_sequence": null, '
    '"usage": {"input_tokens": 41, "output_tokens": 1}}}\n\n'
)
_COMMITTED_TEXT_BLOCK = (
    'event: content_block_start\ndata: {"type": "content_block_start", '
    '"index": 0, "content_block": {"type": "text", "text": ""}}\n\n'
)
_COMMITTED_TEXT = (
    'event: content_block_delta\ndata: {"type": "content_block_delta", '
    '"index": 0, "delta": {"type": "text_delta", "text": "half an answer"}}\n\n'
)
_COMMITTED_TEXT_STOP = (
    'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n'
)
_COMMITTED_TOOL_BLOCK = (
    'event: content_block_start\ndata: {"type": "content_block_start", '
    '"index": 1, "content_block": {"type": "tool_use", "id": "t1", '
    '"name": "Bash", "input": {}}}\n\n'
)
_COMMITTED_TOOL_JSON = (
    'event: content_block_delta\ndata: {"type": "content_block_delta", '
    '"index": 1, "delta": {"type": "input_json_delta", '
    '"partial_json": "{\\"command\\": \\"ls -"}}\n\n'
)
_COMMITTED_TEXT_FRAMES = (
    _COMMITTED_START,
    _COMMITTED_TEXT_BLOCK,
    _COMMITTED_TEXT,
)


def _committed_executor(
    providers: Mapping[str, ProviderPort],
    *,
    end_cleanly_after_commit: bool = True,
    health: RouteHealthRegistry | None = None,
) -> ProviderExecutor:
    """An executor whose stall deadline fires in a fraction of a second."""
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
        policy=RouteExecutionPolicy(
            first_token_timeout=0.05,
            total_timeout=0.0,
            stall_timeout=0.05,
            attempt_share_floor=0.0,
            end_cleanly_after_commit=end_cleanly_after_commit,
        ),
        health=health or RouteHealthRegistry(eject_after_failures=0),
    )


async def _drain(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_a_committed_text_stall_ends_the_message_instead_of_erroring() -> None:
    """The incident: 1,333 characters, then silence, then an API error."""
    primary = StallingProvider(before=_COMMITTED_TEXT_FRAMES)
    secondary = FakeProvider()
    executor = _committed_executor({"primary": primary, "secondary": secondary})

    received = await _drain(
        executor.stream(
            _plan(
                _routed_request("primary", "big"),
                _routed_request("secondary", "small"),
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_truncated",
        )
    )

    events = parse_sse_text("".join(received))
    assert_anthropic_stream_contract(events)
    assert [event.event for event in events][-3:] == [
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    delta = next(event for event in events if event.event == "message_delta")
    assert delta.data["delta"]["stop_reason"] == "max_tokens"
    assert delta.data["usage"]["input_tokens"] == 41
    # The chain is not continued: the client has already seen this model's
    # words, so a second model would be splicing into someone else's answer.
    assert secondary.stream_calls == []
    assert primary.stream_close_calls == 1


@pytest.mark.asyncio
async def test_the_ended_attempt_still_records_the_failure_that_ended_it() -> None:
    """A valid message must not make the stall disappear from the log."""
    primary = StallingProvider(before=_COMMITTED_TEXT_FRAMES)
    attempts, observer = _attempt_log()
    executor = _committed_executor({"primary": primary})

    await _drain(
        executor.stream(
            _plan(_routed_request("primary", "big")),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_truncated_row",
            on_attempt_result=observer,
        )
    )

    assert [(a.attempt, a.outcome, a.error_kind) for a in attempts] == [
        (0, "failed", "timeout")
    ]
    assert attempts[0].truncated == {
        "chars": len("half an answer"),
        "blocks": 1,
        "reason": "timeout",
        "stop_reason_sent": "max_tokens",
        "ended_cleanly": True,
    }
    assert "stopped producing output" in (attempts[0].error_message or "")


@pytest.mark.asyncio
async def test_the_stalled_model_is_charged_for_the_failure_exactly_once() -> None:
    """The committed branch used to return without ever telling the registry."""
    primary = StallingProvider(before=_COMMITTED_TEXT_FRAMES)
    registry = _RecordingRegistry()
    executor = _committed_executor({"primary": primary}, health=registry)

    await _drain(
        executor.stream(
            _plan(_routed_request("primary", "big")),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_truncated_health",
        )
    )

    assert registry.calls == [("primary/big", "timeout", 504)]


@pytest.mark.asyncio
async def test_disabling_the_clean_ending_restores_the_committed_error() -> None:
    """The operator's escape hatch is byte-for-byte the old behaviour."""
    primary = StallingProvider(before=_COMMITTED_TEXT_FRAMES)
    attempts, observer = _attempt_log()
    executor = _committed_executor({"primary": primary}, end_cleanly_after_commit=False)

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_truncated_off",
        on_attempt_result=observer,
    )
    chunks = stream.__aiter__()
    received: list[str] = []
    with pytest.raises(ExecutionFailure) as failure:
        while True:
            received.append(await anext(chunks))

    assert received == list(_COMMITTED_TEXT_FRAMES)
    assert failure.value.kind is FailureKind.TIMEOUT
    assert attempts[0].truncated is None


@pytest.mark.asyncio
async def test_a_stall_inside_a_tool_call_still_errors_and_says_so() -> None:
    """A tool call with silently empty arguments would be *run* by the client."""
    primary = StallingProvider(
        before=(
            _COMMITTED_START,
            _COMMITTED_TEXT_BLOCK,
            _COMMITTED_TEXT,
            _COMMITTED_TEXT_STOP,
            _COMMITTED_TOOL_BLOCK,
            _COMMITTED_TOOL_JSON,
        )
    )
    attempts, observer = _attempt_log()
    executor = _committed_executor({"primary": primary})

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_truncated_tool",
        on_attempt_result=observer,
    )
    chunks = stream.__aiter__()
    with pytest.raises(ExecutionFailure):
        while True:
            await anext(chunks)

    assert attempts[0].outcome == "failed"
    assert attempts[0].truncated == {
        "chars": len("half an answer"),
        "blocks": 2,
        "reason": "incomplete_tool_use",
        "stop_reason_sent": None,
        "ended_cleanly": False,
    }


@pytest.mark.asyncio
async def test_a_committed_stall_between_blocks_ends_the_message() -> None:
    """State (d): nothing open, so only the message itself has to be ended."""
    primary = StallingProvider(
        before=(
            _COMMITTED_START,
            _COMMITTED_TEXT_BLOCK,
            _COMMITTED_TEXT,
            _COMMITTED_TEXT_STOP,
        )
    )
    executor = _committed_executor({"primary": primary})

    received = await _drain(
        executor.stream(
            _plan(_routed_request("primary", "big")),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_truncated_between",
        )
    )

    events = parse_sse_text("".join(received))
    assert_anthropic_stream_contract(events)
    assert [event.event for event in events].count("content_block_stop") == 1


@pytest.mark.asyncio
async def test_a_mid_stream_upstream_failure_also_ends_the_message() -> None:
    """Not only stalls: a 502 after the first paragraph is the same problem."""
    failure = ExecutionFailure(
        kind=FailureKind.UPSTREAM, status_code=502, message="boom", retryable=True
    )
    primary = ScriptedProvider(chunks=_COMMITTED_TEXT_FRAMES, error=failure)
    attempts, observer = _attempt_log()
    executor = _committed_executor({"primary": primary})

    received = await _drain(
        executor.stream(
            _plan(_routed_request("primary", "big")),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id="req_truncated_502",
            on_attempt_result=observer,
        )
    )

    assert_anthropic_stream_contract(parse_sse_text("".join(received)))
    assert attempts[0].error_kind == "upstream"
    assert attempts[0].truncated is not None
    assert attempts[0].truncated["reason"] == "upstream"


@pytest.mark.asyncio
async def test_a_scaffolding_only_stall_keeps_the_old_error() -> None:
    """No content block means no message to build: the 5.41.0 shape is intact."""
    primary = StallingProvider(before=("event: a\n\n",))
    executor = _committed_executor({"primary": primary})

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_truncated_scaffold",
    )
    chunks = stream.__aiter__()
    with pytest.raises(ExecutionFailure):
        while True:
            await anext(chunks)


@pytest.mark.asyncio
async def test_a_responses_wire_request_keeps_the_old_error() -> None:
    """That assembler can only say completed or failed, never "incomplete"."""
    primary = StallingProvider(before=_COMMITTED_TEXT_FRAMES)
    executor = _committed_executor({"primary": primary})

    stream = executor.stream(
        _plan(_routed_request("primary", "big")),
        wire_api="responses",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_truncated_responses",
    )
    chunks = stream.__aiter__()
    with pytest.raises(ExecutionFailure):
        while True:
            await anext(chunks)
