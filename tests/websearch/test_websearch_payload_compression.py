"""Captured search results stored compressed, history converted (7.77.0)."""

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.websearch import analytics
from my_claude_code.websearch.analytics import WebSearchLogStore
from my_claude_code.websearch.registry import SearchOutcome, SearchRouteOutcome

_BASE_TS = datetime(2026, 9, 1, 12, 0, tzinfo=UTC).timestamp()
_KEY = analytics._HISTORY_CONVERSION_KEY


def _page(index: int, words: str) -> dict[str, object]:
    # Repetitive page text, the way real results are: compresses well.
    body = " ".join(f"{words} paragraph {n} of result {index}." for n in range(400))
    return {
        "results": [
            {"url": f"https://example.org/{index}", "title": f"T{index}", "text": body}
        ]
    }


def _outcome(
    index: int,
    *,
    output: dict[str, object] | None,
    route_id: str | None = None,
    query: str = "query",
) -> SearchOutcome:
    ts = _BASE_TS + index
    return SearchOutcome(
        ts_epoch=ts,
        ts_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat(),
        provider="exa" if index % 2 else "tavily",
        key_index=0,
        key_label="exak…1234",
        query=query,
        results_count=1,
        duration_ms=10.0,
        status="success",
        error_kind=None,
        error_message=None,
        cost_usd=None,
        route_id=route_id or f"route-{index}",
        attempt_number=1,
        input_payload={"query": query, "max_results": 5},
        output_payload=output,
        provider_config={"base_url": "https://api.example.org"},
    )


def _route(index: int, query: str = "query") -> SearchRouteOutcome:
    ts = _BASE_TS + index
    return SearchRouteOutcome(
        route_id=f"route-{index}",
        ts_epoch=ts,
        ts_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat(),
        query=query,
        primary_provider="exa",
        terminal_provider="exa",
        provider_path=("exa",),
        attempt_count=1,
        fallback_used=False,
        duration_ms=12.0,
        status="success",
        results_count=1,
        cost_usd=None,
        error_kind=None,
        error_message=None,
    )


_WORDS = (
    "Alpha proxy",
    "Bucuresti șosea ăla",
    "em — dash → arrow",
    "MiXeD CaSe Needle",
    "plain words",
)


def _seed(path: Path, count: int = 12) -> None:
    store = WebSearchLogStore(path)
    try:
        for index in range(count):
            store.record(_outcome(index, output=_page(index, _WORDS[index % 5])))
            store.record_route(_route(index))
        store.record(_outcome(count, output=None))
        store.record(_outcome(count + 1, output={"tiny": 1}))
        store.flush()
    finally:
        store.close()


