"""Skipped route attempts stored compactly (7.76.0).

Nine attempt rows in ten record a model the chain never asked. A request's
skipped attempts that hold nothing but their attempt number, model, reason
and the request's time and key are stored as one ``request_attempt_skips`` row
naming one deduplicated ``attempt_skip_sets`` row; the background history
conversion does the same for rows an older version wrote, deleting each row
only once its compact form reads back, through the readers' own path, as
exactly that row. Every reader returns byte-identical output either way.
"""

import dataclasses
import io
import json
import sqlite3
import time
import zipfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import export as ex
from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_TS = 1_790_000_000.0
_KEY = request_log_module._HISTORY_CONVERSION_KEY
_SKIPPED = RouteAttemptOutcome.SKIPPED
_LADDER = {
    "ladder": {
        "tries": [{"status": 429, "source": "upstream"}, {"status": 200}],
        "summary": {"statuses_by_code": {"429": 1, "200": 1}},
        "root_cause": "rate limited once",
    },
    "early_retries": 1,
}

#: What a skipped attempt's reason can be, as the router writes them.
REASONS: list[tuple[str | None, str | None]] = [
    (None, "never reached"),
    ("paused", "paused by you on Model Config"),
    ("cooldown", "provider in rate-limit cooldown for 37s; a later model was tried"),
    ("route_ended", "not tried: a auth failure ends the route"),
    ("ejected", "benched: 3 failures in 60 s (ș ă — → ✓ 𝄞 中文)"),
    ("budget_exhausted", "request budget spent before this model was tried"),
    (None, ""),
    (None, None),
]


def _skip(attempt: int, index: int, **overrides: Any) -> RouteAttempt:
    # Requests repeat each other's chains, as real traffic does: the reason
    # and model rotate with the request over a few variants only.
    variant = index % 4
    kind, message = REASONS[(attempt + variant) % len(REASONS)]
    values: dict[str, Any] = {
        "attempt": attempt,
        "provider": ("zen", "nvidia_nim", None)[(attempt + variant) % 3],
        "model_ref": None if (attempt * variant) % 11 == 7 else f"zen/model-{attempt}",
        "outcome": _SKIPPED,
        "error_kind": kind,
        "error_message": message,
        "key_index": None if index % 9 == 4 else index % 3,
        "key_label": None if index % 9 == 4 else f"sk-a...{index % 3}x",
    }
    values.update(overrides)
    return RouteAttempt(**values)


def _attempts(index: int) -> tuple[RouteAttempt, ...]:
    """A chain of 0-9 attempts; most of its rows skipped, some not."""
    shape = index % 10
    key_index = None if index % 9 == 4 else index % 3
    key_label = None if index % 9 == 4 else f"sk-a...{index % 3}x"
    if shape == 0:
        return ()
    if shape == 1:
        # Only skipped attempts: the route ended before any model answered.
        return tuple(_skip(n, index) for n in range(4))
    succeeded = RouteAttempt(
        attempt=0 if shape % 2 else 2,
        provider="nvidia_nim",
        model_ref="nvidia_nim/answering",
        outcome=RouteAttemptOutcome.SUCCEEDED,
        duration_ms=1234.5 + index,
        params=dict(_LADDER) if index % 4 == 0 else None,
        wire_body=json.dumps({"model": "m", "messages": [{"chars": 40 + index}]}),
        reasoning_emitted=bool(index % 2),
        ladder_tries=2 if index % 4 == 0 else 1,
        ttft_ms=300.0 + index,
        first_reasoning_ms=50.0,
        tokens_out=400,
        proxy_label="direct" if index % 5 == 0 else None,
        key_index=key_index,
        key_label=key_label,
    )
    failed = RouteAttempt(
        attempt=1,
        provider="zen",
        model_ref="zen/failing",
        outcome=RouteAttemptOutcome.FAILED,
        error_kind="upstream",
        error_message="Upstream provider returned HTTP 502.",
        duration_ms=99.0,
        ttft_ms=None if index % 3 else 70.0,
        key_index=key_index,
        key_label=key_label,
    )
    chain = [succeeded, failed]
    chain.extend(_skip(n, index) for n in range(3, 3 + shape))
    if shape == 7:
        # A skipped attempt with a bench reason in its params stays a row.
        chain.append(
            _skip(20, index, params={"bench": {"mode": "rate", "failures": 3}})
        )
    if shape == 8:
        # A skipped attempt that carries a wire snapshot stays a row.
        chain.append(_skip(21, index, wire_body='{"model":"m"}'))
    return tuple(sorted(chain, key=lambda attempt: attempt.attempt))


