"""``total_usd`` is a total, and the two halves it adds are disjoint.

SPEC ``DESIGN-REVAMP-OPTIONS.md``, ADDENDUM ROUND 4 §4: the Analytics cost tile
must show the window's WHOLE cost, reported plus estimated, instead of the
reported half alone. The safety argument for that change is one sentence --
*"the two halves are disjoint sets of rows"* -- and the only way to know it is
true is to build a window where a double count would be visible and read the
three numbers back.

So the fixture here is built to make a mistake ARITHMETIC rather than
plausible:

* four rows priced by a host (``cost_source = 'provider'``), summing to 2.00;
* three rows priced by MCC's own ladder (``cost_source = 'models_dev'`` and
  ``'litellm'``), summing to 0.375;
* one row that was never priced at all -- ``cost_usd IS NULL`` and
  ``cost_source IS NULL``. It belongs to NEITHER half. If the total were
  ``SUM(cost_usd)`` over everything the answer would still be 2.375 (that row
  has no dollars to contribute), so the test asserts on the membership too:
  the row counts in ``requests`` and in neither sum.

A double count shows up as 2.375 vs 4.375 in the first comparison. A row
silently folded into the wrong half shows up in the per-half assertions. A
total computed in JavaScript would show up as anything at all when one half is
NULL -- which is the second test, the window where no host ever reported a
price, because that is the owner's own log in miniature.
"""

import time
from typing import Any

from my_claude_code.core.request_log import RequestLogStore, RequestRecord


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
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 120.0,
        "status": "success",
        # The default is the row nobody priced: no dollars, no source, which is
        # what a request served by a host that publishes no rate looks like.
        "cost_usd": None,
        "cost_source": None,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _totals(path, records: list[RequestRecord]) -> dict[str, Any]:
    """Write the window with one store, read it back with a settled one.

    ``close()`` is what flushes the writer thread, so the read happens on a
    store whose rows are all committed -- the same order every neighbouring
    request-log test uses, and the reason none of these numbers are races.
    """

    writer = RequestLogStore(path, max_rows=10_000)
    for record in records:
        writer.enqueue(record)
    writer.close()
    reader = RequestLogStore(path, max_rows=10_000)
    try:
        return reader.cost_breakdown()
    finally:
        reader.close()


def _disjoint_window() -> list[RequestRecord]:
    """Four reported, three estimated, one never priced."""

    now = time.time()
    rows: list[RequestRecord] = []
    # Reported: 4 x 0.50 = 2.00. The host said what it charged.
    rows.extend(
        _record(
            f"reported-{index}",
            ts_epoch=now - index * 60,
            provider="open_router",
            cost_usd=0.50,
            cost_source="provider",
        )
        for index in range(4)
    )
    # Estimated: 0.125 + 0.125 + 0.125 = 0.375, from two different rungs, so the
    # "everything that is not provider" side is exercised rather than assumed.
    rows.append(
        _record(
            "est-models-dev",
            ts_epoch=now - 300,
            cost_usd=0.125,
            cost_source="models_dev",
        )
    )
    rows.append(
        _record(
            "est-litellm-a", ts_epoch=now - 360, cost_usd=0.125, cost_source="litellm"
        )
    )
    rows.append(
        _record(
            "est-litellm-b", ts_epoch=now - 420, cost_usd=0.125, cost_source="litellm"
        )
    )
    # Never priced: in neither half, still a request.
    rows.append(_record("unpriced", ts_epoch=now - 480, provider="unknown_host"))
    return rows


def test_the_total_is_both_halves_and_neither_counts_a_row_twice(tmp_path) -> None:
    totals = _totals(tmp_path / "requests.db", _disjoint_window())["totals"]

    assert totals["reported_usd"] == 2.00
    assert totals["estimated_usd"] == 0.375
    # The whole point: 2.00 + 0.375, not 2.00, and not 4.375 (a double count).
    assert totals["total_usd"] == 2.375
    assert totals["requests"] == 8
    # The unpriced row is in the denominator and in neither numerator.
    assert totals["priced"] == 7
    assert totals["priced"] < totals["requests"]


def test_the_unpriced_row_moves_no_dollar_figure(tmp_path) -> None:
    """Add a second never-priced row: every sum stays put, ``requests`` grows.

    This is the property a reader depends on and a ``COALESCE(cost_usd, 0)``
    would quietly destroy: an unpriced request is INCOMPLETE, not free. It must
    make the share worse, never the bill better.
    """

    window = _disjoint_window()
    with_unpriced = [
        *window,
        _record("unpriced-2", ts_epoch=time.time() - 540, provider="unknown_host"),
    ]

    before = _totals(tmp_path / "before.db", window)["totals"]
    after = _totals(tmp_path / "after.db", with_unpriced)["totals"]

    assert after["reported_usd"] == before["reported_usd"]
    assert after["estimated_usd"] == before["estimated_usd"]
    assert after["total_usd"] == before["total_usd"]
    assert after["priced"] == before["priced"]
    assert after["requests"] == before["requests"] + 1


def test_a_window_nobody_reported_prices_still_has_a_total(tmp_path) -> None:
    """The owner's own log in miniature: no host reports, everything estimated.

    ``reported_usd`` is NULL here (SQLite sums zero rows to NULL, and NULL is
    the honest answer for "no host priced anything"), while the total is the
    estimated figure. That asymmetry is why the total is summed in SQL: a
    caller adding two possibly-NULL fields in JavaScript would print ``$NaN``
    or ``$0.00`` on exactly the window this change exists to fix.
    """

    now = time.time()
    rows = [
        _record(
            f"est-{index}",
            ts_epoch=now - index * 60,
            cost_usd=1.25,
            cost_source="models_dev",
        )
        for index in range(3)
    ]
    rows.append(_record("unpriced", ts_epoch=now - 300))

    totals = _totals(tmp_path / "requests.db", rows)["totals"]

    assert totals["reported_usd"] is None
    assert totals["estimated_usd"] == 3.75
    assert totals["total_usd"] == 3.75
    assert totals["priced"] == 3
    assert totals["requests"] == 4


def test_the_groups_agree_with_the_total(tmp_path) -> None:
    """Every per-dimension row sums to the totals -- the other half of "safe".

    The tile reads ``totals``; the tables below it read ``by_provider``. If the
    two disagreed the page would show a headline that its own breakdown cannot
    account for, which is a different wrong number from the one this change
    fixes and just as unusable. Each group's ``total_usd`` is summed by the same
    SQL expression over the same rows, so the sums must match exactly here: the
    fixture's dollars are all exact binary doubles (0.5 and 0.125), so
    re-associating the addition cannot change a single bit.
    """

    payload = _totals(tmp_path / "requests.db", _disjoint_window())

    totals = payload["totals"]
    for dimension in ("by_provider", "by_model", "by_harness"):
        rows = payload[dimension]
        assert rows, dimension
        assert sum(row["reported_usd"] or 0.0 for row in rows) == totals["reported_usd"]
        assert (
            sum(row["estimated_usd"] or 0.0 for row in rows) == totals["estimated_usd"]
        )
        assert sum(row["total_usd"] or 0.0 for row in rows) == totals["total_usd"]
        assert sum(row["requests"] for row in rows) == totals["requests"]
