r"""The hermeticity guard: no test may reach the real machine.

Twice in one day a green test run deleted the developer's real
``HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MyClaudeCodeDesktop``
value, and repeated runs rewrote real files inside ``~/.fcc``. Both classes of
damage came from the same shape of mistake: a test redirects *some* of the
machine (``HOME``, one harness path) and the code under test then reaches the
rest of it (the registry, the eleven other harness catalogues, the staging
directory). Review cannot reliably catch a missing fixture argument, and a
per-run ``-p`` plugin is not enough either -- one was in place for the second
incident and the file it did not ``--ignore`` still got through.

So the guard lives here, is installed for the whole session from
``pytest_configure``, and is not something a test can forget to ask for:

* every test gets a ``tmp_path``-derived ``HOME``/``USERPROFILE``/``APPDATA``/
  ``LOCALAPPDATA``/``XDG_*`` and no ``MCC_CONFIG_DIR`` (a session-wide static
  ``MCC_CONFIG_DIR`` outranks ``HOME`` in ``resolve_config_dir`` and breaks
  ~112 tests, and sharing one directory across xdist workers races on
  ``os.replace(".env.tmp" -> ".env")``);
* ``winreg`` is replaced by an in-memory fake for the whole session. Reads
  succeed -- 720 tests read the registry indirectly through ``urllib`` and
  ``platform`` -- and every write fails unless the test opts in with
  ``@pytest.mark.touches_registry``. Even then it gets the fake: nothing in the
  suite may reach the real registry;
* every file-writing entry point (``builtins.open``/``io.open``, ``os.open``
  with a write flag, ``os.replace``/``rename``/``mkdir``/``unlink``/...,
  ``shutil.copy*``/``move``) refuses a target that is *inside* one of the real
  machine's configuration roots. Matching is by prefix, not equality: the
  6.41.1 guard compared the config *directory* for equality, so
  ``~/.fcc/crush/crush.json`` sailed straight past it;
* ``subprocess.Popen`` refuses browsers and coding-agent CLIs unless the test
  opts in with ``@pytest.mark.spawns_process``. ``python``, ``pwsh``, ``node``,
  ``sh``, ``git``, ``cmd`` and ``tasklist`` stay allowed -- the suite launches
  those on purpose;
* ``socket.socket.bind`` refuses the default server port, so a test can never
  fight a server the developer has running;
* at teardown the resolved config directory is checked, for real this time:
  the old assertion lived in a fixture finaliser that ran after
  ``_reset_config_dir_cache`` had already cleared what it was inspecting, so it
  never once fired across 1619 tests that did resolve the real home.

Violations raise :class:`HermeticityViolation`, which derives from
``BaseException`` on purpose. ``HarnessCatalogueFanoutPublisher._publish_one``
catches ``Exception`` around the write that produced the ``~/.fcc`` damage and
turns it into a ``logger.warning``; an ``AssertionError`` guard was therefore
swallowed and the test stayed green while the file was written. A guard that
production code can catch is not a guard.
"""

import asyncio
import asyncio.proactor_events
import builtins
import contextlib
import io
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import urllib.parse
from collections.abc import Generator
from pathlib import Path
from typing import Any

import primp
import pytest

from tests.support.offline_tokenizer import install_offline_tokenizer
from tests.support.token_host_block import (
    HermeticityViolation,
    install_token_host_block,
    refused_hosts,
)

__all__ = [
    "REAL_HOME",
    "HermeticityViolation",
    "hermetic_marker_gates",
    "is_local_host",
    "isolate_the_machine",
    "isolate_the_network",
    "network_refusals",
    "protected_root_for",
    "pytest_configure",
    "pytest_runtest_setup",
    "pytest_runtest_teardown",
    "under_real_config_dir",
]


# ``HermeticityViolation`` lives in the standard-library-only token-host block
# module so a child process can raise the same class without importing pytest.


# --------------------------------------------------------------- real machine
#
# Everything below is captured at import time, which is conftest import time --
# before any fixture has had a chance to redirect an environment variable or
# monkeypatch ``Path.home``. These are the paths of the machine the suite is
# running on, and nothing in ``tests/`` may write to them.


