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

import builtins
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest

__all__ = [
    "REAL_HOME",
    "HermeticityViolation",
    "hermetic_marker_gates",
    "isolate_the_machine",
    "protected_root_for",
    "pytest_configure",
    "pytest_runtest_teardown",
    "under_real_config_dir",
]


class HermeticityViolation(BaseException):
    """A test tried to touch the real machine.

    Derived from ``BaseException`` so that no ``except Exception`` in the
    application -- and there are many, including the one wrapped around the
    harness-catalogue write that damaged the developer's ``~/.fcc`` -- can
    swallow it into a warning and leave the test green.
    """


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
        normalised
        for normalised in (_norm(candidate) for candidate in _PROTECTED_CANDIDATES)
        if normalised is not None
    )
)

# Pytest's ``tmp_path`` lives under ``%LOCALAPPDATA%\Temp`` on Windows -- and
# the checkout itself lives under ``$HOME`` on a Linux CI runner. Both are
# inside a protected root, so the allowed roots win over the protected ones;
# without that every test that writes a file at all would be a violation.
# ``TMPDIR``/``TEMP``/``TMP`` are read once here, before any redirect.
_ALLOWED_ROOTS: tuple[str, ...] = tuple(
    dict.fromkeys(
        normalised
        for normalised in (
            _norm(candidate)
            for candidate in (
                tempfile.gettempdir(),
                os.environ.get("TMPDIR"),
                os.environ.get("TEMP"),
                os.environ.get("TMP"),
                # The checkout itself, so a test may write into the repository
                # tree it was launched from (build artefacts, .pytest_cache).
                Path(__file__).resolve().parents[2],
            )
        )
        if normalised is not None
    )
)

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

_gates = {"registry": False, "subprocess": False, "port": False}


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


# ------------------------------------------------------------------- plugin


def pytest_configure(config: pytest.Config) -> None:
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
    _install_interceptors()


@pytest.fixture(autouse=True)
def isolate_the_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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
    yield home


@pytest.fixture(autouse=True)
def hermetic_marker_gates(request: pytest.FixtureRequest):
    """Open the opt-in gates the process-wide interceptors read.

    The interceptors are installed once for the session and cannot take a
    fixture argument, so the markers are projected into module state here and
    closed again afterwards -- a test that forgets its marker must fail, and a
    test that has one must not leave the gate open for the next one.
    """

    _gates["registry"] = request.node.get_closest_marker("touches_registry") is not None
    _gates["subprocess"] = request.node.get_closest_marker("spawns_process") is not None
    _gates["port"] = request.node.get_closest_marker("binds_reserved_port") is not None
    SESSION_WINREG.values.clear()
    SESSION_WINREG.writes.clear()
    yield SESSION_WINREG
    _gates["registry"] = False
    _gates["subprocess"] = False
    _gates["port"] = False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Fail a test that resolved the config directory to the real home.

    ``tryfirst`` so this runs *before* the fixture finalisers, one of which
    (``_reset_config_dir_cache``) clears the very state being inspected. The
    identical assertion used to live in a fixture finaliser and never fired
    once, across 1619 tests that did resolve the real home.
    """

    paths = sys.modules.get("my_claude_code.config.paths")
    if paths is None:
        return
    resolution = getattr(paths, "_resolution", None)
    if resolution is None:
        return
    resolved = getattr(resolution, "path", None)
    if resolved is not None and under_real_config_dir(resolved):
        raise HermeticityViolation(
            f"HERMETICITY VIOLATION: this test resolved the config directory "
            f"to {resolved}, which is inside the real home. It reads the "
            f"developer's own .env, requests.db and custom_providers.json, so "
            f"it passes or fails for a reason that is not in the repository.\n"
            f"  HOME/USERPROFILE are redirected for every test; something here "
            f"put them back or resolved the path before the redirect.\n"
            f"  See tests/README.md for the hermeticity contract."
        )
