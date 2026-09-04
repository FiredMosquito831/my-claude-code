"""Desktop deployment preferences and per-platform login registration."""

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which
from typing import Literal, cast

from .claude_discovery import native_origin
from .paths import config_dir_path
from .settings import get_settings

logger = logging.getLogger(__name__)

DESKTOP_STATE_FILENAME = "desktop.json"
SERVER_MODES = ("spawn", "attach", "off")
WINDOW_PREFERENCES = ("auto", "app-mode", "pywebview", "browser")
type ServerMode = Literal["spawn", "attach", "off"]
type WindowPreference = Literal["auto", "app-mode", "pywebview", "browser"]
type AutostartTarget = Literal["tray", "server"]

WINDOWS_RUN_VALUE = "MyClaudeCodeDesktop"
LAUNCH_AGENT_LABEL = "com.myclaudecode.tray"
LINUX_SYSTEMD_UNIT = "mcc-server.service"
LINUX_AUTOSTART_ID = "mcc-server"

_BOOLEAN_DEFAULTS: dict[str, bool] = {
    "tray_enabled": True,
    "start_at_login": False,
    "minimize_to_tray": False,
    "window_open": True,
}


class DesktopStateError(Exception):
    """Raised when persisted desktop state cannot be written."""


@dataclass(frozen=True)
class DesktopState:
    """Immutable snapshot of desktop deployment preferences."""

    tray_enabled: bool = True
    start_at_login: bool = False
    minimize_to_tray: bool = False
    server_mode: ServerMode = "spawn"
    window: WindowPreference = "auto"
    window_open: bool = True
    last_applied_window_width: int | None = None
    last_applied_window_height: int | None = None


def desktop_state_path() -> Path:
    return config_dir_path() / DESKTOP_STATE_FILENAME


def _default_state() -> DesktopState:
    return DesktopState()


