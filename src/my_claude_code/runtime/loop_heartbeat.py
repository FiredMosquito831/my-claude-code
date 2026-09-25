"""The beat that keeps ``/health`` honest, and the answer it keeps ready.

Two small things that belong together:

* :class:`LoopHeartbeat` -- one task that sleeps
  ``HEALTH_HEARTBEAT_INTERVAL_MS`` and tells :func:`core.loop_health.loop_health`
  how much later than that it woke. That is the whole loop-lag monitor.
* :func:`cached_health_answer` -- the ``/health`` document, rendered once and
  re-rendered by the same task, so answering a probe is two ``send`` calls and
  no work at all. The document is a constant today; keeping it *cached* rather
  than *built per request* is what makes the answer independent of everything
  the route layer would otherwise do to produce it (dependency resolution, the
  middleware stack, a router match).

Why this is not a second thread with a second socket: the listening socket has
exactly one owner (``cli/commands.py::_bind_listening_socket``), and a second
event loop accepting from a duplicate of it would take an arbitrary half of
*every* request -- including the ones it cannot serve. The measured answer to
"how does ``/health`` stay fast" is therefore: cost it nothing here, and stop
the long gestures holding the loop in the first place (7.27.0's other half).
"""

import asyncio
import json
import time
from contextlib import suppress

from loguru import logger

from my_claude_code.core.loop_health import (
    BUSY_MARKER_HEADER,
    BUSY_MARKER_VALUE,
    loop_health,
)

#: The body every healthy ``/health`` has returned since the route existed, and
#: still returns byte for byte. Nothing is removed from it, ever; a busy answer
#: only adds keys beside these.
HEALTHY_FIELDS: dict[str, object] = {"status": "healthy"}

_JSON_CONTENT_TYPE = b"application/json"

_CACHED: tuple[bytes, list[tuple[bytes, bytes]]] | None = None


def _render(
    fields: dict[str, object], *, busy: bool
) -> tuple[bytes, list[tuple[bytes, bytes]]]:
    body = json.dumps(fields, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", _JSON_CONTENT_TYPE),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if busy:
        headers.append(
            (
                BUSY_MARKER_HEADER.encode("ascii"),
                BUSY_MARKER_VALUE.encode("ascii"),
            )
        )
    return body, headers


def refresh_health_answer() -> None:
    """Re-render the ready answer. Called by the beat, cheap enough to be free."""

    global _CACHED
    _CACHED = _render(dict(HEALTHY_FIELDS), busy=False)


def reset_health_answer() -> None:
    """Drop the cache so the next read rebuilds it. Tests, and a restart."""

    global _CACHED
    _CACHED = None


def cached_health_answer() -> tuple[bytes, list[tuple[bytes, bytes]]]:
    """The answer to send when the loop is keeping up.

    Rebuilt on demand if the beat has not run yet, so a probe that arrives in
    the first hundred milliseconds of a process is answered from the same path
    as every later one rather than from a second one nobody tested.
    """

    cached = _CACHED
    if cached is None:
        refresh_health_answer()
        cached = _CACHED
    assert cached is not None
    return cached


def busy_health_answer() -> tuple[bytes, list[tuple[bytes, bytes]]]:
    """The answer to send when the loop is late: the same 200, plus why."""

    snapshot = loop_health().snapshot()
    fields = dict(HEALTHY_FIELDS)
    fields.update(snapshot.as_body_fields())
    return _render(fields, busy=True)


class LoopHeartbeat:
    """One task, one sleep, one subtraction.

    It measures the only thing that matters about an event loop under load: how
    long it took to come back to a task that asked for a hundred milliseconds.
    """

    def __init__(self, *, interval_seconds: float) -> None:
        self._interval = max(0.001, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def set_interval(self, interval_seconds: float) -> None:
        """Beat at a new interval from the next beat on.

        The loop reads ``_interval`` afresh for every sleep, so a saved
        ``HEALTH_HEARTBEAT_INTERVAL_MS`` needs no new task: the beat already
        asleep finishes on the old interval and the one after it uses the new
        one. Nothing is cancelled, so the loop-health record is never left
        without a monitor in between.
        """

        self._interval = max(0.001, float(interval_seconds))

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        refresh_health_answer()
        self._task = asyncio.create_task(self._run(), name="mcc-loop-heartbeat")

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        # Nothing is measuring any more, so nothing may claim the loop is late.
        loop_health().disarm()

    async def _run(self) -> None:
        record = loop_health()
        previous = time.monotonic()
        worst = 0.0
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            now = time.monotonic()
            lag = record.beat(max(0.0, now - previous - self._interval))
            previous = now
            refresh_health_answer()
            # One line per new record, and never once per beat: this is ten
            # wakes a second for the life of the process, and a log that says
            # "the loop was late" ten times a second is a log nobody reads.
            if lag >= max(1.0, record.busy_lag_seconds * 2.0) and lag > worst * 1.5:
                worst = lag
                logger.debug(
                    "LOOP: the event loop was {:.0f} ms late for a {:.0f} ms "
                    "beat ({}).",
                    lag * 1000.0,
                    self._interval * 1000.0,
                    record.snapshot().busy_reason or "no gesture named itself",
                )


__all__ = [
    "HEALTHY_FIELDS",
    "LoopHeartbeat",
    "busy_health_answer",
    "cached_health_answer",
    "refresh_health_answer",
    "reset_health_answer",
]
