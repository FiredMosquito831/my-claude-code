"""Admin API tests for the onboarding checklist endpoints."""

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.config import claude_discovery
from my_claude_code.config.claude_discovery import DiscoveredSettings
from my_claude_code.config.paths import onboarding_state_path
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _no_real_wsl(monkeypatch):
    """Never shell out to ``wsl.exe`` to answer a settings-discovery question.

    7.122.0: "Connect a coding agent" reads every Claude Code settings file
    discovery finds, and discovery enumerates WSL distributions -- the same
    idiom tests/api/test_admin_claude_settings.py uses for the same reason.
    """

    monkeypatch.setattr(claude_discovery, "wsl_distributions", lambda: ())


EXPECTED_STEP_IDS = (
    "provider",
    "models",
    "client",
    "connect_agents",
    # 7.99.0: the media server step (the user's 2026-10-18 12:53 amendment).
    "media_tools",
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


# ------------------------------------------------------------------ 7.122.0

#: The keys every answer had before 7.122.0, in their order, and the step keys.
BASE_KEYS = ["dismissed", "steps", "required_total", "required_done", "complete"]
BASE_STEP_KEYS = [
    "id",
    "label",
    "description",
    "view",
    "optional",
    "done",
    "instructions",
    "target",
    "guide_anchors",
    "agents",
]


def _step(body: dict, step_id: str) -> dict:
    return next(step for step in body["steps"] if step["id"] == step_id)


def _log_one_request(tmp_path: Path, *, harness: str | None = None) -> None:
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
            input_text="a prompt nobody should see here",
            output_text="an answer nobody should see here",
            harness=harness,
        )
    )
    store.close()


def test_the_new_keys_are_additive_and_come_last(monkeypatch, tmp_path):
    """Every key the 7.121 page read is still there, in the same order."""

    _set_home(monkeypatch, tmp_path)
    body = _local_client(create_test_app()).get("/admin/api/onboarding").json()

    assert list(body) == [*BASE_KEYS, "skipped", "request_log"]
    for step in body["steps"]:
        assert list(step) == [*BASE_STEP_KEYS, "measurable"], step["id"]
    assert tuple(step["id"] for step in body["steps"]) == EXPECTED_STEP_IDS
    assert body["required_total"] == 4
    assert body["skipped"] == []
    assert body["request_log"] == {"enabled": True, "total": 0, "last": None}


