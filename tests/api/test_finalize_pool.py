"""A finished answer's stream never waits on the default executor (7.90.1).

The dashboard's free-text search over all time runs one body scan per query on
the event loop's default executor, minutes each, and a scan cannot be
cancelled. Until 7.90.1 a streamed completion's finalize went to that same
pool, so once the scans filled it the client had every token but the stream did
not close until a scan finished (270 s and 282 s in the 2026-10-09 repro).

These tests park more threads than the default executor has workers -- the
scans -- and check that the stream still closes, that the finalize still sees
the request's context, and that a stop with four parked finalize jobs finishes
inside its bound.
"""

import asyncio
import contextvars
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

import pytest

from my_claude_code.api import finalize_pool
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from my_claude_code.core.stop_deadline import stop_deadline
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

pytestmark = pytest.mark.local_serial

#: Workers of the stand-in default executor, and the scans parked on it.
_DEFAULT_WORKERS = 4
_SCANS = 8

_REQUEST_TAG: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_tag", default=None
)


@pytest.fixture
def store(tmp_path) -> Iterator[RequestLogStore]:
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _enqueued(store: RequestLogStore, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    rows: list[RequestRecord] = []
    monkeypatch.setattr(store, "enqueue", rows.append)
    return rows


def _capture(store: RequestLogStore) -> RequestCapture:
    request = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "ping"}],
        }
    )
    capture = RequestCapture(
        store,
        request_id="req_finalize_pool",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="ping",
        params={"max_tokens": 32},
        request=request,
    )
    capture._record.provider = "nvidia_nim"
    capture._record.resolved_model = "acme-1"
    return capture


async def _body() -> AsyncIterator[str]:
    for event, data in (
        (
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "pong"},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ):
        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"


@asynccontextmanager
async def _default_executor_full() -> AsyncIterator[None]:
    """Every default-executor worker parked, the way the search scans hold it."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=_DEFAULT_WORKERS, thread_name_prefix="scan"
    )
    loop.set_default_executor(executor)
    release = threading.Event()
    parked = [loop.run_in_executor(None, release.wait) for _ in range(_SCANS)]
    # The premise, checked: a fresh to_thread really cannot get a worker.
    probe = asyncio.ensure_future(asyncio.to_thread(lambda: None))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(probe), 0.3)
        yield
    finally:
        release.set()
        await asyncio.gather(*parked, probe)
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_a_stream_closes_while_scans_fill_the_default_executor(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: on 7.90.0 this waits until a scan ends (here, forever)."""

    rows = _enqueued(store, monkeypatch)

    async def read_to_the_end() -> list[str]:
        return [chunk async for chunk in _capture(store).wrap(_body())]

    async with _default_executor_full():
        started = time.perf_counter()
        chunks = await asyncio.wait_for(read_to_the_end(), 2.0)
        closed_after = time.perf_counter() - started

    assert closed_after < 2.0
    assert len(chunks) == 3
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].tokens_in == 5


@pytest.mark.asyncio
async def test_the_finalize_sees_the_requests_context_on_its_own_pool(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context copy ``asyncio.to_thread`` made is kept."""

    rows = _enqueued(store, monkeypatch)
    seen: list[tuple[str | None, str]] = []
    real = RequestCapture._compute_finalize_fields

    def spying(self: RequestCapture, record: RequestRecord) -> None:
        seen.append((_REQUEST_TAG.get(), threading.current_thread().name))
        real(self, record)

    monkeypatch.setattr(RequestCapture, "_compute_finalize_fields", spying)
    _REQUEST_TAG.set("req-ctx-7901")

    await _capture(store)._finalize_off_loop("success")

    assert len(seen) == 1
    tag, thread_name = seen[0]
    assert tag == "req-ctx-7901"
    assert thread_name.startswith("mcc-finalize")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_closing_the_pool_with_four_parked_jobs_keeps_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded, never queued behind a scan, and the next finalize still runs."""

    monkeypatch.setattr(finalize_pool, "FINALIZE_DRAIN_SECONDS", 0.3)
    release = threading.Event()
    parked = [
        finalize_pool.run_on_finalize_pool(release.wait)
        for _ in range(finalize_pool.FINALIZE_POOL_WORKERS)
    ]
    queued = finalize_pool.run_on_finalize_pool(time.sleep, 0)
    try:
        async with _default_executor_full():
            started = time.perf_counter()
            await finalize_pool.close_finalize_pool()
            took = time.perf_counter() - started
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert all(not job.done() for job in parked)
    finally:
        release.set()
        await asyncio.gather(*parked)

    assert 0.25 <= took < 1.0
    # A RELOAD's next generation gets a fresh pool, not "cannot schedule".
    assert await finalize_pool.run_on_finalize_pool(lambda: 7) == 7
    await finalize_pool.close_finalize_pool()


@pytest.mark.asyncio
async def test_a_stop_with_four_parked_finalize_jobs_finishes_within_its_budget() -> (
    None
):
    """The runtime's own close, with the stop clock running."""

    manager = ProviderRuntimeManager(
        Settings().model_copy(update={"model": "nvidia_nim/model"})
    )
    runtime = ApplicationRuntime(manager, transcriber=None)
    release = threading.Event()
    parked = [
        finalize_pool.run_on_finalize_pool(release.wait)
        for _ in range(finalize_pool.FINALIZE_POOL_WORKERS)
    ]
    budget = stop_deadline().request(5.0)
    started = time.perf_counter()
    try:
        closed = await asyncio.wait_for(
            runtime.close(), stop_deadline().teardown_remaining()
        )
        took = time.perf_counter() - started
    finally:
        release.set()
        await asyncio.gather(*parked)

    assert closed is True
    assert took < budget
    assert took < finalize_pool.FINALIZE_DRAIN_SECONDS + 1.0
