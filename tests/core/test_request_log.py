"""Unit tests for the SQLite request log store."""

import gc
import hashlib
import sqlite3
import time
from compression import zstd
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    LIST_BODY_PREVIEW_CHARS,
    MAX_ERROR_CHARS,
    MAX_TEXT_CHARS,
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    compact_request_log,
    get_request_log_store,
    pack_bodies,
    reset_request_log_stores,
)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    yield store
    store.close()


def _record(request_id: str, **overrides) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "tokens_in": 10,
        "tokens_out": 20,
        "ttft_ms": 12.5,
        "duration_ms": 120.0,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def test_enqueue_persists_record(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    row = store.get_request("r1")
    assert row is not None
    assert row["provider"] == "nvidia_nim"
    assert row["stream"] is True
    assert row["tokens_in"] == 10
    assert row["params"] is None
    assert row["ts_iso"].endswith("+00:00")


def test_close_flushes_and_is_idempotent(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    store.close()
    _, total = store.list_requests()
    assert total == 1


def test_list_paging_and_order(store: RequestLogStore) -> None:
    base = time.time()
    for index in range(5):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    rows, total = store.list_requests(limit=2, offset=0)
    assert total == 5
    assert [row["id"] for row in rows] == ["r4", "r3"]
    rows, _ = store.list_requests(limit=2, offset=4)
    assert [row["id"] for row in rows] == ["r0"]


def test_list_filters(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(
        _record(
            "a", provider="p1", resolved_model="m1", status="success", ts_epoch=base
        )
    )
    store.enqueue(
        _record(
            "b", provider="p2", resolved_model="m2", status="error", ts_epoch=base + 10
        )
    )
    store.enqueue(
        _record(
            "c",
            provider="p1",
            resolved_model="m2",
            status="cancelled",
            endpoint="/v1/responses",
            ts_epoch=base + 20,
        )
    )
    store.close()
    rows, total = store.list_requests(provider="p1")
    assert total == 2
    assert {row["id"] for row in rows} == {"a", "c"}
    _, total = store.list_requests(model="m2")
    assert total == 2
    _, total = store.list_requests(status="error")
    assert total == 1
    _, total = store.list_requests(endpoint="/v1/responses")
    assert total == 1
    _, total = store.list_requests(since=base + 5, until=base + 15)
    assert total == 1


def test_list_multi_value_filters(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(
        _record(
            "a", provider="p1", resolved_model="m1", status="success", ts_epoch=base
        )
    )
    store.enqueue(
        _record(
            "b",
            provider="p2",
            resolved_model="m2",
            status="success",
            ts_epoch=base + 10,
        )
    )
    store.enqueue(
        _record(
            "c",
            provider="p3",
            resolved_model="m3",
            status="success",
            ts_epoch=base + 20,
        )
    )
    store.close()
    # Comma-separated provider means "any of these".
    _, total = store.list_requests(provider="p1,p3")
    assert total == 2
    # Comma-separated model matches either resolved_model or requested_model.
    _, total = store.list_requests(model="m1,m2")
    assert total == 2
    # Combined provider + model intersection.
    _, total = store.list_requests(provider="p1,p2", model="m2")
    assert total == 1
    # A single value still behaves like a scalar equality.
    _, total = store.list_requests(provider="p1")
    assert total == 1


def test_list_text_search(store: RequestLogStore) -> None:
    store.enqueue(
        _record("a", input_text="deploy the kubernetes cluster", output_text="done")
    )
    store.enqueue(_record("b", input_text="hello", output_text="kubernetes is complex"))
    store.enqueue(_record("c", input_text="hello", output_text="world"))
    store.close()
    rows, total = store.list_requests(q="kubernetes")
    assert total == 2
    assert {row["id"] for row in rows} == {"a", "b"}
    _, total = store.list_requests(q="KUBERNETES")
    assert total == 2  # SQLite LIKE is case-insensitive for ASCII
    _, total = store.list_requests(q="missing-text")
    assert total == 0
    _, total = store.list_requests(q="hello", provider="nvidia_nim")
    assert total == 2  # matches b and c, combined with the provider filter


def test_list_truncates_bodies_but_get_returns_full(store: RequestLogStore) -> None:
    long_text = "x" * (LIST_BODY_PREVIEW_CHARS + 100)
    store.enqueue(_record("r1", input_text=long_text, output_text=long_text))
    store.close()
    rows, _ = store.list_requests()
    assert len(rows[0]["input_text"]) == LIST_BODY_PREVIEW_CHARS
    assert rows[0]["input_text_truncated"] is True
    full = store.get_request("r1")
    assert full is not None
    assert len(full["input_text"]) == LIST_BODY_PREVIEW_CHARS + 100
    assert full["input_text_truncated"] is False


def test_get_missing_returns_none(store: RequestLogStore) -> None:
    assert store.get_request("nope") is None


def test_stats_aggregates(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(
        _record("s1", ts_epoch=base, duration_ms=100.0, tokens_in=5, tokens_out=7)
    )
    store.enqueue(
        _record(
            "s2",
            ts_epoch=base + 3600,
            duration_ms=300.0,
            status="error",
            error_kind="rate_limit",
            error_message="slow down",
            tokens_in=15,
            tokens_out=1,
        )
    )
    store.enqueue(
        _record(
            "s3",
            ts_epoch=base + 7200,
            duration_ms=None,
            status="cancelled",
            tokens_in=None,
            tokens_out=None,
        )
    )
    store.close()
    stats = store.stats()
    assert stats["total"] == 3
    assert stats["success"] == 1
    assert stats["error"] == 1
    assert stats["cancelled"] == 1
    assert stats["error_rate"] == pytest.approx(1 / 3)
    assert stats["tokens_in"] == 20
    assert stats["tokens_out"] == 8
    assert stats["avg_duration_ms"] == pytest.approx(200.0)
    # p50/p95 are interpolated from the 64-bucket latency histogram now.
    # The exact interpolation is still computed, and still pinned, on the
    # raw-row path -- which is what a free-text search and a pre-backfill
    # window use. On a fixture this small the two legitimately disagree by
    # a lot: the histogram places the rank inside a log bucket, and with
    # two samples there is nothing in the bucket to interpolate against.
    exact = store._stats_from_rows()
    assert exact["p50_duration_ms"] == pytest.approx(200.0)
    assert exact["p95_duration_ms"] == pytest.approx(290.0)
    assert stats["by_provider"][0]["key"] == "nvidia_nim"
    assert stats["by_provider"][0]["requests"] == 3
    assert stats["by_provider"][0]["errors"] == 1
    assert stats["by_model"][0]["tokens_out"] == 8
    assert stats["top_errors"] == [{"message": "slow down", "count": 1}]
    # 2h window -> hourly buckets
    assert len(stats["series"]) == 3
    assert "T" in stats["series"][0]["bucket"]


def test_stats_window_filter(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(_record("old", ts_epoch=base - 3 * 86400))
    store.enqueue(_record("new", ts_epoch=base))
    store.close()
    stats = store.stats(since=base - 10)
    assert stats["total"] == 1
    daily = store.stats()
    assert daily["total"] == 2
    assert all("T" not in point["bucket"] for point in daily["series"])


def test_stats_applies_all_list_filters_to_every_aggregate(
    store: RequestLogStore,
) -> None:
    base = time.time()
    store.enqueue(
        _record(
            "match-error",
            provider="selected",
            requested_model="requested-match",
            resolved_model="resolved-other",
            endpoint="/v1/responses",
            ts_epoch=base,
            status="error",
            input_text="needle in input",
            output_text="ignored",
            tokens_in=7,
            tokens_out=3,
            duration_ms=40.0,
            ttft_ms=8.0,
            error_message="selected failure",
        )
    )
    store.enqueue(
        _record(
            "match-success",
            provider="selected",
            requested_model="requested-match",
            resolved_model="resolved-other",
            endpoint="/v1/responses",
            ts_epoch=base + 60,
            input_text="ignored",
            output_text="needle in output",
            tokens_in=11,
            tokens_out=5,
            duration_ms=80.0,
            ttft_ms=12.0,
        )
    )
    store.enqueue(
        _record(
            "wrong-provider",
            provider="other",
            requested_model="requested-match",
            endpoint="/v1/responses",
            ts_epoch=base + 120,
            status="error",
            input_text="needle",
            error_message="unselected failure",
        )
    )
    store.enqueue(
        _record(
            "wrong-model",
            provider="selected",
            requested_model="different",
            resolved_model="different",
            endpoint="/v1/responses",
            ts_epoch=base + 180,
            input_text="needle",
        )
    )
    store.enqueue(
        _record(
            "wrong-endpoint",
            provider="selected",
            requested_model="requested-match",
            endpoint="/v1/messages",
            ts_epoch=base + 240,
            input_text="needle",
        )
    )
    store.enqueue(
        _record(
            "outside-window",
            provider="selected",
            requested_model="requested-match",
            endpoint="/v1/responses",
            ts_epoch=base + 3600,
            input_text="needle",
        )
    )
    store.close()

    stats = store.stats(
        provider="selected",
        model="requested-match",
        endpoint="/v1/responses",
        since=base - 1,
        until=base + 300,
        q="needle",
    )

    assert stats["total"] == 2
    assert stats["success"] == 1
    assert stats["error"] == 1
    assert stats["cancelled"] == 0
    assert stats["error_rate"] == pytest.approx(0.5)
    assert stats["tokens_in"] == 18
    assert stats["tokens_out"] == 8
    assert stats["avg_duration_ms"] == pytest.approx(60.0)
    assert stats["p50_duration_ms"] == pytest.approx(60.0)
    assert stats["p95_duration_ms"] == pytest.approx(78.0)
    assert stats["avg_ttft_ms"] == pytest.approx(10.0)
    assert stats["by_provider"] == [
        {
            "key": "selected",
            "requests": 2,
            "tokens_in": 18,
            "tokens_out": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_reported": 0,
            "errors": 1,
            "avg_duration_ms": 60.0,
        }
    ]
    assert stats["by_model"] == [
        {
            "key": "resolved-other",
            "requests": 2,
            "tokens_in": 18,
            "tokens_out": 8,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_reported": 0,
            "errors": 1,
            "avg_duration_ms": 60.0,
        }
    ]
    assert stats["top_errors"] == [{"message": "selected failure", "count": 1}]
    assert sum(point["requests"] for point in stats["series"]) == 2
    assert sum(point["tokens"] for point in stats["series"]) == 26
    assert sum(point["errors"] for point in stats["series"]) == 1


def test_stats_status_filter_changes_cards_breakdowns_errors_and_series(
    store: RequestLogStore,
) -> None:
    base = time.time()
    store.enqueue(_record("success", ts_epoch=base, duration_ms=10.0))
    store.enqueue(
        _record(
            "error",
            ts_epoch=base + 1,
            status="error",
            duration_ms=90.0,
            error_message="boom",
        )
    )
    store.close()

    stats = store.stats(status="success")

    assert stats["total"] == 1
    assert stats["success"] == 1
    assert stats["error"] == 0
    assert stats["error_rate"] == 0.0
    # p50/p95 are interpolated from the 64-bucket latency histogram now.
    # The exact interpolation is still computed, and still pinned, on the
    # raw-row path -- which is what a free-text search and a pre-backfill
    # window use. On a fixture this small the two legitimately disagree by
    # a lot: the histogram places the rank inside a log bucket, and with
    # two samples there is nothing in the bucket to interpolate against.
    assert store._stats_from_rows(status="success")["p50_duration_ms"] == 10.0
    assert stats["by_provider"][0]["requests"] == 1
    assert stats["by_provider"][0]["errors"] == 0
    assert stats["top_errors"] == []
    assert stats["series"][0]["requests"] == 1
    assert stats["series"][0]["errors"] == 0


def test_prune_keeps_newest(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=3)
    base = time.time()
    for index in range(6):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    deleted = store.prune()
    assert deleted == 3
    rows, total = store.list_requests(limit=10)
    assert total == 3
    assert [row["id"] for row in rows] == ["r5", "r4", "r3"]
    store.close()


def test_clear(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    assert store.clear() == 1
    _, total = store.list_requests()
    assert total == 0


def test_error_message_capped(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", status="error", error_message="e" * 5000))
    store.close()
    row = store.get_request("r1")
    assert row is not None
    assert len(row["error_message"]) == 2000


def _live_connection_count() -> int:
    return sum(1 for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection))


def test_enqueue_caps_bodies_before_queueing(store: RequestLogStore) -> None:
    """Oversized bodies must be capped before the record reaches the queue."""
    record = _record(
        "r1",
        input_text="i" * (MAX_TEXT_CHARS + 500),
        output_text="o" * (MAX_TEXT_CHARS + 500),
        status="error",
        error_message="e" * (MAX_ERROR_CHARS + 500),
    )
    store.enqueue(record)
    # ``enqueue`` caps in place, so the queued object itself is already bounded
    # rather than holding the full body until the writer flushes it.
    assert record.input_text is not None
    assert record.output_text is not None
    assert record.error_message is not None
    assert len(record.input_text) == MAX_TEXT_CHARS
    assert len(record.output_text) == MAX_TEXT_CHARS
    assert len(record.error_message) == MAX_ERROR_CHARS


def test_read_paths_close_connections(store: RequestLogStore) -> None:
    """Read paths must not accumulate connections between GC passes."""
    store.enqueue(_record("r1"))
    store.close()
    gc.collect()
    gc.disable()
    try:
        before = _live_connection_count()
        for _ in range(25):
            store.list_requests()
            store.get_request("r1")
        after = _live_connection_count()
    finally:
        gc.enable()
    assert after == before


def _auto_vacuum_mode(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    finally:
        conn.close()


def test_auto_vacuum_becomes_incremental(tmp_path) -> None:
    """The store must end up on incremental auto-vacuum.

    A populated database is converted by the writer thread rather than during
    construction, so poll instead of asserting immediately.
    """
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _auto_vacuum_mode(store.db_path) == 2:
                break
            time.sleep(0.05)
        assert _auto_vacuum_mode(store.db_path) == 2
    finally:
        store.close()


def test_stats_covering_index_is_created(tmp_path) -> None:
    """Aggregates must be able to run index-only, without touching bodies."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        deadline = time.monotonic() + 10.0
        plan: list[str] = []
        while time.monotonic() < deadline:
            conn = sqlite3.connect(store.db_path)
            try:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                if "idx_requests_stats_v4" in names:
                    plan = [
                        str(row[3])
                        for row in conn.execute(
                            "EXPLAIN QUERY PLAN SELECT COUNT(*),"
                            " AVG(duration_ms) FROM requests"
                        )
                    ]
                    break
            finally:
                conn.close()
            time.sleep(0.05)
        assert plan, "covering index was never created"
        assert any("idx_requests_stats_v4" in step for step in plan), plan
    finally:
        store.close()


def test_construction_does_not_block_on_vacuum(tmp_path) -> None:
    """Converting a large database must not happen on the caller's thread."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=50_000)
    body = "x" * 20_000
    for index in range(300):
        seed.enqueue(_record(f"s{index}", input_text=body, output_text=body))
    seed.close()
    # Force the legacy (non-incremental) layout the conversion has to fix.
    conn = sqlite3.connect(path)
    try:
        conn.isolation_level = None
        conn.execute("PRAGMA auto_vacuum=NONE")
        conn.execute("VACUUM")
    finally:
        conn.close()
    assert _auto_vacuum_mode(path) == 0

    started = time.perf_counter()
    store = RequestLogStore(path, max_rows=50_000)
    construction_seconds = time.perf_counter() - started
    try:
        assert construction_seconds < 1.0
    finally:
        store.close()


def test_prune_reclaims_file_space(tmp_path) -> None:
    """Repeated insert/prune cycles must not grow the file without bound."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        body = "x" * 10_000
        sizes = []
        for cycle in range(4):
            for index in range(40):
                store.enqueue(
                    _record(f"c{cycle}-{index}", input_text=body, output_text=body)
                )
            store.prune()
            sizes.append(store.db_path.stat().st_size)
    finally:
        store.close()
    # Later cycles must not keep ratcheting the file upward.
    assert sizes[-1] <= sizes[0] * 2


def test_percentiles_on_empty_table_are_none(store: RequestLogStore) -> None:
    """No rows at all must not error the new rank-lookup path."""
    stats = store.stats()
    assert stats["total"] == 0
    assert stats["p50_duration_ms"] is None
    assert stats["p95_duration_ms"] is None


def test_percentiles_ignore_rows_without_duration(store: RequestLogStore) -> None:
    """Rows with duration_ms IS NULL must not shift the rank computation."""
    store.enqueue(_record("no-duration", duration_ms=None))
    store.enqueue(_record("has-duration", duration_ms=42.0))
    store.close()
    stats = store.stats()
    assert stats["total"] == 2
    # p50/p95 are interpolated from the 64-bucket latency histogram now.
    # The exact interpolation is still computed, and still pinned, on the
    # raw-row path -- which is what a free-text search and a pre-backfill
    # window use. On a fixture this small the two legitimately disagree by
    # a lot: the histogram places the rank inside a log bucket, and with
    # two samples there is nothing in the bucket to interpolate against.
    exact = store._stats_from_rows()
    assert exact["p50_duration_ms"] == pytest.approx(42.0)
    assert exact["p95_duration_ms"] == pytest.approx(42.0)


def test_percentiles_match_old_interpolation_unfiltered_and_filtered(
    store: RequestLogStore,
) -> None:
    """Pin the exact interpolation on the raw-row path.

    Asserted against ``_stats_from_rows`` deliberately: this is the
    oracle the bucketed histogram is measured against, so repointing it
    at ``stats()`` would delete the only exact percentile in the suite.

    Expected values are hand-computed with the same formula the removed
    ``_percentile`` used: ``position = fraction * (n - 1)``, interpolating
    between the floor and ceiling ranks.
    """
    # provider "a": durations 10, 50, 90 (n=3) -> p50 index 1.0 = 50;
    #   p95 position 1.9 interpolates rank1=50 and rank2=90: 50+40*0.9=86.0
    store.enqueue(_record("a1", provider="a", duration_ms=10.0))
    store.enqueue(_record("a2", provider="a", duration_ms=90.0))
    store.enqueue(_record("a3", provider="a", duration_ms=50.0))
    # provider "b": durations 20, 40 -- combine with "a" for the unfiltered set.
    store.enqueue(_record("b1", provider="b", duration_ms=20.0))
    store.enqueue(_record("b2", provider="b", duration_ms=40.0))
    store.close()

    # Unfiltered: combined sorted durations are 10, 20, 40, 50, 90 (n=5).
    # p50 position 2.0 = index 2 = 40; p95 position 3.8 interpolates
    # rank3=50 and rank4=90: 50+40*0.8=82.0. This path uses the index-seek
    # branch of ``_percentiles`` (no WHERE clause).
    unfiltered = store._stats_from_rows()
    assert unfiltered["p50_duration_ms"] == pytest.approx(40.0)
    assert unfiltered["p95_duration_ms"] == pytest.approx(82.0)

    # Filtered: this path uses the single-sort branch of ``_percentiles``
    # (a WHERE clause is present), which must still match the same formula.
    filtered = store._stats_from_rows(provider="a")
    assert filtered["p50_duration_ms"] == pytest.approx(50.0)
    assert filtered["p95_duration_ms"] == pytest.approx(86.0)


def test_stats_cache_evicts_least_recently_used(tmp_path) -> None:
    """The stats cache must be bounded rather than growing without limit."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    try:
        store.enqueue(_record("r1"))
        store.close()
        max_entries = request_log_module._STATS_CACHE_MAX_ENTRIES
        # Fill the cache with distinct filter combinations, one past capacity.
        for index in range(max_entries + 1):
            store.stats(provider=f"provider-{index}")
        with store._stats_lock:
            assert len(store._stats_cache) == max_entries
            # The oldest key (provider-0) was evicted; the newest survives.
            # The key is the whole filter tuple, so its arity is pinned here on
            # purpose: a filter added to `stats()` and forgotten in the key
            # would serve one question's numbers as another's.
            empty = (None,) * 9
            assert ("provider-0", *empty) not in store._stats_cache
            assert (f"provider-{max_entries}", *empty) in store._stats_cache
    finally:
        store.close()


def test_breakdown_truncation_flag(tmp_path) -> None:
    """A breakdown beyond the cap must be truncated with a visible flag."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    try:
        limit = request_log_module._BREAKDOWN_LIMIT
        for index in range(limit + 5):
            store.enqueue(_record(f"r{index}", provider=f"provider-{index}"))
        store.close()
        stats = store.stats()
        assert len(stats["by_provider"]) == limit
        assert stats["by_provider_truncated"] is True
        # Untruncated breakdowns still report the flag as False, not absent.
        assert stats["by_model_truncated"] is False
    finally:
        store.close()


def test_pulse_reports_total_and_last_ts(store: RequestLogStore) -> None:
    base = time.time()
    store.enqueue(_record("r1", ts_epoch=base))
    store.enqueue(_record("r2", ts_epoch=base + 10))
    store.close()
    pulse = store.pulse()
    assert pulse["total"] == 2
    assert pulse["last_ts"] == pytest.approx(base + 10)


def test_pulse_on_empty_table(store: RequestLogStore) -> None:
    pulse = store.pulse()
    assert pulse == {"total": 0, "last_ts": None}


def test_pulse_applies_filters(store: RequestLogStore) -> None:
    store.enqueue(_record("a", provider="p1"))
    store.enqueue(_record("b", provider="p2"))
    store.close()
    assert store.pulse(provider="p1")["total"] == 1
    assert store.pulse(provider="missing")["total"] == 0


def test_stats_are_cached_within_ttl(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    first = store.stats()
    assert first["total"] == 1
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol, status)"
            " VALUES ('r2', ?, '2024-01-01T00:00:00+00:00', '/v1/messages',"
            " 'anthropic', 'success')",
            (time.time(),),
        )
    assert store.stats()["total"] == 1  # served from the short-lived cache
    # Mutating a returned payload must not corrupt the cached copy.
    store.stats()["enabled"] = True
    assert "enabled" not in store.stats()


def test_shared_store_registry(tmp_path) -> None:
    path = tmp_path / "shared.db"
    first = get_request_log_store(path)
    assert get_request_log_store(path) is first
    assert get_request_log_store(path, enabled=False) is None
    reset_request_log_stores()
    assert get_request_log_store(path) is not first
    reset_request_log_stores()


_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, ts_epoch REAL NOT NULL, ts_iso TEXT NOT NULL,
    endpoint TEXT NOT NULL, protocol TEXT NOT NULL, requested_model TEXT,
    provider TEXT, resolved_model TEXT, stream INTEGER NOT NULL DEFAULT 0,
    input_text TEXT, output_text TEXT, input_sha256 TEXT, output_sha256 TEXT,
    input_chars INTEGER, output_chars INTEGER, reasoning TEXT, params TEXT,
    tokens_in INTEGER, tokens_out INTEGER, ttft_ms REAL, duration_ms REAL,
    status TEXT NOT NULL, error_kind TEXT, error_message TEXT, headers TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_stats ON requests(
    ts_epoch, status, provider, resolved_model, endpoint,
    requested_model, duration_ms, ttft_ms, tokens_in, tokens_out);
"""


def _indexes(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        conn.close()


def test_key_attribution_round_trips(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", key_index=1, key_label="abcd…wxyz"))
    store.close()
    row = store.get_request("r1")
    assert row is not None
    assert row["key_index"] == 1
    assert row["key_label"] == "abcd…wxyz"


def test_list_filters_and_aggregates_by_key(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", key_index=0, key_label="aaaa…1111"))
    store.enqueue(_record("r2", key_index=0, key_label="aaaa…1111"))
    store.enqueue(_record("r3", key_index=1, key_label="bbbb…2222"))
    store.close()

    rows, total = store.list_requests(key="aaaa…1111")
    assert total == 2
    assert {row["id"] for row in rows} == {"r1", "r2"}
    assert all(row["key_label"] == "aaaa…1111" for row in rows)

    by_key = {entry["key"]: entry for entry in store.stats()["by_key"]}
    assert by_key["aaaa…1111"]["requests"] == 2
    assert by_key["bbbb…2222"]["requests"] == 1


def test_stats_key_filter_narrows_totals(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", key_index=0, key_label="aaaa…1111"))
    store.enqueue(_record("r2", key_index=1, key_label="bbbb…2222"))
    store.close()
    assert store.stats()["total"] == 2
    assert store.stats(key="bbbb…2222")["total"] == 1


def test_migrates_a_database_created_before_key_columns(tmp_path) -> None:
    """An existing log must gain the key columns without losing its rows."""
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
            " status, provider, tokens_in, tokens_out)"
            " VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
            " 'success', 'nvidia_nim', 5, 7)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    store = RequestLogStore(db_path, max_rows=100)
    try:
        store.enqueue(_record("fresh", key_index=0, key_label="cccc…3333"))
        store.close()

        legacy = store.get_request("legacy")
        assert legacy is not None
        assert legacy["key_label"] is None

        fresh = store.get_request("fresh")
        assert fresh is not None
        assert fresh["key_label"] == "cccc…3333"

        indexes = _indexes(db_path)
        assert "idx_requests_key" in indexes
        # The pre-existing covering index lacked key_label, so it must be
        # replaced rather than silently kept by CREATE INDEX IF NOT EXISTS.
        assert "idx_requests_stats" not in indexes
        assert "idx_requests_stats_v3" not in indexes
        assert "idx_requests_stats_v4" in indexes
    finally:
        store.close()


def test_key_breakdown_labels_unattributed_rows(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    by_key = {entry["key"] for entry in store.stats()["by_key"]}
    assert by_key == {"(unknown)"}


class TestCacheTokenAnalytics:
    """Cached prompt tokens are billed differently; they need their own columns."""

    def test_totals_and_breakdowns_report_cache_tokens(self, tmp_path) -> None:
        store = RequestLogStore(tmp_path / "requests.db")
        store.enqueue(
            _record(
                "a",
                provider="nvidia_nim",
                tokens_in=100,
                tokens_out=10,
                cache_read_tokens=900,
                cache_write_tokens=0,
            )
        )
        store.close()

        store = RequestLogStore(tmp_path / "requests.db")
        stats = store.stats()
        assert stats["cache_read_tokens"] == 900
        assert stats["cache_write_tokens"] == 0
        # tokens_in stays the *uncached* portion, matching Anthropic's usage
        # semantics -- summing it with cache reads would double count.
        assert stats["tokens_in"] == 100

        (provider,) = [r for r in stats["by_provider"] if r["key"] == "nvidia_nim"]
        assert provider["tokens_in"] == 100
        assert provider["tokens_out"] == 10
        assert provider["cache_read_tokens"] == 900
        store.close()

    def test_columns_are_added_to_a_database_created_before_them(
        self, tmp_path
    ) -> None:
        """Existing installs must migrate in place, not lose their history."""

        db_path = tmp_path / "requests.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_OLD_SCHEMA)
            conn.execute(
                "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
                " status, provider, tokens_in, tokens_out)"
                " VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
                " 'success', 'nvidia_nim', 5, 7)",
                (time.time(),),
            )
            conn.commit()
        finally:
            conn.close()

        store = RequestLogStore(db_path)
        store.enqueue(_record("fresh", provider="nvidia_nim", cache_read_tokens=7))
        store.close()

        with sqlite3.connect(db_path) as conn:
            rows = dict(
                conn.execute("SELECT id, cache_read_tokens FROM requests").fetchall()
            )
        assert rows["legacy"] is None  # pre-existing row survives, value unset
        assert rows["fresh"] == 7


def test_cache_reported_distinguishes_unsupported_from_zero(tmp_path) -> None:
    """A provider that never reports caching must not look like 0% caching."""

    store = RequestLogStore(tmp_path / "requests.db")
    store.enqueue(_record("silent", provider="nvidia_nim"))  # no cache fields
    store.enqueue(
        _record("reports", provider="deepseek", tokens_in=10, cache_read_tokens=0)
    )
    store.close()

    store = RequestLogStore(tmp_path / "requests.db")
    rows = {r["key"]: r for r in store.stats()["by_provider"]}
    # nvidia_nim said nothing about caching at all...
    assert rows["nvidia_nim"]["cache_reported"] == 0
    # ...whereas deepseek actively reported zero cached tokens.
    assert rows["deepseek"]["cache_reported"] == 1
    store.close()


def test_route_trace_round_trips_chain_attempt_and_diversion(
    store: RequestLogStore,
) -> None:
    """The whole routing decision, not just which model happened to answer.

    Nothing asserted any of this reaching storage before, which is how a vision
    diversion stayed invisible in the log for three releases: a diverted
    request looked identical to a route pointing at the adapter model.
    """
    store.enqueue(
        _record(
            "r_fallback",
            provider="opencode",
            resolved_model="deepseek-v4-flash-free",
            route_attempt=1,
            route_primary_model="nous_portal/tencent/hy3:free",
            route_chain=(
                "nous_portal/tencent/hy3:free,opencode/deepseek-v4-flash-free"
            ),
        )
    )
    store.enqueue(
        _record(
            "r_vision",
            provider="chatgpt_oauth",
            resolved_model="gpt-5.6-luna",
            route_attempt=0,
            route_chain="chatgpt_oauth/gpt-5.6-luna",
            route_diverted_from="nous_portal/tencent/hy3:free",
            route_diversion="vision",
        )
    )
    store.enqueue(_record("r_plain", route_attempt=0, route_chain="nvidia_nim/a"))
    store.close()

    fallback = store.get_request("r_fallback")
    assert fallback is not None
    assert fallback["route_attempt"] == 1
    assert fallback["route_chain"] == (
        "nous_portal/tencent/hy3:free,opencode/deepseek-v4-flash-free"
    )
    assert fallback["route_diversion"] is None

    vision = store.get_request("r_vision")
    assert vision is not None
    assert vision["route_diversion"] == "vision"
    assert vision["route_diverted_from"] == "nous_portal/tencent/hy3:free"
    assert vision["route_attempt"] == 0

    stats = store.stats()
    assert stats["served_by_fallback"] == 1
    assert stats["diverted"] == 1
    assert stats["fallback_routes"] == [
        {
            "primary": "nous_portal/tencent/hy3:free",
            "served_by": "opencode/deepseek-v4-flash-free",
            "count": 1,
        }
    ]
    assert stats["diverted_routes"] == [
        {
            "diverted_from": "nous_portal/tencent/hy3:free",
            "reason": "vision",
            "served_by": "chatgpt_oauth/gpt-5.6-luna",
            "count": 1,
        }
    ]


def test_route_trace_columns_are_added_to_a_pre_existing_database(tmp_path) -> None:
    """Live databases are 1.7 GB and predate every one of these columns."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old"))
    seed.close()

    with sqlite3.connect(path) as conn:
        for column in ("route_chain", "route_diverted_from", "route_diversion"):
            conn.execute(f"ALTER TABLE requests DROP COLUMN {column}")

    reopened = RequestLogStore(path, max_rows=100)
    reopened.enqueue(_record("new", route_chain="a/b,c/d", route_diversion="vision"))
    reopened.close()

    old_row = reopened.get_request("old")
    new_row = reopened.get_request("new")
    assert old_row is not None and new_row is not None
    assert old_row["route_chain"] is None
    assert new_row["route_chain"] == "a/b,c/d"


# --------------------------------------------------------- lifetime totals ---


def test_lifetime_totals_survive_the_retention_cap(tmp_path) -> None:
    """The bug this table exists for.

    Every figure a raw scan produces is a sum over ``requests``, which
    ``prune`` caps. Once the cap is reached one row leaves for each one that
    arrives, so those sums stop moving however much traffic runs. The all-time
    counters must not -- and neither must the stats rollup, which is exempt
    from retention for exactly the same reason.
    """
    store = RequestLogStore(tmp_path / "requests.db", max_rows=3)
    base = time.time()
    for index in range(10):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    store.prune()

    windowed = store._stats_from_rows()
    rolled_up = store.stats()
    lifetime = store.lifetime()

    assert windowed["total"] == 3
    assert windowed["tokens_in"] == 30
    assert rolled_up["served_from"] == "rollup"
    assert rolled_up["total"] == 10
    assert rolled_up["tokens_in"] == 100
    assert lifetime["requests"] == 10
    assert lifetime["tokens_in"] == 100
    assert lifetime["tokens_out"] == 200


def test_lifetime_breaks_down_by_provider_and_model(store: RequestLogStore) -> None:
    store.enqueue(_record("a", provider="nous_portal", resolved_model="hy3"))
    store.enqueue(_record("b", provider="nous_portal", resolved_model="hy3"))
    store.enqueue(_record("c", provider="open_router", resolved_model="other"))
    store.close()

    lifetime = store.lifetime()
    by_model = {row["name"]: row for row in lifetime["by_model"]}
    by_provider = {row["name"]: row for row in lifetime["by_provider"]}

    assert by_model["hy3"]["requests"] == 2
    assert by_model["hy3"]["tokens_in"] == 20
    assert by_provider["nous_portal"]["requests"] == 2
    assert by_provider["open_router"]["requests"] == 1


def test_lifetime_counts_statuses_fallbacks_and_diversions(
    store: RequestLogStore,
) -> None:
    store.enqueue(_record("ok"))
    store.enqueue(_record("bad", status="error"))
    store.enqueue(_record("gone", status="cancelled"))
    store.enqueue(_record("fell", route_attempt=2))
    # The router writes both halves together: the reason, and what was
    # replaced. A row with only the reason is not a shape it can produce.
    store.enqueue(
        _record(
            "saw",
            route_diversion="vision",
            route_diverted_from="nous_portal/tencent/hy3:free",
        )
    )
    # Nothing was replaced here -- the image had nowhere to go -- so it must
    # not be counted as a diversion.
    store.enqueue(_record("blind", route_diversion="vision_unavailable"))
    store.close()

    lifetime = store.lifetime()
    assert lifetime["requests"] == 6
    assert (lifetime["success"], lifetime["error"], lifetime["cancelled"]) == (4, 1, 1)
    assert lifetime["served_by_fallback"] == 1
    assert lifetime["diverted"] == 1


def test_lifetime_does_not_double_count_a_replayed_record(
    store: RequestLogStore,
) -> None:
    """The insert is ``INSERT OR REPLACE``; the counters are add-only."""
    store.enqueue(_record("same"))
    store.close()
    assert store.lifetime()["requests"] == 1

    reopened = RequestLogStore(store.db_path, max_rows=100)
    reopened.enqueue(_record("same", tokens_in=999))
    reopened.close()

    lifetime = reopened.lifetime()
    assert lifetime["requests"] == 1
    assert lifetime["tokens_in"] == 10


def test_lifetime_is_seeded_from_rows_written_before_the_upgrade(tmp_path) -> None:
    """Upgrading must not report zero all-time on a database full of history."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old1"))
    seed.enqueue(_record("old2", status="error"))
    seed.close()

    # Reproduce a database written by a version that had no rollup at all.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM request_totals")
        conn.execute("DELETE FROM request_log_meta")

    reopened = RequestLogStore(path, max_rows=100)
    reopened.close()

    lifetime = reopened.lifetime()
    assert lifetime["requests"] == 2
    assert lifetime["error"] == 1
    assert lifetime["tokens_in"] == 20


def test_backfill_runs_once_and_new_rows_still_count(tmp_path) -> None:
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old"))
    seed.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM request_totals")
        conn.execute("DELETE FROM request_log_meta")

    first = RequestLogStore(path, max_rows=100)
    first.enqueue(_record("new"))
    first.close()
    assert first.lifetime()["requests"] == 2

    # A second start must not re-seed the buckets it already wrote.
    second = RequestLogStore(path, max_rows=100)
    second.enqueue(_record("newer"))
    second.close()
    assert second.lifetime()["requests"] == 3


def test_clear_erases_the_lifetime_counters_too(store: RequestLogStore) -> None:
    store.enqueue(_record("r1"))
    store.close()
    assert store.lifetime()["requests"] == 1
    store.clear()
    assert store.lifetime()["requests"] == 0


def test_lifetime_on_an_empty_database_is_zero_not_null(store: RequestLogStore) -> None:
    lifetime = store.lifetime()
    assert lifetime["requests"] == 0
    assert lifetime["tokens_in"] == 0
    assert lifetime["first_day"] is None
    assert lifetime["by_model"] == []


# ------------------------------------------------------------- server uptime -


def test_coverage_records_a_session_for_a_running_store(tmp_path) -> None:
    """A quiet stretch is ambiguous unless uptime is recorded separately."""
    before = time.time()
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    store.enqueue(_record("r1"))
    store.close()

    coverage = store.coverage()
    assert len(coverage["sessions"]) == 1
    session = coverage["sessions"][0]
    assert session["started_at"] >= before
    assert session["last_seen_at"] >= session["started_at"]
    assert coverage["tracking_since"] is not None


def test_coverage_reports_nothing_before_tracking_began(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    store.close()
    coverage = store.coverage(since=1.0, until=2.0)
    assert coverage["sessions"] == []
    assert coverage["covered_seconds"] == 0.0
    # Still set, so a caller can say "not recorded" rather than "down".
    assert coverage["tracking_since"] is not None


def test_coverage_merges_overlapping_sessions(tmp_path) -> None:
    """Two servers on one database must not add up to 200% uptime."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM server_sessions")
        conn.executemany(
            "INSERT INTO server_sessions (started_at, last_seen_at, pid)"
            " VALUES (?, ?, ?)",
            [(100.0, 200.0, 1), (150.0, 250.0, 2), (400.0, 500.0, 3)],
        )

    coverage = store.coverage()
    # 100->250 merged (150s) plus 400->500 (100s), not 100+100+100.
    assert coverage["covered_seconds"] == 250.0


def test_coverage_clips_sessions_to_the_requested_window(tmp_path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM server_sessions")
        conn.execute(
            "INSERT INTO server_sessions (started_at, last_seen_at, pid)"
            " VALUES (?, ?, ?)",
            (100.0, 300.0, 1),
        )

    assert store.coverage(since=200.0, until=250.0)["covered_seconds"] == 50.0
    assert store.coverage(since=250.0)["covered_seconds"] == 50.0


# ------------------------------------------------------- compressed bodies ---


def test_bodies_round_trip_through_compression(store: RequestLogStore) -> None:
    store.enqueue(
        _record(
            "r1",
            input_text="question " * 100,
            output_text="answer " * 100,
            thinking_text="pondering",
            tool_calls=[{"name": "Read", "input": {"path": "a.py"}}],
        )
    )
    store.close()

    row = store.get_request("r1")
    assert row is not None
    assert row["input_text"] == "question " * 100
    assert row["output_text"] == "answer " * 100
    assert row["thinking_text"] == "pondering"
    assert row["tool_calls"] == [{"name": "Read", "input": {"path": "a.py"}}]


def test_bodies_are_not_stored_inline_when_compressing(store: RequestLogStore) -> None:
    """The whole point: the text must leave the row it used to bloat."""
    store.enqueue(_record("r1", input_text="x" * 5000))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        inline = conn.execute(
            "SELECT input_text, output_text FROM requests WHERE id = 'r1'"
        ).fetchone()
        blobs = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
    assert inline == (None, None)
    assert blobs == 1


def test_compression_actually_shrinks_repetitive_bodies(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    body = "the quick brown fox jumps over the lazy dog. " * 500
    store.enqueue(_record("r1", input_text=body, output_text=body))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        stored = conn.execute(
            "SELECT LENGTH(b.payload) FROM request_bodies r"
            " JOIN body_blobs b ON b.sha = r.sha WHERE r.request_id = 'r1'"
        ).fetchone()[0]
    assert stored < len(body) * 2 / 10


def test_list_view_truncates_a_compressed_body(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", input_text="y" * (LIST_BODY_PREVIEW_CHARS + 500)))
    store.close()

    rows, _ = store.list_requests(limit=1)
    assert len(rows[0]["input_text"]) == LIST_BODY_PREVIEW_CHARS
    assert rows[0]["input_text_truncated"] is True
    # The detail view still returns the whole thing.
    full = store.get_request("r1")
    assert full is not None
    assert len(full["input_text"]) == LIST_BODY_PREVIEW_CHARS + 500
    assert full["input_text_truncated"] is False


def test_list_view_of_a_compressed_row_keeps_its_shape(store: RequestLogStore) -> None:
    """List rows carry thinking_chars, never thinking_text."""
    store.enqueue(_record("r1", thinking_text="private reasoning"))
    store.close()

    rows, _ = store.list_requests(limit=1)
    assert "thinking_text" not in rows[0]


def test_search_finds_text_inside_compressed_bodies(store: RequestLogStore) -> None:
    store.enqueue(_record("hit", input_text="a needle in the haystack"))
    store.enqueue(_record("miss", input_text="nothing of interest"))
    store.close()

    rows, total = store.list_requests(q="needle")
    assert total == 1
    assert rows[0]["id"] == "hit"
    assert store.stats(q="needle")["total"] == 1


def test_search_is_case_insensitive_like_the_inline_form(
    store: RequestLogStore,
) -> None:
    store.enqueue(_record("r1", input_text="A Needle In The Haystack"))
    store.close()
    assert store.list_requests(q="needle")[1] == 1


def test_search_spans_legacy_inline_rows_and_compressed_rows(tmp_path) -> None:
    """Both storage forms coexist after an upgrade; search must cover both."""
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=100, compress_bodies=False)
    legacy.enqueue(_record("old", input_text="shared marker, stored inline"))
    legacy.close()

    modern = RequestLogStore(path, max_rows=100)
    modern.enqueue(_record("new", input_text="shared marker, compressed"))
    modern.close()

    rows, total = modern.list_requests(q="shared marker")
    assert total == 2
    assert {row["id"] for row in rows} == {"old", "new"}


def test_rows_written_before_the_upgrade_are_still_readable(tmp_path) -> None:
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=100, compress_bodies=False)
    legacy.enqueue(_record("old", input_text="written the old way"))
    legacy.close()

    modern = RequestLogStore(path, max_rows=100)
    modern.close()

    row = modern.get_request("old")
    assert row is not None
    assert row["input_text"] == "written the old way"


def test_compression_can_be_turned_off(tmp_path) -> None:
    store = RequestLogStore(
        tmp_path / "requests.db", max_rows=100, compress_bodies=False
    )
    store.enqueue(_record("r1", input_text="kept inline"))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        inline = conn.execute(
            "SELECT input_text FROM requests WHERE id = 'r1'"
        ).fetchone()[0]
        blobs = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
    assert inline == "kept inline"
    assert blobs == 0
    row = store.get_request("r1")
    assert row is not None
    assert row["input_text"] == "kept inline"


def test_pruning_removes_the_bodies_of_deleted_rows(tmp_path) -> None:
    """Orphaned blobs would defeat the entire point of retention."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=2)
    base = time.time()
    for index in range(6):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index, input_text="body"))
    store.close()
    store.prune()

    with sqlite3.connect(store.db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM request_bodies b"
            " WHERE NOT EXISTS (SELECT 1 FROM requests r WHERE r.id = b.request_id)"
        ).fetchone()[0]
    assert remaining == 2
    assert orphans == 0


def test_clear_removes_bodies_too(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", input_text="body"))
    store.close()
    store.clear()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 0


def test_a_corrupt_blob_degrades_instead_of_raising(store: RequestLogStore) -> None:
    store.enqueue(_record("r1", input_text="original"))
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE body_blobs SET payload = ? WHERE sha = ("
            " SELECT input_sha FROM request_bodies WHERE request_id = 'r1')",
            (b"not zstd at all",),
        )

    row = store.get_request("r1")
    assert row is not None
    assert row["id"] == "r1"
    assert row["input_text"] is None


def test_a_record_with_no_bodies_writes_no_blob(store: RequestLogStore) -> None:
    store.enqueue(
        _record(
            "r1", input_text=None, output_text=None, thinking_text=None, tool_calls=None
        )
    )
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 0


def _chatty(index: int) -> str:
    """A body shaped like real traffic: a long shared prefix, a small tail."""
    return (
        "You are Claude Code, an AI assistant. Follow the project conventions. " * 40
        + f"\n\nUser turn {index}: please explain the failure in module {index}."
    )


def test_a_dictionary_is_trained_once_there_is_enough_traffic(tmp_path) -> None:
    """A fresh install must start compressing well without waiting for a restart."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5000)
    for index in range(300):
        store.enqueue(_record(f"r{index}", input_text=_chatty(index)))
    store.close()
    store.enqueue(_record("after", input_text=_chatty(999)))

    trained = RequestLogStore(path, max_rows=5000)
    trained.enqueue(_record("after", input_text=_chatty(999)))
    trained.close()

    with sqlite3.connect(path) as conn:
        dicts = conn.execute("SELECT COUNT(*) FROM body_dictionaries").fetchone()[0]
        # The prompt blob is the one that carries the volume worth compressing.
        blob = (
            "SELECT {} FROM request_bodies r"
            " JOIN body_blobs b ON b.sha = r.input_sha"
            " WHERE r.request_id = ?"
        )
        used = conn.execute(blob.format("b.dict_id"), ("after",)).fetchone()[0]
        # r0 predates training, so it carries no dictionary.
        before = conn.execute(blob.format("LENGTH(b.payload)"), ("r0",)).fetchone()[0]
        after = conn.execute(blob.format("LENGTH(b.payload)"), ("after",)).fetchone()[0]
    assert dicts == 1
    assert used is not None
    assert after < before


def test_rows_written_before_training_stay_readable_after_it(tmp_path) -> None:
    """Blobs record their own dictionary, so training must never orphan them."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5000)
    for index in range(300):
        store.enqueue(_record(f"r{index}", input_text=_chatty(index)))
    store.close()

    trained = RequestLogStore(path, max_rows=5000)
    trained.enqueue(_record("after", input_text=_chatty(999)))
    trained.close()

    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT b.dict_id FROM request_bodies r JOIN body_blobs b"
                " ON b.sha = r.sha WHERE r.request_id = 'r0'"
            ).fetchone()[0]
            is None
        )

    old_row = trained.get_request("r0")
    new_row = trained.get_request("after")
    assert old_row is not None and new_row is not None
    assert old_row["input_text"] == _chatty(0)
    assert new_row["input_text"] == _chatty(999)
    # And search still spans both dictionary generations. "999" appears only in
    # the row written after training; the shared prefix appears in every row,
    # including the 300 compressed without a dictionary.
    assert trained.list_requests(q="module 999")[1] == 1
    assert trained.list_requests(q="Claude Code assistant")[1] == 301


def test_training_does_not_repeat_on_every_restart(tmp_path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5000)
    for index in range(300):
        store.enqueue(_record(f"r{index}", input_text=_chatty(index)))
    store.close()

    for _ in range(3):
        reopened = RequestLogStore(path, max_rows=5000)
        reopened.close()

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM body_dictionaries").fetchone()[0] == 1


def test_close_drains_a_deep_queue_instead_of_abandoning_it(tmp_path) -> None:
    """Regression: a fixed close deadline silently dropped queued records.

    Compressing bodies is real CPU work on the writer thread, so a backlog can
    outlive a fixed timeout. Replaying 4,000 real requests lost 2,950 of them
    before the shutdown wait was made to scale with the queue.
    """
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100_000)
    body = "a plausible assistant reply with some structure. " * 500
    for index in range(1_500):
        store.enqueue(_record(f"r{index}", input_text=body, output_text=body))
    store.close()

    _, total = store.list_requests(limit=1)
    assert total == 1_500
    with sqlite3.connect(store.db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 1_500
        )


def test_search_still_matches_needles_that_json_escapes(store: RequestLogStore) -> None:
    """The byte-level prefilter is skipped for these, not silently wrong.

    Quotes, backslashes and newlines are rewritten by JSON encoding, so a
    needle containing one does not survive into the stored blob byte for byte.
    Such needles must fall back to decoding rather than report no match.
    """
    store.enqueue(_record("quoted", input_text='he said "deploy now" firmly'))
    store.enqueue(_record("slashed", input_text=r"path is C:\Users\fgghk"))
    store.enqueue(_record("newline", input_text="first line\nsecond line"))
    store.close()

    assert store.list_requests(q='"deploy now"')[1] == 1
    assert store.list_requests(q=r"C:\Users")[1] == 1
    assert store.list_requests(q="line\nsecond")[1] == 1


def test_search_does_not_match_the_payload_structure(store: RequestLogStore) -> None:
    """A hit on the encoding must be verified against the real text."""
    store.enqueue(_record("r1", input_text="hello", output_text="world"))
    store.close()

    # These appear in the stored JSON but in no body.
    for structural in ('","', '{"i":', '"o"'):
        assert store.list_requests(q=structural)[1] == 0, structural


def test_search_matches_the_same_rows_with_and_without_compression(tmp_path) -> None:
    """Compression must not change which requests a search finds."""
    texts = [
        "deploy the kubernetes cluster",
        "KUBERNETES in shouty caps",
        "nothing relevant here",
        'a "quoted" phrase',
    ]
    results = {}
    for label, compress in (("inline", False), ("compressed", True)):
        store = RequestLogStore(
            tmp_path / f"{label}.db", max_rows=1000, compress_bodies=compress
        )
        for index, text in enumerate(texts):
            store.enqueue(_record(f"r{index}", input_text=text, output_text=""))
        store.close()
        results[label] = {
            term: {row["id"] for row in store.list_requests(q=term)[0]}
            for term in ("kubernetes", "KUBERNETES", '"quoted"', "relevant", "zzz")
        }
    assert results["inline"] == results["compressed"]


# ------------------------------------------------------------ search scope ---


@pytest.fixture(params=[True, False], ids=["compressed", "inline"])
def searchable(request, tmp_path):
    """The same assertions must hold whichever way bodies are stored."""
    store = RequestLogStore(
        tmp_path / "requests.db", max_rows=1000, compress_bodies=request.param
    )
    yield store
    store.close()


def test_search_covers_reasoning_and_tool_calls(searchable: RequestLogStore) -> None:
    """55% of real requests carry reasoning and 78% carry tool calls.

    Searching only the prompt and reply made the log quietly blind to most of
    what it had stored.
    """
    searchable.enqueue(
        _record(
            "r1",
            input_text="the prompt",
            output_text="the reply",
            thinking_text="weighing the tradeoffs of a rollback",
            tool_calls=[{"name": "Bash", "input": {"command": "git revert HEAD"}}],
        )
    )
    searchable.close()

    assert searchable.list_requests(q="prompt")[1] == 1
    assert searchable.list_requests(q="reply")[1] == 1
    assert searchable.list_requests(q="tradeoffs")[1] == 1
    assert searchable.list_requests(q="git revert")[1] == 1
    assert searchable.list_requests(q="Bash")[1] == 1
    assert searchable.list_requests(q="absent")[1] == 0


def test_search_requires_every_word_but_not_their_order(
    searchable: RequestLogStore,
) -> None:
    searchable.enqueue(_record("both", input_text="deploy the kubernetes cluster"))
    searchable.enqueue(_record("one", input_text="deploy the docker container"))
    searchable.close()

    assert {
        row["id"] for row in searchable.list_requests(q="kubernetes deploy")[0]
    } == {"both"}
    assert searchable.list_requests(q="deploy")[1] == 2
    assert searchable.list_requests(q="kubernetes missing")[1] == 0


def test_search_spans_different_parts_of_one_request(
    searchable: RequestLogStore,
) -> None:
    """One word from the prompt and one from the reasoning must still match."""
    searchable.enqueue(
        _record("r1", input_text="restart the proxy", thinking_text="port 8082 is busy")
    )
    searchable.close()
    assert searchable.list_requests(q="proxy 8082")[1] == 1


def test_search_ignores_surrounding_whitespace(searchable: RequestLogStore) -> None:
    searchable.enqueue(_record("r1", input_text="a distinctive phrase"))
    searchable.close()
    assert searchable.list_requests(q="  distinctive  ")[1] == 1
    assert searchable.list_requests(q="   ")[1] == 1  # no terms: not a filter


# ------------------------------------------------------------ deduplication --


def test_identical_bodies_are_stored_once(store: RequestLogStore) -> None:
    """29.7% of real requests repeat a prompt already stored."""
    body = "the very same context, sent again " * 200
    for index in range(5):
        store.enqueue(_record(f"r{index}", input_text=body, output_text="same"))
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        mappings = conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0]
        blobs = conn.execute("SELECT COUNT(*) FROM body_blobs").fetchone()[0]
    assert mappings == 5
    # One prompt blob and one reply blob, shared by all five.
    assert blobs == 2
    for index in range(5):
        row = store.get_request(f"r{index}")
        assert row is not None
        assert row["input_text"] == body


def test_a_shared_blob_survives_until_its_last_request_goes(tmp_path) -> None:
    """Deleting one request must not blank the others that share its body."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=2)
    base = time.time()
    body = "shared between several requests " * 100
    for index in range(4):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index, input_text=body))
    store.close()
    store.prune()

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM body_blobs").fetchone()[0] == 2
    survivor = store.get_request("r3")
    assert survivor is not None
    assert survivor["input_text"] == body


def test_orphaned_blobs_are_collected(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1)
    base = time.time()
    for index in range(4):
        store.enqueue(
            _record(f"r{index}", ts_epoch=base + index, input_text=f"unique {index}")
        )
    store.close()
    store.prune()

    with sqlite3.connect(store.db_path) as conn:
        # One surviving request: its own prompt, plus the reply all four shared.
        assert conn.execute("SELECT COUNT(*) FROM body_blobs").fetchone()[0] == 2


# --------------------------------------------------------------- compaction --


def test_compaction_converts_inline_history_and_shrinks_the_file(tmp_path) -> None:
    """Compression only ever applied to new writes; history kept paying full price."""
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=10_000, compress_bodies=False)
    body = "a realistic assistant transcript with structure. " * 400
    for index in range(300):
        legacy.enqueue(
            _record(
                f"r{index}",
                input_text=body + f" turn {index}",
                output_text="answered",
                thinking_text="considered it",
                tool_calls=[{"name": "Bash", "input": {"command": f"echo {index}"}}],
            )
        )
    legacy.close()
    before = path.stat().st_size

    result = compact_request_log(path)

    assert result["converted"] == 300
    assert result["vacuumed"] is True
    assert path.stat().st_size < before / 2

    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM requests WHERE input_text IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM request_bodies").fetchone()[0] == 300

    reopened = RequestLogStore(path, max_rows=10_000)
    try:
        row = reopened.get_request("r7")
        assert row is not None
        assert row["input_text"] == body + " turn 7"
        assert row["output_text"] == "answered"
        assert row["thinking_text"] == "considered it"
        assert row["tool_calls"] == [{"name": "Bash", "input": {"command": "echo 7"}}]
        # Search must keep working across the converted rows.
        assert reopened.list_requests(q="echo 123")[1] == 1
        assert reopened.list_requests(q="considered")[1] == 300
    finally:
        reopened.close()


def test_compaction_is_idempotent(tmp_path) -> None:
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=10_000, compress_bodies=False)
    for index in range(300):
        legacy.enqueue(_record(f"r{index}", input_text=f"body {index} " * 200))
    legacy.close()

    first = compact_request_log(path)
    second = compact_request_log(path)

    assert first["converted"] == 300
    assert second["converted"] == 0
    store = RequestLogStore(path, max_rows=10_000)
    try:
        row = store.get_request("r5")
        assert row is not None
        assert row["input_text"] == "body 5 " * 200
    finally:
        store.close()


def test_compaction_preserves_every_body_exactly(tmp_path) -> None:
    """Row-for-row equality, because this rewrites real history in place."""
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=10_000, compress_bodies=False)
    expected = {}
    for index in range(300):
        text = f"unique-{index} " + ("shared filler " * 50)
        expected[f"r{index}"] = text
        legacy.enqueue(_record(f"r{index}", input_text=text))
    legacy.close()

    compact_request_log(path)

    store = RequestLogStore(path, max_rows=10_000)
    try:
        for request_id, text in expected.items():
            row = store.get_request(request_id)
            assert row is not None, request_id
            assert row["input_text"] == text, request_id
    finally:
        store.close()


# ------------------------------------------------------- prompt/reply split --


def test_a_repeated_prompt_is_stored_once_despite_different_replies(
    store: RequestLogStore,
) -> None:
    """The saving the split exists for.

    A retry re-sends the same context and gets a different answer. Keyed on the
    whole body that deduplicates nothing; keyed on the prompt alone it removes
    35.3% of the stored bytes on a real log.
    """
    prompt = "the same long prompt, sent again and again " * 200
    for index in range(4):
        store.enqueue(
            _record(f"r{index}", input_text=prompt, output_text=f"reply {index}")
        )
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        prompts = conn.execute(
            "SELECT COUNT(DISTINCT input_sha) FROM request_bodies"
        ).fetchone()[0]
        replies = conn.execute(
            "SELECT COUNT(DISTINCT sha) FROM request_bodies"
        ).fetchone()[0]
    assert prompts == 1
    assert replies == 4
    for index in range(4):
        row = store.get_request(f"r{index}")
        assert row is not None
        assert row["input_text"] == prompt
        assert row["output_text"] == f"reply {index}"


def test_a_request_with_only_a_prompt_stores_no_reply_blob(
    store: RequestLogStore,
) -> None:
    store.enqueue(
        _record("r1", input_text="just a prompt", output_text=None, tool_calls=None)
    )
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT sha, input_sha FROM request_bodies WHERE request_id = 'r1'"
        ).fetchone()
    assert row[0] is None
    assert row[1] is not None
    stored = store.get_request("r1")
    assert stored is not None
    assert stored["input_text"] == "just a prompt"
    assert stored["output_text"] is None


def test_search_spans_the_two_blobs_of_one_request(store: RequestLogStore) -> None:
    """One word in the prompt, one in the reasoning, still one match."""
    store.enqueue(
        _record("r1", input_text="restart the proxy", thinking_text="port 8082 is busy")
    )
    store.enqueue(_record("r2", input_text="restart the proxy", thinking_text="fine"))
    store.close()
    assert {row["id"] for row in store.list_requests(q="proxy 8082")[0]} == {"r1"}


def test_bodies_written_before_the_split_are_still_read_and_searched(
    tmp_path,
) -> None:
    """Combined blobs stay readable until compaction splits them."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.enqueue(_record("r1", input_text="findable prompt", output_text="reply"))
    store.close()

    # Reproduce the pre-split layout: one blob carrying everything.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM body_blobs")
        conn.execute("DELETE FROM request_bodies")
    reopened = RequestLogStore(path, max_rows=100)
    try:
        combined = pack_bodies(
            {"input_text": "findable prompt", "output_text": "reply"}
        )
        sha = hashlib.sha256(combined).hexdigest()
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO body_blobs (sha, dict_id, payload) VALUES (?, NULL, ?)",
                (sha, zstd.compress(combined, level=3)),
            )
            conn.execute(
                "INSERT INTO request_bodies (request_id, sha, input_sha)"
                " VALUES ('r1', ?, NULL)",
                (sha,),
            )
        row = reopened.get_request("r1")
        assert row is not None
        assert row["input_text"] == "findable prompt"
        assert row["output_text"] == "reply"
        assert reopened.list_requests(q="findable")[1] == 1
    finally:
        reopened.close()


def test_compaction_splits_blobs_written_before_the_split(tmp_path) -> None:
    """The second pass: existing combined blobs are re-keyed, and shrink."""
    path = tmp_path / "requests.db"
    legacy = RequestLogStore(path, max_rows=10_000, compress_bodies=False)
    prompt = "a shared prompt of some length " * 300
    for index in range(300):
        legacy.enqueue(
            _record(f"r{index}", input_text=prompt, output_text=f"reply {index}")
        )
    legacy.close()

    compact_request_log(path)

    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM request_bodies WHERE input_sha IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT input_sha) FROM request_bodies"
            ).fetchone()[0]
            == 1
        )

    store = RequestLogStore(path, max_rows=10_000)
    try:
        for index in (0, 150, 299):
            row = store.get_request(f"r{index}")
            assert row is not None
            assert row["input_text"] == prompt
            assert row["output_text"] == f"reply {index}"
        assert store.list_requests(q="shared prompt")[1] == 300
    finally:
        store.close()


# ---------------------------------------------------------------- attempts --


def _attempts() -> tuple[RouteAttempt, ...]:
    return (
        RouteAttempt(
            attempt=0,
            provider="nous_portal",
            model_ref="nous_portal/tencent/hy3:free",
            outcome=RouteAttemptOutcome.FAILED,
            error_kind="timeout",
            error_message="exceeded the 600s request budget",
            duration_ms=600_000.0,
        ),
        RouteAttempt(
            attempt=1,
            provider="opencode",
            model_ref="opencode/hy3-free",
            outcome=RouteAttemptOutcome.SUCCEEDED,
            duration_ms=1_200.0,
        ),
        RouteAttempt(
            attempt=2,
            provider="groq",
            model_ref="groq/llama-3.3-70b",
            outcome=RouteAttemptOutcome.SKIPPED,
            error_message="never reached",
        ),
    )


def test_a_rescued_request_keeps_the_reason_its_primary_failed(store):
    """The reason a fallback was needed must survive the request succeeding.

    ``requests`` records only the model that answered, so a chain that rescued
    a request reported ``status='success'`` and lost the failure entirely. On 21
    days of real traffic that hid the cause of 1,144 fallbacks.
    """
    store.enqueue(_record("req_chain", attempts=_attempts()))
    store.close()

    stored = store.get_request("req_chain")
    assert [(a["attempt"], a["outcome"]) for a in stored["route_attempts"]] == [
        (0, "failed"),
        (1, "succeeded"),
        (2, "skipped"),
    ]
    first = stored["route_attempts"][0]
    assert first["model_ref"] == "nous_portal/tencent/hy3:free"
    assert first["error_kind"] == "timeout"
    assert first["error_message"] == "exceeded the 600s request budget"
    assert first["duration_ms"] == 600_000.0
    # And the row itself still reports the request as the success it was.
    assert stored["status"] == "success"


def test_a_request_with_no_chain_reports_no_attempts(store):
    """A single-model route costs nothing: no rows, and an empty list."""
    store.enqueue(_record("req_plain"))
    store.close()

    assert store.get_request("req_plain")["route_attempts"] == []


def test_attempts_are_deleted_with_the_request_they_belong_to(store, tmp_path):
    """Retention must not leave attempts behind for pruned requests.

    A side table keyed on request_id grows forever unless prune reaches it, and
    a request log that trims itself to a row cap is exactly where that would go
    unnoticed.
    """
    small = RequestLogStore(tmp_path / "small.db", max_rows=1)
    try:
        small.enqueue(_record("req_old", attempts=_attempts()))
        small.enqueue(_record("req_new", attempts=_attempts()))
        small.close()
        small.prune()

        with sqlite3.connect(tmp_path / "small.db") as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM request_attempts WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_attempts.request_id)"
            ).fetchone()[0]
        assert orphans == 0
    finally:
        small.close()


# ------------------------------------------------- recovery observability --


_LEGACY_ATTEMPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_attempts (
    request_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    provider TEXT,
    model_ref TEXT,
    outcome TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    duration_ms REAL,
    PRIMARY KEY (request_id, attempt)
);
"""


def _recovered_attempts() -> tuple[RouteAttempt, ...]:
    return (
        RouteAttempt(
            attempt=0,
            provider="nous_portal",
            model_ref="nous_portal/m1",
            outcome=RouteAttemptOutcome.FAILED,
            error_kind="timeout",
            params={"early_retries": 2, "midstream_recoveries": 1},
        ),
        RouteAttempt(
            attempt=1,
            provider="groq",
            model_ref="groq/m2",
            outcome=RouteAttemptOutcome.SUCCEEDED,
            duration_ms=900.0,
            params={"salvages": 1},
        ),
    )


def test_attempt_params_round_trip(store: RequestLogStore) -> None:
    """Recovery counters survive the writer and come back parsed."""
    store.enqueue(_record("req_rec", attempts=_recovered_attempts()))
    store.enqueue(_record("req_bare"))
    store.close()

    stored = store.get_request("req_rec")
    assert stored is not None
    first, second = stored["route_attempts"]
    assert first["params"] == {"early_retries": 2, "midstream_recoveries": 1}
    assert second["params"] == {"salvages": 1}

    bare = store.get_request("req_bare")
    assert bare is not None
    assert bare["route_attempts"] == []


def test_attempt_credentials_round_trip(store: RequestLogStore) -> None:
    """Which key served which attempt, per attempt rather than per request."""
    store.enqueue(
        _record(
            "req_keys",
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="nvidia_nim",
                    model_ref="nvidia_nim/m1",
                    outcome=RouteAttemptOutcome.FAILED,
                    key_index=0,
                    key_label="ab...cd",
                ),
                RouteAttempt(
                    attempt=1,
                    provider="nvidia_nim",
                    model_ref="nvidia_nim/m1",
                    outcome=RouteAttemptOutcome.FAILED,
                    key_index=-1,
                    key_label="(no key available)",
                ),
            ),
        )
    )
    store.close()

    stored = store.get_request("req_keys")
    assert stored is not None
    first, second = stored["route_attempts"]
    assert (first["key_index"], first["key_label"]) == (0, "ab...cd")
    # The sentinel, not a NULL: the pool was fully benched, which is a
    # measurement rather than a gap.
    assert (second["key_index"], second["key_label"]) == (-1, "(no key available)")


def test_migrates_a_database_created_before_attempt_credentials(tmp_path) -> None:
    """The two credential columns arrive without disturbing existing rows."""
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.executescript(_LEGACY_ATTEMPTS_SCHEMA)
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
            " status) VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
            " 'success')",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO request_attempts (request_id, attempt, provider,"
            " model_ref, outcome) VALUES ('legacy', 0, 'nvidia_nim',"
            " 'nvidia_nim/old', 'failed')"
        )
        conn.commit()
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_attempts)")
        }
        assert "key_index" not in columns
        assert "key_label" not in columns
    finally:
        conn.close()

    store = RequestLogStore(db_path, max_rows=100)
    try:
        # The width guard skips an empty batch, so this must actually write.
        store.enqueue(_record("fresh", attempts=_recovered_attempts()))
        store.close()

        check = sqlite3.connect(db_path)
        try:
            columns = {
                str(row[1])
                for row in check.execute("PRAGMA table_info(request_attempts)")
            }
            indexes = {
                str(row[1])
                for row in check.execute("PRAGMA index_list(request_attempts)")
            }
        finally:
            check.close()
        assert {"key_index", "key_label"} <= columns
        assert "idx_request_attempts_model_v1" in indexes

        legacy = store.get_request("legacy")
        assert legacy is not None
        (attempt,) = legacy["route_attempts"]
        assert attempt["outcome"] == "failed"
        # Written before the columns existed: NULL, which the UI renders as a
        # dash rather than as a keyless request.
        assert attempt["key_index"] is None
        assert attempt["key_label"] is None
    finally:
        store.close()


def test_the_attempt_model_index_exists_after_migration(store: RequestLogStore) -> None:
    store.enqueue(_record("indexed", attempts=_recovered_attempts()))
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        indexes = {
            str(row[1]) for row in conn.execute("PRAGMA index_list(request_attempts)")
        }
    assert "idx_request_attempts_model_v1" in indexes


def _reasoning_record(request_id: str, *, emitted, thinking_chars: int, **overrides):
    return _record(
        request_id,
        thinking_chars=thinking_chars,
        attempts=(
            RouteAttempt(
                attempt=0,
                provider="commandcode",
                model_ref="commandcode/m1",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                reasoning_emitted=emitted,
            ),
        ),
        **overrides,
    )


def test_reasoning_by_model_reports_requested_and_returned_separately(
    store: RequestLogStore,
) -> None:
    """Asked and answered are two facts, and their four combinations differ."""
    store.enqueue(_reasoning_record("asked_thought", emitted=True, thinking_chars=100))
    store.enqueue(_reasoning_record("asked_silent", emitted=True, thinking_chars=0))
    store.enqueue(
        _reasoning_record("unasked_thought", emitted=False, thinking_chars=50)
    )
    store.enqueue(
        _record(
            "failed_one",
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="commandcode",
                    model_ref="commandcode/m1",
                    outcome=RouteAttemptOutcome.FAILED,
                    reasoning_emitted=True,
                ),
            ),
        )
    )
    store.close()

    (row,) = store.reasoning_by_model()
    assert row["model_ref"] == "commandcode/m1"
    # The failed attempt is excluded: it answered nothing either way.
    assert row["attempts"] == 3
    assert row["requested"] == 2
    assert row["returned"] == 2
    assert row["unmeasured"] == 0


def test_reasoning_measured_counts_a_zero_char_row_as_measured_not_returned(
    store: RequestLogStore,
) -> None:
    """A stored 0 is a completed stream that returned no reasoning (6.8.0).

    Requested, measured, and the honest answer is "it thought nothing" -- not
    "nobody was counting", which is what a NULL means and what this row used
    to be written as.
    """
    store.enqueue(_reasoning_record("asked_silent", emitted=True, thinking_chars=0))
    store.close()

    (row,) = store.reasoning_by_model()
    assert row["attempts"] == 1
    assert row["requested"] == 1
    assert row["returned"] == 0
    assert row["unmeasured"] == 0


def test_reasoning_by_model_counts_unmeasured_attempts_separately(
    store: RequestLogStore,
) -> None:
    store.enqueue(_reasoning_record("measured", emitted=True, thinking_chars=10))
    store.enqueue(_reasoning_record("unmeasured", emitted=None, thinking_chars=10))
    store.close()

    (row,) = store.reasoning_by_model()
    assert row["attempts"] == 2
    assert row["requested"] == 1
    # Not folded into "requested 0": nobody was recording, which is not the
    # same fact as a body that carried nothing.
    assert row["unmeasured"] == 1


def test_reasoning_by_model_honours_the_since_window(store: RequestLogStore) -> None:
    now = time.time()
    store.enqueue(
        _reasoning_record(
            "old", emitted=True, thinking_chars=10, ts_epoch=now - 10 * 86_400
        )
    )
    store.enqueue(
        _reasoning_record("recent", emitted=True, thinking_chars=10, ts_epoch=now)
    )
    store.close()

    assert store.reasoning_by_model()[0]["attempts"] == 2
    (row,) = store.reasoning_by_model(since=now - 86_400)
    assert row["attempts"] == 1


def test_migrates_a_database_created_before_attempt_params(tmp_path) -> None:
    """Existing attempt rows must gain the params column without losing data."""
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.executescript(_LEGACY_ATTEMPTS_SCHEMA)
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
            " status) VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
            " 'success')",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO request_attempts (request_id, attempt, provider,"
            " model_ref, outcome) VALUES ('legacy', 0, 'nvidia_nim',"
            " 'nvidia_nim/old', 'failed')"
        )
        conn.commit()
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_attempts)")
        }
        assert "params" not in columns
    finally:
        conn.close()

    store = RequestLogStore(db_path, max_rows=100)
    try:
        store.enqueue(_record("fresh", attempts=_recovered_attempts()))
        store.close()

        check = sqlite3.connect(db_path)
        try:
            columns = {
                str(row[1])
                for row in check.execute("PRAGMA table_info(request_attempts)")
            }
        finally:
            check.close()
        assert "params" in columns

        legacy = store.get_request("legacy")
        assert legacy is not None
        (attempt,) = legacy["route_attempts"]
        assert attempt["outcome"] == "failed"
        # Written before recovery was counted: NULL, not zero.
        assert attempt["params"] is None

        fresh = store.get_request("fresh")
        assert fresh is not None
        assert fresh["route_attempts"][0]["params"] == {
            "early_retries": 2,
            "midstream_recoveries": 1,
        }
    finally:
        store.close()


def test_stats_sums_recovery_over_the_window(store: RequestLogStore) -> None:
    """The analytics payload carries the counters summed over every attempt."""
    store.enqueue(_record("r1", attempts=_recovered_attempts()))
    store.enqueue(
        _record(
            "r2",
            provider="opencode",
            resolved_model="oc/m",
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="opencode",
                    model_ref="oc/m",
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                    params={"early_retries": 1, "salvages": 4},
                ),
            ),
        )
    )
    store.close()

    assert store.stats()["recovery"] == {
        "early_retries": 3,
        "midstream_recoveries": 1,
        "salvages": 5,
    }
    # The window filters carry through to the attempts being summed.
    narrowed = store.stats(provider="opencode")
    assert narrowed["recovery"] == {
        "early_retries": 1,
        "midstream_recoveries": 0,
        "salvages": 4,
    }


def test_an_unreadable_params_value_cannot_take_stats_down(store) -> None:
    """One malformed blob reports nothing measured instead of raising."""
    store.enqueue(_record("r1", attempts=_recovered_attempts()))
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE request_attempts SET params = 'not json'"
            " WHERE request_id = 'r1' AND attempt = 0"
        )

    # The raw-row path is the one that reads the stored blob back, and one
    # malformed value there must report nothing measured rather than raise.
    assert store._stats_from_rows()["recovery"] == {
        "early_retries": 0,
        "midstream_recoveries": 0,
        "salvages": 0,
    }
    # The rollup summed the same counters off the record itself, inside the
    # write transaction, so a value corrupted afterwards cannot reach it.
    assert store.stats()["recovery"] == {
        "early_retries": 2,
        "midstream_recoveries": 1,
        "salvages": 1,
    }
    row = store.get_request("r1")
    assert row is not None
    assert row["route_attempts"][0]["params"] is None


# --------------------------------------------------------------------------- #
# The outbound wire request, recorded per attempt
# --------------------------------------------------------------------------- #


def _wire_attempts() -> tuple[RouteAttempt, ...]:
    """One attempt that was sent 16,384 tokens after asking for 64,000."""
    return (
        RouteAttempt(
            attempt=0,
            provider="nvidia_nim",
            model_ref="nvidia_nim/thinkingmachines/inkling",
            outcome=RouteAttemptOutcome.SUCCEEDED,
            params={"early_retries": 1, "wire": {"max_tokens": 16384, "tools": 40}},
            wire_body='{"max_tokens": 16384, "model": "inkling"}',
            reasoning_emitted=True,
        ),
    )


def test_wire_request_round_trips_through_the_writer(store: RequestLogStore) -> None:
    """The wire body, its summary and the reasoning flag survive a round trip."""
    store.enqueue(_record("req_wire", attempts=_wire_attempts()))
    store.close()

    stored = store.get_request("req_wire")
    assert stored is not None
    (attempt,) = stored["route_attempts"]
    # The whole point: the recorded number is the wire's, not the client's.
    assert attempt["params"]["wire"]["max_tokens"] == 16384
    # Recovery counters keep their existing flat shape alongside it.
    assert attempt["params"]["early_retries"] == 1
    assert attempt["wire_body"] == {"max_tokens": 16384, "model": "inkling"}
    assert attempt["reasoning_emitted"] is True


def test_reasoning_emitted_false_is_stored_as_false_not_as_missing(
    store: RequestLogStore,
) -> None:
    """``False`` and "not measured" must stay distinguishable in the column."""
    store.enqueue(
        _record(
            "req_none",
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="commandcode",
                    model_ref="commandcode/m",
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                    wire_body="{}",
                    reasoning_emitted=False,
                ),
            ),
        )
    )
    store.close()

    stored = store.get_request("req_none")
    assert stored is not None
    (attempt,) = stored["route_attempts"]
    assert attempt["reasoning_emitted"] is False


def test_migrates_a_database_created_before_the_wire_columns(tmp_path) -> None:
    """The live database predates these columns; the ALTERs must be guarded.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so the
    columns only appear if the explicit, ``PRAGMA table_info``-guarded ALTERs
    run -- and running the store twice must not fail the second time.
    """
    db_path = tmp_path / "requests.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.executescript(_LEGACY_ATTEMPTS_SCHEMA)
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
            " status) VALUES ('legacy', ?, 'x', '/v1/messages', 'anthropic',"
            " 'success')",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO request_attempts (request_id, attempt, provider,"
            " model_ref, outcome) VALUES ('legacy', 0, 'groq', 'groq/old',"
            " 'succeeded')"
        )
        conn.commit()
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_attempts)")
        }
        assert "wire_body" not in columns
        assert "reasoning_emitted" not in columns
    finally:
        conn.close()

    store = RequestLogStore(db_path, max_rows=100)
    try:
        store.enqueue(_record("fresh", attempts=_wire_attempts()))
        store.close()
    finally:
        store.close()

    # Idempotent: a second open of the same file finds the columns and skips.
    again = RequestLogStore(db_path, max_rows=100)
    try:
        legacy = again.get_request("legacy")
        assert legacy is not None
        (old_attempt,) = legacy["route_attempts"]
        # Written before wire capture existed: NULL, which is "not measured"
        # and must never read as "no reasoning was sent".
        assert old_attempt["wire_body"] is None
        assert old_attempt["reasoning_emitted"] is None

        fresh = again.get_request("fresh")
        assert fresh is not None
        (new_attempt,) = fresh["route_attempts"]
        assert new_attempt["params"]["wire"]["max_tokens"] == 16384
        assert new_attempt["reasoning_emitted"] is True
    finally:
        again.close()


# --------------------------------------------------------- the retry ladder --


_LADDER_PARAMS = {
    "ladder": {
        "tries": [
            {"source": "upstream", "key_index": 0, "status": 429, "waited_ms": 2700.0},
            {"source": "upstream", "key_index": 1, "status": 502, "upstream_ms": 830.0},
        ],
        "summary": {
            "tries": 2,
            "statuses_by_code": {"429": 1, "502": 1},
            "keys": 2,
            "time_upstream_ms": 830.0,
            "time_sleeping_ms": 2700.0,
            "time_limiter_ms": 0.0,
            "tries_dropped": 0,
        },
        "credentials": [
            {
                "key_index": 0,
                "key_label": "ab...cd",
                "class": "rate_limit",
                "benched_for_s": 60.0,
                "status": 429,
                "retry_after": None,
                "reason": "429, no Retry-After -- operator cooldown 60s",
            }
        ],
        "root_cause": "2 tries across 2 keys: 1\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502",
    }
}


def _ladder_attempt(**overrides) -> RouteAttempt:
    defaults: dict[str, Any] = {
        "attempt": 0,
        "provider": "nvidia_nim",
        "model_ref": "nvidia_nim/moonshotai/kimi-k3",
        "outcome": RouteAttemptOutcome.FAILED,
        "error_kind": "upstream",
        "error_message": "Upstream provider NIM returned HTTP 502.",
        "duration_ms": 107_534.16,
        "params": dict(_LADDER_PARAMS),
        "key_index": 1,
        "key_label": "ef...gh",
        "ladder_tries": 2,
    }
    defaults.update(overrides)
    return RouteAttempt(**defaults)


def test_attempt_rows_carry_every_insert_column(store) -> None:
    """The 42-vs-43 lesson, as a test rather than as an outage.

    A column added to the INSERT tuple without its name in
    ``_ATTEMPT_INSERT_COLUMNS`` -- or the reverse -- once broke every
    request-log write. The width is asserted at write time; this asserts that
    every named column also survives the round trip back out.
    """
    from my_claude_code.core.request_log import _ATTEMPT_INSERT_COLUMNS

    store.enqueue(_record("req_ladder", attempts=(_ladder_attempt(),)))
    store.close()

    stored = store.get_request("req_ladder")["route_attempts"][0]
    # ``request_id`` identifies the row rather than appearing in it.
    for column in _ATTEMPT_INSERT_COLUMNS:
        if column == "request_id":
            continue
        assert column in stored, f"{column} never comes back out of _fetch_attempts"
    assert stored["ladder_tries"] == 2


def test_ladder_survives_a_round_trip_through_params(store) -> None:
    store.enqueue(_record("req_ladder", attempts=(_ladder_attempt(),)))
    store.close()

    ladder = store.get_request("req_ladder")["route_attempts"][0]["params"]["ladder"]
    assert ladder["summary"]["statuses_by_code"] == {"429": 1, "502": 1}
    assert (
        ladder["root_cause"]
        == "2 tries across 2 keys: 1\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502"
    )
    assert ladder["credentials"][0]["benched_for_s"] == 60.0


def test_ladder_tries_column_is_added_to_a_pre_ladder_database(tmp_path) -> None:
    """``CREATE TABLE IF NOT EXISTS`` is a no-op, so the ALTER must be guarded.

    Rows written before the column existed read ``None``: not measured, which
    is emphatically not "this attempt went through on its first try".
    """
    path = tmp_path / "requests.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE request_attempts ("
        " request_id TEXT NOT NULL, attempt INTEGER NOT NULL, provider TEXT,"
        " model_ref TEXT, outcome TEXT NOT NULL, error_kind TEXT,"
        " error_message TEXT, duration_ms REAL,"
        " PRIMARY KEY (request_id, attempt))"
    )
    conn.execute(
        "INSERT INTO request_attempts (request_id, attempt, outcome)"
        " VALUES ('req_old', 0, 'failed')"
    )
    conn.commit()
    conn.close()

    store = RequestLogStore(path, max_rows=100)
    try:
        columns = set()
        with sqlite3.connect(path) as probe:
            columns = {
                str(row[1])
                for row in probe.execute("PRAGMA table_info(request_attempts)")
            }
        assert "ladder_tries" in columns
        store.enqueue(_record("req_new", attempts=(_ladder_attempt(),)))
        store.close()
        stored = store.get_request("req_new")
        assert stored is not None
        assert stored["route_attempts"][0]["ladder_tries"] == 2
        with sqlite3.connect(path) as probe:
            old = probe.execute(
                "SELECT ladder_tries FROM request_attempts WHERE request_id='req_old'"
            ).fetchone()
        assert old[0] is None
    finally:
        store.close()


def test_stats_counts_every_upstream_status_not_just_the_last_one(store) -> None:
    """The whole point: a request that met a 429 and a 502 reports both."""
    store.enqueue(_record("req_ladder", status="error", attempts=(_ladder_attempt(),)))
    store.close()

    rows = {row["status"]: row for row in store.stats()["upstream_statuses"]}
    assert rows[429]["count"] == 1
    assert rows[502]["count"] == 1
    assert rows[429]["requests"] == 1


def test_stats_reports_no_upstream_statuses_for_pre_ladder_rows(store) -> None:
    """An empty block is "not measured", never "there were no retries"."""
    store.enqueue(_record("req_plain"))
    store.close()

    assert store.stats()["upstream_statuses"] == []


def test_export_rows_roll_the_ladder_up_per_request(store) -> None:
    store.enqueue(_record("req_ladder", status="error", attempts=(_ladder_attempt(),)))
    store.close()

    rows = list(
        store.iter_export_rows(
            columns=["id", "ts_epoch", "stream"], need_bodies=False, need_ladder=True
        )
    )
    assert rows[0]["ladder_tries"] == 2
    assert (
        rows[0]["ladder_statuses"]
        == "429\N{MULTIPLICATION SIGN}1, 502\N{MULTIPLICATION SIGN}1"
    )
    assert (
        rows[0]["ladder_root_cause"]
        == "2 tries across 2 keys: 1\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502"
    )


def test_export_rows_leave_a_pre_ladder_request_blank_not_zero(store) -> None:
    store.enqueue(_record("req_plain"))
    store.close()

    rows = list(
        store.iter_export_rows(
            columns=["id", "ts_epoch", "stream"], need_bodies=False, need_ladder=True
        )
    )
    assert rows[0]["ladder_tries"] is None
    assert rows[0]["ladder_statuses"] == ""
    assert rows[0]["ladder_root_cause"] == ""


def test_export_rollup_keeps_the_root_cause_of_a_request_that_recovered(store) -> None:
    """A chain that retried its way to a success still has a story to tell."""
    store.enqueue(
        _record(
            "req_recovered",
            attempts=(
                _ladder_attempt(
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                    error_kind=None,
                    error_message=None,
                ),
            ),
        )
    )
    store.close()

    rows = list(
        store.iter_export_rows(
            columns=["id", "ts_epoch", "stream"], need_bodies=False, need_ladder=True
        )
    )
    assert (
        rows[0]["ladder_root_cause"]
        == "2 tries across 2 keys: 1\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502"
    )


def _local_answer_fixture(store: RequestLogStore) -> None:
    """Three shapes the ``local`` filter has to tell apart."""
    base = time.time()
    store.enqueue(_record("upstream", provider="p1", ts_epoch=base))
    store.enqueue(
        _record(
            "local",
            provider=None,
            optimization="title_generation_skip",
            ts_epoch=base + 1,
        )
    )
    # provider IS NULL AND optimization IS NULL: the "(unknown)" case. Not a
    # local answer, and it must survive "hide".
    store.enqueue(_record("unknown", provider=None, ts_epoch=base + 2))
    store.close()


def test_local_hide_drops_only_locally_answered_rows(store: RequestLogStore) -> None:
    _local_answer_fixture(store)
    rows, total = store.list_requests(local="hide")
    assert total == 2
    assert sorted(row["id"] for row in rows) == ["unknown", "upstream"]


def test_local_only_keeps_exactly_those_rows(store: RequestLogStore) -> None:
    _local_answer_fixture(store)
    rows, total = store.list_requests(local="only")
    assert total == 1
    assert rows[0]["id"] == "local"


def test_local_all_and_absent_are_the_same_unfiltered_query(
    store: RequestLogStore,
) -> None:
    _local_answer_fixture(store)
    _, total_all = store.list_requests(local="all")
    _, total_absent = store.list_requests()
    assert total_all == total_absent == 3


def test_stats_cache_does_not_serve_one_local_value_under_another(
    store: RequestLogStore,
) -> None:
    """Two calls inside the 5 s TTL, different ``local``, different totals.

    The cache key is the filter tuple; leaving ``local`` out of it would have
    made the second call a hit and shown "all" numbers under "hide".
    """
    _local_answer_fixture(store)
    assert store.stats(local="all")["total"] == 3
    assert store.stats(local="hide")["total"] == 2
    assert store.stats(local="only")["total"] == 1
    # And the first answer is still the first answer.
    assert store.stats(local="all")["total"] == 3


def test_pulse_counts_under_the_local_filter(store: RequestLogStore) -> None:
    _local_answer_fixture(store)
    assert store.pulse(local="hide")["total"] == 2
    assert store.pulse(local="only")["total"] == 1
    assert store.pulse()["total"] == 3


def test_local_filter_composes_with_the_synthetic_provider_keys(
    store: RequestLogStore,
) -> None:
    """``provider=local:<rule>`` and ``local=hide`` are contradictory on purpose.

    Both predicates are ANDed, so asking for a rule's rows while hiding local
    answers returns nothing rather than quietly dropping one of the two.
    """
    _local_answer_fixture(store)
    _, total = store.list_requests(provider="local:title_generation_skip", local="hide")
    assert total == 0
    _, total = store.list_requests(provider="local:title_generation_skip", local="only")
    assert total == 1
    _, total = store.list_requests(provider="(unknown)", local="hide")
    assert total == 1


# --------------------------------------------------------------------------- #
# The stats rollup
# --------------------------------------------------------------------------- #


def _rollup_fixture(store: RequestLogStore, base: float) -> None:
    """Seed a store with every shape the rollup has to reproduce.

    Several UTC hours; providers named, absent-with-an-optimization (a local
    answer) and absent-without-one (genuinely unknown); models where
    ``resolved_model`` differs from ``requested_model``; all three statuses;
    several key labels; five harnesses; rows with and without durations, TTFTs,
    route attempts, diversions, images and recovery counters.
    """
    hour = 3600.0
    providers = ["p1", "p2", None, None]
    optimizations = [None, None, "cached_answer", None]
    for index in range(48):
        provider = providers[index % 4]
        optimization = optimizations[index % 4]
        status = ("success", "error", "cancelled")[index % 3]
        attempts: tuple[RouteAttempt, ...] = ()
        if index % 8 == 0:
            attempts = (
                RouteAttempt(
                    attempt=0,
                    provider=provider,
                    model_ref="m/1",
                    outcome=RouteAttemptOutcome.FAILED,
                    params={
                        "early_retries": 2,
                        "midstream_recoveries": 1,
                        "ladder": {
                            "tries": [
                                {"status": 429},
                                {"status": 429},
                                {"status": 502},
                            ]
                        },
                    },
                    ladder_tries=3,
                ),
                RouteAttempt(
                    attempt=1,
                    provider=provider,
                    model_ref="m/2",
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                    params={"salvages": 1},
                    ladder_tries=1,
                ),
            )
        store.enqueue(
            _record(
                f"rollup-{index}",
                ts_epoch=base + (index % 6) * hour + index,
                provider=provider,
                optimization=optimization,
                requested_model=f"req-{index % 3}",
                resolved_model=f"res-{index % 4}",
                endpoint=("/v1/messages", "/v1/responses")[index % 2],
                key_label=f"key-{index % 3}",
                # Five harnesses, including the two ids that are not registry
                # entries at all, so the rollup is exercised on both halves of
                # the vocabulary the column actually holds.
                harness=("claude", "codex", "claude_agent_sdk", "script", "unknown")[
                    index % 5
                ],
                status=status,
                # Distinct, well separated counts keep every LIMIT-10 list's
                # ordering unambiguous, so the comparison is not asserting on
                # SQLite's tie-breaking.
                duration_ms=None if index % 7 == 0 else float(10 * (index + 1)),
                ttft_ms=None if index % 5 == 0 else float(index + 1),
                tokens_in=index,
                tokens_out=index * 2,
                cache_read_tokens=None if index % 4 == 0 else index,
                cache_write_tokens=index,
                tool_call_count=index % 3,
                thinking_chars=index % 5,
                input_image_count=index % 6,
                error_message=f"boom-{index % 9}" if status == "error" else None,
                route_attempt=index % 3,
                route_primary_model=f"primary-{index % 4}",
                route_diverted_from=f"diverted-{index % 2}" if index % 5 == 0 else None,
                route_diversion=(
                    ("vision_unavailable", "policy")[index % 2]
                    if index % 5 == 0
                    else None
                ),
                attempts=attempts,
            )
        )
    store.close()


_PERCENTILE_KEYS = ("p50_duration_ms", "p95_duration_ms")


def _sorted_durations(store: RequestLogStore, arguments: dict[str, Any]) -> list[float]:
    """The exact ordered sample the raw percentile path would read."""
    where, args = store._where(**arguments)
    connector = " AND" if where else " WHERE"
    with sqlite3.connect(store.db_path) as conn:
        return [
            float(row[0])
            for row in conn.execute(
                f"SELECT duration_ms FROM requests{where}{connector}"
                " duration_ms IS NOT NULL ORDER BY duration_ms",
                args,
            )
        ]


def _assert_payloads_agree(
    rolled_up,
    raw,
    label: str,
    *,
    store: RequestLogStore | None = None,
    arguments: dict[str, Any] | None = None,
) -> None:
    """Every payload key must match exactly except the bucketed percentiles.

    The percentiles get the one bound that is provable rather than a tuned
    tolerance. The histogram walk lands in the bucket holding the observation
    at the same rank the exact interpolation starts from, and returns a value
    inside that bucket -- so the estimate is within one bucket width of that
    observation, always. On a fixture this small it is emphatically *not*
    within a bucket width of the interpolated exact percentile: two samples
    decades apart interpolate to a value no bucket contains, which is the
    small-sample behaviour the design accepts and the README states.
    """
    assert rolled_up["served_from"] == "rollup", label
    assert raw["served_from"] == "rows", label
    ignored = {*_PERCENTILE_KEYS, "served_from"}
    for name in sorted(set(raw) - ignored):
        assert rolled_up[name] == raw[name], f"{label}: {name}"
    if store is None or arguments is None:
        return
    values = _sorted_durations(store, arguments)
    for name, fraction in zip(_PERCENTILE_KEYS, (0.50, 0.95), strict=True):
        approximate = rolled_up[name]
        if not values:
            assert approximate is None, f"{label}: {name}"
            assert raw[name] is None, f"{label}: {name}"
            continue
        ranked = values[int(fraction * (len(values) - 1))]
        low, high = request_log_module._latency_bucket_edges(
            request_log_module._latency_bucket(ranked)
        )
        # Rounding is monotone, so the payload's rounded value stays inside
        # the rounded edges of the bucket its exact value fell in.
        assert round(low, 2) <= approximate <= round(high, 2), (
            f"{label}: {name} ({ranked})"
        )


def test_rollup_and_raw_stats_agree_across_every_filter_combination(
    store: RequestLogStore,
) -> None:
    """The equality contract.

    Exact counters must match a raw scan for every filter ``_where`` supports,
    under all three ``local`` values, windowed and unwindowed. This is the
    contract the whole rollup rests on: if a dimension is missing or a counter
    is mismapped, a cell in this cross product disagrees.
    """
    # Hour-aligned, so the window snap of the rollup is a no-op and the two
    # paths are answering the identical question.
    base = float(int(time.time() // 3600) * 3600 - 48 * 3600)
    _rollup_fixture(store, base)

    filters: list[dict[str, Any]] = [
        {},
        {"provider": "p1"},
        {"provider": "p1,p2"},
        {"provider": "local:cached_answer"},
        {"provider": "(unknown)"},
        {"provider": "p1,local:cached_answer,(unknown)"},
        {"provider": "no-such-provider"},
        {"model": "res-1"},
        {"model": "req-2"},
        {"model": "res-1,req-2"},
        {"status": "success"},
        {"status": "error"},
        {"status": "cancelled"},
        {"endpoint": "/v1/responses"},
        {"key": "key-1"},
        {"key": "no-such-key"},
        {"harness": "claude"},
        {"harness": "claude,codex"},
        {"harness": "claude_agent_sdk"},
        {"harness": "no-such-harness"},
        {"provider": "p2", "status": "error", "endpoint": "/v1/messages"},
        {"harness": "codex", "provider": "p1", "status": "error"},
    ]
    for local in ("all", "hide", "only"):
        for since in (None, base + 2 * 3600):
            for extra in filters:
                arguments: dict[str, Any] = {
                    "local": local,
                    "since": since,
                    **extra,
                }
                label = repr(arguments)
                _assert_payloads_agree(
                    store.stats(**arguments),
                    store._stats_from_rows(**arguments),
                    label,
                    store=store,
                    arguments=arguments,
                )


def test_a_free_text_search_falls_back_to_rows(store: RequestLogStore) -> None:
    """``q`` is not a rollup dimension, so it forces the raw scan."""
    store.enqueue(_record("hit", input_text="needle in a haystack"))
    store.enqueue(_record("miss", input_text="nothing here"))
    store.close()

    searched = store.stats(q="needle")
    assert searched["served_from"] == "rows"
    assert searched["total"] == 1
    raw = store._stats_from_rows(q="needle")
    for name in sorted(searched):
        assert searched[name] == raw[name], name
    assert store.stats()["served_from"] == "rollup"


def test_the_rollup_backfill_runs_once_and_is_resumable(tmp_path) -> None:
    """Restarting mid-backfill must not double count or lose a bucket."""
    path = tmp_path / "requests.db"
    base = float(int(time.time() // 3600) * 3600 - 96 * 3600)
    seed = RequestLogStore(path, max_rows=1000)
    for index in range(60):
        seed.enqueue(_record(f"r{index}", ts_epoch=base + index * 3600.0))
    seed.close()

    first = RequestLogStore(path, max_rows=1000)
    first.close()
    complete = first.stats()
    assert complete["served_from"] == "rollup"
    assert complete["total"] == 60

    # Rewind to a partially built rollup: the completion marker is gone, the
    # resume marker sits mid-walk, and every bucket at or past it is dropped --
    # exactly the state a crash between two committed chunks leaves behind.
    midpoint = int(base) + 24 * 3600
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM request_log_meta WHERE key = ?",
            (request_log_module._ROLLUP_BACKFILL_KEY,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            (request_log_module._ROLLUP_BACKFILL_THROUGH_KEY, str(midpoint)),
        )
        for table in request_log_module._ROLLUP_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE hour_epoch >= ?", (midpoint,))

    resumed = RequestLogStore(path, max_rows=1000)
    resumed.close()
    assert resumed.stats()["total"] == 60
    for name in sorted(complete):
        if name == "served_from":
            continue
        assert resumed.stats()[name] == complete[name], name

    # A third construction finds the completion marker and does nothing.
    again = RequestLogStore(path, max_rows=1000)
    again.close()
    assert again.stats()["total"] == 60


def test_the_rollup_backfill_runs_on_the_writer_thread_not_in_init(tmp_path) -> None:
    """Construction must stay instant with a large backfill still pending.

    A full ``VACUUM`` in ``__init__`` once made construction take 17 seconds.
    The rollup backfill is the same shape of one-time work and belongs in the
    same place: the writer thread, never a request path and never here.
    """
    path = tmp_path / "requests.db"
    base = float(int(time.time() // 3600) * 3600 - 400 * 3600)
    seed = RequestLogStore(path, max_rows=20000)
    for index in range(2000):
        seed.enqueue(_record(f"r{index}", ts_epoch=base + index * 600.0))
    seed.close()
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    assert rows == 2000

    # Drop the rollup so the backfill genuinely has all 2000 rows to do again.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)",
            (
                request_log_module._ROLLUP_BACKFILL_KEY,
                request_log_module._ROLLUP_BACKFILL_THROUGH_KEY,
            ),
        )
        for table in request_log_module._ROLLUP_TABLES:
            conn.execute(f"DELETE FROM {table}")

    started = time.monotonic()
    store = RequestLogStore(path, max_rows=20000)
    construction = time.monotonic() - started
    try:
        assert construction < 0.1, f"__init__ took {construction:.3f}s"
    finally:
        store.close()
    assert store.stats()["total"] == 2000


def test_the_stats_index_is_versioned_to_v4_and_v3_is_dropped(tmp_path) -> None:
    """The index rule: a new column list means a new name and an explicit drop."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=10)
    store.enqueue(_record("r1"))
    store.close()

    with sqlite3.connect(path) as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_requests_stats_v3" not in names
        assert "idx_requests_stats_v4" in names
        plan = [
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*), SUM(tokens_in)"
                " FROM requests WHERE is_local = 0"
            )
        ]
    assert any("idx_requests_stats_v4" in step for step in plan), plan


