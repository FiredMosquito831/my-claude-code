"""The 5-minute / 1-hour split of a request's cache writes, as stored (7.111.0).

Two nullable ``requests`` columns, added by a guarded ``ALTER TABLE`` only:
``cache_write_5m_tokens`` and ``cache_write_1h_tokens``. NULL is "the host did
not report the split" -- every non-Anthropic host and every row written before
7.111.0 -- and 0 is a reported zero. The one-time cost backfill never prices a
row with 1-hour writes (it is handed one write count).
"""

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    _ADDED_COLUMNS,
    _REQUEST_INSERT_COLUMNS,
    _REQUEST_INSERT_SQL,
    RequestLogStore,
    RequestRecord,
    required_request_columns,
    set_cost_backfill_pricer,
)

_SPLIT = ("cache_write_5m_tokens", "cache_write_1h_tokens")


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-5",
        "provider": "anthropic_oauth",
        "resolved_model": "claude-sonnet-5",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "cache_write_tokens": 150,
        "status": "success",
        "ts_epoch": 1785542400.0,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _columns(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [
            str(row[1]) for row in connection.execute("PRAGMA table_info(requests)")
        ]
    finally:
        connection.close()


def _drop_split(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for column in _SPLIT:
            connection.execute(f"ALTER TABLE requests DROP COLUMN {column}")
        connection.commit()
    finally:
        connection.close()


class TestTheColumns:
    def test_both_are_post_release_columns_declared_once(self) -> None:
        names = [name for name, _sql in _ADDED_COLUMNS]
        for column in _SPLIT:
            assert names.count(column) == 1
            assert column in _REQUEST_INSERT_COLUMNS
            # ALTER-only: never part of the original CREATE TABLE set that
            # ``LEGACY_REQUEST_LOG_COLUMNS`` mirrors.
            assert column not in required_request_columns()

    def test_the_insert_has_one_placeholder_per_column(self) -> None:
        assert _REQUEST_INSERT_SQL.count("?") == len(_REQUEST_INSERT_COLUMNS)

    def test_no_default_is_declared(self) -> None:
        """A DEFAULT would turn "not reported" into a measured zero."""
        for name, sql in _ADDED_COLUMNS:
            if name in _SPLIT:
                assert sql == f"ALTER TABLE requests ADD COLUMN {name} INTEGER"


class TestTheRoundTrip:
    @pytest.fixture
    def store(self, tmp_path):
        store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
        yield store
        store.close()

    def test_a_reported_split_round_trips(self, store) -> None:
        store.enqueue(
            _record("split", cache_write_5m_tokens=100, cache_write_1h_tokens=50)
        )
        store.close()
        row = store.get_request("split")
        assert row is not None
        assert (row["cache_write_5m_tokens"], row["cache_write_1h_tokens"]) == (100, 50)
        assert row["cache_write_tokens"] == 150

    def test_not_reported_is_null_and_never_zero(self, store) -> None:
        store.enqueue(_record("total-only"))
        store.close()
        row = store.get_request("total-only")
        assert row is not None
        assert row["cache_write_5m_tokens"] is None
        assert row["cache_write_1h_tokens"] is None
        assert row["cache_write_tokens"] == 150

    def test_a_reported_zero_is_stored_as_zero(self, store) -> None:
        store.enqueue(
            _record("five-only", cache_write_5m_tokens=150, cache_write_1h_tokens=0)
        )
        store.close()
        row = store.get_request("five-only")
        assert row is not None
        assert row["cache_write_1h_tokens"] == 0
        assert row["cache_write_5m_tokens"] == 150


class TestTheMigration:
    def test_an_old_database_gains_both_columns_once_and_keeps_its_rows(
        self, tmp_path
    ) -> None:
        path = tmp_path / "old.db"
        store = RequestLogStore(path, max_rows=100)
        store.enqueue(_record("before"))
        store.close()
        _drop_split(path)
        assert not set(_SPLIT) & set(_columns(path))

        migrated = RequestLogStore(path, max_rows=100)
        migrated.enqueue(
            _record("after", cache_write_5m_tokens=1, cache_write_1h_tokens=149)
        )
        migrated.close()

        columns = _columns(path)
        for column in _SPLIT:
            assert columns.count(column) == 1
        before = migrated.get_request("before")
        after = migrated.get_request("after")
        assert before is not None and after is not None
        # A row written before the columns existed reads "not reported".
        assert before["cache_write_1h_tokens"] is None
        assert before["cache_write_5m_tokens"] is None
        assert after["cache_write_1h_tokens"] == 149

    def test_the_migration_runs_once_and_is_a_no_op_after(self, tmp_path) -> None:
        """Guarded by ``PRAGMA table_info``: the second pass issues no ALTER at all."""

        path = tmp_path / "guarded.db"
        RequestLogStore(path, max_rows=100).close()
        _drop_split(path)

        def alters(connection: sqlite3.Connection) -> list[str]:
            seen: list[str] = []
            connection.set_trace_callback(seen.append)
            RequestLogStore._ensure_added_columns(connection)
            connection.set_trace_callback(None)
            return [sql for sql in seen if sql.lstrip().upper().startswith("ALTER")]

        connection = sqlite3.connect(path)
        try:
            first = alters(connection)
            second = alters(connection)
        finally:
            connection.close()
        assert [sql.split()[-2] for sql in first] == list(_SPLIT)
        assert second == []


def _meta(path: Path, key: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (key,)
        ).fetchone()
    return None if row is None else str(row[0])


def test_the_backfill_never_prices_a_row_with_one_hour_writes(tmp_path) -> None:
    """It is handed one write count, so it would price 1-hour writes at the 5-minute rate."""

    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    for record in (
        _record("no-split"),
        _record("five-only", cache_write_5m_tokens=150, cache_write_1h_tokens=0),
        _record("one-hour", cache_write_5m_tokens=100, cache_write_1h_tokens=50),
    ):
        store.enqueue(record)
    store.close()
    calls: list[tuple[Any, ...]] = []

    def pricer(*args: Any) -> tuple[float | None, str | None]:
        calls.append(args)
        return (0.25, "models_dev_backfill")

    set_cost_backfill_pricer(pricer)
    walker = RequestLogStore(path, max_rows=100)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _meta(path, request_log_module._COST_BACKFILL_KEY) is not None:
                break
            time.sleep(0.05)
    finally:
        walker.close()
    with sqlite3.connect(path) as conn:
        costs = {
            str(row[0]): (row[1], row[2])
            for row in conn.execute("SELECT id, cost_usd, cost_source FROM requests")
        }
    assert costs == {
        "no-split": (0.25, "models_dev_backfill"),
        "five-only": (0.25, "models_dev_backfill"),
        "one-hour": (None, None),
    }
    assert len(calls) == 2
