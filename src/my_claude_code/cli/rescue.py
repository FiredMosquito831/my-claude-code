"""``mcc-desktop --rescue``: replace a dead server, and only a dead one (7.71.0).

The desktop app asks for this only after the operating system has said that
nothing listens on the configured port (rescue spec, decision table rows 8, 9
and 11). It is the second of three layers that keep a merely *slow* server from
ever being touched (spec section 2.3):

1. the window asks only on a fresh "nothing listens" from the OS;
2. **this command looks again, from fresh OS tables, and refuses if anything
   holds the port, or if it cannot look at all**;
3. the server the window then starts carries ``--no-port-takeover``.

What it may touch is decision R1, exactly: the "old servers" are My Claude Code
server launches (structurally, ``core/mcc_processes.py`` -- never a launcher,
never by name, never a substring) that are tied to **this port and this
configuration folder** -- a launch whose latest row in THIS configuration
folder's ``server_sessions`` recorded this port (or that row is this folder's
and the launch is the very server the window last heard on this port), or the
exact child the window itself started. A launch on another port, a launch of
another configuration folder, a launch that still holds a listening socket
anywhere, and anything that is not My Claude Code are never candidates; the
ones the window named are reported as left alone, with the reason.

The sequence, every bound an existing number (spec section 2.5):

* wait up to ``server_stop_wait_seconds`` (``SERVER_GRACEFUL_SHUTDOWN_SECONDS``
  plus the server's own stop margins, 24 s at the default) for every candidate
  to finish and exit by itself -- a 7.69.2+ server that lost its listener does
  exactly that -- aborting if anything binds the port meanwhile;
* stop, by exact pid and innermost first, only what is still there, re-derived
  from a fresh process table, and only members that still own no socket;
* wait up to ``DESKTOP_BUSY_GRACE_SECONDS`` (15 s) for the port to be free.

It prints one JSON document and writes one WARNING line per action to the
server log. It never shows a notification: the desktop app asked, and the
desktop app says it (user answer 1).

It never calls ``os.kill(pid, 0)``: on Windows that is ``TerminateProcess``.
Liveness here is ``OpenProcess`` + ``GetExitCodeProcess`` on Windows and the
existing POSIX check elsewhere.

**The same rule at every server's start (7.72.0).** Decision 12 ("on upgrade,
tell old MCC servers to exit") and answer 4 ("hand-started servers also clear
dead servers of the same port and config folder") put the same scope and the
same stop rules into the start of every server that may take its port:
:func:`plan_old_server_cleanup` picks the candidates with the one scope
function (:func:`~my_claude_code.core.server_inventory.old_server_scope`), and
:func:`run_old_server_cleanup` waits the same budget and stops by the same
exact-pid path as the rescue. The installer starts the new server after an
update, so this is also what the update does to old servers -- with no change
to the installer. A server started with ``--no-port-takeover`` (every server the
desktop app starts) does none of it: there the rescue above is the only thing
that stops an old server.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from my_claude_code.core.mcc_processes import (
    ProcessChain,
    ProcessFacts,
    mcc_server_chains,
    process_is_alive,
)
from my_claude_code.core.request_log import SESSION_VERSION_SINCE, ServerSession
from my_claude_code.core.server_inventory import (
    SCOPE_SELF,
    SCOPE_SERVING,
    old_server_scope,
)

#: The reasons the window may give, as it spells them.
RESCUE_REASONS = ("process-gone", "listener-lost", "never-bound")

#: The ``outcome`` values. ``port_free`` is the only one after which the
#: window starts a server.
OUTCOME_PORT_FREE = "port_free"
OUTCOME_REFUSED = "refused"

#: How often the waits look again. A cadence, not a budget.
POLL_SECONDS = 0.5

REPORT_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class RescueRequest:
    """What the window asked for, and the bounds it is held to."""

    host: str
    port: int
    reason: str
    known_pid: int | None = None
    child_pid: int | None = None
    #: Seconds each old server is given to finish and exit by itself.
    stop_wait_seconds: float = 24.0
    #: Seconds the port is given to come free after the stops.
    port_wait_seconds: float = 15.0


@dataclass(slots=True)
class RescueWorld:
    """Everything the rescue reads or does to the machine, injectable.

    The tests supply a fake process table, fake sockets, fake sessions, a fake
    clock and a spy for ``stop_chain`` that fails the test if anything out of
    scope is ever asked to stop.
    """

    #: Whether the OS lets a socket bind the port right now.
    port_is_free: Callable[[], bool]
    #: Every ``(port, pid)`` a listening TCP socket reports; empty = could not look.
    listening: Callable[[], frozenset[tuple[int, int]]]
    #: The whole process table; empty = could not look.
    processes: Callable[[], list[ProcessFacts]]
    #: THIS configuration folder's recorded server runs, read-only.
    sessions: Callable[[], list[ServerSession]]
    #: Whether one exact pid is alive. Never a signal.
    alive: Callable[[int], bool]
    #: Stop one chain by exact pid, innermost first. Returns whether all went.
    stop_chain: Callable[[ProcessChain], bool]
    #: Whether an update helper is replacing the environment right now.
    updating: Callable[[], bool]
    #: One WARNING line to the server log.
    log: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    #: Pids that are this command's own (never candidates).
    self_pids: frozenset[int] = field(default_factory=frozenset)


@dataclass(slots=True)
class _Candidate:
    chain: ProcessChain
    why: str
    exited_by_itself: bool = False
    stopped: bool = False
    #: It opened a listening socket again before it had to be stopped, so it
    #: was kept running and is reported under ``left_alone`` instead.
    kept: bool = False
    note: str = ""
    #: Its own row in this configuration folder, when it has one.
    session: ServerSession | None = None


def _pids(chain: ProcessChain) -> list[int]:
    return list(chain.pids)


def _scope(
    request: RescueRequest,
    chains: list[ProcessChain],
    sessions: list[ServerSession],
    listening_pids: frozenset[int],
    self_pids: frozenset[int],
) -> tuple[list[_Candidate], list[dict[str, Any]]]:
    """Decision R1 for the window: the candidates, and what was left alone.

    The scope itself is :func:`old_server_scope`, the one function every caller
    that may stop an old server uses. The window adds exactly two things it
    alone can know: the exact child it started, and the server it last heard
    on this port (a 7.69.2+ server that lost its listener stops claiming the
    port on its row while it closes).
    """

    candidates: list[_Candidate] = []
    left_alone: list[dict[str, Any]] = []
    for chain in chains:
        members = set(chain.pids)
        if members & self_pids:
            continue
        named = (request.known_pid in members) or (request.child_pid in members)
        verdict = old_server_scope(
            chain,
            port=request.port,
            sessions=sessions,
            listening_pids=listening_pids,
            self_pids=self_pids,
        )
        session = verdict.session
        if verdict.kind == SCOPE_SERVING:
            # Owns a listening socket somewhere: a running server. Never a
            # candidate, whatever else is true of it.
            if named or (session is not None and session.port == request.port):
                left_alone.append({"pids": _pids(chain), "why": verdict.why})
            continue
        if request.child_pid is not None and request.child_pid in members:
            candidates.append(
                _Candidate(chain, "the server the desktop app itself started")
            )
            continue
        if verdict.is_old:
            candidates.append(_Candidate(chain, verdict.why, session=session))
            continue
        if (
            session is not None
            and session.port is None
            and request.known_pid is not None
            and request.known_pid in members
        ):
            candidates.append(
                _Candidate(
                    chain,
                    f"the server the desktop app last heard on port {request.port}, "
                    "of this configuration folder, now closing",
                    session=session,
                )
            )
            continue
        if named:
            left_alone.append(
                {
                    "pids": _pids(chain),
                    "why": f"not tied to port {request.port} and this "
                    f"configuration folder ({verdict.why})",
                }
            )
    return candidates, left_alone


def _wait_for_exits(
    candidates: list[_Candidate],
    *,
    alive: Callable[[int], bool],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    budget: float,
    abort: Callable[[], str | None],
) -> tuple[list[_Candidate], str | None]:
    """Give every candidate up to ``budget`` seconds to exit by itself.

    Returns what is still alive, and the sentence ``abort`` gave if it ended
    the wait early. Bounded by ``budget`` on ``clock`` whatever the candidates
    do; looks every :data:`POLL_SECONDS`.
    """

    deadline = clock() + max(0.0, budget)
    remaining = list(candidates)
    while True:
        remaining = [
            item for item in remaining if any(alive(pid) for pid in item.chain.pids)
        ]
        if not remaining:
            return remaining, None
        reason = abort()
        if reason is not None:
            return remaining, reason
        if clock() >= deadline:
            return remaining, None
        sleep(POLL_SECONDS)


def _stop_left_over(
    remaining: list[_Candidate],
    fresh_processes: list[ProcessFacts],
    fresh_endpoints: frozenset[tuple[int, int]],
    *,
    stop_chain: Callable[[ProcessChain], bool],
    announce: Callable[[_Candidate, tuple[ProcessFacts, ...]], None] | None = None,
    may_continue: Callable[[], bool] = lambda: True,
) -> None:
    """Stop, by exact pid and innermost first, what is left and still dead.

    Each candidate is re-derived from a FRESH process table, and only the pids
    observed at the first look -- with the same start time, so a pid handed to
    another process since is never one of them -- are stopped. A candidate any
    of whose processes now owns a listening socket is kept running.
    """

    fresh_listening = frozenset(pid for _port, pid in fresh_endpoints)
    fresh_chains = mcc_server_chains(fresh_processes)
    for item in remaining:
        observed = {member.pid: member.started_at for member in item.chain.members}
        fresh = next(
            (
                chain
                for chain in fresh_chains
                if chain.root.pid == item.chain.root.pid
                or set(observed) & set(chain.pids)
            ),
            None,
        )
        if fresh is None:
            item.exited_by_itself = True
            item.note = "gone before it had to be stopped"
            continue
        # Only pids observed at the start -- never a pid that appeared since,
        # which may already belong to somebody else.
        members = tuple(
            member
            for member in fresh.members
            if member.pid in observed and observed[member.pid] == member.started_at
        )
        if not members:
            item.exited_by_itself = True
            item.note = "gone before it had to be stopped"
            continue
        if fresh_listening & {member.pid for member in members}:
            item.kept = True
            item.note = "it opened a listening socket again, so it was left running"
            continue
        if not may_continue():
            item.note = "this server began stopping, so it was left running"
            continue
        if announce is not None:
            announce(item, members)
        item.stopped = stop_chain(ProcessChain(root=members[0], members=members))
        if not item.stopped:
            item.note = "some of it could not be stopped"


def _holder_of(port: int, endpoints: frozenset[tuple[int, int]]) -> list[int]:
    return sorted({pid for listened, pid in endpoints if listened == port})


def _refused(
    request: RescueRequest,
    detail: str,
    world: RescueWorld,
    *,
    servers: list[_Candidate] | None = None,
    left_alone: list[dict[str, Any]] | None = None,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    world.log(
        f"Rescue of the server on port {request.port} did not go ahead: {detail}. "
        "Nothing more was stopped and nothing was started."
    )
    return _report(
        request,
        OUTCOME_REFUSED,
        detail,
        servers or [],
        left_alone or [],
        timings or {},
    )


def _report(
    request: RescueRequest,
    outcome: str,
    detail: str,
    servers: list[_Candidate],
    left_alone: list[dict[str, Any]],
    timings: dict[str, float],
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "outcome": outcome,
        "reason": request.reason,
        "host": request.host,
        "port": request.port,
        "known_pid": request.known_pid,
        "child_pid": request.child_pid,
        "servers": [
            {
                "pids": _pids(item.chain),
                "why": item.why,
                "exited_by_itself": item.exited_by_itself,
                "stopped": item.stopped,
                "note": item.note,
            }
            for item in servers
        ],
        "left_alone": left_alone,
        "stop_wait_seconds": request.stop_wait_seconds,
        "port_wait_seconds": request.port_wait_seconds,
        "timings": {name: round(value, 3) for name, value in timings.items()},
        "detail": detail,
    }


def _rebound(
    request: RescueRequest, world: RescueWorld, candidates: list[_Candidate]
) -> str | None:
    """Whether something has bound the port; the sentence if it has."""

    if world.port_is_free():
        return None
    holders = _holder_of(request.port, world.listening())
    ours = sorted(
        {pid for item in candidates for pid in item.chain.pids} & set(holders)
    )
    if ours:
        return (
            f"pid {', '.join(map(str, ours))} bound port {request.port} again, so it "
            "is working, not dead"
        )
    if holders:
        return f"port {request.port} was taken by pid {', '.join(map(str, holders))}"
    return f"the operating system would not bind port {request.port} any more"


def run_rescue(request: RescueRequest, world: RescueWorld) -> dict[str, Any]:
    """The whole sequence. Returns the report; prints and decides nothing else."""

    timings: dict[str, float] = {}
    started = world.clock()
    if request.reason not in RESCUE_REASONS:
        return _refused(request, f"unknown reason {request.reason!r}", world)
    if world.updating():
        return _refused(
            request, "an update is replacing My Claude Code right now", world
        )

    # -- 1. look again, from fresh tables ----------------------------------
    endpoints = world.listening()
    if not endpoints:
        # An empty enumeration is "could not look", never "nothing listens"
        # (core/mcc_processes.py, listening_endpoints).
        return _refused(request, "the listening sockets could not be listed", world)
    holders = _holder_of(request.port, endpoints)
    if holders:
        return _refused(
            request,
            f"port {request.port} is held by pid {', '.join(map(str, holders))}",
            world,
        )
    if not world.port_is_free():
        return _refused(
            request, f"the operating system would not bind port {request.port}", world
        )
    processes = world.processes()
    if not processes:
        return _refused(request, "the process table could not be read", world)
    sessions = world.sessions()
    listening_pids = frozenset(pid for _port, pid in endpoints)
    candidates, left_alone = _scope(
        request,
        mcc_server_chains(processes),
        sessions,
        listening_pids,
        world.self_pids,
    )
    timings["scan"] = world.clock() - started

    # -- 2. let each one finish and exit by itself ---------------------------
    wait_started = world.clock()
    remaining, rebound = _wait_for_exits(
        candidates,
        alive=world.alive,
        clock=world.clock,
        sleep=world.sleep,
        budget=request.stop_wait_seconds,
        abort=lambda: _rebound(request, world, candidates),
    )
    if rebound is not None:
        timings["wait"] = world.clock() - wait_started
        return _refused(
            request,
            rebound,
            world,
            servers=candidates,
            left_alone=left_alone,
            timings=timings,
        )
    for item in candidates:
        if item not in remaining:
            item.exited_by_itself = True
            world.log(
                f"Rescue of port {request.port}: pid "
                f"{', '.join(map(str, item.chain.pids))} ({item.why}) finished and "
                "exited by itself."
            )
    timings["wait"] = world.clock() - wait_started

    # -- 3. stop, by exact pid, what is left and still listener-less ---------
    stop_started = world.clock()
    if remaining:
        fresh_processes = world.processes()
        fresh_endpoints = world.listening()
        if not fresh_processes or not fresh_endpoints:
            return _refused(
                request,
                "the process table or the sockets could not be read again before "
                "stopping anything",
                world,
                servers=candidates,
                left_alone=left_alone,
                timings=timings,
            )
        if _holder_of(request.port, fresh_endpoints):
            rebound = _rebound(request, world, candidates) or (
                f"port {request.port} is held again"
            )
            return _refused(
                request,
                rebound,
                world,
                servers=candidates,
                left_alone=left_alone,
                timings=timings,
            )

        def announce(item: _Candidate, members: tuple[ProcessFacts, ...]) -> None:
            world.log(
                f"Rescue of port {request.port}: stopping pid "
                f"{', '.join(str(member.pid) for member in members)} ({item.why}); "
                f"it was given {request.stop_wait_seconds:.0f} s to finish and exit "
                f"by itself and did not. Reason: {request.reason}."
            )

        _stop_left_over(
            remaining,
            fresh_processes,
            fresh_endpoints,
            stop_chain=world.stop_chain,
            announce=announce,
        )
        left_alone.extend(
            {"pids": _pids(item.chain), "why": item.note}
            for item in remaining
            if item.kept
        )
    timings["stop"] = world.clock() - stop_started

    # -- 4. wait for the port -------------------------------------------------
    port_started = world.clock()
    port_deadline = port_started + max(0.0, request.port_wait_seconds)
    while not world.port_is_free():
        if world.clock() >= port_deadline:
            timings["port"] = world.clock() - port_started
            return _refused(
                request,
                f"port {request.port} was still not free after "
                f"{request.port_wait_seconds:.0f} s",
                world,
                servers=candidates,
                left_alone=left_alone,
                timings=timings,
            )
        world.sleep(POLL_SECONDS)
    timings["port"] = world.clock() - port_started
    timings["total"] = world.clock() - started

    servers = [item for item in candidates if not item.kept]
    world.log(
        f"Rescue of the server on port {request.port} ({request.reason}): the port "
        f"is free; {_summary(servers)} The desktop app starts a new server now."
    )
    return _report(request, OUTCOME_PORT_FREE, "", servers, left_alone, timings)


def _summary(servers: list[_Candidate]) -> str:
    if not servers:
        return "nothing needed stopping."
    parts = []
    for item in servers:
        pids = ", ".join(map(str, item.chain.pids))
        if item.exited_by_itself:
            parts.append(f"pid {pids} exited by itself")
        elif item.stopped:
            parts.append(f"pid {pids} was stopped by process id")
        else:
            parts.append(f"pid {pids} could not be fully stopped")
    return "; ".join(parts) + "."


# ------------------------------------------------ the same rule at a start


@dataclass(frozen=True, slots=True)
class CleanupRequest:
    """What a starting server cleans up after, and the bound it is held to."""

    port: int
    #: Seconds each old server is given to finish and exit by itself: the same
    #: ``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` + stop margins the rescue waits.
    stop_wait_seconds: float = 24.0
    #: This server's own version, for the line that names an old server's.
    version: str = ""


@dataclass(slots=True)
class CleanupWorld:
    """Everything the start-time cleanup reads or does to the machine.

    Injectable for the same reason :class:`RescueWorld` is; and the test suite
    replaces the real one for EVERY test (``tests/conftest.py``), because this
    is the one path in a server's own start that stops processes.
    """

    listening: Callable[[], frozenset[tuple[int, int]]]
    processes: Callable[[], list[ProcessFacts]]
    sessions: Callable[[], list[ServerSession]]
    alive: Callable[[int], bool]
    stop_chain: Callable[[ProcessChain], bool]
    #: One line to the server log, at INFO and at WARNING.
    info: Callable[[str], None]
    warning: Callable[[str], None]
    #: One line to this process's console, for the person who started it.
    console: Callable[[str], None]
    #: Whether this server has been asked to stop; nothing more is stopped then.
    stopping: Callable[[], bool]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(slots=True)
class CleanupPlan:
    """The candidates of one start, picked before anything waits or stops."""

    request: CleanupRequest
    candidates: list[_Candidate] = field(default_factory=list)
    #: Every OTHER server that owns no listening socket, with why it is not an
    #: old server of this port and folder: reported, never stopped.
    left_alone: list[dict[str, Any]] = field(default_factory=list)
    #: Why nothing was looked at (the OS could not be asked); empty otherwise.
    skipped: str = ""
    #: One human description per candidate, by root pid.
    labels: dict[int, str] = field(default_factory=dict)

    @property
    def planned_pids(self) -> frozenset[int]:
        return frozenset(pid for item in self.candidates for pid in item.chain.pids)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    parts = text.split("+", 1)[0].split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _version_words(session: ServerSession | None, current: str) -> str:
    """What is known about an old server's version, in words."""

    if session is None:
        return "version unknown"
    if session.version is None:
        return (
            f"version unknown, older than {SESSION_VERSION_SINCE}, the first "
            "version that records it"
        )
    if current and session.version == current:
        return f"version {session.version}, the same as this server"
    theirs = _version_tuple(session.version)
    ours = _version_tuple(current) if current else None
    if theirs is not None and ours is not None and theirs < ours:
        return f"version {session.version}, older than this server's {current}"
    return f"version {session.version}"