def test_request_rows_carry_every_insert_column(store: RequestLogStore) -> None:
    """The 42/43 guard for the ``requests`` INSERT.

    A hand-written column list with a hand-written placeholder list once
    shipped one marker short and broke every write. Both are generated from
    ``_REQUEST_INSERT_COLUMNS`` now; this pins that the tuple, the row builder
    and the table agree.
    """
    columns = request_log_module._REQUEST_INSERT_COLUMNS
    assert request_log_module._REQUEST_INSERT_SQL.count("?") == len(columns)

    store.enqueue(_record("r1"))
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        table = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM requests WHERE id = 'r1'"
        ).fetchone()
    assert set(columns) <= table
    assert len(row) == len(columns)
    assert row[columns.index("id")] == "r1"
    assert row[columns.index("is_local")] == 0


def test_a_new_requests_column_must_be_declared_to_the_rollup(
    store: RequestLogStore,
) -> None:
    """The drift guard.

    A column added to ``requests`` without a decision about the rollup would
    let a new fact land silently, and the rollup would report a confident zero
    for it forever. Failing here is the prompt to make that decision.
    """
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
    unacknowledged = columns - request_log_module._ROLLUP_ACKNOWLEDGED_COLUMNS
    assert not unacknowledged, (
        f"new requests column(s) {sorted(unacknowledged)}: decide whether each"
        " is a rollup dimension (_ROLLUP_DIMENSIONS), a rollup counter"
        " (_ROLLUP_COUNTERS), or neither, then add it to"
        " _REQUEST_INSERT_COLUMNS so this set follows."
    )
    assert request_log_module._ROLLUP_ACKNOWLEDGED_COLUMNS - columns == set()


