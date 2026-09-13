"""What each attempt's own clock records, and why it is not the request's.

``requests.ttft_ms`` starts when the *client's* request arrived, so a fallback
is charged with every predecessor's stall: across the live log the mean is
8.6 s at ``route_attempt = 0`` and 25.2 s above it, which is roughly 16.5 s of
somebody else's time filed under the model that rescued the request. These
tests pin the per-attempt clocks that make that separable, and the one place
they are measured -- the executor's chunk loop, which every provider family
passes through, as opposed to ``wire_capture.ResponseShape``, which is
installed in three provider modules and only on the success path.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from my_claude_code.application.execution import (
    ProviderExecutor,
    RouteAttemptRecord,
)
from my_claude_code.application.ports import ProviderPort
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import (
    REASONING_HEARTBEAT,
    sse_carries_content,
    sse_carries_reasoning,
)
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)

_MESSAGE_START = 'event: message_start\ndata: {"type": "message_start"}\n\n'
_TEXT = (
    "event: content_block_delta\n"
    'data: {"type": "content_block_delta", "delta":'
    ' {"type": "text_delta", "text": "hi"}}\n\n'
)
_THINKING = (
    "event: content_block_delta\n"
    'data: {"type": "content_block_delta", "delta":'
    ' {"type": "thinking_delta", "thinking": "hmm"}}\n\n'
)
_STOP = "event: message_stop\ndata: {}\n\n"


class _Scripted:
    """A provider that emits a scripted script of (delay, chunk) pairs."""

    def __init__(
        self,
        script: tuple[tuple[float, str], ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self._script = script
        self._error = error
        self.stream_calls = 0

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
    ) -> AsyncIterator[str]:
        self.stream_calls += 1
        for delay, chunk in self._script:
            if delay:
                await asyncio.sleep(delay)
            yield chunk
        if self._error is not None:
            raise self._error


def _routed(
    provider_id: str, model: str, *, stream: bool = True
) -> RoutedMessagesRequest:
    """One rung of a chain. ``stream=False`` is what turns on the buffering path.

    The executor derives ``buffer_until_complete`` from the primary request's
    own ``stream`` flag, so a non-streaming request is how a test reaches the
    branch that holds every chunk back until the message is complete.
    """
    return RoutedMessagesRequest(
        request=MessagesRequest(
            model=model, messages=[Message(role="user", content="hi")], stream=stream
        ),
        resolved=ResolvedModel(
            original_model="gateway",
            provider_id=provider_id,
            provider_model=model,
            provider_model_ref=f"{provider_id}/{model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


def _executor(providers: Mapping[str, ProviderPort]) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 7,
    )


async def _run(
    providers: Mapping[str, ProviderPort],
    *routed: RoutedMessagesRequest,
) -> list[RouteAttemptRecord]:
    records: list[RouteAttemptRecord] = []
    stream = _executor(providers).stream(
        RoutedMessagesPlan(routed),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_latency",
        on_attempt_result=records.append,
    )
    with contextlib_suppress():
        async for _chunk in stream:
            pass
    return records


class contextlib_suppress:
    """Swallow whatever the route raises: the ledger is what is under test."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return True


def _by_index(records: list[RouteAttemptRecord]) -> dict[int, RouteAttemptRecord]:
    return {record.attempt: record for record in records}


def test_a_thinking_delta_is_reasoning_and_not_answer_content() -> None:
    """The two scans must disagree about a thinking frame, or nothing else works.

    ``sse_carries_content`` is true for *every* ``content_block_delta``,
    reasoning included, which is why the loop asks the reasoning question first.
    """
    assert sse_carries_reasoning(_THINKING)
    assert sse_carries_content(_THINKING)
    assert not sse_carries_reasoning(_TEXT)
    assert sse_carries_content(_TEXT)
    assert not sse_carries_reasoning(_MESSAGE_START)


@pytest.mark.asyncio
async def test_attempt_ttft_recorded_on_first_content_chunk() -> None:
    provider = _Scripted(((0.05, _MESSAGE_START), (0.05, _TEXT), (0.0, _STOP)))
    records = _by_index(await _run({"p": provider}, _routed("p", "m")))
    assert records[0].outcome == "succeeded"
    ttft = records[0].ttft_ms
    assert ttft is not None
    # Two 50 ms sleeps before the text frame; the envelope frame does not count.
    assert ttft >= 90.0
    assert records[0].first_reasoning_ms is None


@pytest.mark.asyncio
async def test_attempt_ttft_ignores_scaffolding_frames() -> None:
    """A stream of pure envelope leaves it NULL: nobody could read anything."""
    provider = _Scripted(((0.0, _MESSAGE_START), (0.0, _STOP)))
    records = _by_index(await _run({"p": provider}, _routed("p", "m")))
    assert records[0].ttft_ms is None


@pytest.mark.asyncio
async def test_attempt_first_reasoning_ms_set_by_heartbeat_before_content() -> None:
    """The provider held a fragment back; that is the model working, not silence."""
    provider = _Scripted(
        ((0.05, REASONING_HEARTBEAT), (0.05, _TEXT), (0.0, _STOP)),
    )
    records = _by_index(await _run({"p": provider}, _routed("p", "m")))
    first_reasoning = records[0].first_reasoning_ms
    ttft = records[0].ttft_ms
    assert first_reasoning is not None
    assert ttft is not None
    assert first_reasoning < ttft


