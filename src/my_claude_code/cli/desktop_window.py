"""Window providers for the MCC desktop shell.

The desktop app shows the admin dashboard in a window. There is no single
"native window" on three operating systems -- a native webview is WebView2 on
Windows, WKWebView on macOS and WebKitGTK on Linux -- so this module models the
window as a *seam* with an ordered fallback chain instead of a single engine.

The preferred provider is Chromium app-mode, because it is a real Chrome
profile: ``window.open(url, "_blank")`` (the ChatGPT and Anthropic OAuth
flows), ``<a download>`` (the analytics export window) and
``navigator.clipboard.writeText`` (the copy buttons) all behave exactly as they
do in the browser the dashboard was written against. Embedded webviews break
all three to varying degrees per engine.

Every provider loads ``http://127.0.0.1:<port>/admin`` over real HTTP. Nothing
here ever presents a ``file://`` origin, because the admin API's
``require_loopback_admin`` rejects one with a 403.

Since 6.44.0 the chain has a first link that is not a browser at all: the
project's own desktop shell, a small Tauri window fetched and verified by
``config/desktop_shell.py``. It leads ``auto`` because it is the only provider
that gives My Claude Code a window of its own -- its own icon, its own dock
entry, and on Linux its own tray. App-mode remains directly behind it, and
remains what an explicit ``window=app-mode`` selects, so nothing that works
today stops working.
"""