def _label(item: _Candidate, *, version: str, now: float) -> str:
    starts = [m.started_at for m in item.chain.members if m.started_at is not None]
    parts = [_version_words(item.session, version)]
    if starts:
        parts.append(
            "started " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(min(starts)))
        )
    if item.session is not None:
        age = max(0.0, now - item.session.last_seen_at)
        parts.append(f"last heartbeat {age:.0f} s ago")
    pids = ", ".join(map(str, item.chain.pids))
    return f"pid {pids} ({'; '.join(parts)})"


def plan_old_server_cleanup(
    request: CleanupRequest,
    *,
    processes: list[ProcessFacts],
    endpoints: frozenset[tuple[int, int]],
    sessions: list[ServerSession],
    self_pid: int,
    now: float | None = None,
) -> CleanupPlan:
    """Pick the old servers of ``request.port`` and this folder. Stops nothing.

    The scope is :func:`old_server_scope`, unchanged, plus the one fact a
    starting server alone can add: an old server started BEFORE this one.
    "Could not look" -- no sockets listed, no process table, no start time for
    this very server -- is never "nothing there": the plan is then empty and
    says why.
    """

    plan = CleanupPlan(request)
    if not endpoints:
        plan.skipped = "the listening sockets could not be listed"
        return plan
    if not processes:
        plan.skipped = "the process table could not be read"
        return plan
    chains = mcc_server_chains(processes)
    own_chain = next((chain for chain in chains if self_pid in chain.pids), None)
    own_members: tuple[ProcessFacts, ...] = (
        own_chain.members
        if own_chain is not None
        else tuple(item for item in processes if item.pid == self_pid)
    )
    own_starts = [m.started_at for m in own_members if m.started_at is not None]
    if not own_starts:
        plan.skipped = (
            "this server's own start time could not be read, so no other server "
            "can be shown to be older"
        )
        return plan
    self_pids = frozenset(m.pid for m in own_members) | {self_pid}
    listening_pids = frozenset(pid for _port, pid in endpoints)
    moment = time.time() if now is None else now
    for chain in chains:
        verdict = old_server_scope(
            chain,
            port=request.port,
            sessions=sessions,
            listening_pids=listening_pids,
            self_pids=self_pids,
            started_before=min(own_starts),
        )
        if verdict.is_old:
            item = _Candidate(chain, verdict.why, session=verdict.session)
            plan.candidates.append(item)
            plan.labels[chain.root.pid] = _label(
                item, version=request.version, now=moment
            )
        elif verdict.kind not in (SCOPE_SELF, SCOPE_SERVING):
            plan.left_alone.append(
                {"pids": _pids(chain), "kind": verdict.kind, "why": verdict.why}
            )
    return plan


