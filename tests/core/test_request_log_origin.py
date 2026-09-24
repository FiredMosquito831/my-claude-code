"""The request log's origin columns (7.42.0) and the explicit folder backfill.

Five nullable columns added by guarded ``ALTER TABLE``; none of them is a
rollup dimension or a rollup counter; every existing view answers exactly what
it answered before, whatever the new columns hold; and the history backfill
fills only a NULL folder, only from a stored prompt, only when asked.

Every path in this file is fake.
"""

import json
import sqlite3
import time
from typing import Any

import pytest

from my_claude_code.core.export import (
    REQUEST_FIELD_IDS,
    request_detail_columns,
    request_detail_headers,
)
from my_claude_code.core.request_log import (
    _ADDED_COLUMNS,
    _LIST_METADATA_COLUMNS,
    _REQUEST_INSERT_COLUMNS,
    _ROLLUP_DIMENSIONS,
    RequestLogStore,
    RequestRecord,
)

ORIGIN_COLUMNS = (
    "session_id",
    "agent_id",
    "parent_session_id",
    "project_dir",
    "origin_source",
)
SESSION = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
AGENT = "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
FOLDER = "C:\\Users\\devuser\\Projects\\demo"
ENV_PROMPT = (
    "You are Claude Code.\n# Environment\n"
    f" - Primary working directory: {FOLDER}\n - Platform: win32\n\nhello"
)
BASE_TS = 1_789_000_000.0


def _record(request_id: str, offset: int = 0, **overrides: Any) -> RequestRecord:
    values: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "ts_epoch": BASE_TS + offset,
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "harness": "claude",
        "stream": True,
        "tokens_in": 10 + offset,
        "tokens_out": 20,
        "duration_ms": 120.0 + offset,
        "ttft_ms": 40.0,
        "status": "success" if offset % 3 else "error",
        "input_text": "hi",
    }
    values.update(overrides)
    return RequestRecord(**values)


def _with_origin(record: RequestRecord) -> RequestRecord:
    record.session_id = SESSION
    record.agent_id = AGENT
    record.parent_session_id = SESSION
    record.project_dir = FOLDER
    record.origin_source = (
        "session_id=header.x-claude-code-session-id;"
        "agent_id=header.x-claude-code-agent-id;"
        "parent_session_id=header.x-claude-code-agent-id;"
        "project_dir=prompt.env-block"
    )
    return record


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    yield store
    store.close()


def _rows(path, sql: str, *args: Any) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


class TestTheColumns:
    def test_each_is_declared_once_and_written_by_the_insert(self) -> None:
        names = [name for name, _sql in _ADDED_COLUMNS]
        for column in ORIGIN_COLUMNS:
            assert names.count(column) == 1
            assert column in _REQUEST_INSERT_COLUMNS
            assert column in _LIST_METADATA_COLUMNS

    def test_request_log_origin_columns_guarded_alter(self, tmp_path) -> None:
        """Opened twice on a file that lacked them: added once, never twice."""

        path = tmp_path / "legacy.db"
        old = RequestLogStore(path, max_rows=100)
        old.enqueue(_record("old"))
        old.close()
        connection = sqlite3.connect(path)
        try:
            # A file from before 7.42.0 had neither the columns nor the 7.43.0
            # index over two of them, and SQLite refuses to drop a column an
            # index still names.
            connection.execute("DROP INDEX IF EXISTS idx_requests_origin_v1")
            for column in ORIGIN_COLUMNS:
                connection.execute(f"ALTER TABLE requests DROP COLUMN {column}")
            connection.commit()
        finally:
            connection.close()

        first = RequestLogStore(path, max_rows=100)
        first.enqueue(_with_origin(_record("new", 1)))
        first.close()
        second = RequestLogStore(path, max_rows=100)
        second.close()

        columns = [row[1] for row in _rows(path, "PRAGMA table_info(requests)")]
        for column in ORIGIN_COLUMNS:
            assert columns.count(column) == 1
        legacy = second.get_request("old")
        fresh = second.get_request("new")
        assert legacy is not None and fresh is not None
        assert all(legacy[column] is None for column in ORIGIN_COLUMNS)
        assert fresh["session_id"] == SESSION
        assert fresh["project_dir"] == FOLDER

    def test_request_log_rollup_columns_unchanged(self, store) -> None:
        """The origin is unbounded (a new session per conversation), so it is
        never a rollup dimension: that would multiply the rollup by the number
        of sessions per hour. The dimension list is pinned exactly."""

        assert _ROLLUP_DIMENSIONS == (
            "hour_epoch",
            "is_local",
            "provider",
            "resolved_model",
            "requested_model",
            "status",
            "endpoint",
            "key_label",
            "optimization",
            "harness",
        )
        for column in ORIGIN_COLUMNS:
            assert column not in _ROLLUP_DIMENSIONS
        rollup_columns = {
            row[1]
            for row in _rows(store.db_path, "PRAGMA table_info(request_stats_rollup)")
        }
        assert not (rollup_columns & set(ORIGIN_COLUMNS))


