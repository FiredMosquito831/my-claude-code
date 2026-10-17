"""The prompt chunk store (7.93.0): prompts stored as chunk lists of the search index file.

Spec: INVESTIGATION-REQUEST-LOG-COMPRESSION-2 §11 item 5 (C) and the USER
DECISIONS of 2026-10-04: every converted prompt is decoded and compared byte
for byte before its stored form is replaced; a mismatch leaves the row
untouched; exports and reads identical; resumable; old versions read a
converted prompt as empty; freed space handed back only while the freelist
holds nothing recoverable; chunks removed only by mark and sweep.
"""

import hashlib
import io
import json
import random
import sqlite3
import threading
import time
import zipfile
from collections.abc import Iterator
from compression import zstd
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import export as ex
from my_claude_code.core import request_log as rl
from my_claude_code.core import request_search as rs
from my_claude_code.core.request_log import (
    _INPUT_FIELDS,
    RequestLogStore,
    RequestRecord,
    pack_fields,
)

_KEY = rl._HISTORY_CONVERSION_KEY
_WORDS = (
    "alpha",
    "proxy",
    "8082",
    "README.md",
    "TODO",
    "Traceback",
    "pytest",
    "too",
    "long",
    "error",
    "429",
    "C:\\Users\\dev",
    "toolu_",
    "mod_name",
    "50%",
)
# Text a prompt can hold that JSON writes in more than one way: non-ASCII
# (escaped), quotes, backslashes, tabs.
_ODD = (
    "naïve — ș ă → … ✓ 日本語 🙂",
    'quote " and back\\slash\ttab',
    "done — tests",
)
# Control characters and a lone surrogate too: every Python string the writer
# can be handed. (The xlsx export cannot hold the first and no export can
# encode the second, chunked or not, so they stay out of the tests that
# compare exports.)
_ODDER = (*_ODD, "ctrl \x01 and \x7f", "lone \ud800 surrogate")
_TERMS = (
    "too long",
    "TODO",
    "turn",
    "zqxjvkw",
    "ș",
    "—",
    "🙂",
    "日本語",
    "naïve",
    '"',
    "back\\slash",
    "x",
    "echo 3",
    "Traceback 429",
)
_FILTERS: tuple[dict[str, Any], ...] = (
    {"local": "hide"},
    {"local": "all"},
    {"local": "hide", "status": "error"},
)
_FILTER_NAMES = (
    "provider",
    "model",
    "status",
    "endpoint",
    "key",
    "since",
    "until",
    "local",
    "harness",
    "session",
    "folder",
    "exit",
)


class _Reader(RequestLogStore):
    """The real store with no writer thread: it only reads."""

    def _writer_loop(self) -> None:
        return


def _lines(rng: random.Random, count: int) -> str:
    """Lines of words and hex ids: text that cuts on newlines and compresses as prose does."""
    out = [
        " ".join(
            rng.choice(_WORDS) if rng.random() < 0.6 else f"{rng.getrandbits(40):010x}"
            for _ in range(rng.randint(4, 12))
        )
        for _ in range(count)
    ]
    return "\n".join(out) + "\n"


def _records(
    count: int, *, seed: int = 5, start: int = 0, odd: tuple[str, ...] = _ODD
) -> list[RequestRecord]:
    """Conversations whose prompts grow turn by turn, as real ones do."""
    rng = random.Random(seed)
    sessions: dict[int, str] = {}
    out: list[RequestRecord] = []
    for n in range(start, start + count):
        session = n % 4
        text = sessions.get(
            session,
            f"system prompt of session {session}\n"
            + "".join(f"rule {k}: be careful\n" for k in range(60))
            + _lines(rng, 40),
        )
        text += (
            f"turn {n}:\n" + _lines(rng, rng.randint(5, 60)) + rng.choice(odd) + "\n"
        )
        sessions[session] = text
        out.append(
            RequestRecord(
                id=f"rec-{n:05d}",
                ts_epoch=1000.0 + n,
                endpoint="/v1/messages",
                protocol="anthropic",
                requested_model="claude-opus-4-1",
                provider="zen",
                resolved_model="big-pickle",
                status="error" if n % 7 == 0 else "success",
                input_text=text,
                output_text=f"reply {n} too long",
                thinking_text=f"thinking about turn {n}" if n % 2 else None,
                tool_calls=[{"name": "Bash", "input": {"command": f"echo {n}"}}]
                if n % 3 == 0
                else None,
            )
        )
    return out


