"""Ask the process holding our port whether it is a live My Claude Code (7.70.0).

The rule this implements is the user's decision R5 (2026-10-01): *a server that
starts and finds a live My Claude Code server ANSWERING on its port backs off
with a clear message instead of killing it; the kill is kept only for a holder
that does not answer.* It amends ``SERVER_PORT_TAKEOVER=always`` from "stop
whatever holds the port" to "replace a holder that is not answering".

Why it is needed, in one incident: on 2026-09-28 at 14:41 a newly started
server stopped the user's working server because that server's event loop was
busy for longer than one late ``/health`` answer, the process lookup timed out,
and ``always`` reads "could not identify" as "kill". The holder was alive and
serving; it simply answered late.

**Identity is still decided by the process** (``cli/port_takeover.py``); the
answer here is asked only to decide *back off*, never to decide *kill*. So a
stranger that answers is treated exactly as before, and a silent holder of any
kind is handled exactly as before -- only an answer that is unmistakably My
Claude Code's (its marker headers, its pid header, or its healthy body) can
stop a takeover, and stopping a takeover kills nothing.

The patience is the user's own, not a new number: the desktop's escalating
probe ladder ``DESKTOP_HEALTH_PROBE_TIMEOUTS`` (5, 10, 15 s) for
``DESKTOP_HEALTH_FAILURE_THRESHOLD`` (3) tries -- at most 30 s, stopping at the
first answer. A holder that says it is shutting down is given the server's own
stop budget (``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` + the fixed teardown margin +
the watchdog's beat, 24 s by default) to leave, and is never killed while it
says so.

Everything here is pure or takes its I/O as an argument, so the decision table
is tested without a process; :func:`ask_health` is the one real request, a
plain ``http.client`` GET that never goes through a proxy.
"""

import http.client
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from .loop_health import BUSY_MARKER_HEADER, BUSY_MARKER_VALUE
from .server_pid import SERVER_PID_HEADER, pid_from_headers
from .startup_state import STARTING_MARKER_HEADER, STARTING_MARKER_VALUE
from .stop_deadline import (
    HARD_EXIT_GRACE_SECONDS,
    SHUTDOWN_MARKER_HEADER,
    SHUTDOWN_MARKER_VALUE,
    STOP_TEARDOWN_MARGIN_SECONDS,
    clamp_stop_budget,
)

#: The path asked. The same one every probe in the product asks.
HEALTH_PATH = "/health"

#: Bytes of a body kept for classification. MCC's healthy body is 20 bytes and
#: its busy body a couple of hundred; anything much larger is not MCC.
_MAX_BODY_BYTES = 1024

#: Every ``x-mcc-*`` header MCC's ``/health`` can carry starts with this.
_MCC_HEADER_PREFIX = "x-mcc-"


class HolderAnswer(StrEnum):
    """What the holder said, reduced to what a start may do about it."""

    #: 2xx from My Claude Code, loop keeping up.
    HEALTHY = "healthy"
    #: 2xx from My Claude Code with ``x-mcc-busy``: alive, working.
    BUSY = "busy"
    #: 503 + ``x-mcc-starting``: My Claude Code, bound and coming up.
    STARTING = "starting"
    #: 503 + ``x-mcc-shutdown``: My Claude Code, leaving on purpose.
    DRAINING = "draining"
    #: Something answered, and it was not My Claude Code.
    NOT_MCC = "not-mcc"
    #: Nothing answered within the whole ladder.
    SILENT = "silent"
    #: The port came free while we were asking.
    FREE = "free"


#: Answers from a live My Claude Code that must never be killed: the start
#: backs off instead.
ANSWERING: frozenset[HolderAnswer] = frozenset(
    {HolderAnswer.HEALTHY, HolderAnswer.BUSY, HolderAnswer.STARTING}
)

#: Every answer that is My Claude Code's, leaving included.
MCC_ANSWERS: frozenset[HolderAnswer] = ANSWERING | {HolderAnswer.DRAINING}


def _lowered(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).strip().lower(): str(value) for name, value in headers.items()}


def _is_healthy_body(body: bytes | str) -> bool:
    """Whether ``body`` is MCC's ``{"status": "healthy", ...}`` document.

    Recognises a server from before ``x-mcc-pid`` existed (7.69.x and older),
    whose healthy answer carries no MCC header at all.
    """

    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    try:
        document = json.loads(text)
    except ValueError:
        return False
    return isinstance(document, dict) and document.get("status") == "healthy"


