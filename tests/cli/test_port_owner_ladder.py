"""The port-owner ladder after ``ss``/``netstat``: ``lsof`` and Linux ``/proc``.

PORT-OWNER (7.111.3). Two defects in ``cli.port_diagnostics``:

1. A minimal Linux or macOS with neither ``ss`` nor ``netstat`` could never
   name the holder of a port, so an update while the server ran ended "could
   not identify the port holder" (7.79.1's residual). Two rungs now follow the
   original two: ``lsof -nP -iTCP:<port> -sTCP:LISTEN`` everywhere but Windows,
   and ``/proc/net/tcp`` + ``tcp6`` with the inode -> pid walk through
   ``/proc/<pid>/fd`` on Linux.
2. On a loaded machine ``netstat -ano`` overran its timeout, the lookup
   returned ``None``, and ``mcc-server --stop-holder`` read that as "Nothing
   was listening" while the old server still listened (2026-10-21, 7.103.1).
   A lookup that cannot tell is now *undetermined*, never "free".

Every recorded output here is typed in the tool's real column shape; pids,
inodes and names are invented.
"""

import inspect
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.cli import port_diagnostics
from my_claude_code.cli.port_diagnostics import (
    PortOwner,
    PortOwnerUndetermined,
    diagnose_port_owner,
    lookup_port_owner,
    undetermined_raises,
)

LSOF_HEADER = "COMMAND     PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME"

LSOF_LISTENING_V4 = "\n".join(
    [
        LSOF_HEADER,
        "python3   4321 dev    7u  IPv4 0x6c1a9f3c2b8e1d01      0t0  TCP *:8082 (LISTEN)",
        "",
    ]
)
LSOF_LISTENING_LOOPBACK = "\n".join(
    [
        LSOF_HEADER,
        "mcc-serve 5150 dev    9u  IPv4 0x6c1a9f3c2b8e1d02      0t0  TCP 127.0.0.1:8082 (LISTEN)",
        "",
    ]
)
LSOF_LISTENING_V6 = "\n".join(
    [
        LSOF_HEADER,
        "node      9001 dev   21u  IPv6 0x6c1a9f3c2b8e1d03      0t0  TCP [::1]:8082 (LISTEN)",
        "",
    ]
)

PROC_TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode"
)
# 0x1F92 = 8082. Row 0 listens on 0.0.0.0:8082 (inode 424242); row 1 is an
# accepted connection on 127.0.0.1:8082 (ESTABLISHED, 01); row 2 is TIME_WAIT
# (06, inode 0); row 3 listens on 127.0.0.1:631 (0x0277).
PROC_TCP = "\n".join(
    [
        PROC_TCP_HEADER,
        "   0: 00000000:1F92 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000  1000        0 424242 1 0000000000000000 100 0 0 10 0",
        "   1: 0100007F:1F92 0100007F:C8A2 01 00000000:00000000 00:00000000 "
        "00000000  1000        0 424243 1 0000000000000000 20 4 30 10 -1",
        "   2: 0100007F:1F92 0100007F:C8A8 06 00000000:00000000 03:00000EB1 "
        "00000000     0        0 0 3 0000000000000000",
        "   3: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000     0        0 31337 1 0000000000000000 100 0 0 10 0",
        "",
    ]
)
PROC_TCP6_HEADER = (
    "  sl  local_address                         remote_address                        "
    "st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
)
# [::1]:8082 listening (inode 515151).
PROC_TCP6 = "\n".join(
    [
        PROC_TCP6_HEADER,
        "   0: 00000000000000000000000001000000:1F92 "
        "00000000000000000000000000000000:0000 0A 00000000:00000000 "
        "00:00000000 00000000  1000        0 515151 1 0000000000000000 100 0 0 10 0",
        "",
    ]
)
PROC_TCP_NO_LISTENER = "\n".join(
    [
        PROC_TCP_HEADER,
        "   0: 0100007F:0277 00000000:0000 0A 00000000:00000000 00:00000000 "
        "00000000     0        0 31337 1 0000000000000000 100 0 0 10 0",
        "",
    ]
)


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _commands(monkeypatch, behaviours: dict[str, Any], calls: list[str]) -> None:
    """``subprocess.run`` answering per command name; unknown ones are missing."""

    def run(command, *args, **kwargs):
        name = command[0]
        calls.append(name)
        behaviour = behaviours.get(name, "missing")
        if behaviour == "missing":
            raise FileNotFoundError(2, "No such file or directory", name)
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        returncode, stdout, stderr = behaviour
        return _Completed(returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)


