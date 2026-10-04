"""Compressed wire snapshots (7.73.0): lossless, self-describing, invisible to readers.

``request_attempts.wire_body`` held each attempt's redacted outbound body as
plain JSON text -- 1.67 GB of a real 12 GB log. New rows store it compressed
when that is smaller: a BLOB envelope in the same column, beside the TEXT rows
every earlier version wrote, read back to exactly the text that was written.
"""

import json
import sqlite3
from collections.abc import Iterator
from compression import zstd
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import export as export_module
from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_TS = 1_790_000_000.0


def _snapshot(index: int, *, messages: int = 6) -> str:
    """A wire snapshot shaped like real ones: knobs whole, text replaced by shape."""
    return json.dumps(
        {
            "model": f"vendor/model-{index % 3}",
            "max_tokens": 16384,
            "stream": True,
            "temperature": 0.2,
            "reasoning": {"effort": "high"},
            "messages": [
                {"role": "user" if turn % 2 == 0 else "assistant", "chars": 40 + turn}
                for turn in range(messages)
            ],
            "tools": {"count": 12, "_names": ["read", "write", "bash", "grep"]},
            "_original_chars": 9000 + index,
        }
    )


#: Every shape the column has to give back exactly. ``None`` is no snapshot,
#: which must stay distinct from an empty one.
CORPUS: dict[str, str | None] = {
    "none": None,
    "empty": "",
    "tiny": "{}",
    "small": '{"max_tokens": 16384, "model": "inkling"}',
    "typical": _snapshot(1),
    "at the 8,000-char cap": _snapshot(2, messages=160)[:8000],
    "huge": _snapshot(3, messages=6000),
    "unicode": json.dumps(
        {"note": "ș ă — → … ✓ 𝄞 中文", "model": "m"}, ensure_ascii=False
    ),
    "escaped unicode": json.dumps({"note": "ș ă — → … ✓ 𝄞"}),
    "looks like a zstd frame": "\x28\xb5\x2f\xfd" + "\x00" * 12 + "frame?",
    "looks like an envelope": "\x01\x00\x28\xb5\x2f\xfd" + "x" * 64,
    "a short envelope look-alike": "\x01\x00\x28\xb5\x2f\xfd",
    "not json": "plain text " * 40,
}


def _attempt(wire_body: str | None, **overrides: Any) -> RouteAttempt:
    fields: dict[str, Any] = {
        "attempt": 0,
        "provider": "nvidia_nim",
        "model_ref": "nvidia_nim/test-model",
        "outcome": RouteAttemptOutcome.SUCCEEDED,
        "duration_ms": 110.0,
        "params": {"early_retries": 1, "wire": {"surface": "chat"}},
        "wire_body": wire_body,
        "reasoning_emitted": True,
        "key_index": 0,
        "key_label": "ab...cd",
        "ladder_tries": 1,
        "ttft_ms": 80.0,
    }
    fields.update(overrides)
    return RouteAttempt(**fields)


def _record(request_id: str, index: int, attempts: tuple[RouteAttempt, ...]) -> Any:
    return RequestRecord(
        id=request_id,
        ts_epoch=_TS + index,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        stream=True,
        input_text=f"prompt {index}",
        output_text=f"reply {index}",
        tokens_in=10,
        tokens_out=20,
        duration_ms=120.0,
        status="success",
        attempts=attempts,
    )


def _reference_store_attempts(conn: sqlite3.Connection, batch: list[Any]) -> None:
    """The 7.72.2 writer of ``request_attempts``, verbatim but for the column
    tuple it was generated from: ``wire_body`` went in as the text itself."""

    columns = request_log_module._ATTEMPT_INSERT_COLUMNS
    rows = [
        (
            record.id,
            attempt.attempt,
            attempt.provider,
            attempt.model_ref,
            attempt.outcome.value,
            attempt.error_kind,
            attempt.error_message,
            attempt.duration_ms,
            json.dumps(attempt.params) if attempt.params else None,
            attempt.wire_body,
            None
            if attempt.reasoning_emitted is None
            else int(attempt.reasoning_emitted),
            attempt.key_index,
            attempt.key_label,
            attempt.ladder_tries,
            attempt.tokens_in,
            attempt.tokens_out,
            attempt.cost_usd,
            attempt.cost_source,
            record.ts_epoch,
            attempt.ttft_ms,
            attempt.first_reasoning_ms,
            attempt.proxy_label,
        )
        for record in batch
        for attempt in record.attempts
    ]
    conn.executemany(
        f"INSERT OR REPLACE INTO request_attempts ({', '.join(columns)}) VALUES"
        f" ({', '.join('?' * len(columns))})",
        rows,
    )