def _record(index: int, *, prefix: str = "s") -> RequestRecord:
    return RequestRecord(
        id=f"{prefix}{index:05d}",
        ts_epoch=_TS + index * 7.25,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="answering",
        stream=bool(index % 2),
        input_text=f"Prompt {index}: why does test_{index} fail?",
        output_text=f"Reply {index}.",
        tokens_in=10 + index,
        tokens_out=20,
        duration_ms=120.0 + index,
        status="error" if index % 10 == 1 else "success",
        error_kind="upstream" if index % 10 == 1 else None,
        error_message="all models failed" if index % 10 == 1 else None,
        key_index=None if index % 9 == 4 else index % 3,
        key_label=None if index % 9 == 4 else f"sk-a...{index % 3}x",
        route_attempt=1 if index % 3 == 0 else 0,
        route_primary_model="zen/model-0",
        attempts=_attempts(index),
    )


def _quiet(patch: pytest.MonkeyPatch) -> None:
    patch.setattr(RequestLogStore, "_run_history_conversion", lambda self, conn: None)
    patch.setattr(RequestLogStore, "_maybe_refresh_dictionaries", lambda self: None)


def _as_rows(patch: pytest.MonkeyPatch) -> None:
    """Write attempts the way 7.75.0 and every earlier version did."""
    patch.setattr(
        RequestLogStore,
        "_store_skip_packs",
        lambda self, conn, rows, rewritten: rows,
    )


def _write(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[RequestRecord],
    *,
    as_rows: bool = False,
    compress_bodies: bool = True,
) -> list[str]:
    with monkeypatch.context() as patch:
        _quiet(patch)
        if as_rows:
            _as_rows(patch)
        store = RequestLogStore(path, max_rows=0, compress_bodies=compress_bodies)
        for record in records:
            store.enqueue(record)
        store.close()
    return [record.id for record in records]


def _attempt_export(store: RequestLogStore) -> dict[str, bytes]:
    """The attempt export in every format, every field, as the route renders it."""
    selected = ex.validate_fields(ex.ATTEMPT_SCOPE, list(ex.ATTEMPT_FIELD_IDS))
    columns = ex.attempt_output_columns(selected)
    headers = ex.attempt_detail_headers(columns)

    def rows() -> Iterator[dict[str, Any]]:
        iterator = store.iter_export_attempt_rows(page_size=7)
        try:
            for row in iterator:
                ex.compute_attempt_detail_derived(row, selected, {})
                yield {column: row.get(column) for column in columns}
        finally:
            iterator.close()

    out = {
        "json": b"".join(ex.render_json_array(rows())),
        "csv": b"".join(ex.render_csv(rows(), columns, headers)),
        "txt": b"".join(
            ex.render_txt(rows(), columns, headers, title="t", summary="s")
        ),
    }
    with zipfile.ZipFile(
        io.BytesIO(b"".join(ex.render_xlsx(rows(), columns, headers)))
    ) as archive:
        out["xlsx"] = b"".join(
            name.encode() + b"\0" + archive.read(name)
            for name in sorted(archive.namelist())
            if name != "docProps/core.xml"
        )
    return out


def _request_export(store: RequestLogStore) -> bytes:
    """The request export with the ladder columns, which read attempts."""
    selected = ex.validate_fields(ex.REQUEST_SCOPE, list(ex.REQUEST_FIELD_IDS))
    sql_columns = ex.request_detail_columns(selected)
    output_columns = sql_columns + ex.request_detail_derived_columns(selected)
    iterator = store.iter_export_rows(
        columns=sql_columns,
        need_bodies=ex.requires_request_bodies(selected),
        need_ladder="ladder" in selected,
    )
    rows: list[dict[str, Any]] = []
    try:
        for row in iterator:
            ex.compute_request_detail_derived(row, selected, {})
            rows.append({column: row.get(column) for column in output_columns})
    finally:
        iterator.close()
    return b"".join(ex.render_json_array(iter(rows)))


def _reads(path: Path, ids: list[str]) -> dict[str, Any]:
    """Everything a reader can see of the attempts, and the analytics on them."""
    with pytest.MonkeyPatch.context() as patch:
        _quiet(patch)
        store = RequestLogStore(path, max_rows=0)
        try:
            details = {request_id: store.get_request(request_id) for request_id in ids}
            out = {
                "details": details,
                "attempt_export": _attempt_export(store),
                "attempt_export_filtered": [
                    list(store.iter_export_attempt_rows(status="error")),
                    list(
                        store.iter_export_attempt_rows(since=_TS + 500, until=_TS + 900)
                    ),
                ],
                "request_export": _request_export(store),
                "stats": store.stats(),
                "stats_rows": store._stats_from_rows(),
                "stats_rows_window": store._stats_from_rows(since=_TS + 100),
                "latency": store.latency_by_model(),
                "latency_window": store.latency_by_model(since=_TS + 300),
                "reasoning": store.reasoning_by_model(),
                "reasoning_window": store.reasoning_by_model(since=_TS + 300),
                "list": store.list_requests_page(limit=500),
                "lifetime": store.lifetime(),
            }
        finally:
            store.close()
    return out