def _prompt_bytes(record: RequestRecord) -> bytes:
    return pack_fields({"input_text": record.input_text}, _INPUT_FIELDS)


def _write(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[RequestRecord],
    **options: Any,
) -> None:
    """Write ``records`` as history: nothing is converted while they are written.

    No dictionary is trained meanwhile either, so each prompt is stored as
    large as an older version stored it, across pages a conversion frees.
    """
    with monkeypatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        patch.setattr(RequestLogStore, "_maybe_refresh_dictionaries", lambda self: None)
        store = RequestLogStore(
            path, max_rows=0, queue_max_size=len(records) + 10, **options
        )
        for record in records:
            store.enqueue(record)
        store.close()


def _raws(path: Path) -> dict[str, bytes | None]:
    """Every stored blob's packed bytes, read through the store's own decoder."""
    store = _Reader(path, max_rows=0)
    try:
        with closing(store._connect()) as conn:
            return {
                str(sha): store._raw_payload(payload, dict_id)
                for sha, dict_id, payload in conn.execute(
                    "SELECT sha, dict_id, payload FROM body_blobs"
                )
            }
    finally:
        store.close()


def _state(path: Path) -> dict[str, Any] | None:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (_KEY,)
        ).fetchone()
    return None if row is None else json.loads(row[0])


def _wait(predicate: Any, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def _chunks_caught_up(path: Path) -> bool:
    state = _state(path)
    if state is None or state.get("done_at") is None:
        return False
    chunks = state.get("chunks") or {}
    if chunks.get("done_at") is None:
        return False
    with closing(sqlite3.connect(path)) as conn:
        top = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM body_blobs").fetchone()[
            0
        ]
    return int(chunks.get("through") or 0) >= int(top)


def _convert(path: Path, **options: Any) -> dict[str, Any]:
    """Open the log as a server would and let it convert; the finished document."""
    store = RequestLogStore(path, max_rows=0, **options)
    try:
        _wait(lambda: _chunks_caught_up(path))
    finally:
        store.close()
    state = _state(path)
    assert state is not None
    return state


def _chunked(path: Path) -> dict[str, bytes]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            str(sha): bytes(payload)
            for sha, payload in conn.execute(
                "SELECT sha, payload FROM body_blobs WHERE dict_id = ?",
                (rl._CHUNKED_DICT_ID,),
            )
        }


def _prompt_shas(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT input_sha FROM request_bodies"
                " WHERE input_sha IS NOT NULL"
            )
        }


def _export_bytes(store: RequestLogStore) -> dict[str, bytes]:
    """The request export in every format, every field, as the route renders it."""
    selected = ex.validate_fields(ex.REQUEST_SCOPE, list(ex.REQUEST_FIELD_IDS))
    sql_columns = ex.request_detail_columns(selected)
    output_columns = sql_columns + ex.request_detail_derived_columns(selected)
    headers = ex.request_detail_headers(output_columns)

    def rows() -> Iterator[dict[str, Any]]:
        iterator = store.iter_export_rows(
            columns=sql_columns,
            need_bodies=ex.requires_request_bodies(selected),
            need_ladder="ladder" in selected,
        )
        try:
            for row in iterator:
                ex.compute_request_detail_derived(row, selected, {})
                yield {column: row.get(column) for column in output_columns}
        finally:
            iterator.close()

    out = {
        "json": b"".join(ex.render_json_array(rows())),
        "csv": b"".join(ex.render_csv(rows(), output_columns, headers)),
        "txt": b"".join(
            ex.render_txt(rows(), output_columns, headers, title="t", summary="s")
        ),
    }
    with zipfile.ZipFile(
        io.BytesIO(b"".join(ex.render_xlsx(rows(), output_columns, headers)))
    ) as archive:
        out["xlsx"] = b"".join(
            name.encode() + b"\0" + archive.read(name)
            for name in sorted(archive.namelist())
            if name != "docProps/core.xml"
        )
    return out


def _reads(path: Path, ids: list[str]) -> dict[str, Any]:
    """Everything a reader sees: details, a list page with previews, every export."""
    store = _Reader(path, max_rows=0)
    try:
        return {
            "details": {
                request_id: store.get_request(request_id) for request_id in ids
            },
            "list": store.list_requests(limit=50, offset=0, body_preview_chars=400),
            "exports": _export_bytes(store),
        }
    finally:
        store.close()


