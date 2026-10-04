"""``mcc-desktop --rescue``: what it may stop, and everything it may not (7.71.0).

Decision R1 (2026-10-01): "old servers" are ONLY My Claude Code servers tied to
THIS port and THIS configuration folder -- never another port (the user's
agents run their own servers there), never another configuration folder, never
anything that is not My Claude Code. Decision R4: only a port the OS says is
free can be rescued, and the rescue looks again and refuses if anything holds
it. Decision R2: each old server is given its stop budget to finish and exit by
itself; only what is left is stopped, by exact pid, innermost first.

Every test drives :func:`run_rescue` against a fake machine. The stop is a spy
that FAILS the test the moment it is asked to stop anything out of scope.
"""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from my_claude_code.cli import rescue
from my_claude_code.cli.rescue import (
    OUTCOME_PORT_FREE,
    OUTCOME_REFUSED,
    RescueRequest,
    RescueWorld,
    parse_rescue_arguments,
    run_rescue,
)
from my_claude_code.core.mcc_processes import ProcessChain, ProcessFacts
from my_claude_code.core.request_log import ServerSession

PORT = 18731
OTHER_PORT = 18732
_TOOL_PYTHON = r"C:\Users\x\AppData\Roaming\uv\tools\my-claude-code\Scripts\python.exe"


#: When every fake launch's processes were created: before every fake session
#: row (``_session`` writes at 1001+), as a real launch is before its own row.
LAUNCHED_AT = 900.0


def _launch(
    root_pid: int,
    leaf_pid: int,
    *,
    parent: int = 1,
    started_at: float | None = LAUNCHED_AT,
) -> list[ProcessFacts]:
    """The two processes of one ``mcc-server`` launch: trampoline and server."""

    return [
        ProcessFacts(
            pid=root_pid,
            parent_pid=parent,
            image="mcc-server.exe",
            executable=r"C:\Users\x\.local\bin\mcc-server.exe",
            command='"mcc-server"',
            started_at=started_at,
        ),
        ProcessFacts(
            pid=leaf_pid,
            parent_pid=root_pid,
            image="python.exe",
            executable=_TOOL_PYTHON,
            command=f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-server.exe"',
            started_at=started_at,
        ),
    ]


def _agent_launcher(pid: int) -> ProcessFacts:
    """A coding agent: MCC's family, never a server, never touched."""

    return ProcessFacts(
        pid=pid,
        parent_pid=1,
        image="python.exe",
        executable=_TOOL_PYTHON,
        command=f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-claude.exe"',
    )


def _stranger(pid: int) -> ProcessFacts:
    return ProcessFacts(
        pid=pid,
        parent_pid=1,
        image="nginx.exe",
        executable=r"C:\nginx\nginx.exe",
        command=r'"C:\nginx\nginx.exe"',
    )


def _session(pid: int, port: int | None, *, row: int = 1) -> ServerSession:
    return ServerSession(
        id=row,
        pid=pid,
        started_at=1_000.0 + row,
        last_seen_at=2_000.0 + row,
        host="127.0.0.1",
        port=port,
    )


