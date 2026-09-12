"""The one-time historical cost backfill, and the label it writes.

``cost_usd``/``cost_source`` arrived in 6.54.0 and were deliberately not
backfilled. The reasoning on record -- "pricing 275,000 old requests at today's
rates would produce a confident number that was never anybody's bill" -- is
right about confidence and wrong about availability: the fix is to label the
number, not to refuse it. These tests hold the label, the never-overwrite rule,
the never-zero rule, and the two properties that make a walk over hundreds of
thousands of rows safe on a live server: it resumes, and it never runs twice.
"""

import sqlite3
import time
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    set_cost_backfill_pricer,
)

_MARKER = request_log_module._COST_BACKFILL_KEY
_THROUGH = request_log_module._COST_BACKFILL_THROUGH_KEY

# 2026-08-01 00:00 UTC and 2026-08-02 00:00 UTC: two different UTC days, which
# is what the walk chunks on.
_DAY_ONE = 1785542400.0
_DAY_TWO = 1785628800.0


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "harness": "claude",
        "stream": True,
        "tokens_in": 1000,
        "tokens_out": 100,
        "duration_ms": 120.0,
        "status": "success",
        "ts_epoch": _DAY_ONE,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


class _FlatPricer:
    """A pricer that answers the same way for every row, and counts its calls.

    A class rather than a closure with an attribute bolted on, so the recorded
    calls are a declared field that the type checker can see.
    """

    def __init__(
        self, amount: float | None, source: str | None = "models_dev_backfill"
    ) -> None:
        self.amount = amount
        self.source = source
        self.calls: list[tuple[Any, ...]] = []

    def __call__(
        self,
        provider: str | None,
        model: str | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cache_read: int | None,
        cache_write: int | None,
    ) -> tuple[float | None, str | None]:
        self.calls.append(
            (provider, model, tokens_in, tokens_out, cache_read, cache_write)
        )
        return (self.amount, self.source)


def _seed(path, records: list[RequestRecord]) -> None:
    store = RequestLogStore(path, max_rows=10_000)
    for record in records:
        store.enqueue(record)
    store.close()


def _run_backfill(path, *, timeout: float = 60.0) -> bool:
    """Open a store and let its writer thread finish the walk.

    Driven through the writer's own idle branch rather than by calling the
    method, because *where* it runs is half of what is being asserted: on the
    writer thread, after the first flush, never on a request path.
    """
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


def _meta(path, key: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (key,)
        ).fetchone()
    return None if row is None else str(row[0])


def _costs(path) -> dict[str, tuple[Any, Any]]:
    with sqlite3.connect(path) as conn:
        return {
            str(row[0]): (row[1], row[2])
            for row in conn.execute("SELECT id, cost_usd, cost_source FROM requests")
        }


def test_a_backfilled_row_carries_a_retroactive_cost_source(tmp_path) -> None:
    """A price resolved today for a request from August says so in the column."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("old")])
    set_cost_backfill_pricer(_FlatPricer(0.25))

    assert _run_backfill(path)

    assert _costs(path) == {"old": (0.25, "models_dev_backfill")}


def test_a_row_the_backfill_cannot_price_is_marked_and_not_retried(tmp_path) -> None:
    """``unpriced`` is the record of an answered question, and NULL survives."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("nobody")])
    refuser = _FlatPricer(None, "unpriced")
    set_cost_backfill_pricer(refuser)

    assert _run_backfill(path)

    assert _costs(path) == {"nobody": (None, "unpriced")}
    seen = len(refuser.calls)
    assert seen == 1

    # The marker stops the walk, and the sentinel stops the predicate: neither
    # a second store nor a second pass asks about this row again.
    assert _run_backfill(path)
    assert len(refuser.calls) == seen


def test_the_backfill_never_overwrites_a_row_priced_at_capture(tmp_path) -> None:
    """A live-path price is provenance, and provenance is not re-derived."""
    path = tmp_path / "requests.db"
    _seed(
        path,
        [
            _record("captured", cost_usd=1.5, cost_source="models_dev"),
            _record("reported", cost_usd=2.5, cost_source="provider"),
            _record("old"),
        ],
    )
    set_cost_backfill_pricer(_FlatPricer(0.25))

    assert _run_backfill(path)

    assert _costs(path) == {
        "captured": (1.5, "models_dev"),
        "reported": (2.5, "provider"),
        "old": (0.25, "models_dev_backfill"),
    }


