"""The egress keepalive at ``_first_chunk_streaming_response``.

What is pinned here, each against the real seam and, where it matters, the
real ``RequestCapture`` -- no upstream is called:

* a stream silent for longer than ``idle_seconds`` gets keepalive frames, in
  the dialect of its surface, both before its first frame and after a real
  frame has gone out;
* none before ``idle_seconds``, none past ``max_seconds`` of one silence, and a
  real frame re-arms the clock;
* the content bytes are byte-identical with the keepalive on and off, and a
  keepalive is never counted as progress (``ttft_ms``, chunk count,
  ``output_chars``, the terminal-event gate);
* a failure after a keepalive committed the response is still reported, in the
  surface's own in-stream form;
* the pending ``__anext__`` is cancelled *and awaited* before the body is
  closed, exactly once, on every way out -- including a stop deadline that a
  body refusing to unwind must not be able to hold.
"""

import asyncio
import contextvars
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from my_claude_code.api import response_streams
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.api.response_streams import (
    ANTHROPIC_KEEPALIVE_FRAME,
    SSE_COMMENT_KEEPALIVE_FRAME,
    ManagedStreamingResponse,
    StreamKeepalive,
    anthropic_sse_streaming_response,
    openai_sse_streaming_response,
    terminal_execution_error_response,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import anthropic_failure_payload
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.request_log import RequestLogStore

FAST = StreamKeepalive(idle_seconds=0.05, interval_seconds=0.05, max_seconds=0)


def _events(*frames: tuple[str, dict[str, Any]]) -> list[str]:
    return [f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in frames]


ANSWER = _events(
    (
        "message_start",
        {"type": "message_start", "message": {"usage": {"input_tokens": 4}}},
    ),
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
            "delta": {"type": "text_delta", "text": "Hello "},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "world"},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
)


async def _paced(
    chunks: list[str], pauses: dict[int, float] | None = None
) -> AsyncGenerator[str]:
    """Yield ``chunks``, sleeping ``pauses[i]`` seconds before chunk ``i``."""
    for index, chunk in enumerate(chunks):
        pause = (pauses or {}).get(index, 0.0)
        if pause:
            await asyncio.sleep(pause)
        yield chunk


def _anthropic_error(exc: BaseException) -> JSONResponse:
    if isinstance(exc, ExecutionFailure):
        return terminal_execution_error_response(
            status_code=exc.status_code,
            content=anthropic_failure_payload(exc),
        )
    return JSONResponse(
        status_code=500,
        content={"type": "error", "error": {"type": "api_error", "message": "x"}},
    )


def _openai_error(exc: BaseException) -> JSONResponse:
    return terminal_execution_error_response(
        status_code=503,
        content={
            "error": {
                "message": f"no model answered: {type(exc).__name__}",
                "type": "api_error",
                "param": None,
                "code": None,
            }
        },
    )


async def _anthropic(
    body: AsyncIterator[str], keepalive: StreamKeepalive | None
) -> Any:
    return await anthropic_sse_streaming_response(
        body,
        pre_start_error_response=_anthropic_error,
        request_id="req_keepalive",
        keepalive=keepalive,
    )


async def _openai(body: AsyncIterator[str], keepalive: StreamKeepalive | None) -> Any:
    return await openai_sse_streaming_response(
        body,
        headers={"Cache-Control": "no-cache"},
        pre_start_error_response=_openai_error,
        keepalive=keepalive,
    )


async def _drain(response: StreamingResponse) -> list[str]:
    return [
        chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        async for chunk in response.body_iterator
    ]


def _without(frames: list[str], keepalive: str) -> str:
    return "".join(frame for frame in frames if frame != keepalive)


# --------------------------------------------------------------------------
# Settings -> policy
# --------------------------------------------------------------------------


def test_the_shipped_policy_is_idle_30_interval_20_cap_300() -> None:
    policy = StreamKeepalive.from_settings(Settings())
    assert policy == StreamKeepalive(
        idle_seconds=30.0, interval_seconds=20.0, max_seconds=300.0
    )