def _filters(**chosen: Any) -> dict[str, Any]:
    filters: dict[str, Any] = dict.fromkeys(_FILTER_NAMES)
    filters.update(chosen)
    return filters


def _searches(path: Path, *, index: bool) -> dict[str, list[tuple[int, float]]]:
    """Every term x filter set, newest first: the scan's own pass, or the planner's."""
    store = _Reader(path, max_rows=0)
    try:
        out: dict[str, list[tuple[int, float]]] = {}
        for q in _TERMS:
            for chosen in _FILTERS:
                key = f"{q!r} {chosen}"
                if index:
                    out[key] = list(store.match_rows(q=q, **_filters(**chosen)))
                else:
                    with closing(store._connect()) as conn:
                        out[key] = list(
                            store._match_rows_pass(conn, q, _filters(**chosen))
                        )
        return out
    finally:
        store.close()


def _copy(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source)) as a, closing(sqlite3.connect(target)) as b:
        a.backup(b)
    index = rs.search_index_path(source)
    if index.exists():
        with (
            closing(sqlite3.connect(index)) as a,
            closing(sqlite3.connect(rs.search_index_path(target))) as b,
        ):
            a.backup(b)


# ------------------------------------------------------------------ lossless


@pytest.mark.parametrize("indexed", [True, False], ids=["indexed", "not-indexed"])
def test_every_prompt_is_stored_as_chunks_and_reads_back_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, indexed: bool
) -> None:
    path = tmp_path / "requests.db"
    records = _records(48)
    # Written by this version (each prompt indexed as it was written) or by
    # one with no search index (the conversion indexes them itself).
    _write(path, monkeypatch, records, search_index=indexed)
    before = _raws(path)
    ids = [record.id for record in records]
    reads_before = _reads(path, ids)
    assert not _chunked(path)

    state = _convert(path)

    assert _raws(path) == before
    prompts = _prompt_shas(path)
    chunked = _chunked(path)
    assert set(chunked) == prompts
    assert {before[sha] for sha in prompts} == {_prompt_bytes(r) for r in records}
    chunks = state["chunks"]
    assert chunks["converted"] == len(prompts)
    assert chunks["failed"] == 0
    assert chunks["bytes_after"] < chunks["bytes_before"]
    # An older version decompresses the stored bytes as a zstd frame: they
    # are not one, so it shows the prompt as empty (and deletes nothing).
    for envelope in chunked.values():
        assert envelope[0] == rl._CHUNK_ENVELOPE_V1
        with pytest.raises(zstd.ZstdError):
            zstd.decompress(envelope)
    assert _reads(path, ids) == reads_before


def test_a_prompt_holding_a_lone_surrogate_reads_back_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    records = _records(20, seed=3, odd=_ODDER)
    assert any("\ud800" in (record.input_text or "") for record in records)
    assert any("\x01" in (record.input_text or "") for record in records)
    _write(path, monkeypatch, records)
    before = _raws(path)
    store = _Reader(path, max_rows=0)
    try:
        details = {r.id: store.get_request(r.id) for r in records}
    finally:
        store.close()

    state = _convert(path)

    assert state["chunks"]["converted"] == len(records)
    assert state["chunks"]["failed"] == 0
    assert _raws(path) == before
    store = _Reader(path, max_rows=0)
    try:
        assert {r.id: store.get_request(r.id) for r in records} == details
    finally:
        store.close()


def test_a_turn_reuses_the_chunks_of_the_turn_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(40))
    _convert(path)
    named: list[int] = []
    for envelope in _chunked(path).values():
        chunk_ids, _check = rl._read_chunk_envelope(envelope)
        named.extend(chunk_ids)
    # Forty growing prompts of four conversations: most chunk references are
    # repeats of a chunk an earlier turn already stored.
    assert len(set(named)) * 3 < len(named)