def _table(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return [tuple(row) for row in conn.execute(sql)]


def _skipped_rows(path: Path) -> int:
    return _table(
        path, "SELECT COUNT(*) FROM request_attempts WHERE outcome = 'skipped'"
    )[0][0]


def _compact_members(path: Path) -> int:
    return _table(
        path,
        "SELECT COALESCE(SUM(json_array_length(s.attempts)), 0)"
        " FROM request_attempt_skips AS k JOIN attempt_skip_sets AS s"
        " ON s.id = k.set_id",
    )[0][0]


def _state(path: Path) -> dict[str, Any] | None:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (_KEY,)
        ).fetchone()
    return None if row is None else json.loads(row[0])


def _drive(store: RequestLogStore, limit: int = 100_000) -> int:
    """Run steps on this thread until there is nothing left; the step count."""
    assert store._dictionaries_checked.wait(30)
    conn = store._connect()
    try:
        steps = 0
        while store._history_step(conn):
            steps += 1
            assert steps < limit
        return steps
    finally:
        conn.close()


def _wait_done(path: Path, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(path)
        if (
            state is not None
            and state.get("done_at") is not None
            and (state.get("skipped") or {}).get("done_at") is not None
        ):
            return state
        time.sleep(0.05)
    raise AssertionError(f"the conversion never finished: {_state(path)}")


def _finished_7750_document(path: Path, *, returned: int = 0) -> None:
    """The document 7.75.0 leaves once it is done: no ``skipped`` part."""
    phase = {
        "through": 10,
        "end": 10,
        "converted": 3,
        "kept": 7,
        "failed": 0,
        "bytes_before": 900,
        "bytes_after": 90,
        "done_at": _TS + 50,
    }
    document = {
        "started_at": _TS,
        "page_size": 4096,
        "bytes_at_start": 4096 * 400,
        "freelist_at_start": 0,
        "wire": dict(phase),
        "bodies": dict(phase),
        "metadata": dict(phase),
        "freed_pages": returned,
        "returned_pages": returned,
        "done_at": _TS + 60,
        "bytes_at_end": 4096 * 300,
        "reopened_at": _TS + 55,
        "reopened_for": ["metadata"],
        "returned_at_reopen": returned,
    }
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            (_KEY, json.dumps(document, sort_keys=True)),
        )


def _copy(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source)) as a, closing(sqlite3.connect(target)) as b:
        a.backup(b)


# ---------------------------------------------------------------- new rows


def test_new_skipped_attempts_are_stored_compactly_and_read_back_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_path = tmp_path / "rows.db"
    compact_path = tmp_path / "compact.db"
    records = [_record(index) for index in range(90)]
    ids = _write(rows_path, monkeypatch, records, as_rows=True)
    _write(compact_path, monkeypatch, records)

    skipped_total = _skipped_rows(rows_path)
    assert skipped_total > 300
    # Only the skipped attempts that hold more than the compact form are rows.
    holding_more = _table(
        compact_path,
        "SELECT COUNT(*) FROM request_attempts WHERE outcome = 'skipped'"
        " AND (params IS NOT NULL OR wire_body IS NOT NULL)",
    )[0][0]
    assert _skipped_rows(compact_path) == holding_more == 18
    assert _compact_members(compact_path) == skipped_total - holding_more
    # Every other attempt row is stored exactly as before.
    other = (
        "SELECT request_id, attempt, typeof(wire_body), CAST(wire_body AS BLOB),"
        " provider, model_ref, outcome, error_kind, error_message, duration_ms,"
        " params, reasoning_emitted, key_index, key_label, ladder_tries,"
        " tokens_in, tokens_out, cost_usd, cost_source, ts_epoch, ttft_ms,"
        " first_reasoning_ms, proxy_label FROM request_attempts"
        " WHERE outcome != 'skipped' OR params IS NOT NULL OR wire_body IS NOT NULL"
        " ORDER BY request_id, attempt"
    )
    assert _table(compact_path, other) == _table(rows_path, other)
    # A set is stored once, however many requests repeat it.
    sets = _table(compact_path, "SELECT attempts FROM attempt_skip_sets")
    assert len(sets) == len(set(sets))
    assert (
        len(sets)
        < _table(compact_path, "SELECT COUNT(*) FROM request_attempt_skips")[0][0]
    )

    assert _reads(compact_path, ids) == _reads(rows_path, ids)


