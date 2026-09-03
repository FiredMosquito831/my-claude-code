"""Admin API tests for the onboarding checklist endpoints."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app

EXPECTED_STEP_IDS = (
    "provider",
    "models",
    "client",
    "coding_agents",
    "websearch",
    "messaging",
    "analytics",
    "guide",
)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def test_get_onboarding_returns_200_with_step_list(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    response = _local_client(app).get("/admin/api/onboarding")

    assert response.status_code == 200
    body = response.json()
    assert tuple(step["id"] for step in body["steps"]) == EXPECTED_STEP_IDS
    assert body["dismissed"] is False
    assert isinstance(body["required_total"], int)
    assert isinstance(body["complete"], bool)
    for step in body["steps"]:
        assert isinstance(step["instructions"], list)
        assert len(step["instructions"]) > 0
        assert "target" in step


def test_get_onboarding_rejects_non_loopback_client(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    remote_client = TestClient(app, client=("203.0.113.10", 50000))
    response = remote_client.get("/admin/api/onboarding")

    assert response.status_code == 403


def test_post_onboarding_sets_dismissed(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    client = _local_client(app)

    response = client.post("/admin/api/onboarding", json={"dismissed": True})

    assert response.status_code == 200
    assert response.json()["dismissed"] is True

    # Persisted across requests.
    follow_up = client.get("/admin/api/onboarding")
    assert follow_up.json()["dismissed"] is True


def test_post_onboarding_rejects_non_loopback_client(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    remote_client = TestClient(app, client=("203.0.113.10", 50000))
    response = remote_client.post("/admin/api/onboarding", json={"dismissed": True})

    assert response.status_code == 403


def test_post_onboarding_merges_visited_additively(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    client = _local_client(app)

    first = client.post("/admin/api/onboarding", json={"visited": ["guide"]})
    assert first.status_code == 200

    second = client.post("/admin/api/onboarding", json={"visited": ["providers"]})
    assert second.status_code == 200

    guide_step = next(step for step in second.json()["steps"] if step["id"] == "guide")
    assert guide_step["done"] is True


def test_get_onboarding_client_step_reflects_claude_settings_file(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    client = _local_client(app)

    before = client.get("/admin/api/onboarding").json()
    client_step_before = next(
        step for step in before["steps"] if step["id"] == "client"
    )
    assert client_step_before["done"] is False

    settings = Settings()
    apply_response = client.post("/admin/api/claude-settings/apply", json={})
    assert apply_response.status_code == 200

    after = client.get("/admin/api/onboarding").json()
    client_step_after = next(step for step in after["steps"] if step["id"] == "client")
    assert client_step_after["done"] is True
    assert settings is not None  # sanity: Settings() constructible in this env


def test_get_onboarding_analytics_step_reflects_request_log(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    client = _local_client(app)

    before = client.get("/admin/api/onboarding").json()
    analytics_before = next(
        step for step in before["steps"] if step["id"] == "analytics"
    )
    assert analytics_before["done"] is False

    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.enqueue(
        RequestRecord(
            id="r1",
            endpoint="/v1/messages",
            protocol="anthropic",
            provider="p1",
            resolved_model="m1",
            ts_epoch=time.time(),
            status="success",
            error_message=None,
            tokens_in=10,
            tokens_out=1,
            duration_ms=100.0,
            input_text="in",
            output_text="out",
        )
    )
    store.close()

    after = client.get("/admin/api/onboarding").json()
    analytics_after = next(step for step in after["steps"] if step["id"] == "analytics")
    assert analytics_after["done"] is True


def test_onboarding_response_never_leaks_configured_auth_token(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "super-secret-token")
    app = create_test_app()

    response = _local_client(app).get("/admin/api/onboarding")

    assert "super-secret-token" not in response.text
