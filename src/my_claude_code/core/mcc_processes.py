"""One scanner for "which of these processes are ours", shared by every caller.

Until 6.72.2 the question "is this process My Claude Code?" was asked in one
place only -- :mod:`my_claude_code.cli.port_takeover`, about the single pid that
held the configured port -- and it was asked with a substring test over the
whole command line. That was sufficient for the port takeover and is wrong for
everything else, because on Windows every MCC command runs out of a uv tool
environment whose directory is literally named ``my-claude-code``::

    "...\\uv\\tools\\my-claude-code\\Scripts\\python.exe" "...\\bin\\mcc-claude.exe"

A substring test over that line says "my-claude-code" and therefore says
"server" -- about a *coding agent* the user is sitting in front of. A rule that
answers that way must never be allowed anywhere near a process that gets
stopped, and the user's instruction is explicit: never match by image name, and
never touch anything but a server.

So identification here is deliberately narrow and structural:

* the **executable's own basename** is one of the four console scripts that
  ``pyproject.toml`` maps to ``entrypoints:serve`` (this catches the uv
  trampoline, whose command line is only ``"mcc-server"``), or
* one of the command line's **argument tokens** -- never the first token, which
  is the interpreter, and never a bare substring -- has such a basename (this
  catches the interpreter that the trampoline execs), or
* the command line runs ``-m my_claude_code…`` explicitly.

``mcc-claude`` and its sixteen sibling launchers are, by construction, not in
that set; neither is the user's unrelated ``python.exe``. There is exactly one
list of server names (:data:`SERVER_ENTRYPOINT_NAMES`) and exactly one bulk
scan (:func:`scan_processes`), and ``cli.port_takeover`` now reads both from
here rather than keeping a second copy.
"""

import os
import platform
import shlex
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_WINDOWS = platform.system() == "Windows"

#: The console scripts ``pyproject.toml`` maps to ``cli.entrypoints:serve``.
#: These four, and nothing else, start a server. ``mcc-desktop`` is the shell,
#: ``mcc-claude`` and friends are launchers; none of them binds the port and
#: none of them may ever be matched here.
SERVER_ENTRYPOINT_NAMES = frozenset(
    {
        "mcc-server",
        "my-claude-code",
        "fcc-server",
        "free-claude-code",
    }
)

#: Executable basenames that belong to MCC whatever they are doing -- the whole
#: command family, launchers included. Used only to answer "is this process
#: ours" for the port takeover, which may stop a holder of *the configured
#: port*; a launcher never holds it, so this staying broad costs nothing.
MCC_COMMAND_PREFIXES = (
    "mcc-",
    "fcc-",
    "my-claude-code",
    "free-claude-code",
)

#: Module paths that mean "an MCC process", for a command line that runs the
#: package rather than a console script.
_MCC_MODULE_PREFIXES = ("my_claude_code", "free_claude_code")

#: Substrings that identify MCC *anywhere* in a command line, including inside
#: an interpreter path. Deliberately NOT used by :attr:`ProcessFacts.is_mcc_server`
#: -- the uv tool environment is a directory called ``my-claude-code``, so this
#: list matches every launcher too. It answers one narrower question, asked by
#: :mod:`my_claude_code.cli.port_takeover`: "the process holding the port I am
#: configured to bind -- is it one of mine?" A launcher never holds that port,
#: so breadth is free there and fatal here.
MCC_COMMAND_SUBSTRINGS = (
    "mcc-server",
    "mcc_server",
    "mcc-desktop",
    "my_claude_code",
    "my-claude-code",
    "free_claude_code",
    "fcc-server",
)

#: Executable stems that are MCC and nothing else, for the same question.
MCC_IMAGE_NAMES = (
    "mcc-server",
    "mcc-desktop",
    "my-claude-code",
    "fcc-server",
    "fcc-desktop",
)

#: How long the bulk scan may take before the caller gives up on it. The scan
#: is a report, never a gate, so a slow machine loses the report and not the
#: start.
SCAN_TIMEOUT_SECONDS = 20.0

