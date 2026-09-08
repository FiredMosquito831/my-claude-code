"""The event loop stays responsive while requests are in flight.

This is the regression guard for the three loop-holding defects 6.62.0 fixed:
finalisation on the loop (fix 3), the models.dev parse on the loop (fix 4) and
the OpenAI SDK import inside the first request (fix 5). The measurement is the
same one the investigation used -- a 20 ms heartbeat task, and the gap between
the tick it should have had and the tick it got.

The assertions are deliberately about *loop holding*, not about wall-clock
throughput: a shared CI runner cannot be asked how fast it is, but it can be
asked whether one coroutine blocked every other one, and that is the whole
defect.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.core.request_log import RequestLogStore

#: The bound the spec sets. A tick that is late by more than this means some
#: coroutine held the loop, which is the only thing under test here.
MAX_LOOP_GAP_MS = 150.0
MEAN_LOOP_GAP_MS = 30.0


class _Heartbeat:
    """A 20 ms tick, and the gaps it did not get."""

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _Heartbeat:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self._stop.set()
        if self._task is not None:
            await self._task
        return False

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            self.gaps.append((now - last - 0.02) * 1000.0)
            last = now

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0

    @property
    def mean_gap(self) -> float:
        return sum(self.gaps) / len(self.gaps) if self.gaps else 0.0


@pytest.fixture
def store(tmp_path) -> Any:
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _sse(*frames: tuple[str, dict[str, Any]]) -> list[str]:
    return [f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in frames]


async def _stream() -> AsyncIterator[str]:
    frames = _sse(
        (
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 12}}},
        ),
        *(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "token "},
                },
            )
            for _ in range(200)
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    for frame in frames:
        yield frame
        await asyncio.sleep(0)


def _capture(store: RequestLogStore, index: int) -> RequestCapture:
    return RequestCapture(
        store,
        request_id=f"req_{index}",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="hello",
        params={"max_tokens": 100},
    )


@pytest.mark.asyncio
async def test_loop_gap_under_concurrent_streams(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """16 concurrent 200-frame streams, each with an expensive finalisation.

    ``_apply_cost`` stands in for the whole of the finalisation arithmetic --
    PIL, tiktoken, the pricing ladder -- at 100 ms of blocking CPU per
    request. Sixteen of those on the event loop is 1.6 seconds of frozen
    server; off it, the heartbeat never misses a beat by more than the bound.
    """

    monkeypatch.setattr(store, "enqueue", lambda record: None)

    def expensive(_self: RequestCapture, _record: Any) -> None:
        time.sleep(0.1)

    # Stands in for the whole of the finalisation arithmetic, which is
    # exactly what ``_apply_cost`` sits at the end of.
    monkeypatch.setattr(RequestCapture, "_apply_cost", expensive)

    async def one(index: int) -> None:
        capture = _capture(store, index)
        async for _chunk in capture.wrap(_stream()):
            pass

    async with _Heartbeat() as beat:
        await asyncio.gather(*(one(index) for index in range(16)))

    assert beat.max_gap < MAX_LOOP_GAP_MS, (
        f"max loop gap {beat.max_gap:.0f}ms over {len(beat.gaps)} ticks"
    )
    assert beat.mean_gap < MEAN_LOOP_GAP_MS


@pytest.mark.asyncio
async def test_first_request_has_no_import_stall() -> None:
    """The OpenAI SDK is imported by the startup warmup, not by request #1.

    Measured at 2,170 ms in one heartbeat gap on the machine that reported
    this: ``importlib`` reading and compiling the SDK's type tree inside the
    request handler, with the loop held for all of it.
    """

    import sys

    from my_claude_code.runtime import warmup

    warmup.reset_request_path_warmup_for_tests()
    warmup.start_request_path_warmup()

    # The thread is the point: the loop must not be held while it works.
    async with _Heartbeat() as beat:
        deadline = time.monotonic() + 30.0
        while "openai" not in sys.modules and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    assert "openai" in sys.modules, "the warmup did not import the OpenAI SDK"
    assert beat.max_gap < MAX_LOOP_GAP_MS, (
        f"max loop gap {beat.max_gap:.0f}ms while the SDK was imported"
    )


@pytest.mark.asyncio
async def test_the_warmup_runs_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One server start, one warmup thread, however often it is asked for."""

    from my_claude_code.runtime import warmup

    warmup.reset_request_path_warmup_for_tests()
    spawned: list[int] = []
    monkeypatch.setattr(warmup, "_spawn", lambda: spawned.append(1))

    warmup.start_request_path_warmup()
    warmup.start_request_path_warmup()
    warmup.start_request_path_warmup()

    assert spawned == [1]