def test_pruning_does_not_shrink_the_rollup(tmp_path) -> None:
    """Retention caps ``requests``; the rollup outlives it, like the totals."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=3)
    base = time.time()
    for index in range(10):
        store.enqueue(_record(f"r{index}", ts_epoch=base + index))
    store.close()
    before = store.stats()["total"]
    with sqlite3.connect(store.db_path) as conn:
        rollup_rows = conn.execute(
            "SELECT COALESCE(SUM(requests), 0) FROM request_stats_rollup"
        ).fetchone()[0]

    store.prune()

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM request_stats_rollup"
            ).fetchone()[0]
            == rollup_rows
        )
    assert before == 10
    assert store.stats()["total"] == 10
    assert store._stats_from_rows()["total"] == 3


def test_clearing_the_log_also_clears_the_rollup(store: RequestLogStore) -> None:
    """ "Clear log" is an explicit erase, so the rollup goes with the rows."""
    store.enqueue(_record("r1"))
    store.close()
    assert store.stats()["total"] == 1

    store.clear()

    with sqlite3.connect(store.db_path) as conn:
        for table in request_log_module._ROLLUP_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert store.stats()["total"] == 0


def test_a_replayed_record_is_counted_once_in_the_rollup(tmp_path) -> None:
    """``INSERT OR REPLACE`` rewrites a row; the rollup must not re-add it."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    store.enqueue(_record("same"))
    store.close()

    replay = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    replay.enqueue(_record("same"))
    replay.close()

    with sqlite3.connect(replay.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert (
            conn.execute("SELECT SUM(requests) FROM request_stats_rollup").fetchone()[0]
            == 1
        )
    assert replay.stats()["total"] == 1


def test_the_window_snap_is_reported(store: RequestLogStore) -> None:
    """A window that is not hour-aligned is rounded outward, and says so."""
    base = float(int(time.time() // 3600) * 3600 - 3600)
    store.enqueue(_record("r1", ts_epoch=base + 60))
    store.close()

    since = base + 1800
    until = base + 3000
    payload = store.stats(since=since, until=until)

    assert payload["served_from"] == "rollup"
    assert payload["window"]["since"] == since
    assert payload["window"]["until"] == until
    assert payload["window"]["snapped_since"] == base
    assert payload["window"]["snapped_until"] == base + 3599
    # The row at base + 60 sits outside the requested window and inside the
    # snapped one, which is the whole point of reporting both.
    assert payload["total"] == 1
    assert store._stats_from_rows(since=since, until=until)["total"] == 0


def test_migrating_a_database_shaped_like_the_live_log(tmp_path) -> None:
    """An upgrade over a real 6.16.0-shaped database.

    The pre-existing totals must not be recounted, ``is_local`` must be
    computed for every row including the ``(unknown)`` ones that are not local
    answers, the index must roll to ``_v4``, and the rollup must reproduce a
    raw scan under all three ``local`` values.
    """
    path = tmp_path / "requests.db"
    base = float(int(time.time() // 3600) * 3600 - 12 * 3600)
    seed = RequestLogStore(path, max_rows=1000)
    for index in range(30):
        seed.enqueue(
            _record(
                f"r{index}",
                ts_epoch=base + index * 600.0,
                provider=(None if index % 3 else "p1"),
                optimization=("cached_answer" if index % 3 == 1 else None),
                duration_ms=float(20 * (index + 1)),
            )
        )
    seed.close()
    totals_before = seed.lifetime()["requests"]

    # Rewind the database to the previous release's shape: no ``is_local``, the
    # old index, no rollup, and the totals backfill already marked done.
    with sqlite3.connect(path) as conn:
        # The index carries the column, so it has to go first.
        conn.execute("DROP INDEX IF EXISTS idx_requests_stats_v4")
        conn.execute("ALTER TABLE requests DROP COLUMN is_local")
        conn.execute(
            "CREATE INDEX idx_requests_stats_v3 ON requests("
            " ts_epoch, status, provider, resolved_model, endpoint,"
            " requested_model, key_label, duration_ms, ttft_ms,"
            " tokens_in, tokens_out, cache_read_tokens, cache_write_tokens)"
        )
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?, ?)",
            (
                request_log_module._ROLLUP_BACKFILL_KEY,
                request_log_module._ROLLUP_BACKFILL_THROUGH_KEY,
                request_log_module._IS_LOCAL_BACKFILL_KEY,
            ),
        )
        for table in request_log_module._ROLLUP_TABLES:
            conn.execute(f"DROP TABLE {table}")

    upgraded = RequestLogStore(path, max_rows=1000)
    upgraded.close()

    with sqlite3.connect(path) as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_requests_stats_v3" not in names
        assert "idx_requests_stats_v4" in names
        wrong = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE is_local <>"
            " (CASE WHEN provider IS NULL AND optimization IS NOT NULL"
            " THEN 1 ELSE 0 END)"
        ).fetchone()[0]
    assert wrong == 0
    # The totals backfill keeps its marker, so it does not run a second time.
    assert upgraded.lifetime()["requests"] == totals_before

    for local in ("all", "hide", "only"):
        _assert_payloads_agree(
            upgraded.stats(local=local),
            upgraded._stats_from_rows(local=local),
            f"local={local}",
            store=upgraded,
            arguments=dict[str, Any](local=local),
        )


# ------------------------------------------------------------------ harness --


def _headers(user_agent: str | None = None, harness: str | None = None) -> dict:
    """One row's stored ``headers`` mapping, in the shape capture writes it."""
    stored: dict[str, str] = {}
    if user_agent is not None:
        stored["user-agent"] = user_agent
    if harness is not None:
        stored["x-mcc-harness"] = harness
    return stored


def _drop_harness_column(path) -> None:
    """Rewind a database to the shape it had before ``harness`` existed."""
    with sqlite3.connect(path) as conn:
        # The index carries the column, so it has to go first.
        conn.execute("DROP INDEX IF EXISTS idx_requests_harness_v1")
        conn.execute("ALTER TABLE requests DROP COLUMN harness")
        conn.execute(
            "DELETE FROM request_log_meta WHERE key = ?",
            (request_log_module._HARNESS_BACKFILL_KEY,),
        )


def test_the_guarded_alter_adds_harness_to_a_database_without_it(tmp_path) -> None:
    """A database created by the previous release must gain the column."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("r1"))
    seed.close()
    _drop_harness_column(path)
    with sqlite3.connect(path) as conn:
        before = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
    assert "harness" not in before

    upgraded = RequestLogStore(path, max_rows=100)
    upgraded.close()

    with sqlite3.connect(path) as conn:
        after = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "harness" in after
    # The index is versioned by name and created only after the ALTER, so its
    # presence is what proves the ordering held.
    assert "idx_requests_harness_v1" in indexes


def test_a_stored_record_round_trips_its_harness(store: RequestLogStore) -> None:
    """The column is written by the insert and read back by both queries."""
    store.enqueue(_record("r1", harness="opencode2"))
    store.close()
    detail = store.get_request("r1")
    assert detail is not None
    assert detail["harness"] == "opencode2"
    rows, _total = store.list_requests()
    assert rows[0]["harness"] == "opencode2"


def test_the_harness_backfill_classifies_rows_written_before_the_column(
    tmp_path,
) -> None:
    """History is attributed from the headers already stored on each row."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=1000)
    seed.enqueue(_record("ua", headers=_headers("claude-cli/2.0.0 (external, cli)")))
    seed.enqueue(_record("sdk", headers=_headers("opencode/1.4.2")))
    seed.enqueue(_record("explicit", headers=_headers("curl/8.4.0", harness="droid")))
    seed.enqueue(_record("silent", headers=None))
    seed.enqueue(_record("broken", headers=_headers("curl/8.4.0")))
    seed.close()
    # A blob no JSON decoder can read must not stop the walk.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE requests SET headers = '{not json' WHERE id = 'broken'")
    _drop_harness_column(path)

    upgraded = RequestLogStore(path, max_rows=1000)
    upgraded.close()

    with sqlite3.connect(path) as conn:
        stored = dict(conn.execute("SELECT id, harness FROM requests"))
        marker = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?",
            (request_log_module._HARNESS_BACKFILL_KEY,),
        ).fetchone()
    assert stored == {
        "ua": "claude",
        "sdk": "opencode",
        # The explicit header beats the user-agent, which is the whole point of
        # storing it: this row would otherwise read as a curl one-liner.
        "explicit": "droid",
        "silent": "unknown",
        "broken": "unknown",
    }
    assert marker is not None


def test_the_harness_backfill_is_resumable_and_never_redoes_work(tmp_path) -> None:
    """``harness IS NULL`` is the progress, so a restart resumes exactly."""
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=1000)
    for index in range(6):
        seed.enqueue(_record(f"r{index}", headers=_headers("opencode/1.4.2")))
    seed.close()
    _drop_harness_column(path)

    # One chunk's worth of work, committed, with a value the classifier could
    # never produce: if the resumed pass revisited these rows it would
    # overwrite the sentinel with "opencode" and the assertion below would say
    # so. Nothing else records that the chunk happened.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE requests ADD COLUMN harness TEXT")
        conn.execute(
            "UPDATE requests SET harness = 'sentinel' WHERE id IN ('r0', 'r1')"
        )

    resumed = RequestLogStore(path, max_rows=1000)
    resumed.close()

    with sqlite3.connect(path) as conn:
        stored = dict(conn.execute("SELECT id, harness FROM requests"))
    assert stored["r0"] == "sentinel"
    assert stored["r1"] == "sentinel"
    assert {stored[f"r{index}"] for index in range(2, 6)} == {"opencode"}

    # And once the marker is written the walk never runs again, however the
    # rows look: a row nulled afterwards stays null.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE requests SET harness = NULL WHERE id = 'r5'")
    reopened = RequestLogStore(path, max_rows=1000)
    reopened.close()
    with sqlite3.connect(path) as conn:
        after = conn.execute("SELECT harness FROM requests WHERE id = 'r5'").fetchone()[
            0
        ]
    assert after is None


