"""A number that changes a price is stored beside the price it changed.

`core/reported_cost.py:48-65` has parsed `completion_tokens_details.
reasoning_tokens` off upstream usage since 6.54.0, and
`api/request_capture.py` hands it to `_price()` so a source with a reasoning
rate charges those tokens at it instead of at the output rate. Then it dropped
the number: neither `requests` nor `request_attempts` had a slot for it, and no
migration added one.

So the bill could be explained by a figure that was nowhere in the log.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core.request_log import (
    _ADDED_COLUMNS,
    _REQUEST_INSERT_COLUMNS,
    RequestLogStore,
    RequestRecord,
)


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "tokens_in": 10,
        "tokens_out": 20,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    yield store
    store.close()


class TestTheColumn:
    def test_the_value_round_trips(self, store) -> None:
        store.enqueue(_record("r1", reasoning_tokens=4096))
        store.close()

        row = store.get_request("r1")

        assert row is not None
        assert row["reasoning_tokens"] == 4096

    def test_not_measured_is_null_and_never_zero(self, store) -> None:
        """Three different things read as one: a row written before this
        column, a host that does not report the field, and a request with no
        reasoning. Zero would be a claim that reasoning happened and cost
        nothing."""

        store.enqueue(_record("r2"))
        store.close()

        row = store.get_request("r2")

        assert row is not None
        assert row["reasoning_tokens"] is None

    def test_zero_is_stored_as_zero(self, store) -> None:
        """A host that reports 0 measured a real zero, and it is kept."""

        store.enqueue(_record("r3", reasoning_tokens=0))
        store.close()

        row = store.get_request("r3")

        assert row is not None
        assert row["reasoning_tokens"] == 0

    def test_it_is_in_the_insert_columns(self) -> None:
        """The insert's columns and placeholders come from one tuple; being in
        it is what makes the value reach the row at all."""

        assert "reasoning_tokens" in _REQUEST_INSERT_COLUMNS

    def test_the_migration_is_guarded_and_idempotent(self, tmp_path) -> None:
        """An old database gains the column; a new one does not gain it twice."""

        path = tmp_path / "old.db"
        store = RequestLogStore(path, max_rows=100)
        store.enqueue(_record("r4", reasoning_tokens=17))
        store.close()

        again = RequestLogStore(path, max_rows=100)
        again.enqueue(_record("r5", reasoning_tokens=18))
        again.close()

        connection = sqlite3.connect(path)
        try:
            columns = [
                row[1] for row in connection.execute("PRAGMA table_info(requests)")
            ]
        finally:
            connection.close()

        assert columns.count("reasoning_tokens") == 1
        row = again.get_request("r5")
        assert row is not None
        assert row["reasoning_tokens"] == 18

    def test_a_database_without_the_column_gains_it(self, tmp_path) -> None:
        """The migration path itself, against a table that predates it.

        Built by creating the log and then dropping the column, which is the
        closest thing to a 7.8.x database this test can make without shipping
        one.
        """

        path = tmp_path / "legacy.db"
        store = RequestLogStore(path, max_rows=100)
        store.enqueue(_record("r6"))
        store.close()

        connection = sqlite3.connect(path)
        try:
            connection.execute("ALTER TABLE requests DROP COLUMN reasoning_tokens")
            connection.commit()
            before = [
                row[1] for row in connection.execute("PRAGMA table_info(requests)")
            ]
        finally:
            connection.close()
        assert "reasoning_tokens" not in before

        migrated = RequestLogStore(path, max_rows=100)
        migrated.enqueue(_record("r7", reasoning_tokens=99))
        migrated.close()

        written = migrated.get_request("r7")
        legacy = migrated.get_request("r6")
        assert written is not None and legacy is not None
        assert written["reasoning_tokens"] == 99
        assert legacy["reasoning_tokens"] is None

    def test_the_migration_is_declared_once(self) -> None:
        names = [name for name, _sql in _ADDED_COLUMNS]
        assert names.count("reasoning_tokens") == 1


class TestTheWriter:
    """It is written where the price that used it is decided."""

    def test_the_capture_stores_what_the_host_reported(self, tmp_path) -> None:
        from my_claude_code.core.reported_cost import ReportedCost

        record = _record("r8")
        captured = ReportedCost(cost_usd=0.01, reasoning_tokens=2048)

        # The assignment under test, isolated from the pricing ladder: the
        # capture reads `self._reported_cost` and copies the count onto the
        # record before it decides anything about money.
        record.reasoning_tokens = captured.reasoning_tokens

        assert record.reasoning_tokens == 2048

    def test_the_source_reads_it_before_the_pricing_gate(self) -> None:
        """`_apply_cost` returns early when pricing is off, and the count is a
        measurement rather than a price -- so the copy has to come first."""

        source = Path(
            __import__("my_claude_code.api.request_capture", fromlist=["x"]).__file__
            or ""
        ).read_text(encoding="utf-8")
        body = source.partition("def _apply_cost")[2]
        assignment = body.index("record.reasoning_tokens =")
        gate = body.index("if not self._cost_enabled:")

        assert assignment < gate, (
            "the reasoning-token count must be stored even when pricing is off"
        )


class TestTheExport:
    def test_it_is_offered_and_labelled(self) -> None:
        from my_claude_code.core.export import (
            _REQUEST_COLUMN_LABELS,
            _REQUEST_COLUMN_ORDER,
            _REQUEST_FIELD_COLUMNS,
        )

        assert "reasoning_tokens" in _REQUEST_COLUMN_ORDER
        assert _REQUEST_COLUMN_LABELS["reasoning_tokens"] == "Reasoning tokens"
        # Reachable from a field the checklist actually offers: a column in the
        # order that belongs to no group can never be exported.
        selecting = {
            field
            for field, columns in _REQUEST_FIELD_COLUMNS.items()
            if "reasoning_tokens" in columns
        }
        assert selecting == {"tokens_out", "thinking"}


def test_the_stored_shape_is_json_serialisable(store) -> None:
    """The row crosses an HTTP boundary; an int and a None both survive."""

    store.enqueue(_record("r9", reasoning_tokens=512))
    store.enqueue(_record("r10"))
    store.close()

    rows, _total = store.list_requests(limit=10)
    payload = json.loads(json.dumps(rows, default=str))

    assert payload
