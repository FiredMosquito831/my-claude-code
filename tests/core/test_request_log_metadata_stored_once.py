"""Repeated request metadata stored once (7.75.0).

``requests.headers`` / ``route_chain`` / ``params`` repeat a few hundred
distinct values across hundreds of thousands of rows. New rows name each value
by the id of its single ``request_values`` row; the background history
conversion does the same for rows an older version wrote, moving each row so
the pages it frees can go back to the disk. Every reader returns exactly the
text an inline row returns, and the ref never reaches a caller.
"""

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
from my_claude_code.core.request_log import RequestLogStore, RequestRecord

_TS = 1_790_000_000.0
_KEY = request_log_module._HISTORY_CONVERSION_KEY
_REFS = ("headers_ref", "route_chain_ref", "params_ref")
_UA_CLAUDE = "claude-cli/2.1.258 (external, cli)"

HEADERS: list[dict[str, str] | None] = [
    None,
    {},
    {"user-agent": _UA_CLAUDE, "anthropic-version": "2023-06-01"},
    {"user-agent": "opencode/1.0", "x-note": "ș ă — → … ✓ 𝄞 中文"},
    {"user-agent": "codex_cli_rs/0.40.0", "padding": "x" * 900},
]
ROUTE_CHAINS: list[str | None] = [
    None,
    "",
    "nvidia_nim/test-model",
    "zen/a > zen/b > nvidia_nim/c",
    "ș → ✓",
]
PARAMS: list[dict[str, Any] | None] = [
    None,
    {},
    {"tools_count": 12, "max_tokens": 16384},
    {"note": "ș ă", "temperature": 0.5, "stream": True},
]


