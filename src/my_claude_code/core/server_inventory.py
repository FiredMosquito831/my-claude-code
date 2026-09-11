"""Which other My Claude Code servers are running, and which of them are dead.

On 2026-09-10 two ``mcc-server`` launches on this developer's machine stopped
owning a listening socket and kept running anyway. Twenty-five hours later they
were still there: still heartbeating into ``server_sessions`` every thirty
seconds, still holding ``~/.local/bin/mcc-server.exe`` and the uv tool
environment's ``python.exe`` open as loaded images, and therefore still making
every ``uv tool install`` fail its first attempt with ``os error 32``. Nothing
in the product could see them. 6.59.0's ``SERVER_PORT_TAKEOVER`` reaps the
holder *of the configured port*, and these held no port at all.

The obvious fix -- "stop any MCC server that owns no socket" -- is wrong, and
the machine that produced the evidence is the machine that proves it wrong:
those two processes had live outbound HTTPS connections to providers and over
an hour of CPU time apiece, because the user was running agents against them.
**A process that owns no listening socket in one scan is not abandoned.** It
may be starting; it may be draining; it may be serving work that was accepted
before the socket closed; the scan itself may have failed. So the default
behaviour of everything in this module is to *say what it found* and stop
there.

Four statuses, and only one of them is ever actionable:

``serving``
    Some member of the chain owns a listening socket. A second MCC instance on
    another port is entirely legitimate. Never touched.
``live``
    Its session heartbeat is fresh. This is a running server, whatever the
    socket scan said. Never touched.
``stale``
    The only class that can be proven dead, by one of exactly two arguments:
    its heartbeat has gone quiet *and* the port it recorded is now served by a
    different MCC server (it has been superseded, and cannot be serving anyone
    on the address it claims); or it is a launcher trampoline whose entire
    descendant tree is gone (a supervisor with nothing left to supervise, which
    holds a file open for no reason at all).
``unknown``
    Anything else, including every session row written before 6.72.2, which
    recorded no address and therefore cannot support either argument. Never
    touched.

Even ``stale`` is only reported unless the operator has opted in. Detection is
a fact; stopping somebody's server is a decision, and it is not this module's
to take by default.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from my_claude_code.core.mcc_processes import (
    ProcessChain,
    ProcessFacts,
    listening_pids,
    mcc_server_chains,
    stop_chain,
)
from my_claude_code.core.request_log import ServerSession, read_server_sessions

#: How quiet a session's heartbeat must go before the word "stale" is even
#: available. The writer touches the row every 30 seconds, so anything on this
#: scale is many missed beats, not a slow disk.
DEFAULT_STALE_AFTER_SECONDS = 900.0

#: A chain younger than this is never called stale on the strength of a missing
#: child, because a launch in progress looks exactly like a launch that failed.
YOUNG_CHAIN_SECONDS = 120.0

STATUS_SERVING = "serving"
STATUS_LIVE = "live"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"

#: The only status any caller may act on, and only when told to.
ACTIONABLE_STATUSES = frozenset({STATUS_STALE})


@dataclass(frozen=True, slots=True)
class ServerObservation:
    """One MCC server launch as this process can see it from the outside."""

    #: Every pid in the launch, outermost first.
    pids: tuple[int, ...]
    #: The pid that matched a session row, when one did.
    session_pid: int | None
    session_id: int | None
    host: str | None
    port: int | None
    started_at: float | None
    last_seen_at: float | None
    status: str
    #: Why the status is what it is, in one clause, for the log line.
    reason: str
    #: The executables the launch keeps open; an install cannot replace these.
    holds: tuple[str, ...]

    @property
    def is_actionable(self) -> bool:
        return self.status in ACTIONABLE_STATUSES

    @property
    def heartbeat_age_seconds(self) -> float | None:
        if self.last_seen_at is None:
            return None
        return max(0.0, time.time() - self.last_seen_at)

    def describe(self) -> str:
        """One line naming everything an operator needs to decide about it."""

        where = (
            f"{self.host}:{self.port}"
            if self.host and self.port
            else (f"port {self.port}" if self.port else "no recorded address")
        )
        session = f"session {self.session_id}" if self.session_id else "no session row"
        started = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at))
            if self.started_at
            else "unknown"
        )
        age = self.heartbeat_age_seconds
        beat = "never" if age is None else f"{age:.0f}s ago"
        pids = ", ".join(str(pid) for pid in self.pids)
        line = (
            f"{self.status}: pid {pids} ({session}, {where}, started {started}, "
            f"last heartbeat {beat}) -- {self.reason}"
        )
        if self.holds:
            line += f"; holds {', '.join(self.holds)}"
        return line

    def as_status_entry(self) -> dict[str, object]:
        """The shape the desktop status document publishes. Plain types only."""

        return {
            "pids": list(self.pids),
            "session_id": self.session_id,
            "host": self.host,
            "port": self.port,
            "started_at": self.started_at,
            "last_seen_at": self.last_seen_at,
            "status": self.status,
            "reason": self.reason,
            "holds": list(self.holds),
        }


def _session_for(
    chain: ProcessChain, sessions: list[ServerSession]
) -> ServerSession | None:
    """The most recent session row written by any member of ``chain``."""

    pids = set(chain.pids)
    matches = [
        session
        for session in sessions
        if session.pid is not None and session.pid in pids
    ]
    if not matches:
        return None
    return max(matches, key=lambda session: session.last_seen_at)


def _chain_started_at(chain: ProcessChain) -> float | None:
    stamps = [
        member.started_at for member in chain.members if member.started_at is not None
    ]
    return min(stamps) if stamps else None


def observe_servers(
    *,
    request_log_path: Path | str,
    self_pid: int | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    processes: list[ProcessFacts] | None = None,
    listening: frozenset[int] | None = None,
    sessions: list[ServerSession] | None = None,
    now: float | None = None,
) -> list[ServerObservation]:
    """Describe every MCC server launch on this machine except this process's.

    Every input can be supplied, which is how the tests drive this against a
    fabricated process table, a fabricated set of listening pids and a
    fabricated session list without going near a real machine.
    """

    moment = time.time() if now is None else now
    chains = mcc_server_chains(processes)
    sockets = listening_pids() if listening is None else listening
    rows = (
        read_server_sessions(request_log_path) if sessions is None else list(sessions)
    )

    # Which ports are currently served, and by which launch: the evidence for
    # "this session has been superseded" and the only thing that can supply it.
    served_ports: dict[int, tuple[int, ...]] = {}
    for chain in chains:
        if not sockets.intersection(chain.pids):
            continue
        session = _session_for(chain, rows)
        if session is not None and session.port is not None:
            served_ports[session.port] = chain.pids

    observations: list[ServerObservation] = []
    for chain in chains:
        if self_pid is not None and self_pid in chain.pids:
            continue
        session = _session_for(chain, rows)
        holds = chain.holds()
        serving = bool(sockets.intersection(chain.pids))
        started_at = _chain_started_at(chain)
        if session is not None and session.started_at:
            started_at = session.started_at

        status = STATUS_UNKNOWN
        reason = "no session row and no listening socket; MCC cannot tell what it is"
        if serving:
            status = STATUS_SERVING
            reason = "owns a listening socket; this is another running server"
        elif session is not None:
            age = max(0.0, moment - session.last_seen_at)
            if age <= stale_after_seconds:
                status = STATUS_LIVE
                reason = (
                    f"heartbeat {age:.0f}s old; a running server, not "
                    "necessarily reachable, but running"
                )
            elif session.port is not None and session.port in served_ports:
                status = STATUS_STALE
                owner = ", ".join(str(pid) for pid in served_ports[session.port])
                reason = (
                    f"heartbeat {age:.0f}s old and port {session.port} is now "
                    f"served by pid {owner}; this launch has been superseded"
                )
            else:
                reason = (
                    f"heartbeat {age:.0f}s old but nothing else claims its "
                    "port; it may still be working, so it is left alone"
                )
        elif (
            len(chain.members) == 1
            and started_at is not None
            and moment - started_at > YOUNG_CHAIN_SECONDS
        ):
            status = STATUS_STALE
            reason = (
                "a launcher whose server process is gone; it supervises "
                "nothing and only holds its own executable open"
            )

        observations.append(
            ServerObservation(
                pids=chain.pids,
                session_pid=session.pid if session else None,
                session_id=session.id if session else None,
                host=session.host if session else None,
                port=session.port if session else None,
                started_at=started_at,
                last_seen_at=session.last_seen_at if session else None,
                status=status,
                reason=reason,
                holds=holds,
            )
        )
    return observations


#: How long a written survey is worth showing. Past this it is history, not
#: news, and a window that quoted it would be describing yesterday's machine.
SURVEY_FRESH_SECONDS = 3600.0


def write_survey(path: Path | str, observations: list[ServerObservation]) -> None:
    """Record the survey so a later process can read it without re-scanning.

    Enumerating every process costs about two seconds on Windows. The desktop
    shell asks for the status document every ten seconds, so having
    ``--print-status`` run its own scan would put a permanent two-second tax on
    a document whose whole job is to be quick. It reads this instead.
    """

    target = Path(path)
    payload = {
        "at": time.time(),
        "servers": [item.as_status_entry() for item in observations],
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside and rename: a reader that catches this mid-write would
        # otherwise get half a document, and the reader is a status probe that
        # has no way to ask again.
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        logger.debug("Could not record the server survey at {}: {}", target, exc)


def read_survey(path: Path | str) -> list[dict[str, object]]:
    """The last recorded survey, or an empty list if there is no fresh one."""

    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        payload = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    written_at = payload.get("at")
    if not isinstance(written_at, int | float):
        return []
    if time.time() - float(written_at) > SURVEY_FRESH_SECONDS:
        return []
    servers = payload.get("servers")
    if not isinstance(servers, list):
        return []
    return [entry for entry in servers if isinstance(entry, dict)]


def report_servers(observations: list[ServerObservation], *, context: str) -> None:
    """Write one log line per other server found. Stops nothing, ever."""

    if not observations:
        return
    logger.info(
        "{context}: {count} other My Claude Code server "
        "{noun} running on this machine.",
        context=context,
        count=len(observations),
        noun="process is" if len(observations) == 1 else "processes are",
    )
    for observation in observations:
        if observation.is_actionable:
            logger.warning("  {line}", line=observation.describe())
        else:
            logger.info("  {line}", line=observation.describe())


def stop_stale_servers(
    observations: list[ServerObservation],
    *,
    processes: list[ProcessFacts] | None = None,
) -> list[ServerObservation]:
    """Stop the ``stale`` observations by exact pid. Returns what was stopped.

    Called only when the operator has set the action setting to ``stop``.
    Nothing but :data:`ACTIONABLE_STATUSES` is ever passed to
    :func:`~my_claude_code.core.mcc_processes.stop_chain`, and the chain is
    re-derived from a process table so the pids signalled are the ones the
    observation actually named.
    """

    actionable = [item for item in observations if item.is_actionable]
    if not actionable:
        return []
    wanted = {pid for item in actionable for pid in item.pids}
    chains = {
        chain.root.pid: chain
        for chain in mcc_server_chains(processes)
        if wanted.intersection(chain.pids)
    }
    stopped: list[ServerObservation] = []
    for item in actionable:
        chain = next(
            (
                candidate
                for candidate in chains.values()
                if set(candidate.pids) == set(item.pids)
            ),
            None,
        )
        if chain is None:
            # The process table moved under us between observing and acting.
            # Signalling the pids anyway would be signalling a pid that may
            # now belong to somebody else entirely.
            logger.info(
                "Stale server {pids} is already gone; nothing to stop.",
                pids=", ".join(str(pid) for pid in item.pids),
            )
            continue
        logger.warning("Stopping a stale server -- {line}", line=item.describe())
        if stop_chain(chain):
            stopped.append(item)
    return stopped