class _FakeProc:
    """A recorded ``/proc`` tree behind the module's three filesystem seams."""

    def __init__(
        self,
        tables: dict[str, str],
        fds: dict[str, dict[str, str]] | None = None,
        denied: tuple[str, ...] = (),
        comm: dict[str, str] | None = None,
    ) -> None:
        self.root = Path("/fake-proc")
        self.tables = tables
        self.fds = fds or {}
        self.denied = set(denied)
        self.comm = comm or {}
        self.reads: list[str] = []

    def _relative(self, path: Path) -> list[str]:
        return list(path.relative_to(self.root).parts)

    def list_dir(self, path: Path) -> list[str]:
        parts = self._relative(path)
        if not parts:
            return sorted({*self.fds, *self.denied, "self", "net"})
        if len(parts) == 2 and parts[1] == "fd":
            pid = parts[0]
            if pid in self.denied:
                raise PermissionError(13, "Permission denied", str(path))
            if pid not in self.fds:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return list(self.fds[pid])
        raise FileNotFoundError(2, "No such file or directory", str(path))

    def read_link(self, path: Path) -> str:
        pid, _, descriptor = self._relative(path)
        return self.fds[pid][descriptor]

    def read_text(self, path: Path) -> str:
        parts = self._relative(path)
        self.reads.append("/".join(parts))
        if parts[0] == "net" and parts[1] in self.tables:
            return self.tables[parts[1]]
        if len(parts) == 2 and parts[1] == "comm" and parts[0] in self.comm:
            return self.comm[parts[0]] + "\n"
        raise FileNotFoundError(2, "No such file or directory", str(path))

    def install(self, monkeypatch) -> _FakeProc:
        monkeypatch.setattr(port_diagnostics, "_PROC_ROOT", self.root)
        monkeypatch.setattr(port_diagnostics, "_list_dir", self.list_dir)
        monkeypatch.setattr(port_diagnostics, "_read_link", self.read_link)
        monkeypatch.setattr(port_diagnostics, "_read_text", self.read_text)
        return self


def _no_proc(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(port_diagnostics, "_PROC_ROOT", tmp_path / "no-proc")


def _platform(monkeypatch, name: str) -> None:
    monkeypatch.setattr(port_diagnostics, "_PLATFORM", name)


# ---------------------------------------------------------------- the order


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("linux", ["ss", "netstat", "lsof", "/proc"]),
        ("darwin", ["ss", "netstat", "lsof"]),
        ("win32", ["ss", "netstat"]),
    ],
)
def test_the_ladder_order_is_pinned(monkeypatch, platform, expected) -> None:
    """ss -> netstat -> lsof -> /proc; the two original commands always first."""

    _platform(monkeypatch, platform)
    calls: list[str] = []
    _commands(monkeypatch, {}, calls)
    fake = _FakeProc({"tcp": PROC_TCP_NO_LISTENER}).install(monkeypatch)

    lookup_port_owner("0.0.0.0", 8082)

    seen = calls + (["/proc"] if fake.reads else [])
    assert seen == expected