def _record(index: int) -> RequestRecord:
    headers = HEADERS[index % len(HEADERS)]
    if index % 17 == 5:
        # A value only this row carries.
        headers = {"user-agent": f"unique-client/{index}"}
    return RequestRecord(
        id=f"m{index:05d}",
        ts_epoch=_TS + index,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        stream=bool(index % 2),
        input_text=f"Prompt {index}: why does test_{index} fail?",
        output_text=f"Reply {index}.",
        tokens_in=10 + index,
        tokens_out=20,
        duration_ms=120.0,
        status="success",
        headers=headers,
        route_chain=ROUTE_CHAINS[(index // len(HEADERS)) % len(ROUTE_CHAINS)],
        params=PARAMS[(index // 3) % len(PARAMS)],
    )


@pytest.fixture
def no_background_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer leaves history alone; a test drives the steps itself."""
    monkeypatch.setattr(
        RequestLogStore, "_run_history_conversion", lambda self, conn: None
    )


def _write(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    *,
    inline: bool = False,
    compress_bodies: bool = True,
) -> list[str]:
    """Write ``count`` records; ``inline`` writes them as an older version did."""
    with monkeypatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        patch.setattr(RequestLogStore, "_maybe_refresh_dictionaries", lambda self: None)
        if inline:
            patch.setattr(
                RequestLogStore, "_store_request_values", lambda self, conn, rows: rows
            )
        store = RequestLogStore(path, max_rows=0, compress_bodies=compress_bodies)
        for index in range(count):
            store.enqueue(_record(index))
        store.close()
    return [f"m{index:05d}" for index in range(count)]


def _raw(path: Path) -> dict[str, tuple[Any, ...]]:
    """Each row's rowid, three columns as stored (class + bytes), and refs."""
    with closing(sqlite3.connect(path)) as conn:
        return {
            str(row[0]): tuple(row[1:])
            for row in conn.execute(
                "SELECT id, rowid,"
                " typeof(headers), CAST(headers AS BLOB),"
                " typeof(route_chain), CAST(route_chain AS BLOB),"
                " typeof(params), CAST(params AS BLOB),"
                " headers_ref, route_chain_ref, params_ref FROM requests"
            )
        }


def _resolved(path: Path) -> dict[str, tuple[Any, ...]]:
    """Each row's three values as text, wherever they are stored."""
    with closing(sqlite3.connect(path)) as conn:
        values = {
            int(row[0]): bytes(row[1])
            for row in conn.execute(
                "SELECT id, CAST(value AS BLOB) FROM request_values"
            )
        }
        out: dict[str, tuple[Any, ...]] = {}
        for row in conn.execute(
            "SELECT id, CAST(headers AS BLOB), headers_ref,"
            " CAST(route_chain AS BLOB), route_chain_ref,"
            " CAST(params AS BLOB), params_ref FROM requests"
        ):
            resolved = []
            for inline, ref in ((row[1], row[2]), (row[3], row[4]), (row[5], row[6])):
                assert inline is None or ref is None, "a ref and its column both set"
                resolved.append(values[int(ref)] if ref is not None else inline)
            out[str(row[0])] = tuple(resolved)
        return out


def _state(path: Path) -> dict[str, Any] | None:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (_KEY,)
        ).fetchone()
    return None if row is None else json.loads(row[0])


def _finished_7740_document(path: Path, *, returned: int = 5) -> None:
    """The document 7.74.0 leaves once it is done: no ``metadata`` part."""
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
        "freed_pages": returned,
        "returned_pages": returned,
        "done_at": _TS + 60,
        "bytes_at_end": 4096 * 300,
    }
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            (_KEY, json.dumps(document, sort_keys=True)),
        )


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
    """Until the document is done *with* its metadata part: an earlier
    release's finished document is done before this one reopens it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(path)
        if (
            state is not None
            and state.get("done_at") is not None
            and (state.get("metadata") or {}).get("done_at") is not None
        ):
            return state
        time.sleep(0.05)
    raise AssertionError(f"the conversion never finished: {_state(path)}")


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
    # The zip itself carries its write time; the sheets are the content.
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
    """Everything a reader can see of the rows: detail, list, every export."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        store = RequestLogStore(path, max_rows=0)
        try:
            details = {request_id: store.get_request(request_id) for request_id in ids}
            pages = [
                store.list_requests_page(limit=50, offset=offset)
                for offset in range(0, len(ids) + 50, 50)
            ]
            exports = _export_bytes(store)
            stats = store.stats()
            lifetime = store.lifetime()
        finally:
            store.close()
    return {
        "details": details,
        "pages": pages,
        "exports": exports,
        "stats": stats,
        "lifetime": lifetime,
    }


# ---------------------------------------------------------------- new rows


def test_new_rows_store_each_value_once_and_read_back_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inline_path = tmp_path / "inline.db"
    once_path = tmp_path / "once.db"
    ids = _write(inline_path, monkeypatch, 120, inline=True)
    _write(once_path, monkeypatch, 120)

    raw = _raw(once_path)
    for request_id, row in raw.items():
        classes = (row[1], row[3], row[5])
        # Nothing is left inline that could have been stored once.
        assert set(classes) == {"null"}, (request_id, classes)
    assert _resolved(once_path) == _resolved(inline_path)
    with closing(sqlite3.connect(once_path)) as conn:
        stored = [
            bytes(row[0])
            for row in conn.execute("SELECT CAST(value AS BLOB) FROM request_values")
        ]
    # Each distinct value once.
    assert len(stored) == len(set(stored))
    distinct = {
        value
        for row in _resolved(inline_path).values()
        for value in row
        if value is not None
    }
    assert set(stored) == distinct

    # Detail, list, every export format, stats: byte for byte the same.
    assert _reads(once_path, ids) == _reads(inline_path, ids)
    detail = _reads(once_path, ids[:1])["details"][ids[0]]
    assert not any(ref in detail for ref in _REFS)


def test_null_stays_null_and_an_empty_value_stays_an_empty_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 60)
    raw = _raw(path)
    resolved = _resolved(path)
    for index, request_id in enumerate(ids):
        record = _record(index)
        headers, route_chain, params = resolved[request_id]
        if not record.headers:
            # The writer stores no headers as NULL, never as "{}".
            assert headers is None and raw[request_id][7] is None
        if record.route_chain is None:
            assert route_chain is None and raw[request_id][8] is None
        elif record.route_chain == "":
            assert route_chain == b""
        if record.params is None:
            assert params is None and raw[request_id][9] is None
        elif record.params == {}:
            assert params == b"{}"
    store = RequestLogStore(path, max_rows=0)
    by_shape: dict[str, dict[str, Any]] = {}
    try:
        for name, request_id in (
            ("null_params", ids[0]),
            ("empty_params", ids[3]),
            ("empty_chain", ids[5]),
        ):
            detail = store.get_request(request_id)
            assert detail is not None
            by_shape[name] = detail
    finally:
        store.close()
    assert by_shape["null_params"]["params"] is None
    assert by_shape["empty_params"]["params"] == {}
    assert by_shape["empty_chain"]["route_chain"] == ""


