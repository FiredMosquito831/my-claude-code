"""The four things ``status='cancelled'`` covers, and the two spellings of them.

``cancelled`` means the consumer stopped reading before the stream finished,
and that one word was covering four unrelated events: the client's own idle
watchdog firing while nothing readable had arrived, the same watchdog firing
after MCC had committed a response and then gone quiet, the user stopping a
stream part of the way through, and MCC restarting under an open stream.

Every label here is a projection of columns the log already stored, so these
tests also stand for every historical row: nothing new is captured, and a row
written a year ago classifies exactly as one written today.
"""

import sqlite3
from typing import Any

import pytest

from my_claude_code.core.cancelled_reasons import (
    CANCELLED_SUB_LABELS,
    CLIENT_GAVE_UP_WAITING,
    COMMITTED_THEN_SILENT,
    SERVER_RESTART,
    STATUS_FILTER_VALUES,
    STOPPED_MID_ANSWER,
    classify_cancelled,
    restart_boundaries,
    split_status_filter,
    sub_label_case_sql,
)
from my_claude_code.core.request_log import RequestLogStore, RequestRecord


def _cancelled(**overrides: Any) -> str | None:
    kwargs: dict[str, Any] = {
        "status": "cancelled",
        "ts_epoch": 1_000.0,
        "ttft_ms": None,
        "duration_ms": 300_000.0,
        "output_chars": 0,
        "thinking_chars": 0,
        "boundaries": (),
    }
    kwargs.update(overrides)
    return classify_cancelled(**kwargs)


# --------------------------------------------------------------- the labels


def test_a_request_that_is_not_cancelled_has_no_reason() -> None:
    """A successful request was not cancelled for any reason, not for none."""

    assert _cancelled(status="success") is None
    assert _cancelled(status="error") is None
    assert _cancelled(status=None) is None


def test_no_frame_at_all_is_the_client_giving_up() -> None:
    assert _cancelled(ttft_ms=None) == CLIENT_GAVE_UP_WAITING


def test_a_first_frame_at_the_very_end_is_also_giving_up() -> None:
    """``ttft_ms`` is the first frame of any kind, not the first content.

    A frame that lands at 95% of the request's own duration arrived at the
    instant the client had already gone, so nothing readable ever reached it.
    This is the first investigation's own silence rule; on the measured log it
    is 186 of the 265 rows that have a frame and no content.
    """

    assert _cancelled(ttft_ms=299_000.0, duration_ms=300_000.0) == (
        CLIENT_GAVE_UP_WAITING
    )


def test_a_first_frame_with_time_to_spare_and_no_content_is_committed_then_silent() -> (
    None
):
    assert _cancelled(ttft_ms=16_700.0, duration_ms=313_400.0) == COMMITTED_THEN_SILENT


def test_output_characters_mean_the_answer_was_stopped_part_way() -> None:
    assert _cancelled(ttft_ms=200.0, output_chars=140) == STOPPED_MID_ANSWER


def test_reasoning_characters_count_as_content_too() -> None:
    """A turn that streamed thinking and then stopped was not silent.

    Calling it "committed, then silent" would be a false sentence about a
    request the user watched moving. On the measured log this is 213 of 935.
    """

    assert _cancelled(ttft_ms=200.0, thinking_chars=900) == STOPPED_MID_ANSWER


def test_a_restart_beats_every_other_label() -> None:
    """The one of the four an operator can act on, so it wins outright."""

    boundaries = (1_310.0,)
    # Ends at 1_000 + 310 s = 1_310, right on the boundary, and it had already
    # delivered an answer -- which on its own would be "stopped mid-answer".
    assert (
        _cancelled(
            duration_ms=310_000.0,
            output_chars=5_000,
            ttft_ms=200.0,
            boundaries=boundaries,
        )
        == SERVER_RESTART
    )
    assert _cancelled(duration_ms=310_000.0, boundaries=boundaries) == SERVER_RESTART


def test_a_restart_five_seconds_away_still_counts_and_six_does_not() -> None:
    assert _cancelled(duration_ms=300_000.0, boundaries=(1_305.0,)) == SERVER_RESTART
    assert _cancelled(duration_ms=300_000.0, boundaries=(1_306.0,)) != SERVER_RESTART


def test_an_unmeasured_duration_cannot_be_blamed_on_a_restart() -> None:
    """NULL is "not measured", never "ended at zero"."""

    assert _cancelled(duration_ms=None, boundaries=(1_000.0,)) == CLIENT_GAVE_UP_WAITING