def _norm(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        raw = os.fspath(value)
    except TypeError, ValueError:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        return os.path.normcase(os.path.abspath(raw))
    except OSError, ValueError:
        return None


def _norm_all(value: Any) -> tuple[str, ...]:
    r"""Every spelling of ``value``: as written, and with the links resolved.

    ``os.path.abspath`` tidies separators and ``..`` and stops there, so one
    directory reachable by two names normalises to two different strings -- a
    symlink or bind mount on POSIX, a Windows 8.3 short name on a GitHub
    runner (``C:\Users\RUNNER~1\...`` for ``C:\Users\runneradmin\...``).
    The guard has to hold both spellings, because the code it watches resolves
    paths -- pytest's ``TempPathFactory`` calls ``Path(...).resolve()`` on the
    temp root -- while the environment variable the path came from does not.
    """

    written = _norm(value)
    if written is None:
        return ()
    try:
        resolved = _norm(os.path.realpath(written))
    except OSError, ValueError:
        resolved = None
    if resolved is None or resolved == written:
        return (written,)
    return (written, resolved)


REAL_HOME = Path.home().resolve()

# The three directories the config-dir rule and ``mcc-migrate`` can create,
# rename or remove, plus Claude Code's own. A test must never touch any of them.
REAL_CONFIG_DIRS = (
    REAL_HOME / ".mcc",
    REAL_HOME / ".fcc",
    REAL_HOME / ".fcc-old",
    REAL_HOME / ".claude",
)

_PROTECTED_CANDIDATES: tuple[Any, ...] = (
    *REAL_CONFIG_DIRS,
    # Roaming/Local application data, where the Windows installers, the Start
    # Menu shortcut and the uninstaller all live. ``%APPDATA%`` is consulted
    # first because a redirected profile can leave it pointing elsewhere.
    os.environ.get("APPDATA"),
    os.environ.get("LOCALAPPDATA"),
    REAL_HOME / "AppData" / "Roaming",
    REAL_HOME / "AppData" / "Local",
    # The all-users Start Menu.
    (
        Path(os.environ["PROGRAMDATA"]) / "Microsoft" / "Windows" / "Start Menu"
        if os.environ.get("PROGRAMDATA")
        else None
    ),
    # XDG and macOS autostart/support roots: ``~/.config/autostart``,
    # ``~/.local/share/applications``, ``~/Library/LaunchAgents``.
    REAL_HOME / ".config",
    REAL_HOME / ".local",
    REAL_HOME / "Library",
    # And, last so that every entry above wins the "which root" report, the
    # user profile itself. My Claude Code's whole subject matter is *other*
    # tools' configuration -- ``~/.codex``, ``~/.claude.json``, ``~/.crush``,
    # ``~/.gemini``, ``~/.aider.conf.yml`` -- and enumerating those is a list
    # that goes stale the next time a harness is added. The temporary
    # directories and the checkout are carved back out below, and on Windows
    # ``tmp_path`` lives under ``%LOCALAPPDATA%\Temp``, so the exemption is
    # what makes this workable rather than absolute.
    REAL_HOME,
)

_PROTECTED_ROOTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        spelling
        for candidate in _PROTECTED_CANDIDATES
        for spelling in _norm_all(candidate)
    )
)

# Pytest's ``tmp_path`` lives under ``%LOCALAPPDATA%\Temp`` on Windows -- and
# the checkout itself lives under ``$HOME`` on a Linux CI runner. Both are
# inside a protected root, so the allowed roots win over the protected ones;
# without that every test that writes a file at all would be a violation.
# ``TMPDIR``/``TEMP``/``TMP`` are read once here, before any redirect, in both
# spellings (see ``_norm_all``), and ``pytest_configure`` adds whatever pytest
# itself settled on for its base temp -- an allowed root that only covers the
# environment's spelling of the temp directory is the defect that killed the
# Wheel E2E run on Windows before its first test.
_ALLOWED_ROOTS: list[str] = []


def _allow_root(candidate: Any) -> None:
    """Exempt ``candidate`` from the guard, unless it would open a real root.

    An allowed root that *contains* a protected one would switch the guard off
    for it -- ``TEMP`` pointing at the profile itself would exempt ``~/.mcc``
    -- so a candidate is accepted only when no protected root sits underneath
    it. Adding a temp directory therefore stays exactly as narrow as it reads.
    """

    for spelling in _norm_all(candidate):
        if spelling in _ALLOWED_ROOTS:
            continue
        if any(
            root == spelling or root.startswith(spelling + os.sep)
            for root in _PROTECTED_ROOTS
        ):
            continue
        _ALLOWED_ROOTS.append(spelling)


def _seed_allowed_roots() -> None:
    for candidate in (
        tempfile.gettempdir(),
        os.environ.get("TMPDIR"),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        # The checkout itself, so a test may write into the repository
        # tree it was launched from (build artefacts, .pytest_cache).
        Path(__file__).resolve().parents[2],
    ):
        _allow_root(candidate)


_seed_allowed_roots()

# ``open("NUL", "w")`` is how pytest's own logging plugin discards output during
# ``pytest_configure``; refusing it aborts the session with an INTERNALERROR.
_DEVICE_NAMES = frozenset({"nul", "con", "conin$", "conout$", "null", "tty", "zero"})


def protected_root_for(target: Any) -> str | None:
    """Return the real-machine root ``target`` is inside, or ``None``.

    Prefix matching, deliberately: ``~/.fcc/crush/crush.json`` is as much a
    violation as ``~/.fcc`` itself, and the previous guard's equality test is
    exactly why nine tests rewrote the developer's Crush and Cline catalogues.
    """

    if isinstance(target, int):
        return None
    full = _norm(target)
    if full is None:
        return None
    if os.path.basename(full).lower() in _DEVICE_NAMES:
        return None
    for allowed in _ALLOWED_ROOTS:
        if full == allowed or full.startswith(allowed + os.sep):
            return None
    for root in _PROTECTED_ROOTS:
        if full == root or full.startswith(root + os.sep):
            return root
    return None


def under_real_config_dir(candidate: Any) -> bool:
    """True for one of the real home's config directories, or anything inside.

    Deliberately *not* "anywhere under the user profile": on Windows pytest's
    own ``tmp_path`` lives under the user's AppData/Local/Temp, so the broader
    test would call every correctly isolated directory a violation.
    """

    try:
        resolved = Path(candidate).resolve()
    except OSError, ValueError, TypeError:
        return False
    return any(
        resolved == directory or directory in resolved.parents
        for directory in REAL_CONFIG_DIRS
    )


_WRITE_HINT = (
    "Write under tmp_path instead. HOME/USERPROFILE/APPDATA/LOCALAPPDATA are "
    "already redirected for every test, so something here resolved a real path "
    "anyway: a cached module global, an unpatched harness-catalogue path, a "
    "staging directory, or a path reconstructed from a captured constant."
)


def _refuse(operation: str, target: Any, root: str) -> None:
    raise HermeticityViolation(
        f"HERMETICITY VIOLATION: {operation} refused.\n"
        f"  target: {target}\n"
        f"  inside: {root} -- a real directory on this machine.\n"
        f"  {_WRITE_HINT}\n"
        f"  See tests/README.md for the hermeticity contract."
    )