def test_a_named_owner_from_ss_stops_the_ladder(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    calls: list[str] = []
    ss = 'LISTEN 0 128 0.0.0.0:8082 0.0.0.0:* users:(("python3",pid=12345,fd=6))'
    _commands(monkeypatch, {"ss": (0, ss, "")}, calls)
    fake = _FakeProc({"tcp": PROC_TCP}).install(monkeypatch)

    owner = diagnose_port_owner("0.0.0.0", 8082)

    assert owner == PortOwner(pid=12345, name="python3", command=None)
    assert calls == ["ss"]
    assert fake.reads == []


def test_the_timeout_value_is_unchanged() -> None:
    """No invented limit: each rung is bounded by the same 2 s as before."""

    for function in (diagnose_port_owner, lookup_port_owner):
        assert inspect.signature(function).parameters["timeout"].default == 2.0


def test_every_command_is_bounded_by_the_callers_timeout(monkeypatch) -> None:
    _platform(monkeypatch, "darwin")
    seen: list[float] = []

    def run(command, *args, **kwargs):
        seen.append(kwargs["timeout"])
        raise FileNotFoundError(2, "No such file or directory", command[0])

    monkeypatch.setattr(subprocess, "run", run)
    lookup_port_owner("0.0.0.0", 8082, timeout=5.0)
    assert seen == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------- lsof


@pytest.mark.parametrize(
    ("stdout", "pid", "name"),
    [
        (LSOF_LISTENING_V4, 4321, "python3"),
        (LSOF_LISTENING_LOOPBACK, 5150, "mcc-serve"),
        (LSOF_LISTENING_V6, 9001, "node"),
    ],
)
def test_lsof_names_the_holder_when_ss_and_netstat_are_missing(
    monkeypatch, tmp_path, stdout, pid, name
) -> None:
    _platform(monkeypatch, "darwin")
    _no_proc(monkeypatch, tmp_path)
    calls: list[str] = []
    _commands(monkeypatch, {"lsof": (0, stdout, "")}, calls)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner == PortOwner(pid=pid, name=name, command=None)
    assert lookup.undetermined is False
    assert calls == ["ss", "netstat", "lsof"]


def test_lsof_matches_the_exact_port_only(monkeypatch, tmp_path) -> None:
    """A listener on 18082 is not the holder of 8082 (or of 808)."""

    _platform(monkeypatch, "darwin")
    other = LSOF_LISTENING_V4.replace("*:8082", "*:18082")
    for port in (8082, 808):
        _commands(monkeypatch, {"lsof": (0, other, "")}, [])
        lookup = lookup_port_owner("0.0.0.0", port)
        assert lookup.owner is None
        # lsof exited 0 with a row it could not attribute to the port asked.
        assert lookup.undetermined is True


def test_lsof_with_no_listener_is_a_listing_not_a_failure(monkeypatch) -> None:
    """``lsof`` exits 1 with nothing printed when nothing matches."""

    _platform(monkeypatch, "darwin")
    _commands(monkeypatch, {"lsof": (1, "", "")}, [])

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner is None
    assert lookup.undetermined is False


def test_lsof_warnings_alone_still_mean_no_listener(monkeypatch) -> None:
    _platform(monkeypatch, "darwin")
    warning = (
        "lsof: WARNING: can't stat() fuse.gvfsd-fuse file system /run/user/1000/gvfs\n"
    )
    _commands(monkeypatch, {"lsof": (1, "", warning)}, [])

    assert lookup_port_owner("0.0.0.0", 8082).undetermined is False


def test_an_lsof_error_is_never_read_as_a_free_port(monkeypatch) -> None:
    _platform(monkeypatch, "darwin")
    error = "lsof: unsupported TCP/TPI info selection: -s\nlsof 4.89\n"
    _commands(monkeypatch, {"lsof": (1, "", error)}, [])

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner is None
    assert lookup.undetermined is True
    assert lookup.reason == (
        "ss was not found; netstat was not found; lsof exited with status 1"
    )


# ---------------------------------------------------------------- /proc


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("00000000:1F92", ("0.0.0.0", 8082)),
        ("0100007F:1F92", ("127.0.0.1", 8082)),
        ("0100007F:0277", ("127.0.0.1", 631)),
        ("0101A8C0:0050", ("192.168.1.1", 80)),
        ("00000000000000000000000000000000:1F92", ("::", 8082)),
        ("00000000000000000000000001000000:1F92", ("::1", 8082)),
        ("0000000000000000FFFF00000100007F:1F92", ("::ffff:127.0.0.1", 8082)),
        ("B80D0120000000000000000001000000:01BB", ("2001:db8::1", 443)),
    ],
)
def test_proc_addresses_decode_from_kernel_hex(field, expected) -> None:
    assert port_diagnostics._proc_endpoint(field) == expected