#: Seconds between the polite stop and the forced one, when a caller has
#: explicitly asked for a process to be stopped.
TERMINATE_GRACE_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class ProcessFacts:
    """What the operating system will say about one process. Read-only."""

    pid: int
    parent_pid: int | None = None
    #: The image name as the OS reports it, e.g. ``python.exe``.
    image: str | None = None
    #: The resolved path of the running image, when the OS will give one.
    executable: str | None = None
    #: The full command line, unparsed.
    command: str | None = None
    #: Process start time as a UTC epoch, when the OS will give one.
    started_at: float | None = None

    @property
    def argument_tokens(self) -> tuple[str, ...]:
        """The command line's tokens *after* the interpreter.

        The first token is the thing that was executed -- an interpreter, for
        every MCC process on Windows -- and it is exactly the token that must
        not be consulted, because its path contains the uv tool environment's
        ``my-claude-code`` directory for launchers and servers alike.
        """

        command = self.command or ""
        if not command.strip():
            return ()
        try:
            tokens = shlex.split(command, posix=not _WINDOWS)
        except ValueError:
            tokens = command.split()
        return tuple(token.strip('"') for token in tokens[1:])

    @property
    def is_mcc_server(self) -> bool:
        """Whether this process is an MCC *server*, by structure not by name."""

        if _basename_is_server(self.executable):
            return True
        if _basename_is_server(self.image):
            return True
        tokens = self.argument_tokens
        for index, token in enumerate(tokens):
            if _basename_is_server(token):
                return True
            if token == "-m" and index + 1 < len(tokens):
                module = tokens[index + 1]
                if module.startswith(_MCC_MODULE_PREFIXES):
                    return True
        return False

    @property
    def is_mcc(self) -> bool:
        """Whether this process belongs to MCC at all, launchers included.

        Broader than :attr:`is_mcc_server` on purpose, and used only where the
        question is "may the port takeover stop this holder". False for
        "cannot tell": an unreadable process is a stranger, so a policy that
        only stops our own never stops something it merely failed to read.
        """

        if self.is_mcc_server:
            return True
        for candidate in (self.executable, self.image):
            stem = _stem(candidate)
            if stem and stem.startswith(MCC_COMMAND_PREFIXES):
                return True
        for token in self.argument_tokens:
            stem = _stem(token)
            if stem and stem.startswith(MCC_COMMAND_PREFIXES):
                return True
            if token.startswith(_MCC_MODULE_PREFIXES):
                return True
        return False

    def describe(self) -> str:
        name = self.image or self.executable or "an unidentified process"
        return f"{name} (pid {self.pid})"


@dataclass(frozen=True, slots=True)
class ProcessChain:
    """One MCC server launch: the root command and everything it spawned.

    On Windows a single ``mcc-server`` start is three processes -- the uv
    trampoline, the tool environment's ``python.exe`` shim and the managed
    interpreter that actually runs the server -- all sharing a start time and a
    command line. Treating them as one thing is the difference between "two
    servers are running" and "six stray pythons are running", and it is the
    only grouping under which "stop this server" means anything.
    """

    #: The outermost MCC process of the launch.
    root: ProcessFacts
    #: Root first, then every descendant, in discovery order.
    members: tuple[ProcessFacts, ...] = field(default_factory=tuple)

    @property
    def pids(self) -> tuple[int, ...]:
        return tuple(member.pid for member in self.members)

    @property
    def leaf(self) -> ProcessFacts:
        """The innermost member -- the interpreter that runs the server."""

        return self.members[-1] if self.members else self.root

    def holds(self) -> tuple[str, ...]:
        """The distinct executable paths this chain keeps open, for a report.

        These are precisely the files an install cannot replace while the
        chain lives: the launcher trampoline in the bin directory and the tool
        environment's interpreter.
        """

        seen: list[str] = []
        for member in self.members:
            path = member.executable
            if path and path not in seen:
                seen.append(path)
        return tuple(seen)

    def describe(self) -> str:
        return f"{self.root.describe()} -> pid {self.leaf.pid}"


def _stem(path: str | None) -> str:
    """The lowercased basename of ``path`` without its extension."""

    if not path:
        return ""
    return Path(path.strip().strip('"')).stem.lower()


def _basename_is_server(path: str | None) -> bool:
    return _stem(path) in SERVER_ENTRYPOINT_NAMES


# ------------------------------------------------------------------- scanning