def test_search_answers_exactly_as_before_on_a_converted_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.db"
    converted = tmp_path / "converted.db"
    _write(original, monkeypatch, _records(56), search_index=False)
    _copy(original, converted)
    expected = _searches(original, index=False)
    assert any(expected.values())

    _convert(converted)

    # The scan, reading prompts back from their chunks.
    assert _searches(converted, index=False) == expected
    # The planner as shipped: prompts are in the index, replies are not.
    assert _searches(converted, index=True) == expected
    # Every row answered from the index.
    monkeypatch.setattr(rl, "_SEARCH_SCAN_FIRST_ROWS", 0)
    monkeypatch.setattr(rl, "_SEARCH_COST_PASS_ROW", -1.0)
    monkeypatch.setattr(rl, "_SEARCH_SCAN_SECONDS_MIN", -1.0)
    store = RequestLogStore(converted, max_rows=0)
    try:
        store.request_search_index_build()
        _wait(lambda: store.search_index_status()["state"] == "done")
    finally:
        store.close()
    assert _searches(converted, index=True) == expected


# --------------------------------------------------------------- the guards


def test_a_chunk_list_that_does_not_give_back_the_stored_bytes_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(30))
    before = _raws(path)
    with closing(sqlite3.connect(path)) as conn:
        stored_before = {
            str(sha): (dict_id, bytes(payload))
            for sha, dict_id, payload in conn.execute(
                "SELECT sha, dict_id, payload FROM body_blobs"
            )
        }
    real = rl._chunk_envelope
    spoiled: list[bytes] = []

    def spoil(chunk_ids: Any, text: bytes) -> bytes:
        # Every other prompt: a chunk list that misses its last chunk (with
        # check bytes made for what it does give back).
        if len(spoiled) % 2 == 0 and len(chunk_ids) > 1:
            spoiled.append(text)
            short = list(chunk_ids)[:-1]
            reader = rs.ChunkReader(rs.search_index_path(path))
            try:
                texts = reader.texts(short, cached=False)
            finally:
                reader.close()
            assert texts is not None
            wrong = b"".join(texts)
            assert wrong != text
            return real(short, wrong)
        spoiled.append(b"")
        return real(chunk_ids, text)

    monkeypatch.setattr(rl, "_chunk_envelope", spoil)
    state = _convert(path)

    bad = {
        hashlib.sha256(packed).hexdigest()
        for text in spoiled
        if text and (packed := rl._packed_prompt(text)) is not None
    }
    assert bad
    assert state["chunks"]["failed"] == len(bad)
    assert _raws(path) == before
    chunked = _chunked(path)
    with closing(sqlite3.connect(path)) as conn:
        for sha in bad:
            assert sha not in chunked
            row = conn.execute(
                "SELECT dict_id, payload FROM body_blobs WHERE sha = ?", (sha,)
            ).fetchone()
            assert (row[0], bytes(row[1])) == stored_before[sha]


def test_only_a_prompt_blob_exactly_as_the_writer_packs_it_is_converted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    records = _records(12)
    records.append(
        RequestRecord(
            id="rec-empty",
            ts_epoch=2000.0,
            endpoint="/v1/messages",
            protocol="anthropic",
            status="success",
            input_text="",
            output_text="an answer to nothing",
        )
    )
    _write(path, monkeypatch, records)
    # A prompt blob in a form ``pack_fields`` never writes (spaces after the
    # separators), under its own address, as an older tool might have left.
    odd_raw = json.dumps({"i": "odd prompt\n" * 200}).encode()
    odd_sha = hashlib.sha256(odd_raw).hexdigest()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO body_blobs (sha, dict_id, payload) VALUES (?, NULL, ?)",
            (odd_sha, zstd.compress(odd_raw)),
        )
        conn.execute(
            "UPDATE request_bodies SET input_sha = ? WHERE request_id = 'rec-00003'",
            (odd_sha,),
        )
    before = _raws(path)

    state = _convert(path)

    chunked = _chunked(path)
    assert odd_sha not in chunked
    empty = _prompt_bytes(records[-1])
    assert hashlib.sha256(empty).hexdigest() not in chunked
    assert state["chunks"]["kept"] >= 2
    assert state["chunks"]["failed"] == 0
    assert _raws(path) == before


