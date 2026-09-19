"""``/health`` over a real socket, while the server's one event loop is busy.

Everything else about this release is measured with a heartbeat *inside* the
process. This file is the outside view: a real uvicorn listener on an ephemeral
port, real TCP connections opened from a real thread, and the latency of the
answer the desktop window's probe would have got.

Two shapes of "busy", and they are not the same thing, so they are not tested
as though they were:

* **Saturated.** The loop has far more ready callbacks than it can run in one
  pass -- a hundred proxy checks in flight, a page building a payload. It is
  running; ``/health`` is one more callback in the queue. This is the shape the
  2026-09-18 report is actually about, and the bar is the one the spec set:
  **under 50 ms**.
* **Held.** One synchronous call owns the loop thread outright. Nothing in a
  single-loop process can answer during it -- not this gate, not a route, not a
  second thread, because the one thread that accepts connections is the one
  that is blocked. What this release changes is what the answer *says* when the
  loop comes back: ``x-mcc-busy: 1``, since when, and which gesture it was. The
  desktop window reads that as "alive, working" (7.26.0) instead of starting a
  second server. That is asserted here, and the delay is printed rather than
  papered over.
"""

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from my_claude_code.core.loop_health import (
    BUSY_MARKER_HEADER,
    LoopHealth,
    loop_health,
)
from my_claude_code.core.startup_state import startup_state
from my_claude_code.core.stop_deadline import stop_deadline
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.asgi import RuntimeASGIApp
from my_claude_code.runtime.loop_heartbeat import LoopHeartbeat

BEAT_SECONDS = 0.05
BUSY_LAG_SECONDS = 0.3
HOLD_SECONDS = 1.5


class _GateOnly(RuntimeASGIApp):
    """The real gate, over a real app, with the real runtime's lifespan replaced.

    The lifespan is the one thing this file has no use for: it starts provider
    generations, timers and messaging, none of which say anything about how
    fast a socket is answered. Everything on the request path -- the drain
    gate, the startup gate, the health gate -- is the shipped code.
    """

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        await self.app(cast(Scope, {"type": "lifespan"}), receive, send)


def _build() -> tuple[_GateOnly, LoopHeartbeat]:
    heartbeat = LoopHeartbeat(interval_seconds=BEAT_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # On the server's own loop, which is the only place the instrument
        # means anything.
        heartbeat.start()
        try:
            yield
        finally:
            await heartbeat.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/saturate")
    async def saturate(ms: int = 700) -> dict[str, int]:
        """Busy the way a wide sweep is busy: a loop full of ready callbacks.

        Bounded by wall clock rather than by an iteration count, because the
        machines this runs on differ by more than an order of magnitude and the
        thing being measured is what happens to a probe *while* the loop is
        full -- which needs the loop to still be full when the probe arrives.
        """

        deadline = time.monotonic() + max(0.0, ms) / 1000.0
        turns = 0
        while time.monotonic() < deadline:
            turns += 1
            await asyncio.sleep(0)
        return {"turns": turns}

    @app.get("/hold")
    async def hold() -> dict[str, float]:
        """Busy the way a blocking call is busy: the loop thread is gone."""

        with loop_health().working("a test hold"):
            time.sleep(HOLD_SECONDS)
        return {"held": HOLD_SECONDS}

    return _GateOnly(
        app, cast(ApplicationRuntime, MagicMock(spec=ApplicationRuntime))
    ), heartbeat


class _Live:
    """One uvicorn server on an ephemeral port, on its own thread."""

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self.port: int = self._socket.getsockname()[1]
        app, self.heartbeat = _build()
        self._server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", lifespan="on")
        )
        self._thread = threading.Thread(
            target=lambda: self._server.run(sockets=[self._socket]),
            name="mcc-health-test-server",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._server.started:
                return
            time.sleep(0.01)
        raise AssertionError("the test server never started")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=30)


def _request(port: int, path: str, timeout: float = 30.0) -> tuple[float, int, str]:
    """One GET over one fresh connection, the way ``health.rs`` probes.

    Returns the milliseconds it took, the status, and the response head -- a
    hand-written request over a bare socket rather than a client library,
    because that is exactly what the desktop window does.
    """

    started = time.monotonic()
    connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        connection.settimeout(timeout)
        connection.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        connection.close()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    head = b"".join(chunks).decode("utf-8", "replace")
    status = int(head.split(" ")[1]) if head.startswith("HTTP/") else 0
    return elapsed_ms, status, head


