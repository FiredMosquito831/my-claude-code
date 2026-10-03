"""``mcc-desktop`` finds the server dead, says so once, and says it in the right place.

Until 7.70.0 the window-only host every Windows machine with the desktop app
runs had no ``notify``, and ``_tray_notifier`` turned every outage message into
a no-op -- the 7-hour outage of 2026-10-01 reached nobody. These tests drive
the real monitor tick by tick (``DesktopController.health_tick``) against
faked OS answers, and the real notifier against each place a sentence can go.
"""

import io
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.cli.desktop import (
    DesktopController,
    WindowOnlyHost,
    _tray_notifier,
    desktop_app_is_running,
    server_watch_for,
)
from my_claude_code.cli.desktop_window import ShellWindow
from my_claude_code.cli.launchers.common import PreflightResult
from my_claude_code.config.settings import Settings
from my_claude_code.core.interprocess_lock import InterprocessFileLock
from my_claude_code.core.server_pid import SERVER_PID_HEADER
from my_claude_code.core.server_watch import ServerWatch


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


def _settings() -> Settings:
    return Settings.model_construct(
        host="127.0.0.1",
        port=18777,
        desktop_health_poll_seconds=30.0,
        desktop_health_failure_threshold=3,
        desktop_tick_seconds=10.0,
        desktop_reconnect_restatus_seconds=30.0,
    )


def _controller(tmp_path: Path, window=None) -> DesktopController:
    return DesktopController(
        lock=InterprocessFileLock(tmp_path / "desktop.lock"), window=window
    )


class _RunningProcess:
    pid = 31337

    def poll(self):
        return None


# ------------------------------------------------------------------ cadence


def test_the_shipped_cadence_is_thirty_seconds_healthy_and_five_failing() -> None:
    from my_claude_code.config.constants import (
        DESKTOP_HEALTH_POLL_SECONDS_DEFAULT,
        DESKTOP_HEALTH_RETRY_SECONDS,
    )

    assert DESKTOP_HEALTH_POLL_SECONDS_DEFAULT == 30.0
    assert DESKTOP_HEALTH_RETRY_SECONDS == 5.0
    assert Settings.model_validate({}).desktop_health_poll_seconds == 30.0

    watch = server_watch_for(Settings.model_validate({}))
    watch.record_probe(True)
    assert watch.next_interval() == 30.0
    watch.record_probe(False)
    # The failing cadence is the 5 s every probe ran at before 7.70.0.
    assert watch.next_interval() == 5.0


# ------------------------------------------------------------ the monitor


def _drive(
    monkeypatch,
    tmp_path: Path,
    *,
    answers: list[PreflightResult],
    port_free: bool,
    alive: bool | None,
    ticks: int,
) -> tuple[list[str], list[str]]:
    """Run ``ticks`` probes; the last answer repeats. Return (dead, recovered)."""

    queue = list(answers)

    def preflight(_url: str) -> PreflightResult:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(desktop_module, "preflight_result", preflight)
    monkeypatch.setattr(
        desktop_module, "probe_port_available", lambda *a, **k: port_free
    )
    monkeypatch.setattr(desktop_module, "active_update", lambda: None)
    monkeypatch.setattr(
        desktop_module,
        "process_is_alive",
        lambda pid: True if alive is None else alive,
    )
    monkeypatch.setattr(desktop_module, "diagnose_port_owner", lambda *a, **k: None)
    clock = Clock()
    settings = _settings()
    watch = ServerWatch(
        threshold=3,
        healthy_interval=30.0,
        failing_interval=5.0,
        confirm_seconds=30.0,
        recheck_seconds=30.0,
        clock=clock,
    )
    controller = _controller(tmp_path)
    dead: list[str] = []
    recovered: list[str] = []
    for _ in range(ticks):
        controller.health_tick(watch, settings, dead.append, recovered.append)
        clock.now += watch.next_interval()
    return dead, recovered


HEALTHY = PreflightResult(status_code=200, headers={SERVER_PID_HEADER: "4242"})
DOWN = PreflightResult(error="connection refused")


def test_a_gone_server_is_announced_once_not_once_per_tick(
    monkeypatch, tmp_path
) -> None:
    dead, recovered = _drive(
        monkeypatch,
        tmp_path,
        answers=[HEALTHY, DOWN],
        port_free=True,
        alive=False,
        ticks=300,
    )

    assert len(dead) == 1
    assert "process 4242 has exited" in dead[0]
    assert "port 18777" in dead[0]
    assert recovered == []


