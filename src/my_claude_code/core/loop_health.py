"""How late the event loop is, and the answer ``/health`` gives while it is.

The 2026-09-18 report was a server that never died. A bulk add of three hundred
addresses held the event loop long enough that one ``/health`` probe timed out,
the desktop window read that single late answer as "the server is gone", started
a second server, and the second took the port from the first by pid. 7.26.0
taught the window to wait. This module is the other half: it lets the server
*say* "alive, working" instead of saying nothing at all.

Two facts, one process-wide record:

* **The lag.** One task sleeps :attr:`LoopHealth.interval_seconds` and records
  how much later than that it actually woke. A loop that is keeping up records
  a fraction of a millisecond; a loop held by a sweep records the whole hold.
* **The reason.** The long admin gestures name themselves while they run
  (:meth:`LoopHealth.working`), so a busy answer can say *why* rather than only
  that.

Deliberately in ``core`` and deliberately dependency-free: ``startup_state`` and
``stop_deadline`` are the two records ``/health`` already consults from the
outermost ASGI layer, and this is the third. It holds no configuration of its
own -- the runtime hands it the two numbers at start -- because ``core`` may not
import ``config``.

Nothing here schedules anything. :func:`loop_health` is a record; the task that
beats it lives in ``runtime/application.py``.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

#: The header a busy-but-alive answer carries. The desktop window reads it and
#: paints "working" rather than starting a second server; anything that does not
#: know the header sees an ordinary 200 with an ordinary body, which is why the
#: whole contract is additive.
BUSY_MARKER_HEADER = "x-mcc-busy"

#: Its only value. Present means busy; absent means the loop is keeping up.
BUSY_MARKER_VALUE = "1"

#: How often the beat task wakes, in seconds, when nothing says otherwise.
#: Ten times a second: small enough that a half-second lag is two missed beats
#: rather than a rounding error, large enough to be free.
DEFAULT_BEAT_INTERVAL_SECONDS = 0.1

#: How late the loop has to be before the answer says so, in seconds. Half a
#: second is well above any healthy scheduling jitter and well below the
#: shortest probe timeout any client uses.
DEFAULT_BUSY_LAG_SECONDS = 0.5

#: What a busy answer says when nothing named itself. Not "unknown": the
#: operator asking is owed a sentence, and "some work" is the truth.
DEFAULT_BUSY_REASON = "a long operation"


@dataclass(frozen=True)
class LoopHealthSnapshot:
    """One consistent read of the record: is the loop late, how late, why."""

    busy: bool
    lag_seconds: float
    busy_since: str
    busy_reason: str

    def as_body_fields(self) -> dict[str, object]:
        """The keys a busy ``/health`` body gains. Additive, always.

        A healthy answer gains nothing at all, so every existing reader sees the
        document it has always seen.
        """

        if not self.busy:
            return {}
        return {
            "busy": True,
            "busy_since": self.busy_since,
            "busy_reason": self.busy_reason,
            "busy_lag_ms": round(self.lag_seconds * 1000.0),
        }


class LoopHealth:
    """The process-wide record of event-loop lateness.

    Every method is safe to call from any thread, because the reader is the
    ASGI gate and the writer is a loop task and there is no rule that says they
    share a thread. The lock is held for assignments only.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._interval_seconds = DEFAULT_BEAT_INTERVAL_SECONDS
        self._busy_lag_seconds = DEFAULT_BUSY_LAG_SECONDS
        self._last_beat = time.monotonic()
        self._last_lag = 0.0
        self._busy_since_monotonic: float | None = None
        self._busy_since_wall = ""
        self._reasons: list[str] = []
        self._last_reason = ""
        # Whether anything is actually beating. A record nobody is measuring
        # must never claim the loop is late: without this, a process that never
        # started the monitor -- a test, a tool importing the app, the window
        # between a shutdown and the last answer -- would report a lag equal to
        # its whole uptime and call a perfectly healthy server busy.
        self._armed = False

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def busy_lag_seconds(self) -> float:
        return self._busy_lag_seconds

    def configure(self, *, interval_seconds: float, busy_lag_seconds: float) -> None:
        """Adopt the operator's two numbers. Called once, at start."""

        with self._lock:
            self._interval_seconds = max(0.001, float(interval_seconds))
            self._busy_lag_seconds = max(0.0, float(busy_lag_seconds))

    def reset(self) -> None:
        """Forget everything. Tests, and a second start in one process."""

        with self._lock:
            self._interval_seconds = DEFAULT_BEAT_INTERVAL_SECONDS
            self._busy_lag_seconds = DEFAULT_BUSY_LAG_SECONDS
            self._last_beat = time.monotonic()
            self._last_lag = 0.0
            self._busy_since_monotonic = None
            self._busy_since_wall = ""
            self._reasons = []
            self._last_reason = ""
            self._armed = False

    def disarm(self) -> None:
        """Stop answering the "is the loop late" question. The monitor has stopped."""

        with self._lock:
            self._armed = False
            self._busy_since_monotonic = None
            self._busy_since_wall = ""

    def beat(self, lag_seconds: float | None = None) -> float:
        """Record one wake of the beat task, and return the lag it measured.

        ``lag_seconds`` is normally computed here, from how long it has actually
        been since the previous beat minus how long the task asked to sleep. A
        caller may supply one instead, which is what the unit tests do rather
        than sleeping.
        """

        now = time.monotonic()
        with self._lock:
            measured = (
                max(0.0, now - self._last_beat - self._interval_seconds)
                if lag_seconds is None
                else max(0.0, float(lag_seconds))
            )
            self._last_beat = now
            self._last_lag = measured
            # A beat IS the monitor running, so it is what arms the record.
            self._armed = True
            self._note_busy_locked(measured, now)
            return measured

    def snapshot(self) -> LoopHealthSnapshot:
        """Read the record, counting the beat that has not happened yet.

        This is the load-bearing line. While the loop is held, the beat task is
        not running, so ``_last_lag`` is whatever the last healthy beat wrote --
        zero. The lag a reader wants is the one *in progress*: how long it has
        been since the last beat, less the interval that beat was entitled to.
        A request served at the far end of a three-second hold therefore reports
        three seconds, not nothing.
        """

        now = time.monotonic()
        with self._lock:
            if not self._armed:
                return LoopHealthSnapshot(
                    busy=False, lag_seconds=0.0, busy_since="", busy_reason=""
                )
            pending = max(0.0, now - self._last_beat - self._interval_seconds)
            lag = max(self._last_lag, pending)
            self._note_busy_locked(lag, now)
            busy = self._busy_lag_seconds > 0.0 and lag >= self._busy_lag_seconds
            return LoopHealthSnapshot(
                busy=busy,
                lag_seconds=lag,
                busy_since=self._busy_since_wall if busy else "",
                busy_reason=self._reason_locked() if busy else "",
            )

    def _reason_locked(self) -> str:
        """Which gesture a busy answer names. Caller holds the lock.

        The one that is running, if one is. Otherwise the one that *was*
        running most recently -- because the commonest case is a gesture that
        held the loop outright: nothing could read the record while it ran, and
        the first answer after it is the one that has to explain the gap. The
        memory is cleared the moment the loop catches up, so it can never
        describe a window it did not cause.
        """

        if self._reasons:
            return self._reasons[-1]
        return self._last_reason or DEFAULT_BUSY_REASON

    def _note_busy_locked(self, lag: float, now: float) -> None:
        """Open or close the current busy window. Caller holds the lock."""

        if self._busy_lag_seconds > 0.0 and lag >= self._busy_lag_seconds:
            if self._busy_since_monotonic is None:
                # The window started when the loop was last on time, not when
                # somebody noticed: a probe that arrives at the end of a hold
                # must not report the hold as having just begun.
                self._busy_since_monotonic = now - lag
                self._busy_since_wall = datetime.fromtimestamp(
                    time.time() - lag, tz=UTC
                ).isoformat(timespec="milliseconds")
            return
        self._busy_since_monotonic = None
        self._busy_since_wall = ""
        self._last_reason = ""

    @contextmanager
    def working(self, reason: str) -> Iterator[None]:
        """Name the gesture that is about to hold the loop, for as long as it does.

        Nested gestures stack and the innermost is what a busy answer reports,
        because that is the one an operator can act on. A gesture that raises
        still pops: the reason is about what is running now, and nothing is
        running now.
        """

        text = reason.strip() or DEFAULT_BUSY_REASON
        with self._lock:
            self._reasons.append(text)
        try:
            yield
        finally:
            with self._lock:
                with suppress(ValueError):
                    self._reasons.remove(text)
                # Remembered, not forgotten: a gesture that held the loop
                # outright is over by the time anything can read this, and it
                # is the answer to "why was the server late". Cleared the
                # moment the loop catches up.
                self._last_reason = text


_LOOP_HEALTH = LoopHealth()


def loop_health() -> LoopHealth:
    """The one record. A module global for the same reason ``stop_deadline`` is."""

    return _LOOP_HEALTH


__all__ = [
    "BUSY_MARKER_HEADER",
    "BUSY_MARKER_VALUE",
    "DEFAULT_BEAT_INTERVAL_SECONDS",
    "DEFAULT_BUSY_LAG_SECONDS",
    "DEFAULT_BUSY_REASON",
    "LoopHealth",
    "LoopHealthSnapshot",
    "loop_health",
]
