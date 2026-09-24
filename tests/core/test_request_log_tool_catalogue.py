"""The tools array a request carried, recorded once and found in one query (7.40.0).

Until 7.40.0 the log kept ``params.tools_count`` and nothing else. When one tool
pattern in a 212-tool catalogue broke every ChatGPT-OAuth request on
2026-09-20, finding the tool took hours, because the array had never been kept.

What this file pins:

* the request row carries one 32-byte hash, and each distinct array and each
  distinct tool definition is stored once;
* the hashing and every write happen on the writer thread;
* definitions follow the body-capture switch; names and hashes do not;
* "which requests carried tool X" is one call;
* retention and "Clear log" clean the new tables;
* and every analytics answer that existed before is unchanged by any of it.
"""

import json
import sqlite3
import threading
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import Tool
from my_claude_code.core.export import (
    DEFAULT_REQUEST_FIELDS,
    request_detail_columns,
)
from my_claude_code.core.request_log import (
    _ADDED_COLUMNS,
    _REQUEST_INSERT_COLUMNS,
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)
from my_claude_code.core.tool_catalogue import ToolFingerprinter, fingerprint_tools

BASE_TS = 1_789_909_322.0
VIDEO_SCALE = r"^(?:[1-9]\d{0,4}|-[12]):(?:[1-9]\d{0,4}|-[12])(?![\s\S])"


def _tool(name: str, pattern: str | None = None) -> Tool:
    properties: dict[str, Any] = {"value": {"type": "string"}}
    if pattern is not None:
        properties["videoScale"] = {"type": "string", "pattern": pattern}
    return Tool.model_validate(
        {
            "name": name,
            "description": f"The {name} tool.",
            "input_schema": {"type": "object", "properties": properties},
        }
    )


APPIUM = "mcp__appium-mcp__appium_screen_recording"
SESSION = (
    _tool("Bash"),
    _tool("Read"),
    _tool(APPIUM, VIDEO_SCALE),
)


def _record(request_id: str, index: int = 0, **overrides: Any) -> RequestRecord:
    values: dict[str, Any] = {
        "id": request_id,
        "ts_epoch": BASE_TS + index,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-opus-4",
        "provider": "chatgpt_oauth" if index % 2 == 0 else "custom_agnes",
        "resolved_model": "gpt-5.6-sol" if index % 2 == 0 else "agnes-1",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "tokens_in": 10 + index,
        "tokens_out": 20 + index,
        "cache_read_tokens": index,
        "ttft_ms": 100.0 + index,
        "ttft_winner_ms": 90.0 + index,
        "duration_ms": 900.0 + 10 * index,
        "status": "error" if index % 5 == 4 else "success",
        "error_kind": "model_rejected" if index % 5 == 4 else None,
        "tool_call_count": index % 3,
        "cost_usd": 0.001 * index if index % 2 else None,
        "cost_source": "models_dev" if index % 2 else None,
        "harness": "claude",
        "params": {"tools_count": 3},
        "attempts": (
            RouteAttempt(
                attempt=0,
                provider="chatgpt_oauth",
                model_ref="chatgpt_oauth/gpt-5.6-sol",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                duration_ms=900.0 + 10 * index,
                ttft_ms=90.0 + index,
            ),
        ),
    }
    values.update(overrides)
    return RequestRecord(**values)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    yield store
    store.close()


def _rows(store: RequestLogStore, sql: str, *args: Any) -> list[tuple]:
    connection = sqlite3.connect(store.db_path)
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