def test_a_step_that_dies_half_way_changes_nothing_and_the_walk_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(40))
    before = _raws(path)
    real = RequestLogStore._chunked_text
    calls: list[int] = []
    died = threading.Event()

    def dying(self: RequestLogStore, envelope: bytes, **kw: Any) -> bytes | None:
        if kw.get("local") is not None:
            calls.append(1)
            if len(calls) == 7:
                died.set()
                raise sqlite3.OperationalError("disk I/O error (simulated)")
        return real(self, envelope, **kw)

    monkeypatch.setattr(RequestLogStore, "_chunked_text", dying)
    store = RequestLogStore(path, max_rows=0)
    try:
        assert died.wait(30)
        time.sleep(0.3)
        # The whole step rolled back: no row, and no bookkeeping, moved.
        assert not _chunked(path)
        assert int((_state(path) or {}).get("chunks", {}).get("through") or 0) == 0
        assert _raws(path) == before
        # The next try (after the pause a failed step takes) resumes.
        store._history_retry_at = 0.0
        _wait(lambda: _chunks_caught_up(path))
    finally:
        store.close()
    state = _state(path)
    assert state is not None
    assert state["chunks"]["failed"] == 0
    assert set(_chunked(path)) == _prompt_shas(path)
    assert _raws(path) == before


def test_prompts_written_after_the_history_are_stored_as_chunks_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    first = _records(16)
    _write(path, monkeypatch, first)
    store = RequestLogStore(path, max_rows=0)
    try:
        _wait(lambda: _chunks_caught_up(path))
        later = _records(16, seed=9, start=100)
        for record in later:
            store.enqueue(record)
        _wait(lambda: len(_chunked(path)) == len(_prompt_shas(path)) == 32)
    finally:
        store.close()
    raws = _raws(path)
    for record in first + later:
        packed = _prompt_bytes(record)
        assert raws[hashlib.sha256(packed).hexdigest()] == packed


def test_a_search_running_holds_the_chunk_part_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(20))
    store = RequestLogStore(path, max_rows=0)
    store._search_enter()
    try:
        time.sleep(1.0)
        assert not _chunked(path)
    finally:
        store._search_leave()
    try:
        _wait(lambda: _chunks_caught_up(path))
    finally:
        store.close()
    assert set(_chunked(path)) == _prompt_shas(path)


def test_pause_holds_across_a_restart_and_continue_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(20))
    # Paused in an earlier run.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO request_log_meta (key, value) VALUES (?, '1')",
            (rl._HISTORY_PAUSED_KEY,),
        )
    store = RequestLogStore(path, max_rows=0)
    try:
        assert store._dictionaries_checked.wait(30)
        time.sleep(1.0)
        assert not _chunked(path)
        assert store.history_conversion_status()["paused"] is True
        status = store.continue_history_conversion()
        assert status["paused"] is False
        _wait(lambda: _chunks_caught_up(path))
        paused = store.pause_history_conversion()
        assert paused["paused"] is True
        assert paused["chunk_store"]["prompts"] == 20
        assert paused["chunk_store"]["failed"] == 0
        assert paused["chunk_store"]["index_bytes"] > 0
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM request_log_meta WHERE key = ?",
                (rl._HISTORY_PAUSED_KEY,),
            ).fetchone()
            is not None
        )


def test_without_a_search_index_the_chunk_part_closes_and_converts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(10), search_index=False)
    before = _raws(path)
    store = RequestLogStore(path, max_rows=0, search_index=False)
    try:
        _wait(lambda: (_state(path) or {}).get("done_at") is not None)
    finally:
        store.close()
    state = _state(path)
    assert state is not None
    assert state["chunks"]["skipped"]
    assert state["chunks"]["converted"] == 0
    assert not _chunked(path)
    assert _raws(path) == before


# ------------------------------------------------------- space (IV.13)


def test_pages_free_before_the_chunk_part_are_never_handed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_prompt_chunk_store: None
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(60))
    # An earlier release's conversion, finished, without the chunk part.
    store = RequestLogStore(path, max_rows=0)
    try:
        _wait(lambda: (_state(path) or {}).get("done_at") is not None)
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        state = json.loads(
            conn.execute(
                "SELECT value FROM request_log_meta WHERE key = ?", (_KEY,)
            ).fetchone()[0]
        )
        del state["chunks"]
        conn.execute(
            "UPDATE request_log_meta SET value = ? WHERE key = ?",
            (json.dumps(state), _KEY),
        )
        # Rows somebody deleted: their pages sit on the freelist, where a
        # recovery could still find them.
        conn.execute("CREATE TABLE somebody_elses (value BLOB)")
        conn.executemany(
            "INSERT INTO somebody_elses (value) VALUES (?)",
            [(bytes([n]) * 5000,) for n in range(40)],
        )
        conn.execute("DELETE FROM somebody_elses")
    with closing(sqlite3.connect(path)) as conn:
        free_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert free_before > 0
    monkeypatch.undo()

    state = _convert(path)

    with closing(sqlite3.connect(path)) as conn:
        free_after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert state["reopened_for"] == ["chunks"]
    assert state["freelist_at_start"] == free_before
    assert state["chunks"]["converted"] > 0
    # The conversion freed pages of its own and handed those back ...
    assert state["returned_pages"] > state["returned_at_reopen"]
    # ... and never one of the pages that were free before it began.
    assert free_after >= free_before