def test_idle_zero_turns_the_keepalive_off() -> None:
    settings = Settings().model_copy(update={"stream_keepalive_idle_seconds": 0.0})
    assert StreamKeepalive.from_settings(settings) is None


def test_the_frames_are_each_dialects_own_no_op() -> None:
    """Anthropic's own ping bytes; a comment every SSE parser must ignore."""
    assert ANTHROPIC_KEEPALIVE_FRAME == 'event: ping\ndata: {"type": "ping"}\n\n'
    assert SSE_COMMENT_KEEPALIVE_FRAME == ": keepalive\n\n"


# --------------------------------------------------------------------------
# When keepalives are written
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_silent_before_its_first_frame_gets_pings() -> None:
    """3x the interval of silence, then the answer: pings first, content intact."""
    response = await _anthropic(_paced(ANSWER, {0: 0.2}), FAST)
    frames = await _drain(response)

    pings = [frame for frame in frames if frame == ANTHROPIC_KEEPALIVE_FRAME]
    assert len(pings) >= 3
    assert frames[0] == ANTHROPIC_KEEPALIVE_FRAME
    assert _without(frames, ANTHROPIC_KEEPALIVE_FRAME) == "".join(ANSWER)


@pytest.mark.asyncio
async def test_a_stream_that_goes_quiet_mid_answer_gets_pings() -> None:
    """The 'committed, then silent' bucket: pings after a real frame too."""
    response = await _anthropic(_paced(ANSWER, {3: 0.25}), FAST)
    frames = await _drain(response)

    assert frames[:3] == ANSWER[:3]
    between = frames[3 : frames.index(ANSWER[3])]
    assert len(between) >= 3
    assert set(between) == {ANTHROPIC_KEEPALIVE_FRAME}
    assert _without(frames, ANTHROPIC_KEEPALIVE_FRAME) == "".join(ANSWER)


@pytest.mark.asyncio
async def test_no_keepalive_is_written_before_idle_seconds() -> None:
    """A stream never silent for idle_seconds keeps its exact byte sequence."""
    policy = StreamKeepalive(idle_seconds=0.5, interval_seconds=0.05, max_seconds=0)
    pauses = dict.fromkeys(range(len(ANSWER)), 0.02)
    response = await _anthropic(_paced(ANSWER, pauses), policy)
    assert await _drain(response) == ANSWER


@pytest.mark.asyncio
async def test_keepalives_stop_at_the_cap_and_a_real_frame_rearms_them() -> None:
    """Never worse than off: past max_seconds of one silence, nothing is written."""
    policy = StreamKeepalive(idle_seconds=0.05, interval_seconds=0.05, max_seconds=0.12)
    response = await _anthropic(_paced(ANSWER, {0: 0.6, 3: 0.6}), policy)
    frames = await _drain(response)

    before_first = frames[: frames.index(ANSWER[0])]
    mid = frames[frames.index(ANSWER[2]) + 1 : frames.index(ANSWER[3])]
    # Due at ~0.05 and ~0.10; the next (~0.15) is past the 0.12 cap. Timer
    # slop may drop one, never add one past the cap.
    assert 1 <= len(before_first) <= 2
    assert 1 <= len(mid) <= 2, "a real frame must re-arm the keepalive clock"
    assert _without(frames, ANTHROPIC_KEEPALIVE_FRAME) == "".join(ANSWER)


@pytest.mark.asyncio
async def test_openai_and_gemini_surfaces_get_an_sse_comment_not_a_ping() -> None:
    """The adapters drop an Anthropic ping, so their surface writes a comment."""
    chunks = ['data: {"id":"c1"}\n\n', "data: [DONE]\n\n"]
    response = await _openai(_paced(chunks, {0: 0.2, 1: 0.2}), FAST)
    frames = await _drain(response)

    assert ANTHROPIC_KEEPALIVE_FRAME not in frames
    assert frames.count(SSE_COMMENT_KEEPALIVE_FRAME) >= 4
    assert _without(frames, SSE_COMMENT_KEEPALIVE_FRAME) == "".join(chunks)


