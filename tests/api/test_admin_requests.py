"""Admin API tests for the request log endpoints."""

import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.reasoning import ReasoningAdaptationKind
from my_claude_code.core.request_log import (
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    get_request_log_store,
)
from tests.api.support import create_test_app


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def seeded_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time()
    records = [
        RequestRecord(
            id=f"r{index}",
            endpoint="/v1/messages" if index % 2 == 0 else "/v1/responses",
            protocol="anthropic",
            provider="p1" if index % 2 == 0 else "p2",
            resolved_model="m1",
            ts_epoch=base + index,
            status="error" if index == 4 else "success",
            error_message="boom" if index == 4 else None,
            tokens_in=10 * index,
            tokens_out=index,
            duration_ms=float(100 * (index + 1)),
            input_text="in" * 3000,
            output_text="out",
        )
        for index in range(5)
    ]
    for record in records:
        store.enqueue(record)
    store.close()
    yield store


def test_list_requests_paging_and_filters(client, seeded_store) -> None:
    response = client.get("/admin/api/requests", params={"limit": 2, "offset": 0})
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["capture_bodies"] is True
    assert payload["total"] == 5
    assert [row["id"] for row in payload["rows"]] == ["r4", "r3"]

    page = client.get("/admin/api/requests", params={"limit": 2, "offset": 4}).json()
    assert [row["id"] for row in page["rows"]] == ["r0"]

    by_provider = client.get("/admin/api/requests", params={"provider": "p2"}).json()
    assert by_provider["total"] == 2

    by_status = client.get("/admin/api/requests", params={"status": "error"}).json()
    assert by_status["total"] == 1
    assert by_status["rows"][0]["id"] == "r4"

    by_endpoint = client.get(
        "/admin/api/requests", params={"endpoint": "/v1/responses"}
    ).json()
    assert by_endpoint["total"] == 2

    invalid = client.get("/admin/api/requests", params={"status": "nope"})
    assert invalid.status_code == 422


def test_list_truncates_bodies(client, seeded_store) -> None:
    payload = client.get("/admin/api/requests").json()
    row = payload["rows"][0]
    assert len(row["input_text"]) == 4096
    assert row["input_text_truncated"] is True

    full = client.get(f"/admin/api/requests/{row['id']}").json()
    assert len(full["input_text"]) == 6000
    assert full["input_text_truncated"] is False


def test_get_missing_entry_404(client, seeded_store) -> None:
    assert client.get("/admin/api/requests/nope").status_code == 404


def test_stats_endpoint(client, seeded_store) -> None:
    stats = client.get("/admin/api/requests/stats").json()
    assert stats["enabled"] is True
    assert stats["total"] == 5
    assert stats["error"] == 1
    assert stats["error_rate"] == pytest.approx(0.2)
    assert stats["tokens_in"] == 100
    assert stats["tokens_out"] == 10
    # Interpolated from the 64-bucket log histogram, so it lands inside the
    # bucket holding the exact value rather than on it. One bucket is
    # e**_LATENCY_STEP - 1 = 26.2% wide, which bounds the difference.
    assert stats["p50_duration_ms"] == pytest.approx(300.0, rel=0.262)
    assert stats["served_from"] == "rollup"
    assert {entry["key"] for entry in stats["by_provider"]} == {"p1", "p2"}
    assert stats["top_errors"] == [{"message": "boom", "count": 1}]
    assert len(stats["series"]) >= 1

    # Past the data, and past the UTC hour holding it: the rollup snaps a
    # window outward to whole hours, so a "since" inside the current hour
    # still sees that hour's rows. ``window`` reports both bounds.
    windowed = client.get(
        "/admin/api/requests/stats", params={"since": time.time() + 7200}
    ).json()
    assert windowed["total"] == 0
    assert windowed["window"]["snapped_since"] <= windowed["window"]["since"]


@pytest.mark.parametrize(
    ("params", "expected_total"),
    [
        ({"provider": "p2"}, 2),
        ({"model": "m1"}, 5),
        ({"status": "error"}, 1),
        ({"endpoint": "/v1/responses"}, 2),
        ({"q": "inin"}, 5),
    ],
)
def test_stats_endpoint_applies_request_filters(
    client, seeded_store, params, expected_total
) -> None:
    stats = client.get("/admin/api/requests/stats", params=params).json()

    assert stats["total"] == expected_total
    assert sum(entry["requests"] for entry in stats["by_provider"]) == expected_total
    assert sum(entry["requests"] for entry in stats["by_model"]) == expected_total
    assert sum(point["requests"] for point in stats["series"]) == expected_total