def _reference_wire_body(stored: Any) -> Any:
    """What 7.72.2's ``_fetch_attempts`` made of a stored value."""
    return request_log_module._loads_or_none(stored)


def _stored(path: Path) -> dict[tuple[str, int], tuple[str, Any]]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            (str(request_id), int(attempt)): (str(kind), value)
            for request_id, attempt, kind, value in conn.execute(
                "SELECT request_id, attempt, typeof(wire_body), wire_body"
                " FROM request_attempts"
            )
        }


def _write(store: RequestLogStore, records: list[Any]) -> None:
    for record in records:
        store.enqueue(record)
    store.close()


@pytest.fixture
def corpus_records() -> list[Any]:
    return [
        _record(f"c{index:02d}", index, (_attempt(text),))
        for index, text in enumerate(CORPUS.values())
    ]


def test_every_snapshot_reads_back_exactly_what_was_written(
    tmp_path: Path, corpus_records: list[Any]
) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=0)
    _write(store, corpus_records)

    stored = _stored(store.db_path)
    for record, (name, text) in zip(corpus_records, CORPUS.items(), strict=True):
        _kind, value = stored[(record.id, 0)]
        assert store._decode_wire_body(value) == text, name
        detail = store.get_request(record.id)
        assert detail is not None
        assert detail["route_attempts"][0]["wire_body"] == _reference_wire_body(text)
    # The space actually went: every snapshot worth compressing is a BLOB, and
    # what was too small to shrink, or absent, stayed exactly as it was.
    assert stored[("c00", 0)] == ("null", None)
    assert stored[("c01", 0)] == ("text", "")
    assert stored[("c02", 0)] == ("text", "{}")
    for index, name in enumerate(CORPUS):
        if name in {"typical", "at the 8,000-char cap", "huge", "not json"}:
            assert stored[(f"c{index:02d}", 0)][0] == "blob", name


def test_rows_from_the_old_writer_and_the_new_one_read_the_same(
    tmp_path: Path, corpus_records: list[Any]
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    old = [
        _record(f"old{index:02d}", index, record.attempts)
        for index, record in enumerate(corpus_records)
    ]
    with closing(sqlite3.connect(path)) as conn, conn:
        _reference_store_attempts(conn, old)
    store = RequestLogStore(path, max_rows=0)
    for record in old:
        store.enqueue(record)  # the request rows; attempts are replaced alike
    _write(store, corpus_records)
    with closing(sqlite3.connect(path)) as conn, conn:
        _reference_store_attempts(conn, old)  # back to the old encoding

    stored = _stored(path)
    for old_record, new_record in zip(old, corpus_records, strict=True):
        assert stored[(old_record.id, 0)][0] in {"text", "null"}
        old_detail = store.get_request(old_record.id)
        new_detail = store.get_request(new_record.id)
        assert old_detail is not None and new_detail is not None
        assert old_detail["route_attempts"] == new_detail["route_attempts"]


def test_each_compressed_value_names_its_encoding_and_dictionary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    dictionary = zstd.train_dict(
        [_snapshot(index).encode() for index in range(400)], 4096
    )
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO wire_dictionaries (created_at, content) VALUES (?, ?)",
            (9e12, dictionary.dict_content),  # fresh: no refresh is due
        )
        (wire_id,) = conn.execute("SELECT MAX(id) FROM wire_dictionaries").fetchone()
    store = RequestLogStore(path, max_rows=0)
    _write(store, [_record("r1", 1, (_attempt(_snapshot(1)),))])

    kind, value = _stored(path)[("r1", 0)]
    assert kind == "blob"
    assert value[0] == request_log_module._WIRE_ENVELOPE_V1
    named, offset = request_log_module._read_varint(value, 1)
    assert named == wire_id
    frame = value[offset:]
    # zstd's own id of the dictionary rides in the frame as a second check.
    assert zstd.get_frame_info(frame).dictionary_id == dictionary.dict_id
    assert zstd.decompress(frame, zstd_dict=dictionary).decode() == _snapshot(1)


