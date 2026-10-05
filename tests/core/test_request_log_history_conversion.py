"""Older history recompressed in the background, losslessly (7.74.0).

7.73.0 compresses what it writes. The history written before it -- wire
snapshots stored as plain JSON text, bodies compressed with a dictionary from
2026-08-09 -- is converted by the writer thread while it is idle: one bounded
step at a time, each row replaced only once its new form has been decoded by
the reader's own decoder and found byte-identical, and the pages that frees
handed back to the filesystem a few at a time.
"""

import hashlib
import json
import sqlite3
import threading
import time
from compression import zstd
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

_TS = 1_790_000_000.0
_KEY = request_log_module._HISTORY_CONVERSION_KEY
#: Taken at import, before any fixture replaces it on the class.
_ORIGINAL_RUN = RequestLogStore._run_history_conversion


def _wire(index: int, *, messages: int = 6) -> str:
    return json.dumps(
        {
            "model": f"vendor/model-{index % 3}",
            "max_tokens": 16384,
            "stream": True,
            "messages": [
                {"role": "user" if turn % 2 == 0 else "assistant", "chars": 40 + turn}
                for turn in range(messages)
            ],
            "tools": {"count": 12, "_names": ["read", "write", "bash", "grep"]},
            "_original_chars": 9000 + index,
        }
    )


#: Every shape a stored TEXT snapshot can have. ``None`` is no snapshot.
CORPUS: list[str | None] = [
    None,
    "",
    "{}",
    '{"max_tokens": 16384, "model": "inkling"}',
    _wire(1),
    _wire(2, messages=160)[:8000],
    _wire(3, messages=3000),
    json.dumps({"note": "ș ă — → … ✓ 𝄞 中文", "model": "m"}, ensure_ascii=False),
    json.dumps({"note": "ș ă — → … ✓ 𝄞"}),
    "\x28\xb5\x2f\xfd" + "\x00" * 12 + "frame?",
    "\x01\x00\x28\xb5\x2f\xfd" + "x" * 64,
    "plain text " * 40,
]


def _prompt(index: int) -> str:
    return (
        "You are a coding agent. Follow the conventions of this repository. " * 40
        + f"\nTurn {index}: explain why test_{index} fails in module m{index % 37}."
    )


def _record(index: int, *, prefix: str = "h", wire: str | None = None) -> RequestRecord:
    return RequestRecord(
        id=f"{prefix}{index:05d}",
        ts_epoch=_TS + index,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        stream=True,
        input_text=_prompt(index),
        output_text=f"Reply {index}: the fixture in m{index % 37} is stale. " * 20,
        tokens_in=10,
        tokens_out=20,
        duration_ms=120.0,
        status="success",
        attempts=(
            RouteAttempt(
                attempt=0,
                provider="nvidia_nim",
                model_ref="nvidia_nim/test-model",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                duration_ms=100.0,
                wire_body=wire if wire is not None else _wire(index),
            ),
            RouteAttempt(
                attempt=1,
                provider="nvidia_nim",
                model_ref="nvidia_nim/other-model",
                outcome=RouteAttemptOutcome.SKIPPED,
            ),
        ),
    )


def _train(samples: list[bytes], size: int = 8192) -> bytes:
    return zstd.train_dict(samples, size).dict_content