def classify_health_answer(
    status: int, headers: Mapping[str, str], body: bytes | str = b""
) -> HolderAnswer:
    """Reduce one ``/health`` answer to a :class:`HolderAnswer`.

    The shutdown marker is checked before the starting one, as everywhere else:
    a server asked to stop during a slow start answers "going", and a caller
    told "coming" would wait for something that is leaving.
    """

    lowered = _lowered(headers)
    if status == 503:
        if lowered.get(SHUTDOWN_MARKER_HEADER) == SHUTDOWN_MARKER_VALUE:
            return HolderAnswer.DRAINING
        if lowered.get(STARTING_MARKER_HEADER) == STARTING_MARKER_VALUE:
            return HolderAnswer.STARTING
        return HolderAnswer.NOT_MCC
    if 200 <= status < 300:
        if lowered.get(BUSY_MARKER_HEADER) == BUSY_MARKER_VALUE:
            return HolderAnswer.BUSY
        if SERVER_PID_HEADER in lowered or any(
            name.startswith(_MCC_HEADER_PREFIX) for name in lowered
        ):
            return HolderAnswer.HEALTHY
        if _is_healthy_body(body):
            return HolderAnswer.HEALTHY
    return HolderAnswer.NOT_MCC


@dataclass(frozen=True, slots=True)
class HolderProbe:
    """The outcome of asking the holder, at most once per rung of the ladder."""

    answer: HolderAnswer
    #: The pid the answer named (``x-mcc-pid``), when it named one.
    pid: int | None = None
    #: Seconds from the first try to the answer, or to giving up.
    seconds: float = 0.0
    #: How many tries were made.
    tries: int = 0


