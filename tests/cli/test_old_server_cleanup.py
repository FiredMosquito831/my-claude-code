"""Old servers of this port and folder do not outlive a start or an update (7.72.0).

Decision R1 (2026-10-01): "old servers" are ONLY My Claude Code servers tied to
THIS port and THIS configuration folder -- never another port (the user's agents
run their own servers there), never another configuration folder, never anything
that is not My Claude Code, never a server that answers. Decision R2: each is
given its stop budget to exit by itself; only what is left is stopped, by exact
pid, innermost first. Decision 12 and answer 4 put that rule into the start of
every server that may take its port -- which is also how an update meets them,
because the installer starts the new server.

The scope is ONE function, :func:`old_server_scope`, and every row of its table
is a test here. The cleanup is driven against a fake machine whose stop is a
SPY that fails the test the moment it is asked to stop anything out of scope.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.cli import commands, rescue
from my_claude_code.cli.rescue import (
    CleanupRequest,
    CleanupWorld,
    plan_old_server_cleanup,
    run_old_server_cleanup,
)
from my_claude_code.core import mcc_processes
from my_claude_code.core.mcc_processes import (
    ProcessChain,
    ProcessFacts,
    mcc_server_chains,
)
from my_claude_code.core.request_log import SESSION_VERSION_SINCE, ServerSession
from my_claude_code.core.server_inventory import (
    PID_REUSE_SLACK_SECONDS,
    SCOPE_NEWER,
    SCOPE_NO_RECORD,
    SCOPE_OLD,
    SCOPE_OTHER_PORT,
    SCOPE_SELF,
    SCOPE_SERVING,
    SCOPE_UNPROVEN,
    old_server_scope,
)

PORT = 18750
OTHER_PORT = 18751
VERSION = "7.72.0"
#: Wall-clock moments, epoch seconds: the zombies started long before this
#: server, whose own launch is ``SELF_STARTED``.
ZOMBIE_STARTED = 1_790_000_000.0
SELF_STARTED = ZOMBIE_STARTED + 86_400.0
NOW = SELF_STARTED + 5.0
SELF_ROOT, SELF_PID = 9001, 9002
_TOOL_PYTHON = r"C:\Users\x\AppData\Roaming\uv\tools\my-claude-code\Scripts\python.exe"


def _launch(
    root_pid: int,
    leaf_pid: int,
    *,
    started_at: float | None = ZOMBIE_STARTED,
    parent: int = 1,
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


def _self() -> list[ProcessFacts]:
    return _launch(SELF_ROOT, SELF_PID, started_at=SELF_STARTED)


def _agent_launcher(pid: int) -> ProcessFacts:
    """A coding agent: MCC's family, never a server, never touched."""

    return ProcessFacts(
        pid=pid,
        parent_pid=1,
        image="python.exe",
        executable=_TOOL_PYTHON,
        command=f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-claude.exe"',
        started_at=ZOMBIE_STARTED,
    )


def _stranger(pid: int) -> ProcessFacts:
    return ProcessFacts(
        pid=pid,
        parent_pid=1,
        image="nginx.exe",
        executable=r"C:\nginx\nginx.exe",
        command=r'"C:\nginx\nginx.exe"',
        started_at=ZOMBIE_STARTED,
    )


def _session(
    pid: int,
    port: int | None,
    *,
    row: int = 1,
    started_at: float = ZOMBIE_STARTED + 3.0,
    version: str | None = None,
) -> ServerSession:
    return ServerSession(
        id=row,
        pid=pid,
        started_at=started_at,
        last_seen_at=NOW - 12.0,
        host="127.0.0.1",
        port=port,
        version=version,
    )


def _chain(facts: list[ProcessFacts]) -> ProcessChain:
    chains = mcc_server_chains(facts)
    assert len(chains) == 1
    return chains[0]


# ======================================================== the ONE scope function


def _scope(
    facts: list[ProcessFacts],
    sessions: list[ServerSession],
    *,
    listening: frozenset[int] = frozenset(),
    self_pids: frozenset[int] = frozenset({SELF_PID}),
    started_before: float | None = SELF_STARTED,
) -> Any:
    return old_server_scope(
        _chain(facts),
        port=PORT,
        sessions=sessions,
        listening_pids=listening,
        self_pids=self_pids,
        started_before=started_before,
    )