class Clock:
    """A clock the waits advance by sleeping on it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Machine:
    """A fake machine: process table, sockets, sessions, liveness, a stop spy."""

    def __init__(
        self,
        processes: list[ProcessFacts],
        *,
        listening: set[tuple[int, int]] | None = None,
        sessions: list[ServerSession] | None = None,
        allowed_to_stop: set[int] | None = None,
    ) -> None:
        self.processes = list(processes)
        self.listening = set(listening or {(135, 1)})
        self.sessions = list(sessions or [])
        self.alive_pids = {item.pid for item in processes}
        self.allowed = set(allowed_to_stop or ())
        self.stopped: list[tuple[int, ...]] = []
        self.logs: list[str] = []
        self.clock = Clock()
        self.updating = False
        self.port_free_after: float | None = 0.0
        #: Seconds the OS keeps the port unbindable after a stop; None = for ever.
        self.port_busy_after_stop: float | None = 0.0
        #: When each stop happened, on the rescue's own clock.
        self.stop_times: list[float] = []
        #: Called on every liveness look, to script exits and re-binds.
        self.on_look: Callable[[Machine], None] = lambda _machine: None

    def port_is_free(self) -> bool:
        if any(port == PORT for port, _pid in self.listening):
            return False
        return (
            self.port_free_after is not None and self.clock.now >= self.port_free_after
        )

    def alive(self, pid: int) -> bool:
        self.on_look(self)
        return pid in self.alive_pids

    def stop_chain(self, chain: ProcessChain) -> bool:
        pids = tuple(member.pid for member in chain.members)
        out_of_scope = set(pids) - self.allowed
        if out_of_scope:
            pytest.fail(
                f"the rescue tried to stop {sorted(out_of_scope)}, out of scope"
            )
        self.stopped.append(pids)
        self.stop_times.append(self.clock.now)
        self.alive_pids -= set(pids)
        self.processes = [item for item in self.processes if item.pid not in pids]
        self.port_free_after = (
            None
            if self.port_busy_after_stop is None
            else self.clock.now + self.port_busy_after_stop
        )
        return True

    def world(self) -> RescueWorld:
        return RescueWorld(
            port_is_free=self.port_is_free,
            listening=lambda: frozenset(self.listening),
            processes=lambda: [
                item for item in self.processes if item.pid in self.alive_pids
            ],
            sessions=lambda: list(self.sessions),
            alive=self.alive,
            stop_chain=self.stop_chain,
            updating=lambda: self.updating,
            log=self.logs.append,
            clock=self.clock,
            sleep=self.clock.sleep,
            self_pids=frozenset({999}),
        )


def _request(
    *,
    reason: str = "listener-lost",
    known_pid: int | None = 102,
    child_pid: int | None = None,
) -> RescueRequest:
    return RescueRequest(
        host="127.0.0.1",
        port=PORT,
        reason=reason,
        known_pid=known_pid,
        child_pid=child_pid,
        stop_wait_seconds=24.0,
        port_wait_seconds=15.0,
    )


def _zombie_machine(**kwargs: object) -> Machine:
    """The 2026-09-28 shape: server 101 -> 102 lost its listener on PORT and
    keeps running; it is the only thing in scope."""

    machine = Machine(
        [*_launch(101, 102)],
        sessions=[_session(102, PORT)],
        allowed_to_stop={101, 102},
    )
    for name, value in kwargs.items():
        setattr(machine, name, value)
    return machine


# -- R1: scope -----------------------------------------------------------------


def test_a_chain_listening_on_another_port_is_never_a_candidate() -> None:
    # The user's agents run their own servers on other ports.
    machine = Machine(
        [*_launch(101, 102), *_launch(201, 202)],
        listening={(135, 1), (OTHER_PORT, 202)},
        sessions=[_session(102, PORT, row=1), _session(202, OTHER_PORT, row=2)],
        allowed_to_stop={101, 102},
    )
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_PORT_FREE
    stopped = {pid for pids in machine.stopped for pid in pids}
    assert not stopped & {201, 202}


def test_a_listener_less_server_of_another_port_is_never_a_candidate() -> None:
    # Even dead: a zombie of ANOTHER port is that port's business.
    machine = Machine(
        [*_launch(101, 102), *_launch(201, 202)],
        sessions=[_session(102, PORT, row=1), _session(202, OTHER_PORT, row=2)],
        allowed_to_stop={101, 102},
    )
    report = run_rescue(_request(), machine.world())
    assert [server["pids"] for server in report["servers"]] == [[101, 102]]


def test_a_server_of_another_configuration_folder_is_never_a_candidate() -> None:
    # No row in THIS configuration folder's server_sessions: another folder's.
    machine = Machine(
        [*_launch(301, 302)],
        sessions=[],
        allowed_to_stop=set(),
    )
    report = run_rescue(_request(known_pid=302), machine.world())
    assert report["outcome"] == OUTCOME_PORT_FREE
    assert report["servers"] == []
    assert machine.stopped == []
    assert report["left_alone"] == [
        {
            "pids": [301, 302],
            "why": f"not tied to port {PORT} and this configuration folder "
            "(it has no record in this configuration folder)",
        }
    ]


def test_a_known_pid_that_recorded_another_port_is_left_alone_and_named() -> None:
    machine = Machine(
        [*_launch(101, 102)],
        sessions=[_session(102, OTHER_PORT)],
        allowed_to_stop=set(),
    )
    report = run_rescue(_request(known_pid=102), machine.world())
    assert machine.stopped == []
    assert report["left_alone"][0]["pids"] == [101, 102]
    assert f"recorded port {OTHER_PORT}" in report["left_alone"][0]["why"]


def test_a_serving_chain_is_never_a_candidate() -> None:
    # Owns a listening socket anywhere: a running server, whatever it recorded.
    machine = Machine(
        [*_launch(101, 102)],
        listening={(135, 1), (19999, 102)},
        sessions=[_session(102, PORT)],
        allowed_to_stop=set(),
    )
    report = run_rescue(_request(), machine.world())
    assert machine.stopped == []
    assert report["servers"] == []
    # Excluded at the first look -- never waited on, never re-checked as a
    # candidate (the re-check before a stop is a second, separate layer).
    assert report["left_alone"] == [
        {
            "pids": [101, 102],
            "why": "it owns a listening socket, so it is a running server",
        }
    ]
    assert report["timings"]["wait"] == 0.0


def test_nothing_that_is_not_an_mcc_server_is_ever_a_candidate() -> None:
    # A coding agent (MCC's family, not a server) and a stranger, both with
    # this port's number somewhere in their lives, both untouched.
    machine = Machine(
        [_agent_launcher(501), _stranger(601), *_launch(101, 102)],
        sessions=[
            _session(501, PORT, row=1),
            _session(601, PORT, row=2),
            _session(102, PORT, row=3),
        ],
        allowed_to_stop={101, 102},
    )
    run_rescue(_request(known_pid=501), machine.world())
    stopped = {pid for pids in machine.stopped for pid in pids}
    assert stopped == {101, 102}


def test_the_child_the_app_started_is_a_candidate_by_its_exact_pid() -> None:
    # Never bound, so no session row recorded a port: only the exact pid the
    # window started puts it in scope.
    machine = Machine(
        [*_launch(701, 702, parent=4000)],
        sessions=[_session(702, None)],
        allowed_to_stop={701, 702},
    )
    report = run_rescue(
        _request(reason="never-bound", known_pid=None, child_pid=701), machine.world()
    )
    assert report["outcome"] == OUTCOME_PORT_FREE
    assert machine.stopped == [(701, 702)]
    assert report["servers"][0]["why"] == "the server the desktop app itself started"


def test_a_child_pid_that_matches_nothing_stops_nothing() -> None:
    machine = Machine([*_launch(701, 702)], sessions=[], allowed_to_stop=set())
    report = run_rescue(
        _request(reason="never-bound", known_pid=None, child_pid=12345), machine.world()
    )
    assert machine.stopped == []
    assert report["servers"] == []


def test_the_rescues_own_process_is_never_a_candidate() -> None:
    machine = Machine(
        [*_launch(999, 998)], sessions=[_session(998, PORT)], allowed_to_stop=set()
    )
    report = run_rescue(_request(), machine.world())
    assert machine.stopped == []
    assert report["servers"] == []


# -- R4: refuse unless the OS says the port is free ----------------------------


def test_a_foreign_listener_on_the_port_makes_the_rescue_refuse() -> None:
    machine = _zombie_machine(listening={(135, 1), (PORT, 601)})
    machine.processes.append(_stranger(601))
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_REFUSED
    assert "held by pid 601" in report["detail"]
    assert machine.stopped == []


def test_an_empty_socket_enumeration_makes_the_rescue_refuse() -> None:
    # "Could not look" is never "nothing listens".
    machine = _zombie_machine(listening=set())
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_REFUSED
    assert "could not be listed" in report["detail"]
    assert machine.stopped == []


def test_a_port_the_os_will_not_bind_makes_the_rescue_refuse() -> None:
    machine = _zombie_machine(port_free_after=None)
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_REFUSED
    assert machine.stopped == []
    # Refused at the first look, before any scan or wait.
    assert report["detail"] == f"the operating system would not bind port {PORT}"
    assert report["timings"] == {}


def test_an_empty_process_table_makes_the_rescue_refuse() -> None:
    machine = _zombie_machine()
    world = machine.world()
    world.processes = lambda: []
    report = run_rescue(_request(), world)
    assert report["outcome"] == OUTCOME_REFUSED
    assert "process table" in report["detail"]


def test_an_update_in_progress_makes_the_rescue_refuse() -> None:
    machine = _zombie_machine(updating=True)
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_REFUSED
    assert machine.stopped == []


# -- R2: finish and exit by itself first, then exact pids ----------------------


def test_a_candidate_that_exits_by_itself_is_not_killed() -> None:
    machine = _zombie_machine()

    def exits_at_six_seconds(machine: Machine) -> None:
        if machine.clock.now >= 6.0:
            machine.alive_pids -= {101, 102}

    machine.on_look = exits_at_six_seconds
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_PORT_FREE
    assert machine.stopped == []
    assert report["servers"][0]["exited_by_itself"] is True
    assert report["timings"]["wait"] == pytest.approx(6.0)


def test_a_candidate_that_rebinds_aborts_the_rescue_and_nothing_is_stopped() -> None:
    # An in-process reload that got its socket back: working, not dead.
    machine = _zombie_machine()

    def rebinds_at_four_seconds(machine: Machine) -> None:
        if machine.clock.now >= 4.0:
            machine.listening.add((PORT, 102))

    machine.on_look = rebinds_at_four_seconds
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_REFUSED
    assert "bound port" in report["detail"] and "102" in report["detail"]
    assert machine.stopped == []
    # Aborted inside the wait, the moment it re-bound -- not at the re-check
    # before the stop, 24 s later.
    assert report["timings"]["wait"] < 6.0
    assert "stop" not in report["timings"]


def test_the_stop_is_by_exact_pid_only_after_the_full_budget() -> None:
    machine = _zombie_machine()
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_PORT_FREE
    assert machine.stopped == [(101, 102)]
    assert machine.stop_times and machine.stop_times[0] >= 24.0
    assert report["servers"][0]["stopped"] is True
    assert report["servers"][0]["exited_by_itself"] is False


def test_a_pid_that_appeared_after_the_scan_is_never_stopped() -> None:
    # Re-derived from a fresh table: a member the first look did not see may
    # already belong to somebody else, and is never signalled.
    machine = _zombie_machine()

    def a_new_child_appears(machine: Machine) -> None:
        if machine.clock.now >= 10.0 and all(
            item.pid != 103 for item in machine.processes
        ):
            machine.processes.append(
                ProcessFacts(
                    pid=103,
                    parent_pid=102,
                    image="python.exe",
                    executable=_TOOL_PYTHON,
                    command=f'"{_TOOL_PYTHON}" -m my_claude_code.cli.entrypoints',
                )
            )
            machine.alive_pids.add(103)

    machine.on_look = a_new_child_appears
    run_rescue(_request(), machine.world())
    assert machine.stopped == [(101, 102)]


def test_the_stop_is_innermost_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # core.mcc_processes.stop_chain is the one stop; prove its order.
    from my_claude_code.core import mcc_processes

    order: list[int] = []

    def record(pid: int, *, grace: float = 3.0) -> bool:
        del grace
        order.append(pid)
        return True

    monkeypatch.setattr(mcc_processes, "stop_process", record)
    facts = _launch(101, 102)
    assert mcc_processes.stop_chain(ProcessChain(root=facts[0], members=tuple(facts)))
    assert order == [102, 101]


# -- every wait is bounded ------------------------------------------------------


def test_every_wait_is_bounded() -> None:
    # A candidate that never exits and a port that never frees: the rescue
    # still ends, at 24 s + 15 s on its own clock, and says why.
    machine = _zombie_machine(port_busy_after_stop=None)
    looks = {"count": 0}

    def count(_machine: Machine) -> None:
        looks["count"] += 1

    machine.on_look = count
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_REFUSED
    assert "still not free after 15 s" in report["detail"]
    assert machine.clock.now == pytest.approx(24.0 + 15.0, abs=1.0)
    assert looks["count"] < 200


def test_the_port_wait_is_the_busy_grace() -> None:
    # The stop happens at 24 s; the OS frees the port 6 s later, inside the
    # 15 s wait.
    machine = _zombie_machine(port_busy_after_stop=6.0)
    report = run_rescue(_request(), machine.world())
    assert report["outcome"] == OUTCOME_PORT_FREE
    assert report["timings"]["port"] == pytest.approx(6.0, abs=0.6)


# -- the record -----------------------------------------------------------------


def test_every_action_writes_one_log_line_that_says_what_and_why() -> None:
    machine = _zombie_machine()
    run_rescue(_request(), machine.world())
    stops = [line for line in machine.logs if "stopping pid" in line]
    assert len(stops) == 1
    assert "101, 102" in stops[0]
    assert "given 24 s" in stops[0]
    assert "listener-lost" in stops[0]
    assert any("the port is free" in line for line in machine.logs)


def test_the_report_is_one_json_document_with_the_documented_keys() -> None:
    machine = _zombie_machine()
    report = run_rescue(_request(), machine.world())
    assert json.loads(json.dumps(report)) == report
    assert set(report) == {
        "schema",
        "outcome",
        "reason",
        "host",
        "port",
        "known_pid",
        "child_pid",
        "servers",
        "left_alone",
        "stop_wait_seconds",
        "port_wait_seconds",
        "timings",
        "detail",
    }
    assert set(report["servers"][0]) == {
        "pids",
        "why",
        "exited_by_itself",
        "stopped",
        "note",
    }


# -- the verb -------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--reason",),
        ("--reason", "slow"),
        ("--reason", "process-gone", "--known-pid", "zero"),
        ("--reason", "process-gone", "--known-pid", "0"),
        ("--reason", "process-gone", "--known-pid", "-4"),
        ("--reason", "process-gone", "--reason", "process-gone"),
        ("--known-pid", "4"),
        ("--reason", "process-gone", "--kill", "4"),
    ],
)
def test_the_arguments_are_read_strictly(args: tuple[str, ...]) -> None:
    assert parse_rescue_arguments(args) is None


def test_good_arguments_are_read() -> None:
    assert parse_rescue_arguments(
        ("--reason", "never-bound", "--child-pid", "7001", "--known-pid", "42")
    ) == {"reason": "never-bound", "known_pid": 42, "child_pid": 7001}


def test_bad_arguments_exit_2_with_the_usage_like_an_older_mcc_desktop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from my_claude_code.cli import desktop_entrypoint

    with pytest.raises(SystemExit) as raised:
        desktop_entrypoint.launch(("--rescue", "--reason", "slow"))
    assert raised.value.code == 2
    assert "--rescue" in capsys.readouterr().err


# -- liveness without a signal ----------------------------------------------------


def test_the_rescue_never_signals_a_pid_to_ask_if_it_is_alive() -> None:
    # os.kill(pid, 0) is TerminateProcess on Windows. No call to any kill at
    # all in the module -- the only stop is core.mcc_processes.stop_chain.
    import ast

    tree = ast.parse(Path(rescue.__file__).read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "kill" not in calls
    assert "send_signal" not in calls
    assert "terminate" not in calls


def test_pid_is_alive_tells_a_live_process_from_an_exited_one() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    assert rescue.pid_is_alive(child.pid) is False
    import os

    assert rescue.pid_is_alive(os.getpid()) is True