def test_stats_endpoint_filter_changes_cards_breakdowns_series_and_errors(
    client, seeded_store
) -> None:
    stats = client.get(
        "/admin/api/requests/stats",
        params={
            "provider": "p1",
            "model": "m1",
            "status": "error",
            "endpoint": "/v1/messages",
            "q": "inin",
        },
    ).json()

    assert stats["total"] == 1
    assert stats["success"] == 0
    assert stats["error"] == 1
    assert stats["tokens_in"] == 40
    assert stats["tokens_out"] == 4
    assert stats["p50_duration_ms"] == 500.0
    assert stats["by_provider"] == [
        {
            "key": "p1",
            "requests": 1,
            "tokens_in": 40,
            "tokens_out": 4,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_reported": 0,
            "errors": 1,
            "avg_duration_ms": 500.0,
        }
    ]
    assert stats["by_model"][0]["requests"] == 1
    assert stats["top_errors"] == [{"message": "boom", "count": 1}]
    assert sum(point["requests"] for point in stats["series"]) == 1
    assert sum(point["errors"] for point in stats["series"]) == 1


def test_stats_endpoint_rejects_invalid_status(client, seeded_store) -> None:
    response = client.get(
        "/admin/api/requests/stats", params={"status": "not-a-status"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid status filter"


def test_pulse_endpoint(client, seeded_store) -> None:
    pulse = client.get("/admin/api/requests/pulse").json()
    assert pulse["enabled"] is True
    assert pulse["total"] == 5
    assert pulse["last_ts"] is not None

    filtered = client.get("/admin/api/requests/pulse", params={"provider": "p2"}).json()
    assert filtered["total"] == 2

    windowed = client.get(
        "/admin/api/requests/pulse", params={"since": time.time() + 1000}
    ).json()
    assert windowed["total"] == 0
    assert windowed["last_ts"] is None


def test_pulse_endpoint_rejects_invalid_status(client, seeded_store) -> None:
    response = client.get(
        "/admin/api/requests/pulse", params={"status": "not-a-status"}
    )
    assert response.status_code == 422


def test_pulse_endpoint_disabled_store_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    from my_claude_code.config.settings import Settings
    from tests.api.support import create_test_app as make_app

    app = make_app(Settings())
    disabled_client = TestClient(app, client=("127.0.0.1", 50000))
    pulse = disabled_client.get("/admin/api/requests/pulse").json()
    assert pulse == {"enabled": False}


def test_stats_endpoint_flags_truncated_breakdowns(client, tmp_path) -> None:
    """A gateway with hundreds of providers must not return them all on every poll.

    ``_isolate_request_log`` (autouse, see conftest.py) points
    ``default_request_log_path`` at ``tmp_path / "requests.db"``, so writing
    through that same path is what the app's own store resolves to.
    """
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for index in range(60):
        store.enqueue(
            RequestRecord(
                id=f"p{index}",
                endpoint="/v1/messages",
                protocol="anthropic",
                provider=f"provider-{index}",
                resolved_model="m1",
                status="success",
            )
        )
    store.close()

    stats = client.get("/admin/api/requests/stats").json()
    assert stats["by_provider_truncated"] is True
    assert len(stats["by_provider"]) == 50


def test_admin_requests_loopback_guard_pulse(seeded_store) -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests/pulse").status_code == 403


def test_clear_requests(client, seeded_store) -> None:
    response = client.request("DELETE", "/admin/api/requests")
    assert response.status_code == 200
    assert response.json()["cleared"] == 5
    assert client.get("/admin/api/requests").json()["total"] == 0


def test_request_log_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    from my_claude_code.config.settings import Settings
    from tests.api.support import create_test_app as make_app

    app = make_app(Settings())
    disabled_client = TestClient(app, client=("127.0.0.1", 50000))
    payload = disabled_client.get("/admin/api/requests").json()
    assert payload["enabled"] is False
    assert payload["rows"] == []
    stats = disabled_client.get("/admin/api/requests/stats").json()
    assert stats["enabled"] is False
    cleared = disabled_client.request("DELETE", "/admin/api/requests").json()
    assert cleared["cleared"] == 0


def test_admin_requests_loopback_guard(seeded_store) -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests").status_code == 403
    assert remote.get("/admin/api/requests/stats").status_code == 403
    assert remote.get("/admin/api/requests/r0").status_code == 403
    assert remote.request("DELETE", "/admin/api/requests").status_code == 403


def test_admin_serves_only_bundled_guide_images(tmp_path) -> None:
    """The image route matches shipped names; it must not join paths."""

    from my_claude_code.api import admin_routes as ar

    names = ar._bundled_image_names()
    assert names, "guide screenshots should ship with the package"
    assert all(n.endswith(".png") for n in names)
    # Every image the guide references must actually be bundled.
    index = (Path(ar.__file__).parent / "admin_static" / "index.html").read_text(
        encoding="utf-8"
    )
    referenced = set(re.findall(r'src="/admin/img/([^"]+)"', index))
    assert referenced <= names, (
        f"guide references unbundled images: {referenced - names}"
    )
    # Traversal attempts are rejected because they are not in the name set.
    for hostile in ("../admin.js", "..\admin.js", "/etc/passwd", "nope.png"):
        assert hostile not in names


def test_lifetime_endpoint_reports_all_time_totals(client, seeded_store) -> None:
    payload = client.get("/admin/api/requests/lifetime").json()
    assert payload["enabled"] is True
    assert payload["requests"] == 5
    assert payload["error"] == 1
    assert payload["tokens_in"] == 100
    assert payload["retained_rows_max"] > 0
    assert {row["name"] for row in payload["by_provider"]} == {"p1", "p2"}


def test_lifetime_outlives_the_retention_cap(client, seeded_store) -> None:
    """The dashboard must keep counting after stored rows roll over."""
    # Exactly what prune does at the cap: the oldest rows leave the table.
    with sqlite3.connect(seeded_store.db_path) as conn:
        conn.execute("DELETE FROM requests WHERE id IN ('r0', 'r1', 'r2')")

    windowed = client.get("/admin/api/requests/stats").json()
    lifetime = client.get("/admin/api/requests/lifetime").json()

    # The stats rollup is exempt from retention for the same reason
    # ``request_totals`` is, so the window keeps its pre-prune figure instead
    # of collapsing onto whatever rows happen to survive.
    assert windowed["served_from"] == "rollup"
    assert windowed["total"] == 5
    assert lifetime["requests"] == 5


def test_stats_endpoint_exposes_the_retention_cap_and_coverage(
    client, seeded_store
) -> None:
    payload = client.get("/admin/api/requests/stats").json()
    assert payload["retained_rows_max"] > 0
    assert "coverage" in payload
    assert payload["coverage"]["tracking_since"] is not None


def test_clearing_the_log_also_clears_all_time(client, seeded_store) -> None:
    assert client.get("/admin/api/requests/lifetime").json()["requests"] == 5
    client.request("DELETE", "/admin/api/requests")
    assert client.get("/admin/api/requests/lifetime").json()["requests"] == 0


def test_lifetime_endpoint_disabled_store_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    from my_claude_code.config.settings import Settings
    from tests.api.support import create_test_app as make_app

    disabled = TestClient(make_app(Settings()), client=("127.0.0.1", 50000))
    assert disabled.get("/admin/api/requests/lifetime").json() == {"enabled": False}


def test_lifetime_endpoint_rejects_remote_callers(seeded_store) -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.get("/admin/api/requests/lifetime").status_code == 403


def test_the_list_can_page_through_every_stored_request(client, seeded_store) -> None:
    """Nothing beyond retention limits what the dashboard can show.

    The cap on stored rows is the only limit; paging must reach the last one.
    """
    seen: set[str] = set()
    offset = 0
    while True:
        page = client.get(
            "/admin/api/requests", params={"limit": 2, "offset": offset}
        ).json()
        if not page["rows"]:
            break
        seen.update(row["id"] for row in page["rows"])
        offset += 2
    assert seen == {"r0", "r1", "r2", "r3", "r4"}
    assert page["total"] == 5


def test_the_list_accepts_the_largest_page_size_the_ui_offers(
    client, seeded_store
) -> None:
    payload = client.get("/admin/api/requests", params={"limit": 500}).json()
    assert payload["limit"] == 500
    assert len(payload["rows"]) == 5


def test_the_request_detail_carries_the_credential_for_each_attempt(client) -> None:
    """A route that rotated keys used to be attributed whole to its last one."""
    store = get_request_log_store()
    assert store is not None
    store.enqueue(
        RequestRecord(
            id="rotated",
            endpoint="/v1/messages",
            protocol="anthropic",
            provider="nvidia_nim",
            resolved_model="m1",
            ts_epoch=time.time(),
            status="success",
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="nvidia_nim",
                    model_ref="nvidia_nim/m1",
                    outcome=RouteAttemptOutcome.FAILED,
                    key_index=0,
                    key_label="aa...11",
                ),
                RouteAttempt(
                    attempt=1,
                    provider="nvidia_nim",
                    model_ref="nvidia_nim/m1",
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                    key_index=1,
                    key_label="bb...22",
                ),
            ),
        )
    )
    store.close()

    detail = client.get("/admin/api/requests/rotated").json()
    first, second = detail["route_attempts"]
    assert first["key_label"] != second["key_label"]
    assert (first["key_index"], second["key_index"]) == (0, 1)