def run_old_server_cleanup(plan: CleanupPlan, world: CleanupWorld) -> dict[str, Any]:
    """Wait for the old servers to exit by themselves, then stop what is left.

    Decision R2, exactly as the rescue does it: up to the stop budget for each
    to finish and exit by itself; then, from a fresh process table, stop by
    exact pid and innermost first only what is still there, still owns no
    listening socket and still has the start time it had. Every outcome is one
    line in the server log; what was found and what was stopped is also said on
    this process's console. Returns a report; decides nothing else.
    """

    request = plan.request
    port = request.port
    wait = f"{request.stop_wait_seconds:.0f} s"

    def label(item: _Candidate) -> str:
        return plan.labels.get(
            item.chain.root.pid, f"pid {', '.join(map(str, item.chain.pids))}"
        )

    def say(level: str, text: str, *, console: bool) -> None:
        (world.warning if level == "warning" else world.info)(text)
        if console:
            world.console(text)

    def report(aborted: str = "") -> dict[str, Any]:
        servers = [
            {
                "pids": _pids(item.chain),
                "description": label(item),
                "exited_by_itself": item.exited_by_itself,
                "stopped": item.stopped,
                "kept": item.kept,
                "note": item.note,
            }
            for item in plan.candidates
        ]
        gone = sorted(
            {
                pid
                for item in plan.candidates
                if item.exited_by_itself or item.stopped
                for pid in item.chain.pids
            }
        )
        return {
            "port": port,
            "skipped": plan.skipped,
            "aborted": aborted,
            "servers": servers,
            "left_alone": list(plan.left_alone),
            "gone_pids": gone,
        }

    if plan.skipped:
        say(
            "info",
            f"Old servers of port {port} were not looked for: {plan.skipped}. "
            "Nothing was stopped.",
            console=False,
        )
        return report()
    for entry in plan.left_alone:
        say(
            "info",
            f"Left alone: My Claude Code server pid "
            f"{', '.join(map(str, entry['pids']))} owns no listening socket, but "
            f"{entry['why']}. Only old servers of port {port} and this "
            "configuration folder are stopped when a server starts.",
            console=False,
        )
    if not plan.candidates:
        return report()

    count = len(plan.candidates)
    say(
        "info",
        f"Found {count} old My Claude Code server{'s' if count != 1 else ''} of "
        f"port {port} and this configuration folder with no listening socket: "
        f"{'; '.join(label(item) for item in plan.candidates)}. "
        f"{'Each is' if count != 1 else 'It is'} given up to {wait} to finish "
        "and exit by itself; whatever is still running then is stopped by "
        "process id.",
        console=True,
    )
    remaining, aborted = _wait_for_exits(
        plan.candidates,
        alive=world.alive,
        clock=world.clock,
        sleep=world.sleep,
        budget=request.stop_wait_seconds,
        abort=lambda: "this server began stopping" if world.stopping() else None,
    )
    for item in plan.candidates:
        if item not in remaining:
            item.exited_by_itself = True
            say(
                "info",
                f"Old server {label(item)} of port {port} finished and exited by "
                "itself; nothing was stopped.",
                console=True,
            )
    if aborted is not None:
        say(
            "info",
            f"Old servers of port {port}: {aborted}, so nothing more was stopped "
            f"(still running: {'; '.join(label(item) for item in remaining)}).",
            console=False,
        )
        return report(aborted)
    if not remaining:
        return report()

    fresh_processes = world.processes()
    fresh_endpoints = world.listening()
    if not fresh_processes or not fresh_endpoints:
        reason = (
            "the process table or the sockets could not be read again before "
            "stopping anything"
        )
        say(
            "info",
            f"Old servers of port {port}: {reason}, so nothing was stopped "
            f"(still running: {'; '.join(label(item) for item in remaining)}).",
            console=False,
        )
        return report(reason)
    _stop_left_over(
        remaining,
        fresh_processes,
        fresh_endpoints,
        stop_chain=world.stop_chain,
        may_continue=lambda: not world.stopping(),
    )
    for item in remaining:
        if item.stopped:
            say(
                "warning",
                f"Stopped an old My Claude Code server of port {port} and this "
                f"configuration folder: {label(item)}. It owned no listening "
                f"socket, so it could not answer anyone on port {port}, and it did "
                f"not exit by itself within {wait}, so it was stopped by process "
                "id, innermost first.",
                console=True,
            )
        elif item.kept:
            say(
                "info",
                f"Left running: old server {label(item)} of port {port} opened a "
                "listening socket again, so it is working and was not stopped.",
                console=False,
            )
        elif item.exited_by_itself:
            say(
                "info",
                f"Old server {label(item)} of port {port} exited by itself before "
                "it had to be stopped; nothing was stopped.",
                console=True,
            )
        elif item.note == "some of it could not be stopped":
            say(
                "warning",
                f"Could not fully stop the old My Claude Code server {label(item)} "
                f"of port {port}: stopping it by process id did not end every one "
                "of its processes.",
                console=True,
            )
        else:
            say(
                "info",
                f"Old server {label(item)} of port {port} was left running: "
                f"{item.note}.",
                console=False,
            )
    return report()


