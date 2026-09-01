"""Tests for the admin RTK (token optimizer) endpoints."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config import rtk as rtk_config
from my_claude_code.config.rtk import RtkState
from tests.api.support import create_test_app


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _patch_status(monkeypatch):
    """Record apply_rtk_state calls while keeping the real rtk_status().

    ``rtk_status()`` is left real so the response reflects the persisted state;
    only ``_available_binary`` is forced to ``None`` so it never spawns a
    subprocess or depends on PATH.
    """

    applied: list[RtkState] = []

    def fake_apply(state: RtkState) -> None:
        applied.append(state)

    monkeypatch.setattr("my_claude_code.api.admin_routes.apply_rtk_state", fake_apply)
    monkeypatch.setattr(rtk_config, "_available_binary", lambda: None)
    return applied


def test_get_returns_status_and_state(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    rtk_config.save_rtk_state(RtkState(claude=True, pi=True))
    app = create_test_app()

    with _local_client(app) as client:
        response = client.get("/admin/api/rtk")

    assert response.status_code == 200
    body = response.json()
    assert body["installed"] is False
    assert body["claude"] is True
    assert body["codex"] is False
    assert body["pi"] is True
    assert body["binary_path"] is None
    assert body["version"] is None


def test_post_persists_and_reconciles(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    applied = _patch_status(monkeypatch)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/rtk", json={"claude": True, "codex": True})

    assert response.status_code == 200
    assert response.json()["claude"] is True
    assert response.json()["codex"] is True
    assert response.json()["pi"] is False
    assert applied == [RtkState(claude=True, codex=True, pi=False)]

    persisted = json.loads(rtk_config.rtk_state_path().read_text(encoding="utf-8"))
    assert persisted == {"claude": True, "codex": True, "pi": False}


def test_post_partial_update_preserves_other_agents(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    rtk_config.save_rtk_state(RtkState(claude=True, codex=True, pi=True))
    applied = _patch_status(monkeypatch)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/rtk", json={"codex": False})

    assert response.status_code == 200
    assert response.json() == {
        "installed": False,
        "claude": True,
        "codex": False,
        "pi": True,
        "agents": {"claude": True, "codex": False, "pi": True},
        "binary_path": None,
        "version": None,
        "installed_version": None,
        "pinned_version": rtk_config.RTK_VERSION,
        "version_matches_pin": None,
    }
    assert applied == [RtkState(claude=True, codex=False, pi=True)]


def test_post_empty_body_is_a_no_op(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    applied = _patch_status(monkeypatch)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/rtk", json={})

    assert response.status_code == 200
    assert response.json()["claude"] is False
    # No changes were submitted, so the reconciler must not run.
    assert applied == []
    assert not rtk_config.rtk_state_path().exists()


def test_post_ignores_unknown_fields(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    applied = _patch_status(monkeypatch)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/rtk", json={"claude": True, "hack": "nope"})

    assert response.status_code == 200
    assert response.json()["claude"] is True
    assert applied == [RtkState(claude=True, codex=False, pi=False)]


def test_post_reconcile_failure_is_reported(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)

    from my_claude_code.config.rtk import RtkError

    def fail_apply(state: RtkState) -> None:
        raise RtkError("boom")

    monkeypatch.setattr("my_claude_code.api.admin_routes.apply_rtk_state", fail_apply)
    monkeypatch.setattr(rtk_config, "_available_binary", lambda: None)
    app = create_test_app()

    with _local_client(app) as client:
        response = client.post("/admin/api/rtk", json={"claude": True})

    assert response.status_code == 409
    assert "boom" in response.json()["detail"]


def test_non_loopback_client_is_rejected(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    remote_client = TestClient(app, client=("203.0.113.9", 50000))

    get_response = remote_client.get("/admin/api/rtk")
    post_response = remote_client.post("/admin/api/rtk", json={"claude": True})

    assert get_response.status_code == 403
    assert post_response.status_code == 403
