"""The Coding agents page's Install button: plan, run, verify (7.102.0).

**The boundary (WORKING-NOTES IV.3), unchanged and now said out loud:** the
installer installs MCC and its prerequisites, nothing else; agents are
installed only by the dashboard button you press. Nothing here runs from the
installer, the updater, first start, a launcher or a background task. A run
starts only from ``POST /admin/api/agent-installs/{id}/run`` with the confirm
token, a local Origin and the token of a plan the server wrote and the page
showed.

What a run does, and the rules it keeps (spec N3):

* **One job at a time**, in this process and across processes sharing the
  config folder (a lock file beside the settings).
* **Vendor scripts are downloaded to a file first**: https only, every hop of
  every redirect on the method's own host allow-list, the SHA-256 logged and
  shown. They are then run *by path with the interpreter named*
  (``powershell -NoProfile -ExecutionPolicy Bypass -File <file>``,
  ``bash <file>``, ``sh <file>``). Never ``iex`` of a string, never a shell.
* npm runs as ``npm.cmd`` / ``npm`` with argv.
* Every step has a bound. Output streams to the job and to
  ``<config>/logs/agent-installs/<id>-<stamp>.log``.
* The user's environment is inherited unchanged (a vendor's own documented
  variable, like Goose's ``CONFIGURE=false``, is added for its one step and
  named in the plan). MCC never adds ``sudo``, ``runas`` or
  ``--unsafe-perm``, never runs as root on behalf of a ``SUDO_USER``, and
  never elevates: an elevation is a prompt the operating system shows.
* **Node.js** is installed only when it is missing (never upgraded: decision
  3). On Windows the official LTS ``.msi`` (checked against the release's
  ``SHASUMS256.txt``; Windows asks for permission), on macOS the official
  ``.pkg`` (macOS Installer asks), on Linux/WSL fnm per user. A global npm
  folder the user cannot write is reported, never ``sudo``-ed.
* **Afterwards** the program is looked for with the refreshed PATH (the
  registry on Windows, the vendor's known folders), then ``<binary>
  --version`` runs with a 5 s bound, and the result says what is true:
  installed and verified (with version and path), installed but not on this
  server's PATH yet (open a new terminal), or a classified failure.
* ``dry_run`` walks the plan and writes "would run" lines. It spawns nothing
  and downloads nothing.
"""

import contextlib
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from my_claude_code.config.desktop_apply import is_installed as desktop_is_installed
from my_claude_code.config.desktop_apps import DESKTOP_APPS, DesktopAppSpec
from my_claude_code.config.harnesses import HarnessSpec, harness_specs
from my_claude_code.config.install_methods import (
    DESKTOP_SAME_AS_CLI,
    NODE_INSTALLERS,
    DownloadAction,
    Elevation,
    InstallKind,
    InstallMethod,
    InstallOs,
    Interpreter,
    NodeInstaller,
    NodeRequirement,
    methods_for_os,
)
from my_claude_code.config.paths import agent_install_lock_path, agent_install_logs_dir
from my_claude_code.core.mcc_processes import process_is_alive

#: ``<binary> --version`` and every Node/npm probe: the spec's 5 s.
PROBE_TIMEOUT_SECONDS = 5.0
#: A download that sends nothing for this long is a failed download.
DOWNLOAD_READ_TIMEOUT_SECONDS = 60.0
DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 30.0
#: Redirect hops a download follows, each checked against the allow-list.
MAX_REDIRECTS = 8
#: How many lines a job keeps in memory for the page (the log keeps all).
JOB_LINE_LIMIT = 4000
#: The failure panel's tail (spec N5).
FAILURE_TAIL_LINES = 40
#: How long a finished job's state stays readable by its id.
FINISHED_JOB_TTL_SECONDS = 3600.0
#: The words a run checks the user's own text for before it is spawned. A
#: method row holding one would be a bug the registry guard catches first.
FORBIDDEN_ARGV_WORDS = ("sudo", "runas", "--unsafe-perm")

_WINDOWS = os.name == "nt"


class InstallError(Exception):
    """A plan or a run that cannot go ahead, with the reason a reader sees."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


class InstallBusyError(InstallError):
    """Another Install run is in progress (here or in another MCC process)."""


class StepFailedError(Exception):
    """One step of a run failed; carries the classification for the panel."""

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        exit_code: int | None = None,
        command: str = "",
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.exit_code = exit_code
        self.command = command


# --------------------------------------------------------------------- machine


@dataclass(frozen=True, slots=True)
class Machine:
    """The facts a plan depends on about the computer the server runs on."""

    os: InstallOs
    arch: str
    wsl: bool = False
    headless: bool = False
    headless_reason: str = ""


def _platform() -> str:
    """``sys.platform``, behind a seam a test can pin without touching ``sys``."""

    return sys.platform


def _machine_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64", "armv8", "armv8l"}:
        return "arm64"
    return "x64"


def _is_wsl() -> bool:
    try:
        text = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in text.lower()


def _windows_session_id() -> int | None:
    """Return this process's Windows session (0 = a service), or None."""

    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        session = ctypes.c_ulong(0)
        if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return None
        return int(session.value)
    except OSError, AttributeError, ValueError:
        return None


def current_machine(environ: Mapping[str, str] | None = None) -> Machine:
    """Return the machine the server runs on, and whether it has a desktop.

    Headless is the same rule as MCC's own desktop app: WSL is server-only. A
    Windows service (session 0) or an SSH session has no desktop a window or
    an administrator prompt could appear on; a Linux box with neither
    ``DISPLAY`` nor ``WAYLAND_DISPLAY`` has none either.
    """

    env = os.environ if environ is None else environ
    arch = _machine_arch()
    remote = bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))
    here = _platform()
    if here == "win32":
        session = _windows_session_id()
        if session == 0:
            return Machine(
                InstallOs.WINDOWS,
                arch,
                headless=True,
                headless_reason="this server runs as a Windows service (session 0)",
            )
        if remote:
            return Machine(
                InstallOs.WINDOWS,
                arch,
                headless=True,
                headless_reason="this server runs in an SSH session",
            )
        return Machine(InstallOs.WINDOWS, arch)
    if here == "darwin":
        if remote:
            return Machine(
                InstallOs.MACOS,
                arch,
                headless=True,
                headless_reason="this server runs in an SSH session",
            )
        return Machine(InstallOs.MACOS, arch)
    wsl = _is_wsl()
    if wsl:
        return Machine(
            InstallOs.LINUX,
            arch,
            wsl=True,
            headless=True,
            headless_reason="this server runs inside WSL (server only)",
        )
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return Machine(
            InstallOs.LINUX,
            arch,
            headless=True,
            headless_reason="this server has no display (no DISPLAY or WAYLAND_DISPLAY)",
        )
    if remote:
        return Machine(
            InstallOs.LINUX,
            arch,
            headless=True,
            headless_reason="this server runs in an SSH session",
        )
    return Machine(InstallOs.LINUX, arch)


# --------------------------------------------------------------------- targets


@dataclass(frozen=True, slots=True)
class InstallTarget:
    """One card that can carry an Install button: a CLI or a desktop app."""

    id: str
    kind: str
    display_name: str
    #: For a CLI, its binary then every alias; empty for a desktop app.
    binaries: tuple[str, ...]
    methods: tuple[InstallMethod, ...]
    unavailable_reason: str = ""
    desktop: DesktopAppSpec | None = None
    #: A desktop card that is the same program as a CLI card.
    same_as: str = ""
    #: Never run this program, not even ``--version`` (Antigravity).
    never_run: bool = False


def _cli_target(spec: HarnessSpec) -> InstallTarget:
    return InstallTarget(
        id=spec.id,
        kind="cli",
        display_name=spec.display_name,
        binaries=(spec.binary, *spec.binary_aliases),
        methods=spec.install_methods,
        unavailable_reason=spec.install_unavailable_reason,
        never_run=not spec.available,
    )


def _desktop_target(spec: DesktopAppSpec) -> InstallTarget:
    return InstallTarget(
        id=spec.id,
        kind="desktop",
        display_name=spec.display_name,
        binaries=(),
        methods=spec.install_methods,
        desktop=spec,
        same_as=DESKTOP_SAME_AS_CLI.get(spec.id, ""),
    )


def install_targets() -> tuple[InstallTarget, ...]:
    """Return every CLI, then every desktop app with something to offer.

    A desktop card with no method and no CLI twin (Antigravity's, whose id
    is also a CLI id) is left out, so every target id is unique.
    """

    clis = tuple(_cli_target(spec) for spec in harness_specs())
    desktops = tuple(
        _desktop_target(spec)
        for spec in DESKTOP_APPS
        if spec.install_methods or spec.id in DESKTOP_SAME_AS_CLI
    )
    return clis + desktops