class TestReadBack:
    def test_the_list_and_the_detail_carry_values_and_display_forms(
        self, store
    ) -> None:
        store.enqueue(_with_origin(_record("r1", 1)))
        store.enqueue(_record("r2", 2))
        store.close()

        rows, total, _more = store.list_requests_page(limit=10)
        by_id = {row["id"]: row for row in rows}
        assert total == 2
        row = by_id["r1"]
        assert row["session_id"] == SESSION
        assert row["session_short"] == "0f3c2a1b"
        assert row["agent_id"] == AGENT
        assert row["project_dir"] == FOLDER
        assert row["project_short"].startswith("Projects\\demo · #")
        assert row["origin_provenance"]["project_dir"]["sentence"] == (
            "read from the prompt's environment block"
        )
        bare = by_id["r2"]
        assert bare["session_id"] is None
        assert bare["session_short"] is None
        assert bare["project_short"] is None
        assert bare["origin_provenance"] == {}

        detail = store.get_request("r1")
        assert detail is not None
        assert detail["origin_provenance"]["session_id"]["sentence"] == (
            "stated by the x-claude-code-session-id header"
        )


def _views(store: RequestLogStore) -> dict[str, Any]:
    """Every aggregate view the equality contracts cover, as JSON text."""

    _rows_page, total, _ = store.list_requests_page(limit=100)
    export = list(
        store.iter_export_rows(
            columns=request_detail_columns(REQUEST_FIELD_IDS[:-1]), need_bodies=False
        )
    )
    return {
        "stats": json.dumps(store.stats(), sort_keys=True, default=str),
        "pulse": json.dumps(store.pulse(), sort_keys=True, default=str),
        "latency": json.dumps(store.latency_by_model(), sort_keys=True, default=str),
        "cost": json.dumps(store.cost_breakdown(), sort_keys=True, default=str),
        "harness": json.dumps(
            store.harness_usage(since=0.0), sort_keys=True, default=str
        ),
        "total": total,
        "export": json.dumps(export, sort_keys=True, default=str),
    }


def test_every_existing_view_ignores_the_origin_columns(tmp_path) -> None:
    """Same requests, one log with origin values and one without: every stats,
    pulse, latency, cost, harness and default-export answer is byte-identical.
    The origin columns are additions, never inputs to a figure."""

    plain = RequestLogStore(tmp_path / "plain.db", max_rows=10_000)
    tagged = RequestLogStore(tmp_path / "tagged.db", max_rows=10_000)
    for index in range(30):
        plain.enqueue(_record(f"r{index}", index))
        tagged.enqueue(_with_origin(_record(f"r{index}", index)))
    plain.close()
    tagged.close()

    assert _views(plain) == _views(tagged)


class TestTheExportGroup:
    def test_origin_is_opt_in_and_absent_from_every_other_selection(self) -> None:
        everything_else = [field for field in REQUEST_FIELD_IDS if field != "origin"]

        assert "origin" in REQUEST_FIELD_IDS
        chosen = request_detail_columns(everything_else)
        assert not (set(chosen) & set(ORIGIN_COLUMNS))
        with_origin = request_detail_columns(["origin"])
        assert [column for column in with_origin if column in ORIGIN_COLUMNS] == list(
            ORIGIN_COLUMNS
        )
        assert request_detail_headers(ORIGIN_COLUMNS) == [
            "Session",
            "Subagent",
            "Parent session",
            "Folder",
            "Origin source",
        ]

    def test_an_origin_export_carries_the_raw_values(self, store) -> None:
        store.enqueue(_with_origin(_record("r1", 1)))
        store.close()

        (row,) = list(
            store.iter_export_rows(
                columns=request_detail_columns(["origin"]), need_bodies=False
            )
        )
        assert row["session_id"] == SESSION
        assert row["project_dir"] == FOLDER
        assert row["origin_source"].startswith("session_id=header.")


