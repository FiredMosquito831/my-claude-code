"""Tests for the desktop controller: server ownership, window, health, signals."""

import sys
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.cli.desktop import (
    ActivationSignal,
    DesktopController,
    DesktopError,
    HealthTracker,
    headless_refusal_reason,
    launch_desktop,
    probe_server_presence,
)
from my_claude_code.config.desktop import (
    DesktopState,
    ServerMode,
    desktop_state_path,
    load_desktop_state,
    save_desktop_state,
)


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _controller(
    monkeypatch,
    tmp_path,
    mode: str,
    *,
    preflight_result: str | None,
) -> tuple[DesktopController, list[Any]]:
    """Build a controller whose spawn path is recorded, never actually spawned."""

    _set_home(monkeypatch, tmp_path)
    save_desktop_state(DesktopState(server_mode=cast(ServerMode, mode)))

    spawned: list[Any] = []
    monkeypatch.setattr(
        "my_claude_code.cli.desktop.preflight_proxy",
        lambda url: preflight_result,
    )
    # Never touch a real socket: the port is free unless a test says otherwise.
    monkeypatch.setattr(
        "my_claude_code.cli.desktop.probe_port_available",
        lambda host, port: True,
    )
    controller = DesktopController.__new__(DesktopController)
    object.__setattr__(
        controller, "_spawn_server", lambda settings: spawned.append(settings)
    )
    return controller, spawned


class TestEnsureServer:
    def test_spawn_mode_spawns_when_down(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result="down"
        )

        controller.ensure_server()

        assert len(spawned) == 1

    def test_spawn_mode_noop_when_already_running(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result=None
        )

        controller.ensure_server()

        assert spawned == []

    def test_attach_mode_never_spawns(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "attach", preflight_result="down"
        )

        controller.ensure_server()

        assert spawned == []

    def test_off_mode_never_spawns(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "off", preflight_result="down"
        )

        controller.ensure_server()

        assert spawned == []


class TestRestartServer:
    def test_restart_raises_outside_spawn(self, monkeypatch, tmp_path):
        controller, _spawned = _controller(
            monkeypatch, tmp_path, "attach", preflight_result="down"
        )

        with pytest.raises(DesktopError):
            controller.restart_server()

    def test_restart_raises_in_off(self, monkeypatch, tmp_path):
        controller, _spawned = _controller(
            monkeypatch, tmp_path, "off", preflight_result="down"
        )

        with pytest.raises(DesktopError):
            controller.restart_server()

    def test_restart_spawns_when_down_in_spawn(self, monkeypatch, tmp_path):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result="down"
        )

        controller.restart_server()

        assert len(spawned) == 1


class _Settings:
    host = "127.0.0.1"
    port = 8082


class _FakeWindow:
    """Records provider calls without ever showing anything."""

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.focused = 0
        self.closed = 0
        self._open = False
        self.focusable = True

    def open(self, url: str) -> None:
        self.opened.append(url)
        self._open = True

    def focus(self) -> bool:
        self.focused += 1
        return self.focusable

    def close(self) -> None:
        self.closed += 1
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open


class TestServerPresenceProbe:
    def test_healthy_when_mcc_answers(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "preflight_proxy", lambda url: None)

        assert probe_server_presence(_Settings()) == "healthy"

    def test_free_when_nothing_holds_the_port(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "preflight_proxy", lambda url: "down")
        monkeypatch.setattr(
            desktop_module, "probe_port_available", lambda host, port: True
        )

        assert probe_server_presence(_Settings()) == "free"

    def test_foreign_when_someone_else_holds_the_port(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "preflight_proxy", lambda url: "down")
        monkeypatch.setattr(
            desktop_module, "probe_port_available", lambda host, port: False
        )

        assert probe_server_presence(_Settings()) == "foreign"

    def test_spawn_refuses_a_foreign_port_and_names_the_holder(
        self, monkeypatch, tmp_path
    ):
        controller, spawned = _controller(
            monkeypatch, tmp_path, "spawn", preflight_result="down"
        )
        monkeypatch.setattr(
            desktop_module, "probe_port_available", lambda host, port: False
        )
        monkeypatch.setattr(
            desktop_module,
            "diagnose_port_owner",
            lambda host, port, **kwargs: desktop_module.PortOwner(
                pid=4321, name="nginx", command=None
            ),
        )

        with pytest.raises(DesktopError) as excinfo:
            controller.ensure_server()

        assert spawned == []
        assert "nginx" in str(excinfo.value)
        assert "4321" in str(excinfo.value)