def install_target(target_id: str) -> InstallTarget:
    """Return one target by id, or raise ``InstallError`` (404)."""

    for target in install_targets():
        if target.id == target_id:
            return target
    raise InstallError(f"Unknown agent: {target_id}", status=404)


# ------------------------------------------------------------------- detection


def _expand(entry: str, environ: Mapping[str, str]) -> str:
    """Expand ``~`` and ``%NAME%`` (and ``$NAME``) against ``environ``."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        # The exact spelling first: a mapping can hold both ``ProgramFiles``
        # and ``PROGRAMFILES``, and the one the caller set is the one meant.
        if name in environ:
            return environ[name]
        for key, value in environ.items():
            if key.upper() == name.upper():
                return value
        return match.group(0)

    text = re.sub(r"%([A-Za-z_][A-Za-z0-9_()]*)%", replace, entry)
    if text.startswith("~"):
        home = environ.get("HOME") or environ.get("USERPROFILE") or str(Path.home())
        text = home + text[1:]
    return os.path.normpath(text)


def programs_dir(app_name: str, environ: Mapping[str, str]) -> str:
    """Return ``%LOCALAPPDATA%\\Programs\\<app>``, where a .zip is unpacked."""

    return _expand(f"%LOCALAPPDATA%/Programs/{app_name}", environ)


def _registry_path(environ: Mapping[str, str]) -> str:
    """Return the PATH Windows would hand a NEW process, read-only.

    A process inherits PATH when it starts, so after an installer adds a
    folder the server's own PATH does not show it. The machine and user
    ``Path`` values in the registry do.
    """

    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - win32 always has it
        return ""
    parts: list[str] = []
    for root, subkey in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        parts.extend(
            _expand(entry.strip(), environ)
            for entry in str(value).split(os.pathsep)
            if entry.strip()
        )
    return os.pathsep.join(parts)


def _server_path(environ: Mapping[str, str]) -> str:
    return environ.get("PATH") or environ.get("Path") or ""


def _which(names: Sequence[str], path_value: str) -> str | None:
    if not path_value:
        return None
    for name in names:
        found = shutil.which(name, path=path_value)
        if found:
            return os.path.abspath(found)
    return None


def refreshed_path(environ: Mapping[str, str], extra_dirs: Sequence[str] = ()) -> str:
    """Return the server's PATH, then the registry's, then known folders."""

    parts = [_server_path(environ), _registry_path(environ)]
    parts.extend(_expand(entry, environ) for entry in extra_dirs)
    return os.pathsep.join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class FoundProgram:
    """Where a program was found, and whether the server's own PATH has it."""

    path: str
    on_server_path: bool


def find_program(
    names: Sequence[str],
    environ: Mapping[str, str],
    extra_dirs: Sequence[str] = (),
) -> FoundProgram | None:
    """Look on the server's PATH first, then the refreshed PATH and folders."""

    found = _which(names, _server_path(environ))
    if found:
        return FoundProgram(found, True)
    found = _which(names, refreshed_path(environ, extra_dirs))
    if found:
        return FoundProgram(found, False)
    return None


_VERSION_CACHE: dict[tuple[str, int, int], tuple[float, str]] = {}
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE_TTL_SECONDS = 600.0


def _creationflags() -> int:
    if not _WINDOWS:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def probe_output(argv: Sequence[str], environ: Mapping[str, str]) -> tuple[int, str]:
    """Run a read-only probe (``--version``, ``config get``) for at most 5 s."""

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=PROBE_TIMEOUT_SECONDS,
        env=dict(environ),
        check=False,
        creationflags=_creationflags(),
    )
    text = (completed.stdout or b"").decode("utf-8", "replace")
    if not text.strip():
        text = (completed.stderr or b"").decode("utf-8", "replace")
    return completed.returncode, text


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


def version_text(path: str, environ: Mapping[str, str]) -> str:
    """Return ``<path> --version``'s first line, cached by the file's identity."""

    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = (path, stat.st_mtime_ns, stat.st_size)
    now = time.monotonic()
    with _VERSION_CACHE_LOCK:
        cached = _VERSION_CACHE.get(key)
        if cached is not None and now - cached[0] < _VERSION_CACHE_TTL_SECONDS:
            return cached[1]
    try:
        code, text = probe_output([path, "--version"], environ)
    except OSError, subprocess.SubprocessError:
        return ""
    line = _first_line(text) if code == 0 else ""
    with _VERSION_CACHE_LOCK:
        _VERSION_CACHE[key] = (now, line)
    return line


def clear_probe_caches() -> None:
    """Forget cached versions and Node status (after a run, and in tests)."""

    with _VERSION_CACHE_LOCK:
        _VERSION_CACHE.clear()
    with _NODE_CACHE_LOCK:
        _NODE_CACHE.clear()


# ---------------------------------------------------------------- Node and npm


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """What Node.js and npm look like to the server, read-only (spec §5)."""

    #: ``missing``, ``ok`` or ``unwritable`` (a global npm folder the user
    #: cannot write). "Too old" is per package: see :meth:`satisfies`.
    state: str
    node_path: str = ""
    node_version: str = ""
    npm_path: str = ""
    npm_version: str = ""
    prefix: str = ""
    prefix_writable: bool = False
    #: False when Node was found only in the registry PATH or a known folder.
    on_server_path: bool = True

    @property
    def version_parts(self) -> tuple[int, ...]:
        match = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", self.node_version)
        if not match:
            return ()
        return tuple(int(part) for part in match.groups() if part is not None)

    def satisfies(self, requirement: NodeRequirement) -> bool:
        """Return whether this Node.js meets a package's bound."""

        have = self.version_parts
        if not have:
            return False
        need = requirement.minimum_parts
        width = max(len(have), len(need))
        return have + (0,) * (width - len(have)) >= need + (0,) * (width - len(need))

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "node_path": self.node_path,
            "node_version": self.node_version,
            "npm_path": self.npm_path,
            "npm_version": self.npm_version,
            "prefix": self.prefix,
            "prefix_writable": self.prefix_writable,
            "on_server_path": self.on_server_path,
        }


def fnm_dir(environ: Mapping[str, str]) -> str:
    """Return fnm's base folder the way fnm resolves it."""

    if environ.get("FNM_DIR"):
        return environ["FNM_DIR"]
    if environ.get("XDG_DATA_HOME"):
        return os.path.join(environ["XDG_DATA_HOME"], "fnm")
    return _expand("~/.local/share/fnm", environ)


def node_known_dirs(machine: Machine, environ: Mapping[str, str]) -> tuple[str, ...]:
    """Where a Node.js installed by the button lands, by OS."""

    if machine.os is InstallOs.WINDOWS:
        return tuple(
            _expand(d, environ) for d in NODE_INSTALLERS[machine.os].verify_dirs
        )
    if machine.os is InstallOs.MACOS:
        return ("/usr/local/bin",)
    base = fnm_dir(environ)
    return (
        os.path.join(base, "aliases", "default", "bin"),
        _expand("~/.fnm/aliases/default/bin", environ),
    )


def _writable(path: str) -> bool:
    """Return whether npm could write its global folder here."""

    candidate = Path(path)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent
    return os.access(candidate, os.W_OK)


_NODE_CACHE: dict[str, tuple[float, NodeStatus]] = {}
_NODE_CACHE_LOCK = threading.Lock()
_NODE_CACHE_TTL_SECONDS = 60.0


def detect_node(
    machine: Machine, environ: Mapping[str, str], *, cached: bool = True
) -> NodeStatus:
    """Return Node.js/npm state. Spawns only programs already found."""

    now = time.monotonic()
    if cached:
        with _NODE_CACHE_LOCK:
            hit = _NODE_CACHE.get("node")
            if hit is not None and now - hit[0] < _NODE_CACHE_TTL_SECONDS:
                return hit[1]
    status = _detect_node(machine, environ)
    with _NODE_CACHE_LOCK:
        _NODE_CACHE["node"] = (now, status)
    return status


def _detect_node(machine: Machine, environ: Mapping[str, str]) -> NodeStatus:
    node = find_program(("node",), environ, node_known_dirs(machine, environ))
    if node is None:
        return NodeStatus(state="missing")
    probe_env = _env_with_dir_first(environ, os.path.dirname(node.path))
    try:
        code, text = probe_output([node.path, "--version"], probe_env)
    except OSError, subprocess.SubprocessError:
        code, text = 1, ""
    node_version = _first_line(text) if code == 0 else ""
    npm_path = _which(("npm",), os.path.dirname(node.path)) or _which(
        ("npm",), _server_path(environ)
    )
    if npm_path is None:
        return NodeStatus(
            state="missing",
            node_path=node.path,
            node_version=node_version,
            on_server_path=node.on_server_path,
        )
    npm_version = ""
    prefix = ""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        code, text = probe_output([npm_path, "--version"], probe_env)
        npm_version = _first_line(text) if code == 0 else ""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        code, text = probe_output([npm_path, "config", "get", "prefix"], probe_env)
        prefix = _first_line(text) if code == 0 else ""
    writable = bool(prefix) and _writable(prefix)
    return NodeStatus(
        state="ok" if writable or not prefix else "unwritable",
        node_path=node.path,
        node_version=node_version,
        npm_path=npm_path,
        npm_version=npm_version,
        prefix=prefix,
        prefix_writable=writable,
        on_server_path=node.on_server_path,
    )