# ------------------------------------------------------------- per-test gates
#
# Opt-in markers are read once per test into module state, because the
# interceptors are process-wide and cannot take a fixture argument.

_gates = {"registry": False, "subprocess": False, "port": False, "network": False}


def _open_gates_for(node: Any) -> None:
    """Project the opt-in markers on ``node`` into the process-wide state."""

    _gates["registry"] = node.get_closest_marker("touches_registry") is not None
    _gates["subprocess"] = node.get_closest_marker("spawns_process") is not None
    _gates["port"] = node.get_closest_marker("binds_reserved_port") is not None
    _gates["network"] = node.get_closest_marker("reaches_network") is not None


# ---------------------------------------------------------------- winreg fake


class _FakeKey:
    def __init__(self, path: str) -> None:
        self.path = path

    def __enter__(self) -> _FakeKey:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def Close(self) -> None:
        return None

    def __repr__(self) -> str:
        return f"<fake registry key {self.path}>"


class HermeticWinreg:
    """An in-memory stand-in for ``winreg``, installed for the whole session.

    Reads answer from an empty store rather than failing: 720 tests reach the
    registry only through ``urllib.request.getproxies_registry`` and
    ``platform``, and turning those into failures would be pure noise. Writes
    are refused unless the test carries ``@pytest.mark.touches_registry``, and
    an opted-in write still lands here and never on the real machine.
    """

    HKEY_CLASSES_ROOT = "HKCR"
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_USERS = "HKU"
    HKEY_CURRENT_CONFIG = "HKCC"

    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    KEY_CREATE_SUB_KEY = 0x0004
    KEY_ENUMERATE_SUB_KEYS = 0x0008
    KEY_NOTIFY = 0x0010
    KEY_CREATE_LINK = 0x0020
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200
    KEY_READ = 0x20019
    KEY_WRITE = 0x20006
    KEY_EXECUTE = 0x20019
    KEY_ALL_ACCESS = 0xF003F

    REG_NONE = 0
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_BINARY = 3
    REG_DWORD = 4
    REG_DWORD_LITTLE_ENDIAN = 4
    REG_MULTI_SZ = 7
    REG_QWORD = 11

    error = OSError

    _WRITE_ACCESS = KEY_SET_VALUE | KEY_CREATE_SUB_KEY | KEY_CREATE_LINK

    def __init__(self) -> None:
        self.values: dict[str, dict[str, tuple[Any, int]]] = {}
        self.writes: list[tuple[str, str, str]] = []

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _path(root: Any, sub: Any) -> str:
        return f"{getattr(root, 'path', root)}\\{sub or ''}"

    def _check(self, operation: str, detail: str) -> None:
        if _gates["registry"]:
            return
        raise HermeticityViolation(
            f"HERMETICITY VIOLATION: winreg.{operation} refused.\n"
            f"  target: {detail}\n"
            f"  This test writes to the Windows registry. A run of the suite "
            f"deleted the developer's real "
            f"HKCU\\...\\Run\\MyClaudeCodeDesktop value exactly this way.\n"
            f"  Use the ``fake_winreg`` fixture (tests/support/fake_winreg.py), "
            f"or mark the test @pytest.mark.touches_registry to record the "
            f"write against this in-memory fake.\n"
            f"  See tests/README.md for the hermeticity contract."
        )

    # -- surface -----------------------------------------------------------

    def OpenKey(self, key: Any, sub_key: Any, reserved: int = 0, access: int = 0x20019):
        path = self._path(key, sub_key)
        if access & self._WRITE_ACCESS:
            self._check("OpenKey(..., write access)", path)
        return _FakeKey(path)

    OpenKeyEx = OpenKey

    def CreateKey(self, key: Any, sub_key: Any):
        path = self._path(key, sub_key)
        self._check("CreateKey", path)
        self.values.setdefault(path, {})
        self.writes.append(("CreateKey", path, ""))
        return _FakeKey(path)

    def CreateKeyEx(
        self, key: Any, sub_key: Any, reserved: int = 0, access: int = 0x20006
    ):
        return self.CreateKey(key, sub_key)

    def DeleteKey(self, key: Any, sub_key: Any) -> None:
        path = self._path(key, sub_key)
        self._check("DeleteKey", path)
        self.values.pop(path, None)
        self.writes.append(("DeleteKey", path, ""))

    def DeleteKeyEx(
        self, key: Any, sub_key: Any, access: int = 0x20019, reserved: int = 0
    ) -> None:
        self.DeleteKey(key, sub_key)

    def SetValue(self, key: Any, sub_key: Any, type: int, value: Any) -> None:
        path = self._path(key, sub_key)
        self._check("SetValue", path)
        self.values.setdefault(path, {})[""] = (value, type)
        self.writes.append(("SetValue", path, ""))

    def SetValueEx(
        self, key: Any, value_name: Any, reserved: int, type: int, value: Any
    ) -> None:
        path = getattr(key, "path", str(key))
        self._check("SetValueEx", f"{path} :: {value_name}")
        self.values.setdefault(path, {})[value_name or ""] = (value, type)
        self.writes.append(("SetValueEx", path, value_name or ""))

    def DeleteValue(self, key: Any, value_name: Any) -> None:
        path = getattr(key, "path", str(key))
        self._check("DeleteValue", f"{path} :: {value_name}")
        self.values.get(path, {}).pop(value_name or "", None)
        self.writes.append(("DeleteValue", path, value_name or ""))

    def QueryValue(self, key: Any, sub_key: Any) -> str:
        stored = self.values.get(self._path(key, sub_key), {}).get("")
        if stored is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return str(stored[0])

    def QueryValueEx(self, key: Any, value_name: Any) -> tuple[Any, int]:
        path = getattr(key, "path", str(key))
        stored = self.values.get(path, {}).get(value_name or "")
        if stored is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return stored

    def QueryInfoKey(self, key: Any) -> tuple[int, int, int]:
        path = getattr(key, "path", str(key))
        return (0, len(self.values.get(path, {})), 0)

    def EnumValue(self, key: Any, index: int) -> tuple[str, Any, int]:
        path = getattr(key, "path", str(key))
        items = sorted(self.values.get(path, {}).items())
        if index >= len(items):
            raise OSError(22, "No more data is available")
        name, (value, kind) = items[index]
        return (name, value, kind)

    def EnumKey(self, key: Any, index: int) -> str:
        raise OSError(22, "No more data is available")

    def CloseKey(self, key: Any) -> None:
        return None

    def FlushKey(self, key: Any) -> None:
        return None

    def ConnectRegistry(self, computer_name: Any, key: Any):
        return _FakeKey(str(key))

    def ExpandEnvironmentStrings(self, value: str) -> str:
        return os.path.expandvars(value)