def test_a_price_of_zero_is_stored_as_unpriced_rather_than_as_zero(tmp_path) -> None:
    """The never-zero rule, at the last gate before storage.

    A zero reads as "this request was free", which is a claim only a source
    that publishes a zero may make -- and never a claim a backfill may make
    about a request from six weeks ago. Measured on the real 343,389-row log:
    not one row took this branch.
    """
    path = tmp_path / "requests.db"
    _seed(path, [_record("free")])
    set_cost_backfill_pricer(_FlatPricer(0.0))

    assert _run_backfill(path)

    assert _costs(path) == {"free": (None, "unpriced")}


def test_a_pricer_that_declines_leaves_the_row_alone(tmp_path) -> None:
    """No catalogue is not the same answer as no published rate.

    The pricer returns no source at all when it cannot speak -- a cold
    models.dev cache -- and a row it refused must stay askable, because
    recording "nobody publishes a rate for this" is permanent.
    """
    path = tmp_path / "requests.db"
    _seed(path, [_record("unknowable")])
    set_cost_backfill_pricer(_FlatPricer(None, None))

    # The walk stops rather than spinning, and writes no completion marker.
    assert not _run_backfill(path, timeout=3.0)

    assert _costs(path) == {"unknowable": (None, None)}
    assert _meta(path, _MARKER) is None


def test_no_pricer_means_no_backfill(tmp_path) -> None:
    """``core`` owns no prices, so with nothing injected nothing happens."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("old")])

    assert not _run_backfill(path, timeout=3.0)

    assert _costs(path) == {"old": (None, None)}
    assert _meta(path, _MARKER) is None


def test_the_backfill_resumes_after_an_interrupted_walk(tmp_path) -> None:
    """The predicate is the progress, so a kill costs the uncommitted chunk."""
    path = tmp_path / "requests.db"
    _seed(
        path,
        [
            _record("d1", ts_epoch=_DAY_ONE),
            _record("d2", ts_epoch=_DAY_TWO),
        ],
    )
    # One day committed by hand, with a price the pricer below could never
    # produce: if the resumed walk revisited the row it would overwrite this.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE requests SET cost_usd = 9.0, cost_source = 'models_dev_backfill'"
            " WHERE id = 'd1'"
        )
        conn.execute(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            (_THROUGH, "2026-08-01"),
        )
    pricer = _FlatPricer(0.25)
    set_cost_backfill_pricer(pricer)

    assert _run_backfill(path)

    assert _costs(path) == {
        "d1": (9.0, "models_dev_backfill"),
        "d2": (0.25, "models_dev_backfill"),
    }
    # And the resumed pass never even looked at the finished day.
    assert [call[2] for call in pricer.calls] == [1000]


def test_the_backfill_marker_is_versioned(tmp_path) -> None:
    """A future ladder change forces one rebuild by bumping the name."""
    assert _MARKER.endswith("_v1")
    assert _THROUGH.endswith("_v1")

    path = tmp_path / "requests.db"
    _seed(path, [_record("old")])
    set_cost_backfill_pricer(_FlatPricer(0.25))
    assert _run_backfill(path)

    # A database carrying only an older, differently versioned spelling is not
    # done: nothing a previous name wrote can satisfy the current one, which is
    # what lets a future ladder change force one rebuild by bumping it.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM request_log_meta WHERE key IN (?, ?)", (_MARKER, _THROUGH)
        )
        conn.executemany(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            [
                ("cost_backfilled_at", "1789000000.0"),
                ("cost_backfilled_through", "2026-08-01"),
            ],
        )
        conn.execute("UPDATE requests SET cost_usd = NULL, cost_source = NULL")

    assert _run_backfill(path)
    assert _costs(path) == {"old": (0.25, "models_dev_backfill")}


def test_the_backfill_holds_no_transaction_across_chunks(tmp_path, monkeypatch) -> None:
    """Every chunk commits, so the WAL stays checkpointable throughout."""
    monkeypatch.setattr(request_log_module, "_COST_CHUNK_ROWS", 2)
    path = tmp_path / "requests.db"
    _seed(path, [_record(f"r{index}") for index in range(6)])

    committed: list[int] = []

    def price(
        provider: str | None,
        model: str | None,
        tokens_in: int | None,
        tokens_out: int | None,
        cache_read: int | None,
        cache_write: int | None,
    ) -> tuple[float | None, str | None]:
        # A separate connection, so it sees only what has been committed.
        with sqlite3.connect(path) as conn:
            committed.append(
                conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE cost_source IS NOT NULL"
                ).fetchone()[0]
            )
        return (0.25, "models_dev_backfill")

    set_cost_backfill_pricer(price)
    assert _run_backfill(path)

    # Six rows in chunks of two: the second chunk sees the first one's commit
    # and the third sees both. A single transaction over the walk would leave
    # every observation at zero.
    assert committed[:2] == [0, 0]
    assert max(committed) >= 4
    assert len(_costs(path)) == 6


def test_the_backfill_does_not_run_twice(tmp_path) -> None:
    """The completion marker is the whole of "already done"."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("old")])
    pricer = _FlatPricer(0.25)
    set_cost_backfill_pricer(pricer)
    assert _run_backfill(path)
    first = len(pricer.calls)

    # A row nulled afterwards stays null: the marker is checked before the
    # rows are, exactly as the harness backfill's is.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE requests SET cost_usd = NULL, cost_source = NULL")
    assert _run_backfill(path)

    assert len(pricer.calls) == first
    assert _costs(path) == {"old": (None, None)}


