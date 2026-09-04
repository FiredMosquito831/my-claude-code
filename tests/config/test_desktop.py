"""Tests for config/desktop.py and the admin desktop endpoints."""

import json
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config import desktop as desktop_config
from my_claude_code.config.desktop import (
    LAUNCH_AGENT_LABEL,
    LINUX_AUTOSTART_ID,
    LINUX_SYSTEMD_UNIT,
    WINDOWS_RUN_VALUE,
    DesktopState,
    DesktopStateError,
    chromium_binary,
    load_desktop_state,
    save_desktop_state,
)
from my_claude_code.config.paths import config_dir_path
from tests.api.support import create_test_app


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


class TestLoadDesktopState:
    def test_missing_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        state = load_desktop_state()

        assert state.tray_enabled is True
        assert state.start_at_login is False
        assert state.minimize_to_tray is False
        assert state.server_mode == "spawn"

    def test_corrupt_file_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{", encoding="utf-8")

        state = load_desktop_state()

        assert state.tray_enabled is True
        assert state.start_at_login is False
        assert state.server_mode == "spawn"

    def test_non_dict_json_returns_defaults(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")

        state = load_desktop_state()

        assert state.server_mode == "spawn"

    def test_unknown_keys_are_ignored(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tray_enabled": False, "future_key": "x", "bogus": 5}),
            encoding="utf-8",
        )

        state = load_desktop_state()

        assert state.tray_enabled is False
        assert state.start_at_login is False

    def test_non_boolean_value_falls_back_to_default(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tray_enabled": "yes", "start_at_login": 1}),
            encoding="utf-8",
        )

        state = load_desktop_state()

        assert state.tray_enabled is True
        assert state.start_at_login is False

    def test_server_mode_round_trips(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"server_mode": "attach"}), encoding="utf-8")

        assert load_desktop_state().server_mode == "attach"

    def test_invalid_server_mode_falls_back_to_default(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"server_mode": "nope"}), encoding="utf-8")

        assert load_desktop_state().server_mode == "spawn"

    def test_legacy_auto_start_true_migrates_to_spawn(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"server_auto_start": True}), encoding="utf-8")

        assert load_desktop_state().server_mode == "spawn"

    def test_legacy_auto_start_false_migrates_to_attach(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"server_auto_start": False}), encoding="utf-8")

        assert load_desktop_state().server_mode == "attach"

    def test_server_mode_wins_over_legacy_boolean(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"server_mode": "off", "server_auto_start": True}),
            encoding="utf-8",
        )

        assert load_desktop_state().server_mode == "off"

    def test_window_open_defaults_true_when_absent(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({}), encoding="utf-8")

        assert load_desktop_state().window_open is True

    def test_window_open_round_trips_false(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"window_open": False}), encoding="utf-8")

        assert load_desktop_state().window_open is False

    def test_last_applied_window_size_round_trips(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"last_applied_window_width": 1600, "last_applied_window_height": 1000}
            ),
            encoding="utf-8",
        )

        state = load_desktop_state()
        assert state.last_applied_window_width == 1600
        assert state.last_applied_window_height == 1000

    def test_corrupt_last_applied_window_size_falls_back_to_none(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        path = desktop_config.desktop_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "last_applied_window_width": "wide",
                    "last_applied_window_height": None,
                }
            ),
            encoding="utf-8",
        )

        state = load_desktop_state()
        assert state.last_applied_window_width is None
        assert state.last_applied_window_height is None


