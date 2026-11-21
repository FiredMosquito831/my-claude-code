"""III.8 pre-PR (v8.33.0) SQLite migration & store query tests for 8.34.0."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_claude_code.core import export as export_engine
from my_claude_code.websearch.analytics import (
    _ATTEMPT_MIGRATION_COLUMNS,
    _ROUTE_MIGRATION_COLUMNS,
    SKIPPED_STATUS,
    WebSearchLogStore,
    _initialize_schema,
)
from my_claude_code.websearch.registry import SearchOutcome, SearchRouteOutcome

_TS = datetime(2026, 11, 20, 12, 0, tzinfo=UTC).timestamp()

#: Exact v8.33.0 schema (27-column search_log + 16-column search_route_log).
V833_SCHEMA = """
CREATE TABLE search_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    provider TEXT NOT NULL,
    key_index INTEGER NOT NULL,
    key_label TEXT NOT NULL,
    query TEXT NOT NULL,
    results_count INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    cost_usd REAL,
    route_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    input_json TEXT,
    output_json TEXT,
    provider_config_json TEXT,
    input_chars INTEGER,
    output_chars INTEGER,
    input_sha256 TEXT,
    output_sha256 TEXT,
    content_captured INTEGER NOT NULL DEFAULT 0,
    chain_position INTEGER,
    outcome TEXT,
    failure_kind TEXT,
    tries_json TEXT
);
CREATE TABLE search_route_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL UNIQUE,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    query TEXT NOT NULL,
    primary_provider TEXT NOT NULL,
    terminal_provider TEXT NOT NULL,
    provider_path TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    fallback_used INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    results_count INTEGER NOT NULL,
    cost_usd REAL,
    error_kind TEXT,
    error_message TEXT
);
CREATE TABLE search_log_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX idx_search_log_ts ON search_log (ts_epoch);
CREATE INDEX idx_search_log_provider_ts ON search_log (provider, ts_epoch);
CREATE INDEX idx_search_route_log_ts ON search_route_log (ts_epoch);
CREATE INDEX idx_search_route_log_terminal_ts ON search_route_log (terminal_provider, ts_epoch);
CREATE INDEX idx_search_log_route_id ON search_log (route_id);
"""

_V833_ATTEMPT_INSERT = """
INSERT INTO search_log (
    ts_epoch, ts_iso, provider, key_index, key_label, query,
    results_count, duration_ms, status, error_kind, error_message, cost_usd,
    route_id, attempt_number, input_json, output_json, provider_config_json,
    input_chars, output_chars, input_sha256, output_sha256, content_captured,
    chain_position, outcome, failure_kind, tries_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_V833_ROUTE_INSERT = """
INSERT INTO search_route_log (
    route_id, ts_epoch, ts_iso, query, primary_provider, terminal_provider,
    provider_path, attempt_count, fallback_used, duration_ms, status,
    results_count, cost_usd, error_kind, error_message
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _table_columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _create_v833_snapshot(path: Path) -> None:
    tries_rotated = json.dumps(
        [
            {
                "key_index": 0,
                "key_label": "exak…0000",
                "try": 1,
                "status": 402,
                "kind": "quota",
                "ms": 40.0,
                "ok": False,
                "charged": "quota",
                "benched_for_s": 1800.0,
                "decision": "exa: exceeded credits limit",
                "rotated": True,
            },
            {
                "key_index": 1,
                "key_label": "exak…1111",
                "try": 1,
                "status": None,
                "kind": None,
                "ms": 55.0,
                "ok": True,
            },
        ],
        separators=(",", ":"),
    )
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V833_SCHEMA)
        rows = [
            # 1: normal success
            (
                _TS,
                "2026-11-20T12:00:00+00:00",
                "exa",
                1,
                "exak…1111",
                "normal query",
                3,
                120.0,
                "success",
                None,
                None,
                0.005,
                "route-pre-1",
                1,
                None,
                None,
                None,
                10,
                20,
                None,
                None,
                1,
                0,
                "succeeded",
                None,
                tries_rotated,
            ),
            # 2: historical empty-200
            (
                _TS + 1,
                "2026-11-20T12:00:01+00:00",
                "tavily",
                0,
                "tvly…0000",
                "empty query",
                0,
                90.0,
                "success",
                None,
                None,
                None,
                "route-pre-2",
                1,
                None,
                None,
                None,
                10,
                5,
                None,
                None,
                1,
                0,
                "succeeded",
                None,
                None,
            ),
            # 3: error row
            (
                _TS + 2,
                "2026-11-20T12:00:02+00:00",
                "exa",
                0,
                "exak…0000",
                "broken query",
                0,
                45.0,
                "error",
                "rate_limit",
                "429",
                None,
                "route-pre-3",
                1,
                None,
                None,
                None,
                10,
                0,
                None,
                None,
                1,
                0,
                "failed",
                "rate_limit",
                None,
            ),
            # 4: skipped row
            (
                _TS + 3,
                "2026-11-20T12:00:03+00:00",
                "context7",
                0,
                "",
                "skipped query",
                0,
                0.0,
                SKIPPED_STATUS,
                None,
                "skipped: paused",
                None,
                "route-pre-3",
                2,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                0,
                1,
                "skipped",
                None,
                None,
            ),
            # 5: legacy web_fetch row
            (
                _TS + 4,
                "2026-11-20T12:00:04+00:00",
                "web_fetch",
                0,
                "",
                "https://example.invalid/doc",
                1,
                80.0,
                "success",
                None,
                None,
                None,
                None,
                1,
                None,
                None,
                None,
                12,
                200,
                None,
                None,
                1,
                None,
                None,
                None,
                None,
            ),
        ]
        conn.executemany(_V833_ATTEMPT_INSERT, rows)
        conn.execute(
            _V833_ROUTE_INSERT,
            (
                "route-pre-1",
                _TS,
                "2026-11-20T12:00:00+00:00",
                "normal query",
                "exa",
                "exa",
                "|exa|",
                1,
                0,
                120.0,
                "success",
                3,
                0.005,
                None,
                None,
            ),
        )
        conn.execute(
            "INSERT INTO search_log_meta (key, value) VALUES ('chain_skipped_rows_v1', '1')"
        )
        conn.commit()
    finally:
        conn.close()


def test_v833_snapshot_migrates_additively_and_preserves_counts_and_rollups(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "websearch.db"
    _create_v833_snapshot(db_path)

    pre_cols_attempt = _table_columns(db_path, "search_log")
    pre_cols_route = _table_columns(db_path, "search_route_log")
    assert len(pre_cols_attempt) == 27
    assert len(pre_cols_route) == 16

    conn = sqlite3.connect(db_path)
    try:
        pre_attempt_count = conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[
            0
        ]
        pre_route_count = conn.execute(
            "SELECT COUNT(*) FROM search_route_log"
        ).fetchone()[0]
    finally:
        conn.close()
    assert pre_attempt_count == 5
    assert pre_route_count == 1

    store = WebSearchLogStore(db_path)
    try:
        # Force reader schema check + idempotent second open
        stats = store.stats("daily")
    finally:
        store.close()

    # Reopen idempotently
    store2 = WebSearchLogStore(db_path)
    try:
        post_cols_attempt = _table_columns(db_path, "search_log")
        post_cols_route = _table_columns(db_path, "search_route_log")
        assert len(post_cols_attempt) == 31
        assert len(post_cols_route) == 20
        for col, _ in _ATTEMPT_MIGRATION_COLUMNS:
            assert post_cols_attempt.count(col) == 1
        for col, _ in _ROUTE_MIGRATION_COLUMNS:
            assert post_cols_route.count(col) == 1

        conn = sqlite3.connect(db_path)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
                == pre_attempt_count
            )
            assert (
                conn.execute("SELECT COUNT(*) FROM search_route_log").fetchone()[0]
                == pre_route_count
            )
            idx_names = {
                row[1]
                for row in conn.execute("PRAGMA index_list(search_log)").fetchall()
            }
            assert "idx_search_log_ts_outcome" in idx_names
            assert "idx_search_log_route_id" in idx_names
        finally:
            conn.close()

        # web_fetch and skipped rows excluded from default search stats
        assert stats["totals"]["requests"] == 3
        assert stats["totals"]["errors"] == 1
        assert stats["totals"]["successes"] == 2

        # by_key aggregates intermediate failed tries from tries_json
        by_key = {(r["provider"], r["key_label"]): r for r in stats["by_key"]}
        assert by_key[("exa", "exak…0000")]["tries_failed"] == 1
        assert by_key[("exa", "exak…0000")]["key_rotations"] == 1
        assert by_key[("exa", "exak…1111")]["tries_succeeded"] == 1

        # web_fetch split filters
        fetch_stats = store2.stats("daily", tool_kind="web_fetch")
        assert fetch_stats["totals"]["requests"] == 1
        assert store2.requests(tool_kind="web_fetch")["total"] == 1

        all_stats = store2.stats("daily", tool_kind="all")
        assert all_stats["totals"]["requests"] == 4

        # status="skipped" filter
        skipped = store2.requests(status="skipped")
        assert skipped["total"] == 1
        assert skipped["items"][0]["provider"] == "context7"

        # empty_only derived compatibility on pre-migration empty-200 row
        empty_reqs = store2.requests(empty_only=True)
        assert empty_reqs["total"] == 1
        assert empty_reqs["items"][0]["provider"] == "tavily"

        # routes() returns migrated route with defaulted counters
        routes = store2.routes()
        assert routes["total"] == 1
        route = routes["items"][0]
        assert route["route_id"] == "route-pre-1"
        assert route["door"] is None
        assert route["skipped_count"] == 0
        assert route["key_rotations"] == 0
        assert route["empty_fallback_count"] == 0

        # Export iterators keep web_fetch split aligned with stats()/requests()
        cols = export_engine.websearch_detail_columns(["provider", "tool_kind"])
        exported_default = list(
            store2.iter_export_rows(columns=cols, include_content=False)
        )
        assert len(exported_default) == 4  # 3 counted + 1 skipped, excludes web_fetch
        exported_fetch = list(
            store2.iter_export_rows(
                columns=cols, include_content=False, tool_kind="web_fetch"
            )
        )
        assert len(exported_fetch) == 1
        assert exported_fetch[0]["provider"] == "web_fetch"
    finally:
        store2.close()


@pytest.mark.parametrize("done", [1, 2, 3])
def test_partial_v834_migration_completes_on_reopen(tmp_path: Path, done: int) -> None:
    db_path = tmp_path / "partial.db"
    _create_v833_snapshot(db_path)
    conn = sqlite3.connect(db_path)
    try:
        for col, col_def in _ATTEMPT_MIGRATION_COLUMNS[:done]:
            conn.execute(f"ALTER TABLE search_log ADD COLUMN {col} {col_def}")
        for col, col_def in _ROUTE_MIGRATION_COLUMNS[:done]:
            conn.execute(f"ALTER TABLE search_route_log ADD COLUMN {col} {col_def}")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    try:
        _initialize_schema(conn)
    finally:
        conn.close()

    assert len(_table_columns(db_path, "search_log")) == 31
    assert len(_table_columns(db_path, "search_route_log")) == 20


def test_by_key_surfaces_key_only_present_in_tries_json(tmp_path: Path) -> None:
    """Dead k0 rotated to k1 on every search -> k0 must still appear in by_key."""
    store = WebSearchLogStore(tmp_path / "ws.db")
    try:
        tries = json.dumps(
            [
                {
                    "key_index": 0,
                    "key_label": "exak…dead",
                    "try": 1,
                    "status": 402,
                    "kind": "quota",
                    "ms": 15.0,
                    "ok": False,
                    "rotated": True,
                },
                {
                    "key_index": 1,
                    "key_label": "exak…live",
                    "try": 1,
                    "status": None,
                    "kind": None,
                    "ms": 30.0,
                    "ok": True,
                },
            ]
        )
        store.record(
            SearchOutcome(
                ts_epoch=_TS,
                ts_iso="2026-11-20T12:00:00+00:00",
                provider="exa",
                key_index=1,
                key_label="exak…live",
                query="q",
                results_count=2,
                duration_ms=45.0,
                status="success",
                error_kind=None,
                error_message=None,
                cost_usd=None,
                route_id="r-tries-only",
                tries_json=tries,
                door="rest",
                http_status=200,
            )
        )
        store.record_route(
            SearchRouteOutcome(
                route_id="r-tries-only",
                ts_epoch=_TS,
                ts_iso="2026-11-20T12:00:00+00:00",
                query="q",
                primary_provider="exa",
                terminal_provider="exa",
                provider_path=("exa",),
                attempt_count=1,
                fallback_used=False,
                duration_ms=45.0,
                status="success",
                results_count=2,
                cost_usd=None,
                error_kind=None,
                error_message=None,
                door="rest",
                skipped_count=0,
                key_rotations=1,
                empty_fallback_count=0,
            )
        )
        store.flush()
        stats = store.stats("daily")
        by_key = {(row["provider"], row["key_label"]): row for row in stats["by_key"]}
        assert ("exa", "exak…dead") in by_key
        dead = by_key[("exa", "exak…dead")]
        assert dead["requests"] == 0
        assert dead["tries_total"] == 1
        assert dead["tries_failed"] == 1
        assert dead["key_rotations"] == 1
        assert dead["key_index"] == 0
        live = by_key[("exa", "exak…live")]
        assert live["requests"] == 1
        assert live["tries_succeeded"] == 1
        assert live["tries_failed"] == 0
    finally:
        store.close()