def test_not_for_me_round_trips_through_the_config_folder(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    hidden = client.post(
        "/admin/api/onboarding",
        json={"skipped": ["reasoning", "proxying", "reasoning"]},
    )
    assert hidden.status_code == 200
    assert hidden.json()["skipped"] == ["proxying", "reasoning"]

    # A second client -- another browser, the desktop app -- reads the same.
    again = _local_client(create_test_app()).get("/admin/api/onboarding").json()
    assert again["skipped"] == ["proxying", "reasoning"]
    stored = json.loads(onboarding_state_path().read_text(encoding="utf-8"))
    assert stored["skipped"] == ["proxying", "reasoning"]

    # Dismissing does not un-hide anything.
    dismissed = client.post("/admin/api/onboarding", json={"dismissed": True}).json()
    assert dismissed["skipped"] == ["proxying", "reasoning"]

    # "Show hidden" sends the empty list.
    shown = client.post("/admin/api/onboarding", json={"skipped": []}).json()
    assert shown["skipped"] == []
    assert shown["dismissed"] is True


def test_dismiss_has_an_undo(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    assert client.post("/admin/api/onboarding", json={"dismissed": True}).json()[
        "dismissed"
    ]
    undone = client.post("/admin/api/onboarding", json={"dismissed": False}).json()

    assert undone["dismissed"] is False
    assert client.get("/admin/api/onboarding").json()["dismissed"] is False


def test_a_state_file_from_before_7_122_still_reads(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    path = onboarding_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"dismissed": true, "visited": ["guide"]}', encoding="utf-8")

    body = _local_client(create_test_app()).get("/admin/api/onboarding").json()

    assert body["dismissed"] is True
    assert body["skipped"] == []
    assert _step(body, "guide")["done"] is True


def test_a_corrupt_state_file_hides_nothing_and_is_not_an_error(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    path = onboarding_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"skipped": not json', encoding="utf-8")

    response = _local_client(create_test_app()).get("/admin/api/onboarding")

    assert response.status_code == 200
    assert response.json()["skipped"] == []
    assert response.json()["dismissed"] is False


@pytest.mark.parametrize(
    "skipped",
    [
        ["<img src=x onerror=alert(1)>"],
        ["Proxying"],
        ["../etc/passwd"],
        ["ok_id", ""],
        [f"card_{index}" for index in range(200)],
    ],
)
def test_a_malformed_hidden_list_is_refused_and_nothing_is_written(
    monkeypatch, tmp_path, skipped
):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())
    client.post("/admin/api/onboarding", json={"skipped": ["proxying"]})
    before = hashlib.sha256(onboarding_state_path().read_bytes()).hexdigest()

    response = client.post(
        "/admin/api/onboarding", json={"skipped": skipped, "dismissed": True}
    )

    assert response.status_code == 422
    after = hashlib.sha256(onboarding_state_path().read_bytes()).hexdigest()
    assert after == before


def test_a_non_string_hidden_id_is_refused(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)

    response = _local_client(create_test_app()).post(
        "/admin/api/onboarding", json={"skipped": [1, 2]}
    )

    assert response.status_code == 422
    assert not onboarding_state_path().exists()


def test_essential_3_is_done_from_a_wsl_settings_file(monkeypatch, tmp_path):
    """Decision 2: Claude Code's settings in ANY discovered file count."""

    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())
    wsl_settings = tmp_path / "wsl" / "home" / "me" / ".claude" / "settings.json"
    wsl_settings.parent.mkdir(parents=True)
    applied = client.post(
        "/admin/api/claude-settings/apply", json={"path": str(wsl_settings)}
    )
    assert applied.status_code == 200

    before = client.get("/admin/api/onboarding").json()
    assert _step(before, "client")["done"] is False  # the native file is unset

    monkeypatch.setattr(
        admin_routes,
        "discover_settings_files",
        lambda: [
            DiscoveredSettings(path=str(wsl_settings), origin="wsl", detail="Ubuntu")
        ],
    )
    after = client.get("/admin/api/onboarding").json()

    assert _step(after, "client")["done"] is True
    assert _step(after, "client")["label"] == "Connect a coding agent"


def test_essential_3_is_done_from_a_launcher_users_traffic(monkeypatch, tmp_path):
    """The U2 case end to end: mcc-claude traffic, no settings.json anywhere."""

    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())
    assert _step(client.get("/admin/api/onboarding").json(), "client")["done"] is False

    _log_one_request(tmp_path, harness="claude")
    body = client.get("/admin/api/onboarding").json()

    assert _step(body, "client")["done"] is True
    assert _step(body, "analytics")["done"] is True
    claude = next(
        agent
        for agent in _step(body, "connect_agents")["agents"]
        if agent["id"] == "claude"
    )
    assert claude["requests_7d"] == 1
    assert claude["connected"] is True


def test_the_wsl_probe_is_skipped_once_an_agent_has_answered(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        admin_routes, "discover_settings_files", lambda: calls.append(1) or []
    )
    client = _local_client(create_test_app())

    client.get("/admin/api/onboarding")
    assert calls == [1]

    _log_one_request(tmp_path, harness="claude")
    client.get("/admin/api/onboarding")
    assert calls == [1]


def test_check_it_worked_names_the_newest_request_without_its_text(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())
    _log_one_request(tmp_path, harness="claude")

    response = client.get("/admin/api/onboarding")
    log = response.json()["request_log"]

    assert log["enabled"] is True
    assert log["total"] == 1
    assert set(log["last"]) == {
        "id",
        "ts_epoch",
        "status",
        "provider",
        "resolved_model",
        "requested_model",
        "harness",
        "optimization",
    }
    assert log["last"]["id"] == "r1"
    assert log["last"]["resolved_model"] == "m1"
    assert log["last"]["harness"] == "claude"
    assert "nobody should see" not in response.text


def test_with_the_request_log_off_step_4_is_not_measured(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    client = _local_client(create_test_app(Settings()))

    body = client.get("/admin/api/onboarding").json()

    analytics = _step(body, "analytics")
    assert analytics["measurable"] is False
    assert analytics["optional"] is False
    assert body["required_total"] == 3
    assert body["request_log"] == {"enabled": False, "total": 0, "last": None}
    assert all(
        step["measurable"] for step in body["steps"] if step["id"] != "analytics"
    )
