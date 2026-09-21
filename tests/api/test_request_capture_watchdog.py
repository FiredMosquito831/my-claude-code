"""The seam between a request and its task, asserted on every exit path.

``_begin_finalize`` is the single choke point the in-flight spec names, and
this file is the proof that it really is single: success, error, a consumer
that walked away (``GeneratorExit``), cancellation, a non-streamed answer, an
optimizer answering locally, and a request whose log is switched off entirely
all have to leave the registry empty.
"""

import asyncio
import json
from typing import Any

import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.core import request_tasks
from my_claude_code.core.async_iterators import try_close_async_iterator
from my_claude_code.core.credential_attribution import record_credential
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import record_proxy
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.request_tasks import (
    PHASE_AWAITING_FIRST_BYTE,
    PHASE_BETWEEN_ATTEMPTS,
    PHASE_STREAMING_STOPPED,
)
from my_claude_code.core.upstream_ladder import record_upstream_try
from my_claude_code.core.waiting_clock import credit_waiting


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _capture(store: RequestLogStore | None, **overrides) -> RequestCapture:
    defaults: dict[str, Any] = {
        "request_id": "req_watch",
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "stream": True,
        "requested_model": "mcc/best",
        "input_text": "hello",
        "params": {"max_tokens": 100},
        "harness": "claude",
    }
    defaults.update(overrides)
    return RequestCapture(store, **defaults)


def _frames(*events: str) -> list[str]:
    return [f"event: {name}\ndata: {json.dumps({'type': name})}\n\n" for name in events]


async def _feed(chunks: list[str]):
    for chunk in chunks:
        yield chunk


def test_building_a_capture_registers_exactly_one_request(store) -> None:
    assert request_tasks.count() == 0
    _capture(store)
    assert request_tasks.count() == 1
    entry = request_tasks.snapshot()[0]
    assert entry.request_id == "req_watch"
    assert entry.endpoint == "/v1/messages"
    assert entry.harness == "claude"
    assert entry.requested_model == "mcc/best"


def test_a_request_the_operator_chose_not_to_log_is_still_tracked() -> None:
    """A request with the log off can park for 47 minutes just the same."""

    capture = _capture(None)
    assert capture.enabled is False
    assert request_tasks.count() == 1
    capture.finish_success("done")
    assert request_tasks.count() == 0


def test_finish_success_unregisters(store) -> None:
    _capture(store).finish_success("hi")
    assert request_tasks.count() == 0


def test_finish_error_unregisters(store) -> None:
    _capture(store).finish_error(
        ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            message="no",
            status_code=502,
            retryable=False,
        )
    )
    assert request_tasks.count() == 0


def test_a_locally_answered_optimizer_request_unregisters(store) -> None:
    capture = _capture(store)
    capture.set_optimization("cache_hit", 42)
    capture.finish_success_from_message(
        type("Msg", (), {"content": [], "usage": None, "stop_reason": "end_turn"})()
    )
    assert request_tasks.count() == 0


@pytest.mark.asyncio
async def test_a_completed_stream_unregisters(store) -> None:
    capture = _capture(store)
    body = capture.wrap(_feed(_frames("message_start", "message_stop")))
    assert [chunk async for chunk in body]
    assert request_tasks.count() == 0


@pytest.mark.asyncio
async def test_a_consumer_that_walks_away_unregisters(store) -> None:
    """The ``GeneratorExit`` branch: a client that closed the connection."""

    capture = _capture(store)
    body = capture.wrap(_feed(_frames("message_start", "content", "message_stop")))
    assert await anext(body)
    await try_close_async_iterator(body)
    assert request_tasks.count() == 0


@pytest.mark.asyncio
async def test_a_cancelled_stream_unregisters(store) -> None:
    capture = _capture(store)

    async def never():
        yield _frames("message_start")[0]
        await asyncio.Event().wait()

    body = capture.wrap(never())

    async def drain() -> None:
        async for _ in body:
            pass

    task = asyncio.create_task(drain())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert request_tasks.count() == 0


@pytest.mark.asyncio
async def test_a_stream_that_raises_before_any_byte_unregisters(store) -> None:
    capture = _capture(store)

    async def explode():
        raise RuntimeError("upstream died")
        yield ""  # pragma: no cover - unreachable, keeps it an async generator

    body = capture.wrap(explode())
    with pytest.raises(RuntimeError):
        async for _ in body:
            pass
    assert request_tasks.count() == 0


def test_finalizing_twice_is_still_one_unregister(store) -> None:
    capture = _capture(store)
    capture.finish_success("a")
    capture.finish_success("b")
    assert request_tasks.count() == 0


# ------------------------------------------------------------ progress/phase


def test_the_phase_before_any_attempt_is_between_attempts(store) -> None:
    progress = _capture(store).watchdog_progress()
    assert progress.phase == PHASE_BETWEEN_ATTEMPTS
    assert progress.attempt_index is None


@pytest.mark.asyncio
async def test_the_phase_after_the_first_byte_is_streaming_stopped(store) -> None:
    capture = _capture(store)
    body = capture.wrap(_feed(_frames("message_start")))
    await anext(body)
    assert capture.watchdog_progress().phase == PHASE_STREAMING_STOPPED
    await try_close_async_iterator(body)


def test_a_finished_try_with_no_byte_reads_as_between_attempts(store) -> None:
    """The 09-16 shape: ``tries: 1``, no byte, and then 47 minutes."""

    capture = _capture(store)
    capture._attempt_provider = "opencode"
    capture._attempt_index = 0
    assert capture.watchdog_progress().phase == PHASE_AWAITING_FIRST_BYTE
    record_upstream_try(status=429, error_kind="rate_limit")
    progress = capture.watchdog_progress()
    assert progress.tries == 1
    assert progress.phase == PHASE_BETWEEN_ATTEMPTS


def test_a_try_that_ended_with_a_head_still_reads_as_awaiting_a_byte(store) -> None:
    """A 200 head then silence is an upstream that is being read, not a gap.

    Measured on the scratch rig: a fake upstream that sends the SSE head and
    one empty role delta and then stops parks at
    ``httpx/_models.py Response.aiter_raw`` -- inside the read, not between
    attempts -- and the phase has to say so.
    """

    capture = _capture(store)
    capture._attempt_provider = "custom_rig"
    capture._attempt_index = 0
    record_upstream_try(status=200)
    progress = capture.watchdog_progress()
    assert progress.tries == 1
    assert progress.phase == PHASE_AWAITING_FIRST_BYTE


def test_the_progress_reader_sees_the_pools_and_the_waiting_clock(store) -> None:
    capture = _capture(store)
    record_credential(0, "sk-8...Kofx")
    record_proxy("173.249.24.121:1080")
    credit_waiting(2.5)
    progress = capture.watchdog_progress()
    assert progress.key_label == "sk-8...Kofx"
    assert progress.proxy_label == "173.249.24.121:1080"
    assert progress.waited_seconds == pytest.approx(2.5)


def test_the_progress_reader_never_returns_any_text(store) -> None:
    capture = _capture(
        store,
        input_text="the quick brown fox sk-secret-12345",
        params={"max_tokens": 100, "api_key": "sk-secret-12345"},
    )
    rendered = repr(capture.watchdog_progress())
    assert "sk-secret" not in rendered
    assert "brown fox" not in rendered