def parse_probe_ladder(raw: str | None) -> list[float]:
    """``DESKTOP_HEALTH_PROBE_TIMEOUTS`` as seconds, junk dropped.

    The same rules ``mcc-desktop --print-status`` has always applied to the
    setting (it delegates here): comma-separated, anything unparseable or not
    positive is dropped rather than raised, and an empty result means "no
    ladder".
    """

    timeouts: list[float] = []
    for piece in str(raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = float(piece)
        except ValueError:
            continue
        if value > 0.0:
            timeouts.append(value)
    return timeouts


def probe_timeouts(
    ladder: Sequence[float], *, tries: int, fallback: float
) -> list[float]:
    """One timeout per try: the ladder's rungs, the last one repeating.

    ``tries`` is ``DESKTOP_HEALTH_FAILURE_THRESHOLD``; an empty ladder uses
    ``fallback`` (``DESKTOP_HEALTH_PROBE_TIMEOUT``) for every try, exactly as
    the desktop app does.
    """

    count = max(1, int(tries))
    rungs = [float(value) for value in ladder if float(value) > 0.0]
    if not rungs:
        rungs = [max(0.1, float(fallback))]
    return [rungs[min(index, len(rungs) - 1)] for index in range(count)]


def stop_wait_seconds(graceful_seconds: float) -> float:
    """Seconds a holder that is shutting down is given to leave on its own.

    The same sum the tray, the installer and ``--print-status`` use: the
    configured graceful budget, the server's fixed teardown margin, and the
    beat its own watchdog allows before it hard-exits. Waiting less would
    abandon a server a few seconds before its own watchdog ends it.
    """

    return (
        clamp_stop_budget(graceful_seconds)
        + STOP_TEARDOWN_MARGIN_SECONDS
        + HARD_EXIT_GRACE_SECONDS
    )


#: One answer: status, lower-cased headers, the first kilobyte of the body.
type HealthReply = tuple[int, dict[str, str], bytes]


def ask_health(root_url: str, timeout: float) -> HealthReply | None:
    """``GET /health`` once, bounded by ``timeout``. ``None`` when nothing answered.

    ``http.client`` rather than ``urllib``: a loopback probe must never be
    routed through ``HTTP_PROXY``, and this has no proxy logic to disable.
    ``Connection: close`` so the holder is left with no idle socket of ours.
    """

    parts = urlsplit(root_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", HEALTH_PATH, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(_MAX_BODY_BYTES)
        headers = {name.lower(): value for name, value in response.getheaders()}
        return int(response.status), headers, body
    except OSError, http.client.HTTPException, ValueError:
        return None
    finally:
        connection.close()


def probe_holder(
    root_url: str,
    timeouts: Sequence[float],
    *,
    port_is_free: Callable[[], bool],
    ask: Callable[[str, float], HealthReply | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> HolderProbe:
    """Ask the holder once per timeout, stopping at the first answer.

    Between tries the port itself is asked whether it is free: a holder that
    exits while we wait is not something to take the port from. ``ask``
    defaults to :func:`ask_health`, looked up at call time.
    """

    request = ask if ask is not None else ask_health
    started = clock()
    tries = 0
    for timeout in timeouts:
        tries += 1
        reply = request(root_url, timeout)
        if reply is not None:
            status, headers, body = reply
            return HolderProbe(
                answer=classify_health_answer(status, headers, body),
                pid=pid_from_headers(headers),
                seconds=clock() - started,
                tries=tries,
            )
        if port_is_free():
            return HolderProbe(
                HolderAnswer.FREE, seconds=clock() - started, tries=tries
            )
    return HolderProbe(HolderAnswer.SILENT, seconds=clock() - started, tries=tries)


class PortDecision(StrEnum):
    """What the start does next."""

    #: Nothing holds the port any more: bind.
    FREE = "free"
    #: A live My Claude Code answers: exit, and stop nothing.
    BACK_OFF = "back-off"
    #: Silent, or not My Claude Code: the existing takeover rules decide,
    #: exactly as before 7.70.0.
    TAKE = "take"


@dataclass(frozen=True, slots=True)
class HolderSettlement:
    """The decision, and the answer it was made on."""

    decision: PortDecision
    probe: HolderProbe
    #: Whether a holder that said it was shutting down was waited for.
    waited_for_drain: bool = False


def settle_port_holder(
    *,
    probe: Callable[[], HolderProbe],
    wait_for_free: Callable[[float, HolderProbe], bool],
    drain_wait_seconds: float,
) -> HolderSettlement:
    """Decide what a start does about a holder that is still there.

    The whole table:

    * the port came free while asking -> ``FREE``;
    * healthy, busy or starting -> ``BACK_OFF``;
    * shutting down -> wait up to ``drain_wait_seconds`` for the port; free ->
      ``FREE``; still held -> ask once more: any My Claude Code answer ->
      ``BACK_OFF`` (it is still there and still says so; a server is never
      killed while it answers), free -> ``FREE``, otherwise ``TAKE``;
    * silent, or not My Claude Code -> ``TAKE`` (today's rules, unchanged).
    """

    first = probe()
    if first.answer is HolderAnswer.FREE:
        return HolderSettlement(PortDecision.FREE, first)
    if first.answer in ANSWERING:
        return HolderSettlement(PortDecision.BACK_OFF, first)
    if first.answer is HolderAnswer.DRAINING:
        if wait_for_free(drain_wait_seconds, first):
            return HolderSettlement(PortDecision.FREE, first, waited_for_drain=True)
        second = probe()
        if second.answer is HolderAnswer.FREE:
            return HolderSettlement(PortDecision.FREE, second, waited_for_drain=True)
        if second.answer in MCC_ANSWERS:
            return HolderSettlement(
                PortDecision.BACK_OFF, second, waited_for_drain=True
            )
        return HolderSettlement(PortDecision.TAKE, second, waited_for_drain=True)
    return HolderSettlement(PortDecision.TAKE, first)


_ANSWER_WORDS: dict[HolderAnswer, str] = {
    HolderAnswer.HEALTHY: "answered /health",
    HolderAnswer.BUSY: "answered /health as busy",
    HolderAnswer.STARTING: "answered /health that it is still starting",
    HolderAnswer.DRAINING: "is still shutting down",
}


def back_off_message(
    *,
    port: int,
    settlement: HolderSettlement,
    pid: int | None,
    windows: bool,
    drain_wait_seconds: float | None = None,
) -> str:
    """The one sentence a start that backs off says, to the log and the console."""

    probe = settlement.probe
    who = f"pid {pid}" if pid is not None else "pid unknown"
    said = _ANSWER_WORDS.get(probe.answer, "answered /health")
    if settlement.waited_for_drain and drain_wait_seconds is not None:
        timing = f"it said it was shutting down and was still there after {drain_wait_seconds:.0f} s"
    else:
        timing = f"it {said} in {probe.seconds:.1f} s"
    end = (
        f"end process {pid} in Task Manager"
        if windows and pid is not None
        else (f"kill {pid}" if pid is not None else "stop that process")
    )
    return (
        f"Port {port} is already served by My Claude Code ({who}; {timing}). "
        "This start was abandoned and nothing was stopped. To replace that "
        "server, stop it first -- Ctrl+C in the window it runs in, Quit in the "
        f"desktop app that started it, or {end} -- then start mcc-server again."
    )


def drain_wait_message(*, port: int, pid: int | None, seconds: float) -> str:
    """What a start says while it waits for a holder that is shutting down."""

    who = f"pid {pid}" if pid is not None else "pid unknown"
    return (
        f"Port {port} is held by My Claude Code ({who}), which is shutting "
        f"down. Waiting up to {seconds:.0f} s for it to finish and exit; it "
        "is not stopped."
    )


def silent_holder_message(*, port: int, probe: HolderProbe) -> str:
    """The log line before today's takeover rules decide a holder's fate."""

    if probe.answer is HolderAnswer.NOT_MCC:
        return (
            f"Port {port}: the holder answered /health in {probe.seconds:.1f} s, "
            "and not as My Claude Code; SERVER_PORT_TAKEOVER decides."
        )
    return (
        f"Port {port}: the holder did not answer /health in {probe.tries} "
        f"tries ({probe.seconds:.1f} s); SERVER_PORT_TAKEOVER decides."
    )


__all__ = [
    "ANSWERING",
    "HEALTH_PATH",
    "MCC_ANSWERS",
    "HealthReply",
    "HolderAnswer",
    "HolderProbe",
    "HolderSettlement",
    "PortDecision",
    "ask_health",
    "back_off_message",
    "classify_health_answer",
    "drain_wait_message",
    "parse_probe_ladder",
    "probe_holder",
    "probe_timeouts",
    "settle_port_holder",
    "silent_holder_message",
    "stop_wait_seconds",
]