# --------------------------------------------------------------------------- #
# The reasoning adaptation kind, written once and read back for ever
# --------------------------------------------------------------------------- #


@pytest.fixture
def adaptation_store(tmp_path):
    """Two rows whose only interesting column is the adaptation kind.

    ``dropped`` is written as the bare string it was stored as before 6.6.0,
    not through the enum, because the point of the second test is that a value
    on disk is not reinterpreted by the version that reads it.
    """
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for request_id, kind, message in (
        ("nothing", ReasoningAdaptationKind.NOTHING_SENT.value, "nothing was sent."),
        ("legacy", "dropped", "the level was dropped."),
    ):
        store.enqueue(
            RequestRecord(
                id=request_id,
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="commandcode",
                resolved_model="z-ai/glm-5.3-flash",
                status="success",
                reasoning_adaptation=message,
                reasoning_adaptation_kind=kind,
            )
        )
    store.close()
    yield store


def test_a_nothing_sent_adaptation_survives_write_read_and_serialisation(
    client, adaptation_store
) -> None:
    """The new kind has to reach the dashboard spelled exactly as stored.

    The column is plain TEXT and nothing validates it against the enum, so a
    new member is only really shipped once it has been through SQLite and the
    JSON encoder unchanged -- the badge logic on the other end matches on the
    literal string, and a value mangled anywhere in between would silently
    stop matching.
    """
    detail = client.get("/admin/api/requests/nothing").json()

    assert detail["reasoning_adaptation_kind"] == "nothing_sent"
    assert detail["reasoning_adaptation"] == "nothing was sent."

    rows = client.get("/admin/api/requests").json()["rows"]
    stored = next(row for row in rows if row["id"] == "nothing")
    assert stored["reasoning_adaptation_kind"] == "nothing_sent"


