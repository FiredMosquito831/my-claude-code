"""What MCC may conclude about another server, and what it may never do.

The binding rule, from the user on 2026-09-11 after they read an earlier draft
of this feature: *"don't kill currently running servers -- I have servers
running with agents on WAIT."* Two ``mcc-server`` launches on their machine
owned no listening socket, had been running for twenty-five hours, and were
serving work the whole time. Every test below exists to make sure a future
change cannot quietly reintroduce "owns no socket, therefore finished".

The decoy in most of these tests is a heartbeating MCC server with no listening
socket. It is exactly the shape of the two real ones, and it must survive every
path in this module.
"""

import json
import sqlite3
import time
from pathlib import Path

from my_claude_code.core.mcc_processes import ProcessFacts
from my_claude_code.core.request_log import ServerSession
from my_claude_code.core.server_inventory import (
    STATUS_LIVE,
    STATUS_SERVING,
    STATUS_STALE,
    STATUS_UNKNOWN,
    ServerObservation,
    observe_servers,
    read_survey,
    write_survey,
)

NOW = 1_700_000_000.0
_TOOL_PYTHON = r"C:\Users\x\AppData\Roaming\uv\tools\my-claude-code\Scripts\python.exe"


def _launch(root_pid: int, leaf_pid: int, *, started_at: float) -> list[ProcessFacts]:
    """The two processes of one ``mcc-server`` launch, trampoline and server."""

    command = f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-server.exe"'
    return [
        ProcessFacts(
            pid=root_pid,
            parent_pid=1,
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
            command=command,
            started_at=started_at,
        ),
    ]


def _decoy_launcher() -> ProcessFacts:
    """A coding agent, which must be invisible to every function here."""

    return ProcessFacts(
        pid=59780,
        parent_pid=1,
        image="python.exe",
        executable=_TOOL_PYTHON,
        command=f'"{_TOOL_PYTHON}" "C:\\Users\\x\\.local\\bin\\mcc-claude.exe"',
        started_at=NOW - 90_000,
    )


def _decoy_python() -> ProcessFacts:
    """An unrelated python the user is running. Never ours, never touched."""

    return ProcessFacts(
        pid=4242,
        parent_pid=1,
        image="python.exe",
        executable=r"C:\Python314\python.exe",
        command="python train.py",
        started_at=NOW - 90_000,
    )


def _observe(
    *,
    processes: list[ProcessFacts] | None = None,
    listening: frozenset[int] = frozenset(),
    sessions: list[ServerSession] | None = None,
    self_pid: int | None = None,
) -> list[ServerObservation]:
    """Drive the classifier off a fabricated machine and a fabricated log."""

    return observe_servers(
        request_log_path=Path("does-not-exist.db"),
        self_pid=self_pid,
        processes=processes,
        listening=listening,
        sessions=sessions if sessions is not None else [],
        now=NOW,
    )


# ------------------------------------------------------- the binding invariant


def test_a_heartbeating_server_with_no_socket_is_live_and_never_actionable() -> None:
    """THE regression test. This is the machine of 2026-09-10, exactly.

    A server with no listening socket, twenty-five hours old, heartbeating
    seconds ago. Under the design this replaced it was "abandoned" and would
    have been stopped, with the user's agents on it.
    """

    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        sessions=[
            ServerSession(
                id=232,
                pid=63484,
                started_at=NOW - 90_000,
                last_seen_at=NOW - 6,
                host="0.0.0.0",
                port=8082,
            )
        ],
    )
    assert [item.status for item in observations] == [STATUS_LIVE]
    assert not observations[0].is_actionable


def test_a_quiet_server_is_left_alone_while_nothing_else_claims_its_port() -> None:
    """Silence alone is never enough. It only makes the word available."""

    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        sessions=[
            ServerSession(
                id=232,
                pid=63484,
                started_at=NOW - 90_000,
                last_seen_at=NOW - 90_000,
                host="0.0.0.0",
                port=8082,
            )
        ],
    )
    assert observations[0].status == STATUS_UNKNOWN
    assert not observations[0].is_actionable
    assert "nothing else claims its port" in observations[0].reason


def test_a_session_with_no_recorded_port_can_never_be_proven_stale() -> None:
    """Every row written before 6.72.2 is one of these, including the two real ones."""

    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        sessions=[
            ServerSession(
                id=232, pid=63484, started_at=NOW - 90_000, last_seen_at=NOW - 90_000
            )
        ],
    )
    assert not observations[0].is_actionable