class TestSessionRestoreRoundTrip:
    """`window_open` round-trips through set_window_open -> save -> load."""

    def test_set_window_open_persists_and_loads(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        from my_claude_code.config.desktop import set_window_open

        assert load_desktop_state().window_open is True

        set_window_open(False)
        assert load_desktop_state().window_open is False

        set_window_open(True)
        assert load_desktop_state().window_open is True

    def test_record_applied_window_size_persists_and_loads(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        from my_claude_code.config.desktop import record_applied_window_size

        record_applied_window_size(1500, 950)
        state = load_desktop_state()
        assert state.last_applied_window_width == 1500
        assert state.last_applied_window_height == 950


class TestSaveDesktopState:
    def test_round_trip(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_desktop_state(
            DesktopState(
                tray_enabled=False,
                start_at_login=True,
                minimize_to_tray=True,
                server_mode="attach",
            )
        )
        state = load_desktop_state()

        assert state.tray_enabled is False
        assert state.start_at_login is True
        assert state.minimize_to_tray is True
        assert state.server_mode == "attach"

    def test_save_writes_server_mode_not_legacy_key(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_desktop_state(load_desktop_state())

        persisted = json.loads(desktop_config.desktop_state_path().read_text())
        assert persisted["server_mode"] == "spawn"
        assert "server_auto_start" not in persisted

    def test_creates_parent_dirs(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        assert not config_dir_path().exists()

        save_desktop_state(load_desktop_state())

        assert desktop_config.desktop_state_path().is_file()

    def test_atomic_tmp_file_does_not_linger(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        save_desktop_state(load_desktop_state())

        tmp_path_candidate = desktop_config.desktop_state_path().with_suffix(
            ".json.tmp"
        )
        assert not tmp_path_candidate.exists()

    def test_write_failure_raises_desktop_state_error(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        blocker = config_dir_path()
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("blocked", encoding="utf-8")

        with pytest.raises(DesktopStateError):
            save_desktop_state(load_desktop_state())


class TestWindowsStartAtLogin:
    def test_apply_writes_run_key(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)

        desktop_config.apply_start_at_login("tray")

        assert WINDOWS_RUN_VALUE in fake_winreg.values
        assert (
            "my_claude_code.cli.desktop_entrypoint"
            in fake_winreg.values[WINDOWS_RUN_VALUE]
        )

    def test_apply_uses_tray_target_by_default(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        _set_home(monkeypatch, tmp_path)

        desktop_config.apply_start_at_login()

        assert (
            "my_claude_code.cli.desktop_entrypoint"
            in fake_winreg.values[WINDOWS_RUN_VALUE]
        )

    def test_remove_deletes_run_key(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)
        fake_winreg.values[WINDOWS_RUN_VALUE] = "whatever"

        desktop_config.remove_start_at_login("tray")

        assert WINDOWS_RUN_VALUE not in fake_winreg.values
        assert fake_winreg.closed is True

    def test_remove_missing_run_key_is_quiet(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)

        desktop_config.remove_start_at_login("tray")

        assert WINDOWS_RUN_VALUE not in fake_winreg.values


class TestMacOSStartAtLogin:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(desktop_config, "native_origin", lambda: "macos")

    def test_apply_writes_launch_agent_plist(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        desktop_config.apply_start_at_login("tray")

        path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        content = path.read_text(encoding="utf-8")
        assert path.is_file()
        assert "<key>RunAtLoad</key>" in content
        assert "<true/>" in content
        assert LAUNCH_AGENT_LABEL in content
        assert "my_claude_code.cli.desktop_entrypoint" in content

    def test_remove_deletes_launch_agent_plist(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("present", encoding="utf-8")

        desktop_config.remove_start_at_login("tray")

        assert not path.exists()


class TestLinuxStartAtLogin:
    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(desktop_config, "native_origin", lambda: "linux")

    def test_apply_writes_autostart_desktop_file_without_systemd(
        self, monkeypatch, tmp_path
    ):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(desktop_config, "_systemd_user_available", lambda: False)

        desktop_config.apply_start_at_login("server")

        path = Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"
        content = path.read_text(encoding="utf-8")
        assert path.is_file()
        assert "[Desktop Entry]" in content
        assert "X-GNOME-Autostart-enabled=true" in content
        assert "my_claude_code.cli.entrypoints" in content

    def test_apply_writes_systemd_unit_when_available(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(desktop_config, "_systemd_user_available", lambda: True)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            desktop_config.subprocess,
            "run",
            lambda *args, **kwargs: calls.append(list(args[0])),
        )
        monkeypatch.setattr(
            desktop_config.shutil, "which", lambda name: "/usr/bin/systemctl"
        )

        desktop_config.apply_start_at_login("server")

        unit = Path.home() / ".config" / "systemd" / "user" / LINUX_SYSTEMD_UNIT
        content = unit.read_text(encoding="utf-8")
        assert unit.is_file()
        assert "ExecStart=" in content
        assert "my_claude_code.cli.entrypoints" in content
        assert not (
            Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"
        ).exists()
        assert any("enable" in call for call in calls)
        assert any("daemon-reload" in call for call in calls)

    def test_tray_target_rejected_on_linux(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(desktop_config, "_systemd_user_available", lambda: False)

        with pytest.raises(DesktopStateError):
            desktop_config.apply_start_at_login("tray")

    def test_remove_clears_both_linux_artifacts(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(desktop_config.shutil, "which", lambda name: None)
        unit = Path.home() / ".config" / "systemd" / "user" / LINUX_SYSTEMD_UNIT
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("present", encoding="utf-8")
        autostart = (
            Path.home() / ".config" / "autostart" / f"{LINUX_AUTOSTART_ID}.desktop"
        )
        autostart.parent.mkdir(parents=True, exist_ok=True)
        autostart.write_text("present", encoding="utf-8")

        desktop_config.remove_start_at_login("server")

        assert not unit.exists()
        assert not autostart.exists()


class TestAutostartTargets:
    def test_default_target_is_tray_on_windows(self, monkeypatch):
        monkeypatch.setattr(desktop_config, "native_origin", lambda: "windows")
        assert desktop_config.default_autostart_target() == "tray"

    def test_default_target_is_tray_on_macos(self, monkeypatch):
        monkeypatch.setattr(desktop_config, "native_origin", lambda: "macos")
        assert desktop_config.default_autostart_target() == "tray"

    def test_default_target_is_server_on_linux(self, monkeypatch):
        monkeypatch.setattr(desktop_config, "native_origin", lambda: "linux")
        assert desktop_config.default_autostart_target() == "server"

    def test_default_target_is_server_on_wsl(self, monkeypatch):
        monkeypatch.setattr(desktop_config, "native_origin", lambda: "wsl")
        assert desktop_config.default_autostart_target() == "server"


class TestServerModeHelpers:
    def test_set_server_mode_persists(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        desktop_config.set_server_mode("off")

        assert load_desktop_state().server_mode == "off"

    def test_set_server_mode_rejects_unknown(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)

        with pytest.raises(ValueError):
            desktop_config.set_server_mode("bogus")


class TestChromiumSearch:
    def _which(self, monkeypatch, found: dict[str, str]):
        monkeypatch.setattr(desktop_config, "which", lambda name: found.get(name))
        monkeypatch.setattr(Path, "is_file", lambda self: False)

    def test_windows_prefers_edge(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "win32")
        self._which(monkeypatch, {"msedge": "E:/msedge.exe", "chrome": "C:/chrome.exe"})

        assert chromium_binary() == "E:/msedge.exe"

    def test_windows_falls_back_to_chrome_then_brave(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "win32")
        self._which(monkeypatch, {"brave": "B:/brave.exe"})

        assert chromium_binary() == "B:/brave.exe"

    def test_macos_uses_known_application_paths(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "darwin")
        monkeypatch.setattr(desktop_config, "which", lambda name: None)
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        monkeypatch.setattr(Path, "is_file", lambda self: self.as_posix() == chrome)

        assert chromium_binary() == chrome

    def test_linux_search_order(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "linux")
        self._which(
            monkeypatch, {"chromium": "/usr/bin/chromium", "brave-browser": "/b"}
        )

        assert chromium_binary() == "/usr/bin/chromium"

    def test_no_browser_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "linux")
        self._which(monkeypatch, {})

        assert chromium_binary() is None


class _FakeSettings:
    def __init__(self, desktop_browser_path: str = "") -> None:
        self.desktop_browser_path = desktop_browser_path


class TestConfiguredBrowserPath:
    """DESKTOP_BROWSER_PATH must win over the candidate search, and must
    degrade -- not fail -- when it points at nothing."""

    def test_configured_path_takes_precedence_over_search(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "linux")
        monkeypatch.setattr(
            desktop_config,
            "which",
            lambda name: {"chromium": "/usr/bin/chromium"}.get(name),
        )
        monkeypatch.setattr(
            desktop_config,
            "get_settings",
            lambda: _FakeSettings("/opt/my-browser/browser"),
        )
        monkeypatch.setattr(Path, "is_file", lambda self: True)

        assert chromium_binary() == "/opt/my-browser/browser"

    def test_missing_configured_path_falls_through_to_search(self, monkeypatch, caplog):
        monkeypatch.setattr(desktop_config.sys, "platform", "linux")
        monkeypatch.setattr(
            desktop_config,
            "which",
            lambda name: {"chromium": "/usr/bin/chromium"}.get(name),
        )
        monkeypatch.setattr(
            desktop_config,
            "get_settings",
            lambda: _FakeSettings("/does/not/exist"),
        )
        # Only the configured path is missing; the search targets resolve.
        monkeypatch.setattr(
            Path, "is_file", lambda self: self.as_posix() != "/does/not/exist"
        )

        assert chromium_binary() == "/usr/bin/chromium"

    def test_blank_configured_path_does_not_shadow_the_search(self, monkeypatch):
        monkeypatch.setattr(desktop_config.sys, "platform", "linux")
        monkeypatch.setattr(
            desktop_config,
            "which",
            lambda name: {"chromium": "/usr/bin/chromium"}.get(name),
        )
        monkeypatch.setattr(desktop_config, "get_settings", lambda: _FakeSettings(""))
        monkeypatch.setattr(Path, "is_file", lambda self: False)

        assert chromium_binary() == "/usr/bin/chromium"


class TestResolveAutoWindow:
    def test_prefers_the_desktop_app_when_it_is_already_installed(self, monkeypatch):
        """The readout has to match the chain, which the shell now leads."""

        from my_claude_code.config import desktop_shell

        monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, "auto")
        monkeypatch.setattr(desktop_shell, "is_desktop_shell_installed", lambda: True)
        monkeypatch.setattr(
            desktop_config,
            "_chromium_binary_with_label",
            lambda: ("C:/msedge.exe", "Microsoft Edge"),
        )

        provider, reason = desktop_config.resolve_auto_window()

        assert provider == "shell"
        assert desktop_shell.DESKTOP_SHELL_RELEASE_TAG in reason

    def test_an_uninstalled_shell_is_not_promised(self, monkeypatch):
        """An admin request must never download, so it under-promises instead."""

        from my_claude_code.config import desktop_shell

        monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, "auto")
        monkeypatch.setattr(desktop_shell, "is_desktop_shell_installed", lambda: False)
        monkeypatch.setattr(
            desktop_config,
            "_chromium_binary_with_label",
            lambda: ("C:/msedge.exe", "Microsoft Edge"),
        )

        assert desktop_config.resolve_auto_window() == ("app-mode", "Microsoft Edge")

    def test_prefers_app_mode_when_chromium_found(self, monkeypatch):
        monkeypatch.setattr(
            desktop_config,
            "_chromium_binary_with_label",
            lambda: ("C:/msedge.exe", "Microsoft Edge"),
        )
        provider, reason = desktop_config.resolve_auto_window()
        assert provider == "app-mode"
        assert reason == "Microsoft Edge"

    def test_falls_back_to_pywebview_when_no_chromium(self, monkeypatch):
        monkeypatch.setattr(desktop_config, "_chromium_binary_with_label", lambda: None)
        monkeypatch.setattr(desktop_config, "pywebview_available", lambda: True)
        provider, reason = desktop_config.resolve_auto_window()
        assert provider == "pywebview"
        assert "pywebview" in reason

    def test_falls_back_to_browser_when_nothing_available(self, monkeypatch):
        monkeypatch.setattr(desktop_config, "_chromium_binary_with_label", lambda: None)
        monkeypatch.setattr(desktop_config, "pywebview_available", lambda: False)
        provider, reason = desktop_config.resolve_auto_window()
        assert provider == "browser"
        assert reason == "no Chromium browser found"


class TestAdminDesktopEndpoints:
    def _client(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        app = create_test_app()
        return _local_client(app)

    def test_get_returns_defaults(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.get("/admin/api/desktop")

        assert response.status_code == 200
        body = response.json()
        assert body["tray_enabled"] is True
        assert body["start_at_login"] is False
        assert body["minimize_to_tray"] is False
        assert body["server_mode"] == "spawn"
        assert "server_auto_start" not in body

    def test_post_updates_only_submitted_flags(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"start_at_login": True},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["start_at_login"] is True
        assert body["tray_enabled"] is True

    def test_post_accepts_and_persists_server_mode(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"server_mode": "attach"},
            )

        assert response.status_code == 200
        assert response.json()["server_mode"] == "attach"

        path = desktop_config.desktop_state_path()
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["server_mode"] == "attach"

    def test_post_rejects_invalid_server_mode(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"server_mode": "bogus"},
            )

        assert response.status_code == 422

    def test_post_persists_to_disk(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            client.post(
                "/admin/api/desktop",
                json={"tray_enabled": False, "minimize_to_tray": True},
            )

        path = desktop_config.desktop_state_path()
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["tray_enabled"] is False
        assert persisted["minimize_to_tray"] is True
        assert persisted["server_mode"] == "spawn"
        assert "server_auto_start" not in persisted

    def test_post_round_trips(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"server_mode": "off"})
            response = client.get("/admin/api/desktop")

        assert response.json()["server_mode"] == "off"

    def test_post_empty_body_returns_current_state(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post("/admin/api/desktop", json={})

        assert response.status_code == 200
        assert response.json()["tray_enabled"] is True

    def test_post_ignores_unknown_fields(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"start_at_login": True, "hack": "nope"},
            )

        assert response.status_code == 200
        assert response.json()["start_at_login"] is True

    def test_non_loopback_client_is_rejected(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        app = create_test_app()
        with TestClient(app, client=("203.0.113.9", 50000)) as client:
            response = client.get("/admin/api/desktop")

        assert response.status_code == 403

    def test_autostart_options_windows(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "my_claude_code.config.claude_discovery.native_origin",
            lambda: "windows",
        )
        app = create_test_app()
        with _local_client(app) as client:
            response = client.get("/admin/api/desktop/autostart-options")

        assert response.status_code == 200
        body = response.json()
        assert body["origin"] == "windows"
        assert body["targets"] == ["tray"]
        assert body["default_target"] == "tray"

    def test_get_returns_window_and_resolved_auto_provider(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.get("/admin/api/desktop")

        assert response.status_code == 200
        body = response.json()
        assert body["window"] == "auto"
        assert body["window_auto_provider"] in {"app-mode", "pywebview", "browser"}
        assert isinstance(body["window_auto_reason"], str)

    @pytest.mark.parametrize("value", ["auto", "app-mode", "pywebview", "browser"])
    def test_post_round_trips_each_window_value(self, monkeypatch, tmp_path, value):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post("/admin/api/desktop", json={"window": value})
            assert response.status_code == 200
            assert response.json()["window"] == value

            follow_up = client.get("/admin/api/desktop")
            assert follow_up.json()["window"] == value

    def test_post_rejects_invalid_window(self, monkeypatch, tmp_path):
        with self._client(monkeypatch, tmp_path) as client:
            response = client.post(
                "/admin/api/desktop",
                json={"window": "bogus"},
            )

        assert response.status_code == 400

    def test_post_unrelated_field_does_not_reset_window(self, monkeypatch, tmp_path):
        """Regression guard: ``window=current.window`` must stay in place.

        The endpoint used to hardcode ``window=current.window`` because it
        could not accept the field at all. Now that it can, an unrelated save
        (e.g. flipping the tray toggle) must still preserve whatever window
        preference was already persisted -- that preservation is exactly what
        stopped every unrelated save from silently resetting the preference.
        """
        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"window": "browser"})

            response = client.post("/admin/api/desktop", json={"tray_enabled": False})

        assert response.status_code == 200
        assert response.json()["window"] == "browser"
        assert response.json()["tray_enabled"] is False

        path = desktop_config.desktop_state_path()
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["window"] == "browser"

    def test_autostart_options_linux(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "my_claude_code.config.claude_discovery.native_origin",
            lambda: "linux",
        )
        app = create_test_app()
        with _local_client(app) as client:
            response = client.get("/admin/api/desktop/autostart-options")

        assert response.status_code == 200
        body = response.json()
        assert body["origin"] == "linux"
        assert body["targets"] == ["server"]
        assert body["default_target"] == "server"


def test_desktop_gui_scripts_are_registered() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    gui_scripts = manifest["project"]["gui-scripts"]
    assert gui_scripts["mcc-desktop"] == "my_claude_code.cli.desktop_entrypoint:launch"
    assert gui_scripts["fcc-desktop"] == "my_claude_code.cli.desktop_entrypoint:launch"


def test_desktop_entrypoint_is_callable() -> None:
    from my_claude_code.cli import desktop_entrypoint

    assert callable(desktop_entrypoint.launch)


def test_apply_tray_registration_persists_only_the_flag(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)

    desktop_config.apply_tray_registration(False)

    state = load_desktop_state()
    assert state.tray_enabled is False
    assert state.start_at_login is False
    assert state.server_mode == "spawn"


def test_set_start_at_login_persists_flag_and_reconciles_os(
    monkeypatch, tmp_path, fake_winreg
):
    _set_home(monkeypatch, tmp_path)

    desktop_config.set_start_at_login(True)

    assert load_desktop_state().start_at_login is True
    assert WINDOWS_RUN_VALUE in fake_winreg.values

    desktop_config.set_start_at_login(False)

    assert load_desktop_state().start_at_login is False
    assert WINDOWS_RUN_VALUE not in fake_winreg.values


class TestClosingTheAppDoesNotSuppressTheNextWindow:
    """A close that ends the app must not persist "no window".

    minimize_to_tray is off by default, so closing the window quits. If that
    path recorded window_open=False, the ordinary act of closing the app would
    stop it ever opening a window again on the next launch.
    """

    def _controller(self, monkeypatch, tmp_path):
        from my_claude_code.cli.desktop import DesktopController
        from my_claude_code.core.interprocess_lock import InterprocessFileLock

        _set_home(monkeypatch, tmp_path)
        return DesktopController(lock=InterprocessFileLock(tmp_path / "d.lock"))

    def test_a_close_that_quits_leaves_the_window_state_alone(
        self, monkeypatch, tmp_path
    ):
        controller = self._controller(monkeypatch, tmp_path)
        desktop_config.set_window_open(True)
        # Default: no minimize-to-tray, so closing the window ends the app.
        desktop_config._update_state(minimize_to_tray=False)

        keeps_running = controller.handle_window_closed()

        assert keeps_running is False
        assert desktop_config.load_desktop_state().window_open is True

    def test_a_close_that_minimises_records_no_window(self, monkeypatch, tmp_path):
        controller = self._controller(monkeypatch, tmp_path)
        desktop_config.set_window_open(True)
        desktop_config._update_state(minimize_to_tray=True, tray_enabled=True)

        keeps_running = controller.handle_window_closed()

        assert keeps_running is True
        assert desktop_config.load_desktop_state().window_open is False