def test_a_server_that_holds_its_port_is_never_announced(monkeypatch, tmp_path) -> None:
    """Slow, for 300 ticks: the OS says the port is held, so nothing is said."""

    import my_claude_code.cli.port_takeover as takeover
    from my_claude_code.cli.port_takeover import ProcessIdentity

    monkeypatch.setattr(
        takeover,
        "identity_for_owner",
        lambda *a, **k: ProcessIdentity(
            pid=4242, image="python.exe", command="mcc-server"
        ),
    )
    dead, _ = _drive(
        monkeypatch,
        tmp_path,
        answers=[HEALTHY, DOWN],
        port_free=False,
        alive=True,
        ticks=300,
    )

    assert dead == []


def test_a_listener_loss_is_announced_after_its_confirmation(
    monkeypatch, tmp_path
) -> None:
    dead, _ = _drive(
        monkeypatch,
        tmp_path,
        answers=[HEALTHY, DOWN],
        port_free=True,
        alive=True,
        ticks=40,
    )

    assert len(dead) == 1
    assert "process 4242 is still running but no longer holds the port" in dead[0]


def test_recovery_follows_an_announced_death(monkeypatch, tmp_path) -> None:
    dead, recovered = _drive(
        monkeypatch,
        tmp_path,
        answers=[HEALTHY, DOWN, DOWN, DOWN, DOWN, HEALTHY],
        port_free=True,
        alive=False,
        ticks=8,
    )

    assert len(dead) == 1
    assert recovered == ["The My Claude Code server on port 18777 is answering again."]


# ------------------------------------------------------------ the notifier


@pytest.fixture
def server_log(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "logs" / "server.log"
    monkeypatch.setenv("LOG_FILE", str(path))
    return path


def _logged(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _Tray:
    def __init__(self) -> None:
        self.shown: list[str] = []

    def notify(self, message: str) -> None:
        self.shown.append(message)


def test_the_hosts_own_tray_shows_it_and_the_log_records_it(
    server_log, tmp_path, monkeypatch
) -> None:
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    tray = _Tray()

    _tray_notifier(tray, _controller(tmp_path))("the server is gone")

    assert tray.shown == ["the server is gone"]
    assert console.getvalue() == ""
    [line] = _logged(server_log)
    assert line["message"] == "the server is gone"
    assert line["level"] == "WARNING"


def test_while_the_desktop_app_runs_nothing_is_shown_by_anyone_else(
    server_log, tmp_path, monkeypatch
) -> None:
    """Never a toast under another program's name while the app runs."""

    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    shell = ShellWindow(tmp_path / "MyClaudeCode.exe")
    # A running app, without running one: the shell child's ``poll()`` is None.
    cast(Any, shell)._process = _RunningProcess()
    controller = _controller(tmp_path, window=shell)
    host = WindowOnlyHost(controller)
    assert not callable(getattr(host, "notify", None))
    assert desktop_app_is_running(controller) is True

    _tray_notifier(host, controller)("the server is gone")

    assert console.getvalue() == ""
    [line] = _logged(server_log)
    assert line["message"] == "the server is gone"


def test_without_the_app_the_console_gets_exactly_one_stamped_line(
    server_log, tmp_path, monkeypatch
) -> None:
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)
    controller = _controller(tmp_path)
    assert desktop_app_is_running(controller) is False

    _tray_notifier(WindowOnlyHost(controller), controller)("the server is gone")

    lines = console.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("[")
    assert lines[0].endswith("] My Claude Code: the server is gone")
    assert len(_logged(server_log)) == 1


def test_a_windowless_host_writes_only_the_log(
    server_log, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    controller = _controller(tmp_path)

    _tray_notifier(WindowOnlyHost(controller), controller)("the server is gone")

    assert len(_logged(server_log)) == 1


def test_a_closed_app_is_not_running(tmp_path) -> None:
    class _Exited:
        pid = 1

        def poll(self):
            return 0

    shell = ShellWindow(tmp_path / "MyClaudeCode.exe")
    cast(Any, shell)._process = _Exited()
    assert desktop_app_is_running(_controller(tmp_path, window=shell)) is False
    assert desktop_app_is_running(None) is False


def test_a_notifier_never_raises_when_the_log_cannot_be_written(
    tmp_path, monkeypatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LOG_FILE", str(blocker / "server.log"))
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    _tray_notifier(WindowOnlyHost(_controller(tmp_path)), None)("still fine")