def test_a_second_instance_on_another_port_is_serving_not_stale() -> None:
    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        listening=frozenset({63484}),
        sessions=[
            ServerSession(
                id=232,
                pid=63484,
                started_at=NOW - 90_000,
                last_seen_at=NOW - 90_000,
                host="127.0.0.1",
                port=9999,
            )
        ],
    )
    assert observations[0].status == STATUS_SERVING
    assert not observations[0].is_actionable


def test_an_unreadable_socket_table_never_makes_a_server_actionable() -> None:
    """``listening_pids`` returns an empty set when neither tool can be run.

    That is indistinguishable from "nothing is listening", so it must not be
    the evidence that convicts anybody. Here the heartbeat is what saves the
    server, and it has to be enough on its own.
    """

    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        listening=frozenset(),
        sessions=[
            ServerSession(
                id=232,
                pid=63484,
                started_at=NOW - 90_000,
                last_seen_at=NOW - 1,
                host="0.0.0.0",
                port=8082,
            )
        ],
    )
    assert observations[0].status == STATUS_LIVE


# --------------------------------------------------- what CAN be proven stale


def test_a_superseded_server_is_stale_only_once_something_else_serves_its_port() -> (
    None
):
    processes = [
        *_launch(6764, 63484, started_at=NOW - 90_000),
        *_launch(55192, 35280, started_at=NOW - 300),
    ]
    sessions = [
        ServerSession(
            id=232,
            pid=63484,
            started_at=NOW - 90_000,
            last_seen_at=NOW - 90_000,
            host="0.0.0.0",
            port=8082,
        ),
        ServerSession(
            id=237,
            pid=35280,
            started_at=NOW - 300,
            last_seen_at=NOW - 5,
            host="0.0.0.0",
            port=8082,
        ),
    ]
    observations = _observe(
        processes=processes, listening=frozenset({35280}), sessions=sessions
    )
    by_pid = {item.pids[0]: item for item in observations}
    assert by_pid[6764].status == STATUS_STALE
    assert by_pid[6764].is_actionable
    assert "superseded" in by_pid[6764].reason
    # And the one actually serving is never touched.
    assert by_pid[55192].status == STATUS_SERVING
    assert not by_pid[55192].is_actionable


def test_a_launcher_whose_server_is_gone_is_a_provable_orphan() -> None:
    """The trampoline survived; the process it existed to run did not."""

    orphan = _launch(6764, 63484, started_at=NOW - 90_000)[:1]
    observations = _observe(processes=orphan)
    assert observations[0].status == STATUS_STALE
    assert "supervises nothing" in observations[0].reason


def test_a_launch_still_in_progress_is_not_an_orphan() -> None:
    """A start that has not reached its interpreter yet looks identical."""

    young = _launch(6764, 63484, started_at=NOW - 5)[:1]
    observations = _observe(processes=young)
    assert observations[0].status == STATUS_UNKNOWN
    assert not observations[0].is_actionable


# ------------------------------------------------------------- what is ignored


def test_coding_agents_and_strangers_are_not_observed_at_all() -> None:
    observations = _observe(processes=[_decoy_launcher(), _decoy_python()])
    assert observations == []


def test_this_process_is_never_reported_about_itself() -> None:
    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000), self_pid=63484
    )
    assert observations == []


# --------------------------------------------------------------- the survey file


def test_the_survey_round_trips_and_carries_the_status(tmp_path: Path) -> None:
    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        sessions=[
            ServerSession(
                id=232,
                pid=63484,
                started_at=NOW - 90_000,
                last_seen_at=NOW - 6,
                host="0.0.0.0",
                port=8082,
            )
        ],
    )
    target = tmp_path / "other-servers.json"
    write_survey(target, observations)
    entries = read_survey(target)
    assert len(entries) == 1
    assert entries[0]["status"] == STATUS_LIVE
    assert entries[0]["pids"] == [6764, 63484]
    assert entries[0]["port"] == 8082


def test_a_stale_survey_reads_as_no_information(tmp_path: Path) -> None:
    target = tmp_path / "other-servers.json"
    target.write_text(
        json.dumps({"at": time.time() - 86_400, "servers": [{"status": "live"}]}),
        encoding="utf-8",
    )
    assert read_survey(target) == []


def test_a_missing_or_corrupt_survey_is_not_an_error(tmp_path: Path) -> None:
    assert read_survey(tmp_path / "nothing.json") == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_survey(broken) == []


# ------------------------------------------------- the session table, read-only


