"""Bounded port-availability and ownership diagnostics for the server bind.

Used when a supervisor cannot bind its configured host:port: these helpers
answer two questions without ever disturbing a running process:

* ``probe_port_available`` -- can a socket bind here at all right now?
* ``diagnose_port_owner`` -- if not, which process holds the listener?

Both are read-only. They never send data to a peer and never kill or signal
the owner they discover; reporting is the entire job.

The owner is asked of a ladder, in order: ``ss -ltnp``, ``netstat -ano``,
then (7.111.3) ``lsof -nP -iTCP:<port> -sTCP:LISTEN`` everywhere but Windows,
then ``/proc/net/tcp`` + ``tcp6`` on Linux. The first rung that names an owner
answers, so wherever ``ss`` or ``netstat`` name one the answer is exactly what
it was before the new rungs existed. A minimal Linux or macOS with neither
command now still finds the holder.

"Nothing is listening" and "could not tell" are different answers
(:func:`lookup_port_owner`): a lookup in which no rung could produce a listing
at all -- every command missing, timed out or failing, the way ``netstat`` timed
out on a loaded machine on 2026-10-21 -- or in which a listener was seen whose
process could not be read, is *undetermined*, never "free".
:func:`diagnose_port_owner` keeps returning ``None`` for both, for every caller
that cannot act on the difference; a caller that must not mistake one for the
other asks inside :func:`undetermined_raises`.
"""

import errno
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_PORT_TOKEN = re.compile(r"(?<=[:\s])(\d+)(?=\s|$)")
_PROCESS_TOKEN = re.compile(r'users?:\(\("([^"]*)",pid=(\d+)')
_NETSTAT_PID = re.compile(r"(\d+)\s*$")

#: The platform the ladder is built for. A module attribute so a test can pin
#: it (``monkeypatch.setattr(port_diagnostics, "_PLATFORM", "linux")``).
_PLATFORM = sys.platform

#: Where Linux keeps its socket tables and process directories. A module
#: attribute so a test can point it at a recorded tree.
_PROC_ROOT = Path("/proc")

#: ``TCP_LISTEN`` in the kernel's ``tcp_states.h``, as ``/proc/net/tcp`` prints
#: the ``st`` column.
_PROC_LISTEN_STATE = "0A"

_LSOF_LISTEN_SUFFIX = "(LISTEN)"


@dataclass(slots=True)
class PortOwner:
    """The process holding a listening socket, best effort."""

    pid: int | None
    name: str | None
    command: str | None


@dataclass(frozen=True, slots=True)
class PortOwnerLookup:
    """Everything one walk of the ladder found out about a port.

    ``owner`` is what :func:`diagnose_port_owner` returns. ``undetermined`` is
    True when the walk cannot say the port is free: no rung produced a listing
    at all, or one saw a listener on the port and could not read its process.
    ``reason`` then says why, in words a person can act on ("netstat timed out
    after 2 s").
    """

    owner: PortOwner | None
    undetermined: bool = False
    reason: str | None = None


class PortOwnerUndetermined(Exception):
    """Who holds the port could not be determined -- which is not "nobody".

    Raised by :func:`diagnose_port_owner` instead of returning ``None``, and
    only inside :func:`undetermined_raises`.
    """

    def __init__(self, port: int, lookup: PortOwnerLookup) -> None:
        self.port = port
        self.lookup = lookup
        super().__init__(undetermined_message(port, lookup))


_UNDETERMINED_RAISES: ContextVar[bool] = ContextVar(
    "port_owner_undetermined_raises", default=False
)


@contextmanager
def undetermined_raises() -> Iterator[None]:
    """Make an undetermined lookup raise :class:`PortOwnerUndetermined`.

    For the callers that would otherwise read ``None`` as "nothing is
    listening" and act on it. Everything else -- and everything outside the
    block -- keeps getting ``None``, exactly as before.
    """

    token = _UNDETERMINED_RAISES.set(True)
    try:
        yield
    finally:
        _UNDETERMINED_RAISES.reset(token)


def undetermined_message(port: int, lookup: PortOwnerLookup) -> str:
    """The words for an undetermined lookup, for messages and logs.

    ``could not determine who holds port 8082 (netstat timed out after 2 s)``.
    """

    why = f" ({lookup.reason})" if lookup.reason else ""
    return f"could not determine who holds port {port}{why}"