def test_harness_filters_the_list_and_the_stats(store: RequestLogStore) -> None:
    """One filter, both surfaces, and comma-separated means "any of these"."""
    store.enqueue(_record("a", harness="claude"))
    store.enqueue(_record("b", harness="codex"))
    store.enqueue(_record("c", harness="script"))
    store.close()

    rows, total = store.list_requests(harness="claude")
    assert total == 1
    assert [row["id"] for row in rows] == ["a"]

    _rows, multi = store.list_requests(harness="claude,codex")
    assert multi == 2

    assert store.stats(harness="codex")["total"] == 1
    assert store.stats(harness="claude,script")["total"] == 2
    assert store.stats(harness="no-such-harness")["total"] == 0
    assert store.pulse(harness="claude")["total"] == 1


def test_by_harness_totals_equal_the_raw_group_by(store: RequestLogStore) -> None:
    """The breakdown is a claim about the table; check it against the table."""
    for index in range(9):
        store.enqueue(
            _record(f"r{index}", harness=("claude", "codex", "script")[index % 3])
        )
    store.close()

    with sqlite3.connect(store.db_path) as conn:
        expected = dict(
            conn.execute("SELECT harness, COUNT(*) FROM requests GROUP BY harness")
        )
    stats = store.stats()
    assert {row["key"]: row["requests"] for row in stats["by_harness"]} == expected
    assert stats["by_harness_truncated"] is False