def test_with_compression_off_new_rows_keep_their_metadata_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = tmp_path / "off.db"
    inline = tmp_path / "inline.db"
    ids = _write(off, monkeypatch, 40, compress_bodies=False)
    _write(inline, monkeypatch, 40, inline=True, compress_bodies=False)
    raw = _raw(off)
    assert all(row[7:] == (None, None, None) for row in raw.values())
    with closing(sqlite3.connect(off)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_values").fetchone()[0] == 0
    assert _resolved(off) == _resolved(inline)
    assert _reads(off, ids)["details"] == _reads(inline, ids)["details"]


def test_a_value_that_does_not_read_back_stays_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    inline = tmp_path / "inline.db"
    real = request_log_module._load_request_values

    def wrong(conn: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
        return {value_id: text + " " for value_id, text in real(conn, ids).items()}

    with monkeypatch.context() as patch:
        patch.setattr(request_log_module, "_load_request_values", wrong)
        ids = _write(path, monkeypatch, 40)
    _write(inline, monkeypatch, 40, inline=True)
    assert all(row[7:] == (None, None, None) for row in _raw(path).values())
    assert _raw(path) == _raw(inline)
    assert _reads(path, ids)["details"] == _reads(inline, ids)["details"]


def test_the_harness_backfill_reads_headers_stored_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 30)
    claude = [i for i in range(len(ids)) if _record(i).headers == HEADERS[2]]
    assert claude
    raw = _raw(path)
    # Their headers are stored once: nothing inline, a ref to read.
    assert all(raw[ids[i]][1] == "null" and raw[ids[i]][7] for i in claude)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE requests SET harness = NULL")
        conn.execute(
            "DELETE FROM request_log_meta WHERE key = ?",
            (request_log_module._HARNESS_BACKFILL_KEY,),
        )
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    store.close()
    with closing(sqlite3.connect(path)) as conn:
        harness = dict(conn.execute("SELECT id, harness FROM requests").fetchall())
    assert {harness[ids[i]] for i in claude} == {"claude"}


# ------------------------------------------------------------ prune, clear


def test_the_sweep_deletes_only_values_no_row_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 80)
    keep = ids[40:]
    before = _reads(path, keep)["details"]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM requests WHERE id < ?", (keep[0],))
        named_before = {
            int(value)
            for row in conn.execute(f"SELECT {', '.join(_REFS)} FROM requests")
            for value in row
            if value is not None
        }
        total_before = conn.execute("SELECT COUNT(*) FROM request_values").fetchone()[0]
    assert total_before > len(named_before)
    with closing(sqlite3.connect(path)) as conn, conn:
        RequestLogStore._sweep_request_values(conn)
        left = {int(row[0]) for row in conn.execute("SELECT id FROM request_values")}
    assert left == named_before
    assert _reads(path, keep)["details"] == before


def test_prune_at_the_cap_sweeps_values_with_the_tool_catalogues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 60)
    newest = ids[-20:]
    before = _reads(path, newest)["details"]
    with closing(sqlite3.connect(path)) as conn:
        values_before = conn.execute("SELECT COUNT(*) FROM request_values").fetchone()[
            0
        ]
    store = RequestLogStore(path, max_rows=20)
    try:
        assert store._dictionaries_checked.wait(30)
        store.prune()
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 20
        named = {
            int(value)
            for row in conn.execute(f"SELECT {', '.join(_REFS)} FROM requests")
            for value in row
            if value is not None
        }
        left = {int(row[0]) for row in conn.execute("SELECT id FROM request_values")}
    # The values only the 40 deleted rows named (a unique client among them)
    # went with them; every value a kept row names is still there.
    assert left == named
    assert len(left) < values_before
    assert _reads(path, newest)["details"] == before