#: Builds the machine the start-time survey and cleanup look at, for one
#: configuration folder's request log. ``None`` = the real machine. The ONE
#: seam the test suite uses to put an inert machine in place for every test
#: (``tests/conftest.py``): a server's own start must never reach the real
#: process table from a test.
CleanupWorldFactory = Callable[[Path], CleanupWorld]
_cleanup_world_factory: CleanupWorldFactory | None = None


def set_cleanup_world_factory(factory: CleanupWorldFactory | None) -> None:
    """Replace (or, with ``None``, restore) the machine the start cleanup uses."""

    global _cleanup_world_factory
    _cleanup_world_factory = factory


def cleanup_world(request_log_path: Path) -> CleanupWorld:
    """The machine a starting server looks at, for this folder's request log."""

    factory = _cleanup_world_factory
    if factory is not None:
        return factory(request_log_path)
    return real_cleanup_world(request_log_path)


# -------------------------------------------------------------- the real world


def pid_is_alive(pid: int) -> bool:
    """Whether one exact pid is alive. Never a signal of any kind.

    On Windows ``OpenProcess`` + ``GetExitCodeProcess``: exact, and about a
    microsecond, where ``tasklist`` costs a second and matches substrings. A
    pid that cannot be opened for a reason other than "no such process" is
    alive (unknown is alive). Elsewhere the existing POSIX check.
    """

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() != error_invalid_parameter
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    return process_is_alive(pid)