def test_harness_usage_counts_only_the_requested_window(
    store: RequestLogStore,
) -> None:
    """The card's window is a filter on the rows, not on the labels."""
    now = time.time()
    store.enqueue(_record("recent", ts_epoch=now - 3600, harness="claude"))
    store.enqueue(_record("also", ts_epoch=now - 7200, harness="claude"))
    store.enqueue(_record("old", ts_epoch=now - 40 * 86_400, harness="codex"))
    store.close()

    assert store.harness_usage(since=now - 7 * 86_400) == {"claude": 2}
    assert store.harness_usage(since=now - 90 * 86_400) == {"claude": 2, "codex": 1}


def test_the_harness_breakdown_is_served_by_its_own_index(
    store: RequestLogStore,
) -> None:
    """The index exists to keep the all-time breakdown off a full table scan."""
    store.enqueue(_record("r1", harness="claude"))
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN"
                " SELECT harness, COUNT(*) FROM requests GROUP BY harness"
            )
        )
    assert "idx_requests_harness_v1" in plan


def test_the_stats_cache_key_carries_every_filter_including_harness(
    store: RequestLogStore,
) -> None:
    """Ten filters, ten slots.

    Two calls that differ only in ``harness`` must not share a cache entry, or
    one harness's numbers are served as another's for the TTL.
    """
    store.enqueue(_record("a", harness="claude"))
    store.enqueue(_record("b", harness="codex"))
    store.close()
    assert store.stats(harness="claude")["total"] == 1
    assert store.stats(harness="codex")["total"] == 1
    with store._stats_lock:
        keys = list(store._stats_cache)
    assert all(len(key) == 10 for key in keys)
    assert {key[9] for key in keys} == {"claude", "codex"}