def test_clear_deletes_every_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, 20)
    store = RequestLogStore(path, max_rows=0)
    try:
        store.clear()
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM request_values").fetchone()[0] == 0


# ------------------------------------------------------------------ history


def test_history_stores_inline_metadata_once_moves_rows_and_frees_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 600, inline=True)
    _finished_7740_document(path)
    raw_before = _raw(path)
    resolved_before = _resolved(path)
    reads_before = _reads(path, ids)
    with closing(sqlite3.connect(path)) as conn:
        pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
        top_before = conn.execute("SELECT MAX(rowid) FROM requests").fetchone()[0]

    store = RequestLogStore(path, max_rows=0)
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None and state["done_at"] is not None

    # Lossless, through every reader.
    assert _resolved(path) == resolved_before
    assert _reads(path, ids) == reads_before
    raw_after = _raw(path)
    inline_rows = [
        rid for rid, row in raw_before.items() if {row[1], row[3], row[5]} != {"null"}
    ]
    metadata = state["metadata"]
    assert metadata["converted"] == len(inline_rows) > 0
    assert metadata["failed"] == 0
    assert metadata["converted"] + metadata["kept"] == len(ids)
    assert metadata["bytes_before"] == sum(
        len(raw_before[rid][i])
        for rid in inline_rows
        for i in (2, 4, 6)
        if raw_before[rid][i] is not None
    )
    for rid in inline_rows:
        # Nothing inline any more, and the row moved past the old end ...
        assert {raw_after[rid][1], raw_after[rid][3], raw_after[rid][5]} == {"null"}
        assert raw_after[rid][0] > top_before
    # ... in the order it had.
    moved_order = sorted(inline_rows, key=lambda rid: raw_after[rid][0])
    assert moved_order == sorted(inline_rows, key=lambda rid: raw_before[rid][0])
    for rid in set(ids) - set(inline_rows):
        assert raw_after[rid] == raw_before[rid]
    # The pages the moves freed went back to the disk.
    with closing(sqlite3.connect(path)) as conn:
        pages_after = conn.execute("PRAGMA page_count").fetchone()[0]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert state["freed_pages"] - state["returned_at_reopen"] > 0
    assert state["returned_pages"] > state["returned_at_reopen"]
    assert pages_after < pages_before


def test_pages_free_before_the_reopen_are_never_handed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    """IV.13: pages already free when the document reopens may hold a row
    somebody deleted. Only what this run freed goes back, whatever the earlier
    release's document still counted as owed."""
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, 400, inline=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE somebody_else (payload BLOB)")
        conn.executemany(
            "INSERT INTO somebody_else VALUES (?)", [(b"x" * 3000,) for _ in range(200)]
        )
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TABLE somebody_else")
    with closing(sqlite3.connect(path)) as conn:
        free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    assert free_before >= 150
    _finished_7740_document(path, returned=5)
    with closing(sqlite3.connect(path)) as conn, conn:
        # The earlier release finished with pages it freed still counted as
        # owed (new rows had reused them, so none was free to hand back).
        document = json.loads(
            conn.execute(
                "SELECT value FROM request_log_meta WHERE key = ?", (_KEY,)
            ).fetchone()[0]
        )
        document["freed_pages"] = 100_000
        conn.execute(
            "UPDATE request_log_meta SET value = ? WHERE key = ?",
            (json.dumps(document, sort_keys=True), _KEY),
        )

    # Moved rows may land on any free page, as every insert may; what must
    # never happen is a free page from before the reopen going to the disk.
    # So right after every step that handed pages back, the freelist still
    # holds at least what was free when the document reopened.
    after_each_return: list[int] = []
    real_return = RequestLogStore._return_space_step

    def traced(self: RequestLogStore, conn: sqlite3.Connection) -> bool:
        returned = real_return(self, conn)
        if returned:
            after_each_return.append(
                int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            )
        return returned

    monkeypatch.setattr(RequestLogStore, "_return_space_step", traced)
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    with closing(sqlite3.connect(path)) as conn:
        free_at_reopen = conn.execute("PRAGMA freelist_count").fetchone()[0]
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None and state["done_at"] is not None
    # Measured afresh at the reopen, not the earlier release's figure.
    assert state["freelist_at_start"] == free_at_reopen > 0
    assert state["returned_pages"] > state["returned_at_reopen"]
    assert after_each_return
    assert min(after_each_return) >= free_at_reopen


