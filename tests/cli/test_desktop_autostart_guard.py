"""``MCC_DESKTOP_SKIP_AUTOSTART=1`` must make the reconciliation a no-op.

This guard exists because of a real incident, not a hypothetical one. The OS
autostart registration is *machine-global* -- one HKCU ``Run`` value on
Windows, one LaunchAgent on macOS, one XDG entry on Linux -- while the
preference that drives it, ``start_at_login`` in ``desktop.json``, lives
*inside whichever config directory is in force*.

Those two facts do not compose. Running ``mcc-desktop`` against a scratch
``MCC_CONFIG_DIR`` (a smoke, an installer test, a bug repro) loads a fresh
``desktop.json`` whose ``start_at_login`` is the default ``False``, and
``_reconcile_start_at_login`` then dutifully makes the OS match it -- deleting
the registration belonging to the user's *real* install. That happened during
the S5 installer work, to a real ``HKCU\\...\\Run\\MyClaudeCodeDesktop`` value.

So the switch is deliberately an environment variable and not a flag: it has
to reach a ``mcc-desktop`` child process that whoever is in the middle does not
know how to pass a flag to.
"""

import logging

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.config.desktop import DesktopState


@pytest.fixture
def recorded(monkeypatch) -> list[str]:
    """Replace both OS writers with recorders, so nothing real is touched."""

    calls: list[str] = []
    monkeypatch.setattr(
        desktop_module, "apply_start_at_login", lambda: calls.append("apply")
    )
    monkeypatch.setattr(
        desktop_module, "remove_start_at_login", lambda: calls.append("remove")
    )
    return calls


def _state(**overrides) -> DesktopState:
    return DesktopState(**overrides)


def test_the_reconciliation_writes_the_os_by_default(monkeypatch, recorded) -> None:
    """Without the variable, both directions still reach the OS."""

    monkeypatch.delenv(desktop_module.SKIP_AUTOSTART_ENV, raising=False)

    desktop_module._reconcile_start_at_login(
        _state(tray_enabled=True, start_at_login=True)
    )
    assert recorded == ["apply"]

    desktop_module._reconcile_start_at_login(
        _state(tray_enabled=True, start_at_login=False)
    )
    assert recorded == ["apply", "remove"]

    # A disabled tray never registers: an invisible tray must not relaunch at
    # login. That behaviour predates this guard and must survive it.
    desktop_module._reconcile_start_at_login(
        _state(tray_enabled=False, start_at_login=True)
    )
    assert recorded == ["apply", "remove", "remove"]


@pytest.mark.parametrize("start_at_login", [True, False])
def test_the_switch_stops_both_directions(
    monkeypatch, recorded, caplog, start_at_login: bool
) -> None:
    """Neither ``apply`` nor ``remove`` runs, and it says so once."""

    monkeypatch.setenv(desktop_module.SKIP_AUTOSTART_ENV, "1")

    with caplog.at_level(logging.INFO, logger=desktop_module.__name__):
        desktop_module._reconcile_start_at_login(
            _state(tray_enabled=True, start_at_login=start_at_login)
        )

    assert recorded == [], (
        "the OS autostart registration was written while "
        f"{desktop_module.SKIP_AUTOSTART_ENV}=1 was set"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1, f"expected exactly one log line, got {messages}"
    assert desktop_module.SKIP_AUTOSTART_ENV in messages[0]


@pytest.mark.parametrize("value", ["", " ", "0", "yes", "true", "2"])
def test_only_the_exact_value_one_disables_it(monkeypatch, recorded, value) -> None:
    """A mistyped variable must not silently disable a user's autostart.

    Fail-safe in the direction that matters: the dangerous default is
    "quietly stopped enforcing what you asked for", so anything ambiguous
    keeps the normal behaviour.
    """

    monkeypatch.setenv(desktop_module.SKIP_AUTOSTART_ENV, value)

    desktop_module._reconcile_start_at_login(
        _state(tray_enabled=True, start_at_login=True)
    )

    assert recorded == ["apply"], f"{value!r} was treated as a disable"


def test_the_switch_is_spelled_the_way_the_docs_say(monkeypatch, recorded) -> None:
    """The name is a published contract: smokes and installers set it."""

    assert desktop_module.SKIP_AUTOSTART_ENV == "MCC_DESKTOP_SKIP_AUTOSTART"

    monkeypatch.setenv("MCC_DESKTOP_SKIP_AUTOSTART", "1")
    assert desktop_module.autostart_reconcile_enabled() is False
    monkeypatch.delenv("MCC_DESKTOP_SKIP_AUTOSTART")
    assert desktop_module.autostart_reconcile_enabled() is True
