"""The supervisor, the tray and the updater all stop escalating at the bound.

Three processes wait on a stopping server, and before 6.41.0 each of them got
the wait wrong in its own way: the supervisor's overrun branches could never
run at all (control never came back from ``server.run()``), the tray waited a
hard-coded 5s and then used ``TerminateProcess``, and the update helper waited
a flat hour on a parent that was never going to exit. They now share one rule:
ask, wait the server's own configured budget, then end it by the exact pid.
"""

import subprocess
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.core.stop_deadline import (
    HARD_EXIT_GRACE_SECONDS,
    STOP_TEARDOWN_MARGIN_SECONDS,
    stop_deadline,
)

BOUND = 3.0


def _settings() -> Settings:
    return Settings.model_construct(
        host="0.0.0.0",
        port=8082,
        anthropic_auth_token="freecc",
        model="nvidia_nim/test-model",
        open_admin_browser=False,
        server_graceful_shutdown_seconds=BOUND,
    )


def _run_supervisor(action_name: str, *, runtime_closed: bool):
    """Drive one supervised generation whose drain ends in ``action_name``."""

    from my_claude_code.cli import commands

    settings = _settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    callbacks: dict[str, Callable[[], None]] = {}

    def build_asgi_app(_settings, restart_callback, process_restart_callback):
        callbacks["reload"] = restart_callback
        callbacks["replace"] = process_restart_callback
        return SimpleNamespace(runtime=SimpleNamespace(is_closed=runtime_closed))

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            # What the runtime does when a config apply or an installed update
            # asks for a restart: the callback runs, the stop clock starts, and
            # the drain then overruns (runtime_closed=False).
            callbacks[action_name]()

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.uvicorn, "Config", side_effect=lambda app, **kw: kw | {"app": app}
        ),
        patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
        patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        yield_value = commands._run_supervised_server(
            settings, open_admin_browser=False
        )
    return yield_value


# --------------------------------------------------------------------------
# T6 / T7 -- the overrun branches the author already wrote become reachable.
# --------------------------------------------------------------------------


def test_replace_process_refusal_is_reachable_when_the_drain_overruns() -> None:
    """A self-update must not execv into the new image over a live generation.

    The refusal has been in ``cli/commands.py`` since it was written and could
    never execute: control never returned from ``server.run()`` when the drain
    did not complete, which is the whole bug. Now it does.
    """

    with pytest.raises(SystemExit) as exit_info:
        _run_supervisor("replace", runtime_closed=False)
    assert exit_info.value.code == 1


def test_reload_after_an_overrun_drain_keeps_the_server_up() -> None:
    """ "The server crashed after I applied a setting" was a degraded RELOAD."""

    from my_claude_code.cli import commands

    action = _run_supervisor("reload", runtime_closed=False)
    assert action is commands.ServerExitAction.RELOAD


def test_a_reload_leaves_the_next_generation_able_to_serve() -> None:
    """A stop that ends in RELOAD must not leave the gate shut.

    The ASGI gate and ``ProviderRuntimeManager.acquire`` both read the process
    stop deadline, so a generation that inherited a stopped one would refuse
    every request with 503 forever -- a far worse outcome than the hang.
    """

    _run_supervisor("reload", runtime_closed=False)
    assert stop_deadline().requested is False


def test_a_completed_replace_process_drain_still_replaces_the_process() -> None:
    """The bound changes what happens on an OVERRUN, nothing else."""

    from my_claude_code.cli import commands

    action = _run_supervisor("replace", runtime_closed=True)
    assert action is commands.ServerExitAction.REPLACE_PROCESS
    assert stop_deadline().requested is False