# --------------------------------------------------------------------------
# Byte identity and the measurements
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("pauses", [{}, {0: 0.2}, {3: 0.2}, {0: 0.15, 5: 0.15}])
async def test_content_bytes_are_identical_with_keepalive_on_and_off(
    pauses: dict[int, float],
) -> None:
    off = await _drain(await _anthropic(_paced(ANSWER, pauses), None))
    on = await _drain(await _anthropic(_paced(ANSWER, pauses), FAST))
    assert "".join(off) == "".join(ANSWER)
    assert _without(on, ANTHROPIC_KEEPALIVE_FRAME) == "".join(off)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _capture(store: RequestLogStore) -> RequestCapture:
    return RequestCapture(
        store,
        request_id="req_keepalive_capture",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="hello",
        params={"max_tokens": 100},
    )


def _only_row(store: RequestLogStore) -> dict[str, Any]:
    rows, total = store.list_requests()
    assert total == 1
    return rows[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("keepalive", [None, FAST], ids=["off", "on"])
async def test_ttft_and_content_measure_the_model_not_the_keepalive(
    tmp_path, keepalive: StreamKeepalive | None
) -> None:
    """``RequestCapture`` sets ``ttft_ms`` on the first chunk of *any* kind.

    The keepalive is injected outside the capture, so the capture never sees
    one: ``ttft_ms`` is still the model's first frame (~250 ms here, not the
    ~50 ms of the first ping), and the row is the same with keepalives on.
    """
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store)
    response = await _anthropic(capture.wrap(_paced(ANSWER, {0: 0.25})), keepalive)
    frames = await _drain(response)
    store.close()

    row = _only_row(store)
    assert row["status"] == "success"
    assert row["ttft_ms"] >= 240
    assert row["output_text"] == "Hello world"
    assert capture._saw_terminal_event is True
    if keepalive is not None:
        assert ANTHROPIC_KEEPALIVE_FRAME in frames


@pytest.mark.asyncio
async def test_a_keepalive_is_not_counted_as_a_chunk(monkeypatch) -> None:
    """The stuck-request watchdog must still see a silent stream as silent."""
    counted: list[int] = []
    adopted: list[int] = []
    monkeypatch.setattr(
        response_streams, "note_stream_chunk", lambda: counted.append(1)
    )
    monkeypatch.setattr(
        response_streams, "note_serving_task", lambda: adopted.append(1)
    )
    frames = await _drain(await _anthropic(_paced(ANSWER, {0: 0.2, 3: 0.2}), FAST))

    assert len(counted) == len(ANSWER)
    assert frames.count(ANTHROPIC_KEEPALIVE_FRAME) >= 4
    # The pump announces itself once; each keepalive adopts the writer task.
    assert len(adopted) == frames.count(ANTHROPIC_KEEPALIVE_FRAME) + 1


@pytest.mark.asyncio
async def test_the_body_runs_in_one_context_as_when_awaited_inline() -> None:
    """A context variable the body sets in one step is there in the next."""
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker")
    marker.set("caller")
    seen: list[str] = []

    async def body() -> AsyncGenerator[str]:
        seen.append(marker.get())
        marker.set("set-by-body")
        yield ANSWER[0]
        await asyncio.sleep(0.15)
        seen.append(marker.get())
        yield ANSWER[1]

    await _drain(await _anthropic(body(), FAST))
    assert seen == ["caller", "set-by-body"]


# --------------------------------------------------------------------------
# Failures after a keepalive committed the response
# --------------------------------------------------------------------------


def _failure() -> ExecutionFailure:
    return ExecutionFailure(
        FailureKind.UNAVAILABLE,
        status_code=503,
        message="every model on the route failed",
        retryable=False,
    )


