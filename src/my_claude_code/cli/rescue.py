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
"""

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.core.mcc_processes import (
    ProcessChain,
    ProcessFacts,
    mcc_server_chains,
    process_is_alive,
)
from my_claude_code.core.request_log import ServerSession

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


def _latest_session(
    chain: ProcessChain, sessions: list[ServerSession]
) -> ServerSession | None:
    pids = set(chain.pids)
    rows = [row for row in sessions if row.pid is not None and row.pid in pids]
    if not rows:
        return None
    return max(rows, key=lambda row: row.last_seen_at)


def _pids(chain: ProcessChain) -> list[int]:
    return list(chain.pids)


def _scope(
    request: RescueRequest,
    chains: list[ProcessChain],
    sessions: list[ServerSession],
    listening_pids: frozenset[int],
    self_pids: frozenset[int],
) -> tuple[list[_Candidate], list[dict[str, Any]]]:
    """Decision R1 as a function: the candidates, and what was left alone."""

    candidates: list[_Candidate] = []
    left_alone: list[dict[str, Any]] = []
    for chain in chains:
        members = set(chain.pids)
        if members & self_pids:
            continue
        named = (request.known_pid in members) or (request.child_pid in members)
        session = _latest_session(chain, sessions)
        if members & listening_pids:
            # Owns a listening socket somewhere: a running server. Never a
            # candidate, whatever else is true of it.
            if named or (session is not None and session.port == request.port):
                left_alone.append(
                    {
                        "pids": _pids(chain),
                        "why": "it owns a listening socket, so it is a running server",
                    }
                )
            continue
        if request.child_pid is not None and request.child_pid in members:
            candidates.append(
                _Candidate(chain, "the server the desktop app itself started")
            )
            continue
        if session is not None and session.port == request.port:
            candidates.append(
                _Candidate(
                    chain,
                    f"a server of this configuration folder that recorded port "
                    f"{request.port} and holds no listening socket",
                )
            )
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
                )
            )
            continue
        if named:
            where = (
                f"it recorded port {session.port}"
                if session is not None and session.port is not None
                else "it has no record in this configuration folder"
            )
            left_alone.append(
                {
                    "pids": _pids(chain),
                    "why": f"not tied to port {request.port} and this "
                    f"configuration folder ({where})",
                }
            )
    return candidates, left_alone


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
    deadline = wait_started + max(0.0, request.stop_wait_seconds)
    remaining = list(candidates)
    while True:
        remaining = [
            item
            for item in remaining
            if any(world.alive(pid) for pid in item.chain.pids)
        ]
        if not remaining:
            break
        rebound = _rebound(request, world, candidates)
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
        if world.clock() >= deadline:
            break
        world.sleep(POLL_SECONDS)
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
        fresh_listening = frozenset(pid for _port, pid in fresh_endpoints)
        fresh_chains = mcc_server_chains(fresh_processes)
        for item in remaining:
            observed = set(item.chain.pids)
            fresh = next(
                (
                    chain
                    for chain in fresh_chains
                    if chain.root.pid == item.chain.root.pid
                    or observed & set(chain.pids)
                ),
                None,
            )
            if fresh is None:
                item.exited_by_itself = True
                item.note = "gone before it had to be stopped"
                continue
            # Only pids observed at the start -- never a pid that appeared
            # since, which may already belong to somebody else.
            members = tuple(
                member for member in fresh.members if member.pid in observed
            )
            if not members:
                item.exited_by_itself = True
                item.note = "gone before it had to be stopped"
                continue
            if fresh_listening & {member.pid for member in members}:
                item.kept = True
                item.note = "it opened a listening socket again, so it was left running"
                left_alone.append({"pids": _pids(item.chain), "why": item.note})
                continue
            target = ProcessChain(root=members[0], members=members)
            world.log(
                f"Rescue of port {request.port}: stopping pid "
                f"{', '.join(str(member.pid) for member in members)} ({item.why}); "
                f"it was given {request.stop_wait_seconds:.0f} s to finish and exit "
                f"by itself and did not. Reason: {request.reason}."
            )
            item.stopped = world.stop_chain(target)
            if not item.stopped:
                item.note = "some of it could not be stopped"
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
