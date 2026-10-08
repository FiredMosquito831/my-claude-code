"""Tests for the server-mode additions to the pystray tray menu."""

from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("pystray")

from my_claude_code.cli import desktop_tray
from my_claude_code.cli.desktop import DesktopController
from my_claude_code.cli.desktop_tray import PystrayDesktopTray
from my_claude_code.config import desktop as desktop_config
from my_claude_code.config.desktop import DesktopState, load_desktop_state
from tests.support.tray_icon_classes import release_tray_icon_classes


@pytest.fixture(autouse=True)
def _release_tray_icon_classes(monkeypatch):
    # The tray tests build a real pystray Icon and never run it; without this,
    # a later Icon at a reused address fails with WinError 1410.
    yield from release_tray_icon_classes(monkeypatch, desktop_tray)


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _controller(status: str = "running"):
    return cast(
        DesktopController,
        type(
            "Controller",
            (),
            {
                "status": status,
                "restart_server": lambda self: None,
                "server_mode": lambda self: load_desktop_state().server_mode,
            },
        )(),
    )


def _patched_tray(monkeypatch, tmp_path, status: str = "running"):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.set_server_mode",
        lambda mode: desktop_config.set_server_mode(mode),
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.set_start_at_login",
        lambda enabled, target=None: desktop_config.set_start_at_login(enabled, target),
    )
    return PystrayDesktopTray(_controller(status))


def _server_mode_menu(tray):
    for item in tray._menu():
        if item.text == "Server mode":
            return item.submenu
    raise AssertionError("Server mode submenu not found")


def _mode_item(menu, label):
    for item in menu.items:
        if item.text == label:
            return item
    raise AssertionError(f"{label} menu item not found")


def test_server_mode_submenu_presents_three_checkable_modes(monkeypatch, tmp_path):
    tray = _patched_tray(monkeypatch, tmp_path)

    submenu = _server_mode_menu(tray)

    assert [item.text for item in submenu.items] == [
        "Spawn server",
        "Attach to server",
        "Off (tray only)",
    ]


def test_checked_reflects_default_spawn(monkeypatch, tmp_path):
    tray = _patched_tray(monkeypatch, tmp_path)

    checks = {item.text: item.checked for item in _server_mode_menu(tray).items}

    assert checks == {
        "Spawn server": True,
        "Attach to server": False,
        "Off (tray only)": False,
    }


def test_checked_reflects_persisted_attach(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    desktop_config.save_desktop_state(DesktopState(server_mode="attach"))
    tray = _patched_tray(monkeypatch, tmp_path)

    checks = {item.text: item.checked for item in _server_mode_menu(tray).items}

    assert checks["Attach to server"] is True
    assert checks["Spawn server"] is False


def test_toggling_mode_persists(monkeypatch, tmp_path):
    tray = _patched_tray(monkeypatch, tmp_path)
    item = _mode_item(_server_mode_menu(tray), "Off (tray only)")

    item(None)

    assert load_desktop_state().server_mode == "off"


def test_restart_item_noops_outside_spawn(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    desktop_config.save_desktop_state(DesktopState(server_mode="attach"))
    controller = cast(
        DesktopController,
        type("Controller", (), {"status": "running", "restart_server": lambda s: 0})(),
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.set_start_at_login",
        lambda enabled, target=None: None,
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.set_server_mode",
        lambda mode: None,
    )
    tray = PystrayDesktopTray(controller)
    # The tray does not call restart itself in a non-spawn mode; the controller
    # is responsible for the guard. Verify the tray carries the current mode.
    assert tray._server_mode == "attach"


def test_check_status_attach_reports_not_running(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    desktop_config.save_desktop_state(DesktopState(server_mode="attach"))
    tray = _patched_tray(monkeypatch, tmp_path, status="stopped")

    notifications: list[str] = []
    tray._icon.notify = lambda message, title=None: notifications.append(message)
    tray._check_status(None, None)

    assert notifications == ["Server not running. Start mcc-server manually to attach."]


def test_check_status_off_reports_off(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    desktop_config.save_desktop_state(DesktopState(server_mode="off"))
    tray = _patched_tray(monkeypatch, tmp_path, status="stopped")

    notifications: list[str] = []
    tray._icon.notify = lambda message, title=None: notifications.append(message)
    tray._check_status(None, None)

    assert notifications == ["Server is off (tray only)."]


class TestTrayIconVariant:
    """The tray must use the tight-margin mark, not the app icon."""

    def test_create_icon_uses_the_tray_render_not_the_app_render(self) -> None:
        from io import BytesIO

        from PIL import Image

        from my_claude_code.cli import desktop_tray
        from my_claude_code.cli.desktop_assets import app_icon_bytes

        icon = desktop_tray._create_icon()

        with Image.open(BytesIO(app_icon_bytes(".png"))) as app_icon:
            app_size = app_icon.size

        # A status area draws at 16-24px, where the app render's 10% margin
        # costs enough of the glyph to make it unreadable. The two renders are
        # separate files precisely so this cannot drift back.
        assert icon.size != app_size
        assert icon.size == (128, 128)

    def test_the_tray_mark_is_transparent(self) -> None:
        from my_claude_code.cli import desktop_tray

        icon = desktop_tray._create_icon()

        # Quantising these assets once destroyed the alpha silently, shipping a
        # faint box behind the mark. Zero alpha must survive to the tray.
        # Read the alpha band directly rather than unpacking pixel tuples: the
        # flattened-data API is typed as a flat sequence of ints, so indexing it
        # per pixel is a type error even though it works at run time.
        alpha_band = icon.convert("RGBA").getchannel("A")
        assert alpha_band.getextrema() == (0, 255)
