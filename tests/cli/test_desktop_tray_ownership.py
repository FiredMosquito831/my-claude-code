"""Decision Q2: on Windows the desktop app owns the status-area icon.

The user's own observation, 2026-09-08: *"right now desktop and tray are
together: if I close the tray the desktop app dies too, and if I close the
desktop app it can be relaunched and live via the tray."* That is the v6.43.0
shell paired with the Python tray, and it is two processes each holding half of
one idea. The target is one owner: closing the **window** hides it to the app's
own tray icon and the server keeps running; the tray's *Open dashboard* shows
it again; the tray's *Quit* is the only thing that ends the app.

The Python tray is not deleted. It stays the fallback for every machine that
has no desktop app installed -- which is most of them -- and macOS keeps it
outright, because pystray's macOS backend must own the main thread and moving
that is a change of its own.
"""

import sys
import types

import pytest

from my_claude_code.cli import desktop_window
from my_claude_code.config.desktop import DesktopState


@pytest.fixture
def tray_preference(monkeypatch):
    monkeypatch.setattr(
        desktop_window, "load_desktop_state", lambda: DesktopState(tray_enabled=True)
    )


def _shell(monkeypatch, *, enabled: bool, installed: bool, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(desktop_window, "desktop_shell_enabled", lambda: enabled)
    monkeypatch.setattr(desktop_window, "is_desktop_shell_installed", lambda: installed)


def test_the_shell_owns_the_tray_on_windows_when_it_is_installed(
    monkeypatch, tray_preference
):
    _shell(monkeypatch, enabled=True, installed=True, platform="win32")
    assert desktop_window.shell_owns_tray()
    # And the Python tray stands down, which is what stops two icons.
    assert not desktop_window.python_tray_is_running()


def test_without_a_desktop_app_the_python_tray_is_still_the_tray(
    monkeypatch, tray_preference
):
    """A change of owner, not a platform losing its tray."""

    _shell(monkeypatch, enabled=True, installed=False, platform="win32")
    assert not desktop_window.shell_owns_tray()
    monkeypatch.setitem(sys.modules, "pystray", object())
    assert desktop_window.python_tray_is_running()


def test_desktop_shell_off_keeps_the_python_tray(monkeypatch, tray_preference):
    _shell(monkeypatch, enabled=False, installed=True, platform="win32")
    assert not desktop_window.shell_owns_tray()


def test_macos_keeps_the_python_tray_for_now(monkeypatch, tray_preference):
    """Deliberate, and the reason is in ``SHELL_OWNS_TRAY_PLATFORMS``."""

    _shell(monkeypatch, enabled=True, installed=True, platform="darwin")
    assert not desktop_window.shell_owns_tray()
    assert "darwin" not in desktop_window.SHELL_OWNS_TRAY_PLATFORMS


def test_linux_never_had_a_python_tray(monkeypatch, tray_preference):
    _shell(monkeypatch, enabled=True, installed=True, platform="linux")
    assert desktop_window.shell_owns_tray()


def test_a_tray_the_operator_switched_off_stays_off(monkeypatch):
    """``tray_enabled`` is a preference and it outranks the ownership question."""

    monkeypatch.setattr(
        desktop_window, "load_desktop_state", lambda: DesktopState(tray_enabled=False)
    )
    _shell(monkeypatch, enabled=True, installed=False, platform="win32")
    assert not desktop_window.python_tray_is_running()


def _fake_tray(monkeypatch, launched: list[str]):
    """Stand a tray adapter in for pystray's, which CI does not have.

    ``pystray`` is declared ``win32 or darwin`` in ``pyproject.toml``, so
    importing ``cli.desktop_tray`` on the Linux runner raises. Patching the
    attribute would import it; publishing a module in its place does not, and
    it is the import itself -- whether ``_launch_host`` reaches for the Python
    tray at all -- that these two tests are about.
    """

    module = types.ModuleType("my_claude_code.cli.desktop_tray")
    monkeypatch.setattr(
        module, "launch", lambda: launched.append("PystrayDesktopTray"), raising=False
    )
    monkeypatch.setitem(sys.modules, "my_claude_code.cli.desktop_tray", module)


def test_the_host_runs_without_a_pystray_tray_when_the_shell_owns_it(monkeypatch):
    """The entry point, which is where the second icon would actually appear."""

    from my_claude_code.cli import desktop_entrypoint

    monkeypatch.setattr(desktop_window, "shell_owns_tray", lambda: True)
    launched: list[str] = []
    _fake_tray(monkeypatch, launched)
    monkeypatch.setattr(
        "my_claude_code.cli.desktop.launch_desktop",
        lambda factory, **_kwargs: launched.append(factory.__name__),
    )
    desktop_entrypoint._launch_host()
    assert launched == ["WindowOnlyHost"]


def test_the_host_still_runs_the_pystray_tray_when_it_owns_the_icon(monkeypatch):
    from my_claude_code.cli import desktop_entrypoint

    monkeypatch.setattr(desktop_window, "shell_owns_tray", lambda: False)
    launched: list[str] = []
    _fake_tray(monkeypatch, launched)
    desktop_entrypoint._launch_host()
    assert launched == ["PystrayDesktopTray"]


def test_a_machine_with_no_python_tray_at_all_still_gets_a_host(monkeypatch):
    """The Linux case, and the CI runner's: there is no pystray to import.

    ``_launch_host`` must fall through to the window-only host rather than
    raising -- a machine with a desktop app and no tray module is a supported
    install, not a crash.
    """

    from my_claude_code.cli import desktop_entrypoint

    monkeypatch.setattr(desktop_window, "shell_owns_tray", lambda: False)
    launched: list[str] = []
    monkeypatch.setitem(sys.modules, "my_claude_code.cli.desktop_tray", None)
    monkeypatch.setattr(
        "my_claude_code.cli.desktop.launch_desktop",
        lambda factory, **_kwargs: launched.append(factory.__name__),
    )
    desktop_entrypoint._launch_host()
    assert launched == ["WindowOnlyHost"]
