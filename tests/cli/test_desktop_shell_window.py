"""The ``ShellWindow`` provider: when it leads the chain, and what it launches.

The shell is the first window provider that is a separate program rather than a
browser, so three things have to be true at once and each is pinned here: it
leads ``auto`` when it is available and silently steps aside when it is not; it
hands the child an absolute ``mcc-desktop`` and a tray decision; and raising an
open window is a second launch, never a doorbell -- because the tray polls that
doorbell itself and the two would raise each other forever.
"""

import subprocess
import sys

import pytest

from my_claude_code.cli import desktop_window
from my_claude_code.cli.desktop_window import (
    AUTO_PROVIDER_CHAIN,
    PROVIDER_CHAIN,
    SHELL_DESKTOP_COMMAND_ENV,
    SHELL_SERVER_COMMAND_ENV,
    SHELL_TRAY_ENV,
    AppModeWindow,
    BrowserTabWindow,
    PywebviewWindow,
    ShellWindow,
    create_window,
)
from my_claude_code.config import desktop_shell as shell_config
from my_claude_code.config.desktop_shell import DesktopShellError


class _FakeProcess:
    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def spawns(monkeypatch):
    """Record every process the provider starts, and start none of them."""

    started: list[_FakeProcess] = []

    def _popen(command, **kwargs):
        process = _FakeProcess(command, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(desktop_window.subprocess, "Popen", _popen)
    return started


@pytest.fixture
def shell(monkeypatch, tmp_path):
    """An installed shell binary the provider will find without downloading."""

    binary = tmp_path / "MyClaudeCode"
    binary.write_bytes(b"binary")
    monkeypatch.setattr(desktop_window, "ensure_desktop_shell", lambda: binary)
    monkeypatch.setattr(desktop_window, "is_desktop_shell_installed", lambda: True)
    monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: True)
    monkeypatch.setattr(desktop_window, "python_tray_is_running", lambda: False)
    monkeypatch.setattr(desktop_window, "resolve_installed_command", lambda stem: None)
    return binary


@pytest.fixture
def browser_providers(monkeypatch):
    """Control the three browser-backed providers independently of this machine."""

    state = {"app-mode": False, "pywebview": False}
    monkeypatch.setitem(
        desktop_window._PROVIDERS,
        "app-mode",
        lambda: AppModeWindow("chrome") if state["app-mode"] else None,
    )
    monkeypatch.setitem(
        desktop_window._PROVIDERS,
        "pywebview",
        lambda: PywebviewWindow(object()) if state["pywebview"] else None,
    )
    return state


class TestChain:
    def test_the_shell_leads_auto_when_it_is_available(
        self, shell, browser_providers
    ) -> None:
        browser_providers["app-mode"] = True

        assert isinstance(create_window("auto"), ShellWindow)

    @pytest.mark.parametrize("platform_name", ["win32", "darwin", "linux"])
    def test_the_shell_leads_auto_on_every_platform(
        self, shell, browser_providers, monkeypatch, platform_name
    ) -> None:
        """One chain, three operating systems. The shell is first on all of them."""

        browser_providers["app-mode"] = True
        monkeypatch.setattr(sys, "platform", platform_name)

        assert isinstance(create_window("auto"), ShellWindow)

    def test_auto_falls_back_to_app_mode_when_the_shell_is_absent(
        self, monkeypatch, browser_providers
    ) -> None:
        browser_providers["app-mode"] = True
        monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: True)
        monkeypatch.setattr(
            desktop_window,
            "ensure_desktop_shell",
            lambda: (_ for _ in ()).throw(DesktopShellError("no network")),
        )

        assert isinstance(create_window("auto"), AppModeWindow)

    def test_a_failed_fetch_warns_with_the_reason(
        self, monkeypatch, browser_providers, caplog
    ) -> None:
        """Never block the launch; always say why the app is not the window."""

        browser_providers["app-mode"] = True
        monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: True)
        monkeypatch.setattr(
            desktop_window,
            "ensure_desktop_shell",
            lambda: (_ for _ in ()).throw(DesktopShellError("getaddrinfo failed")),
        )

        with caplog.at_level("WARNING"):
            create_window("auto")

        assert "getaddrinfo failed" in caplog.text

    def test_auto_falls_all_the_way_to_a_browser_tab(
        self, monkeypatch, browser_providers
    ) -> None:
        monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: False)

        assert isinstance(create_window("auto"), BrowserTabWindow)

    def test_the_opt_out_keeps_the_shell_out_of_the_chain(
        self, monkeypatch, browser_providers
    ) -> None:
        """``DESKTOP_SHELL=off`` is the documented way back to app-mode."""

        browser_providers["app-mode"] = True
        monkeypatch.setenv(shell_config.DESKTOP_SHELL_ENABLED_ENV, "off")

        assert isinstance(create_window("auto"), AppModeWindow)

    def test_an_explicit_pin_is_still_honoured(self, shell, browser_providers) -> None:
        browser_providers["app-mode"] = True

        assert isinstance(create_window("app-mode"), AppModeWindow)

    def test_an_explicit_pin_never_degrades_into_the_shell(
        self, shell, browser_providers
    ) -> None:
        """``window=app-mode`` means "not the shell", so its fallbacks are browsers."""

        browser_providers["pywebview"] = True

        assert isinstance(create_window("app-mode"), PywebviewWindow)

    def test_the_pinned_chain_is_unchanged_and_auto_merely_gains_a_link(self) -> None:
        assert PROVIDER_CHAIN == ("app-mode", "pywebview", "browser")
        assert ("shell", *PROVIDER_CHAIN) == AUTO_PROVIDER_CHAIN