def test_sessions_are_read_through_a_read_only_handle(tmp_path: Path) -> None:
    """The hermetic guard: reading somebody else's log never writes to it."""

    from my_claude_code.core.request_log import read_server_sessions

    db = tmp_path / "requests.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE server_sessions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL,"
        " last_seen_at REAL NOT NULL, pid INTEGER, host TEXT, port INTEGER)"
    )
    conn.execute(
        "INSERT INTO server_sessions (started_at, last_seen_at, pid, host, port)"
        " VALUES (?, ?, ?, ?, ?)",
        (NOW - 100, NOW - 5, 63484, "0.0.0.0", 8082),
    )
    conn.commit()
    conn.close()

    before = db.stat().st_mtime_ns
    sessions = read_server_sessions(db)
    assert [(item.pid, item.port) for item in sessions] == [(63484, 8082)]
    assert db.stat().st_mtime_ns == before
    assert not (tmp_path / "requests.db-journal").exists()


def test_a_log_written_before_the_address_columns_still_reads(tmp_path: Path) -> None:
    from my_claude_code.core.request_log import read_server_sessions

    db = tmp_path / "requests.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE server_sessions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL,"
        " last_seen_at REAL NOT NULL, pid INTEGER)"
    )
    conn.execute(
        "INSERT INTO server_sessions (started_at, last_seen_at, pid) VALUES (?, ?, ?)",
        (NOW - 100, NOW - 5, 63484),
    )
    conn.commit()
    conn.close()

    sessions = read_server_sessions(db)
    assert len(sessions) == 1
    assert sessions[0].port is None
    assert sessions[0].host is None


# ----------------------------------------------------------- the opt-in stop


def test_the_stop_path_signals_only_the_provably_stale_chain(monkeypatch) -> None:
    """A heartbeating decoy server must survive even the opt-in sweep.

    This is the last line of defence: ``stop_stale_servers`` is the only
    function in the product that signals another server, and it must be
    incapable of reaching anything that is not ``stale`` -- however the caller
    filters, and whatever else is on the machine.
    """

    from my_claude_code.core import server_inventory

    processes = [
        # Superseded: quiet, and its port now belongs to the one below.
        *_launch(6764, 63484, started_at=NOW - 90_000),
        # The decoy: no listening socket, heartbeating seconds ago.
        *_launch(16884, 37728, started_at=NOW - 90_000),
        # The current server.
        *_launch(55192, 35280, started_at=NOW - 300),
        _decoy_launcher(),
        _decoy_python(),
    ]
    sessions = [
        ServerSession(
            id=232,
            pid=63484,
            started_at=NOW - 90_000,
            last_seen_at=NOW - 90_000,
            host="0.0.0.0",
            port=8082,
        ),
        ServerSession(
            id=233,
            pid=37728,
            started_at=NOW - 90_000,
            last_seen_at=NOW - 3,
            host="0.0.0.0",
            port=8082,
        ),
        ServerSession(
            id=237,
            pid=35280,
            started_at=NOW - 300,
            last_seen_at=NOW - 5,
            host="0.0.0.0",
            port=8082,
        ),
    ]
    observations = _observe(
        processes=processes, listening=frozenset({35280}), sessions=sessions
    )
    by_root = {item.pids[0]: item.status for item in observations}
    assert by_root == {
        6764: STATUS_STALE,
        16884: STATUS_LIVE,
        55192: STATUS_SERVING,
    }

    signalled: list[tuple[int, ...]] = []

    def record(chain, **_kwargs) -> bool:
        signalled.append(chain.pids)
        return True

    monkeypatch.setattr(server_inventory, "stop_chain", record)
    stopped = server_inventory.stop_stale_servers(observations, processes=processes)

    assert signalled == [(6764, 63484)]
    assert [item.pids for item in stopped] == [(6764, 63484)]
    # Spelled out, because this is the sentence the user wrote the rule for.
    every_signalled_pid = {pid for pids in signalled for pid in pids}
    assert 37728 not in every_signalled_pid  # the heartbeating decoy server
    assert 35280 not in every_signalled_pid  # the server that is serving
    assert 59780 not in every_signalled_pid  # the user's coding agent
    assert 4242 not in every_signalled_pid  # the user's unrelated python


def test_nothing_is_stopped_when_nothing_is_stale(monkeypatch) -> None:
    from my_claude_code.core import server_inventory

    def explode(chain, **_kwargs) -> bool:
        raise AssertionError(f"stop_chain must not be called, got {chain.pids}")

    monkeypatch.setattr(server_inventory, "stop_chain", explode)
    observations = _observe(
        processes=_launch(6764, 63484, started_at=NOW - 90_000),
        sessions=[
            ServerSession(
                id=232,
                pid=63484,
                started_at=NOW - 90_000,
                last_seen_at=NOW - 6,
                host="0.0.0.0",
                port=8082,
            )
        ],
    )
    assert server_inventory.stop_stale_servers(observations) == []