def _env_with_dir_first(environ: Mapping[str, str], directory: str) -> dict[str, str]:
    """Return ``environ`` with one folder first on PATH, for one child only."""

    env = dict(environ)
    key = "Path" if "Path" in env and "PATH" not in env else "PATH"
    current = env.get(key, "")
    if directory and directory not in current.split(os.pathsep):
        env[key] = os.pathsep.join(part for part in (directory, current) if part)
    return env


# ------------------------------------------------------------------- the state


@dataclass(frozen=True, slots=True)
class TargetState:
    """Whether a card's program is installed, and where."""

    installed: bool
    path: str = ""
    version: str = ""
    on_server_path: bool = True


def target_state(
    target: InstallTarget,
    environ: Mapping[str, str],
    machine: Machine | None = None,
    *,
    with_version: bool = True,
) -> TargetState:
    """Return the installed state the card shows (any alias counts).

    The server's PATH first -- the same lookup the launcher makes -- then the
    folders this OS's methods install into, so a program a vendor installer
    put in ``~/.local/bin`` before this server's PATH knew about it still
    reads as installed and gets no second copy. ``--version`` runs only for
    a program found, never for Antigravity.
    """

    if target.kind == "cli":
        found_path = _which(target.binaries, _server_path(environ))
        on_path = True
        if found_path is None:
            here = machine or current_machine(environ)
            dirs = [
                _expand(entry, environ)
                for method in methods_for_os(target.methods, here.os, here.arch)
                for entry in method.verify_dirs
            ]
            found_path = _which(target.binaries, os.pathsep.join(dirs))
            on_path = False
        if found_path is None:
            return TargetState(installed=False)
        version = (
            ""
            if target.never_run or not with_version
            else version_text(found_path, environ)
        )
        return TargetState(True, found_path, version, on_path)
    spec = target.desktop
    if spec is None:
        return TargetState(installed=False)
    return TargetState(installed=desktop_is_installed(spec, environ))


def _program_available(names: Sequence[str], environ: Mapping[str, str]) -> str:
    return _which(names, _server_path(environ)) or ""


def _interpreter_path(
    interpreter: Interpreter, machine: Machine, environ: Mapping[str, str]
) -> str:
    if interpreter is Interpreter.POWERSHELL:
        if machine.os is not InstallOs.WINDOWS:
            return ""
        root = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
        candidate = os.path.join(
            root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
        )
        return candidate if os.path.isfile(candidate) else ""
    if machine.os is InstallOs.WINDOWS:
        # Never a bash on Windows: plain ``bash`` there is WSL's, and an MCC
        # on Windows installs for Windows (user decision 7).
        return ""
    return _program_available((interpreter.value,), environ)


def _system32(name: str, environ: Mapping[str, str]) -> str:
    root = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
    return os.path.join(root, "System32", name)