def test_the_rollup_tables_are_rebuilt_when_they_lack_the_harness_dimension(
    tmp_path,
) -> None:
    """A rollup keyed on nine columns cannot answer a ten-column question.

    ``CREATE TABLE IF NOT EXISTS`` never revises an existing definition, so the
    stale tables have to be dropped and rebuilt or every later upsert folds two
    harnesses into one bucket.
    """
    path = tmp_path / "requests.db"
    base = float(int(time.time() // 3600) * 3600 - 6 * 3600)
    seed = RequestLogStore(path, max_rows=1000)
    for index in range(12):
        seed.enqueue(
            _record(
                f"r{index}",
                ts_epoch=base + index * 600.0,
                harness=("claude", "codex", "script")[index % 3],
                duration_ms=float(10 * (index + 1)),
            )
        )
    seed.close()

    # Rewind the rollup to the previous release's shape: three tables with no
    # harness dimension, a bogus row inside them, and the old marker names.
    with sqlite3.connect(path) as conn:
        for table in request_log_module._ROLLUP_TABLES:
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            "CREATE TABLE request_stats_rollup ("
            " hour_epoch INTEGER NOT NULL, is_local INTEGER NOT NULL,"
            " provider TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (hour_epoch, is_local, provider)) WITHOUT ROWID"
        )
        conn.execute("CREATE TABLE request_stats_latency (bucket INTEGER)")
        conn.execute("CREATE TABLE request_stats_detail (kind TEXT)")
        conn.execute("INSERT INTO request_stats_rollup VALUES (0, 0, 'stale', 999999)")
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)",
            (
                request_log_module._ROLLUP_BACKFILL_KEY,
                request_log_module._ROLLUP_BACKFILL_THROUGH_KEY,
            ),
        )
        for key in request_log_module._SUPERSEDED_ROLLUP_KEYS:
            conn.execute(
                "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
                (key, "1"),
            )

    upgraded = RequestLogStore(path, max_rows=1000)
    upgraded.close()

    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(request_stats_rollup)")
        }
        stale = conn.execute(
            "SELECT COUNT(*) FROM request_stats_rollup WHERE provider = 'stale'"
        ).fetchone()[0]
        superseded = conn.execute(
            "SELECT COUNT(*) FROM request_log_meta WHERE key IN (?, ?)",
            request_log_module._SUPERSEDED_ROLLUP_KEYS,
        ).fetchone()[0]
    assert "harness" in columns
    # Dropped, not carried forward: those counts were keyed on a tuple that no
    # longer exists and could never be split by harness after the fact.
    assert stale == 0
    # The pre-harness marker names are gone, so nothing reads them again.
    assert superseded == 0

    # And the rebuilt rollup answers the same as a raw scan, harness included.
    _assert_payloads_agree(
        upgraded.stats(),
        upgraded._stats_from_rows(),
        "rebuilt",
        store=upgraded,
        arguments={},
    )
    assert {row["key"] for row in upgraded.stats()["by_harness"]} == {
        "claude",
        "codex",
        "script",
    }