# ------------------------------------------------------- mark and sweep


def test_the_mark_keeps_chunks_only_the_log_still_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(30))
    _convert(path)
    before = _raws(path)
    # The index lost every entry (a clear whose log erase then failed): only
    # the log's chunk lists name the chunks now.
    index = rs.search_index_path(path)
    with closing(sqlite3.connect(index)) as conn, conn:
        for table in ("blobs", "units", "links"):
            conn.execute(f"DELETE FROM {table}")
    with closing(sqlite3.connect(index)) as conn:
        chunks_before = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    monkeypatch.setattr(rl, "_CHUNK_SWEEP_INTERVAL_SECONDS", 0.0)
    store = RequestLogStore(path, max_rows=0)

    def swept() -> bool:
        marker = store._chunk_marker
        return (
            marker is not None and not marker.is_alive() and store._chunk_mark is None
        )

    try:
        # The mark runs once per process start; wait until it has swept.
        _wait(swept)
    finally:
        store.close()
    with closing(sqlite3.connect(index)) as conn:
        chunks_after = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    # The sweep ran: the replies' chunks, which nothing names now, are gone ...
    assert chunks_after < chunks_before
    # ... and every chunk a prompt's chunk list names is still there.
    assert _raws(path) == before


def test_a_prune_sweeps_exactly_the_chunks_nothing_names_any_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(40))
    _convert(path)
    monkeypatch.setattr(rl, "_CHUNK_SWEEP_INTERVAL_SECONDS", 0.0)
    store = RequestLogStore(path, max_rows=12)
    try:
        assert store.prune() == 28
        _wait(lambda: len(_prompt_shas(path)) <= 12)
        _wait(lambda: _named_and_stored(path)[1] - _named_and_stored(path)[0] <= 1)
    finally:
        store.close()
    named, stored = _named_and_stored(path)
    # Every chunk a manifest names is there; the only other one kept is the
    # highest id, which is never deleted.
    assert stored - named in (0, 1)
    raws = _raws(path)
    assert all(raw is not None for raw in raws.values())
    for sha, raw in raws.items():
        assert raw is not None and hashlib.sha256(raw).hexdigest() == sha


def _named_and_stored(path: Path) -> tuple[int, int]:
    """Chunks a manifest names (index units and log chunk lists), chunks stored."""
    named: set[int] = set()
    for envelope in _chunked(path).values():
        named.update(rl._read_chunk_envelope(envelope)[0])
    with closing(sqlite3.connect(rs.search_index_path(path))) as conn:
        for (manifest,) in conn.execute("SELECT manifest FROM units"):
            named.update(rs.manifest_ids(bytes(manifest)))
        stored = {int(row[0]) for row in conn.execute("SELECT id FROM chunks")}
    assert named <= stored
    return len(named), len(stored)


def test_the_sweep_never_deletes_the_highest_chunk_id_or_one_with_a_link(
    tmp_path: Path,
) -> None:
    index = rs.SearchIndex(tmp_path / "requests.db")
    conn = index.open_writer()
    assert conn is not None
    try:
        blobs = [
            rs.BlobUnits(
                sha=hashlib.sha256(f"blob {n}".encode()).digest(),
                fields=1,
                units={0: f"text {n}\n" * (300 + n)},
            )
            for n in range(3)
        ]
        with conn:
            index.add(conn, blobs)
        top = int(conn.execute("SELECT MAX(id) FROM chunks").fetchone()[0])
        keys = [int(row[0]) for row in conn.execute("SELECT key FROM blobs")]
        with conn:
            index.remove(conn, keys[:2])
        # Removing blobs deletes no chunk (7.93.0): only the sweep does.
        assert int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]) == top
        linked = {int(row[0]) for row in conn.execute("SELECT chunk FROM links")}
        conn.execute("BEGIN IMMEDIATE")
        deleted, through = index.sweep_chunks(
            conn, rs.chunk_marks(top), top, 0, time.perf_counter() + 30
        )
        conn.commit()
        left = {int(row[0]) for row in conn.execute("SELECT id FROM chunks")}
    finally:
        conn.close()
    assert through == top
    assert left == linked | {top}
    assert deleted == top - len(linked | {top})