def test_with_compression_off_skipped_attempts_stay_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = tmp_path / "off.db"
    rows_path = tmp_path / "rows.db"
    records = [_record(index) for index in range(30)]
    ids = _write(off, monkeypatch, records, compress_bodies=False)
    _write(rows_path, monkeypatch, records, as_rows=True, compress_bodies=False)
    assert _table(off, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]
    assert _table(off, "SELECT COUNT(*) FROM attempt_skip_sets") == [(0,)]
    assert _skipped_rows(off) == _skipped_rows(rows_path)
    assert _reads(off, ids)["details"] == _reads(rows_path, ids)["details"]


def test_skipped_attempts_whose_key_or_number_differ_stay_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    rows_path = tmp_path / "rows.db"
    base = _record(3)
    mixed_keys = dataclasses.replace(
        base,
        id="mixed",
        attempts=(
            _skip(0, 1, key_index=0, key_label="sk-a...0x"),
            _skip(1, 1, key_index=1, key_label="sk-b...1x"),
        ),
    )
    same_number = dataclasses.replace(
        base, id="twice", attempts=(_skip(0, 1), _skip(0, 2))
    )
    # A time that is not a float: stored as a row, as it always was.
    int_time = dataclasses.replace(
        base, id="inttime", attempts=(_skip(0, 1), _skip(1, 1))
    )
    int_time.ts_epoch = 1_790_000_123
    records = [mixed_keys, same_number, int_time]
    ids = _write(path, monkeypatch, records)
    _write(rows_path, monkeypatch, records, as_rows=True)
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]
    assert _skipped_rows(path) == _skipped_rows(rows_path) == 5
    assert _reads(path, ids)["details"] == _reads(rows_path, ids)["details"]


def test_a_set_that_does_not_read_back_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    rows_path = tmp_path / "rows.db"
    real = request_log_module._load_skip_sets

    def wrong(conn: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
        return {set_id: text + " " for set_id, text in real(conn, ids).items()}

    records = [_record(index) for index in range(25)]
    with monkeypatch.context() as patch:
        patch.setattr(request_log_module, "_load_skip_sets", wrong)
        ids = _write(path, monkeypatch, records)
    _write(rows_path, monkeypatch, records, as_rows=True)
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]
    assert _skipped_rows(path) == _skipped_rows(rows_path)
    assert _reads(path, ids)["details"] == _reads(rows_path, ids)["details"]


def test_a_compact_form_that_does_not_read_back_keeps_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    rows_path = tmp_path / "rows.db"
    real = request_log_module._skip_row

    def wrong(request_id: str, member: Any, shared: Any) -> dict[str, Any]:
        row = real(request_id, member, shared)
        row["key_label"] = (row["key_label"] or "") + "!"
        return row

    records = [_record(index) for index in range(25)]
    with monkeypatch.context() as patch:
        patch.setattr(request_log_module, "_skip_row", wrong)
        ids = _write(path, monkeypatch, records)
    _write(rows_path, monkeypatch, records, as_rows=True)
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]
    assert _skipped_rows(path) == _skipped_rows(rows_path)
    assert _reads(path, ids)["details"] == _reads(rows_path, ids)["details"]