async def _slow_then_raise(exc: BaseException) -> AsyncGenerator[str]:
    await asyncio.sleep(0.2)
    raise exc
    yield "unreachable"


async def _slow_then_end() -> AsyncGenerator[str]:
    await asyncio.sleep(0.2)
    return
    yield "unreachable"


@pytest.mark.asyncio
async def test_a_pre_start_failure_before_idle_is_still_an_http_error() -> None:
    """Inside idle_seconds nothing is committed: the 6.x error path is intact."""
    policy = StreamKeepalive(idle_seconds=1.0, interval_seconds=1.0, max_seconds=0)

    async def fails_fast() -> AsyncGenerator[str]:
        raise _failure()
        yield "unreachable"

    response = await _anthropic(fails_fast(), policy)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "false"


@pytest.mark.asyncio
async def test_a_late_failure_on_messages_is_anthropics_error_event() -> None:
    response = await _anthropic(_slow_then_raise(_failure()), FAST)
    assert isinstance(response, ManagedStreamingResponse)
    assert response.status_code == 200
    frames = await _drain(response)

    assert frames[0] == ANTHROPIC_KEEPALIVE_FRAME
    tail = _without(frames, ANTHROPIC_KEEPALIVE_FRAME)
    assert tail.startswith("event: error\n")
    assert "every model on the route failed" in tail


@pytest.mark.asyncio
async def test_a_late_failure_on_openai_surfaces_is_the_pre_start_error_body() -> None:
    """The same JSON the HTTP error would have carried, as one data frame."""
    response = await _openai(_slow_then_raise(_failure()), FAST)
    frames = await _drain(response)

    tail = _without(frames, SSE_COMMENT_KEEPALIVE_FRAME)
    assert tail.startswith("data: ") and tail.endswith("\n\n")
    payload = json.loads(tail[len("data: ") :])
    assert payload["error"]["message"] == "no model answered: ExecutionFailure"


@pytest.mark.asyncio
async def test_a_late_empty_stream_is_reported_not_silently_ended() -> None:
    anthropic = _without(
        await _drain(await _anthropic(_slow_then_end(), FAST)),
        ANTHROPIC_KEEPALIVE_FRAME,
    )
    assert anthropic.startswith("event: error\n")
    openai = _without(
        await _drain(await _openai(_slow_then_end(), FAST)),
        SSE_COMMENT_KEEPALIVE_FRAME,
    )
    assert "EmptyStreamError" in json.loads(openai[len("data: ") :])["error"]["message"]


@pytest.mark.asyncio
async def test_a_post_start_failure_keeps_its_existing_terminal_frame() -> None:
    """After a real frame the old path runs unchanged, keepalive or not."""

    async def body() -> AsyncGenerator[str]:
        yield ANSWER[0]
        await asyncio.sleep(0.15)
        raise _failure()

    off = "".join(
        await _drain(
            await _anthropic(_paced([ANSWER[0]]), None)  # shape reference only
        )
    )
    assert off == ANSWER[0]
    frames = await _drain(await _anthropic(body(), FAST))
    tail = _without(frames, ANTHROPIC_KEEPALIVE_FRAME)
    assert tail.startswith(ANSWER[0])
    assert "event: error\n" in tail[len(ANSWER[0]) :]


# --------------------------------------------------------------------------
# Every way out: cancelled AND awaited, body closed exactly once
# --------------------------------------------------------------------------


class _Probe:
    """A body that records how it ended and how often it was closed."""

    def __init__(self, *, first_delay: float, tail_delay: float = 10.0) -> None:
        self.first_delay = first_delay
        self.tail_delay = tail_delay
        self.endings: list[str] = []
        self.finally_runs = 0

    async def body(self) -> AsyncGenerator[str]:
        try:
            await asyncio.sleep(self.first_delay)
            yield ANSWER[0]
            await asyncio.sleep(self.tail_delay)
            yield ANSWER[1]
        except asyncio.CancelledError:
            self.endings.append("cancelled")
            raise
        except GeneratorExit:
            self.endings.append("closed")
            raise
        finally:
            self.finally_runs += 1