def real_world(request: RescueRequest, *, scan_timeout: float) -> RescueWorld:
    """The machine, through the existing helpers and nothing new."""

    from my_claude_code.cli.port_diagnostics import probe_port_available
    from my_claude_code.config.logging_config import write_server_log_line
    from my_claude_code.config.paths import request_log_path, server_log_path
    from my_claude_code.config.update_progress import active_update
    from my_claude_code.core.mcc_processes import (
        listening_endpoints,
        scan_processes,
        stop_chain,
    )
    from my_claude_code.core.request_log import read_server_sessions

    log_file = os.getenv("LOG_FILE", str(server_log_path()))

    def log(message: str) -> None:
        write_server_log_line(
            log_file, "WARNING", message, module=__name__, function="rescue"
        )

    return RescueWorld(
        port_is_free=lambda: probe_port_available(request.host, request.port),
        listening=listening_endpoints,
        processes=lambda: scan_processes(timeout=scan_timeout),
        sessions=lambda: read_server_sessions(request_log_path()),
        alive=pid_is_alive,
        stop_chain=stop_chain,
        updating=lambda: active_update() is not None,
        log=log,
        self_pids=frozenset({os.getpid(), os.getppid()}),
    )


def real_cleanup_world(request_log_path: Path) -> CleanupWorld:
    """The machine, for a server's own start, through the existing helpers.

    Lines go through the server's own logger (it is running in the server, so
    ``server.log`` already has its sink) and, for the person who started it, to
    this process's console. "Stopping" is the server's own stop clock: once a
    stop or a reload is requested, nothing more is stopped.
    """

    from loguru import logger

    from my_claude_code.core.console_notice import write_console_line
    from my_claude_code.core.mcc_processes import (
        listening_endpoints,
        scan_processes,
        stop_chain,
    )
    from my_claude_code.core.request_log import read_server_sessions
    from my_claude_code.core.stop_deadline import stop_deadline

    def info(message: str) -> None:
        logger.info("{}", message)

    def warning(message: str) -> None:
        logger.warning("{}", message)

    def console(message: str) -> None:
        write_console_line(f"My Claude Code: {message}")

    return CleanupWorld(
        listening=listening_endpoints,
        processes=scan_processes,
        sessions=lambda: read_server_sessions(request_log_path),
        alive=pid_is_alive,
        stop_chain=stop_chain,
        info=info,
        warning=warning,
        console=console,
        stopping=lambda: stop_deadline().requested,
    )