import json
import logging
import os
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from my_claude_code.cli.tool_paths import resolve_installed_command
from my_claude_code.config.desktop import (
    WINDOW_PREFERENCES,
    WindowPreference,
    chromium_binary,
    load_desktop_state,
    record_applied_window_size,
)
from my_claude_code.config.desktop_shell import (
    DesktopShellError,
    desktop_shell_enabled,
    ensure_desktop_shell,
    is_desktop_shell_installed,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.settings import get_settings

logger = logging.getLogger(__name__)

LINUX_WM_CLASS = "MyClaudeCode"
PROFILE_DIRNAME = "desktop-profile"

#: The command the shell calls back into for ``--print-status``. It is the only
#: way the shell learns where the config directory, the port and the admin URL
#: are (contract C1), so it is handed an absolute path rather than being left to
#: find ``mcc-desktop`` on a ``PATH`` a GUI launch may not have.
SHELL_DESKTOP_COMMAND_ENV = "MCC_SHELL_DESKTOP_COMMAND"

#: The command the shell starts when the ladder says the server must be started.
SHELL_SERVER_COMMAND_ENV = "MCC_SHELL_SERVER_COMMAND"

#: Set in the shell child's environment to say whether it owns the tray icon.
#: ``mcc-desktop`` is the only writer, and ``cli/desktop_status.py`` is the only
#: reader; see that module for why the answer is not simply ``tray_enabled``.
SHELL_TRAY_ENV = "MCC_DESKTOP_SHELL_TRAY"

#: Provider identifiers a *pinned* preference degrades through, in order. The
#: shell is deliberately not here: it is chosen by ``auto``, never as the
#: consolation prize for an explicit pin that could not be honoured.
PROVIDER_CHAIN: tuple[str, ...] = ("app-mode", "pywebview", "browser")

#: What ``auto`` walks. The shell first, then the browser-backed chain.
AUTO_PROVIDER_CHAIN: tuple[str, ...] = ("shell", *PROVIDER_CHAIN)


@runtime_checkable
class DesktopWindow(Protocol):
    """A showable dashboard window owned by the desktop app."""

    def open(self, url: str) -> None:
        """Show the window on ``url``."""

    def focus(self) -> bool:
        """Raise an existing window; return False when that is not possible."""

    def close(self) -> None:
        """Close the window without touching the server."""

    @property
    def is_open(self) -> bool:
        """Whether a window is currently believed to be showing."""


# ------------------------------------------------------------------- providers
#
# Chromium-family binary discovery (``chromium_binary``) lives in
# ``config.desktop`` -- it is also needed there to report what an ``auto``
# preference resolves to, without ``config`` importing this module or ``api``
# crossing into ``cli`` (see ``tests/contracts/test_import_boundaries.py``).


def _has_remembered_window_placement(profile_dir: Path) -> bool:
    """Best-effort check for a Chromium-remembered window placement.

    Chromium records the last window geometry in ``<profile>/Default/Preferences``
    under the ``browser.window_placement`` key once the user has actually shown
    a window in that profile. A missing file (first run), an unreadable file,
    or malformed JSON must all read as "no memory yet" rather than raise --
    this is a best-effort read, not a schema-validated one.
    """

    prefs_path = profile_dir / "Default" / "Preferences"
    try:
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return False
    if not isinstance(data, dict):
        return False
    browser = data.get("browser")
    return isinstance(browser, dict) and "window_placement" in browser


class AppModeWindow:
    """A dedicated Chrome/Edge/Brave window launched with ``--app``.

    App-mode is a chrome-less browser window backed by a private profile
    directory. It is a real browser, so the dashboard's OAuth popups, blob
    downloads and clipboard writes keep working unchanged.
    """

    def __init__(self, binary: str, *, profile_dir: Path | None = None) -> None:
        self._binary = binary
        self._profile_dir = profile_dir or (config_dir_path() / PROFILE_DIRNAME)
        self._process: subprocess.Popen[bytes] | None = None
        self._last_url: str | None = None

    @staticmethod
    def available() -> bool:
        return chromium_binary() is not None

    @classmethod
    def create(cls) -> AppModeWindow | None:
        binary = chromium_binary()
        return None if binary is None else cls(binary)

    def _pending_window_size(self) -> tuple[int, int] | None:
        """Return the size to force this launch, or ``None`` to defer to Chromium.

        Chromium already persists window geometry per profile in
        ``<profile>/Default/Preferences``. Passing ``--window-size`` on every
        launch would overwrite that memory the moment the user resizes or
        moves the window, so it is only forced on the profile's first run
        (no remembered placement yet) or when the configured size has
        actually changed since we last forced it -- a config change is
        expected to take effect even after the user has resized the window.
        """

        settings = get_settings()
        width, height = settings.desktop_window_width, settings.desktop_window_height
        if not _has_remembered_window_placement(self._profile_dir):
            return width, height
        state = load_desktop_state()
        if (state.last_applied_window_width, state.last_applied_window_height) != (
            width,
            height,
        ):
            return width, height
        return None

    def command(self, url: str) -> list[str]:
        command = [
            self._binary,
            f"--app={url}",
            f"--user-data-dir={self._profile_dir}",
        ]
        size = self._pending_window_size()
        if size is not None:
            command.append(f"--window-size={size[0]},{size[1]}")
        if sys.platform not in {"win32", "darwin"}:
            # Without an explicit WM class the window groups under "Chromium".
            command.append(f"--class={LINUX_WM_CLASS}")
        return command

    def open(self, url: str) -> None:
        self._last_url = url
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        size = self._pending_window_size()
        try:
            self._process = subprocess.Popen(self.command(url))
        except OSError:
            logger.warning("Could not launch %s in app mode.", self._binary)
            self._process = None
            webbrowser.open(url)
            return
        if size is not None:
            record_applied_window_size(size[0], size[1])

    def focus(self) -> bool:
        """Re-invoke the same app command so the profile raises its window.

        Chromium routes a second launch through the running profile process,
        which brings the existing app window forward instead of creating a
        duplicate. Best effort: if we never launched, there is nothing to
        raise and the caller should open instead.
        """

        process = self._process
        if process is None or process.poll() is not None:
            return False
        url = self._last_url
        if url is None:
            return False
        try:
            subprocess.Popen(self.command(url))
        except OSError:
            return False
        return True

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @property
    def is_open(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None


class ShellWindow:
    """The project's own desktop app: a window, an icon, and on Linux a tray.

    Unlike every other provider this one is a *separate program* that resolves
    nothing for itself. It runs ``mcc-desktop --print-status`` to learn the
    admin URL, the server-presence ladder and the timing budgets, which is why
    :meth:`environment` hands it an absolute path to that command rather than
    hoping ``PATH`` carries one: a window launched from a shortcut, a
    LaunchAgent or a ``.desktop`` file does not inherit a login shell's ``PATH``.

    ``open`` therefore takes the admin URL and does not pass it on. The URL is
    not this process's to give -- handing one over would create a second source
    of truth for the port, which is exactly what contract C1 forbids -- and the
    shell derives the same answer from the same status document.

    **Raising an existing window is a second launch, not a doorbell.** The shell
    holds ``tauri-plugin-single-instance``, whose second-instance callback
    focuses the window already open and exits; that is the same trick
    :class:`AppModeWindow` plays with a Chromium profile. Ringing
    ``desktop.activate`` from here instead would feed the tray's own activation
    watcher, which polls that very file, and the two would raise each other in a
    loop.
    """

    def __init__(self, binary: Path) -> None:
        self._binary = binary
        self._process: subprocess.Popen[bytes] | None = None

    @staticmethod
    def available() -> bool:
        """Whether an already-installed shell could be used without a download."""

        return desktop_shell_enabled() and is_desktop_shell_installed()

    @classmethod
    def create(cls) -> ShellWindow | None:
        """Return the shell, fetching the pinned release when it is absent.

        Every failure is a warning and a ``None``, never an exception: a
        machine that is offline, behind a proxy, on an unbuilt architecture or
        out of disk still has a working app-mode window one link down the
        chain. The desktop app not launching is a far worse outcome than the
        desktop app launching in a browser window.
        """

        if not desktop_shell_enabled():
            return None
        try:
            binary = ensure_desktop_shell()
        except DesktopShellError as exc:
            logger.warning(
                "Falling back to a browser window: the My Claude Code desktop "
                "app could not be installed. %s",
                exc,
            )
            return None
        return cls(binary)

    def environment(self, *, owns_tray: bool = False) -> dict[str, str]:
        """Return the environment the shell child is launched with.

        ``owns_tray`` is the Q2 decision made concrete: while the Python tray
        is running -- Windows and macOS, today -- the shell must not draw a
        second icon beside it.
        """

        environment = dict(os.environ)
        environment[SHELL_TRAY_ENV] = "1" if owns_tray else "0"
        for stem, name in (
            ("mcc-desktop", SHELL_DESKTOP_COMMAND_ENV),
            ("mcc-server", SHELL_SERVER_COMMAND_ENV),
        ):
            # Only set what we actually resolved. An empty override is treated
            # by the shell as "not set", but writing one anyway would hide a
            # PATH that does work from a reader of this environment.
            if environment.get(name, "").strip():
                continue
            resolved = resolve_installed_command(stem)
            if resolved is not None:
                environment[name] = resolved
        return environment

    def _spawn(self) -> subprocess.Popen[bytes] | None:
        try:
            return subprocess.Popen(
                [str(self._binary)],
                env=self.environment(owns_tray=not python_tray_is_running()),
            )
        except OSError:
            logger.warning("Could not launch the desktop app at %s.", self._binary)
            return None

    def open(self, url: str) -> None:
        process = self._spawn()
        if process is None:
            webbrowser.open(url)
            return
        self._process = process

    def focus(self) -> bool:
        """Launch again so the single-instance guard raises the open window."""

        if not self.is_open:
            return False
        # Deliberately not stored: this child exists only to hand the running
        # instance a "come to the front", and exits immediately afterwards.
        return self._spawn() is not None

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    @property
    def is_open(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None


def python_tray_is_running() -> bool:
    """Whether the pystray tray is the one drawing the status-area icon.

    One icon, and only one (the "two tray icons" risk in the spec). In 6.44.0
    the Python tray keeps it wherever it exists, because it is the tray that
    has been in front of users for releases and it carries menu items the
    shell's does not. That is Windows and macOS: ``pystray`` is declared
    ``sys_platform == 'win32' or sys_platform == 'darwin'`` in
    ``pyproject.toml``, so on Linux there is no Python tray and the shell's is
    the only one there has ever been.

    Availability is probed rather than assumed from ``sys.platform``, because
    an incomplete install is a real state and a machine with no tray at all
    should get the shell's.
    """

    if not load_desktop_state().tray_enabled:
        return False
    try:
        import pystray  # noqa: F401
    except ImportError, OSError:
        return False
    return True


class PywebviewWindow:
    """Embedded webview, used only when app-mode is unavailable.

    ``pywebview`` wraps three different engines, and the dashboard depends on
    three browser behaviours that embedded engines handle inconsistently, so
    this provider installs shims before the page can use them:

    * ``window.open`` is replaced with a bridge that hands the URL to the
      system browser, because the ChatGPT and Anthropic OAuth flows call it and
      a silent no-op makes login unreachable.
    * downloads require ``webview.settings["ALLOW_DOWNLOADS"]``; when that
      setting is absent the provider declares itself unavailable rather than
      shipping an export window that silently drops files.
    * the clipboard falls back to ``document.execCommand('copy')`` when
      ``navigator.clipboard`` is missing.

    It is also unavailable on macOS, where the GUI loop must own the main
    thread and the tray already does.
    """

    _SHIM_JS = """
    (function () {
      var api = window.pywebview && window.pywebview.api;
      if (api && api.open_external) {
        window.open = function (url) {
          if (url) { api.open_external(String(url)); }
          return null;
        };
      }
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        navigator.clipboard = {
          writeText: function (text) {
            var area = document.createElement('textarea');
            area.value = text;
            document.body.appendChild(area);
            area.select();
            try { document.execCommand('copy'); } finally { area.remove(); }
            return Promise.resolve();
          }
        };
      }
    })();
    """

    def __init__(self, module: Any) -> None:
        self._webview: Any = module
        self._window: Any = None

    @staticmethod
    def _module() -> Any:
        if sys.platform == "darwin":
            # pywebview's run loop requires the main thread, which pystray owns.
            return None
        try:
            import webview
        except ImportError, OSError:
            return None
        settings = getattr(webview, "settings", None)
        if not isinstance(settings, dict) or "ALLOW_DOWNLOADS" not in settings:
            return None
        return webview

    @staticmethod
    def available() -> bool:
        return PywebviewWindow._module() is not None

    @classmethod
    def create(cls) -> PywebviewWindow | None:
        module = cls._module()
        return None if module is None else cls(module)

    def open(self, url: str) -> None:
        import threading

        webview = self._webview
        webview_settings = webview.settings
        webview_settings["ALLOW_DOWNLOADS"] = True
        webview_settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        mcc_settings = get_settings()
        window = webview.create_window(
            "My Claude Code",
            url,
            js_api=_PywebviewApi(),
            width=mcc_settings.desktop_window_width,
            height=mcc_settings.desktop_window_height,
        )
        self._window = window
        loaded = getattr(getattr(window, "events", None), "loaded", None)
        if loaded is not None:
            loaded += self._install_shims
        threading.Thread(
            target=webview.start,
            name="mcc-desktop-webview",
            daemon=True,
        ).start()

    def _install_shims(self) -> None:
        window = self._window
        if window is None:
            return
        window.evaluate_js(self._SHIM_JS)

    def focus(self) -> bool:
        window = self._window
        if window is None:
            return False
        restore = getattr(window, "restore", None)
        if callable(restore):
            restore()
        show = getattr(window, "show", None)
        if callable(show):
            show()
        return True

    def close(self) -> None:
        window = self._window
        self._window = None
        if window is None:
            return
        destroy = getattr(window, "destroy", None)
        if callable(destroy):
            destroy()

    @property
    def is_open(self) -> bool:
        return self._window is not None


class _PywebviewApi:
    """JS-callable bridge exposed to the embedded page."""

    def open_external(self, url: str) -> None:
        webbrowser.open(url)


class BrowserTabWindow:
    """The default browser, in a normal tab. Always available, never focusable."""

    def __init__(self) -> None:
        self._opened = False

    @staticmethod
    def available() -> bool:
        return True

    @classmethod
    def create(cls) -> BrowserTabWindow:
        return cls()

    def open(self, url: str) -> None:
        webbrowser.open(url)
        self._opened = True

    def focus(self) -> bool:
        """A browser tab we do not own cannot be raised."""

        return False

    def close(self) -> None:
        self._opened = False

    @property
    def is_open(self) -> bool:
        return self._opened


_PROVIDERS: dict[str, Callable[[], DesktopWindow | None]] = {
    "shell": ShellWindow.create,
    "app-mode": AppModeWindow.create,
    "pywebview": PywebviewWindow.create,
    "browser": BrowserTabWindow.create,
}


def create_window(preference: str) -> DesktopWindow:
    """Return the first usable window provider for ``preference``.

    ``auto`` walks :data:`PROVIDER_CHAIN` in order. An explicit pin is tried
    first and then *degrades* through the remaining chain with a logged
    warning: an unavailable preference is a misconfiguration, and a guardrail
    must degrade rather than become an outage.
    """

    normalized: WindowPreference | str = preference
    if preference not in WINDOW_PREFERENCES:
        logger.warning(
            "Unknown window preference %r; falling back to 'auto'.", preference
        )
        normalized = "auto"

    if normalized == "auto":
        order = list(AUTO_PROVIDER_CHAIN)
    else:
        order = [normalized, *(name for name in PROVIDER_CHAIN if name != normalized)]

    for name in order:
        window = _PROVIDERS[name]()
        if window is None:
            continue
        if normalized != "auto" and name != normalized:
            logger.warning(
                "Window provider %r is unavailable; using %r instead.",
                normalized,
                name,
            )
        return window
    return BrowserTabWindow()