# ------------------------------------------------------------------------ plan


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One thing a run does, in words and as the exact command."""

    kind: str
    text: str
    #: The argv, with ``<downloaded file>`` where the downloaded path goes.
    command: tuple[str, ...] = ()
    url: str = ""
    hosts: tuple[str, ...] = ()
    timeout_seconds: int = 0
    env: tuple[tuple[str, str], ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "command": list(self.command),
            "url": self.url,
            "hosts": list(self.hosts),
            "timeout_seconds": self.timeout_seconds,
            "env": dict(self.env),
        }


DOWNLOADED = "<downloaded file>"


@dataclass(frozen=True, slots=True)
class Plan:
    """Everything a run would do, as the page shows it before the button."""

    target: InstallTarget
    method: InstallMethod
    machine: Machine
    node: NodeStatus
    runnable: bool
    refused: str
    steps: tuple[PlanStep, ...]
    elevation: tuple[str, ...]
    changes: tuple[str, ...]
    node_text: str
    node_install: bool
    verify_names: tuple[str, ...]
    verify_dirs: tuple[str, ...]

    def body(self) -> dict[str, Any]:
        method = self.method
        return {
            "target": self.target.id,
            "kind": self.target.kind,
            "display_name": self.target.display_name,
            "os": self.machine.os.value,
            "arch": self.machine.arch,
            "method": method_payload(method),
            "runnable": self.runnable,
            "refused": self.refused,
            "steps": [step.payload() for step in self.steps],
            "elevation": list(self.elevation),
            "changes": list(self.changes),
            "node": {
                "needed": method.needs_node is not None,
                "bound": method.needs_node.label if method.needs_node else "",
                "bound_source": method.needs_node.source if method.needs_node else "",
                "install": self.node_install,
                "text": self.node_text,
                "status": self.node.payload(),
            },
            "bins": list(method.bins),
            "verify": {
                "names": list(self.verify_names),
                "dirs": list(self.verify_dirs),
            },
            "copy_command": method.printed_command or method.copy_command,
            "doc_url": method.doc_url,
        }

    @property
    def token(self) -> str:
        """A hash of the plan: a run must carry the plan the page showed."""

        canonical = json.dumps(self.body(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, Any]:
        return {**self.body(), "plan_token": self.token}


def method_payload(method: InstallMethod) -> dict[str, Any]:
    return {
        "id": method.id,
        "label": method.label,
        "kind": method.kind.value,
        "os": method.os.value,
        "arch": method.arch,
        "url": method.url,
        "package": method.package,
        "elevates": method.elevates.value,
        "changes": list(method.changes),
        "doc_url": method.doc_url,
        "checked_on": method.checked_on,
        "checked_by": method.checked_by,
        "copy_command": method.printed_command or method.copy_command,
        "needs_node": (
            {"minimum": method.needs_node.minimum, "source": method.needs_node.source}
            if method.needs_node
            else None
        ),
        "gui": method.gui,
    }


_ELEVATION_WORDS = {
    InstallOs.WINDOWS: "Windows will ask for administrator permission",
    InstallOs.MACOS: "macOS Installer will ask for your password",
    InstallOs.LINUX: "your system will ask for permission",
}


def _native_alternative(target: InstallTarget, machine: Machine) -> str:
    for method in methods_for_os(target.methods, machine.os, machine.arch):
        if method.kind is not InstallKind.NPM and method.runnable:
            return method.label
    return ""


def _node_install_plan(
    machine: Machine, method: InstallMethod
) -> tuple[list[PlanStep], str, list[str], list[str]]:
    """Return the Node.js steps, their words, the elevation and the changes."""

    installer = NODE_INSTALLERS[machine.os]
    steps: list[PlanStep] = []
    if installer.kind in {"msi", "pkg"}:
        suffix = f"{machine.arch}.msi" if installer.kind == "msi" else "pkg"
        steps.append(
            PlanStep(
                kind="node",
                text=(
                    "Read the newest Node.js LTS release from nodejs.org, download "
                    f"its installer (node-<version>-{suffix} or .pkg) and "
                    "SHASUMS256.txt, and check the installer's SHA-256 against it"
                ),
                url=installer.url,
                hosts=installer.hosts,
                timeout_seconds=installer.timeout_seconds,
            )
        )
        if installer.kind == "msi":
            command: tuple[str, ...] = (
                "msiexec.exe",
                "/i",
                DOWNLOADED,
                "/passive",
                "/norestart",
            )
        else:
            command = ("/usr/bin/open", "-W", DOWNLOADED)
        steps.append(
            PlanStep(
                kind="node",
                text="Open the Node.js installer by path",
                command=command,
                timeout_seconds=installer.timeout_seconds,
            )
        )
        text = (
            "Node.js LTS will be installed first (the newest LTS nodejs.org lists "
            f"when you press Install), then {method.package}."
        )
        elevation = [f"{_ELEVATION_WORDS[machine.os]} (the Node.js installer)"]
    else:
        steps.append(
            PlanStep(
                kind="node",
                text="Download fnm's install script and run it with bash, per user",
                command=("bash", DOWNLOADED),
                url=installer.url,
                hosts=installer.hosts,
                timeout_seconds=installer.timeout_seconds,
            )
        )
        steps.append(
            PlanStep(
                kind="node",
                text="Install Node.js LTS with fnm and make it fnm's default",
                command=("<fnm>", "install", "--lts"),
                timeout_seconds=installer.timeout_seconds,
            )
        )
        text = (
            "Node.js LTS will be installed first with fnm, per user and without "
            f"sudo, then {method.package}."
        )
        elevation = []
    return steps, text, elevation, list(installer.changes)


def _refuse(plan_kwargs: dict[str, Any], reason: str) -> dict[str, Any]:
    plan_kwargs["runnable"] = False
    plan_kwargs["refused"] = reason
    return plan_kwargs


def build_plan(
    target: InstallTarget,
    method_id: str,
    machine: Machine,
    node: NodeStatus,
    environ: Mapping[str, str],
    *,
    state: TargetState | None = None,
) -> Plan:
    """Return the plan for one method. Pure: it spawns and downloads nothing.

    ``state`` is the card's installed state (computed by the caller, which may
    spawn ``--version``); ``None`` means "look, without ``--version``".
    """

    methods = methods_for_os(target.methods, machine.os, machine.arch)
    method = next((m for m in methods if m.id == method_id), None)
    if method is None:
        raise InstallError(
            f"{target.display_name} has no install method {method_id!r} on "
            f"{machine.os.value}",
            status=400,
        )
    current = state or target_state(target, environ, machine, with_version=False)
    steps: list[PlanStep] = []
    elevation: list[str] = []
    changes: list[str] = list(method.changes)
    node_text = ""
    node_install = False
    kwargs: dict[str, Any] = {"runnable": True, "refused": ""}

    if target.same_as:
        _refuse(
            kwargs,
            f"{target.display_name} is the same program as its CLI card: install "
            "it from that card's Install button.",
        )
    elif current.installed:
        where = f" at {current.path}" if current.path else ""
        _refuse(
            kwargs,
            f"{target.display_name} is already installed{where}. MCC never "
            "installs a second copy.",
        )
    elif method.kind is InstallKind.LINK:
        _refuse(
            kwargs,
            "The vendor publishes no direct download MCC can open here: use "
            "the vendor's page.",
        )
    elif method.elevates is Elevation.SUDO_PRINTED_ONLY:
        _refuse(
            kwargs,
            "This needs sudo, which MCC never runs. Copy the command and run "
            "it yourself.",
        )
    elif machine.headless and (target.kind == "desktop" or method.gui):
        _refuse(
            kwargs,
            f"Desktop apps are not installed from a server without a desktop: "
            f"{machine.headless_reason}.",
        )
    elif machine.headless and method.elevates is Elevation.OS_PROMPT:
        _refuse(
            kwargs,
            f"This method needs a permission prompt the operating system shows, "
            f"and {machine.headless_reason}, so nothing could show it.",
        )

    if method.elevates is Elevation.OS_PROMPT:
        elevation.append(_ELEVATION_WORDS[machine.os] + " if the installer needs it")

    if kwargs["runnable"]:
        _plan_method_steps(method, machine, node, environ, steps, kwargs)

    if kwargs["runnable"] and method.kind is InstallKind.NPM:
        bound = method.needs_node
        native = _native_alternative(target, machine)
        instead = f" Use {native} instead, or" if native else " You can"
        if node.state == "missing":
            if machine.headless and NODE_INSTALLERS[machine.os].elevates is (
                Elevation.OS_PROMPT
            ):
                _refuse(
                    kwargs,
                    "Node.js is missing, and its installer needs a permission "
                    f"prompt that cannot appear: {machine.headless_reason}.",
                )
            else:
                node_steps, node_text, node_elev, node_changes = _node_install_plan(
                    machine, method
                )
                steps[:0] = node_steps
                elevation[:0] = node_elev
                changes[:0] = node_changes
                node_install = True
        elif bound is not None and not node.satisfies(bound):
            _refuse(
                kwargs,
                f"Node.js {node.node_version or '(unknown version)'} at "
                f"{node.node_path} is older than this package needs "
                f"({bound.label}). MCC never replaces your Node.js.{instead} "
                "upgrade Node.js yourself.",
            )
        elif node.state == "unwritable":
            _refuse(
                kwargs,
                f"npm's global folder {node.prefix} is not writable by you, so "
                "`npm install -g` would need sudo, which MCC never runs."
                f"{instead} fix npm's folder yourself "
                "(https://docs.npmjs.com/resolving-eacces-permissions-errors-"
                "when-installing-packages-globally) and run the command.",
            )
        else:
            where = "" if node.on_server_path else " (not on this server's PATH yet)"
            node_text = (
                f"Uses your Node.js {node.node_version} at {node.node_path}{where}; "
                "nothing is installed besides the package."
            )

    verify_names = _verify_names(target, method)
    verify_dirs = tuple(method.verify_dirs)
    if kwargs["runnable"] or steps:
        steps.append(
            PlanStep(
                kind="verify",
                text=(
                    "Look for "
                    + (
                        " / ".join(verify_names)
                        if verify_names
                        else target.display_name
                    )
                    + " with the refreshed PATH, then run it with --version (5 s)"
                    if target.kind == "cli"
                    else f"Check that MCC can see {target.display_name} afterwards"
                ),
                timeout_seconds=int(PROBE_TIMEOUT_SECONDS),
            )
        )
    if not kwargs["runnable"]:
        steps = []
    return Plan(
        target=target,
        method=method,
        machine=machine,
        node=node,
        runnable=kwargs["runnable"],
        refused=kwargs["refused"],
        steps=tuple(steps),
        elevation=tuple(elevation),
        changes=tuple(changes),
        node_text=node_text,
        node_install=node_install,
        verify_names=verify_names,
        verify_dirs=verify_dirs,
    )


def _verify_names(target: InstallTarget, method: InstallMethod) -> tuple[str, ...]:
    names = list(target.binaries)
    for name in method.bins:
        if name not in names:
            names.append(name)
    return tuple(names)


def _plan_method_steps(
    method: InstallMethod,
    machine: Machine,
    node: NodeStatus,
    environ: Mapping[str, str],
    steps: list[PlanStep],
    kwargs: dict[str, Any],
) -> None:
    timeout = method.timeout_seconds
    if method.kind is InstallKind.SCRIPT:
        interpreter = method.interpreter
        if interpreter is None:
            _refuse(kwargs, "This method names no interpreter.")
            return
        program = _interpreter_path(interpreter, machine, environ)
        if not program:
            _refuse(kwargs, f"{interpreter.value} is not available on this server.")
            return
        steps.append(
            PlanStep(
                kind="download",
                text=f"Download the vendor's script to a file (https, {method.url})",
                url=method.url,
                hosts=method.hosts,
                timeout_seconds=int(DOWNLOAD_READ_TIMEOUT_SECONDS),
            )
        )
        if interpreter is Interpreter.POWERSHELL:
            command: tuple[str, ...] = (
                program,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                DOWNLOADED,
                *method.script_args,
            )
            text = (
                "Run the script by path with Windows PowerShell "
                "(-ExecutionPolicy Bypass for this one script, as the vendor's "
                "own line does)"
            )
        else:
            command = (program, DOWNLOADED, *method.script_args)
            text = f"Run the script by path with {interpreter.value}"
        steps.append(
            PlanStep(
                kind="run",
                text=text,
                command=command,
                timeout_seconds=timeout,
                env=method.env,
            )
        )
        return
    if method.kind is InstallKind.NPM:
        npm = node.npm_path or ("npm.cmd" if machine.os is InstallOs.WINDOWS else "npm")
        steps.append(
            PlanStep(
                kind="run",
                text=f"Install {method.package} with npm",
                command=(npm, "install", "-g", *method.npm_args, method.package),
                timeout_seconds=timeout,
            )
        )
        return
    if method.kind is InstallKind.WINGET:
        winget = _program_available(("winget",), environ)
        if not winget:
            _refuse(kwargs, "winget is not available on this server.")
            return
        steps.append(
            PlanStep(
                kind="run",
                text=f"Install {method.package} with winget",
                command=(
                    winget,
                    "install",
                    "--id",
                    method.package,
                    "--exact",
                    "--source",
                    "winget",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ),
                timeout_seconds=timeout,
            )
        )
        return
    if method.kind is InstallKind.BREW:
        brew = _program_available(("brew",), environ) or next(
            (
                path
                for path in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                if os.path.isfile(path)
            ),
            "",
        )
        if not brew:
            _refuse(kwargs, "Homebrew is not installed on this Mac.")
            return
        cask = ("--cask",) if method.brew_cask else ()
        steps.append(
            PlanStep(
                kind="run",
                text=f"Install {method.package} with Homebrew",
                command=(brew, "install", *cask, method.package),
                timeout_seconds=timeout,
            )
        )
        return
    if method.kind is InstallKind.VSCODE_EXTENSION:
        code = _program_available(("code",), environ)
        if not code:
            _refuse(
                kwargs,
                "VS Code's `code` command is not on PATH: install VS Code first "
                "(https://code.visualstudio.com/download).",
            )
            return
        steps.append(
            PlanStep(
                kind="run",
                text=f"Add the {method.package} extension with VS Code's own command",
                command=(code, "--install-extension", method.package),
                timeout_seconds=timeout,
            )
        )
        return
    if method.kind is InstallKind.DOWNLOAD:
        _plan_download_steps(method, machine, environ, steps, kwargs)
        return
    _refuse(kwargs, f"Unsupported install kind {method.kind.value}.")


def _plan_download_steps(
    method: InstallMethod,
    machine: Machine,
    environ: Mapping[str, str],
    steps: list[PlanStep],
    kwargs: dict[str, Any],
) -> None:
    action = method.action
    steps.append(
        PlanStep(
            kind="download",
            text=f"Download the vendor's file to a fresh temp folder ({method.url})",
            url=method.url,
            hosts=method.hosts,
            timeout_seconds=int(DOWNLOAD_READ_TIMEOUT_SECONDS),
        )
    )
    if action is DownloadAction.RUN:
        steps.append(
            PlanStep(
                kind="run",
                text="Open the installer by path (its own window)",
                command=(DOWNLOADED,),
                timeout_seconds=method.timeout_seconds,
            )
        )
        return
    if action is DownloadAction.ZIP_TO_PROGRAMS:
        target = programs_dir(method.app_name, environ)
        if os.path.exists(target) and os.listdir(target):
            _refuse(kwargs, f"{target} already exists; MCC never unpacks over it.")
            return
        steps.append(
            PlanStep(
                kind="unpack",
                text=f"Unpack the .zip into {target} (per user)",
                command=("unzip", DOWNLOADED, target),
            )
        )
        return
    if action in {DownloadAction.DMG_COPY, DownloadAction.ZIP_TO_APPLICATIONS}:
        target = _expand(f"~/Applications/{method.app_name}", environ)
        if os.path.exists(target):
            _refuse(kwargs, f"{target} already exists; MCC never copies over it.")
            return
        if action is DownloadAction.DMG_COPY:
            steps.append(
                PlanStep(
                    kind="unpack",
                    text=(
                        f"Mount the disk image read-only, copy {method.app_name} "
                        "into ~/Applications, detach it"
                    ),
                    command=("/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly"),
                )
            )
        else:
            steps.append(
                PlanStep(
                    kind="unpack",
                    text=f"Unpack {method.app_name} into ~/Applications",
                    command=("/usr/bin/ditto", "-x", "-k", DOWNLOADED, target),
                )
            )
        return
    _refuse(kwargs, "This download is shown, not run.")


# ------------------------------------------------------------------ listing


def target_payload(
    target: InstallTarget,
    machine: Machine,
    node: NodeStatus,
    environ: Mapping[str, str],
    state: TargetState | None = None,
) -> dict[str, Any]:
    """Return one card's install block: state, methods for this OS, the pick."""

    if state is None:
        state = target_state(target, environ, machine)
    methods = methods_for_os(target.methods, machine.os, machine.arch)
    hidden = target.kind == "desktop" and machine.headless
    rows: list[dict[str, Any]] = []
    recommended = ""
    for method in methods:
        plan = build_plan(target, method.id, machine, node, environ, state=state)
        row = method_payload(method)
        row["runnable"] = plan.runnable
        row["refused"] = plan.refused
        row["node_install"] = plan.node_install
        if plan.runnable and not recommended:
            recommended = method.id
        rows.append(row)
    links = [row for row in rows if row["kind"] == InstallKind.LINK.value]
    return {
        "id": target.id,
        "kind": target.kind,
        "display_name": target.display_name,
        "installed": state.installed,
        "path": state.path,
        "version": state.version,
        "on_server_path": state.on_server_path,
        "offer_install": bool(recommended) and not state.installed and not hidden,
        "recommended": recommended,
        "methods": rows,
        "link": links[0]["url"] if links and not recommended else "",
        "hidden_reason": (
            f"Desktop apps are not installed from here: {machine.headless_reason}."
            if hidden
            else ""
        ),
        "unavailable_reason": target.unavailable_reason,
        "same_as": target.same_as,
    }