def test_the_versioned_rollup_marker_cannot_be_satisfied_by_an_old_one(
    tmp_path,
) -> None:
    """The short-circuit must not read a pre-harness completion marker.

    A v1 marker left on a database whose rollup was built without the harness
    dimension would make ``_ensure_rollup_backfill`` skip, and ``stats()``
    would serve a confident, permanently wrong ``by_harness``.
    """
    assert request_log_module._ROLLUP_BACKFILL_KEY not in (
        request_log_module._SUPERSEDED_ROLLUP_KEYS
    )
    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("r1", harness="claude"))
    seed.close()
    # Only the superseded names present: the rollup must still be built.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)",
            (
                request_log_module._ROLLUP_BACKFILL_KEY,
                request_log_module._ROLLUP_BACKFILL_THROUGH_KEY,
            ),
        )
        for table in request_log_module._ROLLUP_TABLES:
            conn.execute(f"DELETE FROM {table}")
        for key in request_log_module._SUPERSEDED_ROLLUP_KEYS:
            conn.execute(
                "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
                (key, "1"),
            )

    upgraded = RequestLogStore(path, max_rows=100)
    upgraded.close()
    stats = upgraded.stats()
    assert stats["served_from"] == "rollup"
    assert [row["key"] for row in stats["by_harness"]] == ["claude"]