def test_scope_listener_less_same_port_and_folder_is_old() -> None:
    verdict = _scope(_launch(101, 102), [_session(102, PORT)])
    assert verdict.kind == SCOPE_OLD
    assert verdict.is_old
    assert verdict.session is not None and verdict.session.port == PORT


def test_scope_listener_less_other_port_is_never_old() -> None:
    verdict = _scope(_launch(101, 102), [_session(102, OTHER_PORT)])
    assert verdict.kind == SCOPE_OTHER_PORT
    assert verdict.why == f"it recorded port {OTHER_PORT}"


def test_scope_listener_less_with_no_record_here_is_another_folders() -> None:
    # The request log lives inside the configuration folder: no row here means
    # it is not proven to be this folder's server.
    verdict = _scope(_launch(101, 102), [])
    assert verdict.kind == SCOPE_NO_RECORD


def test_scope_a_server_that_listens_anywhere_is_running() -> None:
    # Same port, same folder, a listener on ANY port: a running server.
    verdict = _scope(
        _launch(101, 102), [_session(102, PORT)], listening=frozenset({102})
    )
    assert verdict.kind == SCOPE_SERVING


def test_scope_this_servers_own_launch_is_never_old() -> None:
    verdict = _scope(
        _launch(101, 102), [_session(102, PORT)], self_pids=frozenset({102})
    )
    assert verdict.kind == SCOPE_SELF


def test_scope_a_record_with_no_port_is_unproven() -> None:
    # Never bound, a 7.69.2+ server that already stopped claiming the port,
    # or a row written before 6.72.2: no evidence of the port either way.
    verdict = _scope(_launch(101, 102), [_session(102, None)])
    assert verdict.kind == SCOPE_UNPROVEN
    assert "names no port" in verdict.why


def test_scope_an_unknown_start_time_is_unproven() -> None:
    # Without the process's start time a row cannot be told from an earlier
    # process's that had the same pid.
    verdict = _scope(_launch(101, 102, started_at=None), [_session(102, PORT)])
    assert verdict.kind == SCOPE_UNPROVEN
    assert "no start time" in verdict.why


def test_scope_a_row_older_than_its_pids_process_belongs_to_somebody_else() -> None:
    # Pid 102 served port PORT here last month; the process that has pid 102
    # NOW started an hour ago and is another folder's server.
    stale_row = _session(102, PORT, started_at=ZOMBIE_STARTED - 3_600.0)
    verdict = _scope(_launch(101, 102), [stale_row])
    assert verdict.kind == SCOPE_NO_RECORD
    assert "earlier processes" in verdict.why


def test_scope_the_pid_reuse_slack_is_small_and_only_absorbs_rounding() -> None:
    # Windows reports whole-second creation times; a genuine row is never
    # earlier than its process by more than that.
    row = _session(102, PORT, started_at=ZOMBIE_STARTED - 1.0)
    assert _scope(_launch(101, 102), [row]).kind == SCOPE_OLD
    row = _session(102, PORT, started_at=ZOMBIE_STARTED - PID_REUSE_SLACK_SECONDS - 0.5)
    assert _scope(_launch(101, 102), [row]).kind == SCOPE_NO_RECORD


def test_scope_a_server_younger_than_this_one_is_not_old() -> None:
    verdict = _scope(
        _launch(101, 102, started_at=SELF_STARTED),
        [_session(102, PORT, started_at=SELF_STARTED + 2)],
    )
    assert verdict.kind == SCOPE_NEWER


def test_scope_the_latest_own_row_decides() -> None:
    # A launch that served PORT and later another port is that port's now.
    rows = [
        _session(102, PORT, row=1),
        ServerSession(
            id=2,
            pid=102,
            started_at=ZOMBIE_STARTED + 50,
            last_seen_at=NOW - 1,
            host="127.0.0.1",
            port=OTHER_PORT,
        ),
    ]
    assert _scope(_launch(101, 102), rows).kind == SCOPE_OTHER_PORT