@pytest.mark.parametrize("field", ["", "1F92", "zz:1F92", "0100007F:zz", "01007F:1F92"])
def test_malformed_proc_addresses_are_skipped(field) -> None:
    assert port_diagnostics._proc_endpoint(field) is None


def test_only_listening_rows_of_the_port_are_read() -> None:
    rows = port_diagnostics._listening_sockets(PROC_TCP, 8082)
    assert rows == [("424242", "0.0.0.0:8082")]
    assert port_diagnostics._listening_sockets(PROC_TCP, 631) == [
        ("31337", "127.0.0.1:631")
    ]
    assert port_diagnostics._listening_sockets(PROC_TCP6, 8082) == [
        ("515151", "[::1]:8082")
    ]
    # The ESTABLISHED and TIME_WAIT rows on 8082 are not listeners.
    assert port_diagnostics._listening_sockets(PROC_TCP, 51362) == []


def test_proc_walks_inode_to_pid_when_no_command_exists(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {}, [])
    _FakeProc(
        {"tcp": PROC_TCP, "tcp6": PROC_TCP6},
        fds={
            "1": {"0": "/dev/null"},
            "777": {"0": "/dev/null", "3": "socket:[999]", "4": "pipe:[12]"},
            "4321": {"0": "/dev/pts/0", "7": "socket:[424242]"},
        },
        comm={"4321": "python3", "777": "nginx"},
    ).install(monkeypatch)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner == PortOwner(pid=4321, name="python3", command=None)
    assert lookup.undetermined is False


def test_proc_finds_an_ipv6_only_listener(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {}, [])
    _FakeProc(
        {"tcp": PROC_TCP_NO_LISTENER, "tcp6": PROC_TCP6},
        fds={"9001": {"21": "socket:[515151]"}},
        comm={"9001": "node"},
    ).install(monkeypatch)

    owner = diagnose_port_owner("::", 8082)

    assert owner == PortOwner(pid=9001, name="node", command=None)


def test_proc_with_no_listener_means_free(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {}, [])
    _FakeProc({"tcp": PROC_TCP_NO_LISTENER}).install(monkeypatch)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner is None
    assert lookup.undetermined is False


def test_permission_denied_on_proc_fd_is_undetermined_never_free(monkeypatch) -> None:
    """Another user's listener: the table shows it, its fd directory is closed."""

    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {}, [])
    _FakeProc(
        {"tcp": PROC_TCP},
        fds={"4321": {"0": "/dev/null"}},
        denied=("1", "6000"),
    ).install(monkeypatch)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner is None
    assert lookup.undetermined is True
    assert lookup.reason == (
        "/proc/net/tcp lists a listener on 0.0.0.0:8082, but its process could "
        "not be read (permission denied on /proc/<pid>/fd for 2 processes)"
    )
    assert diagnose_port_owner("0.0.0.0", 8082) is None
    with undetermined_raises(), pytest.raises(PortOwnerUndetermined) as raised:
        diagnose_port_owner("0.0.0.0", 8082)
    assert str(raised.value).startswith("could not determine who holds port 8082 (")


def test_a_denied_directory_does_not_hide_a_readable_owner(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {}, [])
    _FakeProc(
        {"tcp": PROC_TCP},
        fds={"4321": {"7": "socket:[424242]"}},
        denied=("1",),
        comm={"4321": "python3"},
    ).install(monkeypatch)

    assert diagnose_port_owner("0.0.0.0", 8082) == PortOwner(
        pid=4321, name="python3", command=None
    )


def test_a_listener_with_no_findable_process_is_undetermined(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {}, [])
    _FakeProc({"tcp": PROC_TCP}, fds={"1": {"0": "/dev/null"}}).install(monkeypatch)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.undetermined is True
    assert lookup.reason == (
        "/proc/net/tcp lists a listener on 0.0.0.0:8082, but no process holding "
        "it was found"
    )


