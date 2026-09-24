"""The Session and Folder filters over HTTP, and the origin breakdown route (7.43.0).

FastAPI drops an unknown query parameter silently, which is how an export once
disagreed with the table it was taken from (6.13.0 / 6.54.0). So every route
the Analytics page calls is asked the same filtered question here and must
count the same rows. Every path and id is fake.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.request_log import (
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    get_request_log_store,
)
from tests.api.support import create_test_app

S1 = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
S2 = "7d9e1f20-3a4b-4c5d-8e6f-a0b1c2d3e4f5"
A1 = "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
DEMO = "C:\\Users\\devuser\\Projects\\demo"
GAMES = "D:\\work\\games\\Phone games"


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def seeded_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time() - 60
    plan = [
        ("p1", S1, None, DEMO),
        ("c1", S1, A1, DEMO),
        ("c2", S1, A1, DEMO),
        ("g1", S2, None, GAMES),
        ("bare", None, None, None),
    ]
    for index, (request_id, session, agent, folder) in enumerate(plan):
        store.enqueue(
            RequestRecord(
                id=request_id,
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="p1",
                resolved_model="m1",
                harness="claude",
                ts_epoch=base + index,
                status="success",
                tokens_in=10,
                tokens_out=2,
                duration_ms=100.0,
                ttft_ms=20.0,
                input_text="hi",
                output_text="out",
                session_id=session,
                agent_id=agent,
                parent_session_id=session if agent else None,
                project_dir=folder,
                attempts=(
                    RouteAttempt(
                        attempt=0,
                        provider="p1",
                        model_ref="p1/m1",
                        outcome=RouteAttemptOutcome.SUCCEEDED,
                    ),
                ),
            )
        )
    store.close()
    yield store


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"session": S1[:8]}, 3),
        ({"folder": DEMO}, 3),
        ({"folder": "phone GAMES"}, 1),
        ({"session": S1[:8], "folder": GAMES}, 0),
        ({}, 5),
    ],
)
def test_every_analytics_route_honours_the_origin_filters(
    client, seeded_store, params, expected
) -> None:
    listed = client.get("/admin/api/requests", params={"limit": 50, **params}).json()
    assert listed["total"] == expected
    assert client.get("/admin/api/requests/count", params=params).json()["total"] == (
        expected
    )
    stats = client.get("/admin/api/requests/stats", params=params).json()
    assert stats["total"] == expected
    assert client.get("/admin/api/requests/pulse", params=params).json()["total"] == (
        expected
    )
    ttft = client.get("/admin/api/requests/ttft", params=params).json()
    assert ttft["measured"] == expected
    cost = client.get("/admin/api/requests/cost", params=params).json()
    assert cost["totals"]["requests"] == expected


@pytest.mark.parametrize("scope", ["requests", "attempts"])
def test_export_filters_honour_the_origin_params(client, seeded_store, scope) -> None:
    def exported(**params: str) -> list:
        response = client.get(
            "/admin/api/export",
            params={"format": "json", "scope": scope, **params},
        )
        assert response.status_code == 200, response.text
        payload = json.loads(response.content)
        return payload["rows"] if isinstance(payload, dict) else payload

    if scope == "requests":
        assert len(exported(session=S1[:8])) == 3
        assert len(exported(folder="games")) == 1
        assert len(exported()) == 5
        grouped = exported(session=S1[:8], group_by="provider")
        assert sum(row["requests"] for row in grouped) == 3
    else:
        # One attempt per request: an attempt is exported exactly when the
        # request it belongs to would have been.
        assert len(exported(session=S1[:8])) == 3
        assert len(exported(folder="games")) == 1
        assert len(exported()) == 5


def test_origin_route_serves_both_breakdowns(client, seeded_store) -> None:
    payload = client.get("/admin/api/requests/origin").json()
    assert payload["enabled"] is True
    folders = {row["key"]: row for row in payload["by_folder"]}
    assert folders[DEMO]["requests"] == 3
    assert folders[GAMES]["requests"] == 1
    sessions = {row["key"]: row for row in payload["by_session"]}
    assert sessions[S1]["subagent_requests"] == 2
    assert sessions[S1]["subagents"] == 1
    assert sessions[S1]["short"] == S1[:8]
    assert payload["by_folder_truncated"] is False
    narrowed = client.get(
        "/admin/api/requests/origin", params={"folder": "games"}
    ).json()
    assert [row["key"] for row in narrowed["by_session"]] == [S2]


def test_origin_route_validates_like_its_siblings(client, seeded_store) -> None:
    assert (
        client.get("/admin/api/requests/origin", params={"local": "bogus"}).status_code
        == 422
    )
    assert (
        client.get("/admin/api/requests/origin", params={"status": "bogus"}).status_code
        == 422
    )


def test_origin_route_requires_loopback(seeded_store) -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests/origin").status_code == 403


def test_origin_route_disabled_store_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    app = create_test_app()
    disabled = TestClient(app, client=("127.0.0.1", 50000))
    assert disabled.get("/admin/api/requests/origin").json() == {"enabled": False}