def test_a_frame_handed_the_wrong_dictionary_is_no_snapshot_not_garbage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path, max_rows=0).close()
    first = zstd.train_dict([_snapshot(i).encode() for i in range(400)], 4096)
    second = zstd.train_dict([(_snapshot(i) * 2).encode() for i in range(400)], 4096)
    with closing(sqlite3.connect(path)) as conn, conn:
        for content in (first.dict_content, second.dict_content):
            conn.execute(
                "INSERT INTO wire_dictionaries (created_at, content) VALUES (?, ?)",
                (9e12, content),
            )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    frame = zstd.compress(_snapshot(5).encode(), zstd_dict=first)
    right = bytes((1,)) + request_log_module._varint(1) + frame
    wrong = bytes((1,)) + request_log_module._varint(2) + frame
    missing = bytes((1,)) + request_log_module._varint(99) + frame
    assert store._decode_wire_body(right) == _snapshot(5)
    assert store._decode_wire_body(wrong) is None
    assert store._decode_wire_body(missing) is None
    assert store._decode_wire_body(bytes((2,)) + frame) is None
    assert store._decode_wire_body(b"") is None
    assert store._decode_wire_body(bytes((1, 0x80))) is None


def test_with_compression_off_snapshots_stay_plain_text(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=0, compress_bodies=False)
    _write(store, [_record("r1", 1, (_attempt(_snapshot(1)),))])
    assert _stored(store.db_path)[("r1", 0)] == ("text", _snapshot(1))


@pytest.mark.parametrize("level", [3, 9, 19])
def test_the_compression_level_setting_reaches_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: int
) -> None:
    calls: list[tuple[bytes, int]] = []
    real = request_log_module.zstd.compress

    def spy(data: bytes, level: int = 3, **kwargs: Any) -> bytes:
        calls.append((bytes(data), level))
        return real(data, level=level, **kwargs)

    monkeypatch.setattr(request_log_module.zstd, "compress", spy)
    store = RequestLogStore(
        tmp_path / "requests.db", max_rows=0, compression_level=level
    )
    _write(store, [_record("r1", 1, (_attempt(_snapshot(1)),))])
    wire_levels = [used for data, used in calls if data == _snapshot(1).encode()]
    assert wire_levels == [level]


def test_an_older_version_reads_new_rows_as_no_snapshot_without_raising(
    tmp_path: Path, corpus_records: list[Any]
) -> None:
    """7.72.2's reader, run over rows this version wrote: a compressed value is
    simply "no snapshot" there -- the accepted rollback consequence -- and a
    plain one reads as it always did."""
    store = RequestLogStore(tmp_path / "requests.db", max_rows=0)
    _write(store, corpus_records)
    for (kind, value), text in zip(
        _stored(store.db_path).values(), CORPUS.values(), strict=True
    ):
        old_view = _reference_wire_body(value)
        if kind == "blob":
            assert old_view is None
        else:
            assert old_view == _reference_wire_body(text)


def _rows(path: Path, table: str, order: str | None) -> list[dict[str, Any]]:
    """Every row of ``table``; ``order`` None sorts in Python (WITHOUT ROWID)."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        sql = f"SELECT * FROM {table}" + (f" ORDER BY {order}" if order else "")
        rows = [dict(row) for row in conn.execute(sql)]
    return rows if order else sorted(rows, key=repr)


def _plain_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process's stores write snapshots exactly as 7.72.2 did."""
    monkeypatch.setattr(
        RequestLogStore,
        "_encode_wire_body",
        lambda self, text, *, level, compress: text,
    )