class TestHealthTracker:
    def test_single_failure_does_not_notify(self):
        tracker = HealthTracker(threshold=3)

        assert tracker.record(False) is False

    def test_notifies_only_after_consecutive_threshold(self):
        tracker = HealthTracker(threshold=3)

        results = [tracker.record(False) for _ in range(3)]

        assert results == [False, False, True]

    def test_a_recovery_resets_the_streak(self):
        """A self-update restart flaps the probe; that must not count as death."""

        tracker = HealthTracker(threshold=3)
        tracker.record(False)
        tracker.record(False)
        tracker.record(True)

        assert [tracker.record(False) for _ in range(2)] == [False, False]

    def test_outage_is_reported_once(self):
        tracker = HealthTracker(threshold=2)
        tracker.record(False)
        tracker.record(False)

        assert tracker.record(False) is False

    def test_recovery_is_reported_after_a_notified_outage(self):
        tracker = HealthTracker(threshold=1)
        tracker.record(False)

        assert tracker.record_recovery(True) is True

    def test_recovery_is_silent_without_a_notified_outage(self):
        assert HealthTracker(threshold=1).record_recovery(True) is False


class TestActivationSignal:
    def test_signal_is_observed_once(self, tmp_path):
        signal = ActivationSignal(tmp_path / "desktop.activate")
        signal.signal()

        assert signal.poll() is True
        assert signal.poll() is False

    def test_missing_file_is_not_a_signal(self, tmp_path):
        assert ActivationSignal(tmp_path / "desktop.activate").poll() is False

    def test_stale_file_from_a_dead_instance_is_discarded_on_start(self, tmp_path):
        """A crashed instance leaves its doorbell rung; the next owner ignores it."""

        path = tmp_path / "desktop.activate"
        path.write_text("1700000000.000000", encoding="utf-8")
        signal = ActivationSignal(path)

        signal.clear()

        assert path.exists() is False
        assert signal.poll() is False

    def test_a_new_signal_after_clear_is_still_observed(self, tmp_path):
        signal = ActivationSignal(tmp_path / "desktop.activate")
        signal.clear()
        signal.signal()

        assert signal.poll() is True