SESSION_WINREG = HermeticWinreg()


# ----------------------------------------------------------- subprocess gate
#
# The denylist is the set of binaries that change the machine or open a window
# on the developer's screen. Everything the suite launches on purpose --
# ``python`` (86 launches), ``pwsh``/``powershell`` (83), ``node`` (15), ``sh``,
# ``git``, ``cmd``, ``tasklist`` and generated ``.cmd`` shims -- stays allowed.

_DENIED_EXECUTABLES = frozenset(
    {
        # browsers
        "brave",
        "brave-browser",
        "chrome",
        "chromium",
        "chromium-browser",
        "firefox",
        "google-chrome",
        "iexplore",
        "msedge",
        "opera",
        "safari",
        # coding-agent CLIs
        "aider",
        "claude",
        "codex",
        "commandcode",
        "crush",
        "cursor",
        "droid",
        "gemini",
        "kimi",
        "opencode",
        "pi",
        "qwen",
        # installers, package managers and machine-state tools
        # 7.102.0: what the Install button can run on the real machine --
        # Windows Installer, winget, Homebrew, fnm, macOS disk images and
        # VS Code's extension command -- stubbed, never launched.
        "brew",
        "code",
        "fnm",
        "hdiutil",
        "msiexec",
        "winget",
        "defaults",
        "explorer",
        "launchctl",
        "npm",
        "npx",
        "open",
        "pip",
        "pip3",
        "pnpm",
        "reg",
        "schtasks",
        "systemctl",
        "uv",
        "uvx",
        "wsl",
        "xdg-open",
        "yarn",
    }
)


def _executable_name(args: Any) -> str:
    try:
        if isinstance(args, (str, bytes, os.PathLike)):
            first: Any = args
        else:
            first = next(iter(args))
        raw = os.fspath(first)
    except TypeError, ValueError, StopIteration, OSError:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    name = os.path.basename(str(raw)).lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        name = name.removesuffix(suffix)
    return name


# ---------------------------------------------------------------- port gate
#
# ``Settings.port`` defaults to 8082 and the developer runs a server on it. No
# test binds a fixed port today (``tests/cli/test_port_diagnostics.py`` asks the
# OS for a free one), so this is a regression fence rather than a fix.
RESERVED_PORTS = frozenset({8082})


def _intercept(owner, name: str, replacement) -> None:
    """Install ``replacement`` over ``owner.name``.

    A ``setattr`` rather than a direct assignment, on purpose. Every wrapper
    here forwards ``*args, **kwargs``, so none of them can match the precise
    overload set the typeshed stubs give ``builtins.open``, ``os.open``,
    ``shutil.rmtree`` or ``Popen.__init__`` -- and this repository bans
    type-checker suppression comments outright. One deliberately unannotated
    seam is honest about that; twelve scattered suppressions would not be, and
    ``scripts/ci.sh --only suppressions`` would refuse them anyway.
    """

    setattr(owner, name, replacement)


def _winreg_module() -> types.ModuleType:
    """Expose :data:`SESSION_WINREG` as an actual module.

    ``import winreg`` must hand back something that *is* a module -- the
    application does ``import winreg`` inside the autostart functions, and
    ``sys.modules`` holds modules. The methods copied across stay bound to
    ``SESSION_WINREG``, so a test inspecting ``hermetic_marker_gates`` sees the
    writes the application made through the module.
    """

    module = types.ModuleType("winreg")
    for attribute in dir(SESSION_WINREG):
        if not attribute.startswith("_"):
            setattr(module, attribute, getattr(SESSION_WINREG, attribute))
    return module


# ------------------------------------------------------------- installation

_installed = False


