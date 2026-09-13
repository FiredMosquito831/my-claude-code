"""The setting and the OS agree, or the setting reports the OS.

`start_at_login` in `desktop.json` was only ever an *intent*. The admin route
persisted it and deliberately left the OS alone -- "the next ``mcc-desktop``/
tray launch reconciles it" -- and on Windows the reconciliation happens in
``cli/desktop._reconcile_start_at_login``. A user who launches the app binary
directly never runs that code, which is the same fifteen-release shape as the
stale shell: the dashboard said Start at Login and ``HKCU\\...\\Run`` carried
nothing, indefinitely.

Two halves, both tested here:

* the route reconciles the registration itself, because it is loopback-only and
  therefore the browser, the server and the per-user registration are all on
  one machine and under one user;
* the answer it returns is the OS's, not the file's, so a registration that
  could not be made cannot be reported as made.

Every registry call in this file runs against ``fake_winreg``. The real Run key
is never opened: the session-wide guard in ``tests/support/hermetic.py`` refuses
that outright, after two runs of an earlier suite deleted the developer's own
autostart value.
"""

import json

import pytest

from my_claude_code.api import admin_routes
from my_claude_code.config import desktop as desktop_config
from my_claude_code.config.desktop import (
    WINDOWS_RUN_VALUE,
    DesktopState,
    reconcile_start_at_login,
    registration_wanted,
    start_at_login_registered,
)
from tests.api.support import create_test_app
from tests.config.test_desktop import _local_client, _set_home


def _state(**overrides: object) -> DesktopState:
    base = {
        "tray_enabled": True,
        "start_at_login": True,
    }
    base.update(overrides)
    return DesktopState(
        tray_enabled=bool(base["tray_enabled"]),
        start_at_login=bool(base["start_at_login"]),
    )