def test_a_legacy_dropped_row_still_reads_back_as_dropped(
    client, adaptation_store
) -> None:
    """Historic rows are deliberately NOT migrated, and this pins that choice.

    A row means what it meant when it was written. Rewriting every stored
    ``dropped`` to ``nothing_sent`` would be a guess about which of the two
    meanings each one had -- the pre-6.6.0 server could not tell them apart,
    so neither can a migration -- and it would destroy the only record of what
    the server actually decided. The dashboard handles the ambiguity by not
    badging the value; the store handles it by leaving it alone.
    """
    detail = client.get("/admin/api/requests/legacy").json()

    assert detail["reasoning_adaptation_kind"] == "dropped"
    assert detail["reasoning_adaptation"] == "the level was dropped."


@pytest.fixture
def local_answer_store(tmp_path):
    """One upstream row, one locally answered row, one with no provider at all."""
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time()

    def row(request_id, *, provider, optimization=None, offset=0.0):
        return RequestRecord(
            id=request_id,
            endpoint="/v1/messages",
            protocol="anthropic",
            resolved_model="m1",
            status="success",
            provider=provider,
            optimization=optimization,
            ts_epoch=base + offset,
        )

    store.enqueue(row("upstream", provider="p1"))
    store.enqueue(
        row("local", provider=None, optimization="title_generation_skip", offset=1.0)
    )
    store.enqueue(row("unknown", provider=None, offset=2.0))
    store.close()
    yield store


@pytest.mark.parametrize(
    ("local", "expected"),
    [(None, 3), ("all", 3), ("hide", 2), ("only", 1)],
)
def test_the_three_local_values_and_the_unchanged_default(
    client, local_answer_store, local, expected
) -> None:
    """Absent means "all": no existing caller's numbers move."""
    params = {} if local is None else {"local": local}
    listing = client.get("/admin/api/requests", params=params).json()
    assert listing["total"] == expected
    stats = client.get("/admin/api/requests/stats", params=params).json()
    assert stats["total"] == expected
    pulse = client.get("/admin/api/requests/pulse", params=params).json()
    assert pulse["total"] == expected