def parse_rescue_arguments(args: tuple[str, ...]) -> dict[str, Any] | None:
    """``--reason R [--known-pid N] [--child-pid N]``, strictly. None = usage."""

    parsed: dict[str, Any] = {"reason": None, "known_pid": None, "child_pid": None}
    names = {
        "--reason": "reason",
        "--known-pid": "known_pid",
        "--child-pid": "child_pid",
    }
    index = 0
    while index < len(args):
        name = names.get(args[index])
        if name is None or index + 1 >= len(args) or parsed[name] is not None:
            return None
        value = args[index + 1]
        if name == "reason":
            if value not in RESCUE_REASONS:
                return None
            parsed[name] = value
        else:
            if not value.isdigit() or int(value) <= 0:
                return None
            parsed[name] = int(value)
        index += 2
    if parsed["reason"] is None:
        return None
    return parsed


def rescue_command(args: tuple[str, ...]) -> int:
    """The verb: build the request from settings that already exist, run, print."""

    parsed = parse_rescue_arguments(args)
    if parsed is None:
        return 2

    from my_claude_code.cli.desktop import server_stop_wait_seconds
    from my_claude_code.config.settings import get_settings

    settings = get_settings()
    request = RescueRequest(
        host=(settings.host or "127.0.0.1").strip(),
        port=int(settings.port),
        reason=parsed["reason"],
        known_pid=parsed["known_pid"],
        child_pid=parsed["child_pid"],
        stop_wait_seconds=float(server_stop_wait_seconds(settings)),
        port_wait_seconds=float(settings.desktop_busy_grace_seconds),
    )
    world = real_world(
        request, scan_timeout=max(1.0, float(settings.desktop_status_wall_seconds))
    )
    print(json.dumps(run_rescue(request, world), indent=2))
    return 0


__all__ = [
    "OUTCOME_PORT_FREE",
    "OUTCOME_REFUSED",
    "RESCUE_REASONS",
    "RescueRequest",
    "RescueWorld",
    "parse_rescue_arguments",
    "pid_is_alive",
    "real_world",
    "rescue_command",
    "run_rescue",
]