def load_desktop_state() -> DesktopState:
    """Load desktop state, including the legacy boolean migration; never raises."""

    try:
        data = json.loads(desktop_state_path().read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()

    values: dict[str, bool | ServerMode | WindowPreference] = dict(_BOOLEAN_DEFAULTS)
    for name in _BOOLEAN_DEFAULTS:
        if isinstance(data.get(name), bool):
            values[name] = data[name]

    raw_mode = data.get("server_mode")
    if raw_mode in SERVER_MODES:
        server_mode: ServerMode = raw_mode
    elif isinstance(data.get("server_auto_start"), bool):
        server_mode = "spawn" if data["server_auto_start"] else "attach"
    else:
        server_mode = "spawn"

    raw_window = data.get("window")
    # An unknown persisted preference is tolerated, not fatal: the desktop app
    # must still start after a downgrade that no longer knows the value.
    window: WindowPreference = (
        raw_window if raw_window in WINDOW_PREFERENCES else "auto"
    )

    last_width = data.get("last_applied_window_width")
    last_height = data.get("last_applied_window_height")
    applied_width = last_width if isinstance(last_width, int) else None
    applied_height = last_height if isinstance(last_height, int) else None

    return DesktopState(
        tray_enabled=bool(values["tray_enabled"]),
        start_at_login=bool(values["start_at_login"]),
        minimize_to_tray=bool(values["minimize_to_tray"]),
        server_mode=server_mode,
        window=window,
        window_open=bool(values["window_open"]),
        last_applied_window_width=applied_width,
        last_applied_window_height=applied_height,
    )


def save_desktop_state(state: DesktopState) -> None:
    """Persist desktop state atomically, without the retired legacy key."""

    path = desktop_state_path()
    payload = json.dumps(asdict(state))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        raise DesktopStateError(f"Failed to save desktop state: {exc}") from exc


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _update_state(
    **overrides: bool | ServerMode | WindowPreference | int | None,
) -> DesktopState:
    current = load_desktop_state()

    raw_server_mode = overrides.get("server_mode")
    server_mode = (
        cast(ServerMode, raw_server_mode)
        if "server_mode" in overrides and raw_server_mode in SERVER_MODES
        else current.server_mode
    )
    raw_window = overrides.get("window")
    window = (
        cast(WindowPreference, raw_window)
        if "window" in overrides and raw_window in WINDOW_PREFERENCES
        else current.window
    )

    updated = DesktopState(
        tray_enabled=bool(overrides["tray_enabled"])
        if "tray_enabled" in overrides
        else current.tray_enabled,
        start_at_login=bool(overrides["start_at_login"])
        if "start_at_login" in overrides
        else current.start_at_login,
        minimize_to_tray=bool(overrides["minimize_to_tray"])
        if "minimize_to_tray" in overrides
        else current.minimize_to_tray,
        server_mode=server_mode,
        window=window,
        window_open=bool(overrides["window_open"])
        if "window_open" in overrides
        else current.window_open,
        last_applied_window_width=_int_or_none(overrides["last_applied_window_width"])
        if "last_applied_window_width" in overrides
        else current.last_applied_window_width,
        last_applied_window_height=_int_or_none(overrides["last_applied_window_height"])
        if "last_applied_window_height" in overrides
        else current.last_applied_window_height,
    )
    save_desktop_state(updated)
    return updated


def set_server_mode(mode: str) -> DesktopState:
    if mode not in SERVER_MODES:
        raise ValueError(f"Invalid server mode: {mode}")
    validated = cast(ServerMode, mode)
    return _update_state(server_mode=validated)


def set_window_preference(value: str) -> DesktopState:
    if value not in WINDOW_PREFERENCES:
        raise ValueError(f"Invalid window preference: {value}")
    validated = cast(WindowPreference, value)
    return _update_state(window=validated)


def set_tray_enabled(enabled: bool) -> DesktopState:
    """Persist the tray flag.

    The flag gates start-at-login: with the tray disabled, launch-time
    reconciliation removes instead of applies the OS registration -- an
    invisible tray must not relaunch at login.
    """

    return _update_state(tray_enabled=enabled)


def set_window_open(open_: bool) -> DesktopState:
    """Record whether a window is currently showing, for next-launch restore."""

    return _update_state(window_open=open_)


def record_applied_window_size(width: int, height: int) -> DesktopState:
    """Record the size we last forced with ``--window-size``.

    Read back on the next app-mode launch to tell "the user resized the
    window, leave Chromium's memory alone" apart from "the configured size
    changed, honour it once more".
    """

    return _update_state(
        last_applied_window_width=width, last_applied_window_height=height
    )


# ------------------------------------------------------ window provider resolution
#
# This lives in ``config`` (rather than ``cli.desktop_window``, which owns the
# actual window providers) so the admin API can report what an ``auto``
# preference resolves to without crossing the ``api -> cli`` boundary that
# ``tests/contracts/test_import_boundaries.py`` forbids. ``cli.desktop_window``
# imports :func:`chromium_binary` from here to avoid duplicating the lookup.


class _ChromiumCandidate:
    """One Chromium-family browser: PATH names first, then known install paths."""

    __slots__ = ("label", "names", "paths")

    def __init__(
        self, label: str, names: tuple[str, ...], paths: tuple[str, ...] = ()
    ) -> None:
        self.label = label
        self.names = names
        self.paths = paths

    def resolve(self) -> str | None:
        for name in self.names:
            found = which(name)
            if found:
                return found
        for path in self.paths:
            if Path(path).is_file():
                return path
        return None


# Edge ships with every supported Windows build, so it is the reliable first hit.
_WINDOWS_CHROMIUM_CANDIDATES = (
    _ChromiumCandidate(
        "Microsoft Edge",
        ("msedge",),
        (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    _ChromiumCandidate(
        "Google Chrome",
        ("chrome",),
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ),
    ),
    _ChromiumCandidate(
        "Brave",
        ("brave",),
        (
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    ),
)

# Safari has no ``--app`` equivalent, so it is deliberately absent.
_MACOS_CHROMIUM_CANDIDATES = (
    _ChromiumCandidate(
        "Google Chrome",
        ("google-chrome",),
        ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",),
    ),
    _ChromiumCandidate(
        "Microsoft Edge",
        ("microsoft-edge",),
        ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",),
    ),
    _ChromiumCandidate(
        "Brave",
        ("brave-browser",),
        ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",),
    ),
)

_LINUX_CHROMIUM_CANDIDATES = (
    _ChromiumCandidate("Google Chrome", ("google-chrome",)),
    _ChromiumCandidate("Google Chrome", ("google-chrome-stable",)),
    _ChromiumCandidate("Chromium", ("chromium",)),
    _ChromiumCandidate("Chromium", ("chromium-browser",)),
    _ChromiumCandidate("Brave", ("brave-browser",)),
    _ChromiumCandidate("Microsoft Edge", ("microsoft-edge",)),
)


def _chromium_candidates() -> tuple[_ChromiumCandidate, ...]:
    """Return the platform's Chromium candidates, read at call time.

    ``sys.platform`` is read here rather than at import time so tests can
    exercise every platform's search order on one machine.
    """

    if sys.platform == "win32":
        return _WINDOWS_CHROMIUM_CANDIDATES
    if sys.platform == "darwin":
        return _MACOS_CHROMIUM_CANDIDATES
    return _LINUX_CHROMIUM_CANDIDATES


def chromium_binary() -> str | None:
    """Return the first Chromium-family binary found, or ``None``."""

    found = _chromium_binary_with_label()
    return None if found is None else found[0]


def _configured_browser_path() -> tuple[str, str] | None:
    """Return ``(path, display_name)`` for an explicit ``DESKTOP_BROWSER_PATH``.

    An explicit path takes precedence over the candidate search below, since
    that search is the only way today to find a Chromium installed somewhere
    unusual. A path that does not exist is a guardrail, not an outage: it
    logs a warning and falls through to the search rather than failing.
    """

    configured = get_settings().desktop_browser_path.strip()
    if not configured:
        return None
    if Path(configured).is_file():
        return configured, "configured browser"
    logger.warning(
        "DESKTOP_BROWSER_PATH %r does not exist; falling back to browser search.",
        configured,
    )
    return None


def _chromium_binary_with_label() -> tuple[str, str] | None:
    """Return ``(path, display_name)`` for the first Chromium-family browser found."""

    configured = _configured_browser_path()
    if configured is not None:
        return configured
    for candidate in _chromium_candidates():
        resolved = candidate.resolve()
        if resolved is not None:
            return resolved, candidate.label
    return None


def pywebview_available() -> bool:
    """Best-effort check for a usable ``pywebview`` install.

    Mirrors ``cli.desktop_window.PywebviewWindow._module()``'s availability
    check without importing ``cli`` from ``config`` -- this module reports
    whether the provider *would* be usable, it does not construct a window.
    """

    if sys.platform == "darwin":
        # pywebview's run loop requires the main thread, which pystray owns.
        return False
    try:
        import webview
    except ImportError, OSError:
        return False
    settings = getattr(webview, "settings", None)
    return isinstance(settings, dict) and "ALLOW_DOWNLOADS" in settings


def resolve_auto_window() -> tuple[str, str]:
    """Return ``(provider, reason)`` describing what ``auto`` resolves to now.

    Mirrors ``cli.desktop_window.AUTO_PROVIDER_CHAIN`` / ``create_window``'s
    fallback order (the desktop shell, then app-mode, then pywebview, then
    browser) so the admin UI's "auto -> ..." readout matches what a real launch
    would actually pick.

    The shell is reported only when it is *already installed*. This function
    runs inside the server, on an admin request, and a launch's fetch is a
    network download -- something an admin page render must never trigger. The
    honest consequence is that the readout says ``app-mode`` on a machine whose
    next ``mcc-desktop`` launch would download the shell and use it; it becomes
    ``desktop app`` from the launch after that. Under-promising here is the
    right way round.
    """

    # Imported inside the function on purpose: this module is on the server's
    # startup path and the shell fetcher is not (see
    # tests/contracts/test_desktop_shell_not_on_the_server_path.py).
    from .desktop_shell import (
        DESKTOP_SHELL_RELEASE_TAG,
        desktop_shell_enabled,
        is_desktop_shell_installed,
    )

    if desktop_shell_enabled() and is_desktop_shell_installed():
        return "shell", f"My Claude Code {DESKTOP_SHELL_RELEASE_TAG}"

    chromium = _chromium_binary_with_label()
    if chromium is not None:
        return "app-mode", chromium[1]
    if pywebview_available():
        return "pywebview", "app-mode unavailable, pywebview installed"
    return "browser", "no Chromium browser found"


def default_autostart_target() -> AutostartTarget:
    """Return the ADR-defined target for the native platform."""

    return "tray" if native_origin() in {"windows", "macos"} else "server"


def set_start_at_login(
    enabled: bool, target: AutostartTarget | None = None
) -> DesktopState:
    """Apply/remove now and persist; the tray launch reconciles afterwards.

    Registration is honoured only while the tray itself is enabled: a
    disabled tray strips the OS entry at the next launch.
    """

    target = target or default_autostart_target()
    if enabled:
        apply_start_at_login(target)
    else:
        remove_start_at_login(target)
    return _update_state(start_at_login=enabled)


def _autostart_command(target: AutostartTarget) -> list[str]:
    module = (
        "my_claude_code.cli.desktop_entrypoint"
        if target == "tray"
        else "my_claude_code.cli.entrypoints"
    )
    return [sys.executable, "-m", module]


def _windows_run_key_value(target: AutostartTarget) -> str:
    return '"' + '" "'.join(_autostart_command(target)) + '"'


def _windows_run_key() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def _macos_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _macos_launch_agent_plist(target: AutostartTarget) -> str:
    arguments = "\n".join(
        f"        <string>{argument}</string>"
        for argument in _autostart_command(target)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "    <key>Label</key>\n"
        f"    <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n"
        f"{arguments}\n"
        "    </array>\n    <key>RunAtLoad</key>\n    <true/>\n"
        "</dict>\n</plist>\n"
    )


def _linux_systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / LINUX_SYSTEMD_UNIT


def _linux_systemd_content() -> str:
    command = " ".join(shlex.quote(part) for part in _autostart_command("server"))
    return (
        "[Unit]\nDescription=My Claude Code server\nAfter=network-online.target\n\n"
        f"[Service]\nType=simple\nExecStart={command}\nRestart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"


def _linux_autostart_content() -> str:
    command = " ".join(shlex.quote(part) for part in _autostart_command("server"))
    return (
        "[Desktop Entry]\nType=Application\nName=My Claude Code Server\n"
        f"Exec={command}\nX-GNOME-Autostart-enabled=true\n"
    )


def _systemd_user_available() -> bool:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    try:
        result = subprocess.run(
            [systemctl, "--user", "show-environment"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return result.returncode == 0


def apply_start_at_login(target: AutostartTarget | None = None) -> None:
    """Register the selected target, defaulting to the platform's ADR target."""

    selected = target or default_autostart_target()
    origin = native_origin()
    if origin == "windows":
        _apply_windows_start_at_login(selected)
    elif origin == "macos":
        _apply_macos_start_at_login(selected)
    else:
        _apply_linux_start_at_login(selected)


def remove_start_at_login(target: AutostartTarget | None = None) -> None:
    selected = target or default_autostart_target()
    origin = native_origin()
    if origin == "windows":
        _remove_windows_start_at_login()
    elif origin == "macos":
        _remove_macos_start_at_login()
    else:
        _remove_linux_start_at_login(selected)


def _apply_windows_start_at_login(target: AutostartTarget) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _windows_run_key(), 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, _windows_run_key_value(target)
        )


def _remove_windows_start_at_login() -> None:
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _windows_run_key(), 0, winreg.KEY_SET_VALUE
        )
    except OSError:
        return
    try:
        winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)


def _apply_macos_start_at_login(target: AutostartTarget) -> None:
    path = _macos_launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_macos_launch_agent_plist(target), encoding="utf-8")


def _remove_macos_start_at_login() -> None:
    with suppress(OSError):
        _macos_launch_agent_path().unlink(missing_ok=True)


def _apply_linux_start_at_login(target: AutostartTarget) -> None:
    if target != "server":
        raise DesktopStateError("Linux/WSL autostart supports the headless server only")
    if _systemd_user_available():
        path = _linux_systemd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_linux_systemd_content(), encoding="utf-8")
        systemctl = shutil.which("systemctl")
        if systemctl is not None:
            subprocess.run(
                [systemctl, "--user", "daemon-reload"], check=False, timeout=10
            )
            subprocess.run(
                [systemctl, "--user", "enable", "--now", LINUX_SYSTEMD_UNIT],
                check=False,
                timeout=10,
            )
        with suppress(OSError):
            _linux_autostart_path().unlink(missing_ok=True)
        return
    path = _linux_autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_linux_autostart_content(), encoding="utf-8")


def _remove_linux_start_at_login(target: AutostartTarget) -> None:
    if target != "server":
        return
    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [systemctl, "--user", "disable", "--now", LINUX_SYSTEMD_UNIT],
                check=False,
                timeout=10,
            )
            subprocess.run(
                [systemctl, "--user", "daemon-reload"], check=False, timeout=10
            )
    with suppress(OSError):
        _linux_systemd_path().unlink(missing_ok=True)
    with suppress(OSError):
        _linux_autostart_path().unlink(missing_ok=True)


def apply_tray_registration(enabled: bool) -> None:
    """Persist the tray flag; a running tray remains until Quit.

    The flag also gates start-at-login: with the tray disabled, the next
    launch's reconciliation removes any OS registration.
    """

    _update_state(tray_enabled=enabled)
