"""``search_log``'s search-chain attempt columns (7.112.0): ALTER-only, nullable.

III.8: a new column is an ``ALTER TABLE`` behind a ``PRAGMA table_info``
guard; the INSERT list and its placeholders come from one tuple; NULL means
"not measured" and is never written as 0 or "". A row written without a chain
keeps NULL in every new column, and the stats count exactly what they did.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_claude_code.api.web_tools import search_chain
from my_claude_code.websearch import analytics
from my_claude_code.websearch.analytics import (
    _CHAIN_ATTEMPT_COLUMNS,
    _INSERT_COLUMNS,
    _INSERT_SQL,
    SKIPPED_STATUS,
    WebSearchLogStore,
    _initialize_schema,
    _row_tuple,
)
from my_claude_code.websearch.registry import SearchOutcome

CHAIN_COLUMNS = ("chain_position", "outcome", "failure_kind", "tries_json")

#: ``search_log`` exactly as 7.111.6 (and every version since 7.77.0) creates
#: it -- the shape every existing websearch.db has.
PRE_CHAIN_SCHEMA = """
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
    content_captured INTEGER NOT NULL DEFAULT 0
);
"""

#: The INSERT 7.111.6 runs -- an old version writing a migrated file.
PRE_CHAIN_INSERT = """
INSERT INTO search_log (
    ts_epoch, ts_iso, provider, key_index, key_label, query,
    results_count, duration_ms, status, error_kind, error_message, cost_usd,
    route_id, attempt_number, input_json, output_json, provider_config_json,
    input_chars, output_chars, input_sha256, output_sha256, content_captured
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_TS = datetime(2026, 10, 24, 12, 0, tzinfo=UTC).timestamp()


def _outcome(
    *,
    status: str = "success",
    chain_position: int | None = None,
    outcome: str | None = None,
    failure_kind: str | None = None,
    tries_json: str | None = None,
) -> SearchOutcome:
    return SearchOutcome(
        ts_epoch=_TS,
        ts_iso=datetime.fromtimestamp(_TS, tz=UTC).isoformat(),
        provider="exa",
        key_index=0,
        key_label="exa-…1111",
        query="q",
        results_count=1,
        duration_ms=1.0,
        status=status,
        error_kind=None,
        error_message=None,
        cost_usd=None,
        route_id="route-1",
        chain_position=chain_position,
        outcome=outcome,
        failure_kind=failure_kind,
        tries_json=tries_json,
    )


def _columns(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [
            str(row[1]) for row in connection.execute("PRAGMA table_info(search_log)")
        ]
    finally:
        connection.close()


def _old_database(path: Path, rows: int = 3) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(PRE_CHAIN_SCHEMA)
        for index in range(rows):
            connection.execute(
                PRE_CHAIN_INSERT,
                (
                    _TS + index,
                    "iso",
                    "exa",
                    0,
                    "l",
                    "q",
                    1,
                    1.0,
                    "success",
                    None,
                    None,
                    None,
                    f"r{index}",
                    1,
                    None,
                    None,
                    None,
                    0,
                    0,
                    None,
                    None,
                    1,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def test_the_insert_list_and_its_placeholders_come_from_one_tuple() -> None:
    assert _INSERT_SQL.count("?") == len(_INSERT_COLUMNS)
    assert tuple(_INSERT_COLUMNS[-4:]) == CHAIN_COLUMNS
    assert tuple(name for name, _type in _CHAIN_ATTEMPT_COLUMNS) == CHAIN_COLUMNS
    row = _row_tuple(_outcome(), capture_content=True, max_content_chars=2048)
    assert len(row) == len(_INSERT_COLUMNS)


def test_a_fresh_database_has_the_columns(tmp_path: Path) -> None:
    store = WebSearchLogStore(tmp_path / "websearch.db")
    store.close()
    assert _columns(tmp_path / "websearch.db")[-4:] == list(CHAIN_COLUMNS)


def test_an_existing_database_gains_them_once_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "websearch.db"
    _old_database(path)
    for _ in range(2):  # idempotent: the second open changes nothing
        connection = sqlite3.connect(path)
        try:
            _initialize_schema(connection)
        finally:
            connection.close()
        assert _columns(path)[-4:] == list(CHAIN_COLUMNS)
        assert _columns(path).count("tries_json") == 1
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT route_id, chain_position, outcome, failure_kind, tries_json"
            " FROM search_log ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [(f"r{index}", None, None, None, None) for index in range(3)]


@pytest.mark.parametrize("done", [1, 2, 3])
def test_a_kill_between_two_alters_is_completed_by_the_next_open(
    tmp_path: Path, done: int
) -> None:
    path = tmp_path / "websearch.db"
    _old_database(path)
    connection = sqlite3.connect(path)
    try:
        for name, column_type in _CHAIN_ATTEMPT_COLUMNS[:done]:
            connection.execute(
                f"ALTER TABLE search_log ADD COLUMN {name} {column_type}"
            )
        connection.commit()
    finally:
        connection.close()

    store = WebSearchLogStore(path)
    store.record(_outcome(chain_position=2, outcome="failed", failure_kind="upstream"))
    store.close()

    assert _columns(path)[-4:] == list(CHAIN_COLUMNS)


def test_an_old_version_still_writes_and_reads_the_migrated_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "websearch.db"
    _old_database(path, rows=1)
    WebSearchLogStore(path).close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            PRE_CHAIN_INSERT,
            (
                _TS,
                "iso",
                "tavily",
                0,
                "l",
                "q",
                2,
                1.0,
                "success",
                None,
                None,
                None,
                "old-writer",
                1,
                None,
                None,
                None,
                0,
                0,
                None,
                None,
                1,
            ),
        )
        connection.commit()
        old_read = connection.execute(
            "SELECT id, ts_epoch, ts_iso, provider, key_index, key_label, query,"
            " results_count, duration_ms, status, error_kind, error_message,"
            " cost_usd, route_id, attempt_number, input_chars, output_chars,"
            " input_sha256, output_sha256, content_captured FROM search_log"
            " WHERE route_id = 'old-writer'"
        ).fetchone()
        new_columns = connection.execute(
            "SELECT chain_position, outcome, failure_kind, tries_json FROM search_log"
            " WHERE route_id = 'old-writer'"
        ).fetchone()
    finally:
        connection.close()
    assert old_read[3] == "tavily"
    assert new_columns == (None, None, None, None)


def test_a_row_without_a_chain_stores_null_never_zero(tmp_path: Path) -> None:
    store = WebSearchLogStore(tmp_path / "websearch.db")
    store.record(_outcome())
    store.close()
    connection = sqlite3.connect(tmp_path / "websearch.db")
    try:
        stored = connection.execute(
            "SELECT chain_position, outcome, failure_kind, tries_json,"
            " typeof(chain_position) FROM search_log"
        ).fetchone()
    finally:
        connection.close()
    assert stored == (None, None, None, None, "null")


def test_a_chain_row_round_trips(tmp_path: Path) -> None:
    tries = [{"key_index": 0, "status": 401, "charged": "auth"}]
    store = WebSearchLogStore(tmp_path / "websearch.db")
    store.record(
        _outcome(
            chain_position=0,
            outcome="failed",
            failure_kind="authentication",
            tries_json=json.dumps(tries),
            status="error",
        )
    )
    store.close()
    connection = sqlite3.connect(tmp_path / "websearch.db")
    try:
        stored = connection.execute(
            "SELECT chain_position, outcome, failure_kind, tries_json, status"
            " FROM search_log"
        ).fetchone()
    finally:
        connection.close()
    assert stored[:3] == (0, "failed", "authentication")
    assert json.loads(stored[3]) == tries
    assert stored[4] == "error"


def test_skipped_rows_are_listed_but_never_counted(tmp_path: Path) -> None:
    store = WebSearchLogStore(tmp_path / "websearch.db")
    store.record(_outcome())
    store.record(_outcome(status="error"))
    store.record(_outcome(status=SKIPPED_STATUS, outcome="skipped", chain_position=0))
    store.flush()
    try:
        stats = store.stats("daily")
        listed = store.requests(limit=10)
    finally:
        store.close()
    totals = stats["attempts"]["totals"]
    assert (totals["requests"], totals["successes"], totals["errors"]) == (2, 1, 1)
    assert sum(entry["requests"] for entry in stats["attempts"]["series"]) == 2
    assert listed["total"] == 3
    statuses = sorted(row["status"] for row in listed["items"])
    assert statuses == ["error", "skipped", "success"]


def test_the_skipped_status_has_one_spelling() -> None:
    assert search_chain.SKIPPED_STATUS == analytics.SKIPPED_STATUS
