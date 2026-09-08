"""Take the configured port back before binding it.

The rule this implements is the user's, and it is deliberately blunt: *when the
server starts, whatever is holding its port stops holding it.* Before 6.59.0 a
held port was a diagnosis and an exit -- the supervisor named the holder and
raised ``SystemExit(1)`` -- which is the correct instinct for a shared machine
and the wrong one for this program. The commonest holder by far is MCC's own
previous generation: a server that overran its drain, a duplicate the desktop
shell spawned into a start it could not see, or a child the update helper left
behind. Refusing to start because of one of those is refusing to recover.

Three policies, on ``SERVER_PORT_TAKEOVER``:

``always`` (default)
    Kill whoever holds the port. A holder that is *not* MCC gets one loud
    WARNING naming its image and pid before it is stopped -- the user asked for
    "force kill anything on the port", and the least this can do is say what it
    killed and leave that sentence in the log.
``mcc-only``
    Kill only a holder this module can positively identify as MCC. Anything
    else is left alone and the start fails with the old diagnosis. This is the
    setting for a machine where port 8082 might legitimately belong to someone
    else.
``never``
    6.58.4's behaviour exactly: wait a bounded moment for the port to free, and
    fail with the diagnosis if it does not.

**Identification is by process, not by HTTP.** A starting MCC answers 503, a
draining one answers 503 with a different marker, and one wedged before its
listener answers nothing at all -- so asking the port what it is cannot
distinguish MCC from a stranger, and asking it was how the desktop window came
to tell the user that MCC's own python.exe "is not the MCC server". The image
name and command line answer it without a request.
"""

import os
import platform
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .port_diagnostics import PortOwner, diagnose_port_owner, wait_for_port_free

#: Executable names that are MCC and nothing else.
_MCC_IMAGE_NAMES = (
    "mcc-server",
    "mcc-desktop",
    "my-claude-code",
    "fcc-server",
    "fcc-desktop",
)

#: Substrings that identify MCC inside a generic interpreter's command line.
#: ``python.exe`` is the image name of every MCC server on Windows, so the
#: command line is the only thing that separates ours from anybody else's.
_MCC_COMMAND_SUBSTRINGS = (
    "mcc-server",
    "mcc_server",
    "mcc-desktop",
    "my_claude_code",
    "my-claude-code",
    "free_claude_code",
    "fcc-server",
)

#: How long to wait for a killed holder to actually release the socket. A
#: terminated process on Windows can hold a listening socket for a beat after
#: the handle count drops, and binding into that beat is a bind failure with a
#: misleading message.
RELEASE_WAIT_SECONDS = 8.0

#: Seconds between the polite stop and the forced one.
TERMINATE_GRACE_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """What a pid is, as far as the operating system will say."""

    pid: int
    image: str | None
    command: str | None

    @property
    def is_mcc(self) -> bool:
        """Whether this process is one of ours.

        False for "cannot tell": a process this module cannot identify is
        treated as a stranger, so ``mcc-only`` never kills something it merely
        failed to read.
        """

        image = (self.image or "").lower()
        stem = Path(image).stem
        if stem.startswith(_MCC_IMAGE_NAMES):
            return True
        command = (self.command or "").lower()
        return any(needle in command for needle in _MCC_COMMAND_SUBSTRINGS)

    def describe(self) -> str:
        name = self.image or "an unidentified process"
        return f"{name} (pid {self.pid})"


@dataclass(frozen=True, slots=True)
class TakeoverOutcome:
    """What the takeover did, for the caller to log and the tests to assert."""

    #: True when the port is free to bind now.
    free: bool
    #: The holder found, if any.
    identity: ProcessIdentity | None
    #: What happened, one of ``free``/``killed``/``refused``/``failed``.
    action: str

    def describe(self) -> str:
        if self.identity is None:
            return "the port was already free"
        return f"{self.action} {self.identity.describe()}"


def _windows_image(pid: int, *, timeout: float) -> str | None:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        parts = line.split('",')
        if parts and parts[0].strip('"').strip():
            return parts[0].strip('"').strip()
    return None


def _windows_command(pid: int, *, timeout: float) -> str | None:
    """The command line of ``pid``, via CIM.

    Only reached when the image name is a bare interpreter, because starting a
    PowerShell costs about a second and the image name settles the question for
    every shim.
    """

    script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _posix_identity(pid: int) -> ProcessIdentity:
    command = None
    image = None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        raw = b""
    if raw:
        command = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    try:
        image = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip() or None
    except OSError:
        image = None
    return ProcessIdentity(pid=pid, image=image, command=command)