def test_the_four_labels_are_exhaustive_over_every_shape() -> None:
    """A cancelled row always gets exactly one, so a breakdown always sums."""

    seen = set()
    for ttft in (None, 0.0, 100.0, 299_000.0):
        for duration in (None, 0.0, 300_000.0):
            for output in (None, 0, 12):
                for thinking in (None, 0, 12):
                    for boundaries in ((), (1_300.0,)):
                        label = _cancelled(
                            ttft_ms=ttft,
                            duration_ms=duration,
                            output_chars=output,
                            thinking_chars=thinking,
                            boundaries=boundaries,
                        )
                        assert label in CANCELLED_SUB_LABELS
                        seen.add(label)
    assert seen == set(CANCELLED_SUB_LABELS)


# ----------------------------------------------------------- the boundaries


def test_the_newest_session_end_is_not_a_restart_boundary() -> None:
    """Otherwise "the log ends here" would read as "the server restarted"."""

    assert restart_boundaries([(0.0, 100.0), (110.0, 200.0)]) == (100.0,)


def test_a_session_start_is_never_its_own_boundary() -> None:
    assert restart_boundaries([(0.0, 100.0)]) == ()
    assert restart_boundaries([]) == ()


# ------------------------------------------------------- the status filter


def test_the_three_original_status_values_are_unchanged() -> None:
    assert STATUS_FILTER_VALUES[:3] == ("success", "error", "cancelled")
    for value in ("success", "error", "cancelled"):
        assert split_status_filter(value) == (value, None)


def test_a_sub_label_rides_on_the_status_value() -> None:
    assert split_status_filter("cancelled:server_restart") == (
        "cancelled",
        SERVER_RESTART,
    )


def test_an_unknown_sub_label_is_left_for_the_caller_to_reject() -> None:
    """It must not silently become "cancelled" and a filter nobody asked for."""

    assert split_status_filter("cancelled:nonsense") == ("cancelled:nonsense", None)
    assert split_status_filter("success:server_restart") == (
        "success:server_restart",
        None,
    )
    assert split_status_filter(None) == (None, None)
    assert "cancelled:nonsense" not in STATUS_FILTER_VALUES


# --------------------------------------- the two spellings are one definition


_SHAPES: tuple[dict[str, Any], ...] = (
    {"ttft_ms": None, "duration_ms": 300_000.0, "output_chars": 0},
    {"ttft_ms": None, "duration_ms": None, "output_chars": None},
    {"ttft_ms": 299_500.0, "duration_ms": 300_000.0, "output_chars": 0},
    {"ttft_ms": 16_700.0, "duration_ms": 313_400.0, "output_chars": 0},
    {"ttft_ms": 200.0, "duration_ms": 9_000.0, "output_chars": 140},
    {"ttft_ms": 200.0, "duration_ms": 9_000.0, "thinking_chars": 900},
    {"ttft_ms": 0.0, "duration_ms": 0.0, "output_chars": 0},
    {"ttft_ms": 100.0, "duration_ms": 310_000.0, "output_chars": 0},
)


def test_the_sql_and_python_classifiers_agree(tmp_path) -> None:
    """One definition, two spellings, and this is what keeps them one.

    Python answers a row at a time -- the list chip, the modal, the export --
    and SQL answers the questions Python cannot: the whole-population
    breakdown, and the list filter, which has to be in the ``WHERE`` or the
    pager would page over the wrong rows.
    """

    path = tmp_path / "agree.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE requests (id TEXT, status TEXT, ts_epoch REAL, ttft_ms REAL,"
        " duration_ms REAL, output_chars INTEGER, thinking_chars INTEGER)"
    )
    conn.execute(
        "CREATE TABLE server_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_at REAL NOT NULL, last_seen_at REAL NOT NULL)"
    )
    # One restart (the first session's end, followed by a second session) and
    # one session that is simply the newest, whose end is not a boundary.
    sessions = [(0.0, 1_310.0), (1_320.0, 9_999.0)]
    conn.executemany(
        "INSERT INTO server_sessions (started_at, last_seen_at) VALUES (?, ?)",
        sessions,
    )
    boundaries = restart_boundaries(sessions)
    assert boundaries == (1_310.0,)

    for index, shape in enumerate(_SHAPES):
        conn.execute(
            "INSERT INTO requests (id, status, ts_epoch, ttft_ms, duration_ms,"
            " output_chars, thinking_chars) VALUES (?, 'cancelled', ?, ?, ?, ?, ?)",
            (
                f"r{index}",
                1_000.0,
                shape.get("ttft_ms"),
                shape.get("duration_ms"),
                shape.get("output_chars"),
                shape.get("thinking_chars"),
            ),
        )
    conn.commit()

    from_sql = {
        str(row["id"]): str(row["reason"])
        for row in conn.execute(
            f"SELECT id, {sub_label_case_sql()} AS reason FROM requests"
        )
    }
    conn.close()

    from_python = {
        f"r{index}": classify_cancelled(
            status="cancelled",
            ts_epoch=1_000.0,
            boundaries=boundaries,
            **shape,
        )
        for index, shape in enumerate(_SHAPES)
    }
    assert from_sql == from_python
    # Not a vacuous pass: the shapes above have to reach more than one label.
    assert len(set(from_sql.values())) >= 3