class TestLaunch:
    def test_open_runs_the_installed_binary(self, shell, spawns) -> None:
        window = ShellWindow(shell)

        window.open("http://127.0.0.1:9000/admin")

        assert spawns[0].command == [str(shell)]
        assert window.is_open

    def test_the_admin_url_is_not_passed_on_the_command_line(
        self, shell, spawns
    ) -> None:
        """C1: the shell derives the URL from ``--print-status``, not from us."""

        ShellWindow(shell).open("http://127.0.0.1:9000/admin")

        assert "9000" not in " ".join(spawns[0].command)

    def test_the_child_is_told_where_mcc_desktop_is(
        self, shell, spawns, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            desktop_window,
            "resolve_installed_command",
            lambda stem: f"C:/tools/{stem}.exe",
        )

        ShellWindow(shell).open("http://127.0.0.1:9000/admin")

        environment = spawns[0].kwargs["env"]
        assert environment[SHELL_DESKTOP_COMMAND_ENV] == "C:/tools/mcc-desktop.exe"
        assert environment[SHELL_SERVER_COMMAND_ENV] == "C:/tools/mcc-server.exe"

    def test_an_existing_override_is_left_alone(
        self, shell, spawns, monkeypatch
    ) -> None:
        monkeypatch.setenv(SHELL_DESKTOP_COMMAND_ENV, "/custom/mcc-desktop")
        monkeypatch.setattr(
            desktop_window, "resolve_installed_command", lambda stem: "/found/here"
        )

        ShellWindow(shell).open("http://127.0.0.1:9000/admin")

        assert (
            spawns[0].kwargs["env"][SHELL_DESKTOP_COMMAND_ENV] == "/custom/mcc-desktop"
        )

    def test_the_python_tray_keeps_the_icon_when_it_is_running(
        self, shell, spawns, monkeypatch
    ) -> None:
        """Q2, made concrete: exactly one tray icon, and today it is Python's."""

        monkeypatch.setattr(desktop_window, "python_tray_is_running", lambda: True)

        ShellWindow(shell).open("http://127.0.0.1:9000/admin")

        assert spawns[0].kwargs["env"][SHELL_TRAY_ENV] == "0"

    def test_the_shell_draws_the_tray_when_python_has_none(self, shell, spawns) -> None:
        ShellWindow(shell).open("http://127.0.0.1:9000/admin")

        assert spawns[0].kwargs["env"][SHELL_TRAY_ENV] == "1"

    def test_a_launch_failure_falls_back_to_the_browser(
        self, shell, monkeypatch
    ) -> None:
        def _boom(command, **kwargs):
            raise OSError("no such file")

        opened: list[str] = []
        monkeypatch.setattr(desktop_window.subprocess, "Popen", _boom)
        monkeypatch.setattr(desktop_window.webbrowser, "open", opened.append)

        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")

        assert opened == ["http://127.0.0.1:9000/admin"]
        assert not window.is_open


class TestSingleInstance:
    def test_focus_launches_again_so_the_guard_raises_the_open_window(
        self, shell, spawns
    ) -> None:
        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")

        assert window.focus() is True
        assert len(spawns) == 2

    def test_focus_does_not_adopt_the_transient_child(self, shell, spawns) -> None:
        """The second launch exits immediately; treating it as *the* window
        would make ``is_open`` false a moment later and stop the app."""

        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")
        first = spawns[0]

        window.focus()
        spawns[1].returncode = 0

        assert window.is_open
        window.close()
        assert first.terminated

    def test_focus_reports_no_window_to_raise_when_none_is_open(
        self, shell, spawns
    ) -> None:
        assert ShellWindow(shell).focus() is False
        assert spawns == []

    def test_focus_never_rings_the_activation_doorbell(
        self, shell, spawns, tmp_path, monkeypatch
    ) -> None:
        """Ringing it would feed the tray's own watcher and loop forever."""

        monkeypatch.setattr(
            desktop_window, "config_dir_path", lambda: tmp_path / "config"
        )
        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")

        window.focus()

        assert not (tmp_path / "config" / "desktop.activate").exists()


class TestClose:
    def test_close_terminates_the_child(self, shell, spawns) -> None:
        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")

        window.close()

        assert spawns[0].terminated
        assert not window.is_open

    def test_closing_twice_is_harmless(self, shell, spawns) -> None:
        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")
        window.close()
        window.close()

        assert len(spawns) == 1

    def test_a_child_that_will_not_stop_is_killed(self, shell, spawns) -> None:
        window = ShellWindow(shell)
        window.open("http://127.0.0.1:9000/admin")
        process = spawns[0]

        def _wait(timeout: float = 5.0):
            raise subprocess.TimeoutExpired("MyClaudeCode", timeout)

        process.wait = _wait
        window.close()

        assert process.returncode == -9


class TestAvailability:
    def test_available_reports_an_installed_shell_without_downloading(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: True)
        monkeypatch.setattr(desktop_window, "is_desktop_shell_installed", lambda: True)

        assert ShellWindow.available() is True

    def test_available_is_false_when_the_shell_is_switched_off(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: False)
        monkeypatch.setattr(desktop_window, "is_desktop_shell_installed", lambda: True)

        assert ShellWindow.available() is False

    def test_create_returns_nothing_when_switched_off(self, monkeypatch) -> None:
        monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: False)

        assert ShellWindow.create() is None

    def test_a_disabled_tray_means_python_is_not_drawing_one(
        self, monkeypatch, tmp_path
    ) -> None:
        from my_claude_code.config.desktop import DesktopState

        monkeypatch.setattr(
            desktop_window,
            "load_desktop_state",
            lambda: DesktopState(tray_enabled=False),
        )

        assert desktop_window.python_tray_is_running() is False
