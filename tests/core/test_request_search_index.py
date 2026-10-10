"""The request log's search index (7.92.0): exactly the scan's rows, from an index.

Every search the index answers must return exactly the rows the scan of the
stored bodies returns (``_where`` + ``fcc_bodies_match``), in the same order,
however much of the log the index covers. These tests hold that on a log
written through the real writer, for each matching rule the scan has
(INVESTIGATION-REQUEST-LOG-SEARCH.md §5.4), and through every way the index
is written or emptied: at write time, by the build, by the catch-up of bodies
an older version wrote, by retention and by Clear log.
"""

import hashlib
import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import request_log as rl
from my_claude_code.core import request_search as rs
from my_claude_code.core.request_log import (
    _BODY_FIELDS,
    RequestLogStore,
    RequestRecord,
    pack_fields,
    search_units,
    searchable_text,
    unpack_bodies,
)
from tests.support.search_log import CORPUS, build_search_log

FILTER_NAMES = (
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
FILTER_SETS: tuple[dict[str, Any], ...] = (
    {"local": "hide"},
    {"local": "all"},
    {"local": "hide", "status": "error"},
    {"local": "hide", "provider": "zen"},
)
EXTRA_TERMS = (
    "eadm",
    "a.b",
    "a-b",
    "ok",
    "id",
    "日本",
    "naïve",
    "100%",
    "qa",
    "TOOLU_",
    "pytest",
    "app.py",
    "\\",
    "u0103",
    "Ăsta",
    "ĂSTA",
    "too\u3000long",
)


def _filters(**chosen: Any) -> dict[str, Any]:
    filters: dict[str, Any] = dict.fromkeys(FILTER_NAMES)
    filters.update(chosen)
    return filters


def scan_rows(store: RequestLogStore, q: str, **chosen: Any) -> list[tuple[int, float]]:
    """The scan's rows, newest first, as ``match_rows`` read them before 7.92.0."""

    conn = store._connect()
    try:
        return list(store._match_rows_pass(conn, q, _filters(**chosen)))
    finally:
        conn.close()


def index_rows(
    store: RequestLogStore, q: str, **chosen: Any
) -> list[tuple[int, float]]:
    return list(store.match_rows(q=q, **_filters(**chosen)))


@pytest.fixture
def force_index(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Use the index for every row past the first, whatever the planner prices.

    Returns a list that grows by one for every search the index prepared, so a
    test can show the index -- not the scan -- gave the answer.
    """

    monkeypatch.setattr(rl, "_SEARCH_SCAN_FIRST_ROWS", 0)
    monkeypatch.setattr(rl, "_SEARCH_COST_PASS_ROW", -1.0)
    monkeypatch.setattr(rl, "_SEARCH_SCAN_SECONDS_MIN", -1.0)
    prepared: list[int] = []
    original = rs.SearchIndex.prepare

    def counting(
        self: rs.SearchIndex, conn: sqlite3.Connection, plans: Any, **kw: Any
    ) -> Any:
        tables = original(self, conn, plans, **kw)
        prepared.append(len(plans))
        return tables

    monkeypatch.setattr(rs.SearchIndex, "prepare", counting)
    return prepared


def _wait(predicate: Any, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def _index_conn(store: RequestLogStore) -> sqlite3.Connection:
    """The index file, opened without ever creating it."""

    assert store._search is not None
    return sqlite3.connect(store._search.path.as_uri() + "?mode=rw", uri=True)


def _indexed_keys(store: RequestLogStore) -> set[str]:
    """Addresses (hex) of the blobs the index holds; none before the file exists."""

    try:
        conn = _index_conn(store)
    except sqlite3.OperationalError:
        return set()
    try:
        return {bytes(row[0]).hex() for row in conn.execute("SELECT sha FROM blobs")}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def _log_blobs(store: RequestLogStore, below: int | None = None) -> set[str]:
    """Addresses of the blobs the log holds (with a rowid under ``below``)."""

    conn = store._connect()
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT sha FROM body_blobs WHERE rowid < ?",
                (below if below is not None else 2**62,),
            )
        }
    finally:
        conn.close()


def _record(index: int, ts: float, **body: Any) -> RequestRecord:
    return RequestRecord(
        id=f"rec-{index:05d}",
        ts_epoch=ts,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-opus-4-1",
        provider="zen",
        resolved_model="big-pickle",
        status="success",
        **body,
    )


def _write(path: Path, records: list[RequestRecord], **options: Any) -> None:
    store = RequestLogStore(
        path, max_rows=0, queue_max_size=len(records) + 10, **options
    )
    for record in records:
        store.enqueue(record)
    store.close()


def _assert_identical(store: RequestLogStore, terms: Any) -> int:
    checked = 0
    for q in terms:
        for chosen in FILTER_SETS:
            expected = scan_rows(store, q, **chosen)
            assert index_rows(store, q, **chosen) == expected, (q, chosen)
            reads: list[float] = []
            got = list(
                store.match_rows(q=q, **_filters(**chosen), on_read=reads.append)
            )
            assert got == expected, (q, chosen, "on_read")
            checked += 1
    return checked


# ---------------------------------------------------------------- chunking


def test_chunks_end_after_a_newline_and_rejoin_to_the_text() -> None:
    rng = random.Random(3)
    lines = [
        "".join(rng.choice("abcdefgh  →ș") for _ in range(rng.randint(0, 160)))
        for _ in range(4000)
    ]
    data = "\n".join(lines).encode("utf-8")
    chunks = rs.cut_chunks(data)
    assert b"".join(chunks) == data
    assert len(chunks) > 20
    for chunk in chunks[:-1]:
        assert chunk.endswith(b"\n")
        assert len(chunk) >= rs.CHUNK_MIN_BYTES


def test_a_growing_text_keeps_every_chunk_but_its_last() -> None:
    rng = random.Random(5)
    text = "\n".join(f"line {n} {rng.random()}" for n in range(3000)).encode()
    first = rs.cut_chunks(text)
    grown = rs.cut_chunks(text + b"\nmore lines\n" * 400)
    assert grown[: len(first) - 1] == first[:-1]


def test_a_single_long_line_is_never_cut() -> None:
    line = b"x" * (rs.CHUNK_MAX_BYTES * 2)
    assert rs.cut_chunks(line + b"\nend") == [line + b"\n", b"end"]


def test_plan_terms_folds_like_the_scan_and_routes_short_words_to_tests() -> None:
    plans = rs.plan_terms("README readme Abc ab é\x00x Ăsta")
    probes = [plan.probe for plan in plans]
    assert probes == [
        b"readme",
        b"abc",
        b"ab",
        "é\x00x".encode(),
        "Ăsta".encode(),
    ]
    by_probe = {plan.probe: plan for plan in plans}
    assert by_probe[b"abc"].exact
    assert by_probe[b"abc"].grams == ("abc",)
    assert by_probe[b"ab"].grams == ()
    assert by_probe["é\x00x".encode()].grams == ()
    assert not by_probe[b"readme"].exact
    assert by_probe[b"readme"].grams == ("adm", "dme", "ead", "rea")


# ------------------------------------------------------- what is indexed


def test_search_fields_are_the_body_fields_in_order() -> None:
    assert tuple(short for short, _name in _BODY_FIELDS) == rs.SEARCH_FIELDS


def test_units_hold_exactly_the_text_the_scan_reads() -> None:
    rng = random.Random(11)
    alphabet = 'abcAB ș\nĂ→"\\\x00x{}:,'
    for _ in range(300):
        values: dict[str, Any] = {}
        if rng.random() < 0.8:
            values["input_text"] = "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 60))
            )
        if rng.random() < 0.7:
            values["output_text"] = "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 30))
            )
        if rng.random() < 0.4:
            values["thinking_text"] = "".join(rng.choice(alphabet) for _ in range(20))
        if rng.random() < 0.5:
            values["tool_calls"] = [
                {
                    "id": "toolu_x",
                    "name": "Bash",
                    "input": {"command": "ls C:\\a", "n": 3},
                }
            ]
        raw = pack_fields(values, _BODY_FIELDS)
        read = search_units(raw, True)
        assert read is not None
        _fields, units = read
        haystack = (
            searchable_text(unpack_bodies(raw)).encode("utf-8", "surrogatepass").lower()
        )
        folded = [
            text.encode("utf-8", "surrogatepass").lower() for text in units.values()
        ]
        for _ in range(20):
            start = rng.randint(0, max(0, len(haystack) - 1))
            probe = haystack[start : start + rng.randint(1, 6)]
            for word in probe.split():
                assert (word in haystack) == any(word in text for text in folded)


def test_units_refuse_what_the_scan_reads_another_way() -> None:
    canonical = pack_fields({"input_text": "héllo"}, _BODY_FIELDS)
    assert search_units(canonical, True) == (1, {0: "héllo"})
    # Raw UTF-8, as an older writer could have stored it: the scan's byte
    # pre-filter reads it differently, so the index leaves it to the scan.
    assert search_units('{"i":"héllo"}'.encode(), True) is None
    assert search_units(b'{"i": "x"}', True) is None
    assert search_units(b"not json", True) is None
    assert search_units(b"[1]", True) is None
    # A field present with no text still hides the reply blob's field.
    assert search_units(pack_fields({"input_text": ""}, _BODY_FIELDS), True) == (1, {})


# ------------------------------------------------------- identity, end to end


def test_the_index_answers_every_search_exactly_like_the_scan(
    tmp_path: Path, force_index: list[int]
) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=600, seed=21)
    assert store.search_index_coverage() == {"rows": 600, "covered": 600}
    checked = _assert_identical(store, (*CORPUS, *EXTRA_TERMS))
    assert checked == (len(CORPUS) + len(EXTRA_TERMS)) * len(FILTER_SETS)
    assert len(force_index) >= checked // 2


@pytest.mark.parametrize("factor", [0, 10**9], ids=["forward", "links"])
def test_both_ways_from_chunks_to_blobs_answer_like_the_scan(
    tmp_path: Path, force_index: list[int], monkeypatch: pytest.MonkeyPatch, factor: int
) -> None:
    """All time, every word, through the links of its chunks and forward."""

    monkeypatch.setattr(rs, "_FORWARD_FACTOR", factor)
    store, _times = build_search_log(tmp_path / "requests.db", rows=400, seed=27)
    for q in (*CORPUS, *EXTRA_TERMS):
        assert index_rows(store, q, local="all") == scan_rows(store, q, local="all"), q
    assert force_index


def test_a_window_looked_up_through_its_own_blobs_answers_like_the_scan(
    tmp_path: Path, force_index: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every window reads only the blobs its own rows name (``scope``)."""

    monkeypatch.setattr(rl, "_SEARCH_SCOPE_SHARE", 2.0)
    scopes: list[int] = []
    real_window = RequestLogStore._search_window

    def recording(
        conn: sqlite3.Connection, where: str, args: Any
    ) -> tuple[set[int], int]:
        keys, uncovered = real_window(conn, where, args)
        scopes.append(len(keys))
        return keys, uncovered

    monkeypatch.setattr(RequestLogStore, "_search_window", staticmethod(recording))
    store, times = build_search_log(tmp_path / "requests.db", rows=500, seed=33)
    newest = max(times)
    for since in (None, newest - 3 * 86400, newest - 20 * 86400):
        for q in (*CORPUS, "e", "ok"):
            for chosen in FILTER_SETS:
                window = {**chosen, "since": since}
                assert index_rows(store, q, **window) == scan_rows(
                    store, q, **window
                ), (
                    q,
                    window,
                )
    assert scopes
    assert force_index


def test_a_window_mixing_covered_and_uncovered_rows_answers_like_the_scan(
    tmp_path: Path, force_index: list[int]
) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=500, seed=4)
    conn = _index_conn(store)
    keys = [int(row[0]) for row in conn.execute("SELECT key FROM blobs ORDER BY key")]
    with conn:
        conn.executemany(
            "DELETE FROM blobs WHERE key = ?", [(key,) for key in keys[::2]]
        )
    conn.close()
    coverage = store.search_index_coverage()
    assert coverage is not None
    assert 0 < coverage["covered"] < coverage["rows"]
    _assert_identical(store, CORPUS)
    assert force_index


def _count_prepare(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    prepared: list[int] = []
    original = rs.SearchIndex.prepare

    def counting(
        self: rs.SearchIndex, conn: sqlite3.Connection, plans: Any, **kw: Any
    ) -> Any:
        prepared.append(len(plans))
        return original(self, conn, plans, **kw)

    monkeypatch.setattr(rs.SearchIndex, "prepare", counting)
    return prepared


def test_a_window_within_its_first_rows_is_the_scans_own_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No index file is opened, nor any query added, for a window of few rows."""

    store, _times = build_search_log(tmp_path / "requests.db", rows=120, seed=11)
    attached: list[int] = []
    original = rs.SearchIndex.attach

    def counting(self: rs.SearchIndex, conn: sqlite3.Connection) -> bool:
        attached.append(1)
        return original(self, conn)

    monkeypatch.setattr(rs.SearchIndex, "attach", counting)
    assert rl._SEARCH_SCAN_FIRST_ROWS >= 120
    _assert_identical(store, CORPUS[:10])
    assert attached == []


def test_the_rest_of_a_pass_starts_below_the_last_timestamp_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_index: list[int]
) -> None:
    """A read limit inside a run of equal timestamps reads the whole run first.

    Every row once, none twice, in the scan's order: the first statement
    finishes the timestamp it stopped in, and the rest is strictly older.
    """

    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(
                index,
                1000.0 + index // 7,
                input_text=f"row {index} " + ("needle" if index % 3 else "hay"),
                output_text="reply",
            )
            for index in range(60)
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    for first in (1, 10, 13, 14, 59, 60):
        monkeypatch.setattr(rl, "_SEARCH_SCAN_FIRST_ROWS", first)
        for q in ("needle", "hay", "row", "zqxjvkw"):
            for chosen in ({"local": "all"}, {"local": "all", "since": 1004.0}):
                expected = scan_rows(store, q, **chosen)
                assert index_rows(store, q, **chosen) == expected, (first, q, chosen)
                reads: list[float] = []
                got = list(
                    store.match_rows(q=q, **_filters(**chosen), on_read=reads.append)
                )
                assert got == expected, (first, q, chosen, "on_read")
                # Every row of the window is read exactly once.
                assert len(reads) == (60 if "since" not in chosen else 60 - 28)
    assert force_index


def test_a_build_under_way_leaves_a_mostly_uncovered_window_to_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows the index lacks cost the scan either way: too many, and the scan answers."""

    monkeypatch.setattr(rl, "_SEARCH_SCAN_FIRST_ROWS", 0)
    monkeypatch.setattr(rl, "_SEARCH_SCAN_SECONDS_MIN", 0.0)
    prepared = _count_prepare(monkeypatch)
    store, _times = build_search_log(
        tmp_path / "requests.db", rows=320, seed=5, inline_share=0.0
    )
    for q in ("zqxjvkw", CORPUS[0]):
        assert index_rows(store, q, local="all") == scan_rows(store, q, local="all")
    assert prepared, "a fully covered log is answered from the index"
    prepared.clear()
    conn = _index_conn(store)
    keys = [int(row[0]) for row in conn.execute("SELECT key FROM blobs ORDER BY key")]
    with conn:
        conn.executemany(
            "DELETE FROM blobs WHERE key = ?",
            [(key,) for number, key in enumerate(keys) if number % 10],
        )
    conn.close()
    coverage = store.search_index_coverage()
    assert coverage is not None
    assert coverage["covered"] < coverage["rows"] * 0.2
    for q in ("zqxjvkw", CORPUS[0]):
        for chosen in (
            {"local": "all"},
            {"local": "all", "since": sorted(_times)[192]},
        ):
            assert index_rows(store, q, **chosen) == scan_rows(store, q, **chosen)
    assert prepared == []


def test_a_build_says_how_long_it_has_left_only_after_running_a_while(
    tmp_path: Path,
) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=40, seed=3)
    now = time.time()
    running = {
        "state": "running",
        "started_at": now - 600,
        "total": 1_000,
        "done": 500,
        "session_done": 3,
    }
    with store._search_lock:
        store._search_build = {**running, "session_started": now - 2}
    status = store.search_index_status()
    assert status["percent"] == 50.0
    assert status["eta_seconds"] is None
    with store._search_lock:
        store._search_build = {
            **running,
            "session_started": now - 60,
            "session_done": 100,
        }
    status = store.search_index_status()
    assert status["eta_seconds"] == pytest.approx(300, rel=0.05)


def test_an_entry_under_another_address_is_never_answered_from_the_index(
    tmp_path: Path, force_index: list[int]
) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=300, seed=8)
    conn = _index_conn(store)
    with conn:
        # Every entry now names a content address the log does not hold.
        conn.execute("UPDATE blobs SET sha = randomblob(32)")
    conn.close()
    assert store.search_index_coverage() == {"rows": 300, "covered": 60}
    _assert_identical(store, CORPUS[:8])