def test_ss_answering_and_proc_seeing_an_unnamed_listener_is_undetermined(
    monkeypatch,
) -> None:
    """ss without root lists another user's socket with no process (7.79.1).

    The old answer, ``None``, is kept for every legacy caller; the lookup now
    says it could not tell.
    """

    _platform(monkeypatch, "linux")
    ss = "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nLISTEN 0 2048 0.0.0.0:8082 0.0.0.0:*\n"
    _commands(monkeypatch, {"ss": (0, ss, ""), "lsof": (1, "", "")}, [])
    _FakeProc({"tcp": PROC_TCP}, denied=("4321",)).install(monkeypatch)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner is None
    assert lookup.undetermined is True
    assert diagnose_port_owner("0.0.0.0", 8082) is None


def test_no_proc_tables_and_no_commands_is_undetermined(monkeypatch, tmp_path) -> None:
    _platform(monkeypatch, "linux")
    _no_proc(monkeypatch, tmp_path)
    _commands(monkeypatch, {}, [])

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.undetermined is True
    assert lookup.reason == (
        "ss was not found; netstat was not found; lsof was not found; "
        "/proc/net/tcp could not be read"
    )


# ---------------------------------------------------------------- the lie


def test_a_timed_out_netstat_is_undetermined_not_free(monkeypatch) -> None:
    """The 2026-10-21 incident on Windows: netstat overran its timeout."""

    _platform(monkeypatch, "win32")
    _commands(monkeypatch, {"netstat": "timeout"}, [])

    lookup = lookup_port_owner("127.0.0.1", 8082)

    assert lookup.owner is None
    assert lookup.undetermined is True
    assert lookup.reason == "ss was not found; netstat timed out after 2 s"
    # Every caller that cannot tell the difference keeps getting None ...
    assert diagnose_port_owner("127.0.0.1", 8082) is None
    # ... and a caller that must not mistake it for "nobody" is told.
    with undetermined_raises(), pytest.raises(PortOwnerUndetermined) as raised:
        diagnose_port_owner("127.0.0.1", 8082)
    assert str(raised.value) == (
        "could not determine who holds port 8082 "
        "(ss was not found; netstat timed out after 2 s)"
    )
    assert raised.value.port == 8082
    assert raised.value.lookup == lookup


def test_ss_and_netstat_both_timing_out_on_linux_falls_to_proc(monkeypatch) -> None:
    _platform(monkeypatch, "linux")
    _commands(monkeypatch, {"ss": "timeout", "netstat": "timeout"}, [])
    _FakeProc(
        {"tcp": PROC_TCP},
        fds={"4321": {"7": "socket:[424242]"}},
        comm={"4321": "python3"},
    ).install(monkeypatch)

    lookup = lookup_port_owner("0.0.0.0", 8082)

    assert lookup.owner == PortOwner(pid=4321, name="python3", command=None)
    assert lookup.undetermined is False


def test_raising_is_scoped_to_the_block(monkeypatch) -> None:
    _platform(monkeypatch, "win32")
    _commands(monkeypatch, {"netstat": "timeout"}, [])

    with undetermined_raises(), pytest.raises(PortOwnerUndetermined):
        diagnose_port_owner("127.0.0.1", 8082)
    assert diagnose_port_owner("127.0.0.1", 8082) is None


def test_a_definite_no_listener_never_raises(monkeypatch) -> None:
    _platform(monkeypatch, "win32")
    listing = (
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1160\n"
    )
    _commands(monkeypatch, {"netstat": (0, listing, "")}, [])

    with undetermined_raises():
        assert diagnose_port_owner("127.0.0.1", 8082) is None


# ---------------------------------------------------------------- real /proc


@pytest.mark.local_serial
@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux /proc only (CI ubuntu job)"
)
@pytest.mark.parametrize(
    ("family", "address"),
    [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")],
)
def test_the_real_proc_names_a_listener_this_test_opens(family, address) -> None:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.bind((address, 0))
    except OSError as exc:
        pytest.skip(f"cannot bind {address}: {exc}")
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        answer = port_diagnostics._proc_rung(port, 2.0)
        assert answer.owner is not None, answer
        assert answer.owner.pid == os.getpid()
        assert answer.owner.name
    finally:
        sock.close()