@pytest.mark.asyncio
async def test_a_client_gone_mid_silence_cancels_and_awaits_the_pending_read() -> None:
    probe = _Probe(first_delay=0.0)
    response = await _anthropic(probe.body(), FAST)
    iterator = response.body_iterator
    assert await anext(iterator) == ANSWER[0]
    assert await anext(iterator) == ANTHROPIC_KEEPALIVE_FRAME

    reader = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0.01)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    # The stream's own close, not the response's: the response only traces a
    # close failure, and "asynchronous generator is already running" is
    # exactly the failure an un-awaited cancel produces here.
    await iterator.aclose()
    assert probe.endings == ["cancelled"]
    assert probe.finally_runs == 1
    await response.aclose()
    assert probe.finally_runs == 1


@pytest.mark.asyncio
async def test_a_client_gone_between_frames_closes_the_body_once() -> None:
    """Body parked on its yield, not on a read: closed with GeneratorExit."""
    probe = _Probe(first_delay=0.0, tail_delay=0.0)
    response = await _anthropic(probe.body(), FAST)
    iterator = response.body_iterator
    assert await anext(iterator) == ANSWER[0]
    await response.aclose()
    await response.aclose()
    assert probe.endings == ["closed"]
    assert probe.finally_runs == 1


@pytest.mark.asyncio
async def test_a_handler_cancelled_before_the_first_frame_closes_the_body() -> None:
    probe = _Probe(first_delay=10.0)
    handler = asyncio.create_task(_anthropic(probe.body(), FAST))
    await asyncio.sleep(0.02)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert probe.endings == ["cancelled"]
    assert probe.finally_runs == 1


@pytest.mark.asyncio
async def test_a_client_gone_after_a_keepalive_commit_is_recorded_cancelled(
    tmp_path,
) -> None:
    """Nothing but pings reached the client: the row says cancelled, once."""
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store)
    probe = _Probe(first_delay=10.0)
    response = await _anthropic(capture.wrap(probe.body()), FAST)
    iterator = response.body_iterator
    assert await anext(iterator) == ANTHROPIC_KEEPALIVE_FRAME
    await iterator.aclose()
    await response.aclose()
    store.close()

    row = _only_row(store)
    assert row["status"] == "cancelled"
    assert row["ttft_ms"] is None
    assert probe.finally_runs == 1


@pytest.mark.asyncio
async def test_the_stop_deadline_bounds_a_body_that_will_not_unwind(
    monkeypatch,
) -> None:
    """The 6.41.0 drain: a read that ignores cancellation is abandoned in time."""
    release = asyncio.Event()
    unwound: list[str] = []

    async def stubborn() -> AsyncGenerator[str]:
        yield ANSWER[0]
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()
            unwound.append("late")
            raise
        yield ANSWER[1]

    monkeypatch.setattr(response_streams, "_cleanup_wait_budget", lambda: 0.1)
    response = await _anthropic(stubborn(), FAST)
    iterator = response.body_iterator
    assert await anext(iterator) == ANSWER[0]
    reader = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0.01)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    # The stream's own close is bounded, not only the response cleanup around
    # it (which applies the same budget one level up).
    started = time.monotonic()
    await asyncio.wait_for(iterator.aclose(), 2.0)
    assert time.monotonic() - started < 1.0
    await asyncio.wait_for(response.aclose(), 2.0)

    release.set()
    for _ in range(50):
        if unwound:
            break
        await asyncio.sleep(0.01)
    assert unwound == ["late"]


@pytest.mark.asyncio
async def test_keepalive_off_takes_the_original_inline_path(monkeypatch) -> None:
    """With the setting at 0 no pump task exists at all."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("keepalive machinery used while off")

    monkeypatch.setattr(response_streams, "_KeepaliveSource", refuse)
    frames = await _drain(await _anthropic(_paced(ANSWER, {0: 0.1}), None))
    assert frames == ANSWER
