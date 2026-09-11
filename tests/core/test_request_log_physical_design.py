"""The physical design of the request log: memory mapping and partial indexes.

Neither changes an answer. Every test here is therefore either a *plan*
assertion -- the query must reach its index -- or an *equality* assertion --
the faster path must return exactly what the slower one returned. Wall-time
assertions are deliberately absent: a timing assertion on CI is a flake
generator, and the measurements that justify these indexes are in the
docstrings of the code they justify.
"""

import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import RequestLogStore, RequestRecord

_INDEX_NAMES = ("idx_requests_image_v1", "idx_requests_optimization_v1")


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 120.0,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _seeded(path: Path) -> RequestLogStore:
    """A store holding rows of every shape the two indexes care about."""
    store = RequestLogStore(path, max_rows=10_000)
    now = time.time()
    for index in range(30):
        store.enqueue(
            _record(
                f"plain-{index}",
                ts_epoch=now - index,
                est_image_tokens=None,
                optimization=None,
            )
        )
    for index in range(10):
        store.enqueue(
            _record(
                f"image-{index}",
                ts_epoch=now - index,
                est_tokens_in=100 + index,
                est_image_tokens=50 + index,
                input_image_count=1,
                image_bytes_in=4096,
                cache_read_tokens=0,
            )
        )
    for index in range(10):
        store.enqueue(
            _record(
                f"opt-{index}",
                ts_epoch=now - index,
                optimization="local:echo",
                optimization_tokens_saved=7 + index,
            )
        )
    return store


def _wait_for_indexes(path: Path) -> set[str]:
    """The indexes are a writer-thread migration, so poll rather than assume."""
    deadline = time.monotonic() + 10.0
    names: set[str] = set()
    while time.monotonic() < deadline:
        conn = sqlite3.connect(path)
        try:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            conn.close()
        if set(_INDEX_NAMES) <= names:
            break
        time.sleep(0.05)
    return names


def _mmap_is_available(path: Path) -> bool:
    """Builds compiled with SQLITE_MAX_MMAP_SIZE=0 answer 0 to any request."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA mmap_size=1048576")
        return int(conn.execute("PRAGMA mmap_size").fetchone()[0]) > 0
    finally:
        conn.close()


def test_the_store_sets_an_mmap_size_proportional_to_the_database(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        store.close()
        size = store.db_path.stat().st_size
        expected = store._mmap_size()
        assert 0 < expected <= request_log_module._MMAP_MAX_BYTES
        assert expected >= size

        if not _mmap_is_available(store.db_path):
            return
        conn = store._connect()
        try:
            assert int(conn.execute("PRAGMA mmap_size").fetchone()[0]) == expected
        finally:
            conn.close()
    finally:
        store.close()


def test_the_mmap_size_is_capped_and_zero_for_a_missing_file(
    tmp_path, monkeypatch
) -> None:
    """A large database maps the cap; a file that is not there maps nothing."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        store.close()
        missing = RequestLogStore.__new__(RequestLogStore)
        missing._db_path = tmp_path / "not-here" / "requests.db"
        assert missing._mmap_size() == 0

        # A cap below the file's own size proves the minimum is taken, without
        # needing a gigabyte of fixture.
        monkeypatch.setattr(request_log_module, "_MMAP_MAX_BYTES", 4096)
        assert store._mmap_size() == 4096
    finally:
        store.close()


def test_every_partial_index_has_a_versioned_name() -> None:
    """Changing a column list means a new name and a drop, never a silent keep."""
    for statement in request_log_module._PARTIAL_INDEXES:
        match = re.search(r"CREATE INDEX IF NOT EXISTS (\S+)", statement)
        assert match is not None, statement
        assert re.search(r"_v\d+$", match.group(1)), match.group(1)
        assert " WHERE " in statement, "a partial index without a WHERE is not partial"


def test_the_partial_indexes_are_created_on_the_writer_thread(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        assert set(_INDEX_NAMES) <= _wait_for_indexes(store.db_path)
    finally:
        store.close()


def test_image_estimate_uses_the_partial_index(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        store.close()
        _wait_for_indexes(store.db_path)
        conn = store._connect()
        try:
            sql = store._image_estimate_sql(conn, " AND ts_epoch >= ?")
            plan = " | ".join(
                str(row[3])
                for row in conn.execute("EXPLAIN QUERY PLAN " + sql, (0.0, 50))
            )
        finally:
            conn.close()
        assert "idx_requests_image_v1" in plan, plan
    finally:
        store.close()


def test_image_estimate_matches_the_unhinted_query(tmp_path) -> None:
    """The equality oracle: the hint may change the plan, never the answer."""
    store = _seeded(tmp_path / "requests.db")
    try:
        store.close()
        _wait_for_indexes(store.db_path)
        hinted = store.image_estimate_by_provider(since=0.0)

        conn = store._connect()
        try:
            conn.execute("DROP INDEX IF EXISTS idx_requests_image_v1")
            conn.commit()
            sql = store._image_estimate_sql(conn, " AND ts_epoch >= ?")
            assert "INDEXED BY" not in sql
            unhinted = [dict(row) for row in conn.execute(sql, (0.0, 50)).fetchall()]
        finally:
            conn.close()

        assert hinted == unhinted
        assert hinted, "the fixture must produce at least one host"
    finally:
        store.close()


def test_the_image_estimate_still_answers_without_its_index(tmp_path) -> None:
    """``INDEXED BY`` is an error when the index is gone -- so it is dropped."""
    store = _seeded(tmp_path / "requests.db")
    try:
        store.close()
        _wait_for_indexes(store.db_path)
        expected = store.image_estimate_by_provider(since=0.0)

        conn = store._connect()
        try:
            conn.execute("DROP INDEX IF EXISTS idx_requests_image_v1")
            conn.commit()
        finally:
            conn.close()
        # A different ``since`` so the five-second cache cannot answer instead.
        store._stats_cache.clear()
        assert store.image_estimate_by_provider(since=0.0) == expected
    finally:
        store.close()


def test_optimization_stats_uses_the_partial_index(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        store.close()
        _wait_for_indexes(store.db_path)
        conn = store._connect()
        try:
            plan = " | ".join(
                str(row[3])
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN"
                    " SELECT optimization AS rule, COUNT(*) AS requests,"
                    " COALESCE(SUM(optimization_tokens_saved), 0) AS tokens_saved,"
                    " MIN(ts_epoch) AS first_ts, MAX(ts_epoch) AS last_ts"
                    " FROM requests WHERE optimization IS NOT NULL"
                    " GROUP BY rule ORDER BY requests DESC"
                )
            )
        finally:
            conn.close()
        assert "idx_requests_optimization_v1" in plan, plan
    finally:
        store.close()


def test_the_optimization_totals_are_unchanged_by_the_index(tmp_path) -> None:
    """Equality oracle for the panel the index serves."""
    store = _seeded(tmp_path / "requests.db")
    try:
        store.close()
        _wait_for_indexes(store.db_path)
        with_index = store.optimization_stats()

        conn = store._connect()
        try:
            conn.execute("DROP INDEX IF EXISTS idx_requests_optimization_v1")
            conn.commit()
        finally:
            conn.close()
        assert store.optimization_stats() == with_index
    finally:
        store.close()


def test_the_indexes_are_idempotent_across_restarts(tmp_path) -> None:
    path = tmp_path / "requests.db"
    _seeded(path).close()
    for _ in range(3):
        RequestLogStore(path, max_rows=10_000).close()
    assert set(_INDEX_NAMES) <= _wait_for_indexes(path)