@pytest.mark.asyncio
async def test_a_thinking_delta_does_not_start_the_answer_clock() -> None:
    """Q2, in one test: 40 s of thinking is not a 40 s time to first token."""
    provider = _Scripted(
        ((0.0, _THINKING), (0.08, _TEXT), (0.0, _STOP)),
    )
    records = _by_index(await _run({"p": provider}, _routed("p", "m")))
    ttft = records[0].ttft_ms
    first_reasoning = records[0].first_reasoning_ms
    assert first_reasoning is not None
    assert ttft is not None
    assert ttft - first_reasoning >= 70.0


@pytest.mark.asyncio
async def test_attempt_ttft_null_when_attempt_failed_before_any_content() -> None:
    # Non-streaming, so the envelope frame the first model did emit is held
    # rather than committed and the chain is still allowed to fall back.
    primary = _Scripted(((0.0, _MESSAGE_START),), error=RuntimeError("upstream 503"))
    secondary = _Scripted(((0.0, _TEXT), (0.0, _STOP)))
    records = _by_index(
        await _run(
            {"a": primary, "b": secondary},
            _routed("a", "one", stream=False),
            _routed("b", "two", stream=False),
        )
    )
    assert records[0].outcome == "failed"
    assert records[0].ttft_ms is None
    assert records[1].ttft_ms is not None


@pytest.mark.asyncio
async def test_a_failed_attempt_keeps_the_first_token_it_did_stream() -> None:
    """The whole point of measuring in the loop rather than on the success path.

    ``params.response_shape.first_chunk_ms`` exists on exactly the succeeded
    attempts -- 16,362 rows, which is the succeeded count. The attempts that
    burn the time are the failures.
    """
    primary = _Scripted(
        ((0.05, _TEXT),), error=RuntimeError("upstream 500 after first token")
    )
    records = _by_index(await _run({"a": primary}, _routed("a", "one", stream=False)))
    assert records[0].outcome == "failed"
    assert records[0].ttft_ms is not None


@pytest.mark.asyncio
async def test_attempt_ttft_measured_before_buffering() -> None:
    """A held chunk arrived: timing the release would report our policy, not theirs."""
    provider = _Scripted(((0.05, _TEXT), (0.20, _STOP)))
    records = _by_index(await _run({"p": provider}, _routed("p", "m", stream=False)))
    ttft = records[0].ttft_ms
    duration = records[0].duration_ms
    assert ttft is not None
    assert duration is not None
    # Stamped at arrival, so it is far below the duration the hold produced.
    assert ttft < duration - 100.0


@pytest.mark.asyncio
async def test_a_skipped_attempt_has_no_latency_at_all() -> None:
    primary = _Scripted(((0.0, _TEXT), (0.0, _STOP)))
    records = _by_index(
        await _run(
            {"a": primary, "b": _Scripted(())},
            _routed("a", "one"),
            _routed("b", "two"),
        )
    )
    assert records[1].outcome == "skipped"
    assert records[1].ttft_ms is None
    assert records[1].first_reasoning_ms is None


@pytest.mark.asyncio
async def test_fallback_chain_winner_ttft_excludes_predecessor_time() -> None:
    """The regression this feature exists to prevent, measured end to end.

    Two models stall and fail, the third answers quickly. The third attempt's
    own clock must show its own 0.0x s -- not the 0.6 s the client waited.
    """
    stall_a = _Scripted((), error=RuntimeError("503 a"))
    stall_b = _Scripted((), error=RuntimeError("503 b"))

    async def _slow_fail(_seconds: float) -> None:
        await asyncio.sleep(_seconds)

    class _Stalling(_Scripted):
        def __init__(self, seconds: float, error: Exception) -> None:
            super().__init__((), error=error)
            self._seconds = seconds

        async def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy,
        ) -> AsyncIterator[str]:
            self.stream_calls += 1
            await _slow_fail(self._seconds)
            if False:  # pragma: no cover - makes this an async generator
                yield ""
            assert self._error is not None
            raise self._error

    slow_a = _Stalling(0.25, RuntimeError("503 a"))
    slow_b = _Stalling(0.25, RuntimeError("503 b"))
    winner = _Scripted(((0.03, _TEXT), (0.0, _STOP)))
    assert stall_a is not stall_b  # both constructed; the stalling pair is used
    records = _by_index(
        await _run(
            {"a": slow_a, "b": slow_b, "c": winner},
            _routed("a", "one"),
            _routed("b", "two"),
            _routed("c", "three"),
        )
    )
    assert [records[i].outcome for i in (0, 1, 2)] == ["failed", "failed", "succeeded"]
    assert records[0].ttft_ms is None
    assert records[1].ttft_ms is None
    winner_ttft = records[2].ttft_ms
    assert winner_ttft is not None
    # Its own clock: well under the ~530 ms the client waited for a first token.
    assert winner_ttft < 300.0
    # And each predecessor's duration is charged to the predecessor.
    for index in (0, 1):
        duration = records[index].duration_ms
        assert duration is not None
        assert duration >= 200.0
