"""Tests for bounded port-availability and ownership diagnostics."""

import socket
import sys

import pytest

from my_claude_code.cli import port_diagnostics
from my_claude_code.cli.port_diagnostics import (
    diagnose_port_owner,
    is_address_in_use,
    probe_port_available,
    wait_for_port_free,
)


def _free_port() -> int:
    """Bind an ephemeral socket and return the port the OS assigned."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_probe_reports_a_free_port_as_available() -> None:
    port = _free_port()
    assert probe_port_available("127.0.0.1", port) is True


@pytest.mark.local_serial
def test_probe_reports_a_listening_port_as_taken() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert probe_port_available("127.0.0.1", port) is False
    finally:
        sock.close()


def test_probe_never_sends_data(monkeypatch) -> None:
    sent: list = []

    class _Socket:
        def __init__(self, *_a, **_k):
            pass

        def settimeout(self, _t):
            pass

        def setsockopt(self, *_a):
            pass

        def bind(self, _addr):
            raise OSError

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _Socket)
    assert probe_port_available("127.0.0.1", 8082) is False
    assert sent == []


@pytest.mark.local_serial
def test_wait_for_port_free_returns_true_once_released() -> None:
    import threading
    import time

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _release_later():
        time.sleep(0.05)
        sock.close()

    threading.Thread(target=_release_later, daemon=True).start()
    assert wait_for_port_free("127.0.0.1", port, timeout=2.0, interval=0.02) is True


def test_wait_for_port_free_times_out_when_held(monkeypatch) -> None:
    monkeypatch.setattr(
        "my_claude_code.cli.port_diagnostics.probe_port_available",
        lambda *a, **k: False,
    )
    assert wait_for_port_free("127.0.0.1", 8082, timeout=0.1, interval=0.02) is False


def _fake_completed_process(stdout: str):
    import subprocess as _sp

    class _Result:
        def __init__(self, stdout: str) -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def _run(*_a, **_k):
        return _Result(stdout)

    return _sp.run, _run


def test_diagnose_parses_ss_output(monkeypatch) -> None:
    import subprocess as _sp

    lines = [
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process",
        'LISTEN 0 128 0.0.0.0:8082 0.0.0.0:* users:(("python3.11",pid=12345,fd=6))',
    ]
    out = chr(10).join(lines)
    orig, fake = _fake_completed_process(out)
    monkeypatch.setattr(_sp, "run", fake)
    owner = diagnose_port_owner("0.0.0.0", 8082)
    monkeypatch.setattr(_sp, "run", orig)
    assert owner is not None
    assert owner.pid == 12345
    assert owner.name == "python3.11"


def test_diagnose_parses_netstat_output(monkeypatch) -> None:
    import subprocess as _sp

    lines = [
        "  Proto  Local Address          Foreign Address        State           PID",
        "  TCP    0.0.0.0:8082           0.0.0.0:0              LISTENING       9999",
    ]
    out = chr(10).join(lines)
    orig, fake = _fake_completed_process(out)
    monkeypatch.setattr(_sp, "run", fake)
    owner = diagnose_port_owner("0.0.0.0", 8082)
    monkeypatch.setattr(_sp, "run", orig)
    assert owner is not None
    assert owner.pid == 9999


def test_diagnose_returns_none_when_no_owner(monkeypatch) -> None:
    import subprocess as _sp

    def _run(*_a, **_k):
        return type(
            "R", (), {"returncode": 0, "stdout": "nothing here", "stderr": ""}
        )()

    monkeypatch.setattr(_sp, "run", _run)
    assert diagnose_port_owner("0.0.0.0", 8082) is None


def test_diagnose_is_best_effort_when_command_missing(monkeypatch) -> None:
    import subprocess as _sp

    def _run(*_a, **_k):
        raise OSError("no such command")

    monkeypatch.setattr(_sp, "run", _run)
    assert diagnose_port_owner("0.0.0.0", 8082) is None


def test_is_address_in_use_detects_eaddrinuse() -> None:
    assert is_address_in_use(OSError(98, "Address already in use")) is True
    assert is_address_in_use(OSError(13, "Permission denied")) is False


@pytest.mark.local_serial
def test_probe_false_on_a_real_held_socket() -> None:
    """A genuinely bound+listening ephemeral socket is reported as taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert probe_port_available("127.0.0.1", port) is False
    finally:
        sock.close()