def test_the_supervisor_arms_no_watchdog_for_a_reload() -> None:
    """C8 depends on it: a RELOAD's overrun must keep the process alive."""

    from my_claude_code.cli import commands

    settings = _settings()
    armed: list[float] = []
    deadline = stop_deadline()

    with patch.object(
        type(deadline), "arm_hard_exit", lambda self, **kw: armed.append(1.0)
    ):
        get_settings = MagicMock(return_value=settings)
        get_settings.cache_clear = MagicMock()
        callbacks: dict[str, Callable[[], None]] = {}

        def build_asgi_app(_s, restart_callback, process_restart_callback):
            callbacks["reload"] = restart_callback
            return SimpleNamespace(runtime=SimpleNamespace(is_closed=True))

        class FakeServer:
            def __init__(self, config):
                self.config = config
                self.should_exit = False

            def run(self):
                callbacks["reload"]()

        with (
            patch.object(commands, "get_settings", get_settings),
            patch.object(
                commands.uvicorn,
                "Config",
                side_effect=lambda app, **kw: kw | {"app": app},
            ),
            patch.object(commands.uvicorn, "Server", side_effect=FakeServer),
            patch.object(commands, "build_asgi_app", side_effect=build_asgi_app),
            patch.object(commands, "_schedule_open_admin_browser"),
            patch.object(commands, "kill_all_best_effort"),
            patch.object(commands, "probe_port_available", return_value=True),
            patch.object(commands, "wait_for_port_free", return_value=True),
        ):
            commands._run_supervised_server(settings, open_admin_browser=False)

    assert armed == []


def test_a_signal_stop_starts_the_stop_clock() -> None:
    """Ctrl+C, SIGTERM and the tray's CTRL_BREAK are the commonest stops.

    uvicorn owns the signal handlers -- MCC installs none of its own -- and
    they set ``should_exit`` directly without going through the supervisor's
    ``request()``. Arming the shared deadline only on the in-process RELOAD and
    REPLACE_PROCESS paths would have left every bound in this release inert in
    exactly the case the user reported.
    """

    from my_claude_code.cli import commands

    settings = _settings()
    get_settings = MagicMock(return_value=settings)
    get_settings.cache_clear = MagicMock()
    observed: dict[str, object] = {}

    class SignalledServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def handle_exit(self, sig, frame):
            observed["uvicorn_handler_ran"] = True
            self.should_exit = True

        def run(self):
            self.handle_exit(2, None)
            observed["requested_during_run"] = stop_deadline().requested
            observed["budget"] = stop_deadline().budget

    with (
        patch.object(commands, "get_settings", get_settings),
        patch.object(
            commands.uvicorn, "Config", side_effect=lambda app, **kw: kw | {"app": app}
        ),
        patch.object(commands.uvicorn, "Server", side_effect=SignalledServer),
        patch.object(
            commands,
            "build_asgi_app",
            side_effect=lambda *_a, **_k: SimpleNamespace(
                runtime=SimpleNamespace(is_closed=True)
            ),
        ),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "probe_port_available", return_value=True),
        patch.object(commands, "wait_for_port_free", return_value=True),
    ):
        action = commands._run_supervised_server(settings, open_admin_browser=False)

    assert action is commands.ServerExitAction.STOP
    assert observed["uvicorn_handler_ran"] is True, (
        "uvicorn's own handler must still run"
    )
    assert observed["requested_during_run"] is True
    assert observed["budget"] == BOUND


# --------------------------------------------------------------------------
# T8 -- the tray.
# --------------------------------------------------------------------------


class _StubbornChild:
    """A server child that never exits on its own."""

    def __init__(self, *, dies_on: str | None = None) -> None:
        self.pid = 4242
        self.dies_on = dies_on
        self.signals: list[object] = []
        self.calls: list[str] = []
        self.waits: list[float] = []
        self._dead = False

    def poll(self) -> int | None:
        return 0 if self._dead else None

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(float(timeout or 0.0))
        if self._dead:
            return 0
        raise subprocess.TimeoutExpired(cmd="mcc-server", timeout=timeout or 0.0)

    def send_signal(self, sig: object) -> None:
        self.signals.append(sig)
        if self.dies_on == "signal":
            self._dead = True

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.dies_on == "terminate":
            self._dead = True

    def kill(self) -> None:
        self.calls.append("kill")
        self._dead = True


def _controller(child: _StubbornChild):
    from my_claude_code.cli import desktop

    controller = desktop.DesktopController.__new__(desktop.DesktopController)
    controller._process = child
    return controller


def test_tray_restart_completes_when_the_old_server_is_draining(monkeypatch) -> None:
    """The tray asks, waits the server's own budget, then ends the exact pid.

    Before this it did none of those three: no graceful ask at all (on Windows
    ``terminate()`` is ``TerminateProcess``), a hard-coded 5s wait that killed a
    server legitimately mid-drain under a 300s budget, and no escalation past
    ``kill``. The composite defect is why "restart" from the tray was
    unreliable in both directions at once.
    """

    from my_claude_code.cli import desktop

    monkeypatch.setattr(desktop, "get_settings", _settings)
    child = _StubbornChild()
    controller = _controller(child)
    controller._stop_child()
    waits = child.waits

    # It asked first...
    assert child.signals, "the tray must ask the server to stop before killing it"
    # ...waited the server's whole configured stop budget, not five seconds...
    assert waits[0] == pytest.approx(
        BOUND + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS
    )
    # ...then escalated, in order, and stopped waiting.
    assert child.calls == ["terminate", "kill"]
    assert controller._process is None