def identify_process(pid: int, *, timeout: float = 5.0) -> ProcessIdentity:
    """Best-effort image name and command line for ``pid``. Read-only."""

    if platform.system() != "Windows":
        return _posix_identity(pid)
    image = _windows_image(pid, timeout=timeout)
    command = None
    stem = Path((image or "").lower()).stem
    if not stem or stem in {"python", "pythonw", "py", "uv", "cmd", "powershell"}:
        command = _windows_command(pid, timeout=timeout)
    return ProcessIdentity(pid=pid, image=image, command=command)


def identity_for_owner(
    owner: PortOwner | None, *, timeout: float = 5.0
) -> ProcessIdentity | None:
    """Turn a listener's pid into what that process actually is.

    Split from :func:`identify_port_holder` so a caller that has already asked
    who holds the port -- ``cli.desktop`` does, for its conflict message -- can
    identify it without a second ``netstat``.
    """

    if owner is None or owner.pid is None:
        return None
    identity = identify_process(owner.pid, timeout=timeout)
    if identity.image is None and owner.name:
        identity = ProcessIdentity(
            pid=owner.pid, image=owner.name, command=identity.command
        )
    return identity


def identify_port_holder(
    host: str, port: int, *, timeout: float = 5.0
) -> ProcessIdentity | None:
    """Who holds ``host:port`` right now, by process. Never signals anyone."""

    return identity_for_owner(
        diagnose_port_owner(host, port, timeout=timeout), timeout=timeout
    )


def _kill(pid: int) -> bool:
    """Stop ``pid``, politely first. Returns whether the signal was delivered."""

    if pid == os.getpid():
        return False
    # On Windows SIGTERM is TerminateProcess already; a failure here means the
    # process is gone or is not ours to signal. Either way, fall through to the
    # forced attempt below, which reports the truth.
    with suppress(OSError, ValueError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except AttributeError, OSError, ValueError:
        if platform.system() == "Windows":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except OSError, subprocess.SubprocessError:
                return False
        else:
            return False
    return not _alive(pid)


def _alive(pid: int) -> bool:
    if platform.system() == "Windows":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return True
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def take_port(
    host: str,
    port: int,
    policy: str,
    *,
    wait_seconds: float = RELEASE_WAIT_SECONDS,
) -> TakeoverOutcome:
    """Make ``host:port`` bindable under ``policy``. Returns what it did.

    Never raises: a takeover that cannot happen is an outcome the supervisor
    reports, not an exception it has to catch on the one path where a clear
    message matters most.
    """

    identity = identify_port_holder(host, port)
    if identity is None:
        # Either nothing holds it (a race with a stopping generation) or the
        # holder could not be identified. Both are "wait and see"; a takeover
        # with no pid to take it from has nothing to do.
        free = wait_for_port_free(host, port, timeout=wait_seconds)
        return TakeoverOutcome(
            free=free, identity=None, action="free" if free else "failed"
        )

    if policy == "never":
        logger.error(
            "Port {port} is held by {who} and SERVER_PORT_TAKEOVER=never, so "
            "nothing was stopped.",
            port=port,
            who=identity.describe(),
        )
        return TakeoverOutcome(free=False, identity=identity, action="refused")

    if policy == "mcc-only" and not identity.is_mcc:
        logger.error(
            "Port {port} is held by {who}, which is not My Claude Code, and "
            "SERVER_PORT_TAKEOVER=mcc-only. Nothing was stopped.",
            port=port,
            who=identity.describe(),
        )
        return TakeoverOutcome(free=False, identity=identity, action="refused")

    if identity.is_mcc:
        logger.warning(
            "Port {port} is held by a previous My Claude Code process, {who}. "
            "Stopping it and taking the port.",
            port=port,
            who=identity.describe(),
        )
    else:
        # The loud one. Killing a stranger's process is the user's explicit
        # instruction, and a line in the log naming what was killed is the
        # least this owes them.
        logger.warning(
            "SERVER_PORT_TAKEOVER=always: stopping {who}, which is NOT My "
            "Claude Code but is holding port {port}. Set "
            "SERVER_PORT_TAKEOVER=mcc-only to stop only My Claude Code's own "
            "processes, or never to stop nothing.",
            who=identity.describe(),
            port=port,
        )

    _kill(identity.pid)
    free = wait_for_port_free(host, port, timeout=wait_seconds)
    if not free:
        logger.error(
            "Stopped {who} but port {port} is still not bindable.",
            who=identity.describe(),
            port=port,
        )
    return TakeoverOutcome(
        free=free, identity=identity, action="stopped" if free else "failed"
    )
