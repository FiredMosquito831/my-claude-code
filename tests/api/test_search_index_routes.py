"""The search index's routes on the Requests page (7.92.0)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.api.dependencies import get_settings
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log as rl
from my_claude_code.core.request_log import RequestLogStore
from tests.api.support import create_test_app
from tests.support.search_log import build_search_log


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RequestLogStore:
    built, _times = build_search_log(tmp_path / "requests.db", rows=80, seed=3)
    monkeypatch.setattr(
        admin_routes, "_request_log_store_or_none", lambda _settings: built
    )
    return built


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def test_status_says_what_the_index_covers(
    client: TestClient, store: RequestLogStore
) -> None:
    response = client.get("/admin/api/requests/search-index")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["available"] is True
    assert body["state"] == "idle"
    # Written by this version, so every row is covered from the start.
    assert body["coverage"] == {"rows": 80, "covered": 80}
    assert body["bytes"] > 0


def test_build_starts_only_on_a_post_and_pause_pauses_it(
    client: TestClient, store: RequestLogStore
) -> None:
    assert client.get("/admin/api/requests/search-index").json()["state"] == "idle"

    started = client.post("/admin/api/requests/search-index/build")
    paused = client.post("/admin/api/requests/search-index/pause")
    resumed = client.post("/admin/api/requests/search-index/build")

    assert started.status_code == 200
    assert started.json()["state"] == "running"
    assert paused.json()["state"] == "paused"
    assert resumed.json()["state"] == "running"


def test_the_routes_are_loopback_only(
    client: TestClient, store: RequestLogStore
) -> None:
    remote = TestClient(client.app, client=("203.0.113.10", 50000))

    assert remote.get("/admin/api/requests/search-index").status_code == 403
    assert remote.post("/admin/api/requests/search-index/build").status_code == 403
    assert remote.post("/admin/api/requests/search-index/pause").status_code == 403


def test_the_routes_say_so_when_the_log_is_off() -> None:
    settings = Settings()
    settings.request_log_enabled = False
    app = create_test_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, client=("127.0.0.1", 50000))

    status = client.get("/admin/api/requests/search-index").json()

    assert status["enabled"] is False
    assert status["available"] is False
    assert client.post("/admin/api/requests/search-index/build").status_code == 409
    assert client.post("/admin/api/requests/search-index/pause").status_code == 409


def test_a_python_without_the_index_says_why_and_refuses_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    reason = "This Python's SQLite (3.31.1) cannot keep the search index (no such tokenizer: trigram), so searches read every stored request, as before."
    monkeypatch.setattr(rl, "search_index_support", lambda: (False, reason))
    built, _times = build_search_log(tmp_path / "requests.db", rows=20, seed=5)
    monkeypatch.setattr(
        admin_routes, "_request_log_store_or_none", lambda _settings: built
    )

    status = client.get("/admin/api/requests/search-index").json()
    refused = client.post("/admin/api/requests/search-index/build")

    assert status["available"] is False
    assert status["reason"] == reason
    assert status["coverage"] is None
    assert refused.status_code == 409
    assert refused.json()["detail"] == reason