def test_a_request_written_again_merges_exactly_as_rows_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``INSERT OR REPLACE`` per attempt: the newer write wins attempt by attempt."""
    path = tmp_path / "requests.db"
    rows_path = tmp_path / "rows.db"
    first = _record(5)
    again = dataclasses.replace(
        first,
        status="error",
        attempts=(
            RouteAttempt(
                attempt=3,
                provider="zen",
                model_ref="zen/model-3",
                outcome=RouteAttemptOutcome.FAILED,
                error_kind="upstream",
                error_message="HTTP 500",
                duration_ms=12.0,
            ),
            _skip(4, 99, key_index=7, key_label="sk-z...9x"),
        ),
    )
    other = _record(15)
    batches = [[first, other], [again], [_record(25), _record(25), _record(35)]]
    for target, as_rows in ((path, False), (rows_path, True)):
        for batch in batches:
            _write(target, monkeypatch, batch, as_rows=as_rows)
    ids = [first.id, other.id, "s00025", "s00035"]
    reads = _reads(path, ids)
    assert reads == _reads(rows_path, ids)
    # The rewritten request is rows only now; the others stay compact.
    assert _table(
        path,
        "SELECT request_id FROM request_attempt_skips ORDER BY request_id",
    ) == [("s00015",), ("s00035",)]
    trace = {
        attempt["attempt"]: attempt
        for attempt in reads["details"][first.id]["route_attempts"]
    }
    assert list(trace) == [0, 1, 3, 4, 5, 6, 7]
    assert trace[3]["outcome"] == "failed"
    assert trace[4]["key_label"] == "sk-z...9x"
    assert trace[5]["key_label"] == first.key_label


def test_a_stored_row_wins_over_a_compact_attempt_of_the_same_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    record = _record(12)
    _write(path, monkeypatch, [record])
    with closing(sqlite3.connect(path)) as conn, conn:
        (members,) = conn.execute(
            "SELECT s.attempts FROM request_attempt_skips AS k"
            " JOIN attempt_skip_sets AS s ON s.id = k.set_id WHERE k.request_id = ?",
            (record.id,),
        ).fetchone()
        number = json.loads(members)[0][0]
        conn.execute(
            "INSERT INTO request_attempts (request_id, attempt, outcome,"
            " error_message) VALUES (?, ?, 'failed', 'stored row')",
            (record.id, number),
        )
    reads = _reads(path, [record.id])
    detail = reads["details"][record.id]
    same = [a for a in detail["route_attempts"] if a["attempt"] == number]
    assert len(same) == 1 and same[0]["error_message"] == "stored row"
    numbers = [a["attempt"] for a in detail["route_attempts"]]
    assert numbers == sorted(numbers) and len(numbers) == len(set(numbers))
    # The attempt export: one row per attempt, the stored one for that number.
    exported = json.loads(reads["attempt_export"]["json"])
    assert [row["attempt"] for row in exported] == numbers
    (row,) = [row for row in exported if row["attempt"] == number]
    assert row["error_message"] == "stored row"


def test_every_insert_column_comes_back_for_a_compact_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    record = _record(4)
    _write(path, monkeypatch, [record])
    assert _skipped_rows(path) == 0
    detail = _reads(path, [record.id])["details"][record.id]
    skipped = [a for a in detail["route_attempts"] if a["outcome"] == "skipped"]
    assert skipped
    for column in request_log_module._ATTEMPT_INSERT_COLUMNS:
        if column == "request_id":
            continue
        assert all(column in attempt for attempt in skipped), column
    # The compact form holds every column a compact attempt can have.
    assert set(request_log_module._SKIP_SET_FIELDS) | set(
        request_log_module._SKIP_SHARED_FIELDS
    ) | set(request_log_module._SKIP_EMPTY_FIELDS) | {"request_id", "outcome"} == set(
        request_log_module._ATTEMPT_INSERT_COLUMNS
    )


def test_a_reader_sees_both_tables_in_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write between the reader's two reads must not hide attempts.

    Here a request's compact attempts are turned back into rows (what a
    request written again does) right between the read of its rows and the
    read of its compact form: without one snapshot the reader would see
    neither.
    """
    path = tmp_path / "requests.db"
    record = _record(6)
    _write(path, monkeypatch, [record])
    expected = _reads(path, [record.id])["details"][record.id]["route_attempts"]
    real = RequestLogStore._skip_rows
    fired: list[bool] = []

    def expand_first(
        self: RequestLogStore,
        conn: sqlite3.Connection,
        request_ids: Any,
        *,
        cache: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        if not fired:
            fired.append(True)
            other = sqlite3.connect(path)
            try:
                other.execute("BEGIN IMMEDIATE")
                self._expand_skip_packs(other, [record.id])
                other.commit()
            finally:
                other.close()
        return real(self, conn, request_ids, cache=cache)

    with monkeypatch.context() as patch:
        _quiet(patch)
        store = RequestLogStore(path, max_rows=0)
        try:
            patch.setattr(RequestLogStore, "_skip_rows", expand_first)
            detail = store.get_request(record.id)
        finally:
            store.close()
    assert fired
    assert detail is not None
    assert detail["route_attempts"] == expected
    # The write did land: the attempts are rows now.
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]