def _as_older_version_wrote_it(path: Path) -> None:
    """Every compressed payload back to the TEXT an older version stored."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT id, output_json FROM search_log WHERE typeof(output_json) = 'blob'"
        ).fetchall()
        for row_id, stored in rows:
            text = analytics._payload_text(stored)
            assert text is not None
            connection.execute(
                "UPDATE search_log SET output_json = ? WHERE id = ?", (text, row_id)
            )
        connection.execute("DELETE FROM search_log_meta")
        connection.commit()
    finally:
        connection.close()


def _storage(path: Path) -> dict[int, str]:
    connection = sqlite3.connect(path)
    try:
        return {
            int(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT id, typeof(output_json) FROM search_log ORDER BY id"
            )
        }
    finally:
        connection.close()


def _state(path: Path) -> dict[str, Any] | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM search_log_meta WHERE key = ?", (_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # the writer has not created its tables yet
    finally:
        connection.close()
    return json.loads(row[0]) if row else None


def _wait_done(path: Path, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(path)
        if state is not None and state.get("done_at") is not None:
            return state
        time.sleep(0.05)
    raise AssertionError(f"conversion not done within {timeout}s: {_state(path)}")


def _views(store: WebSearchLogStore) -> dict[str, Any]:
    """Everything a reader can see, for before/after equality."""
    views: dict[str, Any] = {
        "details": [store.request(i) for i in range(1, 20)],
        "page": store.requests(limit=500, include_content=True),
        "export": list(
            store.iter_export_rows(
                columns=["id", "query", "output", "input", "provider_config"],
                include_content=True,
            )
        ),
    }
    for term in ("needle", "Proxy", "ș", "ă", "—", "→", "zzzz-none", "result 3."):
        views[f"q:{term}"] = (
            store.requests(q=term, limit=500),
            store.stats("daily", q=term),
        )
    return views


def _output(store: WebSearchLogStore, request_id: int) -> object:
    item = store.request(request_id)
    assert item is not None
    return item["output"]


def _views_without_converting(path: Path, monkeypatch) -> dict[str, Any]:
    """``_views`` of a database the conversion is kept from touching."""
    with monkeypatch.context() as patch:
        patch.setattr(WebSearchLogStore, "_run_history_conversion", lambda _s, _c: None)
        store = WebSearchLogStore(path)
        try:
            return _views(store)
        finally:
            store.close()


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    analytics.reset_analytics_state()
    yield
    analytics.reset_analytics_state()


class TestNewRows:
    def test_output_is_stored_compressed_and_reads_back_identical(
        self, tmp_path
    ) -> None:
        path = tmp_path / "websearch.db"
        output = _page(1, "Alpha proxy")
        store = WebSearchLogStore(path)
        try:
            store.record(_outcome(1, output=output))
            store.flush()
            item = store.request(1)
        finally:
            store.close()
        assert item is not None
        assert item["output"] == output
        assert item["input"] == {"query": "query", "max_results": 5}
        connection = sqlite3.connect(path)
        try:
            kinds, stored, chars = connection.execute(
                "SELECT typeof(input_json) || ',' || typeof(output_json) || ','"
                " || typeof(provider_config_json), output_json, output_chars"
                " FROM search_log"
            ).fetchone()
        finally:
            connection.close()
        assert kinds == "text,blob,text"
        assert stored[0] == analytics._PAYLOAD_ENVELOPE_V1
        text = analytics._json_dumps(output)
        assert analytics._unwrap_payload(stored) == text.encode("utf-8")
        assert len(stored) * 4 < len(text.encode("utf-8"))
        assert chars == len(text)

    def test_a_payload_compression_does_not_shrink_stays_text(self, tmp_path) -> None:
        path = tmp_path / "websearch.db"
        store = WebSearchLogStore(path)
        try:
            store.record(_outcome(1, output={"a": 1}))
            store.record(_outcome(2, output=None))
            store.flush()
            assert _output(store, 1) == {"a": 1}
            assert _output(store, 2) is None
        finally:
            store.close()
        assert _storage(path) == {1: "text", 2: "null"}

    def test_text_that_is_not_utf8_is_stored_as_before(self) -> None:
        assert analytics._stored_output("lone \udc80 surrogate") == (
            "lone \udc80 surrogate"
        )
        assert analytics._stored_output(None) is None


class TestReading:
    def test_unknown_or_broken_envelope_reads_as_no_payload(self, tmp_path) -> None:
        path = tmp_path / "websearch.db"
        _seed(path, count=2)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE search_log SET output_json = ? WHERE id = 1", (b"\x09junk",)
            )
            connection.execute(
                "UPDATE search_log SET output_json = ? WHERE id = 2",
                (bytes((analytics._PAYLOAD_ENVELOPE_V1,)) + b"not zstd",),
            )
            connection.commit()
        finally:
            connection.close()
        store = WebSearchLogStore(path)
        try:
            assert _output(store, 1) is None
            assert _output(store, 2) is None
            # The search runs the SQL function over both and raises nothing.
            assert store.requests(q="junk")["total"] == 0
            assert store.stats("daily", q="zstd")["totals"]["requests"] == 0
        finally:
            store.close()


class TestSearch:
    @pytest.mark.parametrize(
        ("term", "expected"),
        [
            ("needle", 2),
            ("NEEDLE", 2),
            ("MiXeD CaSe", 2),
            ("ș", 3),
            ("ă", 3),
            ("—", 2),
            ("→", 2),
            ("https://example.org/7", 1),
            ("zzzz-none", 0),
        ],
    )
    def test_terms_inside_compressed_results_are_found(
        self, tmp_path, term: str, expected: int
    ) -> None:
        path = tmp_path / "websearch.db"
        _seed(path)
        assert "blob" in _storage(path).values()
        store = WebSearchLogStore(path)
        try:
            assert store.requests(q=term)["total"] == expected
            assert store.stats("daily", q=term)["routes"]["totals"]["searches"] == (
                expected
            )
        finally:
            store.close()

    def test_every_view_is_identical_on_both_encodings(
        self, tmp_path, monkeypatch
    ) -> None:
        compressed = tmp_path / "compressed.db"
        _seed(compressed)
        plain = tmp_path / "plain.db"
        _seed(plain)
        _as_older_version_wrote_it(plain)
        before = _views_without_converting(plain, monkeypatch)
        assert set(_storage(plain).values()) == {"text", "null"}
        store = WebSearchLogStore(compressed)
        try:
            after = _views(store)
        finally:
            store.close()
        assert "blob" in _storage(compressed).values()
        assert json.dumps(after, sort_keys=True, default=str) == json.dumps(
            before, sort_keys=True, default=str
        )
        assert before["q:ș"][0]["total"] == 3


class TestHistoryConversion:
    def test_history_is_converted_losslessly_and_space_goes_back(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "websearch.db"
        _seed(path, count=30)
        _as_older_version_wrote_it(path)
        before = _views_without_converting(path, monkeypatch)
        connection = sqlite3.connect(path)
        try:
            originals = dict(
                connection.execute(
                    "SELECT id, CAST(output_json AS BLOB) FROM search_log"
                ).fetchall()
            )
            pages_before = connection.execute("PRAGMA page_count").fetchone()[0]
        finally:
            connection.close()

        store = WebSearchLogStore(path)
        try:
            state = _wait_done(path)
            after = _views(store)
        finally:
            store.close()

        phase = state["payloads"]
        assert phase["converted"] == 30
        assert phase["kept"] == 1  # the tiny payload
        assert phase["failed"] == 0
        assert phase["bytes_before"] > 4 * phase["bytes_after"]
        assert state["returned_pages"] > 0
        assert state["returned_pages"] <= state["freed_pages"]
        storage = _storage(path)
        assert list(storage.values()).count("blob") == 30
        connection = sqlite3.connect(path)
        try:
            for row_id, stored in connection.execute(
                "SELECT id, output_json FROM search_log"
            ):
                if isinstance(stored, bytes):
                    assert analytics._unwrap_payload(stored) == originals[row_id]
                else:
                    assert (stored or "").encode() == (originals[row_id] or b"")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            pages_after = connection.execute("PRAGMA page_count").fetchone()[0]
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()
        assert pages_after < pages_before
        assert json.dumps(after, sort_keys=True, default=str) == json.dumps(
            before, sort_keys=True, default=str
        )

    def test_a_value_that_does_not_read_back_is_left_and_counted(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "websearch.db"
        _seed(path, count=4)
        _as_older_version_wrote_it(path)
        real = analytics._reads_back
        connection = sqlite3.connect(path)
        try:
            victim = connection.execute(
                "SELECT CAST(output_json AS BLOB) FROM search_log WHERE id = 2"
            ).fetchone()[0]
        finally:
            connection.close()

        def reads_back(envelope: bytes, raw: bytes) -> bool:
            return False if raw == victim else real(envelope, raw)

        monkeypatch.setattr(analytics, "_reads_back", reads_back)
        store = WebSearchLogStore(path)
        try:
            state = _wait_done(path)
            assert _output(store, 2) == json.loads(victim)
        finally:
            store.close()
        assert state["payloads"]["failed"] == 1
        assert state["payloads"]["converted"] == 3
        assert _storage(path)[2] == "text"

    def test_resumes_from_its_marker_and_never_redoes_a_step(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "websearch.db"
        _seed(path, count=6)
        _as_older_version_wrote_it(path)
        # One row per step, and a crash in the third step's UPDATE.
        monkeypatch.setattr(analytics, "_HISTORY_FETCH_ROWS", 1)
        monkeypatch.setattr(analytics, "_HISTORY_STEP_SECONDS", 0.0)
        calls = {"n": 0}
        real = analytics._payload_envelope

        def envelope(raw: bytes) -> bytes | None:
            calls["n"] += 1
            if calls["n"] == 3:
                raise sqlite3.OperationalError("simulated crash mid-step")
            return real(raw)

        monkeypatch.setattr(analytics, "_payload_envelope", envelope)
        store = WebSearchLogStore(path)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and calls["n"] < 3:
                time.sleep(0.02)
        finally:
            store.close()
        state = _state(path)
        assert state is not None
        # Steps 1 and 2 committed with their marker; step 3 rolled back whole.
        assert state["payloads"]["through"] == 2
        assert state["payloads"]["converted"] == 2
        assert [_storage(path)[i] for i in (1, 2, 3)] == ["blob", "blob", "text"]
        monkeypatch.setattr(analytics, "_payload_envelope", real)
        store = WebSearchLogStore(path)
        try:
            state = _wait_done(path)
        finally:
            store.close()
        assert state["payloads"]["converted"] == 6
        assert state["payloads"]["failed"] == 0

    def test_no_space_is_handed_back_without_incremental_auto_vacuum(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "websearch.db"
        _seed(path, count=10)
        _as_older_version_wrote_it(path)
        # A database whose auto_vacuum could not be made incremental.
        monkeypatch.setattr(analytics, "_ensure_auto_vacuum", lambda _c: None)
        connection = sqlite3.connect(path)
        try:
            connection.isolation_level = None
            connection.execute("PRAGMA auto_vacuum=NONE")
            connection.execute("VACUUM")
        finally:
            connection.close()
        store = WebSearchLogStore(path)
        try:
            state = _wait_done(path)
        finally:
            store.close()
        assert state["payloads"]["converted"] == 10
        assert state["returned_pages"] == 0
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
            assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 0
        finally:
            connection.close()

    def test_a_new_database_finishes_at_once_and_silently(self, tmp_path) -> None:
        path = tmp_path / "websearch.db"
        store = WebSearchLogStore(path)
        try:
            state = _wait_done(path)
        finally:
            store.close()
        assert state["payloads"]["converted"] == 0
        assert state["returned_pages"] == 0

    def test_the_conversion_yields_to_a_queued_record_and_a_close(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "websearch.db"
        _seed(path, count=3)
        _as_older_version_wrote_it(path)
        with monkeypatch.context() as patch:
            patch.setattr(
                WebSearchLogStore, "_run_history_conversion", lambda _s, _c: None
            )
            store = WebSearchLogStore(path)
            store.close()
        # The writer is gone; drive the idle branch by hand.
        steps: list[str] = []
        monkeypatch.setattr(
            store, "_history_step", lambda _c: steps.append("step") is not None
        )
        connection = sqlite3.connect(path)
        try:
            store._stopping.clear()
            store._queue.put(_outcome(99, output=None))
            store._run_history_conversion(connection)
            assert steps == []
            store._queue.get_nowait()
            store._stopping.set()
            store._run_history_conversion(connection)
            assert steps == []
            store._stopping.clear()
            store._run_history_conversion(connection)
            assert steps == ["step"]
        finally:
            connection.close()
