"""How ``mcc-desktop`` decides the server it watches is dead, and who says so (7.70.0).

Until 7.70.0 the Python host probed ``/health`` every 5 s and, after three
failures in a row, raised one generic "stopped answering" notification -- which
on Windows with the desktop app installed went nowhere at all, because the host
runs a window-only stand-in that has no ``notify`` (the 7-hour outage of
2026-10-01 that nothing reported). This module is the decision half of the fix;
``cli/desktop.py`` gathers the facts and delivers the sentence.

**Dead is decided by the operating system, never by a timeout** (user decision
R4). After ``DESKTOP_HEALTH_FAILURE_THRESHOLD`` failed probes the host asks the
OS who holds the port, and only two answers are "dead":

* the process that last answered (its pid from ``x-mcc-pid``) is **gone**, or
  the port is held by a positively identified **stranger** -- said at once;
* that process is **alive but no longer holds the port** (a lost listener), or
  nothing listens and no pid is known -- said only once the same answer has
  held for ``DESKTOP_HEALTH_FAILURE_THRESHOLD`` observations spanning
  ``DESKTOP_HEALTH_FAILURE_THRESHOLD`` x ``DESKTOP_TICK_SECONDS`` (30 s), so an
  in-process reload, which closes and re-binds its own listener, is never
  announced as a death.

A server that still holds its port is **slow**, and slow is never dead: no
notification, ever, however long it lasts. A lookup that fails is "unknown",
and unknown is treated as alive. An update in progress is not a death either.

**Cadence** (decision 5 of the self-inflicted-load investigation): every
``DESKTOP_HEALTH_POLL_SECONDS`` (now 30 s) while the server answers, every
``DESKTOP_HEALTH_RETRY_SECONDS`` (5 s, fixed) once a probe has failed or before
the first one. While the server is slow the OS is re-asked at most every
``DESKTOP_RECONNECT_RESTATUS_SECONDS`` (30 s), because ``netstat`` on a busy
machine is itself load.

**Who shows it** (user answer 1, 2026-10-01 16:24): "if a desktop app running
come from desktop app else come from console where it is running".
:func:`notification_route` is that sentence as a function. The desktop app has
no notification code of its own yet, so while it runs the host shows nothing
and writes the line to ``server.log``; the app shows it from its next release.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    """What the OS said about a server that stopped answering."""

    #: Something that is ours, or is the pid we know, still holds the port.
    SLOW = "slow"
    #: The port is held and the holder could not be identified: alive.
    UNKNOWN = "unknown"
    #: An update helper is replacing the server on purpose.
    UPDATING = "updating"
    #: A positively identified non-MCC process holds the port.
    FOREIGN = "foreign"
    #: The known server process has exited.
    PROCESS_GONE = "process_gone"
    #: The known server process is alive and no longer holds the port.
    LISTENER_LOST = "listener_lost"
    #: Nothing holds the port and no server pid is known.
    NOT_LISTENING = "not_listening"


#: Announced on the first observation.
IMMEDIATE_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.PROCESS_GONE, Verdict.FOREIGN}
)

#: Announced only once they have held for the confirmation window.
CONFIRMED_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.LISTENER_LOST, Verdict.NOT_LISTENING}
)

#: Every verdict that is a dead server.
DEAD_VERDICTS: frozenset[Verdict] = IMMEDIATE_VERDICTS | CONFIRMED_VERDICTS


@dataclass(frozen=True, slots=True)
class PortFacts:
    """One fresh look at the port and the known server process."""

    #: Whether the OS let a socket bind the port just now.
    port_free: bool
    #: The pid the OS says holds the port, when it is held and could be read.
    holder_pid: int | None = None
    #: Whether that holder is My Claude Code; ``None`` when it could not tell.
    holder_is_mcc: bool | None = None
    #: The holder's image name, when known.
    holder_image: str | None = None
    #: The pid of the server last heard from (``x-mcc-pid``), or our child.
    known_pid: int | None = None
    #: Whether ``known_pid`` is alive; ``None`` when unknown (= alive).
    known_alive: bool | None = None
    #: Whether an update helper is installing right now.
    updating: bool = False


def classify_outage(facts: PortFacts) -> Verdict:
    """The verdict for one observation. Pure."""

    if facts.updating:
        return Verdict.UPDATING
    if not facts.port_free:
        if facts.holder_pid is not None and facts.holder_pid == facts.known_pid:
            return Verdict.SLOW
        if facts.holder_is_mcc is True:
            return Verdict.SLOW
        if facts.holder_is_mcc is False:
            return Verdict.FOREIGN
        return Verdict.UNKNOWN
    if facts.known_pid is None:
        return Verdict.NOT_LISTENING
    if facts.known_alive is False:
        return Verdict.PROCESS_GONE
    return Verdict.LISTENER_LOST


def _since(wall: float | None) -> str:
    if wall is None:
        return ""
    return f" (since {time.strftime('%H:%M', time.localtime(wall))})"


def dead_message(
    verdict: Verdict, facts: PortFacts, *, port: int, since: float | None
) -> str:
    """The sentence for a dead server: what happened, in plain words."""

    head = f"The My Claude Code server on port {port} is not answering{_since(since)}"
    if verdict is Verdict.PROCESS_GONE:
        return (
            f"{head}: process {facts.known_pid} has exited. Start it again "
            "with mcc-server."
        )
    if verdict is Verdict.LISTENER_LOST:
        return (
            f"{head}: process {facts.known_pid} is still running but no longer "
            "holds the port, so it cannot take new requests. Nothing was "
            "stopped; start a new server with mcc-server once it has exited."
        )
    if verdict is Verdict.FOREIGN:
        holder = facts.holder_image or "another program"
        return (
            f"{head}: the port is now held by {holder} (pid {facts.holder_pid}), "
            "which is not My Claude Code. Nothing was stopped."
        )
    return f"{head}: nothing is listening on the port. Start it again with mcc-server."


def recovered_message(*, port: int) -> str:
    """The sentence when a server announced as dead answers again."""

    return f"The My Claude Code server on port {port} is answering again."


class WatchStep(StrEnum):
    """What one probe asks the caller to do next."""

    NOTHING = "nothing"
    #: Look at the port and the process (one OS lookup) and report the facts.
    CHECK = "check"
    #: A server announced as dead answers again: say so.
    RECOVERED = "recovered"


class ServerWatch:
    """The host's health state: cadence, the threshold, and one announcement per outage.

    Pure apart from the two clocks, which are injectable so a test can run an
    hour of probing in a millisecond.
    """

    def __init__(
        self,
        *,
        threshold: int,
        healthy_interval: float,
        failing_interval: float,
        confirm_seconds: float,
        recheck_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._healthy_interval = max(0.0, float(healthy_interval))
        self._failing_interval = max(
            0.0, min(float(failing_interval), self._healthy_interval)
        )
        self._confirm_seconds = max(0.0, float(confirm_seconds))
        self._recheck_seconds = max(0.0, float(recheck_seconds))
        self._clock = clock
        self._wall = wall
        self._last_ok: bool | None = None
        self._failures = 0
        self._announced = False
        self._since: float | None = None
        self._next_check_at = 0.0
        self._pending: tuple[Verdict, float, int] | None = None
        self._known_pid: int | None = None

    @property
    def known_pid(self) -> int | None:
        """The pid of the server last heard from, if it ever named one."""

        return self._known_pid

    @property
    def since(self) -> float | None:
        """Wall time of the first failed probe of the current outage."""

        return self._since

    @property
    def announced(self) -> bool:
        """Whether this outage has been announced."""

        return self._announced

    def note_pid(self, pid: int | None) -> None:
        """Remember the pid an answer named (``x-mcc-pid``)."""

        if pid is not None and pid > 0:
            self._known_pid = pid

    def next_interval(self) -> float:
        """Seconds to the next probe: slow while healthy, fast otherwise."""

        if self._last_ok:
            return self._healthy_interval
        return self._failing_interval

    def record_probe(self, healthy: bool) -> WatchStep:
        """Fold one ``/health`` probe in."""

        if healthy:
            recovered = self._announced
            self._last_ok = True
            self._failures = 0
            self._announced = False
            self._since = None
            self._pending = None
            self._next_check_at = 0.0
            return WatchStep.RECOVERED if recovered else WatchStep.NOTHING
        self._last_ok = False
        self._failures += 1
        if self._since is None:
            self._since = self._wall()
        if self._announced or self._failures < self._threshold:
            return WatchStep.NOTHING
        if self._clock() < self._next_check_at:
            return WatchStep.NOTHING
        return WatchStep.CHECK

    def record_facts(self, facts: PortFacts) -> Verdict | None:
        """Fold one OS observation in; return the verdict to announce, once.

        ``None`` for anything that is not (yet) a death. A dead verdict is
        returned exactly once per outage; only a healthy probe re-arms it.
        """

        verdict = classify_outage(facts)
        now = self._clock()
        if verdict in IMMEDIATE_VERDICTS:
            self._announced = True
            self._pending = None
            return verdict
        if verdict in CONFIRMED_VERDICTS:
            pending = self._pending
            if pending is None or pending[0] is not verdict:
                pending = (verdict, now, 1)
            else:
                pending = (verdict, pending[1], pending[2] + 1)
            self._pending = pending
            _, first_seen, count = pending
            if count >= self._threshold and now - first_seen >= self._confirm_seconds:
                self._announced = True
                self._pending = None
                return verdict
            # Confirming: look again on the very next failed probe.
            self._next_check_at = now
            return None
        # Slow, unknown or updating: patience, and a cheaper cadence for the OS
        # lookup than for the probe.
        self._pending = None
        self._next_check_at = now + self._recheck_seconds
        return None


class NotificationRoute(StrEnum):
    """Where one notification goes."""

    #: The host's own tray icon (pystray), named My Claude Code, shows it.
    TRAY = "tray"
    #: The desktop app is running: it is the app's to show. Until the app can
    #: (its next release), the host shows nothing and only logs it.
    APP = "app"
    #: No app: one line on the console this host runs in.
    CONSOLE = "console"
    #: No app and no console (a windowless host): the log line is the record.
    LOG_ONLY = "log-only"


def notification_route(
    *, tray_can_notify: bool, desktop_app_running: bool, console_available: bool
) -> NotificationRoute:
    """User answer 1 as a function, with the host's own tray first.

    The tray comes first because it *is* the app's icon when it exists -- a
    pystray icon titled My Claude Code -- and that path is unchanged. Never a
    toast from another program's name while the app runs.
    """

    if tray_can_notify:
        return NotificationRoute.TRAY
    if desktop_app_running:
        return NotificationRoute.APP
    if console_available:
        return NotificationRoute.CONSOLE
    return NotificationRoute.LOG_ONLY


__all__ = [
    "CONFIRMED_VERDICTS",
    "DEAD_VERDICTS",
    "IMMEDIATE_VERDICTS",
    "NotificationRoute",
    "PortFacts",
    "ServerWatch",
    "Verdict",
    "WatchStep",
    "classify_outage",
    "dead_message",
    "notification_route",
    "recovered_message",
]