def _wait_for_backfill(store: RequestLogStore, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = store.origin_backfill_status()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("the folder backfill did not finish")


class TestTheFolderBackfill:
    @pytest.mark.parametrize("compress", [True, False], ids=["compressed", "inline"])
    def test_it_fills_only_null_folders_of_prompt_harnesses(
        self, tmp_path, compress
    ) -> None:
        path = tmp_path / "requests.db"
        seed = RequestLogStore(path, max_rows=10_000, compress_bodies=compress)
        seed.enqueue(_record("claude-old", 1, input_text=ENV_PROMPT))
        seed.enqueue(
            _record(
                "claude-session",
                2,
                input_text=ENV_PROMPT,
                session_id=SESSION,
                origin_source="session_id=header.x-claude-code-session-id",
            )
        )
        seed.enqueue(
            _record("sdk", 3, harness="claude_agent_sdk", input_text=ENV_PROMPT)
        )
        seed.enqueue(
            _record(
                "claude-known",
                4,
                input_text=ENV_PROMPT,
                project_dir="D:\\already\\known",
                origin_source="project_dir=prompt.env-block",
            )
        )
        seed.enqueue(_record("claude-noenv", 5, input_text="no environment here"))
        seed.close()

        store = RequestLogStore(path, max_rows=10_000, compress_bodies=compress)
        try:
            assert store.origin_backfill_status()["running"] is False
            started = store.request_origin_backfill()
            assert started["running"] is True
            status = _wait_for_backfill(store)
        finally:
            store.close()

        assert status["error"] is None
        assert status["filled"] == 2
        assert status["scanned"] == 3
        assert status["completed_at"] is not None
        folders = dict(_rows(path, "SELECT id, project_dir FROM requests"))
        assert folders == {
            "claude-old": FOLDER,
            "claude-session": FOLDER,
            "sdk": None,
            "claude-known": "D:\\already\\known",
            "claude-noenv": None,
        }
        sources = dict(_rows(path, "SELECT id, origin_source FROM requests"))
        assert sources["claude-old"] == "project_dir=prompt.stored-prompt"
        assert sources["claude-session"] == (
            "session_id=header.x-claude-code-session-id;"
            "project_dir=prompt.stored-prompt"
        )
        assert sources["claude-known"] == "project_dir=prompt.env-block"
        # The session is never invented by the backfill.
        sessions = dict(_rows(path, "SELECT id, session_id FROM requests"))
        assert sessions["claude-old"] is None

    def test_it_never_runs_unless_asked(self, tmp_path) -> None:
        path = tmp_path / "requests.db"
        store = RequestLogStore(path, max_rows=10_000)
        store.enqueue(_record("claude-old", 1, input_text=ENV_PROMPT))
        time.sleep(1.0)
        store.close()

        assert _rows(path, "SELECT project_dir FROM requests") == [(None,)]
        assert _rows(
            path,
            "SELECT COUNT(*) FROM request_log_meta WHERE key LIKE 'origin_%'",
        ) == [(0,)]

    def test_a_second_press_resumes_from_the_cursor(self, tmp_path) -> None:
        path = tmp_path / "requests.db"
        store = RequestLogStore(path, max_rows=10_000)
        store.enqueue(_record("first", 1, input_text=ENV_PROMPT))
        store.close()
        store = RequestLogStore(path, max_rows=10_000)
        try:
            store.request_origin_backfill()
            first = _wait_for_backfill(store)
            store.enqueue(_record("second", 2, input_text=ENV_PROMPT))
            time.sleep(0.6)
            store.request_origin_backfill()
            second = _wait_for_backfill(store)
        finally:
            store.close()

        assert (first["scanned"], first["filled"]) == (1, 1)
        # Only the row added after the first walk is read again.
        assert (second["scanned"], second["filled"]) == (1, 1)
        assert second["through_rowid"] is not None
