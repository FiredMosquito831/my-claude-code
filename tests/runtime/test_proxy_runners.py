"""7.108.0 PR-R2: the runner supervisor, with real child processes.

The "wireproxy" is ``sys.executable`` running ``tests/support/fake_wireproxy.py``
(a SOCKS5 server with an RFC 1929 login, TCP tunnels and ``/readyz``). What is
pinned: start -> ready; a crash with automatic restart off stays stopped with
its exit code, and with it on climbs the back-off ladder to ``crashed``; a stop
kills only the recorded pid; a stale ``pid.json`` whose creation time differs
is NOT killed (and one that matches is); the private key never reaches the log;
the shutdown finishes inside its bound; on Windows, a server killed hard takes
its runner with it (Job Object).
"""

import json
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from my_claude_code.application.proxy_runner_sources import (
    RunnerEdit,
    save_runner_source,
)
from my_claude_code.application.proxy_runner_state import (
    STATE_CRASHED,
    STATE_READY,
    STATE_STOPPED,
    RunnerHub,
    RunnerReading,
)
from my_claude_code.cli.process_registry import kill_pid_tree_best_effort
from my_claude_code.config.proxy_chains import ProxyChains
from my_claude_code.config.proxy_runner_bins import BinaryRefusal, ResolvedBinary
from my_claude_code.config.proxy_sources import (
    ProxySources,
    current_proxy_sources,
    save_proxy_sources,
)
from my_claude_code.runtime.proxy_runner_process import creation_token, popen_flags
from my_claude_code.runtime.proxy_runners import (
    CONFIG_FILENAME,
    LOG_FILENAME,
    PID_FILENAME,
    ProxyRunnerSupervisor,
)

pytestmark = pytest.mark.local_serial

SUPPORT = Path(__file__).resolve().parents[1] / "support"
FAKE = SUPPORT / "fake_wireproxy.py"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wireguard"
PROTON = (FIXTURES / "wg-NL-FREE-12.conf").read_text("utf-8")
KEY = next(
    line.split("=", 1)[1].strip()
    for line in PROTON.splitlines()
    if line.startswith("PrivateKey")
)
WAIT = 20.0


def _free_ports(count: int) -> list[int]:
    held = []
    for _ in range(count):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        held.append(probe)
    ports = [probe.getsockname()[1] for probe in held]
    for probe in held:
        probe.close()
    return ports


def _fake() -> ResolvedBinary:
    return ResolvedBinary(Path(sys.executable), "test", "1.1.3", leading=(str(FAKE),))


class _Rig:
    """A store with one Proton-shaped runner, a supervisor, and its records."""

    def __init__(self, tmp_path: Path, *, auto_restart: bool = False) -> None:
        self.hub = RunnerHub()
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self.killed: list[int] = []
        ports = _free_ports(2)
        allowed = set(ports)
        _, sources, self.source_id = save_runner_source(
            ProxyChains(),
            ProxySources(),
            "",
            RunnerEdit(
                profile_text=PROTON,
                filename="wg-NL-FREE-12.conf",
                auto_restart=auto_restart,
            ),
            port_range=f"{min(ports)}-{max(ports)}",
            port_free=lambda port: port in allowed,
        )
        save_proxy_sources(sources)
        self.base = tmp_path / "runners"
        self.supervisor = ProxyRunnerSupervisor(
            resolve=_fake,
            base_dir=lambda: self.base,
            register=self.registered.append,
            unregister=self.unregistered.append,
            kill_tree=self._kill,
            hub=self.hub,
            backoff=(0.05, 0.05),
            graceful_seconds=2.0,
            ready_poll_seconds=0.05,
        )

    def _kill(self, pid: int) -> None:
        self.killed.append(pid)
        kill_pid_tree_best_effort(pid)

    @property
    def runner(self):
        runner = current_proxy_sources().sources[self.source_id].runner
        assert runner is not None
        return runner

    @property
    def login(self):
        login = current_proxy_sources().secret(self.runner.login)
        assert login is not None
        return login

    def reading(self) -> RunnerReading:
        return self.hub.reading(self.source_id) or RunnerReading()

    def wait_for(self, predicate: Callable[[RunnerReading], bool]) -> RunnerReading:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            reading = self.reading()
            if predicate(reading):
                return reading
            time.sleep(0.05)
        raise AssertionError(f"timed out; last reading {self.reading()}")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[_Rig]:
    made = _Rig(tmp_path)
    yield made
    made.supervisor.stop_all()