def test_the_tray_does_not_kill_a_child_that_stopped_when_asked(monkeypatch) -> None:
    """A cooperative server is never terminated; that is the point of asking."""

    from my_claude_code.cli import desktop

    monkeypatch.setattr(desktop, "get_settings", _settings)
    child = _StubbornChild(dies_on="signal")
    _controller(child)._stop_child()

    assert child.calls == []


def test_the_tray_waits_the_configured_budget_not_a_constant() -> None:
    """The number the operator set is the number the tray honours."""

    from my_claude_code.cli.desktop import server_stop_wait_seconds

    fast = Settings.model_construct(server_graceful_shutdown_seconds=5.0)
    slow = Settings.model_construct(server_graceful_shutdown_seconds=120.0)
    assert server_stop_wait_seconds(fast) < server_stop_wait_seconds(slow)
    assert server_stop_wait_seconds(fast) == pytest.approx(
        5.0 + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS
    )
    # A misconfigured or absent value still yields a usable wait.
    assert server_stop_wait_seconds(Settings.model_construct()) > 0.0


# --------------------------------------------------------------------------
# T9 -- the updater's deferred helper.
# --------------------------------------------------------------------------


def test_update_deferred_helper_completes_within_its_bound(tmp_path) -> None:
    """The helper stops waiting at the bound and ends the parent it was given.

    The user's report -- "deferred helpers that sat for hours" -- is this script
    polling a parent pid for a flat 3600s, then writing a failure receipt and
    exiting without installing anything. The parent could not exit, so the hour
    was always spent and the update was never applied.
    """

    from my_claude_code.application import release_updates

    script = release_updates._deferred_helper_script(
        uv_executable="uv.exe",
        command=["uv.exe", "tool", "install", "my-claude-code"],
        result_path=tmp_path / "pending-upgrade.json",
        stage_dir=tmp_path,
        server_launcher=tmp_path / "fcc-server.exe",
        working_directory=tmp_path,
        commands=["mcc-server"],
        wait_seconds=BOUND + STOP_TEARDOWN_MARGIN_SECONDS,
    )

    assert "AddSeconds(3600)" not in script, "the flat hour is the bug"
    assert f"AddSeconds({BOUND + STOP_TEARDOWN_MARGIN_SECONDS:.1f})" in script
    # It escalates to the exact pid whose identity Test-ParentAlive pinned by
    # creation time, and only then gives up.
    assert "Stop-Process -Id $parent -Force" in script
    stop_at = script.index("Stop-Process -Id $parent -Force")
    install_at = script.index("tool")
    assert stop_at < install_at, "the helper must install after it clears the parent"
    # And the receipt it writes when even that fails says what actually
    # happened, instead of "timed out waiting".
    assert "could not be stopped" in script


def test_the_deferred_helper_bound_follows_the_configured_budget(monkeypatch) -> None:
    """One setting, honoured by the server, the tray and the updater alike."""

    from my_claude_code.application import release_updates

    monkeypatch.setattr(
        release_updates,
        "get_settings",
        lambda: Settings.model_construct(server_graceful_shutdown_seconds=400.0),
    )
    assert release_updates._helper_wait_seconds() == pytest.approx(
        400.0 + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS
    )

    monkeypatch.setattr(
        release_updates,
        "get_settings",
        lambda: Settings.model_construct(server_graceful_shutdown_seconds=1.0),
    )
    # Never so short that a healthy handoff loses the race to its own helper.
    assert release_updates._helper_wait_seconds() >= 30.0


def test_the_installer_no_longer_waits_six_hours() -> None:
    """Two bounds for the same wait (3600s in-app, 6h in the installer) was
    itself the inconsistency; the installer now escalates like everything else.
    """

    from pathlib import Path

    installer = (
        Path(__file__).resolve().parents[2] / "scripts" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "AddHours(6)" not in installer
    assert "did not stop within 6 hours" not in installer
    assert "$DeferredWaitSeconds = 600" in installer
    assert "Stop-Process -Id `$target.Id -Force" in installer