def test_scope_nothing_that_is_not_an_mcc_server_is_ever_a_chain() -> None:
    # The scope is only ever asked about structural server chains: a coding
    # agent and a stranger never become one, whatever their command lines say.
    chains = mcc_server_chains([_agent_launcher(501), _stranger(601)])
    assert chains == []


# ================================================================ a fake machine


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Machine:
    """Process table, sockets, sessions, liveness -- and a stop SPY."""

    def __init__(
        self,
        processes: list[ProcessFacts],
        *,
        listening: set[tuple[int, int]] | None = None,
        sessions: list[ServerSession] | None = None,
        allowed_to_stop: set[int] | None = None,
    ) -> None:
        self.processes = [*_self(), *processes]
        # This server is about to bind PORT; the OS lists something else too,
        # so the enumeration is never "could not look" by accident.
        self.listening = set(listening or set()) | {(135, 4)}
        self.sessions = list(sessions or [])
        self.alive_pids = {item.pid for item in self.processes}
        self.allowed = set(allowed_to_stop or ())
        self.stopped: list[tuple[int, ...]] = []
        self.stop_times: list[float] = []
        self.info: list[str] = []
        self.warnings: list[str] = []
        self.console: list[str] = []
        self.clock = Clock()
        self.stopping = False
        self.looks = 0
        self.on_look: Callable[[Machine], None] = lambda _machine: None

    def alive(self, pid: int) -> bool:
        self.looks += 1
        self.on_look(self)
        return pid in self.alive_pids

    def stop_chain(self, chain: ProcessChain) -> bool:
        pids = tuple(member.pid for member in chain.members)
        out_of_scope = set(pids) - self.allowed
        if out_of_scope:
            pytest.fail(
                f"the cleanup tried to stop {sorted(out_of_scope)}, out of scope"
            )
        self.stopped.append(pids)
        self.stop_times.append(self.clock.now)
        self.alive_pids -= set(pids)
        return True

    def table(self) -> list[ProcessFacts]:
        return [item for item in self.processes if item.pid in self.alive_pids]

    def world(self) -> CleanupWorld:
        return CleanupWorld(
            listening=lambda: frozenset(self.listening),
            processes=self.table,
            sessions=lambda: list(self.sessions),
            alive=self.alive,
            stop_chain=self.stop_chain,
            info=self.info.append,
            warning=self.warnings.append,
            console=self.console.append,
            stopping=lambda: self.stopping,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def run(self, *, stop_wait: float = 24.0) -> dict[str, Any]:
        request = CleanupRequest(
            port=PORT, stop_wait_seconds=stop_wait, version=VERSION
        )
        plan = plan_old_server_cleanup(
            request,
            processes=self.table(),
            endpoints=frozenset(self.listening),
            sessions=list(self.sessions),
            self_pid=SELF_PID,
            now=NOW,
        )
        return run_old_server_cleanup(plan, self.world())


def _stopped_pids(machine: Machine) -> set[int]:
    return {pid for pids in machine.stopped for pid in pids}


def _zombie_machine(**sessions: Any) -> Machine:
    """The 2026-09-28 shape: 101 -> 102 lost its listener on PORT, kept running."""

    return Machine(
        _launch(101, 102),
        sessions=[_session(102, PORT, **sessions)],
        allowed_to_stop={101, 102},
    )


# ================================================================== the table


def test_a_listener_less_server_of_this_port_and_folder_is_stopped() -> None:
    machine = _zombie_machine()
    report = machine.run()
    assert machine.stopped == [(101, 102)]
    assert machine.stop_times == [pytest.approx(24.0, abs=0.6)]
    assert report["servers"][0]["stopped"] is True
    assert report["gone_pids"] == [101, 102]


def test_a_listener_less_server_of_another_port_is_untouched() -> None:
    machine = Machine(
        _launch(201, 202),
        sessions=[_session(202, OTHER_PORT)],
        allowed_to_stop=set(),
    )
    report = machine.run()
    assert machine.stopped == []
    assert report["servers"] == []
    assert report["left_alone"][0]["pids"] == [201, 202]
    assert any(
        "201, 202" in line and f"recorded port {OTHER_PORT}" in line
        for line in machine.info
    )


def test_a_listener_less_server_of_another_folder_is_untouched() -> None:
    # It writes its rows into ITS folder's request log, not this one.
    machine = Machine(_launch(301, 302), sessions=[], allowed_to_stop=set())
    report = machine.run()
    assert machine.stopped == []
    assert report["left_alone"] == [
        {
            "pids": [301, 302],
            "kind": SCOPE_NO_RECORD,
            "why": "it has no record in this configuration folder",
        }
    ]


def test_an_answering_server_of_this_port_is_untouched_and_not_reported() -> None:
    # 7.70.0's back-off decides about a server that answers; this never does.
    machine = Machine(
        _launch(401, 402),
        listening={(PORT, 402)},
        sessions=[_session(402, PORT)],
        allowed_to_stop=set(),
    )
    report = machine.run()
    assert machine.stopped == []
    assert report["servers"] == []
    assert report["left_alone"] == []
    assert machine.info == []


def test_an_older_version_without_a_listener_is_stopped_and_named() -> None:
    machine = _zombie_machine(version="7.61.0")
    machine.run()
    assert machine.stopped == [(101, 102)]
    assert len(machine.warnings) == 1
    line = machine.warnings[0]
    assert "pid 101, 102" in line
    assert f"version 7.61.0, older than this server's {VERSION}" in line
    assert "owned no listening socket" in line
    assert "did not exit by itself within 24 s" in line
    assert "stopped by process id, innermost first" in line
    assert machine.console[-1] == line


def test_a_row_without_a_version_is_named_as_older_than_the_first_recorder() -> None:
    machine = _zombie_machine()
    machine.run()
    assert (
        f"version unknown, older than {SESSION_VERSION_SINCE}, the first version "
        "that records it" in machine.warnings[0]
    )


def test_an_older_version_that_holds_the_port_is_left_to_the_update() -> None:
    # The holder the installer is replacing: it listens, so the normal
    # stop-holder -> swap -> start flow owns it, never this cleanup.
    machine = Machine(
        _launch(501, 502),
        listening={(PORT, 502)},
        sessions=[_session(502, PORT, version="7.61.0")],
        allowed_to_stop=set(),
    )
    machine.run()
    assert machine.stopped == []


@pytest.mark.parametrize(
    ("facts", "row", "said"),
    [
        (_launch(601, 602), _session(602, None), "names no port"),
        (_launch(601, 602, started_at=None), _session(602, PORT), "no start time"),
    ],
)
def test_ambiguous_evidence_is_untouched_and_reported(
    facts: list[ProcessFacts], row: ServerSession, said: str
) -> None:
    machine = Machine(facts, sessions=[row], allowed_to_stop=set())
    report = machine.run()
    assert machine.stopped == []
    assert report["left_alone"][0]["kind"] == SCOPE_UNPROVEN
    lines = [line for line in machine.info if "601, 602" in line]
    assert len(lines) == 1
    assert said in lines[0] and lines[0].startswith("Left alone:")


def test_nothing_that_is_not_an_mcc_server_is_touched() -> None:
    # Both carry this port's number in a session row of this folder.
    machine = Machine(
        [_agent_launcher(701), _stranger(801), *_launch(101, 102)],
        sessions=[
            _session(701, PORT, row=1),
            _session(801, PORT, row=2),
            _session(102, PORT, row=3),
        ],
        allowed_to_stop={101, 102},
    )
    machine.run()
    assert _stopped_pids(machine) == {101, 102}


def test_a_server_that_started_after_this_one_is_untouched() -> None:
    machine = Machine(
        _launch(901, 902, started_at=SELF_STARTED + 1),
        sessions=[_session(902, PORT, started_at=SELF_STARTED + 3)],
        allowed_to_stop=set(),
    )
    report = machine.run()
    assert machine.stopped == []
    assert report["left_alone"][0]["kind"] == SCOPE_NEWER


def test_a_reused_pid_of_another_folders_server_is_untouched() -> None:
    # This folder's log says pid 102 served PORT -- a month ago. The pid now
    # belongs to a server started an hour ago with no row here.
    machine = Machine(
        _launch(101, 102),
        sessions=[_session(102, PORT, started_at=ZOMBIE_STARTED - 30 * 86_400)],
        allowed_to_stop=set(),
    )
    machine.run()
    assert machine.stopped == []


def test_this_servers_own_launch_is_never_touched() -> None:
    machine = Machine([], sessions=[_session(SELF_PID, PORT)], allowed_to_stop=set())
    report = machine.run()
    assert machine.stopped == []
    assert report["left_alone"] == []


# ===================================================== R2: exit by itself first


def test_one_that_exits_by_itself_is_not_stopped() -> None:
    machine = _zombie_machine()

    def exits_at_six_seconds(machine: Machine) -> None:
        if machine.clock.now >= 6.0:
            machine.alive_pids -= {101, 102}

    machine.on_look = exits_at_six_seconds
    report = machine.run()
    assert machine.stopped == []
    assert report["servers"][0]["exited_by_itself"] is True
    assert report["gone_pids"] == [101, 102]
    assert any("finished and exited by itself" in line for line in machine.info)
    assert machine.clock.now == pytest.approx(6.0, abs=0.6)


def test_one_that_opens_a_listener_again_is_kept() -> None:
    machine = _zombie_machine()

    def rebinds(machine: Machine) -> None:
        if machine.clock.now >= 4.0:
            machine.listening.add((19999, 102))

    machine.on_look = rebinds
    report = machine.run()
    assert machine.stopped == []
    assert report["servers"][0]["kept"] is True


def test_a_pid_whose_process_changed_since_the_first_look_is_never_stopped() -> None:
    # 102 exits during the wait and its pid goes to a new process: same pid,
    # another start time. The fresh table must not hand it to the stop.
    machine = Machine(
        _launch(101, 102),
        sessions=[_session(102, PORT)],
        allowed_to_stop={101},
    )

    def replaced(machine: Machine) -> None:
        if machine.clock.now >= 10.0 and all(
            item.started_at != SELF_STARTED + 30 for item in machine.processes
        ):
            machine.processes = [
                item
                if item.pid != 102
                else ProcessFacts(
                    pid=102,
                    parent_pid=101,
                    image="python.exe",
                    executable=_TOOL_PYTHON,
                    command=f'"{_TOOL_PYTHON}" -m my_claude_code.cli.entrypoints',
                    started_at=SELF_STARTED + 30,
                )
                for item in machine.processes
            ]

    machine.on_look = replaced
    machine.run()
    assert machine.stopped == [(101,)]


def test_the_stop_is_by_exact_pid_innermost_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[int] = []

    def record(pid: int, *, grace: float = 3.0) -> bool:
        del grace
        if pid not in {101, 102}:
            pytest.fail(f"stop_process asked to stop {pid}, out of scope")
        order.append(pid)
        return True

    monkeypatch.setattr(mcc_processes, "stop_process", record)
    machine = _zombie_machine()
    world = machine.world()
    world.stop_chain = mcc_processes.stop_chain
    request = CleanupRequest(port=PORT, stop_wait_seconds=24.0, version=VERSION)
    plan = plan_old_server_cleanup(
        request,
        processes=machine.table(),
        endpoints=frozenset(machine.listening),
        sessions=list(machine.sessions),
        self_pid=SELF_PID,
        now=NOW,
    )
    run_old_server_cleanup(plan, world)
    assert order == [102, 101]


def test_every_wait_is_bounded() -> None:
    # A candidate that never exits and a stop that does not take: the cleanup
    # still ends at the 24 s budget on its own clock, and says so.
    machine = _zombie_machine()

    def refuses_to_die(chain: ProcessChain) -> bool:
        machine.stopped.append(tuple(member.pid for member in chain.members))
        return False

    world = machine.world()
    world.stop_chain = refuses_to_die
    request = CleanupRequest(port=PORT, stop_wait_seconds=24.0, version=VERSION)
    plan = plan_old_server_cleanup(
        request,
        processes=machine.table(),
        endpoints=frozenset(machine.listening),
        sessions=list(machine.sessions),
        self_pid=SELF_PID,
        now=NOW,
    )
    report = run_old_server_cleanup(plan, world)
    assert machine.clock.now == pytest.approx(24.0, abs=0.6)
    assert machine.looks < 200
    assert report["servers"][0]["stopped"] is False
    assert any("Could not fully stop" in line for line in machine.warnings)


def test_this_server_stopping_ends_the_wait_and_stops_nothing() -> None:
    machine = _zombie_machine()

    def stop_at_three(machine: Machine) -> None:
        if machine.clock.now >= 3.0:
            machine.stopping = True

    machine.on_look = stop_at_three
    report = machine.run()
    assert machine.stopped == []
    assert report["aborted"] == "this server began stopping"
    assert machine.clock.now < 5.0


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ("no_sockets", "the listening sockets could not be listed"),
        ("no_processes", "the process table could not be read"),
        ("no_own_start", "this server's own start time could not be read"),
    ],
)
def test_could_not_look_is_never_nothing_there(change: str, said: str) -> None:
    machine = _zombie_machine()
    machine.allowed = set()
    processes = machine.table()
    endpoints = frozenset(machine.listening)
    if change == "no_sockets":
        endpoints = frozenset()
    elif change == "no_processes":
        processes = []
    else:
        processes = [
            item
            if item.pid not in {SELF_ROOT, SELF_PID}
            else ProcessFacts(
                pid=item.pid,
                parent_pid=item.parent_pid,
                image=item.image,
                executable=item.executable,
                command=item.command,
            )
            for item in processes
        ]
    plan = plan_old_server_cleanup(
        CleanupRequest(port=PORT, version=VERSION),
        processes=processes,
        endpoints=endpoints,
        sessions=list(machine.sessions),
        self_pid=SELF_PID,
        now=NOW,
    )
    report = run_old_server_cleanup(plan, machine.world())
    assert machine.stopped == []
    assert said in report["skipped"]
    assert any(said in line and "Nothing was stopped" in line for line in machine.info)