class TestWindowLifecycle:
    def test_show_window_opens_when_closed(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        window = _FakeWindow()
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", window)

        controller.show_window()

        assert len(window.opened) == 1
        assert window.opened[0].endswith("/admin")

    def test_show_window_focuses_an_open_window(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        window = _FakeWindow()
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", window)
        controller.show_window()

        controller.show_window()

        assert window.focused == 1
        assert len(window.opened) == 1

    def test_unfocusable_window_is_reopened(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        window = _FakeWindow()
        window.focusable = False
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", window)
        controller.show_window()

        controller.show_window()

        assert len(window.opened) == 2

    def test_close_window_never_stops_the_server(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        window = _FakeWindow()
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", window)
        stopped: list[str] = []
        object.__setattr__(controller, "_stop_child", lambda: stopped.append("child"))

        controller.close_window()

        assert window.closed == 1
        assert stopped == []

    def test_minimize_to_tray_keeps_the_app_alive_on_close(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        save_desktop_state(DesktopState(minimize_to_tray=True, tray_enabled=True))
        controller = DesktopController.__new__(DesktopController)

        assert controller.handle_window_closed() is True

    def test_without_minimize_to_tray_closing_ends_the_app(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        save_desktop_state(DesktopState(minimize_to_tray=False))
        controller = DesktopController.__new__(DesktopController)

        assert controller.handle_window_closed() is False


class TestHeadlessRefusal:
    def test_windows_is_not_refused(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "native_origin", lambda: "windows")

        assert headless_refusal_reason() is None

    def test_wsl_names_mcc_server_and_the_windows_browser(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "native_origin", lambda: "wsl")

        reason = headless_refusal_reason()

        assert reason is not None
        assert "mcc-server" in reason
        assert "Windows" in reason

    def test_the_refusal_quotes_the_configured_port_not_the_default(self, monkeypatch):
        """A message naming :8082 on a machine serving :9000 sends the reader
        somewhere empty, so the URL has to come from settings."""

        class _Moved:
            host = "127.0.0.1"
            port = 9000

        monkeypatch.setattr(desktop_module, "native_origin", lambda: "wsl")
        monkeypatch.setattr(desktop_module, "get_settings", lambda: _Moved())

        reason = headless_refusal_reason()

        assert reason is not None
        assert "9000" in reason
        assert "8082" not in reason

    def test_linux_without_a_display_is_refused(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "native_origin", lambda: "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        reason = headless_refusal_reason()

        assert reason is not None
        assert "mcc-server" in reason

    def test_linux_with_a_display_but_no_shell_still_names_mcc_server(
        self, monkeypatch
    ):
        """Without the shell nothing on Linux can draw a tray, so it refuses."""

        monkeypatch.setattr(desktop_module, "native_origin", lambda: "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(
            desktop_module,
            "desktop_shell_unavailable_reason",
            lambda: "there is no network here.",
        )

        reason = headless_refusal_reason()

        assert reason is not None
        assert "mcc-server" in reason
        assert "there is no network here." in reason

    def test_linux_refusal_lifts_when_the_shell_is_installed(self, monkeypatch):
        """The shell carries its own tray, which is the whole reason Linux was
        refused. With it, a Linux desktop session is supported."""

        monkeypatch.setattr(desktop_module, "native_origin", lambda: "linux")
        monkeypatch.setenv("DISPLAY", ":0")
        monkeypatch.setattr(
            desktop_module, "desktop_shell_unavailable_reason", lambda: None
        )

        assert headless_refusal_reason() is None

    def test_linux_without_a_display_is_refused_even_with_the_shell(self, monkeypatch):
        """A window needs a display; the shell cannot conjure one."""

        monkeypatch.setattr(desktop_module, "native_origin", lambda: "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(
            desktop_module, "desktop_shell_unavailable_reason", lambda: None
        )

        reason = headless_refusal_reason()

        assert reason is not None
        assert "WAYLAND_DISPLAY" in reason

    def test_the_shell_reason_reports_the_documented_opt_out(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "desktop_shell_enabled", lambda: False)

        assert "DESKTOP_SHELL=off" in (
            desktop_module.desktop_shell_unavailable_reason() or ""
        )

    def test_the_shell_reason_is_none_once_it_installs(self, monkeypatch):
        monkeypatch.setattr(desktop_module, "desktop_shell_enabled", lambda: True)
        monkeypatch.setattr(desktop_module, "ensure_desktop_shell", lambda: None)

        assert desktop_module.desktop_shell_unavailable_reason() is None

    def test_entrypoint_exits_non_zero_when_refused(self, monkeypatch, capsys):
        from my_claude_code.cli import desktop_entrypoint

        monkeypatch.setattr(
            desktop_entrypoint, "headless_refusal_reason", lambda: "no display here"
        )

        with pytest.raises(SystemExit) as excinfo:
            desktop_entrypoint.launch([])

        assert excinfo.value.code == 1
        assert "no display here" in capsys.readouterr().err


class TestSingleInstance:
    def test_second_launch_signals_and_opens_nothing(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(desktop_module, "config_dir_path", lambda: tmp_path)

        class _HeldLock:
            def __init__(self, path):
                self.path = path

            def acquire(self, **_kwargs):
                return False

        monkeypatch.setattr(desktop_module, "InterprocessFileLock", _HeldLock)
        windows: list[str] = []
        monkeypatch.setattr(
            desktop_module,
            "create_window",
            lambda preference: windows.append(preference),
        )

        def _tray_factory(_controller):
            raise AssertionError("the second instance must not build a tray")

        launch_desktop(_tray_factory)

        assert windows == []
        assert ActivationSignal(tmp_path / "desktop.activate").poll() is True

    def test_signal_shows_the_running_instance_window(self, tmp_path, monkeypatch):
        # ``show_window`` persists the open state, which lands in
        # ``<config dir>/desktop.json`` -- the developer's real one without this.
        _set_home(monkeypatch, tmp_path)
        window = _FakeWindow()
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", window)
        signal = ActivationSignal(tmp_path / "desktop.activate")
        signal.clear()
        signal.signal()

        if signal.poll():
            controller.show_window()

        assert len(window.opened) == 1


class TestWindowPreferenceFlag:
    """``--window`` persists like ``--server-mode`` and shows in ``--status``."""

    def test_window_flag_persists(self, monkeypatch, tmp_path):
        from my_claude_code.cli import desktop_entrypoint

        _set_home(monkeypatch, tmp_path)

        desktop_entrypoint.launch(["--window", "browser"])

        assert load_desktop_state().window == "browser"

    def test_unknown_window_value_names_the_valid_choices(
        self, monkeypatch, tmp_path, capsys
    ):
        from my_claude_code.cli import desktop_entrypoint

        _set_home(monkeypatch, tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            desktop_entrypoint.launch(["--window", "holographic"])

        assert excinfo.value.code == 2
        assert "app-mode" in capsys.readouterr().err

    def test_status_reports_the_window_preference(self, monkeypatch, tmp_path, capsys):
        from my_claude_code.cli import desktop_entrypoint

        _set_home(monkeypatch, tmp_path)
        save_desktop_state(DesktopState(window="app-mode"))

        desktop_entrypoint.launch(["--status"])

        assert "window=app-mode" in capsys.readouterr().out

    def test_invalid_persisted_window_value_does_not_crash(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        desktop_state_path().parent.mkdir(parents=True, exist_ok=True)
        desktop_state_path().write_text('{"window": "hologram"}', encoding="utf-8")

        assert load_desktop_state().window == "auto"


class _RecordingTray:
    """Stands in for the tray adapter; records stops, can end a watch loop."""

    def __init__(self, stop_event=None) -> None:
        self.stopped = []
        self._stop_event = stop_event

    def stop(self) -> None:
        self.stopped.append(True)
        if self._stop_event is not None:
            self._stop_event.set()


class _FlippingWindow(_FakeWindow):
    """Answers ``is_open`` truthfully N times, then reports closed."""

    def __init__(self, reads_before_close: int) -> None:
        super().__init__()
        self._reads_remaining = reads_before_close

    @property
    def is_open(self) -> bool:
        if self._reads_remaining <= 0:
            return False
        self._reads_remaining -= 1
        return True


class TestLaunchReconciliation:
    """A launch applies/removes the OS registration to match desktop.json."""

    @pytest.fixture
    def launched(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(desktop_module, "config_dir_path", lambda: tmp_path)
        # A healthy server keeps ensure_server a no-op without any sockets.
        monkeypatch.setattr(desktop_module, "preflight_proxy", lambda url: None)
        applied = []
        removed = []
        monkeypatch.setattr(
            desktop_module,
            "apply_start_at_login",
            lambda: applied.append(True),
        )
        monkeypatch.setattr(
            desktop_module,
            "remove_start_at_login",
            lambda: removed.append(True),
        )
        holder: dict[str, Any] = {}

        class _InstantTray:
            def __init__(self, controller):
                self.controller = controller

            def run(self):
                pass

            def stop(self):
                holder["stopped"] = True

        def _tray_factory(controller):
            tray = _InstantTray(controller)
            holder["tray"] = tray
            return tray

        def _run(state):
            save_desktop_state(state)
            window = _FakeWindow()
            launch_desktop(_tray_factory, window_factory=lambda preference: window)
            return window

        holder["run"] = _run
        return applied, removed, holder

    def test_a_persisted_flag_is_applied_to_the_os(self, launched):
        applied, removed, holder = launched

        holder["run"](DesktopState(start_at_login=True))

        assert applied == [True]
        assert removed == []

    def test_a_disabled_tray_never_registers_start_at_login(self, launched):
        applied, removed, holder = launched

        holder["run"](DesktopState(start_at_login=True, tray_enabled=False))

        assert applied == []
        assert removed == [True]

    def test_an_off_flag_removes_any_stale_registration(self, launched):
        applied, removed, holder = launched

        holder["run"](DesktopState())

        assert applied == []
        assert removed == [True]


class TestWindowCloseWatcher:
    """Only a True->False is_open edge routes through handle_window_closed."""

    def _controller(self, monkeypatch, tmp_path, *, minimize_to_tray):
        _set_home(monkeypatch, tmp_path)
        save_desktop_state(DesktopState(minimize_to_tray=minimize_to_tray))
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", _FakeWindow())
        return controller

    def test_a_survived_close_records_no_window_and_keeps_the_tray(
        self, monkeypatch, tmp_path
    ):
        controller = self._controller(monkeypatch, tmp_path, minimize_to_tray=True)
        controller.show_window()
        controller.window.close()
        tray = _RecordingTray()

        assert desktop_module._poll_window_transition(True, controller, tray) is False

        assert tray.stopped == []
        assert load_desktop_state().window_open is False

    def test_an_app_ending_close_stops_the_tray_and_preserves_the_state(
        self, monkeypatch, tmp_path
    ):
        controller = self._controller(monkeypatch, tmp_path, minimize_to_tray=False)
        controller.show_window()
        controller.window.close()
        tray = _RecordingTray()

        assert desktop_module._poll_window_transition(True, controller, tray) is False

        assert tray.stopped == [True]
        assert load_desktop_state().window_open is True

    def test_edges_require_a_previous_open(self, monkeypatch, tmp_path):
        controller = self._controller(monkeypatch, tmp_path, minimize_to_tray=False)
        tray = _RecordingTray()

        assert desktop_module._poll_window_transition(False, controller, tray) is False

        assert tray.stopped == []
        assert load_desktop_state().window_open is True

    def test_a_steady_open_is_not_a_close(self, monkeypatch, tmp_path):
        controller = self._controller(monkeypatch, tmp_path, minimize_to_tray=False)
        controller.show_window()
        tray = _RecordingTray()

        assert desktop_module._poll_window_transition(True, controller, tray) is True

        assert tray.stopped == []

    def test_the_daemon_loop_detects_the_edge_and_ends_itself(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        save_desktop_state(DesktopState(minimize_to_tray=False))
        controller = DesktopController.__new__(DesktopController)
        object.__setattr__(controller, "_window", _FlippingWindow(reads_before_close=4))
        stop = threading.Event()
        tray = _RecordingTray(stop)

        watcher = threading.Thread(
            target=desktop_module._watch_window_close,
            args=(controller, tray, stop, 0.0),
            daemon=True,
        )
        watcher.start()
        watcher.join(timeout=5)

        assert not watcher.is_alive()
        assert tray.stopped == [True]


class TestFatalErrorSurfacing:
    # The surfaced channels are win32-only (MessageBoxW via ctypes.windll;
    # pystray tray import) -- Linux CI has neither.
    pytestmark = pytest.mark.skipif(
        sys.platform != "win32",
        reason="fatal-error surfacing targets win32 MessageBoxW/tray",
    )

    """GUI-subsystem failures must reach the user without a console."""

    @staticmethod
    def _entrypoint():
        from my_claude_code.cli import desktop_entrypoint

        return desktop_entrypoint

    def test_windows_surfaces_via_message_box_and_stderr(self, monkeypatch, capsys):
        entrypoint = self._entrypoint()
        boxes = []

        class _FakeUser32:
            @staticmethod
            def MessageBoxW(_hwnd, message, title, _flags):
                boxes.append((message, title))
                return 0

        class _FakeWindll:
            user32 = _FakeUser32()

        monkeypatch.setattr(entrypoint.ctypes, "windll", _FakeWindll())
        monkeypatch.setattr(sys, "platform", "win32")

        entrypoint._report_fatal_error("Port 8082 is held.")

        assert boxes == [("Port 8082 is held.", "My Claude Code")]
        assert "Port 8082 is held." in capsys.readouterr().err

    def test_other_platforms_fall_back_to_stderr_only(self, monkeypatch, capsys):
        entrypoint = self._entrypoint()
        monkeypatch.setattr(sys, "platform", "linux")

        entrypoint._report_fatal_error("no display here")

        err = capsys.readouterr().err
        assert "no display here" in err

    def test_launch_reports_desktop_errors_and_exits_non_zero(
        self, monkeypatch, tmp_path
    ):
        entrypoint = self._entrypoint()
        import my_claude_code.cli.desktop_tray as tray_module

        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(entrypoint, "headless_refusal_reason", lambda: None)

        def _boom():
            raise DesktopError("port held by another program")

        monkeypatch.setattr(tray_module, "launch", _boom)
        reported = []
        monkeypatch.setattr(entrypoint, "_report_fatal_error", reported.append)

        with pytest.raises(SystemExit) as excinfo:
            entrypoint.launch([])

        assert excinfo.value.code == 1
        assert reported == ["port held by another program"]