@pytest.mark.parametrize("column", ["cost_usd", "cost_source"])
def test_the_backfill_leaves_a_half_answered_row_alone(tmp_path, column) -> None:
    """The predicate needs both halves NULL, so neither is ever overwritten."""
    path = tmp_path / "requests.db"
    _seed(path, [_record("half")])
    with sqlite3.connect(path) as conn:
        if column == "cost_usd":
            conn.execute("UPDATE requests SET cost_usd = 3.0 WHERE id = 'half'")
        else:
            conn.execute(
                "UPDATE requests SET cost_source = 'litellm' WHERE id = 'half'"
            )
    set_cost_backfill_pricer(_FlatPricer(0.25))

    assert _run_backfill(path)

    stored = _costs(path)["half"]
    assert stored == ((3.0, None) if column == "cost_usd" else (None, "litellm"))


def test_backfilled_costs_are_estimated_and_never_reported(tmp_path) -> None:
    """Reported and estimated are summed apart, and a backfill is an estimate.

    ``reported_usd`` matches ``cost_source = 'provider'`` and nothing else, so
    the twenty rows a host really did report stay the twenty rows the card
    calls reported however much history gets priced afterwards.
    """
    path = tmp_path / "requests.db"
    _seed(
        path,
        [
            _record("reported", cost_usd=2.0, cost_source="provider"),
            _record("fresh", cost_usd=1.0, cost_source="models_dev"),
            _record("later", cost_usd=0.5, cost_source="models_dev_backfill"),
            _record("voted", cost_usd=0.25, cost_source="cross_provider_backfill"),
            _record("nobody", cost_usd=None, cost_source="unpriced"),
        ],
    )
    store = RequestLogStore(path, max_rows=10_000)
    try:
        totals = store.cost_breakdown()["totals"]
    finally:
        store.close()

    assert totals["reported_usd"] == 2.0
    assert totals["estimated_usd"] == 1.75
    # The denominator counts rows that carry a figure, so the sentinel is in
    # ``requests`` and not in ``priced`` -- which is exactly what makes the
    # coverage readout on the cost card true.
    assert totals["priced"] == 4
    assert totals["requests"] == 5


def test_unpriced_rows_are_not_listed_as_a_pricing_source(tmp_path) -> None:
    """ "Priced by" is a list of sources that priced something."""
    path = tmp_path / "requests.db"
    _seed(
        path,
        [
            _record("later", cost_usd=0.5, cost_source="models_dev_backfill"),
            _record("nobody", cost_usd=None, cost_source="unpriced"),
        ],
    )
    store = RequestLogStore(path, max_rows=10_000)
    try:
        by_source = store.cost_breakdown()["by_source"]
    finally:
        store.close()

    assert [row["key"] for row in by_source] == ["models_dev_backfill"]
