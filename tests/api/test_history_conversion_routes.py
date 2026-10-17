"""Pause / Continue of the request log's background history conversion (7.93.0)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.core.request_log import RequestLogStore
from tests.api.support import create_test_app


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RequestLogStore]:
    built = RequestLogStore(tmp_path / "requests.db", max_rows=0)
    monkeypatch.setattr(
        admin_routes, "_request_log_store_or_none", lambda _settings: built
    )
    yield built
    built.close()


def test_pause_and_continue_say_so_and_the_storage_readout_agrees(
    store: RequestLogStore,
) -> None:
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))

    paused = client.post("/admin/api/requests/history-conversion/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    lifetime = client.get("/admin/api/requests/lifetime").json()
    assert lifetime["storage"]["history"]["paused"] is True

    resumed = client.post("/admin/api/requests/history-conversion/continue")
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert store._history_paused is False


def test_only_this_machine_may_pause_it(store: RequestLogStore) -> None:
    client = TestClient(create_test_app(), client=("203.0.113.9", 50000))

    response = client.post("/admin/api/requests/history-conversion/pause")

    assert response.status_code == 403
    assert store._history_paused is False


def test_without_a_request_log_there_is_nothing_to_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_routes, "_request_log_store_or_none", lambda _s: None)
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))

    for action in ("pause", "continue"):
        response = client.post(f"/admin/api/requests/history-conversion/{action}")
        assert response.status_code == 409