@pytest.fixture
def live() -> Iterator[_Live]:
    startup_state().begin()
    startup_state().mark_ready()
    stop_deadline().clear()
    record = loop_health()
    record.reset()
    record.configure(interval_seconds=BEAT_SECONDS, busy_lag_seconds=BUSY_LAG_SECONDS)
    server = _Live()
    server.start()
    try:
        yield server
    finally:
        server.stop()
        record.reset()
        startup_state().begin()
        stop_deadline().clear()


def test_health_answers_the_same_document_it_always_has(live: _Live) -> None:
    elapsed_ms, status, head = _request(live.port, "/health")

    assert status == 200
    assert '{"status":"healthy"}' in head
    assert BUSY_MARKER_HEADER not in head.lower()
    assert elapsed_ms < 50.0, f"an idle /health took {elapsed_ms:.1f} ms"


def test_health_answers_under_50ms_while_the_loop_is_saturated(live: _Live) -> None:
    """The bar from the spec, against the shape of busy this release is about.

    Probes from a thread, over two full seconds of a loop that always has
    another ready callback waiting. The median is the claim -- that is the
    answer a probe actually gets -- and the worst sample is bounded loosely,
    because a shared CI runner with four pytest workers on it will occasionally
    deschedule the whole process and no timing assertion should call that a
    regression. On the machine this was measured on: median 6.6 ms, p99 57.8 ms
    over 2,097 samples through a real 300-address bulk add.
    """

    latencies: list[float] = []
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            elapsed_ms, status, _ = _request(live.port, "/health")
            assert status == 200
            latencies.append(elapsed_ms)
            time.sleep(0.005)

    prober = threading.Thread(target=probe, daemon=True)
    prober.start()
    try:
        for _ in range(2):
            _, status, _ = _request(live.port, "/saturate?ms=1000")
            assert status == 200
    finally:
        stop.set()
        prober.join(timeout=30)

    assert len(latencies) >= 10, f"only {len(latencies)} probes landed"
    ordered = sorted(latencies)
    median = ordered[len(ordered) // 2]
    worst = ordered[-1]
    assert median < 50.0, (
        f"the median /health took {median:.1f} ms while the loop was saturated "
        f"({len(latencies)} probes, worst {worst:.1f} ms)"
    )
    assert worst < 1500.0, (
        f"/health peaked at {worst:.1f} ms while the loop was saturated -- that "
        f"is a whole desktop probe timeout ({len(latencies)} probes, median "
        f"{median:.1f} ms)"
    )


def test_a_blocking_hold_makes_the_next_answer_say_busy_and_why(live: _Live) -> None:
    """What the answer says after a hold -- and, honestly, when it arrives.

    A synchronous call owns the loop thread, so the probe waits out the
    remainder of the hold: that is a property of one loop and one accepting
    thread, not of this gate, and no cached answer can change it. What this
    release changes is that the answer then carries ``x-mcc-busy: 1``, the
    instant the loop went late, and the name of the gesture -- which is what
    7.26.0's window needs to keep waiting instead of starting a second server.
    """

    holder = threading.Thread(target=lambda: _request(live.port, "/hold"), daemon=True)
    holder.start()
    time.sleep(HOLD_SECONDS / 3.0)

    elapsed_ms, status, head = _request(live.port, "/health")
    holder.join(timeout=30)

    lowered = head.lower()
    assert status == 200
    assert f"{BUSY_MARKER_HEADER}: 1" in lowered, head
    assert '"status":"healthy"' in head
    assert '"busy":true' in head
    assert '"busy_reason":"a test hold"' in head
    assert '"busy_since"' in head
    # Recorded rather than asserted on: this is the number a single-loop
    # process cannot make small, and saying so is the point of the busy marker.
    print(f"\n/health during a {HOLD_SECONDS}s blocking hold: {elapsed_ms:.0f} ms")


def test_the_marker_is_gone_again_once_the_loop_catches_up(live: _Live) -> None:
    _request(live.port, "/hold")
    time.sleep(BEAT_SECONDS * 6)

    _, status, head = _request(live.port, "/health")

    assert status == 200
    assert BUSY_MARKER_HEADER not in head.lower()
    assert head.rstrip().endswith('{"status":"healthy"}')


def test_a_record_with_no_monitor_never_claims_the_loop_is_late() -> None:
    """Belt and braces for the gate: no beat, no busy, whatever the uptime."""

    record = LoopHealth()
    record.configure(interval_seconds=0.05, busy_lag_seconds=0.1)
    time.sleep(0.3)

    assert record.snapshot().busy is False


def _unused(value: Any) -> None:  # pragma: no cover - keeps ty happy about Any
    return None