def test_a_finished_7740_document_is_reopened_for_the_metadata_part_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, 50, inline=True)
    _finished_7740_document(path, returned=5)
    earlier = _state(path)
    assert earlier is not None

    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()

    assert state["reopened_for"] == ["metadata"]
    assert state["returned_at_reopen"] == 5
    assert state["reopened_at"] > earlier["done_at"]
    assert state["done_at"] >= state["reopened_at"]
    # The earlier release's parts are as it left them.
    assert state["wire"] == earlier["wire"]
    assert state["bodies"] == earlier["bodies"]
    assert state["started_at"] == earlier["started_at"]
    assert state["metadata"]["done_at"] is not None
    assert state["metadata"]["converted"] > 0
    # A second start finds nothing left: the document stays done.
    again = RequestLogStore(path, max_rows=0)
    assert again._dictionaries_checked.wait(30)
    time.sleep(0.5)
    again.close()
    assert _state(path) == state


def test_a_fresh_conversion_runs_every_part_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 40, inline=True)
    reads_before = _reads(path, ids)
    store = RequestLogStore(path, max_rows=0)
    state = _wait_done(path)
    store.close()
    assert list(request_log_module._HISTORY_PHASES) == ["wire", "bodies", "metadata"]
    done = [state[name]["done_at"] for name in request_log_module._HISTORY_PHASES]
    assert all(at is not None for at in done)
    assert done == sorted(done)
    assert "reopened_for" not in state
    assert state["metadata"]["converted"] > 0
    assert _reads(path, ids) == reads_before


def test_stopped_after_k_steps_it_resumes_and_matches_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    whole = tmp_path / "whole.db"
    parted = tmp_path / "parted.db"
    _write(whole, monkeypatch, 45, inline=True)
    _finished_7740_document(whole)
    with (
        closing(sqlite3.connect(whole)) as source,
        closing(sqlite3.connect(parted)) as copy,
    ):
        source.backup(copy)
    resolved_before = _resolved(whole)

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
        marks.append(state["metadata"]["through"])
        if state.get("done_at") is not None:
            break
        assert len(marks) < 2_000

    assert marks == sorted(marks)
    one, many = _state(whole), _state(parted)
    assert one is not None and many is not None
    for counter in ("converted", "kept", "failed", "bytes_before", "bytes_after"):
        assert one["metadata"][counter] == many["metadata"][counter], counter
    with closing(sqlite3.connect(whole)) as a, closing(sqlite3.connect(parted)) as b:
        for sql in (
            "SELECT rowid, id, headers, route_chain, params, headers_ref,"
            " route_chain_ref, params_ref FROM requests ORDER BY rowid",
            "SELECT id, digest, value FROM request_values ORDER BY id",
        ):
            assert a.execute(sql).fetchall() == b.execute(sql).fetchall()
    assert _resolved(parted) == resolved_before


def test_a_failure_inside_a_step_rolls_the_whole_step_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, 40, inline=True)
    _finished_7740_document(path)
    raw_before = _raw(path)
    document_before = _state(path)
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
                store._convert_metadata_step(conn)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _raw(path) == raw_before
    assert _state(path) == document_before
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT COUNT(*) FROM request_values").fetchone()[0] == 0
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None and state["done_at"] is not None