def test_a_rolled_back_write_never_leaves_a_set_in_the_readers_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set id a rolled-back write used is handed out again to another set."""
    path = tmp_path / "requests.db"
    rows_path = tmp_path / "rows.db"
    first = _record(3)
    second = _record(6)
    real = RequestLogStore._accumulate_totals
    calls: list[int] = []

    def fail_once(conn: sqlite3.Connection, fresh: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error (test)")
        real(conn, fresh)

    with monkeypatch.context() as patch:
        _quiet(patch)
        patch.setattr(RequestLogStore, "_accumulate_totals", staticmethod(fail_once))
        store = RequestLogStore(path, max_rows=0)
        store.enqueue(first)
        deadline = time.monotonic() + 30
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        # The write that rolled back proved its set inside its transaction;
        # nothing of it may be left where a reader looks.
        cached_after_rollback = dict(store._skip_set_cache)
        store.enqueue(second)
        deadline = time.monotonic() + 30
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)
        detail = store.get_request(second.id)
        store.close()
    _write(rows_path, monkeypatch, [second], as_rows=True)
    assert len(calls) == 2
    assert cached_after_rollback == {}
    assert _table(path, "SELECT id FROM requests") == [(second.id,)]
    # The next write reuses the rolled-back set id for another set, and is
    # still stored compactly and read back right.
    assert _table(path, "SELECT request_id, set_id FROM request_attempt_skips") == [
        (second.id, 1)
    ]
    assert detail == _reads(rows_path, [second.id])["details"][second.id]


def test_history_never_touches_a_request_already_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older version writing a compact request again leaves rows beside it."""
    path = tmp_path / "requests.db"
    record = _record(9)
    _write(path, monkeypatch, [record])
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO request_attempts (request_id, attempt, provider,"
            " model_ref, outcome, error_message, key_index, key_label, ts_epoch)"
            " VALUES (?, 30, 'zen', 'zen/late', 'skipped', 'never reached',"
            " ?, ?, ?)",
            (record.id, record.key_index, record.key_label, record.ts_epoch),
        )
    sides = _table(path, "SELECT * FROM request_attempt_skips")
    rows = _table(path, "SELECT rowid, * FROM request_attempts ORDER BY rowid")
    expected = _reads(path, [record.id])["details"][record.id]
    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()
    assert state["skipped"]["failed"] == 0
    assert _table(path, "SELECT * FROM request_attempt_skips") == sides
    assert _table(path, "SELECT rowid, * FROM request_attempts ORDER BY rowid") == rows
    assert _reads(path, [record.id])["details"][record.id] == expected


# ----------------------------------------------------------------- history


def test_history_converts_older_rows_frees_pages_and_reads_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    rows_path = tmp_path / "rows.db"
    records = [_record(index) for index in range(400)]
    ids = _write(path, monkeypatch, records, as_rows=True)
    _copy(path, rows_path)
    reads_before = _reads(rows_path, ids)
    skipped_before = _skipped_rows(path)
    kept_rows = (
        "SELECT rowid, request_id, attempt FROM request_attempts"
        " WHERE outcome != 'skipped' OR params IS NOT NULL OR wire_body IS NOT NULL"
        " ORDER BY rowid"
    )
    survivors = _table(path, kept_rows)
    with closing(sqlite3.connect(path)) as conn:
        pages_before = conn.execute(
            "SELECT COUNT(*) FROM dbstat WHERE name LIKE '%request_attempts%'"
        ).fetchone()[0]

    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()

    phase = state["skipped"]
    assert phase["converted"] == skipped_before - 80
    assert phase["kept"] == 80  # bench params and wire snapshots stay rows
    assert phase["failed"] == 0
    assert phase["bytes_after"] < phase["bytes_before"]
    assert _skipped_rows(path) == 80
    assert _compact_members(path) == phase["converted"]
    # Nothing that stays a row moves: same rowids, same order.
    assert _table(path, kept_rows) == survivors
    with closing(sqlite3.connect(path)) as conn:
        pages_after = conn.execute(
            "SELECT COUNT(*) FROM dbstat WHERE name LIKE '%request_attempts%'"
        ).fetchone()[0]
    assert pages_after < pages_before
    assert state["freed_pages"] > 0
    assert _reads(path, ids) == reads_before


def test_history_leaves_what_it_cannot_prove_exactly_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(
        path, monkeypatch, [_record(index) for index in range(40)], as_rows=True
    )
    before = _table(path, "SELECT rowid, * FROM request_attempts ORDER BY rowid")
    real = request_log_module._skip_row

    def wrong(request_id: str, member: Any, shared: Any) -> dict[str, Any]:
        row = real(request_id, member, shared)
        row["error_message"] = (row["error_message"] or "") + " "
        return row

    monkeypatch.setattr(request_log_module, "_skip_row", wrong)
    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()
    monkeypatch.undo()

    assert state["skipped"]["converted"] == 0
    assert state["skipped"]["kept"] == 8
    assert state["skipped"]["failed"] == _skipped_rows(path) - 8
    assert (
        _table(path, "SELECT rowid, * FROM request_attempts ORDER BY rowid") == before
    )
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]
    rows_path = tmp_path / "rows.db"
    _write(
        rows_path, monkeypatch, [_record(index) for index in range(40)], as_rows=True
    )
    assert _reads(path, ids)["details"] == _reads(rows_path, ids)["details"]