def test_the_tables_are_read_again_before_any_stop() -> None:
    machine = _zombie_machine()
    world = machine.world()
    reads = {"count": 0}

    def unreadable() -> list[ProcessFacts]:
        reads["count"] += 1
        return []

    world.processes = unreadable
    request = CleanupRequest(port=PORT, stop_wait_seconds=24.0, version=VERSION)
    plan = plan_old_server_cleanup(
        request,
        processes=machine.table(),
        endpoints=frozenset(machine.listening),
        sessions=list(machine.sessions),
        self_pid=SELF_PID,
        now=NOW,
    )
    report = run_old_server_cleanup(plan, world)
    assert reads["count"] == 1
    assert machine.stopped == []
    assert "could not be read again" in report["aborted"]


def test_the_found_line_says_what_will_happen_on_the_console_too() -> None:
    machine = _zombie_machine(version="7.61.0")
    machine.run()
    found = machine.console[0]
    assert found.startswith(f"Found 1 old My Claude Code server of port {PORT}")
    assert "pid 101, 102" in found and "given up to 24 s" in found
    assert found in machine.info


def test_the_cleanup_never_signals_a_pid_to_ask_if_it_is_alive() -> None:
    # os.kill(pid, 0) is TerminateProcess on Windows. The only stop in the
    # module is core.mcc_processes.stop_chain, through the world.
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


