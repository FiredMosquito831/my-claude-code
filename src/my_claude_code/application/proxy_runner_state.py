"""What each WireGuard runner is doing, and the hook to the supervisor (7.108.0).

The supervisor that starts and stops wireproxy lives in the runtime
(``runtime/proxy_runners.py``): it owns child processes and registers them
with the process registry, which only the runtime may import. The Proxying
page's routes live in ``api``, which may not import the runtime. This module
is the one meeting point, shaped like ``SOURCE_SCHEDULE`` (7.91.0):

* the runtime **attaches** its supervisor at start and detaches it at close;
  a route presses a button through :data:`RUNNERS`, and with nothing attached
  (a test app, a process that is not the server) the press is refused with a
  sentence rather than starting anything;
* the supervisor **writes** a :class:`RunnerReading` per source as its state
  changes, and the Sources payload reads them. A reading is process memory:
  it is never written into a source's address or its health record (spec
  §5.7), and a restart begins with none.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

#: The states a card shows (spec §5.7). ``stopped`` covers "never started",
#: "stopped by you" and "exited with code N" -- the sentence says which.
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_RESTARTING = "restarting"
STATE_CRASHED = "crashed"
RUNNER_STATES: tuple[str, ...] = (
    STATE_STOPPED,
    STATE_STARTING,
    STATE_READY,
    STATE_RESTARTING,
    STATE_CRASHED,
)
#: The automatic restart's waits, in seconds, when the operator ticked it:
#: then ``crashed`` until they act (spec §5.8).
RESTART_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 5.0, 30.0, 300.0)


@dataclass(frozen=True, slots=True)
class RunnerReading:
    """One runner's state, for its card. No secret is ever in one."""

    state: str = STATE_STOPPED
    sentence: str = "Not started."
    pid: int = 0
    since: str = ""
    exit_code: int | None = None
    restarts: int = 0
    #: When the next automatic restart is due (ISO), while ``restarting``.
    restart_at: str = ""
    #: The last lines wireproxy printed, already redacted.
    log: tuple[str, ...] = field(default_factory=tuple)

    @property
    def running(self) -> bool:
        return self.state in {STATE_STARTING, STATE_READY}

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "sentence": self.sentence,
            "pid": self.pid,
            "since": self.since,
            "exit_code": self.exit_code,
            "restarts": self.restarts,
            "restart_at": self.restart_at,
            "log": list(self.log),
        }


class RunnerControl(Protocol):
    """What the runtime's supervisor answers to. Every call is synchronous and
    bounded; a route runs it on a worker thread."""

    def start(self, source_id: str) -> RunnerReading: ...

    def stop(self, source_id: str) -> RunnerReading: ...

    def restart(self, source_id: str) -> RunnerReading: ...

    def remove(self, source_id: str) -> None: ...


class RunnerUnavailableError(RuntimeError):
    """A button pressed in a process that runs no supervisor."""


class RunnerHub:
    """The supervisor's slot and every runner's last reading."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._control: RunnerControl | None = None
        self._readings: dict[str, RunnerReading] = {}

    def attach(self, control: RunnerControl) -> None:
        with self._lock:
            self._control = control

    def detach(self) -> None:
        with self._lock:
            self._control = None

    def control(self) -> RunnerControl:
        with self._lock:
            control = self._control
        if control is None:
            raise RunnerUnavailableError(
                "This process runs no WireGuard runners (it is not the MCC "
                "server), so nothing was started or stopped."
            )
        return control

    @property
    def attached(self) -> bool:
        with self._lock:
            return self._control is not None

    def set_reading(self, source_id: str, reading: RunnerReading) -> None:
        with self._lock:
            self._readings[source_id] = reading

    def reading(self, source_id: str) -> RunnerReading | None:
        with self._lock:
            return self._readings.get(source_id)

    def readings(self) -> Mapping[str, RunnerReading]:
        with self._lock:
            return dict(self._readings)

    def forget(self, source_id: str) -> None:
        with self._lock:
            self._readings.pop(source_id, None)

    def reset(self) -> None:
        with self._lock:
            self._control = None
            self._readings.clear()


RUNNERS = RunnerHub()


def reset_runner_state() -> None:
    """For tests: no supervisor attached, no readings."""

    RUNNERS.reset()


__all__ = [
    "RESTART_BACKOFF_SECONDS",
    "RUNNERS",
    "RUNNER_STATES",
    "STATE_CRASHED",
    "STATE_READY",
    "STATE_RESTARTING",
    "STATE_STARTING",
    "STATE_STOPPED",
    "RunnerControl",
    "RunnerHub",
    "RunnerReading",
    "RunnerUnavailableError",
    "reset_runner_state",
]