def listing(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return ``GET /admin/api/agent-installs``: every card, Node, the job."""

    env = os.environ if environ is None else environ
    machine = current_machine(env)
    targets = install_targets()
    # ``--version`` of every installed CLI, side by side: each is bounded at
    # 5 s and cached by the file's identity, so a second load spawns nothing.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-state") as pool:
        node_future = pool.submit(detect_node, machine, env)
        futures = [pool.submit(target_state, t, env, machine) for t in targets]
        states = [future.result() for future in futures]
        node = node_future.result()
    return {
        "os": machine.os.value,
        "arch": machine.arch,
        "wsl": machine.wsl,
        "headless": machine.headless,
        "headless_reason": machine.headless_reason,
        "node": node.payload(),
        "job": current_job_summary(),
        "agents": [
            target_payload(t, machine, node, env, state)
            for t, state in zip(targets, states, strict=True)
        ],
    }


def plan_for(
    target_id: str, method_id: str, environ: Mapping[str, str] | None = None
) -> Plan:
    """Return the plan for one card and method, re-detected now."""

    env = os.environ if environ is None else environ
    target = install_target(target_id)
    machine = current_machine(env)
    node = detect_node(machine, env, cached=False)
    return build_plan(target, method_id, machine, node, env)


# ------------------------------------------------------------------- downloads


def check_url(url: str, hosts: Sequence[str]) -> None:
    """Refuse anything but https to a host on this method's allow-list."""

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise StepFailedError(
            f"Refused {url}: MCC downloads only over https.",
            classification="network",
        )
    host = (parts.hostname or "").lower()
    if host not in {h.lower() for h in hosts}:
        raise StepFailedError(
            f"Refused {url}: {host or '(no host)'} is not on this method's "
            f"allow-list ({', '.join(hosts)}).",
            classification="network",
        )


def _http_client() -> httpx.Client:
    """The download client: certificates verified (httpx's default), no
    automatic redirects -- each hop is checked against the allow-list."""

    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(
            DOWNLOAD_READ_TIMEOUT_SECONDS, connect=DOWNLOAD_CONNECT_TIMEOUT_SECONDS
        ),
    )


@dataclass(frozen=True, slots=True)
class Downloaded:
    path: Path
    sha256: str
    size: int
    final_url: str


def download(
    url: str,
    hosts: Sequence[str],
    destination: Path,
    *,
    cancel: threading.Event,
) -> Downloaded:
    """Download ``url`` to ``destination``, checking every redirect hop."""

    current = url
    try:
        with _http_client() as client:
            for _hop in range(MAX_REDIRECTS + 1):
                check_url(current, hosts)
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location:
                            raise StepFailedError(
                                f"{current} redirected without a Location.",
                                classification="network",
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise StepFailedError(
                            f"{current} answered HTTP {response.status_code}.",
                            classification="network",
                        )
                    digest = hashlib.sha256()
                    size = 0
                    with destination.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            if cancel.is_set():
                                raise StepFailedError(
                                    "Cancelled during the download.",
                                    classification="cancelled",
                                )
                            handle.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    return Downloaded(destination, digest.hexdigest(), size, current)
    except httpx.HTTPError as exc:
        raise StepFailedError(
            f"Could not download {current}: {exc}", classification="network"
        ) from exc
    except OSError as exc:
        raise StepFailedError(
            f"Could not write the download to {destination}: {exc}",
            classification=classify_text(str(exc)) or "disk",
        ) from exc
    raise StepFailedError(
        f"{url} redirected more than {MAX_REDIRECTS} times.", classification="network"
    )


# --------------------------------------------------------------- classification

CLASSIFICATION_LABELS = {
    "network": "Network: a download or the vendor's server failed",
    "permission": "Permission: the system refused a write (EACCES / EPERM / access denied)",
    "disk": "Disk: not enough space (ENOSPC)",
    "antivirus": (
        "Antivirus: the downloaded file vanished before it ran, or Windows "
        "reported a virus (exit 225)"
    ),
    "node_too_old": "Node.js too old for this package (EBADENGINE)",
    "cancelled": "Cancelled",
    "timeout": "Took longer than its bound and was stopped",
    "declined": "The permission prompt was declined or the installer was closed",
    "not_found": "Finished, but the program is not where MCC can see it",
    "checksum": "The installer did not match its published SHA-256",
    "other": "The installer failed (see its last lines)",
}

_TEXT_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "node_too_old",
        re.compile(r"EBADENGINE|Unsupported engine|requires Node\.?js", re.I),
    ),
    ("disk", re.compile(r"ENOSPC|No space left|not enough space|disk is full", re.I)),
    (
        "permission",
        re.compile(
            r"EACCES|EPERM|Access is denied|Permission denied|Operation not permitted",
            re.I,
        ),
    ),
    ("antivirus", re.compile(r"virus|potentially unwanted|quarantin", re.I)),
    (
        "network",
        re.compile(
            r"ENOTFOUND|ECONNRESET|ETIMEDOUT|ECONNREFUSED|EAI_AGAIN|getaddrinfo|"
            r"Could not resolve host|unable to connect|network|TLS|certificate|"
            r"Invoke-WebRequest|Invoke-RestMethod|curl: \(\d+\)",
            re.I,
        ),
    ),
)