def _alive(pid: int) -> bool:
    return creation_token(pid) is not None


def _gone(pid: int, within: float = WAIT) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _echo_server() -> tuple[int, socket.socket]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(1024)
                conn.sendall(b"echo:" + data)

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], server


def _socks_round_trip(port: int, user: str, password: str, target_port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(b"\x05\x01\x02")
        assert conn.recv(2) == b"\x05\x02"
        conn.sendall(
            bytes([1, len(user)])
            + user.encode()
            + bytes([len(password)])
            + password.encode()
        )
        if conn.recv(2) != b"\x01\x00":
            return b"refused"
        host = b"127.0.0.1"
        conn.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(host)])
            + host
            + struct.pack("!H", target_port)
        )
        reply = conn.recv(10)
        assert reply[1] == 0
        conn.sendall(b"hello")
        return conn.recv(1024)


def _decoy(tmp_path: Path) -> subprocess.Popen[bytes]:
    """A process that is not this runner: another fake wireproxy of our own."""

    (port,) = _free_ports(1)
    config = tmp_path / "decoy.conf"
    config.write_text(f"[Socks5]\nBindAddress = 127.0.0.1:{port}\n", encoding="utf-8")
    creationflags, new_session = popen_flags()
    return subprocess.Popen(
        [sys.executable, str(FAKE), "-c", str(config)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=new_session,
    )


# ------------------------------------------------------------------- start


def test_start_runs_it_and_it_turns_ready(rig: _Rig) -> None:
    reading = rig.supervisor.start(rig.source_id)
    assert reading.state == "starting"
    ready = rig.wait_for(lambda item: item.state == STATE_READY)

    pid = ready.pid
    assert pid and rig.registered == [pid]
    assert "Ready since" in ready.sentence
    directory = rig.base / rig.source_id
    recorded = json.loads((directory / PID_FILENAME).read_text("utf-8"))
    assert recorded["pid"] == pid
    assert recorded["created"] == creation_token(pid)
    config = (directory / CONFIG_FILENAME).read_text("utf-8")
    assert f"PrivateKey = {KEY}" in config
    assert f"BindAddress = 127.0.0.1:{rig.runner.socks_port}" in config

    echo_port, server = _echo_server()
    try:
        login = rig.login
        answer = _socks_round_trip(
            rig.runner.socks_port, login.username, login.password, echo_port
        )
        assert answer == b"echo:hello"
        assert (
            _socks_round_trip(rig.runner.socks_port, login.username, "wrong", echo_port)
            == b"refused"
        )
    finally:
        server.close()
    # A second Start leaves the running one alone.
    assert rig.supervisor.start(rig.source_id).pid == pid
    assert rig.registered == [pid]


def test_the_private_key_never_reaches_the_log(rig: _Rig, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_WIREPROXY_PRINT_KEY", "1")
    rig.supervisor.start(rig.source_id)
    rig.wait_for(lambda item: item.state == STATE_READY)
    rig.wait_for(lambda item: any("[redacted]" in line for line in item.log))

    rig.supervisor.stop(rig.source_id)
    log = (rig.base / rig.source_id / LOG_FILENAME).read_text("utf-8")
    assert "[redacted]" in log
    for secret in (KEY, KEY[:8], rig.login.password):
        assert secret not in log
        assert not any(secret in line for line in rig.reading().log)
    assert "exited with code" in log


# ------------------------------------------------------------------ crashes


def test_a_crash_with_automatic_restart_off_stays_stopped_with_its_code(
    rig: _Rig, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_WIREPROXY_EXIT_CODE", "3")
    rig.supervisor.start(rig.source_id)

    stopped = rig.wait_for(
        lambda item: item.state == STATE_STOPPED and item.exit_code == 3
    )

    assert "Restart automatically is off: press Restart" in stopped.sentence
    time.sleep(0.5)
    assert len(rig.registered) == 1
    assert rig.reading().state == STATE_STOPPED
    assert rig.unregistered == rig.registered


def test_a_crash_with_automatic_restart_on_climbs_the_ladder_to_crashed(
    tmp_path: Path, monkeypatch
) -> None:
    made = _Rig(tmp_path, auto_restart=True)
    monkeypatch.setenv("FAKE_WIREPROXY_EXIT_CODE", "7")
    try:
        made.supervisor.start(made.source_id)
        crashed = made.wait_for(lambda item: item.state == STATE_CRASHED)
    finally:
        made.supervisor.stop_all()

    assert crashed.exit_code == 7
    assert crashed.restarts == 2
    assert "after 2 automatic restarts" in crashed.sentence
    assert "until you press Start" in crashed.sentence
    assert len(made.registered) == 3


def test_the_ladder_is_the_spec_s_and_off_by_default() -> None:
    from my_claude_code.application.proxy_runner_state import RESTART_BACKOFF_SECONDS

    assert RESTART_BACKOFF_SECONDS == (1.0, 5.0, 30.0, 300.0)
    assert RunnerEdit().auto_restart is None
    _, sources, source_id = save_runner_source(
        ProxyChains(),
        ProxySources(),
        "",
        RunnerEdit(profile_text=PROTON),
        port_range="41000-41001",
        port_free=lambda port: True,
    )
    runner = sources.sources[source_id].runner
    assert runner is not None and runner.auto_restart is False


# --------------------------------------------------------------- stopping


def test_stop_kills_only_the_recorded_pid(rig: _Rig, tmp_path: Path) -> None:
    decoy = _decoy(tmp_path)
    try:
        rig.supervisor.start(rig.source_id)
        pid = rig.wait_for(lambda item: item.state == STATE_READY).pid

        stopped = rig.supervisor.stop(rig.source_id)

        assert stopped.state == STATE_STOPPED
        assert stopped.sentence == "Stopped by you."
        assert _gone(pid)
        assert set(rig.killed) <= {pid}
        assert _alive(decoy.pid)
        assert (
            not (rig.base / rig.source_id / PID_FILENAME).exists()
            or json.loads((rig.base / rig.source_id / PID_FILENAME).read_text("utf-8"))[
                "pid"
            ]
            != pid
        )
    finally:
        decoy.kill()
        decoy.wait(10)


def test_a_stale_pid_file_with_another_creation_time_is_not_killed(
    rig: _Rig, tmp_path: Path
) -> None:
    decoy = _decoy(tmp_path)
    try:
        directory = rig.base / rig.source_id
        directory.mkdir(parents=True)
        (directory / PID_FILENAME).write_text(
            json.dumps({"pid": decoy.pid, "created": "12345"}), encoding="utf-8"
        )

        rig.supervisor.start(rig.source_id)
        rig.wait_for(lambda item: item.state == STATE_READY)

        assert decoy.pid not in rig.killed
        assert _alive(decoy.pid)
        assert (
            json.loads((directory / PID_FILENAME).read_text("utf-8"))["pid"]
            != decoy.pid
        )
    finally:
        decoy.kill()
        decoy.wait(10)


def test_a_stale_pid_file_whose_pid_and_creation_match_is_reaped(
    rig: _Rig, tmp_path: Path
) -> None:
    orphan = _decoy(tmp_path)
    try:
        directory = rig.base / rig.source_id
        directory.mkdir(parents=True)
        (directory / PID_FILENAME).write_text(
            json.dumps({"pid": orphan.pid, "created": creation_token(orphan.pid)}),
            encoding="utf-8",
        )

        rig.supervisor.start(rig.source_id)

        assert rig.killed[:1] == [orphan.pid]
        assert _gone(orphan.pid)
    finally:
        orphan.kill()
        orphan.wait(10)


def test_shutdown_stops_every_runner_inside_its_bound(tmp_path: Path) -> None:
    first = _Rig(tmp_path / "a")
    first.supervisor.start(first.source_id)
    first.wait_for(lambda item: item.state == STATE_READY)
    second_id = first.source_id
    # A second runner on the same supervisor.
    ports = _free_ports(2)
    allowed = set(ports)
    _, sources, other = save_runner_source(
        ProxyChains(),
        current_proxy_sources(),
        "",
        RunnerEdit(profile_text=PROTON),
        port_range=f"{min(ports)}-{max(ports)}",
        port_free=lambda port: port in allowed,
    )
    save_proxy_sources(sources)
    first.supervisor.start(other)
    deadline = time.monotonic() + WAIT
    while (first.hub.reading(other) or RunnerReading()).state != STATE_READY:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    pids = [
        first.supervisor.running_pid(second_id),
        first.supervisor.running_pid(other),
    ]
    assert all(pids)

    started = time.monotonic()
    first.supervisor.stop_all()
    elapsed = time.monotonic() - started

    assert elapsed < 2 * 2.0 + 1.0
    assert all(_gone(pid, 5.0) for pid in pids)
    assert (
        first.supervisor.start(other).sentence == "Not started: the server is stopping."
    )


# ---------------------------------------------------------------- refusals


def test_a_binary_refusal_starts_nothing(rig: _Rig) -> None:
    rig.supervisor._resolve = lambda: BinaryRefusal("wireproxy is not installed.")

    reading = rig.supervisor.start(rig.source_id)

    assert reading.state == STATE_STOPPED
    assert reading.sentence == "Not started: wireproxy is not installed."
    assert rig.registered == []


def test_a_port_another_program_holds_starts_nothing(rig: _Rig) -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", rig.runner.socks_port))
    holder.listen()
    try:
        reading = rig.supervisor.start(rig.source_id)
    finally:
        holder.close()

    assert reading.state == STATE_STOPPED
    assert f"127.0.0.1:{rig.runner.socks_port}" in reading.sentence
    assert "Pick new ports" in reading.sentence
    assert rig.registered == []


def test_remove_stops_it_and_deletes_its_files(rig: _Rig) -> None:
    rig.supervisor.start(rig.source_id)
    pid = rig.wait_for(lambda item: item.state == STATE_READY).pid

    rig.supervisor.remove(rig.source_id)

    assert _gone(pid)
    assert not (rig.base / rig.source_id).exists()
    assert rig.hub.reading(rig.source_id) is None


def test_restart_runs_a_new_process(rig: _Rig) -> None:
    rig.supervisor.start(rig.source_id)
    first = rig.wait_for(lambda item: item.state == STATE_READY).pid

    rig.supervisor.restart(rig.source_id)
    second = rig.wait_for(
        lambda item: item.state == STATE_READY and item.pid != first
    ).pid

    assert _gone(first)
    assert _alive(second)


# ----------------------------------------------------------- the Job Object


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows-only")
def test_a_server_killed_hard_takes_its_runner_with_it(tmp_path: Path) -> None:
    (port,) = _free_ports(1)
    config = tmp_path / "child.conf"
    config.write_text(f"[Socks5]\nBindAddress = 127.0.0.1:{port}\n", encoding="utf-8")
    parent = subprocess.Popen(
        [sys.executable, str(SUPPORT / "job_parent.py"), str(FAKE), str(config)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    child = 0
    try:
        assert parent.stdout is not None
        child_text, assigned, *problem = parent.stdout.readline().decode().split(" ")
        child = int(child_text)
        assert assigned == "1", problem
        assert _alive(child)

        parent.kill()  # TerminateProcess: no cleanup runs in the parent at all
        parent.wait(10)

        assert _gone(child, 10.0)
    finally:
        if parent.poll() is None:
            parent.kill()
        if child and _alive(child):
            kill_pid_tree_best_effort(child)