def test_hide_keeps_the_row_whose_provider_is_genuinely_unknown(
    client, local_answer_store
) -> None:
    rows = client.get("/admin/api/requests", params={"local": "hide"}).json()["rows"]
    assert sorted(row["id"] for row in rows) == ["unknown", "upstream"]


@pytest.mark.parametrize(
    "path",
    ["/admin/api/requests", "/admin/api/requests/stats", "/admin/api/requests/pulse"],
)
def test_a_fourth_local_value_is_refused(client, local_answer_store, path) -> None:
    response = client.get(path, params={"local": "nope"})
    assert response.status_code == 422


def test_stats_endpoint_reports_how_it_was_served(client, seeded_store) -> None:
    """The API contract for the new ``served_from`` field."""
    rolled_up = client.get("/admin/api/requests/stats").json()
    assert rolled_up["served_from"] == "rollup"

    # Free-text search is a correlated EXISTS over compressed bodies, which is
    # not a rollup dimension and never will be, so it falls back to raw rows.
    searched = client.get("/admin/api/requests/stats", params={"q": "inin"}).json()
    assert searched["served_from"] == "rows"


# ------------------------------------------------------------------ harness --


@pytest.fixture
def harness_store(tmp_path):
    """Three agents, one of them not a registry entry at all."""
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    base = time.time()
    for index, harness in enumerate(
        ("claude", "claude", "opencode2", "claude_agent_sdk")
    ):
        store.enqueue(
            RequestRecord(
                id=f"h{index}",
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="p1",
                resolved_model="m1",
                ts_epoch=base - index,
                harness=harness,
                tokens_in=index,
                duration_ms=float(10 * (index + 1)),
            )
        )
    store.close()
    yield store


@pytest.mark.parametrize(
    "path",
    ["/admin/api/requests", "/admin/api/requests/stats", "/admin/api/requests/pulse"],
)
def test_the_three_request_routes_accept_a_harness_filter(
    client, harness_store, path
) -> None:
    """Free-form like ``provider``, not an enum like ``local``: never a 4xx."""
    response = client.get(path, params={"harness": "claude"})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    # An id nothing matches is a legitimate question with an empty answer.
    empty = client.get(path, params={"harness": "no-such-harness"})
    assert empty.status_code == 200
    assert empty.json()["total"] == 0


def test_stats_carries_the_breakdown_and_its_display_names(
    client, harness_store
) -> None:
    """The dashboard must not need a copy of the harness registry to render."""
    payload = client.get("/admin/api/requests/stats").json()
    assert {row["key"]: row["requests"] for row in payload["by_harness"]} == {
        "claude": 2,
        "opencode2": 1,
        "claude_agent_sdk": 1,
    }
    # Both vocabularies resolved: two registry ids and one that is deliberately
    # not in the registry because it is not a launchable agent.
    assert payload["harness_labels"] == {
        "claude": "Claude Code",
        "opencode2": "OpenCode 2",
        "claude_agent_sdk": "Claude Agent SDK",
    }


def test_harness_usage_returns_counts_and_labels(client, harness_store) -> None:
    payload = client.get("/admin/api/requests/harness-usage").json()
    assert payload["enabled"] is True
    assert payload["days"] == 7
    assert payload["counts"] == {"claude": 2, "opencode2": 1, "claude_agent_sdk": 1}
    assert payload["labels"]["opencode2"] == "OpenCode 2"


def test_harness_usage_clamps_its_window(client, harness_store) -> None:
    """1..90 days, so a zero or a decade cannot turn into an unbounded scan."""
    assert (
        client.get("/admin/api/requests/harness-usage", params={"days": 0}).json()[
            "days"
        ]
        == 1
    )
    assert (
        client.get("/admin/api/requests/harness-usage", params={"days": 5000}).json()[
            "days"
        ]
        == 90
    )


def test_harness_usage_is_not_shadowed_by_the_request_id_route(
    client, harness_store
) -> None:
    """Declaration order is the whole guarantee here.

    FastAPI matches in declaration order, so ``/admin/api/requests/{request_id}``
    would answer this path with a 404 for a request whose id is
    "harness-usage" if it were declared first.
    """
    response = client.get("/admin/api/requests/harness-usage")
    assert response.status_code == 200
    assert "counts" in response.json()
    # The path-parameter route still works for a real id.
    assert client.get("/admin/api/requests/h0").json()["harness"] == "claude"
