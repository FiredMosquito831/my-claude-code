"""``request_attempts.proxy_label``: which address an attempt went out through.

Half of what makes a proxy chain real is being able to tell whether it worked.
The ladder already holds the whole sequence inside ``params.ladder``; this
column is the last rung of it denormalised out, exactly as ``ladder_tries`` is,
so the analytics breakdown can group by address without scanning JSON.

Two rules it exists to hold: the value is a label and never a URL, and NULL
means "not measured" rather than "direct".
"""

import sqlite3
from typing import Any

from my_claude_code.core.request_log import (
    _ATTEMPT_ADDED_COLUMNS,
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)
from my_claude_code.core.upstream_ladder import (
    AttemptLadder,
    LadderTry,
    ladder_payload,
    ladder_proxy_label,
)


def _record(request_id: str, proxy_label: str | None) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        harness="claude",
        stream=True,
        tokens_in=10,
        tokens_out=20,
        duration_ms=120.0,
        status="success",
        attempts=(
            RouteAttempt(
                attempt=1,
                provider="nvidia_nim",
                model_ref="nvidia_nim/test-model",
                outcome=RouteAttemptOutcome.SUCCEEDED,
                proxy_label=proxy_label,
            ),
        ),
    )


def _column_rows(path, request_id: str) -> list[Any]:
    conn = sqlite3.connect(path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT proxy_label FROM request_attempts WHERE request_id = ?",
                (request_id,),
            )
        ]
    finally:
        conn.close()


def test_proxy_label_is_added_by_a_guarded_alter() -> None:
    """The pattern eleven columns before it have used.

    A guarded ``ALTER TABLE`` rather than a schema change, so an install that
    already has a log gains the column without a migration step and without a
    backfill it could not compute.
    """

    added = dict(_ATTEMPT_ADDED_COLUMNS)

    assert "proxy_label" in added
    assert added["proxy_label"] == (
        "ALTER TABLE request_attempts ADD COLUMN proxy_label TEXT"
    )


def test_an_attempt_stores_and_reads_back_the_address_it_used(tmp_path) -> None:
    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.enqueue(_record("r-proxied", "203.0.113.7:1080"))
    # Closing is what drains the writer; the read has to come after it, from a
    # store that opens the same file.
    store.close()

    assert _column_rows(path, "r-proxied") == ["203.0.113.7:1080"]

    reader = RequestLogStore(path, max_rows=100)
    try:
        detail = reader.get_request("r-proxied")
    finally:
        reader.close()
    assert detail is not None
    assert detail["route_attempts"][0]["proxy_label"] == "203.0.113.7:1080"


def test_null_means_not_measured_and_direct_is_a_value(tmp_path) -> None:
    """Two different facts, and conflating them would make the column useless.

    An attempt on a provider with no chain measured nothing about its egress.
    An attempt on the Direct rung of a chain measured that the operator chose
    this machine's own address -- which is the row that proves a fallback to
    Direct actually happened.
    """

    path = tmp_path / "requests.db"
    store = RequestLogStore(path, max_rows=100)
    store.enqueue(_record("r-chainless", None))
    store.enqueue(_record("r-direct", "direct"))
    store.close()

    assert _column_rows(path, "r-chainless") == [None]
    assert _column_rows(path, "r-direct") == ["direct"]


def test_the_label_never_contains_credentials(tmp_path) -> None:
    """A proxy URL may carry ``user:pass``; this column may not.

    The masking happens once, where the chain is resolved, so there is no path
    from a stored proxy password to this table -- but the row is worth pinning
    anyway, because it is the table an operator exports and mails to somebody.
    """

    ladder = AttemptLadder(
        tries=[
            LadderTry(status=402, proxy="203.0.113.7:1080"),
            LadderTry(status=200, proxy="direct"),
        ]
    )
    payload = ladder_payload(ladder)

    assert [row["proxy"] for row in payload["tries"]] == [
        "203.0.113.7:1080",
        "direct",
    ]
    assert "@" not in str(payload)
    # The last real try is the one the attempt's verdict belongs to.
    assert ladder_proxy_label(payload) == "direct"


def test_a_ladder_with_no_address_denormalises_to_nothing(tmp_path) -> None:
    """A provider with no chain leaves the column NULL, not "direct"."""

    payload = ladder_payload(AttemptLadder(tries=[LadderTry(status=200)]))

    assert "proxy" not in payload["tries"][0]
    assert ladder_proxy_label(payload) is None