# ============================================================ the start wiring


def _settings(**values: Any) -> Any:
    from my_claude_code.config.settings import Settings

    # ``model_validate`` reads no environment, so the shell's own PORT cannot
    # leak in; ``port`` has no alias, so it is set as the field itself.
    settings = Settings.model_validate(
        {"SERVER_GRACEFUL_SHUTDOWN_SECONDS": 20.0, **values}
    )
    return settings.model_copy(update={"port": PORT})


def _run_start_survey(
    monkeypatch: pytest.MonkeyPatch,
    machine: Machine,
    settings: Any,
    *,
    takeover: bool = True,
) -> tuple[list[list[Any]], list[Any]]:
    """Run the start-time survey's thread body against ``machine``."""

    surveys: list[list[Any]] = []
    sweeps: list[Any] = []
    targets: list[Callable[[], None]] = []

    class Thread:
        def __init__(self, *, target: Callable[[], None], **_kwargs: Any) -> None:
            targets.append(target)

        def start(self) -> None:
            return None

    monkeypatch.setattr(commands.threading, "Thread", Thread)
    monkeypatch.setattr(commands, "os", _FakeOs(SELF_PID))
    monkeypatch.setattr(commands, "_port_takeover_allowed", takeover)
    monkeypatch.setattr(commands, "package_version", lambda: VERSION)
    monkeypatch.setattr(
        commands, "write_survey", lambda _path, items: surveys.append(list(items))
    )

    def sweep(observations: list[Any], **_kwargs: Any) -> list[Any]:
        sweeps.append(list(observations))
        return []

    monkeypatch.setattr(commands, "stop_stale_servers", sweep)
    rescue.set_cleanup_world_factory(lambda _path: machine.world())
    commands._survey_other_servers(settings)
    assert len(targets) == 1
    targets[0]()
    return surveys, sweeps


