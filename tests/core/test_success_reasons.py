"""The two shapes a ``success`` with no answer takes, and the two spellings of them.

``success`` means the client received a valid message, and it keeps meaning
that. A small share of those messages carried no text and no tool call: most
of them reasoning and nothing else ("thought only"), a few nothing at all
("empty"). Every label here is a projection of columns the log already stored,
so these tests also stand for every historical row: nothing new is captured,
and a row written a year ago classifies exactly as one written today.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.cancelled_reasons import (
    STATUS_FILTER_VALUES,
    split_status_filter,
)
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)
from my_claude_code.core.success_reasons import (
    EMPTY,
    REQUEST_STATUS_FILTER_VALUES,
    SUCCESS_REASON_SOURCE_COLUMNS,
    SUCCESS_STATUS_FILTER_VALUES,
    SUCCESS_SUB_LABEL_EXPLANATION,
    SUCCESS_SUB_LABEL_TEXT,
    SUCCESS_SUB_LABELS,
    THOUGHT_ONLY,
    classify_success,
    classify_success_row,
    split_success_status_filter,
    success_sub_label_case_sql,
)

# ------------------------------------------------------------ the classifier

#: (shape, expected label). Missing keys are NULL columns.
_TABLE: tuple[tuple[dict[str, Any], str | None], ...] = (
    # The 199 of 201: reasoning, then nothing.
    ({"thinking_chars": 18_800, "tokens_out": 4_200}, THOUGHT_ONLY),
    ({"output_chars": 0, "tool_call_count": None, "thinking_chars": 1}, THOUGHT_ONLY),
    # The 2 of 201: nothing at all.
    ({"output_chars": 0, "thinking_chars": 0, "tokens_out": 0}, EMPTY),
    ({}, EMPTY),
    ({"output_chars": None, "tokens_out": None, "thinking_chars": None}, EMPTY),
    # Answered: text, a tool call, or both. No label, whatever else they had.
    ({"output_chars": 2, "tokens_out": 18}, None),
    ({"tool_call_count": 1, "tokens_out": 90}, None),
    ({"tool_call_count": 3, "thinking_chars": 900}, None),
    ({"output_chars": 140, "thinking_chars": 900}, None),
    # A row from before ``tool_call_count`` existed: no text, output tokens,
    # nothing counted as reasoning. Probably a tool call nobody counted, so
    # neither label is a true sentence about it.
    ({"output_chars": 0, "tokens_out": 169}, None),
    # MCC's own local answers are empty by design and never labelled.
    ({"optimization": "suggestion_mode_skip"}, None),
    ({"optimization": "title_generation_skip", "thinking_chars": 5}, None),
    ({"optimization": "probe_auto_response", "tokens_out": 0}, None),
)


@pytest.mark.parametrize(("shape", "expected"), _TABLE)
def test_the_classifier_table(shape: dict[str, Any], expected: str | None) -> None:
    assert classify_success(status="success", **shape) == expected


@pytest.mark.parametrize("status", ["error", "cancelled", None, "success:empty"])
def test_only_a_success_has_a_success_label(status: str | None) -> None:
    assert classify_success(status=status) is None
    assert classify_success(status=status, thinking_chars=900) is None


def test_the_row_form_reads_the_same_columns() -> None:
    for shape, expected in _TABLE:
        assert classify_success_row({"status": "success", **shape}) == expected
    assert set(SUCCESS_REASON_SOURCE_COLUMNS) == {
        "status",
        "output_chars",
        "tool_call_count",
        "thinking_chars",
        "tokens_out",
        "optimization",
    }


def test_every_label_has_words_and_a_sentence() -> None:
    assert SUCCESS_SUB_LABELS == (THOUGHT_ONLY, EMPTY)
    assert set(SUCCESS_SUB_LABEL_TEXT) == set(SUCCESS_SUB_LABELS)
    assert set(SUCCESS_SUB_LABEL_EXPLANATION) == set(SUCCESS_SUB_LABELS)
    assert SUCCESS_SUB_LABEL_TEXT == {THOUGHT_ONLY: "thought only", EMPTY: "empty"}


# --------------------------------------- the two spellings are one definition


def test_the_sql_and_python_classifiers_agree(tmp_path) -> None:
    """One definition, two spellings, and this is what keeps them one."""

    conn = sqlite3.connect(tmp_path / "agree.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE requests (id TEXT, status TEXT, output_chars INTEGER,"
        " tool_call_count INTEGER, thinking_chars INTEGER, tokens_out INTEGER,"
        " optimization TEXT)"
    )
    for index, (shape, _) in enumerate(_TABLE):
        conn.execute(
            "INSERT INTO requests VALUES (?, 'success', ?, ?, ?, ?, ?)",
            (
                f"r{index}",
                shape.get("output_chars"),
                shape.get("tool_call_count"),
                shape.get("thinking_chars"),
                shape.get("tokens_out"),
                shape.get("optimization"),
            ),
        )
    conn.commit()
    from_sql = {
        str(row["id"]): row["reason"]
        for row in conn.execute(
            f"SELECT id, {success_sub_label_case_sql()} AS reason FROM requests"
        )
    }
    conn.close()
    from_python = {
        f"r{index}": classify_success(status="success", **shape)
        for index, (shape, _) in enumerate(_TABLE)
    }
    assert from_sql == from_python
    # Not a vacuous pass: all three outcomes are reached.
    assert set(from_sql.values()) == {THOUGHT_ONLY, EMPTY, None}


# ------------------------------------------------------- the status filter


def test_every_old_value_is_still_accepted_and_first() -> None:
    assert REQUEST_STATUS_FILTER_VALUES[: len(STATUS_FILTER_VALUES)] == (
        STATUS_FILTER_VALUES
    )
    assert SUCCESS_STATUS_FILTER_VALUES == ("success:thought_only", "success:empty")
    assert REQUEST_STATUS_FILTER_VALUES[len(STATUS_FILTER_VALUES) :] == (
        SUCCESS_STATUS_FILTER_VALUES
    )


def test_a_success_sub_label_rides_on_the_status_value() -> None:
    assert split_success_status_filter("success:thought_only") == (
        "success",
        THOUGHT_ONLY,
    )
    assert split_success_status_filter("success:empty") == ("success", EMPTY)
    for value in ("success", "error", "cancelled", None, ""):
        assert split_success_status_filter(value) == (value, None)


def test_an_unknown_value_is_left_for_the_caller_to_reject() -> None:
    assert split_success_status_filter("success:nonsense") == (
        "success:nonsense",
        None,
    )
    assert split_success_status_filter("cancelled:thought_only") == (
        "cancelled:thought_only",
        None,
    )
    # A cancelled sub-label passes through untouched, so the cancelled path
    # sees exactly what it saw before.
    assert split_success_status_filter("cancelled:server_restart") == (
        "cancelled:server_restart",
        None,
    )
    # And the cancelled splitter still refuses a success value.
    assert split_status_filter("success:thought_only") == (
        "success:thought_only",
        None,
    )
    assert "success:nonsense" not in REQUEST_STATUS_FILTER_VALUES


# ------------------------------------------------------------- via the store


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=500)
    yield store
    store.close()


def _record(request_id: str, ts_epoch: float, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "custom_agnes",
        "resolved_model": "agnes-3.0-flash",
        "stream": True,
        "status": "success",
        "ts_epoch": ts_epoch,
        "ttft_ms": 300.0,
        "duration_ms": 9_000.0,
        "output_chars": 0,
        "thinking_chars": 0,
        "tokens_out": 0,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _populate(store: RequestLogStore) -> None:
    base = 1_700_000_000.0
    store.enqueue(_record("thought", base + 1, thinking_chars=18_800, tokens_out=4_200))
    store.enqueue(_record("empty", base + 2))
    store.enqueue(_record("text", base + 3, output_chars=2, tokens_out=18))
    store.enqueue(_record("tool", base + 4, tool_call_count=1, tokens_out=90))
    store.enqueue(
        _record(
            "local",
            base + 5,
            provider=None,
            resolved_model=None,
            optimization="suggestion_mode_skip",
        )
    )
    store.enqueue(
        _record(
            "cut", base + 6, status="cancelled", ttft_ms=None, duration_ms=600_000.0
        )
    )
    store.enqueue(
        _record("err", base + 7, status="error", error_kind="upstream", tokens_out=None)
    )


def test_a_list_row_carries_its_label_and_every_other_row_carries_none(store) -> None:
    _populate(store)
    store.close()
    rows = {row["id"]: row for row in store.list_requests(limit=50)[0]}
    assert {key: row["success_reason"] for key, row in rows.items()} == {
        "thought": THOUGHT_ONLY,
        "empty": EMPTY,
        "text": None,
        "tool": None,
        "local": None,
        "cut": None,
        "err": None,
    }
    # The cancelled label is untouched by the new one, and vice versa.
    assert rows["cut"]["cancel_reason"] == "client_gave_up_waiting"
    assert rows["thought"]["cancel_reason"] is None


def test_the_detail_row_answers_the_same_way_the_list_does(store) -> None:
    _populate(store)
    store.close()
    for request_id, expected in (
        ("thought", THOUGHT_ONLY),
        ("empty", EMPTY),
        ("text", None),
        ("local", None),
    ):
        row = store.get_request(request_id)
        assert row is not None
        assert row["success_reason"] == expected, request_id


def test_the_status_filter_narrows_and_plain_success_still_selects_all(store) -> None:
    _populate(store)
    store.close()
    _, total = store.list_requests(limit=50, status="success")
    assert total == 5
    rows, total = store.list_requests(limit=50, status="success:thought_only")
    assert (total, [row["id"] for row in rows]) == (1, ["thought"])
    rows, total = store.list_requests(limit=50, status="success:empty")
    assert (total, [row["id"] for row in rows]) == (1, ["empty"])
    assert store.count_requests(status="success:empty") == 1
    # The cancelled filter is exactly what it was.
    assert store.count_requests(status="cancelled") == 1
    assert store.count_requests(status="cancelled:client_gave_up_waiting") == 1


def test_the_filter_is_in_the_where_so_the_pager_pages_over_the_right_rows(
    store,
) -> None:
    """A filter applied after ``LIMIT`` would hand back short or empty pages."""

    base = 1_700_000_000.0
    thought_ids = []
    for index in range(30):
        if index % 4 == 0:
            request_id = f"t{index:02d}"
            thought_ids.append(request_id)
            store.enqueue(_record(request_id, base + index, thinking_chars=500))
        else:
            store.enqueue(
                _record(f"a{index:02d}", base + index, output_chars=10, tokens_out=5)
            )
    store.close()
    newest_first = list(reversed(thought_ids))
    seen: list[str] = []
    for offset in range(0, len(newest_first), 3):
        rows, total = store.list_requests(
            limit=3, offset=offset, status="success:thought_only"
        )
        assert total == len(newest_first)
        seen.extend(row["id"] for row in rows)
    assert seen == newest_first


def test_the_breakdown_sums_to_what_it_claims(store) -> None:
    _populate(store)
    store.close()
    breakdown = store.no_answer_breakdown()
    assert breakdown == {
        # Every success, the local answer included: it is a success.
        "successes": 5,
        "total": 2,
        "counts": {THOUGHT_ONLY: 1, EMPTY: 1},
        "selected": None,
    }
    assert breakdown["total"] == sum(breakdown["counts"].values())
    # The same population the stats card counts.
    assert store.stats()["success"] == breakdown["successes"]
    # Hiding local answers removes one success and no label.
    hidden = store.no_answer_breakdown(local="hide")
    assert (hidden["successes"], hidden["total"]) == (4, 2)


def test_the_breakdown_follows_the_status_filter_like_the_cancelled_one(
    store,
) -> None:
    _populate(store)
    store.close()
    assert store.no_answer_breakdown(status="error")["total"] == 0
    assert store.no_answer_breakdown(status="cancelled:client_gave_up_waiting") == {
        "successes": 0,
        "total": 0,
        "counts": {THOUGHT_ONLY: 0, EMPTY: 0},
        "selected": None,
    }
    narrowed = store.no_answer_breakdown(status="success:empty")
    # Narrowed to one label, the page still sees both.
    assert narrowed["counts"] == {THOUGHT_ONLY: 1, EMPTY: 1}
    assert narrowed["selected"] == EMPTY


def test_a_sub_label_filter_forces_the_row_scan_and_totals_stay_as_they_were(
    store,
) -> None:
    _populate(store)
    store.close()
    plain = store.stats(status="success")
    assert plain["served_from"] == "rollup"
    assert plain["success"] == 5
    narrowed = store.stats(status="success:thought_only")
    assert narrowed["served_from"] == "rows"
    assert (narrowed["total"], narrowed["success"]) == (1, 1)
    # The unfiltered totals are the rollup's, unchanged: three statuses, no
    # fourth.
    everything = store.stats()
    assert (everything["success"], everything["error"], everything["cancelled"]) == (
        5,
        1,
        1,
    )


def test_the_breakdown_is_cached_per_filter(store) -> None:
    _populate(store)
    store.close()
    first = store.no_answer_breakdown(local="hide")
    # A row written behind the store's back (the writer is closed): a
    # thought-only success, upstream.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
            " requested_model, provider, stream, status, thinking_chars)"
            " VALUES ('late', 1700000100.0, '2023-11-14T22:15:00+00:00',"
            " '/v1/messages', 'anthropic', 'claude-sonnet-4-5', 'custom_agnes',"
            " 1, 'success', 5)"
        )
    # Inside the 5 s window the answer is the cached one ...
    assert store.no_answer_breakdown(local="hide") == first
    # ... and a different filter is a different question.
    assert store.no_answer_breakdown(local="all")["counts"][THOUGHT_ONLY] == 2


def test_nothing_is_stored_the_status_column_keeps_three_values(store) -> None:
    _populate(store)
    store.close()
    with sqlite3.connect(store.db_path) as conn:
        statuses = {row[0] for row in conn.execute("SELECT status FROM requests")}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    assert statuses == {"success", "error", "cancelled"}
    assert "success_reason" not in columns


def test_the_export_iterator_labels_rows_whose_columns_it_was_not_asked_for(
    store,
) -> None:
    """The SELECT tops itself up with the columns the label is read from."""

    _populate(store)
    store.close()
    rows = {
        row["id"]: row
        for row in store.iter_export_rows(
            columns=["id", "ts_epoch", "stream"], need_bodies=False
        )
    }
    assert rows["thought"]["success_reason"] == THOUGHT_ONLY
    assert rows["empty"]["success_reason"] == EMPTY
    assert rows["local"]["success_reason"] is None


def test_the_attempts_export_carries_the_parent_label(store) -> None:
    base = 1_700_000_000.0

    def attempt(outcome: RouteAttemptOutcome) -> RouteAttempt:
        return RouteAttempt(
            attempt=0,
            provider="custom_agnes",
            model_ref="custom_agnes/agnes-3.0-flash",
            outcome=outcome,
            duration_ms=9_000.0,
        )

    store.enqueue(
        _record(
            "thought",
            base + 1,
            thinking_chars=18_800,
            attempts=(attempt(RouteAttemptOutcome.FAILED),),
        )
    )
    store.enqueue(
        _record(
            "text",
            base + 2,
            output_chars=2,
            tokens_out=18,
            attempts=(attempt(RouteAttemptOutcome.SUCCEEDED),),
        )
    )
    store.close()
    labels = {
        row["request_id"]: row["request_success_reason"]
        for row in store.iter_export_attempt_rows()
    }
    assert labels == {"thought": THOUGHT_ONLY, "text": None}


def test_the_dashboard_and_the_store_agree_on_the_success_sub_labels() -> None:
    """Two spellings of one vocabulary, in two languages."""

    admin_js = (
        Path(request_log_module.__file__).parent.parent
        / "api"
        / "admin_static"
        / "admin.js"
    ).read_text(encoding="utf-8")
    for label, text in SUCCESS_SUB_LABEL_TEXT.items():
        assert f'{label}: "{text}"' in admin_js, label
    for label, sentence in SUCCESS_SUB_LABEL_EXPLANATION.items():
        assert f"  {label}:" in admin_js, label
        # The sentence itself, joined the way admin.js splits it over lines.
        words = sentence.split()
        assert words[0] in admin_js and words[-1] in admin_js
    index_html = (
        Path(request_log_module.__file__).parent.parent
        / "api"
        / "admin_static"
        / "index.html"
    ).read_text(encoding="utf-8")
    for value in SUCCESS_STATUS_FILTER_VALUES:
        assert f'<option value="{value}">' in index_html, value