@pytest.fixture
def no_background_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer leaves history alone; a test drives the steps itself."""
    monkeypatch.setattr(
        RequestLogStore, "_run_history_conversion", lambda self, conn: None
    )


def _build_history(
    path: Path, monkeypatch: pytest.MonkeyPatch, count: int = 60
) -> None:
    """A log as an older version left it, plus the dictionaries 7.73.0 trains.

    Snapshots are TEXT; bodies are compressed with no dictionary, legacy
    dictionary 1 or legacy dictionary 2 (``kind`` NULL); then one fresh
    dictionary per kind exists, as after 7.73.0's trainer ran.
    """
    with monkeypatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        patch.setattr(RequestLogStore, "_maybe_refresh_dictionaries", lambda self: None)
        store = RequestLogStore(path, max_rows=0)
        for index in range(count):
            wire = CORPUS[index % len(CORPUS)] if index < len(CORPUS) else None
            store.enqueue(_record(index, wire=wire))
        store.close()
    raws: dict[str, bytes] = {}
    with closing(sqlite3.connect(path)) as conn, conn:
        for rowid, stored in conn.execute(
            "SELECT rowid, wire_body FROM request_attempts"
        ).fetchall():
            if isinstance(stored, bytes):
                raw = zstd.decompress(stored[2:])
                conn.execute(
                    "UPDATE request_attempts SET wire_body = ? WHERE rowid = ?",
                    (raw.decode("utf-8"), rowid),
                )
        for sha, dict_id, payload in conn.execute(
            "SELECT sha, dict_id, payload FROM body_blobs"
        ).fetchall():
            assert dict_id is None
            raws[sha] = zstd.decompress(payload)
        samples = list(raws.values()) * 4
        legacy = [_train(samples), _train(samples[::-1])]
        for content in legacy:
            conn.execute(
                "INSERT INTO body_dictionaries (created_at, content) VALUES (?, ?)",
                (_TS, content),
            )
        for position, (sha, raw) in enumerate(sorted(raws.items())):
            which = position % 3
            if which == 0:
                continue
            conn.execute(
                "UPDATE body_blobs SET dict_id = ?, payload = ? WHERE sha = ?",
                (
                    which,
                    zstd.compress(
                        raw, level=9, zstd_dict=zstd.ZstdDict(legacy[which - 1])
                    ),
                    sha,
                ),
            )
        now = time.time()
        for kind in ("prompt", "rest"):
            conn.execute(
                "INSERT INTO body_dictionaries (created_at, content, kind)"
                " VALUES (?, ?, ?)",
                (now, _train(samples, 4096), kind),
            )
        wires = [
            text.encode() for text in (_wire(i, messages=4 + i % 9) for i in range(400))
        ]
        conn.execute(
            "INSERT INTO wire_dictionaries (created_at, content) VALUES (?, ?)",
            (now, _train(wires, 4096)),
        )


def _stored_sizes(path: Path) -> dict[str, int]:
    """Stored bytes of every snapshot and body, keyed by row."""
    with closing(sqlite3.connect(path)) as conn:
        sizes = {
            f"wire:{rowid}": int(size)
            for rowid, size in conn.execute(
                "SELECT rowid, length(CAST(wire_body AS BLOB)) FROM request_attempts"
                " WHERE wire_body IS NOT NULL"
            )
        }
        sizes.update(
            {
                f"body:{sha}": int(size)
                for sha, size in conn.execute(
                    "SELECT sha, length(payload) FROM body_blobs"
                )
            }
        )
    return sizes


def _truth(path: Path) -> dict[str, Any]:
    """Everything the conversion must keep: decoded values, row sets, addresses."""
    with closing(sqlite3.connect(path)) as conn:
        dicts = {
            int(row[0]): zstd.ZstdDict(bytes(row[1]))
            for row in conn.execute("SELECT id, content FROM body_dictionaries")
        }
        wire_dicts = {
            int(row[0]): zstd.ZstdDict(bytes(row[1]))
            for row in conn.execute("SELECT id, content FROM wire_dictionaries")
        }
        wires: dict[int, Any] = {}
        for rowid, stored in conn.execute(
            "SELECT rowid, wire_body FROM request_attempts"
        ):
            if isinstance(stored, bytes):
                dict_id, offset = request_log_module._read_varint(stored, 1)
                stored = zstd.decompress(
                    stored[offset:], zstd_dict=wire_dicts.get(dict_id)
                ).decode()
            wires[int(rowid)] = stored
        bodies = {
            str(sha): zstd.decompress(
                bytes(payload), zstd_dict=dicts.get(dict_id) if dict_id else None
            )
            for sha, dict_id, payload in conn.execute(
                "SELECT sha, dict_id, payload FROM body_blobs"
            )
        }
        counts = {
            str(name): int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            # Every opening of a store adds a session row, and markers are
            # bookkeeping; neither is history.
            if name not in ("server_sessions", "request_log_meta")
        }
        # 7.76.0: a skipped attempt is a row, or a member of its request's
        # compact set; both are attempts the readers return.
        counts["attempts_as_read"] = counts["request_attempts"] + int(
            conn.execute(
                "SELECT COALESCE(SUM(json_array_length(s.attempts)), 0)"
                " FROM request_attempt_skips AS k"
                " JOIN attempt_skip_sets AS s ON s.id = k.set_id"
            ).fetchone()[0]
        )
    return {"wires": wires, "bodies": bodies, "counts": counts}


def _details(store: RequestLogStore, ids: list[str]) -> dict[str, Any]:
    return {request_id: store.get_request(request_id) for request_id in ids}


def _ids(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return [
            str(row[0]) for row in conn.execute("SELECT id FROM requests ORDER BY id")
        ]


def _state(path: Path) -> dict[str, Any] | None:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (_KEY,)
        ).fetchone()
    return None if row is None else json.loads(row[0])


def _wait_done(
    store: RequestLogStore, path: Path, timeout: float = 120.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(path)
        if state is not None and state.get("done_at") is not None:
            return state
        time.sleep(0.05)
    raise AssertionError(f"the conversion never finished: {_state(path)}")


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


def _storage_classes(path: Path) -> dict[int, str]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            int(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT rowid, typeof(wire_body) FROM request_attempts"
            )
        }


# --------------------------------------------------------------- lossless


def test_history_converts_in_the_background_and_reads_back_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch)
    before = _truth(path)
    ids = _ids(path)
    sizes_before = _stored_sizes(path)
    with monkeypatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        reader = RequestLogStore(path, max_rows=0)
        details_before = _details(reader, ids)
        reader.close()

    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(store, path)
    details_after = _details(store, ids)
    store.close()
    after = _truth(path)

    assert after == before
    assert details_after == details_before
    # A value is only ever replaced by a smaller one.
    sizes_after = _stored_sizes(path)
    assert sizes_after.keys() == sizes_before.keys()
    assert all(sizes_after[key] <= sizes_before[key] for key in sizes_before)
    assert sum(sizes_after.values()) < sum(sizes_before.values())
    assert state["wire"]["failed"] == 0
    assert state["bodies"]["failed"] == 0
    text_rows = sum(1 for value in before["wires"].values() if value is not None)
    assert state["wire"]["converted"] + state["wire"]["kept"] == text_rows
    assert state["wire"]["converted"] > 0
    assert state["bodies"]["converted"] > 0
    classes = _storage_classes(path)
    for rowid, value in before["wires"].items():
        if value is None:
            # No snapshot stays no snapshot, never an empty one.
            assert classes[rowid] == "null"
        elif len(value.encode()) < 16:
            # Compression cannot help: the row stays TEXT, as the writer
            # would store it.
            assert classes[rowid] == "text"


def test_every_body_is_on_the_newest_dictionary_of_its_kind_and_hashes_to_its_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch)
    store = RequestLogStore(path, max_rows=0)
    _wait_done(store, path)
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        newest = {
            str(kind): int(dict_id)
            for kind, dict_id in conn.execute(
                "SELECT kind, MAX(id) FROM body_dictionaries"
                " WHERE kind IS NOT NULL GROUP BY kind"
            )
        }
        dicts = {
            int(row[0]): zstd.ZstdDict(bytes(row[1]))
            for row in conn.execute("SELECT id, content FROM body_dictionaries")
        }
        rows = conn.execute(
            "SELECT b.sha, b.dict_id, b.payload,"
            " EXISTS (SELECT 1 FROM request_bodies WHERE input_sha = b.sha)"
            " FROM body_blobs b"
        ).fetchall()
    assert rows
    moved = 0
    for sha, dict_id, payload, as_prompt in rows:
        raw = zstd.decompress(bytes(payload), zstd_dict=dicts[dict_id])
        assert hashlib.sha256(raw).hexdigest() == sha
        target = newest["prompt" if as_prompt else "rest"]
        if dict_id == target:
            moved += 1
        else:
            # Kept on its old dictionary only because the new one would not
            # have made it smaller.
            again = zstd.compress(raw, level=9, zstd_dict=dicts[target])
            assert len(again) >= len(payload)
    assert moved > 0


def test_a_corrupt_body_is_left_byte_identical_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=20)
    with closing(sqlite3.connect(path)) as conn, conn:
        sha, payload = conn.execute(
            "SELECT sha, payload FROM body_blobs WHERE dict_id IS NOT NULL"
            " ORDER BY rowid LIMIT 1"
        ).fetchone()
        corrupt = bytes(payload[:-7]) + b"garbage"
        conn.execute("UPDATE body_blobs SET payload = ? WHERE sha = ?", (corrupt, sha))
        # And one whose content no longer hashes to its address.
        other_sha, other_dict = conn.execute(
            "SELECT sha, dict_id FROM body_blobs WHERE sha != ? AND dict_id IS NULL"
            " ORDER BY rowid LIMIT 1",
            (sha,),
        ).fetchone()
        assert other_dict is None
        mislabelled = zstd.compress(b"not what the address says", level=9)
        conn.execute(
            "UPDATE body_blobs SET payload = ? WHERE sha = ?", (mislabelled, other_sha)
        )

    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(store, path)
    store.close()

    assert state["bodies"]["failed"] == 2
    with closing(sqlite3.connect(path)) as conn:
        assert (
            bytes(
                conn.execute(
                    "SELECT payload FROM body_blobs WHERE sha = ?", (sha,)
                ).fetchone()[0]
            )
            == corrupt
        )
        assert (
            bytes(
                conn.execute(
                    "SELECT payload FROM body_blobs WHERE sha = ?", (other_sha,)
                ).fetchone()[0]
            )
            == mislabelled
        )


def test_a_round_trip_mismatch_leaves_the_row_untouched_and_logs_only_a_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=20)
    before = _truth(path)
    classes_before = _storage_classes(path)
    real = RequestLogStore._unwrap_wire_envelope

    def lying(self: RequestLogStore, data: bytes) -> bytes:
        # The reader would get back something other than what was stored.
        return real(self, data) + b" "

    warnings: list[str] = []
    sink = request_log_module.logger.add(
        lambda message: warnings.append(str(message)), level="WARNING"
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(RequestLogStore, "_unwrap_wire_envelope", lying)
            patch.setattr(
                RequestLogStore, "_run_history_conversion", lambda self, conn: None
            )
            store = RequestLogStore(path, max_rows=0)
            conn = store._connect()
            try:
                while (_state(path) or {}).get("wire", {}).get("done_at") is None:
                    store._convert_wire_step(conn)
            finally:
                conn.close()
            store.close()
    finally:
        request_log_module.logger.remove(sink)

    state = _state(path)
    assert state is not None
    text_rows = sum(1 for value in before["wires"].values() if value is not None)
    assert state["wire"]["converted"] == 0
    assert state["wire"]["failed"] + state["wire"]["kept"] == text_rows
    assert state["wire"]["failed"] > 0
    assert _storage_classes(path) == classes_before
    assert _truth(path)["wires"] == before["wires"]
    said = [line for line in warnings if "left" in line and "wire snapshots" in line]
    assert len(said) == 1
    assert str(state["wire"]["failed"]) in said[0]
    # A count, never the content.
    assert "vendor/model" not in said[0]


def test_a_snapshot_the_reader_would_decode_differently_is_never_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=20)
    classes = _storage_classes(path)
    real = RequestLogStore._decode_wire_body

    def misreading(self: RequestLogStore, stored: Any) -> str | None:
        text = real(self, stored)
        return text + " " if isinstance(stored, bytes) and text is not None else text

    monkeypatch.setattr(RequestLogStore, "_decode_wire_body", misreading)
    store = RequestLogStore(path, max_rows=0)
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None
    assert state["wire"]["converted"] == 0
    assert state["wire"]["failed"] > 0
    assert _storage_classes(path) == classes


def test_a_body_the_reader_would_decode_differently_is_never_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=20)
    with closing(sqlite3.connect(path)) as conn:
        blobs_before = conn.execute(
            "SELECT sha, dict_id, payload FROM body_blobs ORDER BY sha"
        ).fetchall()
    real = RequestLogStore._raw_payload

    def misreading(self: RequestLogStore, payload: Any, dict_id: Any) -> bytes | None:
        raw = real(self, payload, dict_id)
        return None if raw is None else raw + b"\x00"

    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    monkeypatch.setattr(RequestLogStore, "_raw_payload", misreading)
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None
    assert state["bodies"]["converted"] == 0
    assert state["bodies"]["failed"] > 0
    with closing(sqlite3.connect(path)) as conn:
        assert (
            conn.execute(
                "SELECT sha, dict_id, payload FROM body_blobs ORDER BY sha"
            ).fetchall()
            == blobs_before
        )


class _BusyQueue:
    """A queue that always has a request waiting, and never hands one out."""

    def empty(self) -> bool:
        return False

    def get(self, timeout: float | None = None) -> Any:
        time.sleep(timeout or 0.01)
        raise request_log_module.queue.Empty

    def get_nowait(self) -> Any:
        raise request_log_module.queue.Empty

    def put_nowait(self, item: Any) -> None:
        return None


def test_a_waiting_request_gets_the_writer_before_any_step_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=20)
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    steps: list[int] = []
    monkeypatch.setattr(
        RequestLogStore, "_history_step", lambda self, conn: steps.append(1) or True
    )
    store._history_index_checked = True
    real_queue = store._queue
    monkeypatch.setattr(store, "_queue", _BusyQueue())
    conn = sqlite3.connect(path)
    try:
        # The real method; the fixture only replaced what the writer calls.
        _ORIGINAL_RUN(store, conn)
    finally:
        conn.close()
        monkeypatch.setattr(store, "_queue", real_queue)
    store.close()
    assert steps == []


# ------------------------------------------------------- resume and repeat


def test_stopped_after_k_steps_it_resumes_from_its_marker_and_matches_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    whole = tmp_path / "whole.db"
    parted = tmp_path / "parted.db"
    _build_history(whole, monkeypatch, count=40)
    with (
        closing(sqlite3.connect(whole)) as source,
        closing(sqlite3.connect(parted)) as copy,
    ):
        source.backup(copy)
    before = _truth(whole)

    store = RequestLogStore(whole, max_rows=0)
    _drive(store)
    store.close()

    # One row per step, and a "restart" every few steps.
    monkeypatch.setattr(request_log_module, "_HISTORY_STEP_SECONDS", 0.0)
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
        marks.append(state["wire"]["through"] + state["bodies"]["through"])
        if state.get("done_at") is not None:
            break
        assert len(marks) < 2_000

    # The marker only ever moves forward.
    assert marks == sorted(marks)
    one, many = _state(whole), _state(parted)
    assert one is not None and many is not None
    for phase in ("wire", "bodies"):
        for counter in ("converted", "kept", "failed", "bytes_before", "bytes_after"):
            assert one[phase][counter] == many[phase][counter], (phase, counter)
    with closing(sqlite3.connect(whole)) as a, closing(sqlite3.connect(parted)) as b:
        for sql in (
            "SELECT rowid, wire_body FROM request_attempts ORDER BY rowid",
            "SELECT sha, dict_id, payload FROM body_blobs ORDER BY sha",
        ):
            assert a.execute(sql).fetchall() == b.execute(sql).fetchall()
    assert _truth(parted) == before


def test_a_failure_inside_a_step_rolls_the_whole_step_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=30)
    with closing(sqlite3.connect(path)) as conn:
        rows_before = conn.execute(
            "SELECT rowid, wire_body FROM request_attempts ORDER BY rowid"
        ).fetchall()
    store = RequestLogStore(path, max_rows=0)

    def interrupted(
        self: RequestLogStore, conn: sqlite3.Connection, state: Any
    ) -> None:
        raise sqlite3.OperationalError("interrupted between two statements")

    assert store._dictionaries_checked.wait(30)
    conn = store._connect()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(RequestLogStore, "_save_history_state", interrupted)
            with pytest.raises(sqlite3.OperationalError):
                store._convert_wire_step(conn)
        assert not conn.in_transaction
    finally:
        conn.close()
    with closing(sqlite3.connect(path)) as check:
        assert (
            check.execute(
                "SELECT rowid, wire_body FROM request_attempts ORDER BY rowid"
            ).fetchall()
            == rows_before
        )
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _state(path) is None
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None and state["done_at"] is not None


def test_a_second_run_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=30)
    store = RequestLogStore(path, max_rows=0)
    first = _wait_done(store, path)
    store.close()

    def dump() -> list[str]:
        # Each opening of a store adds its own session row; nothing else may
        # differ.
        with closing(sqlite3.connect(path)) as conn:
            return [
                line
                for line in conn.iterdump()
                if not line.startswith('INSERT INTO "server_sessions"')
                and "VALUES('server_sessions'," not in line
            ]

    dump_before = dump()
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    time.sleep(1.0)
    _drive(store)
    store.close()
    assert dump() == dump_before
    assert _state(path) == first


# ------------------------------------------------------------ interleaving


def test_live_writes_during_the_conversion_are_all_stored_and_nothing_changes_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=80)
    before = _truth(path)
    monkeypatch.setattr(request_log_module, "_HISTORY_STEP_SECONDS", 0.0)
    store = RequestLogStore(path, max_rows=0)

    def burst() -> None:
        for index in range(120):
            store.enqueue(_record(index, prefix="live"))
            if index % 7 == 0:
                time.sleep(0.02)

    writer = threading.Thread(target=burst)
    writer.start()
    writer.join()
    _wait_done(store, path)
    live_ids = [f"live{index:05d}" for index in range(120)]
    stored = _details(store, live_ids)
    store.close()

    assert all(stored[request_id] is not None for request_id in live_ids)
    after = _truth(path)
    for rowid, value in before["wires"].items():
        assert after["wires"][rowid] == value
    for sha, raw in before["bodies"].items():
        assert after["bodies"][sha] == raw
    # Written rows: the conversion added and removed nothing; only the live
    # requests (and their attempts and bodies) are new.
    assert after["counts"]["requests"] == before["counts"]["requests"] + 120
    assert after["counts"]["attempts_as_read"] == (
        before["counts"]["attempts_as_read"] + 240
    )
    for table in ("body_dictionaries", "wire_dictionaries"):
        assert after["counts"][table] == before["counts"][table]


def test_a_prune_pass_during_the_conversion_deletes_exactly_what_it_would_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=80)
    before = _truth(path)
    monkeypatch.setattr(request_log_module, "_HISTORY_STEP_SECONDS", 0.0)
    store = RequestLogStore(path, max_rows=100)
    for index in range(100):
        store.enqueue(_record(index, prefix="live"))
    _wait_done(store, path)
    store.close()

    kept = set(_ids(path))
    # Prune keeps the newest 100 by time: the live rows are newer than all
    # history only by id here, so compare against the rule itself.
    with closing(sqlite3.connect(path)) as conn:
        assert len(kept) == 100
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        orphans = conn.execute(
            "SELECT COUNT(*) FROM request_attempts a WHERE NOT EXISTS"
            " (SELECT 1 FROM requests r WHERE r.id = a.request_id)"
        ).fetchone()[0]
        attempts = {
            int(rowid): request_id
            for rowid, request_id in conn.execute(
                "SELECT rowid, request_id FROM request_attempts"
            )
        }
    assert orphans == 0
    after = _truth(path)
    for rowid, value in before["wires"].items():
        if rowid in attempts:
            assert after["wires"][rowid] == value
    for sha, raw in after["bodies"].items():
        if sha in before["bodies"]:
            assert raw == before["bodies"][sha]


def test_the_wire_walk_waits_for_a_dictionary_then_uses_the_newest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=30)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM wire_dictionaries")
    classes = _storage_classes(path)
    monkeypatch.setattr(
        RequestLogStore, "_maybe_refresh_dictionaries", lambda self: None
    )
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    conn = store._connect()
    try:
        # No dictionary, and the trainer has not said it cannot train one.
        assert store._convert_wire_step(conn) is False
        assert _storage_classes(path) == classes
        # A dictionary arrives: the walk uses it.
        content = _train(
            [_wire(i, messages=4 + i % 9).encode() for i in range(400)], 4096
        )
        store._trained.append(("wire", content, time.time()))
        store._install_trained_dictionaries(conn)
        newest = store._kind_dicts["wire"][0]
        while store._convert_wire_step(conn):
            if (_state(path) or {})["wire"]["done_at"] is not None:
                break
    finally:
        conn.close()
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        envelopes = [
            bytes(row[0])
            for row in conn.execute(
                "SELECT wire_body FROM request_attempts WHERE typeof(wire_body) = 'blob'"
            )
        ]
    assert envelopes
    assert {request_log_module._read_varint(data, 1)[0] for data in envelopes} == {
        newest
    }


def test_too_few_samples_ever_to_train_converts_without_a_dictionary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=30)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM wire_dictionaries")
    before = _truth(path)
    store = RequestLogStore(path, max_rows=0)
    # 30 snapshots: the real trainer finds too few and says so.
    _settle_trainer(store)
    assert "wire" in store._dict_too_few_samples
    _drive(store)
    store.close()
    assert _truth(path)["wires"] == before["wires"]
    with closing(sqlite3.connect(path)) as conn:
        ids = {
            request_log_module._read_varint(bytes(row[0]), 1)[0]
            for row in conn.execute(
                "SELECT wire_body FROM request_attempts WHERE typeof(wire_body) = 'blob'"
            )
        }
    assert ids == {0}


def _settle_trainer(store: RequestLogStore) -> None:
    assert store._dictionaries_checked.wait(60)
    trainer = store._trainer
    if trainer is not None:
        trainer.join(timeout=120)
        assert not trainer.is_alive()


def test_no_dictionary_any_row_names_is_ever_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=40)
    with closing(sqlite3.connect(path)) as conn:
        bodies_before = {
            int(r[0]) for r in conn.execute("SELECT id FROM body_dictionaries")
        }
        wires_before = {
            int(r[0]) for r in conn.execute("SELECT id FROM wire_dictionaries")
        }
    assert {1, 2} <= bodies_before
    store = RequestLogStore(path, max_rows=0)
    _wait_done(store, path)
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        bodies_after = {
            int(r[0]) for r in conn.execute("SELECT id FROM body_dictionaries")
        }
        wires_after = {
            int(r[0]) for r in conn.execute("SELECT id FROM wire_dictionaries")
        }
        named = {
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT dict_id FROM body_blobs WHERE dict_id IS NOT NULL"
            )
        }
        wire_named = {
            request_log_module._read_varint(bytes(r[0]), 1)[0]
            for r in conn.execute(
                "SELECT wire_body FROM request_attempts WHERE typeof(wire_body) = 'blob'"
            )
        }
    assert bodies_after >= bodies_before
    assert wires_after >= wires_before
    assert named <= bodies_after
    assert wire_named - {0} <= wires_after


# ------------------------------------------------------------ space return


def test_freed_pages_go_back_only_after_the_marker_counts_them_and_never_older_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=60)
    # Pages that were free before the conversion freed anything: they may
    # hold deleted rows, and are never handed back by it (IV.13).
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE scratch_deleted (payload BLOB)")
        conn.executemany(
            "INSERT INTO scratch_deleted VALUES (?)",
            [(bytes([i % 251]) * 20_000,) for i in range(40)],
        )
        conn.commit()
        conn.execute("DROP TABLE scratch_deleted")
        conn.commit()
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
    assert free_before > 100

    vacuums: list[str] = []
    real_connect = RequestLogStore._connect

    def traced(self: RequestLogStore) -> sqlite3.Connection:
        conn = real_connect(self)
        conn.set_trace_callback(
            lambda sql: vacuums.append(sql) if "incremental_vacuum" in sql else None
        )
        return conn

    monkeypatch.setattr(RequestLogStore, "_connect", traced)
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    conn = store._connect()
    try:
        # Nothing converted yet: nothing is owed, and nothing is handed back.
        assert store._return_space_step(conn) is False
        assert vacuums == []
        while store._history_step(conn):
            state = _state(path)
            assert state is not None
            # Never more handed back than the conversion itself freed.
            assert state["returned_pages"] <= state["freed_pages"]
            if vacuums:
                assert state["freed_pages"] > 0
    finally:
        conn.close()
    store.close()

    state = _state(path)
    assert state is not None and state["done_at"] is not None
    assert state["freelist_at_start"] == free_before
    assert state["freed_pages"] > 0
    assert vacuums
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] >= free_before
        assert conn.execute("PRAGMA page_count").fetchone()[0] < pages_before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert state["returned_pages"] == state["freed_pages"]


def test_pages_the_conversion_freed_but_new_rows_reused_are_never_made_up_from_older_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    """Owed pages are a count; the freelist above its starting size is the cap.

    When new rows reuse pages the conversion freed, what is left on the
    freelist is the older free pages -- and those are never handed back.
    """
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=60)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE scratch_deleted (payload BLOB)")
        conn.executemany(
            "INSERT INTO scratch_deleted VALUES (?)",
            [(bytes([i % 251]) * 20_000,) for i in range(40)],
        )
        conn.commit()
        conn.execute("DROP TABLE scratch_deleted")
        conn.commit()
        free_start = conn.execute("PRAGMA freelist_count").fetchone()[0]
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    conn = store._connect()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(RequestLogStore, "_return_space_step", lambda self, c: False)
            while store._history_step(conn):
                pass
        state = _state(path)
        assert state is not None
        assert state["wire"]["done_at"] is not None
        assert state["bodies"]["done_at"] is not None
        assert state["freed_pages"] > state["returned_pages"]
        # New rows take the freed pages back, down to the older free pages.
        conn.execute("CREATE TABLE scratch_reuse (payload BLOB)")
        while conn.execute("PRAGMA freelist_count").fetchone()[0] > free_start:
            conn.execute("INSERT INTO scratch_reuse VALUES (?)", (b"r" * 3_000,))
        conn.commit()
        free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        while store._history_step(conn):
            pass
        free_after = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()
    store.close()
    state = _state(path)
    assert state is not None and state["done_at"] is not None
    assert free_after == free_before


def test_an_older_version_reads_converted_history_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7.72.2's readers over converted history: a converted snapshot is "no
    snapshot" there (the accepted rollback consequence); every body decodes,
    because its dictionary is a ``body_dictionaries`` row named by id."""
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=40)
    before = _truth(path)
    store = RequestLogStore(path, max_rows=0)
    _wait_done(store, path)
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        stored = dict(
            conn.execute("SELECT rowid, wire_body FROM request_attempts").fetchall()
        )
        dicts = {
            int(row[0]): zstd.ZstdDict(bytes(row[1]))
            for row in conn.execute("SELECT id, content FROM body_dictionaries")
        }
        blobs = conn.execute("SELECT sha, dict_id, payload FROM body_blobs").fetchall()
    converted = 0
    for rowid, value in stored.items():
        # What 7.72.2's ``_fetch_attempts`` made of a stored value.
        old_view = request_log_module._loads_or_none(value)
        if isinstance(value, bytes):
            converted += 1
            assert old_view is None
        else:
            assert old_view == request_log_module._loads_or_none(before["wires"][rowid])
    assert converted > 0
    for sha, dict_id, payload in blobs:
        old = zstd.decompress(
            bytes(payload), zstd_dict=dicts[dict_id] if dict_id is not None else None
        )
        assert old == before["bodies"][sha]


def test_the_data_mark_does_not_move_for_the_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    """Derived payloads stay valid: the conversion changes no value."""
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=30)
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    time.sleep(1.0)
    mark = store.data_mark()
    _drive(store)
    assert _state(path) is not None
    assert store.data_mark() == mark
    store.close()


def test_progress_is_a_read_only_field_of_the_storage_readout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_background_conversion: None,
) -> None:
    path = tmp_path / "requests.db"
    _build_history(path, monkeypatch, count=30)
    monkeypatch.setattr(request_log_module, "_HISTORY_STEP_SECONDS", 0.0)
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    assert store.storage_footprint()["history"]["state"] == "pending"
    conn = store._connect()
    try:
        store._history_step(conn)
        seen = store.storage_footprint()["history"]
        assert seen["state"] == "converting"
        assert seen["phase"] == "snapshots"
        assert 0 <= seen["percent"] < 100
        while store._history_step(conn):
            pass
    finally:
        conn.close()
    done = store.storage_footprint()["history"]
    store.close()
    assert done["state"] == "done"
    assert done["percent"] == 100

    off = RequestLogStore(tmp_path / "off.db", max_rows=0, compress_bodies=False)
    assert off.storage_footprint()["history"]["state"] == "off"
    off.close()