def test_bodies_moved_to_other_rowids_stay_covered(
    tmp_path: Path, force_index: list[int]
) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=200, seed=12)
    conn = store._connect()
    with conn:
        # What a VACUUM, a copy or a later storage change may do: the same
        # bodies, under other rowids.
        conn.execute(
            "INSERT INTO body_blobs (rowid, sha, dict_id, payload)"
            " SELECT rowid + 1000000, sha || '-', dict_id, payload FROM body_blobs"
        )
        conn.execute("DELETE FROM body_blobs WHERE rowid < 1000000")
        conn.execute("UPDATE body_blobs SET sha = substr(sha, 1, 64)")
    conn.close()
    assert store.search_index_coverage() == {"rows": 200, "covered": 200}
    _assert_identical(store, CORPUS[:8])
    assert force_index


def test_without_the_index_file_every_search_is_the_scan(tmp_path: Path) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=200, seed=2)
    assert store._search is not None
    store._search.path.unlink()
    for name in ("-wal", "-shm"):
        store._search.path.with_name(store._search.path.name + name).unlink(
            missing_ok=True
        )
    _assert_identical(store, CORPUS[:6])
    assert not store._search.path.exists()


# ------------------------------------------------------- the §5.4 rules


def test_rule_1_every_word_split_on_any_whitespace(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(1, 1000.0, input_text="the answer was too long for it"),
            _record(2, 1001.0, input_text="long story", output_text="too bad"),
            _record(3, 1002.0, input_text="only too here"),
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    for q in ("too long", "too\u3000long", "  too\tlong ", "long too", "too"):
        assert index_rows(store, q, local="all") == scan_rows(store, q, local="all")
    assert {row for row, _ts in index_rows(store, "too long", local="all")} == {1, 2}


def test_rule_2_case_folds_plain_letters_only(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(1, 1000.0, input_text="Ăsta e mesajul README"),
            _record(2, 1001.0, input_text="ăsta e bun readme"),
            _record(3, 1002.0, input_text="ĂSTA STA"),
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    expected = {
        "ăsta": {2},
        "Ăsta": {1, 3},
        "ĂSTA": {1, 3},
        "readme": {1, 2},
        "ReadMe": {1, 2},
    }
    for q, rows in expected.items():
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert {row for row, _ts in got} == rows, q


def test_rule_3_tool_call_strings_reply_and_reasoning_are_searched(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    tools = [
        {
            "id": "toolu_abc123",
            "name": "BashTool",
            "input": {"command": "rg needle C:\\x"},
        }
    ]
    _write(
        path,
        [
            _record(1, 1000.0, input_text="plain", tool_calls=tools),
            _record(2, 1001.0, input_text="plain", output_text="reply-only-word"),
            _record(3, 1002.0, input_text="plain", thinking_text="thought-only-word"),
            _record(4, 1003.0, input_text="command name id input"),
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    expected = {
        "toolu_abc": {1},
        "BashTool": {1},
        "needle": {1},
        "C:\\x": {1},
        "reply-only": {2},
        "thought-only": {3},
        "command": {4},
    }
    for q, rows in expected.items():
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert {row for row, _ts in got} == rows, q


def test_rule_4_a_word_never_spans_two_fields(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(
                1,
                1000.0,
                input_text="ends with foo",
                output_text="bar starts here",
                tool_calls=[{"a": "abc", "b": "def"}],
            ),
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    for q, found in (
        ("foobar", False),
        ("foo", True),
        ("abcdef", False),
        ("abc", True),
    ):
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert bool(got) is found, q


def test_rule_5_quotes_backslashes_and_control_characters(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(1, 1000.0, input_text='say "hi" to C:\\Users\\dev and tab\there'),
            _record(2, 1001.0, input_text="say hi to C:/Users/dev"),
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    for q, rows in (
        ('"hi"', {1}),
        ('"hi', {1}),
        ("C:\\Users\\dev", {1}),
        ("\\", {1}),
        ("C:/Users", {2}),
    ):
        answered = len(force_index)
        got = index_rows(store, q, local="all")
        # The index itself answered: a quote reached the trigram query escaped.
        assert len(force_index) == answered + 1, q
        assert got == scan_rows(store, q, local="all")
        assert {row for row, _ts in got} == rows, q


def test_a_word_across_the_first_kilobyte_is_found(
    tmp_path: Path, force_index: list[int]
) -> None:
    # One long line: a chunk never ends inside a line, so no word is cut.
    text = "a" * 1015 + " straddling-word " + "b" * 2000
    path = tmp_path / "requests.db"
    _write(path, [_record(1, 1000.0, input_text=text)])
    store = RequestLogStore(path, max_rows=0)
    store.close()
    for q in ("straddling-word", "aaa straddling", "word bbb"):
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert [row for row, _ts in got] == [1], q


def test_rule_6_inline_rows_keep_their_like_wildcards(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path, [_record(1, 1000.0, input_text="inline abc 100x")], compress_bodies=False
    )
    _write(path, [_record(2, 1001.0, input_text="stored abc 100x")])
    store = RequestLogStore(path, max_rows=0)
    store.close()
    # ``_`` and ``%`` are LIKE wildcards on inline rows only, as before.
    for q, rows in (("a_c", {1}), ("100%", {1}), ("abc", {1, 2})):
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert {row for row, _ts in got} == rows, q


def test_rule_7_the_prompt_blob_hides_a_reply_blob_field_of_the_same_name(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(1, 1000.0, input_text="current prompt", output_text="reply"),
            _record(2, 1001.0, input_text="other prompt", output_text="reply two"),
        ],
    )
    # A reply blob from before the prompt had its own blob also carries one:
    # the prompt blob's wins, so its old prompt must not be found.
    combined = pack_fields(
        {"input_text": "stale-prompt-word", "output_text": "combined-reply-word"},
        _BODY_FIELDS,
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    conn = store._connect()
    with conn:
        dict_id, payload = store._compress_packed(combined)
        sha = hashlib.sha256(combined).hexdigest()
        conn.execute(
            "INSERT INTO body_blobs (sha, dict_id, payload) VALUES (?, ?, ?)",
            (sha, dict_id, payload),
        )
        conn.execute(
            "UPDATE request_bodies SET sha = ? WHERE request_id = 'rec-00001'", (sha,)
        )
    conn.close()
    store = RequestLogStore(path, max_rows=0)
    store.request_search_index_build()
    _wait(lambda: store.search_index_status()["state"] == "done")
    store.close()
    for q, rows in (
        ("stale-prompt-word", set()),
        ("combined-reply-word", {1}),
        ("current", {1}),
        ("prompt", {1, 2}),
    ):
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert {row for row, _ts in got} == rows, q
    assert force_index


def test_rule_8_nul_and_lone_surrogates_are_tested_not_trusted(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(1, 1000.0, input_text="a\x00bc then abc"),
            _record(2, 1001.0, input_text="a\x00bc only"),
            _record(3, 1002.0, input_text="lone \ud800 surrogate xy"),
            _record(4, 1003.0, input_text="ok"),
        ],
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    conn = _index_conn(store)
    tested = conn.execute("SELECT COUNT(*) FROM chunks WHERE flags != 0").fetchone()[0]
    conn.close()
    assert tested >= 3
    for q, rows in (
        ("abc", {1}),
        ("a\x00bc", {1, 2}),
        ("xy", {3}),
        ("ok", {4}),
        ("o", {2, 3, 4}),
    ):
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert {row for row, _ts in got} == rows, q


def test_rule_10_the_index_holds_the_capped_text(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    # 120 characters are stored: the edge word ends at character 120, the
    # tail word starts after it.
    text = "head-word " + "filler line\n" * 8 + "x" * 5 + "edge-word" + " tail-word"
    assert text.index("edge-word") + len("edge-word") == 120
    _write(path, [_record(1, 1000.0, input_text=text)], text_max_chars=120)
    store = RequestLogStore(path, max_rows=0)
    store.close()
    for q, found in (("head-word", True), ("edge-word", True), ("tail-word", False)):
        got = index_rows(store, q, local="all")
        assert got == scan_rows(store, q, local="all")
        assert bool(got) is found, q


# ------------------------------------------------------- how it is written


def test_new_rows_are_indexed_as_they_are_written(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    store.enqueue(_record(1, 1000.0, input_text="first prompt", output_text="answer"))
    store.close()
    conn = store._connect()
    blob_keys = {str(row[0]) for row in conn.execute("SELECT sha FROM body_blobs")}
    conn.close()
    assert _indexed_keys(store) == blob_keys
    assert store.search_index_status()["written"]["blobs"] == len(blob_keys)


def test_an_index_failure_never_costs_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rs.SearchIndex, "add", broken)
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    store.enqueue(_record(1, 1000.0, input_text="kept anyway"))
    store.close()
    assert [row for row, _ts in scan_rows(store, "kept", local="all")] == [1]
    assert index_rows(store, "kept", local="all") == scan_rows(
        store, "kept", local="all"
    )


def test_history_is_indexed_only_when_the_build_is_asked_for(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [
            _record(n, 1000.0 + n, input_text=f"old prompt {n} too long")
            for n in range(40)
        ],
        search_index=False,
    )
    store = RequestLogStore(path, max_rows=0)
    # Nothing is indexed by itself: everything already logged is history.
    time.sleep(0.6)
    assert _indexed_keys(store) == set()
    assert store.search_index_status()["state"] == "idle"
    store.request_search_index_build()
    _wait(lambda: store.search_index_status()["state"] == "done")
    status = store.search_index_status()
    assert status["percent"] == 100.0
    assert status["done"] == status["total"]
    store.close()
    assert store.search_index_coverage() == {"rows": 40, "covered": 40}
    _assert_identical(store, ("too long", "prompt", "1", "zq"))


def test_a_build_continues_after_a_restart_from_where_it_stopped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [_record(n, 1000.0 + n, input_text=f"prompt number {n}") for n in range(60)],
        search_index=False,
    )
    store = RequestLogStore(path, max_rows=0)
    store.close()
    conn = store._connect()
    keys = sorted(int(row[0]) for row in conn.execute("SELECT rowid FROM body_blobs"))
    conn.close()
    middle = keys[len(keys) // 2]
    # A build that stopped half-way: its saved cursor is the lowest key reached.
    sconn = _index_conn(store)
    with sconn:
        sconn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('build', ?)",
            (
                json.dumps(
                    {
                        "state": "running",
                        "started_at": 1.0,
                        "cursor": middle,
                        "total": len(keys),
                        "done": len(keys) - keys.index(middle),
                        "indexed": 0,
                    }
                ),
            ),
        )
    sconn.close()
    store = RequestLogStore(path, max_rows=0)
    assert store.search_index_status()["state"] == "running"
    _wait(lambda: store.search_index_status()["state"] == "done")
    store.close()
    # It continued below the cursor and never went back above it.
    assert _indexed_keys(store) == _log_blobs(store, below=middle)
    assert store.search_index_status()["done"] == len(keys)


def test_pause_stops_the_build_and_continue_resumes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [_record(n, 1000.0 + n, input_text=f"prompt {n}") for n in range(30)],
        search_index=False,
    )
    store = RequestLogStore(path, max_rows=0)
    # Paused before the writer's first idle step can run it.
    with store._search_lock:
        store._search_build = {"state": "paused", "started_at": 5.0, "cursor": None}
        store._search_build_dirty = True
    time.sleep(0.8)
    assert _indexed_keys(store) == set()
    assert store.search_index_status()["state"] == "paused"
    store.request_search_index_build()
    _wait(lambda: store.search_index_status()["state"] == "done")
    store.close()
    assert len(_indexed_keys(store)) == 30


def test_a_search_running_holds_the_build_back(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    _write(
        path,
        [_record(n, 1000.0 + n, input_text=f"p {n}") for n in range(20)],
        search_index=False,
    )
    store = RequestLogStore(path, max_rows=0)
    store._search_enter()
    try:
        store.request_search_index_build()
        time.sleep(0.8)
        assert _indexed_keys(store) == set()
    finally:
        store._search_leave()
    _wait(lambda: store.search_index_status()["state"] == "done")
    store.close()


def test_bodies_an_older_version_wrote_are_caught_up_on_the_next_start(
    tmp_path: Path, force_index: list[int]
) -> None:
    path = tmp_path / "requests.db"
    _write(path, [_record(1, 1000.0, input_text="indexed at write time")])
    # An older version knows nothing of the index file.
    _write(
        path,
        [_record(2, 1001.0, input_text="written by an older version")],
        search_index=False,
    )
    store = RequestLogStore(path, max_rows=0)
    conn = store._connect()
    blob_keys = {str(row[0]) for row in conn.execute("SELECT sha FROM body_blobs")}
    conn.close()
    # Caught up between requests, without being asked: it is newer than the
    # index, so it is not history.
    _wait(lambda: _indexed_keys(store) == blob_keys)
    store.close()
    assert index_rows(store, "older", local="all") == scan_rows(
        store, "older", local="all"
    )


def test_retention_takes_pruned_text_out_of_the_index(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=5, queue_max_size=300)
    for n in range(120):
        store.enqueue(_record(n, 1000.0 + n, input_text=f"unique prompt {n} " * 20))
    _wait(lambda: store._queue.empty())

    def blob_keys() -> set[str]:
        conn = store._connect()
        try:
            return {str(row[0]) for row in conn.execute("SELECT sha FROM body_blobs")}
        finally:
            conn.close()

    # The prune deleted most rows; the sweep follows between requests.
    _wait(lambda: len(blob_keys()) < 120 and _indexed_keys(store) == blob_keys())
    store.close()
    sconn = _index_conn(store)
    linked = sconn.execute("SELECT COUNT(DISTINCT chunk) FROM links").fetchone()[0]
    chunks = sconn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    sconn.close()
    assert chunks == linked


def test_every_prompt_rebuilds_byte_for_byte_from_its_chunks(tmp_path: Path) -> None:
    """The chunk copy is lossless: what the prompt chunk store (N) will rely on.

    For every prompt-only blob, its chunks, in its manifest's order, packed the
    way the writer packs a prompt, hash back to the blob's own address.
    """

    store, _times = build_search_log(tmp_path / "requests.db", rows=300, seed=14)
    assert store._search is not None
    conn = _index_conn(store)
    checked = 0
    try:
        for key, sha in conn.execute("SELECT key, sha FROM blobs WHERE fields = 1"):
            manifest = conn.execute(
                "SELECT manifest FROM units WHERE key = ? AND field = 0", (key,)
            ).fetchone()
            pieces = []
            for chunk_id in rs.manifest_ids(bytes(manifest[0])):
                dict_id, data = conn.execute(
                    "SELECT dict_id, data FROM chunks WHERE id = ?", (chunk_id,)
                ).fetchone()
                pieces.append(store._search.chunk_text(conn, int(dict_id), bytes(data)))
            text = b"".join(pieces).decode("utf-8", "surrogatepass")
            packed = pack_fields({"input_text": text}, _BODY_FIELDS[:1])
            assert hashlib.sha256(packed).digest() == bytes(sha)
            checked += 1
    finally:
        conn.close()
    assert checked > 200


def test_clear_log_clears_the_index(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=0)
    store.enqueue(_record(1, 1000.0, input_text="secret words here"))
    _wait(lambda: bool(_indexed_keys(store)))
    store.clear()
    sconn = _index_conn(store)
    counts = [
        sconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("chunks", "blobs", "units", "links")
    ]
    sconn.close()
    assert counts == [0, 0, 0, 0]
    store.enqueue(_record(2, 1001.0, input_text="after the clear"))
    store.close()
    assert len(_indexed_keys(store)) == 1
    assert index_rows(store, "after", local="all") == scan_rows(
        store, "after", local="all"
    )


def test_a_python_without_the_trigram_tokenizer_keeps_searching_the_old_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rl,
        "search_index_support",
        lambda: (
            False,
            "This Python's SQLite (3.31.1) cannot keep the search index (no such tokenizer: trigram).",
        ),
    )
    store, _times = build_search_log(tmp_path / "requests.db", rows=120, seed=9)
    assert store._search is None
    assert not (tmp_path / "requests-search.db").exists()
    status = store.search_index_status()
    assert status["available"] is False
    assert "trigram" in status["reason"]
    assert store.search_index_coverage() is None
    for q in CORPUS[:6]:
        assert index_rows(store, q, local="hide") == scan_rows(store, q, local="hide")


def test_the_support_check_passes_on_this_python() -> None:
    assert rs.search_index_support() == (True, None)


def test_a_newer_index_schema_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    index = rs.search_index_path(path)
    conn = sqlite3.connect(index)
    with conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema', '99')")
    conn.close()
    store = RequestLogStore(path, max_rows=0)
    store.enqueue(_record(1, 1000.0, input_text="still logged"))
    store.close()
    assert store.search_index_status()["available"] is False
    assert [row for row, _ts in index_rows(store, "logged", local="all")] == [1]