# --------------------------------------------------------------- clear


def test_a_clear_whose_log_erase_fails_keeps_every_prompt_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(20))
    _convert(path)
    before = _raws(path)
    store = RequestLogStore(path, max_rows=0)
    real = store._connection
    calls: list[int] = []

    def failing() -> Any:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked (simulated)")

    try:
        monkeypatch.setattr(store, "_connection", failing)
        with pytest.raises(sqlite3.OperationalError):
            store.clear()
        assert calls
        monkeypatch.setattr(store, "_connection", real)
    finally:
        store.close()
    assert _raws(path) == before


def test_a_clear_erases_the_chunks_with_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(10))
    _convert(path)
    store = RequestLogStore(path, max_rows=0)
    try:
        store.clear()
    finally:
        store.close()
    with closing(sqlite3.connect(rs.search_index_path(path))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert not _chunked(path)


def test_after_a_clear_new_prompts_are_stored_as_chunks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(24))
    store = RequestLogStore(path, max_rows=0)
    try:
        _wait(lambda: _chunks_caught_up(path))
        store.clear()
        # Rowids start again from 1, far below where the walk had got.
        later = _records(6, seed=21, start=300)
        for record in later:
            store.enqueue(record)
        _wait(lambda: len(_chunked(path)) == len(_prompt_shas(path)) == 6)
    finally:
        store.close()
    raws = _raws(path)
    for record in later:
        packed = _prompt_bytes(record)
        assert raws[hashlib.sha256(packed).hexdigest()] == packed


def test_a_scan_reads_chunks_through_its_own_cache_never_the_shared_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planner times a scan's first rows to price the rest (7.92.0); a
    cache earlier searches warmed would make those rows look free."""
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(24))
    _convert(path)
    store = _Reader(path, max_rows=0)
    try:
        with closing(store._connect()) as conn:
            matched = list(store._match_rows_pass(conn, "turn", _filters(local="all")))
        assert matched
        assert len(store._chunk_reader._cache) == 0
        # The shared cache still serves the detail, list and export.
        assert store._decode_bodies(next(iter(_chunked(path).values())), -1)
        assert len(store._chunk_reader._cache) > 0
    finally:
        store.close()


# --------------------------------------------------------------- reader


def test_a_cached_chunk_that_no_longer_matches_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, _records(8))
    _convert(path)
    before = _raws(path)
    store = _Reader(path, max_rows=0)
    try:
        envelope = next(iter(_chunked(path).values()))
        chunk_ids, _check = rl._read_chunk_envelope(envelope)
        raw = store._raw_payload(envelope, rl._CHUNKED_DICT_ID)
        assert raw is not None
        text = store._chunked_text(envelope)
        assert text is not None
        bodies = store._decode_bodies(envelope, rl._CHUNKED_DICT_ID)
        assert bodies == rl.unpack_bodies(raw)
        # A stale text under the same id (an id an older version freed and
        # SQLite handed out again).
        reader = store._chunk_reader
        with reader._lock:
            reader._cache[chunk_ids[0]] = b"stale"
        assert store._raw_payload(envelope, rl._CHUNKED_DICT_ID) == raw
        with reader._lock:
            reader._cache[chunk_ids[0]] = b"stale"
        assert store._decode_bodies(envelope, rl._CHUNKED_DICT_ID) == bodies
        # A chunk list naming a chunk that is gone reads as empty, as any
        # undecodable blob always has.
        assert (
            store._raw_payload(
                rl._chunk_envelope([*chunk_ids, 10**9], text), rl._CHUNKED_DICT_ID
            )
            is None
        )
        assert store._decode_bodies(b"\xc1\x05", rl._CHUNKED_DICT_ID) == {}
    finally:
        store.close()
    assert _raws(path) == before