def _install_interceptors() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    # -- winreg ------------------------------------------------------------
    module = _winreg_module()
    sys.modules["winreg"] = module
    sys.modules["_winreg"] = module

    # -- open --------------------------------------------------------------
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(character in str(mode) for character in "wxa+"):
            root = protected_root_for(file)
            if root is not None:
                _refuse(f"open(mode={mode!r})", file, root)
        return real_open(file, mode, *args, **kwargs)

    _intercept(builtins, "open", guarded_open)
    # ``pathlib.Path.open`` -- and therefore ``write_text``/``write_bytes`` --
    # calls ``io.open``, which is a distinct module attribute from
    # ``builtins.open``. Patching only one of them misses the most common
    # writer in the codebase.
    _intercept(io, "open", guarded_open)

    # -- os.open -----------------------------------------------------------
    write_flags = 0
    for flag in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"):
        write_flags |= getattr(os, flag, 0)
    real_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & write_flags:
            root = protected_root_for(path)
            if root is not None:
                _refuse(f"os.open(flags={flags:#x})", path, root)
        return real_os_open(path, flags, *args, **kwargs)

    _intercept(os, "open", guarded_os_open)

    # -- two-sided os operations ------------------------------------------
    def guard_two_sided(name: str, function):
        def guarded(src, dst, *args, **kwargs):
            for side in (src, dst):
                root = protected_root_for(side)
                if root is not None:
                    _refuse(f"os.{name}({src!r} -> {dst!r})", side, root)
            return function(src, dst, *args, **kwargs)

        return guarded

    for name in ("replace", "rename", "renames", "link", "symlink"):
        function = getattr(os, name, None)
        if function is not None:
            _intercept(os, name, guard_two_sided(name, function))

    # -- one-sided os operations ------------------------------------------
    def guard_one_sided(name: str, function):
        def guarded(path, *args, **kwargs):
            root = protected_root_for(path)
            if root is not None:
                _refuse(f"os.{name}()", path, root)
            return function(path, *args, **kwargs)

        return guarded

    for name in (
        "mkdir",
        "makedirs",
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "truncate",
    ):
        function = getattr(os, name, None)
        if function is not None:
            _intercept(os, name, guard_one_sided(name, function))

    # ``Path.mkdir`` calls ``os.mkdir``, but naming the pathlib call in the
    # failure message is what makes the report readable.
    real_path_mkdir = Path.mkdir

    def guarded_path_mkdir(self, *args, **kwargs):
        root = protected_root_for(self)
        if root is not None:
            _refuse("Path.mkdir()", self, root)
        return real_path_mkdir(self, *args, **kwargs)

    _intercept(Path, "mkdir", guarded_path_mkdir)

    # -- shutil ------------------------------------------------------------
    def guard_destination(name: str, function):
        def guarded(src, dst, *args, **kwargs):
            root = protected_root_for(dst)
            if root is not None:
                _refuse(f"shutil.{name}({src!r} -> {dst!r})", dst, root)
            return function(src, dst, *args, **kwargs)

        return guarded

    for name in ("copy", "copy2", "copyfile", "copytree", "move"):
        function = getattr(shutil, name, None)
        if function is not None:
            _intercept(shutil, name, guard_destination(name, function))

    real_rmtree = shutil.rmtree

    def guarded_rmtree(path, *args, **kwargs):
        root = protected_root_for(path)
        if root is not None:
            _refuse("shutil.rmtree()", path, root)
        return real_rmtree(path, *args, **kwargs)

    _intercept(shutil, "rmtree", guarded_rmtree)

    # -- subprocess --------------------------------------------------------
    real_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, args, *rest, **kwargs):
        name = _executable_name(args)
        if name in _DENIED_EXECUTABLES and not _gates["subprocess"]:
            raise HermeticityViolation(
                f"HERMETICITY VIOLATION: subprocess launch of {name!r} refused.\n"
                f"  args: {args!r}\n"
                f"  Browsers, coding-agent CLIs and machine-state tools must be "
                f"stubbed, not launched: a test run must not open a window, "
                f"install a package or reconfigure the developer's machine.\n"
                f"  Mark the test @pytest.mark.spawns_process if the launch is "
                f"genuinely the thing under test.\n"
                f"  See tests/README.md for the hermeticity contract."
            )
        return real_popen_init(self, args, *rest, **kwargs)

    _intercept(subprocess.Popen, "__init__", guarded_popen_init)

    # -- sockets -----------------------------------------------------------
    real_bind = socket.socket.bind

    def guarded_bind(self, address):
        port = None
        if isinstance(address, tuple) and len(address) >= 2:
            candidate = address[1]
            if isinstance(candidate, int):
                port = candidate
        if port in RESERVED_PORTS and not _gates["port"]:
            raise HermeticityViolation(
                f"HERMETICITY VIOLATION: bind to port {port} refused.\n"
                f"  address: {address!r}\n"
                f"  That is My Claude Code's default server port and the "
                f"developer may have a server running on it. Bind port 0 and "
                f"read the assigned port back, as "
                f"tests/cli/test_port_diagnostics.py does.\n"
                f"  See tests/README.md for the hermeticity contract."
            )
        return real_bind(self, address)

    _intercept(socket.socket, "bind", guarded_bind)


# ------------------------------------------------------------- network gate
#
# 2026-10-21: an audit run (a DNS audit hook over the suite) found existing
# tests resolving ``opencode.ai`` 49 times and ``integrate.api.nvidia.com`` 25
# times. Dropping the proxy variables (``isolate_the_machine``) does not stop
# that: the NIM client and the models.dev fetch never read ``HTTP(S)_PROXY``,
# and the Gemini door resolves providers through its own
# ``gemini_routes.resolve_provider``, so a test that patched only
# ``routes.resolve_provider`` built a real provider. Nor does Python's
# ``socket.connect`` audit event see everything: an asyncio connection on
# Windows goes through the proactor's ``ConnectEx`` and raises no event.
#
# So the gate sits at the three places every connection has to pass:
#
# * name resolution -- ``socket.getaddrinfo`` (which ``asyncio``'s
#   ``loop.getaddrinfo`` calls at call time, so anyio and httpx are covered),
#   ``gethostbyname``/``gethostbyname_ex`` and the reverse ``gethostbyaddr``;
# * the connection -- ``socket.socket.connect``/``connect_ex`` (sync sockets,
#   ``create_connection``, ssl, and asyncio's selector loop, which calls
#   ``sock.connect``) and the proactor loop's ``sock_connect`` (``ConnectEx``);
# * the one client that does neither: ``primp`` (the Rust HTTP stack with its
#   own resolver that ``ddgs`` drives since 9.16.x), gated on its own request
#   verbs.
#
# Loopback is always allowed -- the suite serves fake upstreams, proxies and
# SOCKS servers on 127.0.0.1 -- and so are IP literals for a *lookup* (no query
# leaves the machine for one); a *connection* to a non-loopback literal is
# refused like a name. A refusal raises ``HermeticityViolation`` (a
# ``BaseException``: ``except Exception`` in the application cannot swallow it)
# AND is recorded, and ``isolate_the_network`` fails the test at teardown even
# when something swallowed the raise anyway -- a ``gather(return_exceptions=
# True)`` or a worker thread that died with it. A test that genuinely needs the
# network opts out with ``@pytest.mark.reaches_network``; none on CI should.

