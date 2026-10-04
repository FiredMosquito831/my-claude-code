"""``idx_request_attempts_model_v1`` is dropped, guarded and idempotent (7.74.0).

284 MB on a real 12 GB log, and chosen by none of the 136 statements the admin
read paths issue: ``idx_request_attempts_ts_v1`` covers every query it could
serve. It is no longer created, the writer drops it in the background, and an
older version that recreates it has it dropped again on the next start. No
query plan may change for it.
"""

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_TS = 1_790_000_000.0
_INDEX = "idx_request_attempts_model_v1"
_OLD_CREATE = (
    "CREATE INDEX IF NOT EXISTS idx_request_attempts_model_v1"
    " ON request_attempts(model_ref, outcome, reasoning_emitted, request_id)"
)


def _record(index: int) -> RequestRecord:
    return RequestRecord(
        id=f"i{index:05d}",
        ts_epoch=_TS + index * 60,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model=f"model-{index % 4}",
        stream=True,
        input_text=f"prompt {index}",
        output_text=f"reply {index}",
        thinking_text="thought" if index % 2 else None,
        tokens_in=10,
        tokens_out=20,
        duration_ms=120.0 + index,
        status="success" if index % 5 else "error",
        attempts=(
            RouteAttempt(
                attempt=0,
                provider="nvidia_nim",
                model_ref=f"nvidia_nim/model-{index % 4}",
                outcome=RouteAttemptOutcome.SUCCEEDED
                if index % 5
                else RouteAttemptOutcome.FAILED,
                duration_ms=100.0 + index,
                ttft_ms=40.0 + index,
                reasoning_emitted=bool(index % 2),
                wire_body='{"model": "m"}',
            ),
            RouteAttempt(
                attempt=1,
                provider="nvidia_nim",
                model_ref="nvidia_nim/other",
                outcome=RouteAttemptOutcome.SKIPPED,
            ),
        ),
    )


def _indexes(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }


def _wait_dropped(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while _INDEX in _indexes(path):
        assert time.monotonic() < deadline, "the unused index was never dropped"
        time.sleep(0.05)


def test_a_new_log_never_creates_it_and_keeps_the_index_that_serves(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    store.close()
    names = _indexes(path)
    assert _INDEX not in names
    assert "idx_request_attempts_ts_v1" in names


def test_an_older_versions_index_is_dropped_again_and_again_by_the_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    for index in range(40):
        store.enqueue(_record(index))
    store.close()
    for _round in range(2):
        # What 7.73.0 and older do at start.
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(_OLD_CREATE)
        assert _INDEX in _indexes(path)
        store = RequestLogStore(path, max_rows=0)
        _wait_dropped(path)
        store.close()
        assert _INDEX not in _indexes(path)
    # Idempotent: with nothing to drop, a start does nothing and raises nothing.
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    conn = store._connect()
    try:
        store._drop_unused_attempt_index(conn)
        store._drop_unused_attempt_index(conn)
    finally:
        conn.close()
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _captured_plans(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """EXPLAIN QUERY PLAN of every SELECT the admin attempt readers issue."""
    statements: list[str] = []
    real_connect = RequestLogStore._connect

    def traced(self: RequestLogStore) -> sqlite3.Connection:
        conn = real_connect(self)
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(RequestLogStore, "_connect", traced)
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        # The trainer's sampling reads are not admin reads, and their window
        # moves with the clock.
        patch.setattr(RequestLogStore, "_maybe_refresh_dictionaries", lambda self: None)
        store = RequestLogStore(path, max_rows=0)
        assert store._dictionaries_checked.wait(30)
        statements.clear()
        for since in (None, _TS + 600):
            store.reasoning_by_model(since=since)
            store.latency_by_model(since=since)
            list(store.iter_export_attempt_rows(since=since))
            store.list_requests_page(limit=50)
            store.count_requests(since=since)
            store.stats(since=since)
        store.get_request("i00007")
        store.close()
    plans: dict[str, str] = {}
    with closing(sqlite3.connect(path)) as conn:
        for sql in dict.fromkeys(statements):
            head = sql.lstrip().upper()
            if not head.startswith(("SELECT", "WITH")) or "?" in sql:
                continue
            try:
                rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            except sqlite3.Error:
                continue
            plans[sql] = "\n".join(str(row[-1]) for row in rows)
    return plans


def test_no_attempt_query_plan_changes_when_it_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    for index in range(200):
        store.enqueue(_record(index))
    store.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(_OLD_CREATE)
    with_index = _captured_plans(path, monkeypatch)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f"DROP INDEX {_INDEX}")
    without = _captured_plans(path, monkeypatch)

    attempts = [sql for sql in with_index if "request_attempts" in sql]
    assert attempts, "no attempt statement was captured"
    assert not any(_INDEX in plan for plan in with_index.values())
    assert with_index == without


def test_the_drop_waits_for_the_read_ahead_and_the_conversion_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold, the drop measured 23.0 s on the full-size copy and 1.7 s warm; the
    writer cannot take a request while it runs, so it waits for a reader
    thread to have read the index. Nothing else waits for it."""
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    for index in range(60):
        store.enqueue(_record(index))
    store.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(_OLD_CREATE)
    started: list[str] = []
    monkeypatch.setattr(
        RequestLogStore,
        "_warm_unused_index",
        lambda self: started.append(threading.current_thread().name),
    )
    store = RequestLogStore(path, max_rows=0)
    deadline = time.monotonic() + 60
    while True:
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute(
                "SELECT value FROM request_log_meta WHERE key = ?",
                (request_log_module._HISTORY_CONVERSION_KEY,),
            ).fetchone()
        if row is not None and json.loads(row[0]).get("done_at") is not None:
            break
        assert time.monotonic() < deadline, "the conversion waited for the index"
        time.sleep(0.05)
    # Never read ahead, so never dropped -- on the writer thread or anywhere.
    assert _INDEX in _indexes(path)
    assert started == ["mcc-request-log-index-reader"]
    store._index_warm.set()
    _wait_dropped(path)
    store.close()


def test_the_drop_counts_its_pages_as_the_conversions_own(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    for index in range(300):
        store.enqueue(_record(index))
    store.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(_OLD_CREATE)
    store = RequestLogStore(path, max_rows=0)
    _wait_dropped(path)
    deadline = time.monotonic() + 60
    while True:
        with closing(sqlite3.connect(path)) as conn:
            state = json.loads(
                conn.execute(
                    "SELECT value FROM request_log_meta WHERE key = ?",
                    (request_log_module._HISTORY_CONVERSION_KEY,),
                ).fetchone()[0]
            )
        if state.get("done_at") is not None or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    store.close()
    assert state["freed_pages"] >= 1
