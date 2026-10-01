"""The Analytics answers the dashboard reloads, kept for up to a minute.

Measured 2026-09-28 with the Requests view open: the page reloaded nine
analytics queries up to every 3 s, and TTFT, no-answer, origin and the stats
cards were each 0.5-3.2 s of SQLite against a 10 GB log, recomputed on every
reload of every tab. The user's decision (2026-10-01): those totals may lag by
up to a minute, labelled with when they were computed. These hold that:

- a second ask inside the minute is answered without running the query again,
  and both answers carry the same ``computed_at``;
- a different filter is a different question;
- clearing the log forgets every kept answer;
- the two routes the page polls say ``x-mcc-busy: 1`` while the loop is late.
"""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.loop_health import loop_health
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    get_request_log_store,
)
from tests.api.support import create_test_app

ROUTES = {
    "/admin/api/requests/ttft": "ttft_percentiles",
    "/admin/api/requests/no-answer": "no_answer_breakdown",
    "/admin/api/requests/origin": "origin_breakdown",
    "/admin/api/requests/stats": "stats",
}


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _record(index: int) -> RequestRecord:
    return RequestRecord(
        id=f"r{index}",
        endpoint="/v1/messages",
        protocol="anthropic",
        provider="p1" if index % 2 == 0 else "p2",
        resolved_model="m1",
        ts_epoch=time.time() - 100 + index,
        status="success",
        tokens_in=10,
        tokens_out=1,
        ttft_ms=100.0 + index,
        duration_ms=500.0,
    )


@pytest.fixture
def seeded_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for index in range(4):
        store.enqueue(_record(index))
    store.close()
    yield store


@pytest.fixture
def calls(monkeypatch) -> dict[str, int]:
    """How many times each store query really ran."""

    counts: dict[str, int] = {}
    for method in ROUTES.values():
        real = getattr(RequestLogStore, method)

        def counting(self, *args: Any, _real=real, _name=method, **kwargs: Any):
            counts[_name] = counts.get(_name, 0) + 1
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(RequestLogStore, method, counting)
    return counts


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_a_second_ask_inside_the_minute_does_not_run_the_query_again(
    client, seeded_store, calls, path
) -> None:
    first = client.get(path, params={"local": "hide"})
    second = client.get(path, params={"local": "hide"})

    assert first.status_code == second.status_code == 200
    assert calls[ROUTES[path]] == 1
    one, two = first.json(), second.json()
    assert one["enabled"] is True
    assert isinstance(one["computed_at"], float)
    assert one["computed_at"] == two["computed_at"]
    assert abs(one["computed_at"] - time.time()) < 60
    # The same numbers, apart from nothing.
    assert one == two


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_a_different_filter_is_a_different_question(
    client, seeded_store, calls, path
) -> None:
    client.get(path, params={"local": "hide"})
    client.get(path, params={"local": "hide", "provider": "p2"})

    assert calls[ROUTES[path]] == 2


def test_an_answer_older_than_the_minute_is_computed_again(
    client, seeded_store, calls, monkeypatch
) -> None:
    from my_claude_code.application import derived_payloads

    client.get("/admin/api/requests/ttft")
    real = time.monotonic
    monkeypatch.setattr(
        derived_payloads.time,
        "monotonic",
        lambda: real() + derived_payloads.ANALYTICS_MAX_AGE_SECONDS + 1.0,
    )
    client.get("/admin/api/requests/ttft")

    assert calls["ttft_percentiles"] == 2


def test_the_stats_cards_keep_every_field_they_had(client, seeded_store) -> None:
    stats = client.get("/admin/api/requests/stats").json()

    for field in (
        "enabled",
        "capture_bodies",
        "harness_labels",
        "key_names",
        "retained_rows_max",
        "coverage",
        "cancelled_breakdown",
        "total",
    ):
        assert field in stats, field
    assert stats["total"] == 4


def test_clearing_the_log_forgets_every_kept_answer(
    client, seeded_store, calls
) -> None:
    before = client.get("/admin/api/requests/stats").json()
    assert before["total"] == 4

    cleared = client.delete(
        "/admin/api/requests",
        params={"confirm": "delete-all-request-log-rows"},
        # A browser sends one on every DELETE; the route refuses without it.
        headers={"Origin": "http://127.0.0.1:8082"},
    )
    assert cleared.status_code == 200

    after = client.get("/admin/api/requests/stats").json()
    assert calls["stats"] == 2
    assert after["total"] == 0


@pytest.fixture
def busy_loop():
    health = loop_health()
    health.reset()
    health.configure(interval_seconds=0.1, busy_lag_seconds=0.5)
    # A beat two seconds late: what the beat task records after a hold.
    health.beat(lag_seconds=2.0)
    assert health.snapshot().busy
    yield
    health.reset()


@pytest.mark.parametrize(
    "path", ["/admin/api/requests/in-flight", "/admin/api/requests/pulse"]
)
def test_the_polled_routes_say_busy_while_the_loop_is_late(
    client, seeded_store, busy_loop, path
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers.get("x-mcc-busy") == "1"


@pytest.mark.parametrize(
    "path", ["/admin/api/requests/in-flight", "/admin/api/requests/pulse"]
)
def test_the_polled_routes_say_nothing_extra_while_the_loop_keeps_up(
    client, seeded_store, path
) -> None:
    loop_health().reset()
    response = client.get(path)

    assert response.status_code == 200
    assert "x-mcc-busy" not in response.headers