def _family_for(host: str) -> socket.AddressFamily:
    """Pick the socket family for ``host`` without resolving it."""
    if ":" in host:
        return socket.AF_INET6
    return socket.AF_INET


def probe_port_available(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """Return True if a socket can bind ``(host, port)`` (it is free).

    A successful bind means nothing is listening yet, so the server can still
    rebind. This only probes availability; it sends no bytes and lasts at most
    ``timeout`` seconds.
    """

    sock = socket.socket(_family_for(host), socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_for_port_free(
    host: str, port: int, *, timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """Poll ``probe_port_available`` until the socket frees or ``timeout``.

    A previous server generation often holds the port for a beat after it
    stops, so a restart should wait a bounded moment before declaring a real
    conflict. Returns True once the port can be bound, False on timeout.
    """

    deadline = time.monotonic() + max(timeout, interval)
    while time.monotonic() < deadline:
        if probe_port_available(host, port):
            return True
        time.sleep(interval)
    return probe_port_available(host, port)


def _owner_from_line(line: str, port: int) -> PortOwner | None:
    """Extract the listener on ``port`` from one ``ss``/``netstat`` line."""

    if f":{port}" not in line:
        return None
    lowered = line.lower()
    if "listen" not in lowered and "listening" not in lowered:
        return None

    match = _PROCESS_TOKEN.search(line)
    if match:
        try:
            pid = int(match.group(2))
        except ValueError:
            pid = None
        return PortOwner(pid=pid, name=match.group(1) or None, command=None)

    # ``netstat`` puts the pid in the trailing column.
    pid_match = _NETSTAT_PID.search(line)
    if pid_match:
        try:
            pid = int(pid_match.group(1))
        except ValueError:
            pid = None
        else:
            return PortOwner(pid=pid, name=None, command=None)
    return None


@dataclass(frozen=True, slots=True)
class _RungAnswer:
    """What one rung of the ladder could say about the port."""

    #: The listener's process, when this rung could name it.
    owner: PortOwner | None = None
    #: The rung produced a listing it could read: no owner means none seen.
    listed: bool = False
    #: Why the rung produced no listing (missing, timed out, failed).
    failure: str | None = None
    #: A listener on the port was seen whose process could not be named.
    unresolved: str | None = None


def _seconds(timeout: float) -> str:
    return f"{timeout:g} s"


def _run_command(
    command: tuple[str, ...], timeout: float
) -> subprocess.CompletedProcess[str] | _RungAnswer:
    """Run one rung's command, or say why it could not be run."""

    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _RungAnswer(failure=f"{command[0]} timed out after {_seconds(timeout)}")
    except FileNotFoundError:
        return _RungAnswer(failure=f"{command[0]} was not found")
    except OSError, subprocess.SubprocessError:
        return _RungAnswer(failure=f"{command[0]} could not be run")


def _listing_rung(command: tuple[str, ...], port: int, timeout: float) -> _RungAnswer:
    """``ss``/``netstat``: the two rungs that predate 7.111.3, read as before."""

    completed = _run_command(command, timeout)
    if isinstance(completed, _RungAnswer):
        return completed
    if completed.returncode != 0:
        return _RungAnswer(
            failure=f"{command[0]} exited with status {completed.returncode}"
        )
    for line in completed.stdout.splitlines():
        owner = _owner_from_line(line, port)
        if owner is not None:
            return _RungAnswer(owner=owner, listed=True)
    return _RungAnswer(listed=True)


def _ss_rung(port: int, timeout: float) -> _RungAnswer:
    return _listing_rung(("ss", "-ltnp"), port, timeout)


def _netstat_rung(port: int, timeout: float) -> _RungAnswer:
    return _listing_rung(("netstat", "-ano"), port, timeout)


def _owner_from_lsof(stdout: str, port: int) -> PortOwner | None:
    """The first listener on exactly ``port`` in ``lsof -nP`` output.

    A line reads ``COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME`` and,
    for a listener, ends ``*:8082 (LISTEN)`` / ``127.0.0.1:8082 (LISTEN)`` /
    ``[::1]:8082 (LISTEN)``.
    """

    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] == "COMMAND":
            continue
        if fields[-1] != _LSOF_LISTEN_SUFFIX:
            continue
        if fields[-2].rpartition(":")[2] != str(port):
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        return PortOwner(pid=pid, name=fields[0] or None, command=None)
    return None


def _lsof_rung(port: int, timeout: float) -> _RungAnswer:
    """``lsof``: present on macOS and on most Linux images that lack ``ss``.

    ``lsof`` exits 1 both when nothing matched and on an error, so an exit of
    1 counts as "none seen" only with nothing on stdout and nothing but
    WARNING lines on stderr; anything else is a failure, never "free".
    """

    command = ("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN")
    completed = _run_command(command, timeout)
    if isinstance(completed, _RungAnswer):
        return completed
    stdout = completed.stdout or ""
    owner = _owner_from_lsof(stdout, port)
    if owner is not None:
        return _RungAnswer(owner=owner, listed=True)
    rows = [
        line
        for line in stdout.splitlines()
        if line.strip() and not line.startswith("COMMAND")
    ]
    if completed.returncode == 0:
        if rows:
            return _RungAnswer(
                listed=True,
                unresolved=(
                    f"lsof lists a listener on port {port} but no process id "
                    "could be read from it"
                ),
            )
        return _RungAnswer(listed=True)
    warnings_only = all(
        "WARNING" in line for line in (completed.stderr or "").splitlines() if line
    )
    if completed.returncode == 1 and not rows and warnings_only:
        return _RungAnswer(listed=True)
    return _RungAnswer(failure=f"lsof exited with status {completed.returncode}")


def _proc_endpoint(field: str) -> tuple[str, int] | None:
    """Decode a ``/proc/net/tcp[6]`` address column: ``0100007F:1F92``.

    The address is the kernel's in-memory bytes printed as 32-bit words in
    host (little-endian) order -- 8 hex digits for IPv4, 32 for IPv6 -- and
    the port is plain big-endian hex.
    """

    raw_address, separator, raw_port = field.rpartition(":")
    if not separator:
        return None
    try:
        port = int(raw_port, 16)
        packed = bytes.fromhex(raw_address)
    except ValueError:
        return None
    if len(packed) not in (4, 16):
        return None
    words = b"".join(packed[i : i + 4][::-1] for i in range(0, len(packed), 4))
    if len(words) == 4:
        address = socket.inet_ntop(socket.AF_INET, words)
    else:
        address = socket.inet_ntop(socket.AF_INET6, words)
    return address, port


def _listening_sockets(text: str, port: int) -> list[tuple[str, str]]:
    """``(inode, "address:port")`` of every LISTEN row for ``port`` in a table."""

    found: list[tuple[str, str]] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3].upper() != _PROC_LISTEN_STATE:
            continue
        endpoint = _proc_endpoint(fields[1])
        if endpoint is None or endpoint[1] != port:
            continue
        inode = fields[9]
        if inode.isdigit() and inode != "0":
            address = endpoint[0]
            shown = f"[{address}]" if ":" in address else address
            found.append((inode, f"{shown}:{port}"))
    return found


# The filesystem calls the /proc rung makes, one seam each, so a test can
# replay a recorded tree (including a permission-denied fd directory) on any
# platform.
def _list_dir(path: Path) -> list[str]:
    return os.listdir(path)


def _read_link(path: Path) -> str:
    return os.readlink(path)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _proc_rung(port: int, timeout: float) -> _RungAnswer:
    """Linux's own socket tables, then the inode -> pid walk through fd links.

    Needs no command at all, which is the point: a minimal image with neither
    ``ss``, ``netstat`` nor ``lsof`` still has ``/proc``. A listener whose
    process cannot be read -- another user's, without root -- is reported as
    unresolved, never as a free port.
    """

    deadline = time.monotonic() + timeout
    root = _PROC_ROOT
    sockets: list[tuple[str, str]] = []
    readable = False
    for table in ("tcp", "tcp6"):
        try:
            text = _read_text(root / "net" / table)
        except OSError:
            continue
        readable = True
        sockets.extend(_listening_sockets(text, port))
    if not readable:
        return _RungAnswer(failure="/proc/net/tcp could not be read")
    if not sockets:
        return _RungAnswer(listed=True)

    targets = {f"socket:[{inode}]" for inode, _ in sockets}
    where = ", ".join(dict.fromkeys(shown for _, shown in sockets))
    try:
        entries = _list_dir(root)
    except OSError:
        entries = []
    denied = 0
    for entry in entries:
        if not entry.isdigit():
            continue
        if time.monotonic() > deadline:
            return _RungAnswer(
                listed=True,
                unresolved=(
                    f"/proc/net/tcp lists a listener on {where}, and the walk of "
                    f"/proc/<pid>/fd for its process timed out after "
                    f"{_seconds(timeout)}"
                ),
            )
        fd_dir = root / entry / "fd"
        try:
            descriptors = _list_dir(fd_dir)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            # The process exited between the listing and this read.
            continue
        for descriptor in descriptors:
            try:
                link = _read_link(fd_dir / descriptor)
            except OSError:
                continue
            if link in targets:
                try:
                    name = _read_text(root / entry / "comm").strip() or None
                except OSError:
                    name = None
                return _RungAnswer(
                    owner=PortOwner(pid=int(entry), name=name, command=None),
                    listed=True,
                )
    if denied:
        why = (
            f"its process could not be read (permission denied on "
            f"/proc/<pid>/fd for {denied} processes)"
        )
    else:
        why = "no process holding it was found"
    return _RungAnswer(
        listed=True,
        unresolved=f"/proc/net/tcp lists a listener on {where}, but {why}",
    )


def _ladder() -> list[Callable[[int, float], _RungAnswer]]:
    """The rungs, in order: the two original commands first, always."""

    rungs: list[Callable[[int, float], _RungAnswer]] = [_ss_rung, _netstat_rung]
    if not _PLATFORM.startswith("win"):
        rungs.append(_lsof_rung)
    if _PLATFORM.startswith("linux"):
        rungs.append(_proc_rung)
    return rungs


def lookup_port_owner(host: str, port: int, *, timeout: float = 2.0) -> PortOwnerLookup:
    """Walk the ladder for ``host:port`` and say what it found, or why not.

    The first rung that names an owner answers. Otherwise the port is free
    only if some rung produced a listing and no rung saw an unnameable
    listener on it; every other outcome is undetermined, with the reason.
    ``timeout`` bounds each rung, as it bounded each command before.
    """

    listed = False
    failures: list[str] = []
    unresolved: list[str] = []
    for rung in _ladder():
        answer = rung(port, timeout)
        if answer.owner is not None:
            return PortOwnerLookup(owner=answer.owner)
        listed = listed or answer.listed
        if answer.failure:
            failures.append(answer.failure)
        if answer.unresolved:
            unresolved.append(answer.unresolved)
    if unresolved:
        return PortOwnerLookup(
            owner=None, undetermined=True, reason="; ".join(unresolved)
        )
    if listed:
        return PortOwnerLookup(owner=None)
    return PortOwnerLookup(
        owner=None,
        undetermined=True,
        reason="; ".join(failures) or "no way to list listening sockets",
    )


def diagnose_port_owner(
    host: str, port: int, *, timeout: float = 2.0
) -> PortOwner | None:
    """Best-effort identify the process holding ``host:port``.

    Tries ``ss`` first, then ``netstat``, then (7.111.3) ``lsof`` and Linux's
    ``/proc``. Any command may be missing, so failures are swallowed and the
    next probe is tried. Returns ``None`` when no owner can be determined;
    this never kills or signals the process. Inside
    :func:`undetermined_raises`, a lookup that could not tell raises
    :class:`PortOwnerUndetermined` instead of returning ``None``.
    """

    lookup = lookup_port_owner(host, port, timeout=timeout)
    if lookup.undetermined and _UNDETERMINED_RAISES.get():
        raise PortOwnerUndetermined(port, lookup)
    return lookup.owner


def is_address_in_use(exc: OSError) -> bool:
    """Whether ``exc`` is a bind failure because the address is taken."""

    if exc.errno == errno.EADDRINUSE:
        return True
    return "address already in use" in str(exc).lower()