def classify_text(text: str) -> str:
    """Return the class the output names, or "" when it names none."""

    for name, pattern in _TEXT_CLASSES:
        if pattern.search(text):
            return name
    return ""


def classify_failure(
    exit_code: int | None, lines: Sequence[str], *, os_: InstallOs
) -> str:
    """Classify a failed step from its exit code and its last lines (spec N5)."""

    if os_ is InstallOs.WINDOWS and exit_code == 225:
        return "antivirus"
    if exit_code == 1602:
        return "declined"
    return classify_text("\n".join(lines[-200:])) or "other"


# ------------------------------------------------------------------------ jobs


@dataclass
class InstallJob:
    """One Install run: its plan, its output, its outcome."""

    id: str
    plan: Plan
    dry_run: bool
    log_path: Path
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stage: str = "running"
    exit_code: int | None = None
    result: dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=JOB_LINE_LIMIT))
    line_count: int = 0
    current_pid: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, line: str) -> None:
        text = line.rstrip("\r\n")
        with self.lock:
            self.lines.append(text)
            self.line_count += 1
        with (
            contextlib.suppress(OSError),
            self.log_path.open("a", encoding="utf-8") as handle,
        ):
            handle.write(text + "\n")

    def lines_since(self, cursor: int) -> tuple[list[str], int]:
        with self.lock:
            first = self.line_count - len(self.lines)
            start = max(cursor, first)
            items = list(self.lines)[start - first :]
            return items, self.line_count

    def tail(self, count: int) -> list[str]:
        with self.lock:
            return list(self.lines)[-count:]

    def payload(self, cursor: int = 0) -> dict[str, Any]:
        lines, total = self.lines_since(cursor)
        return {
            "job_id": self.id,
            "target": self.plan.target.id,
            "display_name": self.plan.target.display_name,
            "method": self.plan.method.id,
            "dry_run": self.dry_run,
            "stage": self.stage,
            "running": self.stage == "running",
            "lines": lines,
            "cursor": total,
            "exit_code": self.exit_code,
            "log_path": str(self.log_path),
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_JOBS: dict[str, InstallJob] = {}
_ACTIVE: list[InstallJob] = []
_JOBS_LOCK = threading.Lock()


def current_job_summary() -> dict[str, Any] | None:
    with _JOBS_LOCK:
        active = _ACTIVE[0] if _ACTIVE else None
    if active is None:
        return None
    return {
        "job_id": active.id,
        "target": active.plan.target.id,
        "display_name": active.plan.target.display_name,
        "stage": active.stage,
    }


def job(job_id: str) -> InstallJob:
    with _JOBS_LOCK:
        found = _JOBS.get(job_id)
    if found is None:
        raise InstallError(f"Unknown install job: {job_id}", status=404)
    return found


def _acquire_lock_file(job_id: str) -> None:
    path = agent_install_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"pid": os.getpid(), "job": job_id})
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
                pid = int(held.get("pid", 0))
            except OSError, ValueError, TypeError, AttributeError:
                pid = 0
            if pid and pid != os.getpid() and process_is_alive(pid):
                raise InstallBusyError(
                    f"Another MCC process (pid {pid}) is installing an agent. "
                    "One install runs at a time."
                ) from None
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(record)
        return
    raise InstallBusyError("Could not take the install lock. Try again.")


def _release_lock_file(job_id: str) -> None:
    path = agent_install_lock_path()
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
        held = json.loads(path.read_text(encoding="utf-8"))
        if held.get("job") == job_id:
            path.unlink()