def _raw_skipped(path: Path) -> list[tuple[Any, ...]]:
    """Every skipped row as stored: rowid, then each column's class and bytes."""
    columns = ", ".join(
        f"typeof({column}), CAST({column} AS BLOB)"
        for column in request_log_module._ATTEMPT_INSERT_COLUMNS
    )
    with closing(sqlite3.connect(path)) as conn:
        return [
            tuple(row)
            for row in conn.execute(
                f"SELECT rowid, {columns} FROM request_attempts"
                " WHERE outcome = 'skipped' ORDER BY rowid"
            )
        ]


def test_history_keeps_orphans_mixed_requests_and_odd_values_as_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, [_record(index) for index in range(30)], as_rows=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        # An orphan: its request is gone (2 skipped attempts).
        conn.execute("DELETE FROM requests WHERE id = 's00002'")
        # Skipped attempts of one request with different keys (3).
        conn.execute(
            "UPDATE request_attempts SET key_label = 'other'"
            " WHERE request_id = 's00003' AND attempt = 3"
        )
        # Values the compact form does not hold: a BLOB message, a TEXT
        # attempt number, text that is not UTF-8. One attempt each.
        conn.execute(
            "UPDATE request_attempts SET error_message = CAST('x' AS BLOB)"
            " WHERE request_id = 's00005' AND attempt = 4"
        )
        conn.execute(
            "UPDATE request_attempts SET attempt = '9x'"
            " WHERE request_id = 's00006' AND attempt = 5"
        )
        conn.execute(
            "UPDATE request_attempts SET error_message = CAST(X'C328' AS TEXT)"
            " WHERE request_id = 's00007' AND attempt = 3"
        )
    before = _raw_skipped(path)
    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()
    after = _raw_skipped(path)

    phase = state["skipped"]
    assert phase["failed"] == 0
    # 2 orphan + 3 mixed + 3 odd + 3 bench + 3 wire snapshot rows.
    assert phase["kept"] == 14
    assert len(after) == 14
    # Each is exactly the row it was, at the rowid it was.
    assert set(after) <= set(before)
    by_request = {}
    for row in after:
        by_request.setdefault(bytes(row[2]).decode(), []).append(row)
    assert len(by_request["s00002"]) == 2
    assert len(by_request["s00003"]) == 3
    assert len(by_request["s00005"]) == 1
    assert len(by_request["s00006"]) == 1
    # Its odd attempt, and its skipped attempt with a bench reason.
    assert len(by_request["s00007"]) == 2
    sides = {
        row[0] for row in _table(path, "SELECT request_id FROM request_attempt_skips")
    }
    assert "s00002" not in sides and "s00003" not in sides
    assert {"s00005", "s00006", "s00007"} <= sides


def test_history_is_resumable_and_ends_exactly_where_one_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = tmp_path / "whole.db"
    parted = tmp_path / "parted.db"
    _write(whole, monkeypatch, [_record(index) for index in range(120)], as_rows=True)
    with closing(sqlite3.connect(whole)) as conn, conn:
        # Requests whose skipped attempts all stay rows, met by the walk over
        # several steps: each must still be counted once.
        conn.execute("DELETE FROM requests WHERE id IN ('s00012', 's00049')")
        conn.execute(
            "UPDATE request_attempts SET key_label = 'other'"
            " WHERE request_id IN ('s00013', 's00069') AND attempt = 3"
        )
    _finished_7750_document(whole)
    _copy(whole, parted)

    with monkeypatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        store = RequestLogStore(whole, max_rows=0)
        _drive(store)
        store.close()

        patch.setattr(request_log_module, "_HISTORY_STEP_SECONDS", 0.0)
        marks: list[int] = []
        while True:
            store = RequestLogStore(parted, max_rows=0)
            assert store._dictionaries_checked.wait(30)
            conn = store._connect()
            try:
                for _ in range(3):
                    if not store._history_step(conn):
                        break
            finally:
                conn.close()
            store.close()
            state = _state(parted)
            assert state is not None
            marks.append(state["skipped"]["through"])
            if state.get("done_at") is not None:
                break
            assert len(marks) < 2_000
    assert marks == sorted(marks) and len(marks) > 5
    for sql in (
        "SELECT rowid, * FROM request_attempts ORDER BY rowid",
        "SELECT * FROM request_attempt_skips ORDER BY request_id",
        "SELECT attempts FROM attempt_skip_sets ORDER BY attempts",
    ):
        assert _table(parted, sql) == _table(whole, sql)
    one, parts = _state(whole), _state(parted)
    assert one is not None and parts is not None
    for counter in ("converted", "kept", "failed", "bytes_before"):
        assert parts["skipped"][counter] == one["skipped"][counter]


