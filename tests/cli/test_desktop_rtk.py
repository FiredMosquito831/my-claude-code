"""Tests for the RTK token-optimizer additions to the pystray tray menu.

The pystray classes only construct real ``Icon`` objects on a platform with a
backend, so the menu items are inspected via ``PystrayDesktopTray._menu()`` and
the toggle handlers are driven directly against the plain ``MenuItem`` objects
the menu returns. Assertions focus on the state file and the reconciler call,
which is the contract the tray shares with the CLI and the admin dashboard.
"""

from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("pystray")

from my_claude_code.cli.desktop import DesktopController
from my_claude_code.cli.desktop_tray import PystrayDesktopTray
from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.desktop import (
    load_desktop_state,
    set_start_at_login,
    set_tray_enabled,
)
from my_claude_code.config.rtk import RtkState, load_rtk_state


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _patched_tray(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    applied: list[RtkState] = []
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.apply_rtk_state",
        lambda state, **kwargs: applied.append(state),
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.load_desktop_state",
        lambda: type(
            "DesktopState",
            (),
            {
                "tray_enabled": True,
                "start_at_login": False,
                "server_mode": "spawn",
            },
        )(),
    )
    controller = cast(
        DesktopController, type("Controller", (), {"status": "running"})()
    )
    return PystrayDesktopTray(controller), applied


def _token_optimizer_menu(tray):
    for item in tray._menu():
        if item.text == "Token optimizer":
            return item.submenu
    raise AssertionError("Token optimizer submenu not found")


def _agent_item(menu, agent):
    for item in menu.items:
        if item.text == agent:
            return item
    raise AssertionError(f"{agent} menu item not found")


def _toggle(tray, item):
    # ``MenuItem.__call__(icon)`` invokes the action with ``(icon, item)``,
    # matching exactly what the native backend does when the user clicks it.
    item(None)


def test_token_optimizer_submenu_presents_three_checkable_agents(monkeypatch, tmp_path):
    tray, _applied = _patched_tray(monkeypatch, tmp_path)

    submenu = _token_optimizer_menu(tray)

    # Labels come from the harness registry's display names, so the tray, the
    # exit-127 hint and the dashboard card all name the CLI the same way.
    assert [item.text for item in submenu.items] == [
        "Claude Code",
        "Codex CLI",
        "Pi",
    ]


def test_toggling_agent_persists_and_reconciles(monkeypatch, tmp_path):
    tray, applied = _patched_tray(monkeypatch, tmp_path)
    submenu = _token_optimizer_menu(tray)
    claude_item = _agent_item(submenu, "Claude Code")

    _toggle(tray, claude_item)

    assert load_rtk_state() == RtkState(claude=True, codex=False, pi=False)
    assert applied == [RtkState(claude=True, codex=False, pi=False)]


def test_toggling_does_not_disturb_other_agents(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    rtk_config.save_rtk_state(RtkState(claude=True, codex=True, pi=True))
    applied: list[RtkState] = []
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.apply_rtk_state",
        lambda state, **kwargs: applied.append(state),
    )
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.load_desktop_state",
        lambda: type(
            "DesktopState",
            (),
            {
                "tray_enabled": True,
                "start_at_login": False,
                "server_mode": "spawn",
            },
        )(),
    )
    controller = cast(
        DesktopController, type("Controller", (), {"status": "running"})()
    )
    tray = PystrayDesktopTray(controller)
    submenu = _token_optimizer_menu(tray)

    _toggle(tray, _agent_item(submenu, "Codex CLI"))

    assert load_rtk_state() == RtkState(claude=True, codex=False, pi=True)
    assert applied == [RtkState(claude=True, codex=False, pi=True)]


def test_toggle_survives_concurrent_external_write(monkeypatch, tmp_path):
    """A lost-update regression test.

    Simulates the admin HTTP API (a separate process) enabling ``codex`` on
    disk *after* the tray already cached state at construction time, then
    toggling ``claude`` through the tray. The externally-written ``codex``
    value must survive, and the tray's own cache must reflect the fresh
    disk state (not the stale snapshot) afterward.
    """

    tray, applied = _patched_tray(monkeypatch, tmp_path)

    # External writer (e.g. the admin API) changes codex after tray startup,
    # while the tray's cache still holds the all-False snapshot from init.
    rtk_config.save_rtk_state(RtkState(claude=False, codex=True, pi=False))

    submenu = _token_optimizer_menu(tray)
    _toggle(tray, _agent_item(submenu, "Claude Code"))

    assert load_rtk_state() == RtkState(claude=True, codex=True, pi=False)
    assert applied == [RtkState(claude=True, codex=True, pi=False)]
    # The in-memory cache backing the checkmarks must be refreshed too.
    assert tray._rtk_state == {"claude": True, "codex": True, "pi": False}


def test_checked_reflects_persisted_state(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    rtk_config.save_rtk_state(RtkState(claude=True, codex=False, pi=True))
    tray, _applied = _patched_tray(monkeypatch, tmp_path)
    submenu = _token_optimizer_menu(tray)

    checks = {item.text: item.checked for item in submenu.items}

    assert checks == {"Claude Code": True, "Codex CLI": False, "Pi": True}


def _real_state_tray(monkeypatch, tmp_path):
    """Build a tray against the real desktop-state file under a temp home.

    Callers must also take the ``fake_winreg`` fixture: the toggles below drive
    the *real* ``set_start_at_login``, whose Windows branch writes
    ``HKCU\\...\\Run``. Redirecting HOME isolates the state file and nothing
    else, which is how two runs of this suite deleted the developer's own
    autostart registration.
    """
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "my_claude_code.cli.desktop_tray.apply_rtk_state",
        lambda state, **kwargs: None,
    )
    controller = cast(
        DesktopController, type("Controller", (), {"status": "running"})()
    )
    return PystrayDesktopTray(controller)


def test_start_at_login_toggle_reads_disk_not_the_stale_cache(
    monkeypatch, tmp_path, fake_winreg
):
    """An externally changed field must toggle in the right direction.

    The tray caches desktop state at construction. If the dashboard enables
    "start at login" afterwards, a tray toggle computed from the stale cache
    flips False -> True and writes True, which disk already holds: the click
    appears to do nothing. Deriving the new value from disk flips it off.
    """

    tray = _real_state_tray(monkeypatch, tmp_path)
    assert load_desktop_state().start_at_login is False

    # The dashboard (another process) turns it on after the tray started.
    set_start_at_login(True)

    tray._toggle_start_at_login(None, None)

    assert load_desktop_state().start_at_login is False
    assert tray._start_at_login is False


def test_tray_enabled_toggle_reads_disk_not_the_stale_cache(
    monkeypatch, tmp_path, fake_winreg
):
    tray = _real_state_tray(monkeypatch, tmp_path)
    assert load_desktop_state().tray_enabled is True

    set_tray_enabled(False)

    tray._toggle_tray_enabled(None, None)

    assert load_desktop_state().tray_enabled is True
    assert tray._tray_enabled is True
