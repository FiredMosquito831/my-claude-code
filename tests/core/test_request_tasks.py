"""The request-to-task seam: what it records, and that it cannot leak.

Registration is one statement of ``RequestCapture.__init__`` and
unregistration is the *first* statement of ``_begin_finalize``. Those two are
asserted against the real capture in
``tests/api/test_request_capture_watchdog.py``; what is asserted here is the
registry's own behaviour -- the weakref reaping that covers the one path
``_begin_finalize`` cannot reach, and the definition of progress.
"""

import asyncio

import pytest

from my_claude_code.core import request_tasks
from my_claude_code.core.request_tasks import (
    MAX_TASKS_PER_REQUEST,
    PHASE_AWAITING_FIRST_BYTE,
    RequestProgress,
)


def _register(request_id: str = "req_1", **kwargs) -> request_tasks.RequestTaskEntry:
    entry = request_tasks.register(
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        harness="claude",
        requested_model="mcc/best",
        progress=kwargs.pop("progress", None),
    )
    assert entry is not None
    return entry


def test_register_then_unregister_leaves_nothing() -> None:
    _register()
    assert request_tasks.count() == 1
    request_tasks.unregister("req_1")
    assert request_tasks.count() == 0
    # Idempotent: several terminal paths may reach the choke point.
    request_tasks.unregister("req_1")
    assert request_tasks.count() == 0


def test_the_registry_is_empty_when_the_watchdog_is_off() -> None:
    request_tasks.configure(enabled=False)
    assert (
        request_tasks.register(
            request_id="req_off",
            endpoint="/v1/messages",
            protocol="anthropic",
            stream=True,
            harness=None,
            requested_model=None,
            progress=None,
        )
        is None
    )
    assert request_tasks.count() == 0
    request_tasks.note_stream_chunk()  # must not raise off the loop either
    request_tasks.configure(enabled=True)


def test_switching_it_off_drops_what_it_was_already_holding() -> None:
    _register()
    assert request_tasks.count() == 1
    request_tasks.configure(enabled=False)
    assert request_tasks.count() == 0
    request_tasks.configure(enabled=True)


@pytest.mark.asyncio
async def test_a_finished_task_is_reaped_without_any_timeout_rule() -> None:
    """The 09-16 requests lived 47 minutes. No age may ever reap an entry."""

    done = asyncio.Event()

    async def serve() -> None:
        _register("req_task")
        done.set()

    task = asyncio.create_task(serve())
    await done.wait()
    await task
    assert request_tasks.count() == 1
    # The request never finalized -- the path a streaming request with the
    # request log switched off takes -- and the reader reaps it because its
    # only task is finished, not because it is old.
    assert request_tasks.snapshot() == []
    assert request_tasks.count() == 0
    assert request_tasks.reaped_total() == 1


@pytest.mark.asyncio
async def test_a_live_task_is_never_reaped_however_long_it_waits() -> None:
    event = asyncio.Event()
    ready = asyncio.Event()

    async def serve() -> None:
        _register("req_live")
        ready.set()
        await event.wait()

    task = asyncio.create_task(serve())
    await ready.wait()
    try:
        for _ in range(5):
            assert len(request_tasks.snapshot()) == 1
            await asyncio.sleep(0)
    finally:
        event.set()
        await task


@pytest.mark.asyncio
async def test_a_second_task_iterating_the_body_is_adopted() -> None:
    """Streaming is held by a child task; the handler task is not enough."""

    ready = asyncio.Event()
    release = asyncio.Event()
    entry: list[request_tasks.RequestTaskEntry] = []

    async def handler() -> None:
        entry.append(_register("req_two"))

        async def stream() -> None:
            request_tasks.note_stream_chunk()
            ready.set()
            await release.wait()

        await asyncio.create_task(stream())

    task = asyncio.create_task(handler())
    await ready.wait()
    try:
        assert len(entry[0].tasks()) == 2
        assert entry[0].chunks == 1
        assert entry[0].last_chunk_mono is not None
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_an_entry_never_collects_more_tasks_than_its_cap() -> None:
    entry = _register("req_cap")

    async def touch() -> None:
        request_tasks.note_serving_task()
        await asyncio.sleep(3600)

    tasks = [asyncio.create_task(touch()) for _ in range(MAX_TASKS_PER_REQUEST + 4)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert len(entry.tasks()) <= MAX_TASKS_PER_REQUEST
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_a_progress_reader_that_raises_never_escapes() -> None:
    def broken() -> RequestProgress:
        raise RuntimeError("no")

    entry = _register("req_broken", progress=broken)
    assert entry.read_progress() is None


def test_the_signature_moves_only_on_real_progress() -> None:
    base = RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE)
    assert (
        base.signature() == RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE).signature()
    )
    moved = [
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, attempt_index=1),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, tries=1),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, output_chars=1),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, thinking_chars=1),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, ttft_ms=12.0),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, waited_seconds=0.5),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, key_label="sk-8...Kofx"),
        RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, proxy_label="1.2.3.4:1080"),
    ]
    for candidate in moved:
        assert candidate.signature() != base.signature(), candidate
    # The phase itself is NOT progress: a request can change phase by failing.
    renamed = RequestProgress(phase="streaming_stopped")
    assert renamed.signature() == base.signature()