class _FakeOs:
    """``commands.os`` with this test's pid; everything else is the real one."""

    def __init__(self, pid: int) -> None:
        import os

        self._os = os
        self._pid = pid

    def getpid(self) -> int:
        return self._pid

    def __getattr__(self, name: str) -> Any:
        return getattr(self._os, name)


def test_a_start_stops_the_old_server_of_its_port_and_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _zombie_machine(version="7.61.0")
    surveys, _ = _run_start_survey(monkeypatch, machine, _settings())
    assert machine.stopped == [(101, 102)]
    # The survey is written at once (the old server listed), then again
    # without it, so the desktop status and the dashboard never name a server
    # that is gone.
    assert [item.pids for item in surveys[0]] == [(101, 102)]
    assert surveys[-1] == []


def test_report_does_not_turn_the_users_decision_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A first start writes every default into .env, so the user's file says
    # ``report``: the decided behaviour must not depend on that changing.
    machine = _zombie_machine()
    _run_start_survey(
        monkeypatch, machine, _settings(SERVER_STALE_SERVER_ACTION="report")
    )
    assert machine.stopped == [(101, 102)]


def test_a_desktop_started_server_stops_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``--no-port-takeover``: the desktop app's rescue is the only thing that
    # stops an old server there.
    machine = _zombie_machine()
    machine.allowed = set()
    surveys, _ = _run_start_survey(monkeypatch, machine, _settings(), takeover=False)
    assert machine.stopped == []
    assert machine.info == [] and machine.warnings == []
    assert len(surveys) == 1


