"""Tests for bounded port-availability and ownership diagnostics."""

import socket

import pytest

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