@pytest.mark.local_serial
def test_wait_for_port_free_times_out_on_a_real_held_socket() -> None:
    """Holding a real ephemeral socket makes wait_for_port_free give up."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert (
            wait_for_port_free("127.0.0.1", port, timeout=0.2, interval=0.02) is False
        )
    finally:
        sock.close()


@pytest.mark.local_serial
def test_wait_for_port_free_true_after_a_real_release() -> None:
    """Releasing a real socket lets wait_for_port_free return promptly."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    import threading
    import time

    def _release_later() -> None:
        time.sleep(0.05)
        sock.close()

    threading.Thread(target=_release_later, daemon=True).start()
    assert wait_for_port_free("127.0.0.1", port, timeout=2.0, interval=0.02) is True


@pytest.mark.local_serial
def test_probe_reports_a_port_in_time_wait_as_available() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = listener.accept()
    served.close()
    client.close()
    listener.close()

    assert probe_port_available("127.0.0.1", port) is True


# ------------------------------------------- #284: the probe's platform branch
#
# Off Windows the probe sets ``SO_REUSEADDR`` before it binds, as the POSIX
# listener in ``commands._bind_listening_socket`` does, so a port that holds
# only TIME_WAIT entries reads as free -- the listener can bind it. On Windows
# it sets nothing: there ``SO_REUSEADDR`` lets a second socket bind over a live
# ``SO_REUSEADDR`` listener (measured 2026-10-24), so a reuse probe would call
# a held port free, and TIME_WAIT never blocks a plain bind there anyway. The
# test above passes on Windows with or without the change; the tests below are
# the ones that fail when either half of the branch is lost.


class _RecordingSocket:
    """Stands in for ``socket.socket``: records each call the probe makes."""

    def __init__(self, calls: list[tuple[object, ...]]) -> None:
        self._calls = calls

    def settimeout(self, timeout: float) -> None:
        self._calls.append(("settimeout", timeout))

    def setsockopt(self, *args: object) -> None:
        self._calls.append(("setsockopt", *args))

    def bind(self, address: tuple[str, int]) -> None:
        self._calls.append(("bind", address))

    def close(self) -> None:
        self._calls.append(("close",))


def _probe_calls(monkeypatch, platform: str) -> list[tuple[object, ...]]:
    """Run the probe with ``_PLATFORM`` pinned and return what it did."""
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(port_diagnostics, "_PLATFORM", platform)
    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: _RecordingSocket(calls))
    assert probe_port_available("127.0.0.1", 18082) is True
    return calls


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_the_posix_probe_sets_reuseaddr_before_it_binds(
    monkeypatch, platform: str
) -> None:
    assert _probe_calls(monkeypatch, platform) == [
        ("settimeout", 2.0),
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("127.0.0.1", 18082)),
        ("close",),
    ]


def test_the_windows_probe_sets_no_socket_option(monkeypatch) -> None:
    """Byte-identical to the probe before #284: a plain bind, nothing else."""
    assert _probe_calls(monkeypatch, "win32") == [
        ("settimeout", 2.0),
        ("bind", ("127.0.0.1", 18082)),
        ("close",),
    ]


@pytest.mark.local_serial
def test_a_live_reuseaddr_listener_still_reads_as_taken() -> None:
    """MCC's own POSIX listener sets ``SO_REUSEADDR``; the probe never binds over it.

    On every platform: on Linux and macOS the kernel refuses a reuse bind over a
    live listener; on Windows only the missing option keeps the probe from
    binding over it.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert probe_port_available("127.0.0.1", port) is False
    finally:
        listener.close()


@pytest.mark.local_serial
@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux kernel rule; macOS lets a reuse bind take a specific address under a wildcard",
)
def test_a_live_wildcard_reuseaddr_listener_still_reads_as_taken_on_linux() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert probe_port_available("127.0.0.1", port) is False
        assert probe_port_available("0.0.0.0", port) is False
    finally:
        listener.close()


@pytest.mark.local_serial
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="TIME_WAIT never blocks a bind on Windows (measured 2026-10-24)",
)
def test_a_posix_port_reads_taken_while_listened_on_and_free_in_time_wait() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = listener.accept()
    assert probe_port_available("127.0.0.1", port) is False
    served.close()  # the server side closes first, so TIME_WAIT is on its port
    assert client.recv(1) == b""  # the FIN has arrived
    client.close()
    listener.close()

    assert probe_port_available("127.0.0.1", port) is True