class TestTheReader:
    def test_an_empty_run_key_is_not_registered(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        _set_home(monkeypatch, tmp_path)

        assert start_at_login_registered() is False

    def test_a_written_value_is_registered(self, monkeypatch, tmp_path, fake_winreg):
        _set_home(monkeypatch, tmp_path)
        desktop_config.apply_start_at_login("tray")

        assert start_at_login_registered() is True

    def test_removing_it_is_visible_immediately(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        _set_home(monkeypatch, tmp_path)
        desktop_config.apply_start_at_login("tray")
        desktop_config.remove_start_at_login("tray")

        assert start_at_login_registered() is False

    def test_a_registry_that_cannot_be_read_answers_neither_yes_nor_no(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        """``None`` is a third answer, and the UI keeps showing the intent for it."""

        _set_home(monkeypatch, tmp_path)

        def _refuse(*_args: object, **_kwargs: object):
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(fake_winreg, "OpenKey", _refuse)

        assert start_at_login_registered() is None


class TestReconcile:
    def test_it_writes_the_value_the_state_asks_for(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        _set_home(monkeypatch, tmp_path)

        assert reconcile_start_at_login(_state()) is True
        assert WINDOWS_RUN_VALUE in fake_winreg.values

    def test_a_disabled_tray_unregisters(self, monkeypatch, tmp_path, fake_winreg):
        """Registration is honoured only while the tray is enabled: a disabled
        tray has nothing to start."""

        _set_home(monkeypatch, tmp_path)
        desktop_config.apply_start_at_login("tray")

        assert registration_wanted(_state(tray_enabled=False)) is False
        assert reconcile_start_at_login(_state(tray_enabled=False)) is False
        assert WINDOWS_RUN_VALUE not in fake_winreg.values

    def test_the_skip_switch_still_stops_every_write(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        """The switch exists because the registration is machine-global and the
        preference driving it is not. A scratch run must not touch it."""

        _set_home(monkeypatch, tmp_path)
        monkeypatch.setenv(desktop_config.SKIP_AUTOSTART_ENV, "1")

        assert reconcile_start_at_login(_state()) is False
        assert fake_winreg.values == {}

    def test_a_failed_registration_is_reported_as_not_registered(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        """Never raises, and never claims success it did not have."""

        _set_home(monkeypatch, tmp_path)

        def _refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(5, "Access is denied")

        monkeypatch.setattr(desktop_config, "apply_start_at_login", _refuse)

        assert reconcile_start_at_login(_state()) is False


class TestTheRoute:
    @pytest.fixture(autouse=True)
    def _no_window_probe(self, monkeypatch):
        """Keep the response builder off ``shutil.which``.

        ``_desktop_state_response`` resolves the ``auto`` window by probing the
        filesystem for a Chromium binary, and ``fake_winreg`` fakes
        ``sys.platform`` as ``win32`` -- which on a Linux runner makes
        ``shutil.which`` reach for ``_winapi`` and fail. The window probe is
        pinned in ``tests/config/test_desktop.py`` and is not what this file is
        about.
        """

        monkeypatch.setattr(
            admin_routes, "resolve_auto_window", lambda: ("browser", "stubbed")
        )

    def _client(self, monkeypatch, tmp_path):
        _set_home(monkeypatch, tmp_path)
        return _local_client(create_test_app())

    def test_enabling_it_registers_it_now(self, monkeypatch, tmp_path, fake_winreg):
        """The defect, directly: the toggle used to write the file and stop."""

        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"start_at_login": False})
            assert fake_winreg.values == {}

            body = client.post(
                "/admin/api/desktop", json={"start_at_login": True}
            ).json()

        assert body["start_at_login"] is True
        assert body["start_at_login_registered"] is True
        assert WINDOWS_RUN_VALUE in fake_winreg.values

    def test_disabling_it_removes_the_value(self, monkeypatch, tmp_path, fake_winreg):
        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"start_at_login": True})
            assert WINDOWS_RUN_VALUE in fake_winreg.values

            body = client.post(
                "/admin/api/desktop", json={"start_at_login": False}
            ).json()

        assert body["start_at_login"] is False
        assert body["start_at_login_registered"] is False
        assert fake_winreg.values == {}

    def test_the_reported_state_round_trips(self, monkeypatch, tmp_path, fake_winreg):
        """GET after POST agrees with the registry, not with the file."""

        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"start_at_login": True})
            body = client.get("/admin/api/desktop").json()

        assert body["start_at_login_registered"] is True

        persisted = json.loads(
            desktop_config.desktop_state_path().read_text(encoding="utf-8")
        )
        assert persisted["start_at_login"] is True

    def test_an_unrelated_save_does_not_rewrite_the_registration(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        """A window-preference click must not rewrite the Run value."""

        with self._client(monkeypatch, tmp_path) as client:
            client.post("/admin/api/desktop", json={"start_at_login": True})
            before = dict(fake_winreg.values)
            monkeypatch.setattr(
                desktop_config,
                "apply_start_at_login",
                lambda *_a, **_k: pytest.fail("an unrelated save touched the registry"),
            )
            monkeypatch.setattr(
                desktop_config,
                "remove_start_at_login",
                lambda *_a, **_k: pytest.fail("an unrelated save touched the registry"),
            )
            client.post("/admin/api/desktop", json={"window": "browser"})

        assert fake_winreg.values == before

    def test_the_file_still_records_the_intent(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        """When the OS refuses, the preference is still saved and the next
        launch is the retry -- the report is what changes, not the storage."""

        with self._client(monkeypatch, tmp_path) as client:
            monkeypatch.setattr(
                desktop_config,
                "apply_start_at_login",
                lambda *_a, **_k: (_ for _ in ()).throw(OSError(5, "denied")),
            )
            body = client.post(
                "/admin/api/desktop", json={"start_at_login": True}
            ).json()

        assert body["start_at_login"] is True
        assert body["start_at_login_registered"] is False
        persisted = json.loads(
            desktop_config.desktop_state_path().read_text(encoding="utf-8")
        )
        assert persisted["start_at_login"] is True

    def test_the_shipped_default_is_reported_honestly_before_anything_runs(
        self, monkeypatch, tmp_path, fake_winreg
    ):
        """The exact shape found on the reporter's machine.

        ``start_at_login`` has been ``True`` by default since 6.68.0, so a
        fresh install's file says "Start at Login" before anything has
        registered anything. A GET must say so rather than repeat the file --
        and it must not repair it either, because a read is a read.
        """

        with self._client(monkeypatch, tmp_path) as client:
            body = client.get("/admin/api/desktop").json()

            assert body["start_at_login"] is True
            assert body["start_at_login_registered"] is False
            assert fake_winreg.values == {}

            # Any desktop save now reconciles it, because the OS disagrees
            # with the file -- not because this particular field changed.
            repaired = client.post(
                "/admin/api/desktop", json={"window": "browser"}
            ).json()

        assert repaired["start_at_login_registered"] is True
        assert WINDOWS_RUN_VALUE in fake_winreg.values