def test_a_finished_7750_document_is_reopened_for_the_skipped_part_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, [_record(index) for index in range(50)], as_rows=True)
    _finished_7750_document(path, returned=5)
    earlier = _state(path)
    assert earlier is not None

    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()

    assert state["reopened_for"] == ["skipped"]
    assert state["returned_at_reopen"] == 5
    for part in ("wire", "bodies", "metadata"):
        assert state[part] == earlier[part]
    assert state["skipped"]["converted"] > 0
    again = RequestLogStore(path, max_rows=0)
    assert again._dictionaries_checked.wait(30)
    time.sleep(0.5)
    again.close()
    assert _state(path) == state


def test_the_history_status_names_the_skipped_part() -> None:
    state = {
        "page_size": 4096,
        "returned_pages": 0,
        "done_at": None,
        "freed_pages": 0,
        **{
            name: {"done_at": _TS, "end": 10, "through": 10}
            for name in ("wire", "bodies", "metadata")
        },
        "skipped": {"done_at": None, "end": 200, "through": 50},
    }
    status = request_log_module._history_status(state)
    assert status["state"] == "converting"
    assert status["phase"] == "skipped"
    assert status["percent"] == 25


# --------------------------------------------------------------- retention


def test_prune_removes_compact_attempts_with_their_request_and_only_unnamed_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    records = [_record(index) for index in range(60)]
    _write(path, monkeypatch, records)
    sides_before = _table(path, "SELECT request_id, set_id FROM request_attempt_skips")
    assert sides_before
    with monkeypatch.context() as patch:
        _quiet(patch)
        store = RequestLogStore(path, max_rows=30)
        assert store._dictionaries_checked.wait(30)
        # Targeted: the pass follows the requests it deleted.
        with store._sweep_lock:
            store._full_sweeps_owed.clear()
        removed = store.prune()
        store.close()
    assert removed == 30
    survivors = {row[0] for row in _table(path, "SELECT id FROM requests")}
    sides = _table(path, "SELECT request_id, set_id FROM request_attempt_skips")
    assert {row[0] for row in sides} == {
        row[0] for row in sides_before if row[0] in survivors
    }
    named = {row[1] for row in sides}
    assert {row[0] for row in _table(path, "SELECT id FROM attempt_skip_sets")} == named


def test_a_whole_sweep_removes_compact_attempts_an_older_version_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, [_record(index) for index in range(40)])
    with closing(sqlite3.connect(path)) as conn, conn:
        # What an older version's prune does: it knows nothing of the table.
        conn.execute("DELETE FROM requests WHERE id < 's00010'")
    with monkeypatch.context() as patch:
        _quiet(patch)
        store = RequestLogStore(path, max_rows=10_000)
        assert store._dictionaries_checked.wait(30)
        assert "request_attempt_skips" in store._full_sweeps_owed
        store.prune()
        store.close()
    assert _table(
        path,
        "SELECT COUNT(*) FROM request_attempt_skips WHERE request_id NOT IN"
        " (SELECT id FROM requests)",
    ) == [(0,)]
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips")[0][0] > 0


def test_clear_erases_compact_attempts_and_their_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, [_record(index) for index in range(20)])
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips")[0][0] > 0
    with monkeypatch.context() as patch:
        _quiet(patch)
        store = RequestLogStore(path, max_rows=0)
        store.clear()
        store.close()
    assert _table(path, "SELECT COUNT(*) FROM request_attempt_skips") == [(0,)]
    assert _table(path, "SELECT COUNT(*) FROM attempt_skip_sets") == [(0,)]


# ------------------------------------------------------- analytics on rows


def test_the_rollup_backfill_gives_the_same_buckets_either_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its two attempt passes read ``params`` and ``ladder_tries``, NULL here."""
    rows_path = tmp_path / "rows.db"
    compact_path = tmp_path / "compact.db"
    records = [_record(index) for index in range(80)]
    _write(rows_path, monkeypatch, records, as_rows=True)
    _write(compact_path, monkeypatch, records)

    def rebuilt(path: Path) -> dict[str, list[tuple[Any, ...]]]:
        out: dict[str, list[tuple[Any, ...]]] = {}
        with closing(sqlite3.connect(path)) as conn:
            for table in request_log_module._ROLLUP_TABLES:
                conn.execute(f"DELETE FROM {table}")
            RequestLogStore._backfill_rollup_chunk(
                conn, 0, 2**40, has_ln=RequestLogStore._has_ln_function(conn)
            )
            for table in request_log_module._ROLLUP_TABLES:
                out[table] = sorted(
                    (tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                    key=repr,
                )
            conn.rollback()
        return out

    built = rebuilt(compact_path)
    assert built == rebuilt(rows_path)
    assert any(built.values())