def test_the_opt_in_sweep_never_gets_an_old_server_of_this_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _zombie_machine()
    _, sweeps = _run_start_survey(
        monkeypatch, machine, _settings(SERVER_STALE_SERVER_ACTION="stop")
    )
    assert len(sweeps) == 1
    assert all(not {101, 102} & set(item.pids) for item in sweeps[0])
    assert machine.stopped == [(101, 102)]


def test_the_cleanup_waits_the_servers_own_stop_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SERVER_GRACEFUL_SHUTDOWN_SECONDS + the stop margins, as the rescue, the
    # tray and the installer wait: 20 + 3 + 1 at these settings.
    machine = _zombie_machine()
    _run_start_survey(monkeypatch, machine, _settings())
    assert machine.stop_times == [pytest.approx(24.0, abs=0.6)]


def test_the_test_suite_never_reaches_the_real_machine() -> None:
    # The autouse reset in tests/conftest.py: the world a start would use in a
    # test lists nothing and refuses to stop.
    world = rescue.cleanup_world(Path("unused.db"))
    assert world.processes() == []
    assert world.listening() == frozenset()
    from tests.support.token_host_block import HermeticityViolation

    facts = _launch(101, 102)
    with pytest.raises(HermeticityViolation):
        world.stop_chain(ProcessChain(root=facts[0], members=tuple(facts)))
