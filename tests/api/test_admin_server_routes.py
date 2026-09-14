"""The dashboard may only stop what the classifier proved stale.

The engine has been finished since 6.72.2; what these routes add is the ability
for a person to act on it. Every assertion here is about the refusals, because
the failure mode this feature has is not "did not stop something" -- it is
stopping a server somebody is using. On 2026-09-10 an investigation called two
of the user's live agent chains "abandoned" on the strength of one socket scan,
and the plan that followed would have stopped them.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_server_routes
from my_claude_code.core.server_inventory import (
    STATUS_LIVE,
    STATUS_SERVING,
    STATUS_STALE,
    ServerObservation,
)
from tests.api.support import create_test_app

NOW = 1_760_000_000.0


def _observation(
    *,
    pids: tuple[int, ...],
    status: str,
    port: int | None = 8082,
    session_id: int | None = 232,
    reason: str = "because",
) -> ServerObservation:
    return ServerObservation(
        pids=pids,
        session_pid=pids[-1],
        session_id=session_id,
        host="0.0.0.0",
        port=port,
        started_at=NOW - 900,
        last_seen_at=NOW - 5,
        status=status,
        reason=reason,
        holds=(),
    )


STALE = _observation(pids=(6764, 36856), status=STATUS_STALE, reason="superseded")
LIVE = _observation(pids=(16884, 38720), status=STATUS_LIVE, reason="heartbeating")
SERVING = _observation(pids=(55192,), status=STATUS_SERVING, reason="owns a socket")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def inventory(monkeypatch) -> list[ServerObservation]:
    """A fabricated machine, and a recorder for anything that gets signalled."""

    observations = [STALE, LIVE, SERVING]
    monkeypatch.setattr(
        admin_server_routes, "_survey", lambda settings: list(observations)
    )
    return observations


@pytest.fixture
def stopped(monkeypatch) -> list[tuple[int, ...]]:
    calls: list[tuple[int, ...]] = []

    def _stop(selected: list[ServerObservation], **_kwargs: Any):
        calls.extend(item.pids for item in selected)
        return list(selected)

    monkeypatch.setattr(admin_server_routes, "stop_stale_servers", _stop)
    return calls


class TestTheListing:
    def test_it_names_every_field_the_dialog_has_to_show(
        self, client, inventory
    ) -> None:
        """pid / session / port / started / last heartbeat, exactly."""

        with client:
            body = client.get("/admin/api/servers").json()

        assert body["stale_count"] == 1
        first = body["servers"][0]
        for key in (
            "pids",
            "session_id",
            "host",
            "port",
            "started_at",
            "last_seen_at",
            "status",
            "reason",
        ):
            assert key in first, key
        assert first["actionable"] is True
        assert isinstance(first["heartbeat_age_seconds"], float)

    def test_only_stale_is_actionable(self, client, inventory) -> None:
        with client:
            body = client.get("/admin/api/servers").json()

        actionable = {
            tuple(server["pids"]) for server in body["servers"] if server["actionable"]
        }
        assert actionable == {(6764, 36856)}
        assert body["actionable_statuses"] == ["stale"]

    def test_it_is_loopback_only(self, monkeypatch) -> None:
        remote = TestClient(create_test_app(), client=("10.0.0.9", 50000))
        with remote:
            assert remote.get("/admin/api/servers").status_code == 403


class TestTheStop:
    def test_a_stale_pid_set_is_stopped(self, client, inventory, stopped) -> None:
        with client:
            body = client.post(
                "/admin/api/servers/stop", json={"pids": [[6764, 36856]]}
            ).json()

        assert stopped == [(6764, 36856)]
        assert [tuple(item["pids"]) for item in body["stopped"]] == [(6764, 36856)]
        assert body["refused"] == []

    def test_a_live_pid_set_is_refused_even_when_asked_for_by_pid(
        self, client, inventory, stopped
    ) -> None:
        """THE assertion. The client can name it; the server still says no."""

        with client:
            body = client.post(
                "/admin/api/servers/stop", json={"pids": [[16884, 38720]]}
            ).json()

        assert stopped == []
        assert body["stopped"] == []
        assert [tuple(item["pids"]) for item in body["refused"]] == [(16884, 38720)]
        assert "not stale" in body["refused"][0]["reason"]

    def test_a_serving_pid_set_is_refused_too(self, client, inventory, stopped) -> None:
        with client:
            body = client.post(
                "/admin/api/servers/stop", json={"pids": [[55192]]}
            ).json()

        assert stopped == []
        assert body["refused"][0]["status"] == STATUS_SERVING

    def test_a_mixed_request_stops_only_the_stale_half(
        self, client, inventory, stopped
    ) -> None:
        with client:
            body = client.post(
                "/admin/api/servers/stop",
                json={"pids": [[6764, 36856], [16884, 38720], [55192]]},
            ).json()

        assert stopped == [(6764, 36856)]
        assert len(body["refused"]) == 2

    def test_a_pid_set_that_no_longer_matches_is_never_signalled(
        self, client, inventory, stopped
    ) -> None:
        """A pid is a reusable number, and a subset is not the chain.

        Asking for part of a stale chain, or for a chain that has since exited,
        must signal nothing at all -- not "the closest match".
        """

        with client:
            body = client.post(
                "/admin/api/servers/stop", json={"pids": [[6764], [99999]]}
            ).json()

        assert stopped == []
        assert {tuple(item["pids"]) for item in body["refused"]} == {
            (6764,),
            (99999,),
        }
        for item in body["refused"]:
            assert "no MCC server with exactly these pids" in item["reason"]

    def test_an_empty_request_is_a_client_error_not_a_sweep(
        self, client, inventory, stopped
    ) -> None:
        """The one shape that must never be read as "all of them"."""

        with client:
            empty = client.post("/admin/api/servers/stop", json={"pids": []})
            blanks = client.post("/admin/api/servers/stop", json={"pids": [[]]})

        assert empty.status_code == 400
        assert blanks.status_code == 400
        assert stopped == []

    def test_the_response_carries_a_fresh_listing(
        self, client, inventory, stopped
    ) -> None:
        with client:
            body = client.post(
                "/admin/api/servers/stop", json={"pids": [[6764, 36856]]}
            ).json()

        assert len(body["servers"]) == 3
        assert body["stale_count"] == 1

    def test_it_is_loopback_only(self, inventory, stopped) -> None:
        remote = TestClient(create_test_app(), client=("10.0.0.9", 50000))
        with remote:
            response = remote.post(
                "/admin/api/servers/stop", json={"pids": [[6764, 36856]]}
            )

        assert response.status_code == 403
        assert stopped == []