def _log_path(target_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    directory = agent_install_logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{target_id}-{stamp}-{uuid.uuid4().hex[:6]}.log"


def start_job(
    plan: Plan,
    *,
    dry_run: bool,
    environ: Mapping[str, str] | None = None,
    wait: bool = False,
) -> InstallJob:
    """Start one run of a confirmed plan. Raises ``InstallBusyError`` (409)."""

    if not plan.runnable:
        raise InstallError(plan.refused or "This plan cannot run.", status=409)
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        if _ACTIVE:
            running = _ACTIVE[0]
            raise InstallBusyError(
                f"{running.plan.target.display_name} is being installed. One "
                "install runs at a time."
            )
        _acquire_lock_file(job_id)
        new = InstallJob(
            id=job_id, plan=plan, dry_run=dry_run, log_path=_log_path(plan.target.id)
        )
        _ACTIVE.append(new)
        _JOBS[job_id] = new
        _forget_old_jobs()
    env = dict(os.environ if environ is None else environ)
    worker = threading.Thread(
        target=_run_job,
        args=(new, env),
        name=f"agent-install-{job_id[:8]}",
        daemon=True,
    )
    worker.start()
    if wait:
        worker.join()
    return new


def _forget_old_jobs() -> None:
    now = time.time()
    for job_id, old in list(_JOBS.items()):
        if (
            old.finished_at is not None
            and now - old.finished_at > FINISHED_JOB_TTL_SECONDS
        ):
            del _JOBS[job_id]


def cancel_job(job_id: str) -> InstallJob:
    """Stop a run: its own child process tree, by exact PID, nothing else."""

    found = job(job_id)
    found.cancel_event.set()
    pid = found.current_pid
    if pid is not None:
        stop_process_tree(pid)
    return found


def stop_process_tree(pid: int) -> None:
    """Stop ``pid`` and the processes it started -- the run's own child only.

    Windows: ``taskkill /PID <pid> /T /F`` walks children by parent id from
    that exact PID. POSIX: the child was started in its own session, so its
    process group is exactly its tree.
    """

    if pid == os.getpid():
        return
    if _WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [_system32("taskkill.exe", os.environ), "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=_creationflags(),
            )
        return
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return
    with contextlib.suppress(OSError):
        killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            killpg(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        killpg(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def _finish(job_: InstallJob, stage: str, result: dict[str, Any]) -> None:
    job_.stage = stage
    job_.result = result
    job_.finished_at = time.time()
    job_.emit(f"[{stage}] {result.get('message', '')}")
    with _JOBS_LOCK:
        if _ACTIVE and _ACTIVE[0] is job_:
            _ACTIVE.clear()
    _release_lock_file(job_.id)
    clear_probe_caches()


def _command_text(argv: Sequence[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in argv)


def _run_job(job_: InstallJob, environ: dict[str, str]) -> None:
    plan = job_.plan
    method = plan.method
    job_.emit(
        f"MCC {datetime.now(UTC).isoformat(timespec='seconds')} install "
        f"{plan.target.display_name} with {method.label} ({method.os.value}"
        f"{', dry run' if job_.dry_run else ''})"
    )
    job_.emit(f"plan {plan.token}")
    if job_.dry_run:
        for step in plan.steps:
            what = step.url or _command_text(step.command)
            job_.emit(
                f"would {step.kind}: {step.text}" + (f" -- {what}" if what else "")
            )
        _finish(
            job_,
            "done",
            {
                "status": "dry_run",
                "message": "Dry run: nothing was downloaded, run or changed.",
            },
        )
        return
    try:
        with tempfile.TemporaryDirectory(prefix="mcc-agent-install-") as scratch:
            runner = _Runner(job_, Path(scratch), environ)
            runner.run()
            result = runner.verify()
    except StepFailedError as exc:
        cancelled = job_.cancel_event.is_set() or exc.classification == "cancelled"
        classification = "cancelled" if cancelled else exc.classification
        _finish(
            job_,
            "cancelled" if cancelled else "failed",
            _failure_result(job_, str(exc), classification, exc.exit_code, exc.command),
        )
        return
    except Exception as exc:  # the run's thread must always finish its job
        _finish(
            job_,
            "failed",
            _failure_result(job_, f"Unexpected error: {exc}", "other", None, ""),
        )
        return
    _finish(job_, "failed" if result.get("status") == "failed" else "done", result)


def _failure_result(
    job_: InstallJob,
    message: str,
    classification: str,
    exit_code: int | None,
    command: str,
) -> dict[str, Any]:
    method = job_.plan.method
    job_.exit_code = exit_code
    return {
        "status": "cancelled" if classification == "cancelled" else "failed",
        "message": message,
        "classification": classification,
        "classification_label": CLASSIFICATION_LABELS.get(
            classification, classification
        ),
        "exit_code": exit_code,
        "last_lines": job_.tail(FAILURE_TAIL_LINES),
        "log_path": str(job_.log_path),
        "command": command,
        "copy_command": method.printed_command or method.copy_command,
        "doc_url": method.doc_url,
    }


class _Runner:
    """Executes one plan's steps for one job."""

    def __init__(
        self, job_: InstallJob, scratch: Path, environ: dict[str, str]
    ) -> None:
        self.job = job_
        self.plan = job_.plan
        self.scratch = scratch
        self.environ = environ
        self.downloaded: Path | None = None
        self.node = job_.plan.node

    # -- helpers ----------------------------------------------------------

    def _cancelled(self) -> None:
        if self.job.cancel_event.is_set():
            raise StepFailedError("Cancelled.", classification="cancelled")

    def _download(self, url: str, hosts: Sequence[str], name: str) -> Downloaded:
        self._cancelled()
        destination = self.scratch / name
        self.job.emit(f"download {url}")
        result = download(url, hosts, destination, cancel=self.job.cancel_event)
        self.job.emit(
            f"downloaded {result.size} bytes from {result.final_url} to "
            f"{result.path} (sha256 {result.sha256})"
        )
        return result

    def _spawn(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str],
        cwd: Path | None = None,
    ) -> int:
        """Run one command, streaming its output, bounded and cancellable."""

        self._cancelled()
        words = {part.lower() for part in argv} | {
            os.path.splitext(os.path.basename(part))[0].lower() for part in argv
        }
        for forbidden in FORBIDDEN_ARGV_WORDS:
            if forbidden in words:
                raise StepFailedError(
                    f"Refused to run a command naming {forbidden}.",
                    classification="permission",
                )
        command = _command_text(argv)
        self.job.emit(f"$ {command}")
        kwargs: dict[str, Any] = {}
        if _WINDOWS:
            kwargs["creationflags"] = _creationflags() | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        try:
            child = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(env),
                cwd=str(cwd or self.scratch),
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise StepFailedError(
                f"Could not start {argv[0]}: {exc}",
                classification="antivirus" if self.downloaded else "other",
                command=command,
            ) from exc
        except OSError as exc:
            raise StepFailedError(
                f"Could not start {argv[0]}: {exc}",
                classification=classify_text(str(exc)) or "other",
                command=command,
            ) from exc
        self.job.current_pid = child.pid
        self.job.emit(f"(pid {child.pid})")
        reader = threading.Thread(target=self._pump, args=(child,), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while child.poll() is None:
                if self.job.cancel_event.is_set():
                    stop_process_tree(child.pid)
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    stop_process_tree(child.pid)
                    break
                time.sleep(0.1)
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=10)
        finally:
            reader.join(timeout=5)
            self.job.current_pid = None
        code = child.returncode
        self.job.exit_code = code
        if self.job.cancel_event.is_set():
            raise StepFailedError(
                "Cancelled: MCC stopped the installer it started (pid "
                f"{child.pid} and its children).",
                classification="cancelled",
                exit_code=code,
                command=command,
            )
        if timed_out:
            raise StepFailedError(
                f"Stopped after {int(timeout)} s, its bound.",
                classification="timeout",
                exit_code=code,
                command=command,
            )
        self.job.emit(f"exit {code}")
        return code if code is not None else -1

    def _pump(self, child: subprocess.Popen[bytes]) -> None:
        stream = child.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            for piece in raw.decode("utf-8", "replace").split("\r"):
                if piece.strip():
                    self.job.emit(piece)
        with contextlib.suppress(OSError):
            stream.close()

    def _require_exit(
        self, code: int, argv: Sequence[str], ok: Sequence[int] = (0,)
    ) -> None:
        if code in ok:
            return
        tail = self.job.tail(200)
        raise StepFailedError(
            f"{os.path.basename(argv[0])} exited {code}.",
            classification=classify_failure(code, tail, os_=self.plan.machine.os),
            exit_code=code,
            command=_command_text(argv),
        )

    def _resolve(self, command: Sequence[str]) -> list[str]:
        resolved: list[str] = []
        for part in command:
            if part == DOWNLOADED:
                if self.downloaded is None or not self.downloaded.exists():
                    raise StepFailedError(
                        "The downloaded file vanished before it could run "
                        "(often an antivirus quarantine).",
                        classification="antivirus",
                    )
                resolved.append(str(self.downloaded))
            else:
                resolved.append(part)
        return resolved

    # -- the run ------------------------------------------------------------

    def run(self) -> None:
        if self.plan.node_install:
            self._install_node()
        method = self.plan.method
        for step in self.plan.steps:
            if step.kind in {"node", "verify"}:
                continue
            if step.kind == "download":
                name = _download_name(method, step.url)
                self.downloaded = self._download(step.url, step.hosts, name).path
                continue
            if step.kind == "unpack":
                self._unpack(method)
                continue
            env = dict(self.environ)
            env.update(dict(step.env))
            argv = self._resolve(step.command)
            if method.kind is InstallKind.NPM:
                argv[0] = self.node.npm_path or argv[0]
                if self.node.node_path:
                    env = _env_with_dir_first(env, os.path.dirname(self.node.node_path))
            ok: tuple[int, ...] = (0,)
            if (
                method.kind is InstallKind.DOWNLOAD
                and method.action is DownloadAction.RUN
            ):
                ok = (0, 3010)
            code = self._spawn(argv, timeout=step.timeout_seconds or 900, env=env)
            self._require_exit(code, argv, ok)

    def _install_node(self) -> None:
        machine = self.plan.machine
        installer = NODE_INSTALLERS[machine.os]
        self.job.emit("Node.js is missing: installing it first.")
        if installer.kind in {"msi", "pkg"}:
            self._install_node_package(installer)
        else:
            self._install_node_fnm(installer)
        status = _detect_node(machine, self.environ)
        if status.state == "missing" or not status.npm_path:
            raise StepFailedError(
                "The Node.js installer finished, but MCC cannot find node and "
                f"npm in {', '.join(node_known_dirs(machine, self.environ))} or "
                "on PATH. Open a new terminal and run `node --version`.",
                classification="not_found",
            )
        self.job.emit(
            f"Node.js {status.node_version} at {status.node_path}; npm at {status.npm_path}"
        )
        self.node = status

    def _install_node_package(self, installer: NodeInstaller) -> None:
        machine = self.plan.machine
        index = self._download(installer.url, installer.hosts, "node-index.json")
        try:
            releases = json.loads(index.path.read_text(encoding="utf-8"))
            version = next(
                str(entry["version"])
                for entry in releases
                if isinstance(entry, dict) and entry.get("lts")
            )
        except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise StepFailedError(
                f"Could not read the Node.js release list: {exc}",
                classification="network",
            ) from exc
        if installer.kind == "msi":
            name = f"node-{version}-{machine.arch}.msi"
        else:
            name = f"node-{version}.pkg"
        base = f"https://nodejs.org/dist/{version}/"
        package = self._download(base + name, installer.hosts, name)
        sums = self._download(
            base + "SHASUMS256.txt", installer.hosts, "SHASUMS256.txt"
        )
        expected = ""
        for line in sums.path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == name:
                expected = parts[0].lower()
        if not expected or expected != package.sha256:
            raise StepFailedError(
                f"{name} has sha256 {package.sha256}; SHASUMS256.txt says "
                f"{expected or '(not listed)'}. Not run.",
                classification="checksum",
            )
        self.job.emit(f"{name} matches SHASUMS256.txt ({expected}).")
        self.downloaded = package.path
        if installer.kind == "msi":
            argv = [
                _system32("msiexec.exe", self.environ),
                "/i",
                str(package.path),
                "/passive",
                "/norestart",
            ]
            self.job.emit("Windows will ask for administrator permission now.")
            ok: tuple[int, ...] = (0, 3010)
        else:
            argv = ["/usr/bin/open", "-W", str(package.path)]
            self.job.emit("macOS Installer will ask for your password now.")
            ok = (0,)
        code = self._spawn(argv, timeout=installer.timeout_seconds, env=self.environ)
        self._require_exit(code, argv, ok)

    def _install_node_fnm(self, installer: NodeInstaller) -> None:
        script = self._download(installer.url, installer.hosts, "fnm-install.sh")
        bash = _program_available(("bash",), self.environ)
        if not bash:
            raise StepFailedError("bash is not available.", classification="other")
        argv = [bash, str(script.path)]
        code = self._spawn(argv, timeout=installer.timeout_seconds, env=self.environ)
        self._require_exit(code, argv)
        base = fnm_dir(self.environ)
        fnm = _which(("fnm",), base) or _which(("fnm",), _server_path(self.environ))
        if not fnm:
            raise StepFailedError(
                f"fnm's script finished, but fnm is not in {base}.",
                classification="not_found",
            )
        had_default = os.path.exists(os.path.join(base, "aliases", "default"))
        argv = [fnm, "install", "--lts"]
        code = self._spawn(argv, timeout=installer.timeout_seconds, env=self.environ)
        self._require_exit(code, argv)
        if not had_default:
            argv = [fnm, "default", "lts-latest"]
            code = self._spawn(argv, timeout=60, env=self.environ)
            self._require_exit(code, argv)

    def _unpack(self, method: InstallMethod) -> None:
        self._cancelled()
        archive = self.downloaded
        if archive is None or not archive.exists():
            raise StepFailedError(
                "The downloaded file vanished before it could be unpacked "
                "(often an antivirus quarantine).",
                classification="antivirus",
            )
        if method.action is DownloadAction.ZIP_TO_PROGRAMS:
            target = Path(programs_dir(method.app_name, self.environ))
            self.job.emit(f"unpack {archive} -> {target}")
            _extract_zip(archive, target)
            self.job.emit(f"unpacked into {target}")
            return
        if method.action is DownloadAction.ZIP_TO_APPLICATIONS:
            applications = Path(_expand("~/Applications", self.environ))
            applications.mkdir(parents=True, exist_ok=True)
            argv = ["/usr/bin/ditto", "-x", "-k", str(archive), str(applications)]
            code = self._spawn(argv, timeout=600, env=self.environ)
            self._require_exit(code, argv)
            return
        if method.action is DownloadAction.DMG_COPY:
            self._copy_from_dmg(archive, method.app_name)
            return

    def _copy_from_dmg(self, image: Path, app_name: str) -> None:
        mount = self.scratch / "mount"
        mount.mkdir()
        attach = [
            "/usr/bin/hdiutil",
            "attach",
            "-nobrowse",
            "-readonly",
            "-mountpoint",
            str(mount),
            str(image),
        ]
        code = self._spawn(attach, timeout=300, env=self.environ)
        self._require_exit(code, attach)
        try:
            source = mount / app_name
            if not source.exists():
                found = sorted(mount.glob("*.app"))
                if not found:
                    raise StepFailedError(
                        f"No .app inside {image.name}.", classification="other"
                    )
                source = found[0]
            applications = Path(_expand("~/Applications", self.environ))
            applications.mkdir(parents=True, exist_ok=True)
            copy = ["/usr/bin/ditto", str(source), str(applications / source.name)]
            code = self._spawn(copy, timeout=600, env=self.environ)
            self._require_exit(code, copy)
        finally:
            detach = ["/usr/bin/hdiutil", "detach", str(mount)]
            with contextlib.suppress(StepFailedError):
                self._spawn(detach, timeout=120, env=self.environ)

    # -- afterwards ---------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        plan = self.plan
        target = plan.target
        if target.kind == "desktop":
            return self._verify_desktop()
        extra = list(plan.verify_dirs)
        if plan.method.kind is InstallKind.NPM and self.node.prefix:
            prefix = self.node.prefix
            extra.append(prefix if _WINDOWS else os.path.join(prefix, "bin"))
        elif plan.method.kind is InstallKind.NPM and self.node.npm_path:
            extra.append(os.path.dirname(self.node.npm_path))
        found = find_program(plan.verify_names, self.environ, extra)
        if found is None:
            names = " / ".join(plan.verify_names)
            return {
                "status": "not_on_path",
                "message": (
                    f"The installer finished (exit 0), but {names} is not on this "
                    "server's PATH or in "
                    + (
                        ", ".join(_expand(d, self.environ) for d in extra)
                        or "its folders"
                    )
                    + " yet. Open a new terminal and run "
                    f"`{plan.verify_names[0]} --version`."
                ),
                "classification": "not_found",
                "classification_label": CLASSIFICATION_LABELS["not_found"],
                "log_path": str(self.job.log_path),
                "copy_command": plan.method.copy_command,
                "doc_url": plan.method.doc_url,
            }
        try:
            code, text = probe_output([found.path, "--version"], self.environ)
        except (OSError, subprocess.SubprocessError) as exc:
            code, text = -1, str(exc)
        version = _first_line(text)
        name = os.path.splitext(os.path.basename(found.path))[0]
        self.job.emit(f"$ {found.path} --version -> exit {code}: {version}")
        base = {
            "path": found.path,
            "version": version,
            "on_server_path": found.on_server_path,
            "log_path": str(self.job.log_path),
        }
        if code != 0:
            return {
                **base,
                "status": "failed",
                "message": (
                    f"Installed at {found.path}, but `{name} --version` failed "
                    f"(exit {code}): {version or 'no output'}."
                ),
                "classification": classify_text(text) or "other",
                "classification_label": CLASSIFICATION_LABELS.get(
                    classify_text(text) or "other", "other"
                ),
                "exit_code": code,
                "last_lines": self.job.tail(FAILURE_TAIL_LINES),
                "command": f"{found.path} --version",
                "copy_command": plan.method.copy_command,
                "doc_url": plan.method.doc_url,
            }
        if found.on_server_path:
            return {
                **base,
                "status": "verified",
                "message": f"Installed and verified: {name} {version} at {found.path}.",
            }
        return {
            **base,
            "status": "not_on_path",
            "message": (
                f"Installed and verified: {name} {version} at {found.path} -- not on "
                "PATH in this server yet: open a new terminal."
            ),
        }

    def _verify_desktop(self) -> dict[str, Any]:
        spec = self.plan.target.desktop
        name = self.plan.target.display_name
        method = self.plan.method
        if method.action is DownloadAction.ZIP_TO_PROGRAMS:
            where = programs_dir(method.app_name, self.environ)
        elif method.action in {
            DownloadAction.DMG_COPY,
            DownloadAction.ZIP_TO_APPLICATIONS,
        }:
            where = _expand(f"~/Applications/{method.app_name}", self.environ)
        else:
            where = ""
        if spec is not None and desktop_is_installed(spec, self.environ):
            suffix = f" at {where}" if where else ""
            return {
                "status": "verified",
                "message": f"Installed: MCC can see {name}{suffix}.",
                "path": where,
                "log_path": str(self.job.log_path),
            }
        return {
            "status": "not_on_path",
            "message": (
                f"The installer finished, but MCC cannot see {name} yet. Open it "
                "once (Start menu or Applications), then reload this page."
            ),
            "classification": "not_found",
            "classification_label": CLASSIFICATION_LABELS["not_found"],
            "path": where,
            "log_path": str(self.job.log_path),
            "doc_url": method.doc_url,
        }


def _download_name(method: InstallMethod, url: str) -> str:
    """Return the temp file name, with the extension its interpreter expects."""

    if method.kind is InstallKind.SCRIPT:
        if method.interpreter is Interpreter.POWERSHELL:
            return "install.ps1"
        return "install.sh"
    if method.action is DownloadAction.RUN:
        return "setup.exe"
    if method.action is DownloadAction.DMG_COPY:
        return "installer.dmg"
    if method.action in {
        DownloadAction.ZIP_TO_PROGRAMS,
        DownloadAction.ZIP_TO_APPLICATIONS,
    }:
        return "archive.zip"
    name = PurePosixPath(urlsplit(url).path).name
    return name or "download"


def _extract_zip(archive: Path, target: Path) -> None:
    """Extract a ``.zip`` into ``target``; flatten one top folder; no escapes."""

    try:
        with zipfile.ZipFile(archive) as bundle:
            members = [m for m in bundle.infolist() if m.filename and not m.is_dir()]
            tops = {PurePosixPath(m.filename).parts[0] for m in members}
            strip = len(tops) == 1 and all(
                len(PurePosixPath(m.filename).parts) > 1 for m in members
            )
            target_resolved = target.resolve()
            for member in members:
                parts = PurePosixPath(member.filename).parts
                if strip:
                    parts = parts[1:]
                if not parts or any(part in {"", ".", ".."} for part in parts):
                    continue
                destination = target.joinpath(*parts)
                if not destination.resolve().is_relative_to(target_resolved):
                    raise StepFailedError(
                        f"{archive.name} tried to write outside {target}.",
                        classification="other",
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
    except zipfile.BadZipFile as exc:
        raise StepFailedError(
            f"{archive.name} is not a valid .zip: {exc}", classification="other"
        ) from exc
    except OSError as exc:
        raise StepFailedError(
            f"Could not unpack into {target}: {exc}",
            classification=classify_text(str(exc)) or "disk",
        ) from exc


def reset_for_tests() -> None:
    """Forget every job and cache (an autouse fixture calls this)."""

    with _JOBS_LOCK:
        _JOBS.clear()
        _ACTIVE.clear()
    clear_probe_caches()
