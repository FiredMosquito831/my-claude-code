"""``GET /admin/api/config-dir`` -- the Get Started banner's only data source.

There was no test anywhere for this route in 6.40.0, and that is precisely why
the release shipped a **Move to ~/.mcc now** button that could never render: the
predicate asked whether ``config_dir_path()`` existed, and for the legacy user
the button exists for, ``config_dir_path()`` *is* ``~/.fcc``, which does exist.
The button is gone now (a migration is a shell command with the server stopped,
not a click in the page the server is serving), the write route with it, and
what remains is a read-only status the banner renders as a sentence.

Every case here drives resolution through a redirected ``Path.home`` and the
process cache reset, so the shape of the developer's own machine cannot make a
case pass or fail.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config import paths
from tests.api.support import create_test_app


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MCC_CONFIG_DIR", raising=False)
    paths.reset_config_dir_cache()


def _legacy(tmp_path: Path) -> Path:
    legacy = tmp_path / ".fcc"
    legacy.mkdir()
    (legacy / ".env").write_text("MODEL=nvidia_nim/test\n", encoding="utf-8")
    return legacy


def _status(client) -> dict:
    response = client.get("/admin/api/config-dir")
    assert response.status_code == 200
    return response.json()


def test_legacy_only_home_reports_itself_and_says_what_to_run(
    client, tmp_path, monkeypatch
) -> None:
    """The case 6.40.0 got wrong: a user whose data is in ``~/.fcc``."""
    _home(tmp_path, monkeypatch)
    legacy = _legacy(tmp_path)

    payload = _status(client)

    assert payload["usesLegacyHome"] is True
    assert payload["legacyUnhealthy"] is False
    assert payload["currentDir"] == str(legacy)
    # The distinction the old predicate collapsed: the directory in use is not
    # the directory a migration would create.
    assert payload["newDir"] == str(tmp_path / ".mcc")
    assert payload["currentDir"] != payload["newDir"]
    assert "mcc-migrate" in payload["banner"]
    assert "stop the server and the tray" in payload["banner"].lower()


def test_legacy_home_that_failed_a_check_is_still_the_home(
    client, tmp_path, monkeypatch
) -> None:
    """A failed probe is a warning about a directory that stays in use."""
    _home(tmp_path, monkeypatch)
    legacy = _legacy(tmp_path)
    (legacy / "custom_providers.json").write_text("[]", encoding="utf-8")

    payload = _status(client)

    assert payload["usesLegacyHome"] is True
    assert payload["legacyUnhealthy"] is True
    assert payload["failedCheck"] == "custom_providers"
    assert payload["currentDir"] == str(legacy)
    assert "still the directory in use" in payload["banner"]
    assert not (tmp_path / ".mcc").exists()


def test_dual_directories_report_the_winner_and_promise_no_merge(
    client, tmp_path, monkeypatch
) -> None:
    _home(tmp_path, monkeypatch)
    _legacy(tmp_path)
    (tmp_path / ".mcc").mkdir()

    payload = _status(client)

    assert payload["usesLegacyHome"] is False
    assert payload["currentDir"] == str(tmp_path / ".mcc")
    assert "Nothing is ever merged" in payload["banner"]


def test_mcc_config_dir_override_is_reported_with_no_banner(
    client, tmp_path, monkeypatch
) -> None:
    pinned = tmp_path / "pinned-config"
    pinned.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(pinned))
    paths.reset_config_dir_cache()

    payload = _status(client)

    assert payload["currentDir"] == str(pinned)
    assert payload["usesLegacyHome"] is False
    # Nothing to tell the user: they pinned the directory themselves.
    assert payload["banner"] == ""


def test_fresh_mcc_home_has_nothing_to_say(client, tmp_path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    (tmp_path / ".mcc").mkdir()

    payload = _status(client)

    assert payload["currentDir"] == str(tmp_path / ".mcc")
    assert payload["usesLegacyHome"] is False
    assert payload["legacyUnhealthy"] is False
    assert payload["banner"] == ""


def test_config_dir_status_is_loopback_only(tmp_path, monkeypatch) -> None:
    _home(tmp_path, monkeypatch)
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))

    assert remote.get("/admin/api/config-dir").status_code == 403


def test_the_migrate_route_no_longer_exists(client, tmp_path, monkeypatch) -> None:
    """The only way ``~/.fcc`` becomes ``~/.mcc`` is the ``mcc-migrate`` command.

    Not a button, not a POST. ``require_loopback_admin`` accepts a missing
    ``Origin``, so while this route existed any local process could relocate a
    user's keys and request history with one unauthenticated request -- and on
    Windows it could not have succeeded anyway, because the server answering it
    holds the request log open inside the directory being renamed.
    """
    _home(tmp_path, monkeypatch)
    _legacy(tmp_path)

    response = client.post("/admin/api/migrate-config-dir", json={})
    assert response.status_code in (404, 405)

    from my_claude_code.api import admin_routes

    assert not hasattr(admin_routes, "MigrateConfigDirPayload")
    assert not any(
        "migrate-config-dir" in getattr(route, "path", "")
        for route in admin_routes.router.routes
    )


def test_the_dashboard_has_no_migrate_button() -> None:
    """The banner renders a sentence; there is no control to press."""
    static = Path(__file__).resolve().parents[2] / "src/my_claude_code/api/admin_static"
    script = (static / "admin.js").read_text(encoding="utf-8")

    assert "migrate-config-dir" not in script
    assert "config-dir-migrate-button" not in script
    assert "Move to ~/.mcc now" not in script
    assert "config-dir-migrate-button" not in (static / "admin.css").read_text(
        encoding="utf-8"
    )