class TestTheRequestRow:
    def test_the_row_carries_the_32_byte_catalogue_hash(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        expected = fingerprint_tools(SESSION)
        assert expected is not None
        ((stored,),) = _rows(
            store, "SELECT tool_catalogue_sha FROM requests WHERE id = 'r1'"
        )
        assert stored == expected.sha
        assert len(stored) == 32

    def test_a_request_without_tools_stores_nothing(self, store) -> None:
        store.enqueue(_record("r1", params={"tools_count": 0}))
        store.close()

        assert _rows(store, "SELECT tool_catalogue_sha FROM requests") == [(None,)]
        assert _rows(store, "SELECT COUNT(*) FROM tool_catalogues") == [(0,)]
        assert _rows(store, "SELECT COUNT(*) FROM tool_schemas") == [(0,)]

    def test_it_is_a_declared_guarded_column(self) -> None:
        assert "tool_catalogue_sha" in _REQUEST_INSERT_COLUMNS
        names = [name for name, _sql in _ADDED_COLUMNS]
        assert names.count("tool_catalogue_sha") == 1

    def test_a_database_without_the_column_gains_it(self, tmp_path) -> None:
        path = tmp_path / "legacy.db"
        old = RequestLogStore(path, max_rows=100)
        old.enqueue(_record("old"))
        old.close()
        connection = sqlite3.connect(path)
        try:
            connection.execute("ALTER TABLE requests DROP COLUMN tool_catalogue_sha")
            connection.execute("DROP TABLE tool_catalogues")
            connection.execute("DROP TABLE tool_schemas")
            connection.commit()
        finally:
            connection.close()

        migrated = RequestLogStore(path, max_rows=100)
        migrated.enqueue(_record("new", 1, tools=SESSION))
        migrated.close()
        again = RequestLogStore(path, max_rows=100)
        again.close()

        columns = [row[1] for row in _rows(again, "PRAGMA table_info(requests)")]
        assert columns.count("tool_catalogue_sha") == 1
        legacy = again.get_request("old")
        fresh = again.get_request("new")
        assert legacy is not None and fresh is not None
        assert legacy["tool_catalogue_sha"] is None
        assert legacy["tool_catalogue"] is None
        assert fresh["tool_catalogue"]["tool_count"] == 3


class TestContentAddressing:
    def test_one_session_is_one_catalogue_and_one_row_per_tool(self, store) -> None:
        for index in range(5):
            store.enqueue(_record(f"r{index}", index, tools=SESSION))
        store.close()

        assert _rows(store, "SELECT COUNT(*) FROM tool_catalogues") == [(1,)]
        assert _rows(store, "SELECT COUNT(*) FROM tool_schemas") == [(3,)]
        ((tool_count, seen, first, last, members),) = _rows(
            store,
            "SELECT tool_count, seen, first_seen, last_seen, length(member_shas)"
            " FROM tool_catalogues",
        )
        assert (tool_count, seen, members) == (3, 5, 96)
        assert (first, last) == (BASE_TS, BASE_TS + 4)

    def test_two_arrays_share_the_definitions_they_have_in_common(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.enqueue(_record("r2", 1, tools=SESSION[:2]))
        # Same tools, another order: another array, no new definitions.
        store.enqueue(_record("r3", 2, tools=tuple(reversed(SESSION))))
        store.close()

        assert _rows(store, "SELECT COUNT(*) FROM tool_catalogues") == [(3,)]
        assert _rows(store, "SELECT COUNT(*) FROM tool_schemas") == [(3,)]

    def test_a_record_written_twice_is_counted_once(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()
        again = RequestLogStore(store.db_path, max_rows=10_000)
        again.enqueue(_record("r1", tools=SESSION))
        again.close()

        assert _rows(again, "SELECT seen FROM tool_catalogues") == [(1,)]

    def test_a_definition_is_stored_once_as_its_canonical_json(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        catalogue = fingerprint_tools(SESSION)
        assert catalogue is not None
        appium = catalogue.members[2]
        ((name, definition),) = _rows(
            store,
            "SELECT name, definition FROM tool_schemas WHERE sha = ?",
            appium.sha,
        )
        assert name == APPIUM
        assert definition == appium.definition
        schema = json.loads(definition)["input_schema"]
        assert schema["properties"]["videoScale"]["pattern"] == VIDEO_SCALE

    def test_the_incident_question_is_one_query(self, store) -> None:
        """2026-09-20: "which tool carries ``videoScale``?" took hours."""

        for index in range(3):
            store.enqueue(_record(f"r{index}", index, tools=SESSION))
        store.close()

        assert _rows(
            store,
            "SELECT name FROM tool_schemas WHERE definition LIKE '%videoScale%'",
        ) == [(APPIUM,)]

    def test_body_capture_off_keeps_names_and_hashes_but_no_definitions(
        self, store
    ) -> None:
        store.enqueue(_record("r1", tools=SESSION, keep_tool_definitions=False))
        store.close()

        assert _rows(
            store, "SELECT name, definition FROM tool_schemas ORDER BY name"
        ) == [
            ("Bash", None),
            ("Read", None),
            (APPIUM, None),
        ]
        detail = store.get_request("r1")
        assert detail is not None
        assert [tool["name"] for tool in detail["tool_catalogue"]["tools"]] == [
            "Bash",
            "Read",
            APPIUM,
        ]

    def test_capture_turned_back_on_fills_the_missing_definitions(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION, keep_tool_definitions=False))
        store.close()
        again = RequestLogStore(store.db_path, max_rows=10_000)
        # A new array (one more tool) that brings the old tools back.
        again.enqueue(_record("r2", 1, tools=(*SESSION, _tool("Grep"))))
        again.close()

        assert _rows(
            again, "SELECT COUNT(*) FROM tool_schemas WHERE definition IS NULL"
        ) == [(0,)]


class TestTheWriterThread:
    def test_hashing_happens_on_the_writer_thread(self, store, monkeypatch) -> None:
        threads: list[str] = []
        real = ToolFingerprinter.fingerprint

        def spy(self, tools):
            threads.append(threading.current_thread().name)
            return real(self, tools)

        monkeypatch.setattr(ToolFingerprinter, "fingerprint", spy)
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        assert threads == ["mcc-request-log-writer"]

    def test_a_fingerprint_that_fails_never_loses_the_row(
        self, store, monkeypatch
    ) -> None:
        def boom(self, tools):
            raise ValueError("unhashable")

        monkeypatch.setattr(ToolFingerprinter, "fingerprint", boom)
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        detail = store.get_request("r1")
        assert detail is not None
        assert detail["tool_catalogue_sha"] is None


class TestTheDetail:
    def test_the_detail_carries_the_hash_and_the_names_in_order(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.enqueue(_record("r2", 1, tools=SESSION))
        store.close()

        detail = store.get_request("r2")
        catalogue = fingerprint_tools(SESSION)
        assert detail is not None and catalogue is not None
        assert detail["tool_catalogue_sha"] == catalogue.sha.hex()
        shown = detail["tool_catalogue"]
        assert shown["sha"] == catalogue.sha.hex()
        assert shown["tool_count"] == 3
        assert shown["seen"] == 2
        assert shown["tools"] == [
            {"name": member.name, "sha": member.sha.hex()}
            for member in catalogue.members
        ]

    def test_the_detail_never_carries_a_definition(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        detail = store.get_request("r1")
        assert detail is not None
        assert "videoScale" not in repr(detail)
        assert "description" not in repr(detail["tool_catalogue"])


class TestFindingTheRequestsThatCarriedATool:
    def test_one_call_names_every_request_that_carried_it(self, store) -> None:
        for index in range(4):
            store.enqueue(_record(f"with-{index}", index, tools=SESSION))
        store.enqueue(_record("without", 10, tools=SESSION[:2]))
        store.enqueue(_record("other", 11, tools=(*SESSION, _tool("Grep"))))
        store.close()

        found = store.requests_carrying_tool(APPIUM)

        assert [row["id"] for row in found["rows"]] == [
            "other",
            "with-3",
            "with-2",
            "with-1",
            "with-0",
        ]
        assert found["catalogues"] == 2
        assert found["definitions"] == 1
        assert found["seen"] == 5
        assert found["first_seen"] == BASE_TS
        assert found["last_seen"] == BASE_TS + 11
        assert found["has_more"] is False
        assert all(len(row["tool_catalogue_sha"]) == 64 for row in found["rows"])

    def test_every_definition_a_name_has_had_is_followed(self, store) -> None:
        store.enqueue(_record("before", 0, tools=(_tool(APPIUM, VIDEO_SCALE),)))
        store.enqueue(_record("after", 1, tools=(_tool(APPIUM, "^[0-9:]+$"),)))
        store.close()

        found = store.requests_carrying_tool(APPIUM)

        assert found["definitions"] == 2
        assert [row["id"] for row in found["rows"]] == ["after", "before"]

    def test_the_page_is_bounded_and_says_there_is_more(self, store) -> None:
        for index in range(6):
            store.enqueue(_record(f"r{index}", index, tools=SESSION))
        store.close()

        found = store.requests_carrying_tool("Bash", limit=2)

        assert [row["id"] for row in found["rows"]] == ["r5", "r4"]
        assert found["has_more"] is True

    def test_an_unknown_name_finds_nothing(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        found = store.requests_carrying_tool("Nope")

        assert found["catalogues"] == 0
        assert found["rows"] == []
        assert found["seen"] == 0

    def test_a_name_is_matched_exactly_not_as_a_substring(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        assert store.requests_carrying_tool("Bas")["catalogues"] == 0
        assert store.requests_carrying_tool("bash")["catalogues"] == 0


class TestRetention:
    def test_prune_sweeps_catalogues_and_definitions_no_row_carries(
        self, tmp_path
    ) -> None:
        store = RequestLogStore(tmp_path / "requests.db", max_rows=2)
        store.enqueue(_record("old", 0, tools=(_tool("Gone"), _tool("Bash"))))
        for index in range(1, 3):
            store.enqueue(_record(f"new-{index}", index, tools=SESSION))
        store.close()

        assert store.prune() == 1

        assert _rows(store, "SELECT COUNT(*) FROM tool_catalogues") == [(1,)]
        assert sorted(
            name for (name,) in _rows(store, "SELECT name FROM tool_schemas")
        ) == sorted(["Bash", "Read", APPIUM])
        detail = store.get_request("new-2")
        assert detail is not None
        assert detail["tool_catalogue"]["tool_count"] == 3

    def test_the_sweep_is_throttled(self, tmp_path) -> None:
        """``prune`` runs every hundred inserts on a capped log; the sweep reads
        every row, so it runs at most once an hour."""

        store = RequestLogStore(tmp_path / "requests.db", max_rows=1)
        store.enqueue(_record("a", 0, tools=(_tool("A"),)))
        store.enqueue(_record("b", 1, tools=(_tool("B"),)))
        store.close()
        assert store.prune() == 1
        assert _rows(store, "SELECT name FROM tool_schemas") == [("B",)]

        again = RequestLogStore(store.db_path, max_rows=1)
        again._last_tool_sweep = store._last_tool_sweep
        again.enqueue(_record("c", 2, tools=(_tool("C"),)))
        again.close()
        # A fresh store sweeps on its first prune; this one inherited a
        # sweep that just happened.
        assert again.prune() == 1
        assert sorted(
            name for (name,) in _rows(again, "SELECT name FROM tool_schemas")
        ) == ["B", "C"]

    def test_nothing_removed_means_no_sweep(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        assert store.prune() == 0
        assert store._last_tool_sweep is None

    def test_clear_empties_the_tool_tables(self, store) -> None:
        store.enqueue(_record("r1", tools=SESSION))
        store.close()

        store.clear()

        assert _rows(store, "SELECT COUNT(*) FROM tool_catalogues") == [(0,)]
        assert _rows(store, "SELECT COUNT(*) FROM tool_schemas") == [(0,)]


class TestEveryExistingAnswerIsUnchanged:
    """THE equality contract: the same traffic, with and without tools recorded,
    answers every pre-existing question identically."""

    @pytest.fixture
    def pair(self, tmp_path):
        plain = RequestLogStore(tmp_path / "plain.db", max_rows=10_000)
        tooled = RequestLogStore(tmp_path / "tooled.db", max_rows=10_000)
        for index in range(20):
            plain.enqueue(_record(f"r{index}", index))
            tooled.enqueue(
                _record(
                    f"r{index}",
                    index,
                    tools=SESSION if index % 3 else SESSION[:2],
                )
            )
        plain.close()
        tooled.close()
        yield plain, tooled
        plain.close()
        tooled.close()

    def test_stats_rollup_and_rows(self, pair) -> None:
        plain, tooled = pair
        assert plain.stats() == tooled.stats()
        assert plain._stats_from_rows() == tooled._stats_from_rows()

    def test_latency_ttft_and_cost(self, pair) -> None:
        plain, tooled = pair
        assert plain.latency_by_model() == tooled.latency_by_model()
        assert plain.ttft_percentiles() == tooled.ttft_percentiles()
        assert plain.cost_breakdown() == tooled.cost_breakdown()
        assert plain.reasoning_by_model() == tooled.reasoning_by_model()
        assert plain.lifetime() == tooled.lifetime()

    def test_the_list_page(self, pair) -> None:
        plain, tooled = pair
        assert plain.list_requests(limit=50) == tooled.list_requests(limit=50)

    def test_the_default_export(self, pair) -> None:
        plain, tooled = pair
        columns = request_detail_columns(DEFAULT_REQUEST_FIELDS)
        assert "tool_catalogue_sha" not in columns

        def export(store: RequestLogStore) -> list[dict[str, Any]]:
            return list(store.iter_export_rows(columns=columns, need_bodies=False))

        assert export(plain) == export(tooled)

    def test_the_opt_in_export_column_is_hex(self, pair) -> None:
        _plain, tooled = pair
        columns = request_detail_columns([*DEFAULT_REQUEST_FIELDS, "tool_catalogue"])
        assert "tool_catalogue_sha" in columns

        rows = list(tooled.iter_export_rows(columns=columns, need_bodies=False))

        catalogue = fingerprint_tools(SESSION)
        assert catalogue is not None
        by_id = {row["id"]: row["tool_catalogue_sha"] for row in rows}
        assert by_id["r1"] == catalogue.sha.hex()