def _workload() -> list[Any]:
    records = []
    for index in range(60):
        attempts = (
            _attempt(
                _snapshot(index),
                attempt=0,
                outcome=RouteAttemptOutcome.FAILED,
                error_kind="upstream",
                error_message="Upstream returned HTTP 502.",
                model_ref=f"nvidia_nim/model-{index % 4}",
                ttft_ms=None,
            ),
            _attempt(None, attempt=1, outcome=RouteAttemptOutcome.SKIPPED, params=None),
            _attempt(
                _snapshot(index + 1000),
                attempt=2,
                model_ref=f"nvidia_nim/model-{(index + 1) % 4}",
                reasoning_emitted=index % 2 == 0,
                ttft_ms=50.0 + index,
            ),
        )
        records.append(_record(f"w{index:03d}", index, attempts))
    return records


@pytest.fixture
def both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[RequestLogStore, RequestLogStore]]:
    """The same records written the 7.72.2 way and the 7.73.0 way."""
    with monkeypatch.context() as patch:
        _plain_writer(patch)
        plain = RequestLogStore(tmp_path / "plain.db", max_rows=0)
        _write(plain, _workload())
    packed = RequestLogStore(tmp_path / "packed.db", max_rows=0)
    _write(packed, _workload())
    yield plain, packed


def test_written_rows_match_the_old_writer_column_for_column(
    both: tuple[RequestLogStore, RequestLogStore],
) -> None:
    plain, packed = both
    old = _rows(plain.db_path, "request_attempts", "request_id, attempt")
    new = _rows(packed.db_path, "request_attempts", "request_id, attempt")
    assert len(old) == len(new) == 180
    assert sum(isinstance(row["wire_body"], bytes) for row in new) == 120
    for before, after in zip(old, new, strict=True):
        assert packed._decode_wire_body(after.pop("wire_body")) == before.pop(
            "wire_body"
        )
        assert after == before
    for table, order in (
        ("requests", "id"),
        ("request_bodies", "request_id"),
        ("body_blobs", "sha"),
        ("body_dictionaries", "id"),
        ("wire_dictionaries", "id"),
        ("request_stats_rollup", None),
        ("request_stats_detail", None),
        ("request_stats_latency", None),
        ("request_totals", None),
    ):
        assert _rows(plain.db_path, table, order) == _rows(
            packed.db_path, table, order
        ), table


def _render_all(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, bytes]:
    return {
        "json": b"".join(export_module.render_json_array(rows)),
        "csv": b"".join(export_module.render_csv(rows, columns, columns)),
        "txt": b"".join(export_module.render_txt(rows, columns, columns, "t", "s")),
        "xlsx-rows": json.dumps(rows, sort_keys=True, default=str).encode(),
    }


def _readers(store: RequestLogStore) -> dict[str, Any]:
    fields = [field for field, _ in export_module.request_field_labels()]
    columns = export_module.request_detail_columns(fields)
    request_rows = list(
        store.iter_export_rows(columns=columns, need_bodies=True, need_ladder=True)
    )
    attempt_rows = list(store.iter_export_attempt_rows())
    attempt_columns = sorted({key for row in attempt_rows for key in row})
    return {
        "detail": [store.get_request(f"w{index:03d}") for index in range(60)],
        "page": store.list_requests_page(limit=100),
        "search": store.list_requests_page(limit=100, q="prompt 7"),
        "count": store.count_requests(),
        "stats": store.stats(),
        "latency_by_model": store.latency_by_model(),
        "reasoning_by_model": store.reasoning_by_model(),
        "ttft": store.ttft_percentiles(),
        "request_export": _render_all(request_rows, columns),
        "attempt_export": _render_all(attempt_rows, attempt_columns),
    }


def test_every_reader_answers_the_same_for_both_encodings(
    both: tuple[RequestLogStore, RequestLogStore],
) -> None:
    plain, packed = both
    old, new = _readers(plain), _readers(packed)
    for name in old:
        assert old[name] == new[name], name
    # And the detail view, the one reader of the column, really carries it.
    assert new["detail"][0]["route_attempts"][0]["wire_body"] == json.loads(
        _snapshot(0)
    )
