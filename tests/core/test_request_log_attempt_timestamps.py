"""Each route attempt carries the instant its request happened.

``reasoning_by_model`` asks a question about a window of time, and the time
lived only on ``requests`` -- so the plan walked every attempt row through a
covering index and did one rowid lookup into ``requests`` per attempt just to
find out when it happened. These hold the copy, the backfill that fills it in
for history, and the rule that makes the change safe: until every attempt
carries a timestamp the query keeps asking the parent, because a time filter
against NULL would silently drop exactly the history the question is about.
"""

import sqlite3
import time
from typing import Any

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_MARKER = request_log_module._ATTEMPTS_TS_BACKFILL_KEY
_THROUGH = request_log_module._ATTEMPTS_TS_BACKFILL_THROUGH_KEY

_EARLY = 1785542400.0  # 2026-08-01 00:00 UTC
_LATE = 1788480000.0  # 2026-09-04 00:00 UTC


def _record(request_id: str, ts_epoch: float, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "harness": "claude",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 120.0,
        "status": "success",
        "ts_epoch": ts_epoch,
        "thinking_chars": 40,
        "attempts": (
            RouteAttempt(
                attempt=1,
                provider="nvidia_nim",
                model_ref="nvidia_nim/test-model",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                reasoning_emitted=True,
            ),
        ),
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _seed(path, records: list[RequestRecord]) -> None:
    store = RequestLogStore(path, max_rows=10_000)
    for record in records:
        store.enqueue(record)
    store.close()


def _meta(path, key: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (key,)
        ).fetchone()
    return None if row is None else str(row[0])


def _await_backfill(path, *, timeout: float = 60.0) -> bool:
    store = RequestLogStore(path, max_rows=10_000)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _meta(path, _MARKER) is not None:
                return True
            time.sleep(0.05)
        return False
    finally:
        store.close()


def _stamps(path) -> dict[str, Any]:
    with sqlite3.connect(path) as conn:
        return {
            str(row[0]): row[1]
            for row in conn.execute("SELECT request_id, ts_epoch FROM request_attempts")
        }


def test_a_new_attempt_carries_its_request_s_instant(tmp_path) -> None:
    """The parent's instant, copied -- not a second reading of the clock."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("a", _EARLY), _record("b", _LATE)])

    assert _stamps(path) == {"a": _EARLY, "b": _LATE}


def test_the_backfill_dates_the_attempts_that_predate_the_column(tmp_path) -> None:
    """History gets the same copy the live path writes."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("a", _EARLY), _record("b", _LATE)])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE request_attempts SET ts_epoch = NULL")
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)", (_MARKER, _THROUGH)
        )

    assert _await_backfill(path)

    assert _stamps(path) == {"a": _EARLY, "b": _LATE}


def test_an_attempt_whose_request_is_gone_stays_undated(tmp_path) -> None:
    """Nothing to copy is not a failure, and the walk must not retry forever."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("a", _EARLY), _record("orphan", _LATE)])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE request_attempts SET ts_epoch = NULL")
        conn.execute("DELETE FROM requests WHERE id = 'orphan'")
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)", (_MARKER, _THROUGH)
        )

    assert _await_backfill(path)

    assert _stamps(path) == {"a": _EARLY, "orphan": None}


def test_the_backfill_resumes_from_its_cursor(tmp_path) -> None:
    """The cursor is the progress here, because "still NULL" is not."""
    path = tmp_path / "requests.db"
    _seed(path, [_record(f"r{index}", _EARLY + index) for index in range(6)])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE request_attempts SET ts_epoch = NULL")
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)", (_MARKER, _THROUGH)
        )
        # Two rows dated by hand with a value the copy could never produce, and
        # a cursor that says they are behind us: a walk that revisited them
        # would overwrite the sentinel.
        rowids = [
            int(row[0])
            for row in conn.execute("SELECT rowid FROM request_attempts ORDER BY rowid")
        ]
        conn.execute(
            "UPDATE request_attempts SET ts_epoch = 1.0 WHERE rowid <= ?",
            (rowids[1],),
        )
        conn.execute(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            (_THROUGH, str(rowids[1])),
        )

    assert _await_backfill(path)

    stamps = _stamps(path)
    assert stamps["r0"] == 1.0
    assert stamps["r1"] == 1.0
    assert [stamps[f"r{index}"] for index in range(2, 6)] == [
        _EARLY + index for index in range(2, 6)
    ]


def test_the_marker_is_versioned(tmp_path) -> None:
    """Bumping the name is how a future release redoes the copy."""
    assert _MARKER.endswith("_v1")
    assert _THROUGH.endswith("_v1")


def test_reasoning_by_model_answers_the_same_before_and_after_the_backfill(
    tmp_path,
) -> None:
    """The equality contract: the index is an optimisation, not a new answer.

    Same store, same data, the two query shapes -- the one that filters on the
    request and the one that filters on the attempt -- have to agree exactly.
    """
    path = tmp_path / "requests.db"
    _seed(
        path,
        [
            _record("old", _EARLY),
            _record("new1", _LATE + 10),
            _record("new2", _LATE + 20, thinking_chars=0),
        ],
    )
    # Before: no marker, so the query filters on the parent row.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM request_log_meta WHERE key = ?", (_MARKER,))
    store = RequestLogStore(path, max_rows=10_000)
    try:
        before = store.reasoning_by_model(since=_LATE)
    finally:
        store.close()

    assert _await_backfill(path)

    store = RequestLogStore(path, max_rows=10_000)
    try:
        after = store.reasoning_by_model(since=_LATE)
    finally:
        store.close()

    assert before == after
    assert before == [
        {
            "model_ref": "nvidia_nim/test-model",
            "attempts": 2,
            "requested": 2,
            "returned": 1,
            "unmeasured": 0,
        }
    ]


def test_an_undated_attempt_is_not_dropped_before_the_backfill_finishes(
    tmp_path,
) -> None:
    """The whole reason the query waits for the marker.

    An attempt with no timestamp of its own is still an attempt that happened,
    and it is found by the window its parent falls in -- which is what the old
    query shape asks, and what it keeps asking until every row can answer for
    itself.
    """
    path = tmp_path / "requests.db"
    _seed(path, [_record("new1", _LATE + 10)])
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE request_attempts SET ts_epoch = NULL")
        conn.execute("DELETE FROM request_log_meta WHERE key = ?", (_MARKER,))

    store = RequestLogStore(path, max_rows=10_000)
    try:
        rows = store.reasoning_by_model(since=_LATE)
    finally:
        store.close()

    assert [row["attempts"] for row in rows] == [1]


def test_the_attempt_index_exists_and_is_versioned(tmp_path) -> None:
    """Named ``_v1`` per the index rule: a column-list change means ``_v2``."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("a", _EARLY)])
    with sqlite3.connect(path) as conn:
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'request_attempts'"
            )
        }
    assert "idx_request_attempts_ts_v1" in names
    assert "idx_request_attempts_model_v1" in names
