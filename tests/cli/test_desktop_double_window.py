"""One launch, one window: the browser the server used to open on its own.

``mcc-server`` opens the dashboard in the default browser once it is healthy
(``Settings.open_admin_browser``, on by default). That is right for a human who
typed ``mcc-server`` and wrong for every server the desktop app starts, because
the desktop app is about to show the dashboard in a window it owns. Before
6.44.0 a single ``mcc-desktop`` launch therefore produced two dashboards: the
app-mode window and a browser tab nobody asked for. It becomes worse with the
shell, whose window looks nothing like a browser tab.

These tests pin the fix at the only place the desktop app starts a server, and
pin the other half of "one window" too: raising the shell is a second launch its
own single-instance guard answers, never a second window.
"""

from pathlib import Path

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.cli.desktop import OPEN_BROWSER_ENV, DesktopController
from my_claude_code.config.desktop import DesktopState, save_desktop_state


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


class _Settings:
    host = "127.0.0.1"
    port = 9101
    desktop_server_start_timeout = 5.0
    desktop_health_check_interval = 0.01


class _Process:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def spawn(monkeypatch, tmp_path):
    """Run ``_spawn_server`` for real, with the ``Popen`` and the probe faked."""

    _set_home(monkeypatch, tmp_path)
    calls: dict[str, object] = {}

    def _popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(desktop_module.subprocess, "Popen", _popen)
    monkeypatch.setattr(desktop_module, "preflight_proxy", lambda url: None)
    monkeypatch.setattr(
        desktop_module, "resolve_installed_command", lambda stem: f"/bin/{stem}"
    )
    monkeypatch.setattr(desktop_module, "get_settings", lambda: _Settings())
    return calls


class TestBrowserSuppression:
    def test_the_spawned_server_is_told_not_to_open_a_browser(self, spawn) -> None:
        controller = DesktopController.__new__(DesktopController)
        controller._process = None

        controller._spawn_server(_Settings())

        assert spawn["kwargs"]["env"][OPEN_BROWSER_ENV] == "0"

    def test_the_rest_of_the_environment_is_inherited(self, spawn, monkeypatch) -> None:
        """The child still needs ``MCC_CONFIG_DIR``, ``PATH`` and everything else."""

        monkeypatch.setenv("MCC_DOUBLE_WINDOW_MARKER", "kept")
        controller = DesktopController.__new__(DesktopController)
        controller._process = None

        controller._spawn_server(_Settings())

        assert spawn["kwargs"]["env"]["MCC_DOUBLE_WINDOW_MARKER"] == "kept"

    def test_it_holds_even_when_the_user_asked_for_no_window(
        self, spawn, monkeypatch, tmp_path
    ) -> None:
        """``window_open=False`` means "no window", and a browser tab is a window."""

        save_desktop_state(DesktopState(window_open=False))
        controller = DesktopController.__new__(DesktopController)
        controller._process = None

        controller._spawn_server(_Settings())

        assert spawn["kwargs"]["env"][OPEN_BROWSER_ENV] == "0"

    def test_it_holds_for_the_module_fallback_command(self, spawn, monkeypatch) -> None:
        """Even when no shim is installed and the server runs as ``-m``."""

        monkeypatch.setattr(desktop_module, "resolve_installed_command", lambda s: None)
        controller = DesktopController.__new__(DesktopController)
        controller._process = None

        controller._spawn_server(_Settings())

        assert spawn["kwargs"]["env"][OPEN_BROWSER_ENV] == "0"
        assert "-m" in spawn["command"]

    def test_the_variable_is_the_one_settings_actually_reads(self) -> None:
        """A typo here would silently restore the bug, so name it from the model."""

        from my_claude_code.config.settings import Settings

        alias = Settings.model_fields["open_admin_browser"].validation_alias
        assert OPEN_BROWSER_ENV in alias.choices


class TestOneWindow:
    def test_the_tray_open_item_raises_the_shell_instead_of_opening_a_second(
        self, monkeypatch, tmp_path
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        opened: list[str] = []
        focused: list[int] = []

        class _Shell:
            is_open = True

            def open(self, url: str) -> None:
                opened.append(url)

            def focus(self) -> bool:
                focused.append(1)
                return True

            def close(self) -> None:
                return None

        controller = DesktopController.__new__(DesktopController)
        controller._window = _Shell()

        controller.open_admin()

        assert focused == [1]
        assert opened == []

    def test_a_closed_shell_is_opened_rather_than_raised(
        self, monkeypatch, tmp_path
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        opened: list[str] = []

        class _Shell:
            is_open = False

            def open(self, url: str) -> None:
                opened.append(url)

            def focus(self) -> bool:
                raise AssertionError("a closed window cannot be raised")

            def close(self) -> None:
                return None

        controller = DesktopController.__new__(DesktopController)
        controller._window = _Shell()

        controller.show_window()

        assert len(opened) == 1
        assert opened[0].endswith("/admin")