_WINDOWS_SCAN_SCRIPT = (
    "Get-CimInstance Win32_Process | ForEach-Object { "
    "$created = ''; "
    "if ($_.CreationDate) "
    "{ $created = $_.CreationDate.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } "
    "$fields = @($_.ProcessId, $_.ParentProcessId, $_.Name, $created, "
    "$_.ExecutablePath, $_.CommandLine) | ForEach-Object "
    "{ ([string]$_) -replace '[\\r\\n\\t]', ' ' }; "
    "[string]::Join([char]9, $fields) }"
)


def _parse_windows_scan(output: str) -> list[ProcessFacts]:
    facts: list[ProcessFacts] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0].strip())
        except ValueError:
            continue
        parent: int | None = None
        with suppress(ValueError):
            parent = int(parts[1].strip())
        facts.append(
            ProcessFacts(
                pid=pid,
                parent_pid=parent,
                image=parts[2].strip() or None,
                started_at=_parse_utc_stamp(parts[3].strip()),
                executable=parts[4].strip() or None,
                command=parts[5].strip() or None,
            )
        )
    return facts


def _parse_utc_stamp(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    import calendar

    return float(calendar.timegm(parsed))


def _scan_windows(timeout: float) -> list[ProcessFacts]:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_SCAN_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return []
    if completed.returncode != 0:
        return []
    return _parse_windows_scan(completed.stdout)


def _scan_posix() -> list[ProcessFacts]:
    facts: list[ProcessFacts] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        command = None
        with suppress(OSError):
            raw = (entry / "cmdline").read_bytes()
            if raw:
                command = shlex.join(
                    part
                    for part in raw.decode("utf-8", errors="replace").split("\0")
                    if part
                )
        image = None
        with suppress(OSError):
            image = (entry / "comm").read_text(encoding="utf-8").strip() or None
        executable = None
        with suppress(OSError):
            executable = os.readlink(entry / "exe") or None
        parent = None
        started_at = None
        with suppress(OSError, ValueError, IndexError):
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            # The command name is parenthesised and may itself contain spaces
            # and parentheses, so the fields after it are found from the LAST
            # closing parenthesis, never by splitting the whole line.
            tail = stat_text[stat_text.rindex(")") + 1 :].split()
            parent = int(tail[1])
            started_at = _posix_start_time(float(tail[19]))
        facts.append(
            ProcessFacts(
                pid=pid,
                parent_pid=parent,
                image=image,
                executable=executable,
                command=command,
                started_at=started_at,
            )
        )
    return facts


def _posix_start_time(ticks_after_boot: float) -> float | None:
    try:
        ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
    except OSError, ValueError, AttributeError:
        return None
    if ticks_per_second <= 0:
        return None
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except OSError, ValueError, IndexError:
        return None
    return time.time() - uptime + (ticks_after_boot / ticks_per_second)


def scan_processes(*, timeout: float = SCAN_TIMEOUT_SECONDS) -> list[ProcessFacts]:
    """Every process this user can see, in one pass. Never signals anyone.

    One call, not one per pid: the per-pid ``tasklist``/``Get-CimInstance``
    pair that :mod:`cli.port_takeover` uses costs about a second each, which is
    fine for the one holder of one port and unaffordable for a sweep.
    """

    if _WINDOWS:
        return _scan_windows(timeout)
    return _scan_posix()


def mcc_server_chains(
    processes: list[ProcessFacts] | None = None,
    *,
    timeout: float = SCAN_TIMEOUT_SECONDS,
) -> list[ProcessChain]:
    """Group every MCC *server* process into one chain per launch.

    A chain is rooted at the outermost MCC server process -- the one whose
    parent is not itself an MCC server process -- and carries every descendant
    below it. Passing ``processes`` lets a caller reuse a scan (and lets the
    tests supply a fake process table outright).
    """

    facts = scan_processes(timeout=timeout) if processes is None else processes
    by_pid = {item.pid: item for item in facts}
    servers = {item.pid: item for item in facts if item.is_mcc_server}
    children: dict[int, list[ProcessFacts]] = {}
    for item in facts:
        if item.parent_pid is not None:
            children.setdefault(item.parent_pid, []).append(item)

    def is_rooted_here(item: ProcessFacts) -> bool:
        parent_pid = item.parent_pid
        if parent_pid is None or parent_pid == item.pid:
            return True
        parent = by_pid.get(parent_pid)
        if parent is None:
            return True
        # A parent that is itself a server process of the same launch makes
        # this a member, not a root. A parent that merely *spawned* a server
        # (the desktop shell, a terminal) is not part of the chain.
        return parent.pid not in servers

    chains: list[ProcessChain] = []
    for item in facts:
        if item.pid not in servers or not is_rooted_here(item):
            continue
        members: list[ProcessFacts] = [item]
        queue = list(children.get(item.pid, ()))
        seen = {item.pid}
        while queue:
            child = queue.pop(0)
            if child.pid in seen:
                continue
            seen.add(child.pid)
            # Only MCC's own processes join the chain. A server's descendants
            # include things that are emphatically not it -- Windows attaches a
            # ``conhost.exe`` to the trampoline, and a server may spawn a
            # launcher -- and a chain is not merely a report: it is the list of
            # pids the opt-in sweep would signal. Nothing that is not ours may
            # ever be on it.
            if not child.is_mcc:
                continue
            members.append(child)
            queue.extend(children.get(child.pid, ()))
        chains.append(ProcessChain(root=item, members=tuple(members)))
    return chains


# ------------------------------------------------------------ listening ports


def listening_pids(*, timeout: float = 5.0) -> frozenset[int]:
    """Every pid that owns a listening TCP socket, on any address or port.

    "Owns no socket on its configured port" is not evidence of anything: a
    second MCC instance on another port is perfectly legitimate, and treating
    it as abandoned is how a product comes to stop a server its user is
    actively using. The question that matters is whether the process is
    listening *at all*, so this asks about every port.

    Returns an empty set when neither tool can be run, and an empty set is
    deliberately indistinguishable from "nothing is listening": a caller that
    cannot enumerate sockets must not conclude anything about a process.
    """

    for command in (("ss", "-ltnp"), ("netstat", "-ano")):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            continue
        if completed.returncode != 0:
            continue
        found = _listening_pids_from(completed.stdout)
        if found:
            return found
    return frozenset()


def _listening_pids_from(output: str) -> frozenset[int]:
    pids: set[int] = set()
    for line in output.splitlines():
        lowered = line.lower()
        if "listen" not in lowered:
            continue
        # ``ss -ltnp`` spells it ``pid=1234``; ``netstat -ano`` puts the pid in
        # the trailing column.
        marker = lowered.find("pid=")
        if marker >= 0:
            digits = ""
            for char in lowered[marker + 4 :]:
                if not char.isdigit():
                    break
                digits += char
            if digits:
                pids.add(int(digits))
            continue
        trailing = line.split()
        if trailing and trailing[-1].isdigit():
            pids.add(int(trailing[-1]))
    return frozenset(pids)


# ------------------------------------------------------------------- liveness


def process_is_alive(pid: int) -> bool:
    """Whether ``pid`` currently exists. Never signals it with anything real."""

    if _WINDOWS:
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            # Unknown means alive: refusing to answer must never be read as
            # "gone", because "gone" is what licenses a caller to act.
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


def stop_process(pid: int, *, grace: float = TERMINATE_GRACE_SECONDS) -> bool:
    """Stop exactly ``pid``: ask, wait, then force. Returns whether it is gone.

    By pid, never by image name. The one time MCC stopped processes by image
    name it stopped the user's own running application, twice.
    """

    if pid == os.getpid():
        return False
    with suppress(OSError, ValueError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except AttributeError, OSError, ValueError:
        if not _WINDOWS:
            return False
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return False
    return not process_is_alive(pid)


def stop_chain(chain: ProcessChain, *, grace: float = TERMINATE_GRACE_SECONDS) -> bool:
    """Stop every member of ``chain``, innermost first. Returns whether all went.

    Innermost first because the outer members are uv trampolines that exist
    only to wait on the inner one; stopping the root first leaves the
    interpreter -- the process that actually holds the tool environment open --
    running with no parent, which is the exact shape of the leak this is here
    to clear.
    """

    gone = True
    for member in reversed(chain.members):
        if not stop_process(member.pid, grace=grace):
            logger.warning(
                "Could not stop {who} while stopping {chain}.",
                who=member.describe(),
                chain=chain.describe(),
            )
            gone = False
    return gone