# ------------------------------------------------------------- via the store


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=500)
    yield store
    store.close()


def _cancelled_record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "status": "cancelled",
        "ttft_ms": None,
        "duration_ms": 600_000.0,
        "output_chars": 0,
        "thinking_chars": 0,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _populate(store: RequestLogStore) -> None:
    store.enqueue(_cancelled_record("gave_up"))
    store.enqueue(_cancelled_record("silent", ttft_ms=16_700.0, duration_ms=313_400.0))
    store.enqueue(
        _cancelled_record("mid", ttft_ms=200.0, duration_ms=9_000.0, output_chars=140)
    )
    store.enqueue(
        _cancelled_record("fine", status="success", output_chars=900, ttft_ms=100.0)
    )


def test_a_cancelled_list_row_carries_its_reason_and_others_carry_none(store) -> None:
    _populate(store)
    store.close()
    rows = {row["id"]: row for row in store.list_requests(limit=50)[0]}
    assert rows["gave_up"]["cancel_reason"] == CLIENT_GAVE_UP_WAITING
    assert rows["silent"]["cancel_reason"] == COMMITTED_THEN_SILENT
    assert rows["mid"]["cancel_reason"] == STOPPED_MID_ANSWER
    # Present and empty, not absent: the column exists on every row.
    assert rows["fine"]["cancel_reason"] is None


def test_the_detail_row_answers_the_same_way_the_list_does(store) -> None:
    _populate(store)
    store.close()
    assert store.get_request("silent")["cancel_reason"] == COMMITTED_THEN_SILENT
    assert store.get_request("fine")["cancel_reason"] is None


def test_the_breakdown_counts_every_cancelled_row_exactly_once(store) -> None:
    _populate(store)
    store.close()
    breakdown = store.cancelled_breakdown()
    assert breakdown["total"] == 3
    assert breakdown["counts"] == {
        SERVER_RESTART: 0,
        STOPPED_MID_ANSWER: 1,
        CLIENT_GAVE_UP_WAITING: 1,
        COMMITTED_THEN_SILENT: 1,
    }
    # Every label has a row, including the ones at zero: "none of these were
    # restarts" is an answer.
    assert set(breakdown["counts"]) == set(CANCELLED_SUB_LABELS)


def test_a_page_filtered_to_some_other_status_has_nothing_to_break_down(store) -> None:
    _populate(store)
    store.close()
    assert store.cancelled_breakdown(status="success")["total"] == 0


def test_the_status_filter_selects_one_sub_label_and_cancelled_still_selects_all(
    store,
) -> None:
    _populate(store)
    store.close()
    rows, total = store.list_requests(limit=50, status="cancelled")
    assert total == 3
    rows, total = store.list_requests(
        limit=50, status="cancelled:committed_then_silent"
    )
    assert total == 1
    assert [row["id"] for row in rows] == ["silent"]
    assert store.count_requests(status="cancelled:stopped_mid_answer") == 1
    assert store.count_requests(status="cancelled") == 3


def test_a_sub_label_filter_forces_the_row_scan_because_no_rollup_has_it(
    store,
) -> None:
    """The rollups are counters keyed on ``status`` and stay that way.

    Teaching them a fifth dimension would mean rebuilding every historical
    bucket for 0.4% of traffic, so the sub-label is answered by the scan --
    exactly the way a free-text search already is.
    """

    _populate(store)
    store.close()
    assert store.stats(status="cancelled")["served_from"] == "rollup"
    narrowed = store.stats(status="cancelled:stopped_mid_answer")
    assert narrowed["served_from"] == "rows"
    assert narrowed["cancelled"] == 1
    assert narrowed["total"] == 1


def test_a_server_restart_is_read_off_the_session_history(tmp_path) -> None:
    """The one label that is not on the request row, end to end."""

    store = RequestLogStore(tmp_path / "restart.db", max_rows=500)
    try:
        store.enqueue(
            _cancelled_record(
                "cut", ts_epoch=1_000.0, duration_ms=310_000.0, output_chars=42
            )
        )
        store.close()
        with sqlite3.connect(store.db_path) as conn:
            conn.executemany(
                "INSERT INTO server_sessions (started_at, last_seen_at) VALUES (?, ?)",
                [(0.0, 1_310.0), (1_320.0, 9_999.0)],
            )
        # The store registers a session of its own when it opens, so the
        # boundary list is not only the two rows above -- what matters is that
        # the first session's end is in it.
        assert 1_310.0 in store.restart_boundaries()
        row = store.get_request("cut")
        assert row is not None
        assert row["cancel_reason"] == SERVER_RESTART
        assert store.cancelled_breakdown()["counts"][SERVER_RESTART] == 1
    finally:
        store.close()