#: Names that never leave the machine.
_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

#: Every refusal this process made, newest last ("<operation> <target>").
_network_refusals: list[str] = []


def _host_name(host: object) -> str | None:
    if isinstance(host, bytes | bytearray):
        host = bytes(host).decode("ascii", "ignore")
    if not isinstance(host, str):
        return None
    return host.strip().strip("[]").rstrip(".").lower()


def _ip_literal(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return None


def is_local_host(host: object) -> bool:
    """Whether a lookup of, or a connection to, ``host`` stays on this machine.

    ``None`` and ``""`` are the wildcard a server binds to; the unspecified
    address connects to the local machine where it connects at all.
    """

    if host is None:
        return True
    name = _host_name(host)
    if name is None:
        return False
    if name == "" or name in _LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    address = _ip_literal(name)
    if address is None:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def _lookup_stays_local(host: object) -> bool:
    """A lookup sends no query for a loopback name or for any IP literal."""

    if is_local_host(host):
        return True
    name = _host_name(host)
    return name is not None and _ip_literal(name) is not None


def _refuse_network(operation: str, target: object) -> None:
    if _gates["network"]:
        return
    _network_refusals.append(f"{operation} {target!r}")
    raise HermeticityViolation(
        f"HERMETICITY VIOLATION: {operation} {target!r} refused.\n"
        f"  The test suite reaches nothing but this machine: a lookup or a "
        f"connection to a real host is a live call -- it spends nothing today "
        f"and a quota tomorrow, and it makes the result depend on the network.\n"
        f"  Patch the resolver the code under test uses (a provider factory, "
        f"``gemini_routes.resolve_provider``, a fetch function) or serve a fake "
        f"on 127.0.0.1. Mark the test @pytest.mark.reaches_network only if the "
        f"network itself is the thing under test.\n"
        f"  See tests/support/hermetic.py (network gate)."
    )


def network_refusals() -> tuple[str, ...]:
    """The refusals this process has made so far (the gate's self-test reads it)."""

    return tuple(_network_refusals)


def _connect_target(address: Any) -> Any:
    """The host of a socket address, or ``None`` for a local (AF_UNIX) one."""

    if isinstance(address, tuple) and address:
        return address[0]
    return None


_network_gate_installed = False

#: The request verbs ``primp.Client`` exposes. ``request`` takes a method and a
#: URL; the shorthands take the URL alone. All of them are gated on the host.
PRIMP_REQUEST_VERBS = (
    "request",
    "get",
    "head",
    "options",
    "delete",
    "post",
    "put",
    "patch",
)


def _gated_primp_verb(verb: str, real):
    """One ``primp.Client`` request verb, refusing a non-local host.

    Same refusal text as the old ``ddgs.http_client.HttpClient.request`` gate,
    so ``tests/contracts/test_hermetic_guard.py`` keeps naming one thing.
    """

    def gated(self, *args, **kwargs):
        url = kwargs.get("url")
        if url is None:
            url = (
                args[1]
                if verb == "request" and len(args) > 1
                else args[0]
                if args
                else ""
            )
        host = urllib.parse.urlsplit(str(url)).hostname
        if not is_local_host(host):
            _refuse_network("ddgs (primp) request to", str(url))
        return real(self, *args, **kwargs)

    return gated


def _install_network_gate() -> None:
    """Install the gate for this process. Idempotent.

    Installed BEFORE the token-host block, so the block's wrappers stay the
    outermost ``socket.getaddrinfo``/``create_connection`` (its self-test
    checks their names) and a token host still gets the block's own message.
    """

    global _network_gate_installed
    if _network_gate_installed:
        return
    _network_gate_installed = True

    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex
    real_gethostbyaddr = socket.gethostbyaddr

    def gated_getaddrinfo(host, *args, **kwargs):
        if not _lookup_stays_local(host):
            _refuse_network("name lookup of", host)
        return real_getaddrinfo(host, *args, **kwargs)

    def gated_gethostbyname(host):
        if not _lookup_stays_local(host):
            _refuse_network("name lookup of", host)
        return real_gethostbyname(host)

    def gated_gethostbyname_ex(host):
        if not _lookup_stays_local(host):
            _refuse_network("name lookup of", host)
        return real_gethostbyname_ex(host)

    def gated_gethostbyaddr(host):
        if not is_local_host(host):
            _refuse_network("reverse lookup of", host)
        return real_gethostbyaddr(host)

    _intercept(socket, "getaddrinfo", gated_getaddrinfo)
    _intercept(socket, "gethostbyname", gated_gethostbyname)
    _intercept(socket, "gethostbyname_ex", gated_gethostbyname_ex)
    _intercept(socket, "gethostbyaddr", gated_gethostbyaddr)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def gated_connect(self, address):
        target = _connect_target(address)
        if target is not None and not is_local_host(target):
            _refuse_network("connection to", address)
        return real_connect(self, address)

    def gated_connect_ex(self, address):
        target = _connect_target(address)
        if target is not None and not is_local_host(target):
            _refuse_network("connection to", address)
        return real_connect_ex(self, address)

    _intercept(socket.socket, "connect", gated_connect)
    _intercept(socket.socket, "connect_ex", gated_connect_ex)

    # The proactor loop (Windows' default) connects with ``ConnectEx`` and
    # never calls ``socket.connect``: gate its ``sock_connect``, which every
    # ``loop.create_connection`` -- anyio, httpcore, aiohttp -- goes through.
    proactor = asyncio.proactor_events.BaseProactorEventLoop
    real_proactor_connect = proactor.sock_connect

    async def gated_proactor_sock_connect(self, sock, address):
        target = _connect_target(address)
        if target is not None and not is_local_host(target):
            _refuse_network("connection to", address)
        return await real_proactor_connect(self, sock, address)

    _intercept(proactor, "sock_connect", gated_proactor_sock_connect)

    # ``primp`` (the client underneath ``ddgs``, the DuckDuckGo web-search
    # adapter) is Rust, with its own resolver and sockets, invisible to
    # everything above. The gate sits on the client class itself rather than on
    # ``ddgs``: since ``ddgs`` 9.16.x every engine drives ``primp`` directly,
    # and a harness that replaces the client (tests/support/ddg_harness) must
    # be able to serve pages without tripping this gate -- replacing
    # ``primp.Client`` wholesale does that, because only the real class's verbs
    # are gated. The shorthands are gated too: primp implements ``get``/``post``
    # natively, not through Python's ``request``.
    for primp_verb in PRIMP_REQUEST_VERBS:
        _intercept(
            primp.Client,
            primp_verb,
            _gated_primp_verb(primp_verb, getattr(primp.Client, primp_verb)),
        )


@pytest.fixture(autouse=True)
def isolate_the_network(request: pytest.FixtureRequest):
    """Fail a test that tried to reach past this machine, even if it swallowed it.

    The gate raises at the call; this catches the refusals nobody re-raised
    -- an application ``except BaseException``, a ``gather(return_exceptions=
    True)``, a background thread that died with the violation.
    """

    refused_before = len(_network_refusals)
    yield
    refused = _network_refusals[refused_before:]
    if refused and request.node.get_closest_marker("reaches_network") is None:
        pytest.fail(
            "HERMETICITY VIOLATION: this test (or a thread it left running) "
            f"tried to reach past this machine: {'; '.join(refused)}. Patch the "
            "resolver the code under test uses or serve a fake on 127.0.0.1. "
            "See tests/support/hermetic.py (network gate).",
            pytrace=False,
        )


# ------------------------------------------------------------------- plugin


def _allow_pytest_basetemp(config: pytest.Config) -> None:
    r"""Register the directory pytest will keep ``tmp_path`` in.

    ``_ALLOWED_ROOTS`` is seeded from ``TMPDIR``/``TEMP``/``TMP`` as the
    environment spells them, but ``TempPathFactory.getbasetemp`` *resolves*
    that root before creating ``pytest-of-<user>`` inside it. On a GitHub
    Windows runner ``TEMP`` is the 8.3 spelling
    ``C:\Users\RUNNER~1\AppData\Local\Temp`` while ``LOCALAPPDATA`` is the
    long one, so the resolved base temp landed inside a protected root that no
    allowed root covered: pytest-xdist asked for the base temp from
    ``pytest_sessionstart`` and the session died with an INTERNALERROR before
    the first test (Wheel E2E run 34324414216, 2026-09-09).

    So the base temp is registered here, from the same inputs pytest uses and
    before any test runs. It never widens the exemption beyond a temp
    directory: ``_allow_root`` refuses a candidate that would contain one of
    the real machine's roots.
    """

    # ``--basetemp``, when the run gave one (xdist gives its workers one).
    _allow_root(getattr(config.option, "basetemp", None))
    _allow_root(os.environ.get("PYTEST_DEBUG_TEMPROOT"))
    _allow_root(tempfile.gettempdir())
    # Where GitHub's own actions keep their scratch files; ``RUNNER_TEMP`` is
    # not on the list ``tempfile`` consults, and a job that points the suite at
    # it should not have to know about this file.
    _allow_root(os.environ.get("RUNNER_TEMP"))
    # And, if the temp-path plugin has already been configured, the answer
    # itself rather than the ingredients.
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        with contextlib.suppress(Exception):
            _allow_root(factory.getbasetemp())


def pytest_configure(config: pytest.Config) -> None:
    _allow_pytest_basetemp(config)
    config.addinivalue_line(
        "markers",
        "touches_registry: the test writes to the (in-memory, faked) registry",
    )
    config.addinivalue_line(
        "markers",
        "spawns_process: the test may launch a denied executable",
    )
    config.addinivalue_line(
        "markers",
        "binds_reserved_port: the test may bind My Claude Code's default port",
    )
    config.addinivalue_line(
        "markers",
        "reaches_network: the test may resolve or connect to a host that is not "
        "this machine (the network gate's opt-out; nothing on CI may need it)",
    )
    config.addinivalue_line(
        "markers",
        "real_model_sweep: a catalogue sweep in this test lists real providers "
        "(over a fake the test serves) instead of meeting them offline "
        "(tests/conftest.py _isolate_model_sweeps)",
    )
    config.addinivalue_line(
        "markers",
        "request_log_close_bound: the test's subject is RequestLogStore.close()'s "
        "own bounded wait, so close() is NOT made to wait for the writer's end "
        "(tests/support/request_log_writes.py close_then_join)",
    )
    _install_interceptors()
    # The token estimator's BPE file, from the repository: without it every
    # CI run downloaded it from openaipublic.blob.core.windows.net.
    install_offline_tokenizer()
    # 2026-10-21: no test may resolve or connect past this machine. Before the
    # token-host block, so the block's wrappers stay outermost.
    _install_network_gate()
    # 7.69.1: no test may reach an OAuth token host. Below httpx, so mocked
    # transports keep working; see ``tests/support/token_host_block.py``.
    install_token_host_block()


#: A proxy moves the name lookup off this machine, past the token-host block.
_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def isolate_the_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
):
    """Give every test its own home, so nothing resolves to the real one.

    ``MCC_CONFIG_DIR`` is deliberately *unset* rather than pointed at a shared
    directory: it outranks ``HOME`` in ``resolve_config_dir``, so the ~112 tests
    that redirect ``HOME`` themselves and then assert on ``tmp_path/.mcc/.env``
    would read one file and the application would write another, and under
    ``-n auto`` every worker would race on the same ``.env.tmp`` rename.
    """

    # Not ``tmp_path/"home"``: a dozen tests build exactly that themselves with
    # ``mkdir(parents=True)`` and no ``exist_ok``, and would get a
    # ``FileExistsError`` from a redirect that had been there first.
    home = tmp_path / "hermetic-home"
    (home / ".fcc").mkdir(parents=True, exist_ok=True)
    (home / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
    (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
    # An empty ``.env``, so the redirected directory looks like a finished
    # installation rather than a half-built one: without it every test that
    # resolves the config directory logs "~/.fcc failed the 'env' check".
    (home / ".fcc" / ".env").touch()
    for variable in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(variable, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.delenv("MCC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("FCC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("FCC_ENV_FILE", raising=False)
    monkeypatch.delenv("MCC_ENV_FILE", raising=False)
    for variable in _PROXY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    refused_before = len(refused_hosts())
    yield home
    # A refusal swallowed by application code (``gather(return_exceptions=
    # True)`` captures even a BaseException) must still fail the test that
    # tried to reach a token host. Only the block's own self-test may.
    if len(refused_hosts()) > refused_before and (
        "test_no_live_token_hosts" not in request.node.nodeid
    ):
        pytest.fail(
            "HERMETICITY VIOLATION: this test tried to reach an OAuth token "
            f"host ({', '.join(refused_hosts()[refused_before:])}). Mock the "
            "token exchange instead. See tests/support/token_host_block.py.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def hermetic_marker_gates(request: pytest.FixtureRequest):
    """Reset the faked registry, and close the opt-in gates afterwards.

    The interceptors are installed once for the session and cannot take a
    fixture argument, so the markers are projected into module state -- by
    ``pytest_runtest_setup``, which is early enough for a fixture of any scope,
    and again here for anything that reaches the gates through this fixture --
    and closed again afterwards: a test that forgets its marker must fail, and
    a test that has one must not leave the gate open for the next one.
    """

    _open_gates_for(request.node)
    SESSION_WINREG.values.clear()
    SESSION_WINREG.writes.clear()
    yield SESSION_WINREG
    _gates["registry"] = False
    _gates["subprocess"] = False
    _gates["port"] = False
    _gates["network"] = False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Open the opt-in gates before any fixture -- of any scope -- is built.

    ``hermetic_marker_gates`` is function-scoped and pytest builds
    higher-scoped fixtures first, so a module- or session-scoped fixture that
    launches a denied executable ran while the gate its test had asked for was
    still shut: ``tests/api/test_docs_bundle_wheel.py`` builds a real wheel
    with ``uv`` from a module-scoped fixture and was refused however it was
    marked. Setting the gates from the item, before setup, closes that hole;
    the fixture still resets them at teardown so a marked test cannot leave a
    gate open for the next one.
    """

    _open_gates_for(item)


def _real_config_dir_resolved() -> Any:
    """The config directory resolved inside the real home, or ``None``."""

    paths = sys.modules.get("my_claude_code.config.paths")
    if paths is None:
        return None
    resolution = getattr(paths, "_resolution", None)
    if resolution is None:
        return None
    resolved = getattr(resolution, "path", None)
    if resolved is not None and under_real_config_dir(resolved):
        return resolved
    return None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, object]:
    """Fail a test that resolved the config directory to the real home.

    Read *before* the fixture finalisers, one of which
    (``_reset_config_dir_cache``) clears the very state being inspected. The
    identical assertion used to live in a fixture finaliser and never fired
    once, across 1619 tests that did resolve the real home.

    Raised only *after* pytest's own teardown has run, though (a wrapper, not
    a plain ``tryfirst`` hook): a plain hook that raised here skipped pytest's
    teardown of the item, and every later test on that worker then failed
    setup with "previous item was not torn down properly" -- one leak became
    10,000-13,000 errors on three Windows suite runs (v7.103.1, v7.105.0, the
    2026-10-24 nightly).
    """

    resolved = _real_config_dir_resolved()
    try:
        return (yield)
    finally:
        if resolved is not None:
            raise HermeticityViolation(
                f"HERMETICITY VIOLATION: this test resolved the config directory "
                f"to {resolved}, which is inside the real home. It reads the "
                f"developer's own .env, requests.db and custom_providers.json, so "
                f"it passes or fails for a reason that is not in the repository.\n"
                f"  HOME/USERPROFILE are redirected for every test; something "
                f"here (or a thread an earlier test left running) put them back "
                f"or resolved the path before the redirect.\n"
                f"  See tests/README.md for the hermeticity contract."
            )