def test_a_value_that_would_read_back_differently_leaves_its_rows_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 60, inline=True)
    _finished_7740_document(path)
    raw_before = _raw(path)
    reads_before = _reads(path, ids)
    real = request_log_module._load_request_values
    target = "ș → ✓"

    def wrong(conn: sqlite3.Connection, value_ids: set[int]) -> dict[int, str]:
        return {
            value_id: ("s -> v" if text == target else text)
            for value_id, text in real(conn, value_ids).items()
        }

    store = RequestLogStore(path, max_rows=0)
    with monkeypatch.context() as patch:
        patch.setattr(request_log_module, "_load_request_values", wrong)
        _drive(store)
    store.close()
    state = _state(path)
    assert state is not None and state["done_at"] is not None
    hit = [rid for rid, row in raw_before.items() if row[4] == target.encode()]
    assert hit
    assert state["metadata"]["failed"] == len(hit)
    raw_after = _raw(path)
    for rid in hit:
        # Untouched: same rowid, same stored bytes, no ref.
        assert raw_after[rid] == raw_before[rid]
    assert _reads(path, ids) == reads_before


def test_a_value_that_is_not_utf8_text_leaves_its_row_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    path = tmp_path / "requests.db"
    ids = _write(path, monkeypatch, 30, inline=True)
    _finished_7740_document(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE requests SET params = CAST(params AS BLOB) WHERE id = ?", (ids[6],)
        )
        conn.execute(
            "UPDATE requests SET route_chain = CAST(x'c328ff' AS TEXT) WHERE id = ?",
            (ids[7],),
        )
    raw_before = _raw(path)
    assert raw_before[ids[6]][5] == "blob"
    store = RequestLogStore(path, max_rows=0)
    _drive(store)
    store.close()
    state = _state(path)
    assert state is not None
    assert state["metadata"]["failed"] == 2
    raw_after = _raw(path)
    assert raw_after[ids[6]] == raw_before[ids[6]]
    assert raw_after[ids[7]] == raw_before[ids[7]]


def test_old_and_new_rows_side_by_side_read_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older version's inline rows and this version's rows, in one log."""
    mixed = tmp_path / "mixed.db"
    inline = tmp_path / "inline.db"
    _write(mixed, monkeypatch, 30, inline=True)
    with monkeypatch.context() as patch:
        patch.setattr(
            RequestLogStore, "_run_history_conversion", lambda self, conn: None
        )
        store = RequestLogStore(mixed, max_rows=0)
        for index in range(30, 60):
            store.enqueue(_record(index))
        store.close()
    ids = [f"m{index:05d}" for index in range(60)]
    _write(inline, monkeypatch, 60, inline=True)
    raw = _raw(mixed)
    assert any(row[7:] != (None, None, None) for row in raw.values())
    assert any(row[5] == "text" for row in raw.values())
    assert _reads(mixed, ids) == _reads(inline, ids)


def test_progress_names_the_metadata_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_background_conversion: None
) -> None:
    path = tmp_path / "requests.db"
    _write(path, monkeypatch, 30, inline=True)
    _finished_7740_document(path)
    monkeypatch.setattr(request_log_module, "_HISTORY_STEP_SECONDS", 0.0)
    store = RequestLogStore(path, max_rows=0)
    assert store._dictionaries_checked.wait(30)
    # Still the earlier release's finished document until the writer opens it.
    assert store.storage_footprint()["history"]["state"] == "done"
    conn = store._connect()
    try:
        store._history_step(conn)
        seen = store.storage_footprint()["history"]
        assert seen["state"] == "converting"
        assert seen["phase"] == "metadata"
        assert 0 <= seen["percent"] < 100
        while store._history_step(conn):
            pass
    finally:
        conn.close()
    done = store.storage_footprint()["history"]
    store.close()
    assert done["state"] == "done"


def test_the_rollup_and_the_stats_cache_key_are_unchanged(tmp_path: Path) -> None:
    """Three columns written by every insert, read by no aggregate."""
    added = set(request_log_module._REQUEST_INSERT_COLUMNS) - set(
        request_log_module.required_request_columns()
    )
    assert set(_REFS) <= added
    assert set(_REFS) <= request_log_module._ROLLUP_ACKNOWLEDGED_COLUMNS
    for name in request_log_module._ROLLUP_DIMENSIONS:
        assert name not in _REFS
