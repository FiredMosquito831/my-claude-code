"""The generation window: one clock, two minimums, and the columns they feed.

``GEN_MIN_WINDOW_MS`` and ``GEN_MIN_TOKENS`` live here and nowhere else on the
server; the aggregate applies them in SQL, the export applies them in Python,
and this module holds the two spellings to one answer. Everything below is
about a rate that may only be computed from a measured window -- never from
``duration_ms - ttft_ms``, which divides a count that includes reasoning by a
clock that starts when MCC chose to release the first byte.
"""

import sqlite3
from typing import Any

import pytest

from my_claude_code.core.generation_rate import (
    GEN_MIN_TOKENS,
    GEN_MIN_WINDOW_MS,
    GenerationClock,
    measured_generation_rate,
    throughput_rate,
)
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)
from my_claude_code.core.wire_capture import ResponseShape

# --------------------------------------------------------------- the clock --


def _text_delta(text: str) -> str:
    return (
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","delta":'
        f'{{"type":"text_delta","text":"{text}"}}}}\n\n'
    )


def _thinking_delta(text: str) -> str:
    return (
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","delta":'
        f'{{"type":"thinking_delta","thinking":"{text}"}}}}\n\n'
    )


def _json_delta(partial: str) -> str:
    return (
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","delta":'
        f'{{"type":"input_json_delta","partial_json":"{partial}"}}}}\n\n'
    )


SCAFFOLDING = (
    'event: message_start\ndata: {"type":"message_start","message":{}}\n\n'
    'event: ping\ndata: {"type":"ping"}\n\n',
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    'event: message_delta\ndata: {"type":"message_delta","delta":{},'
    '"usage":{"output_tokens":9}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
)


def _clock() -> tuple[GenerationClock, list[float]]:
    """A clock whose every reading is one tick of a list the test controls."""
    ticks = [100.0]
    return GenerationClock(now=lambda: ticks[0]), ticks


def test_scaffolding_never_starts_the_window() -> None:
    clock, _ticks = _clock()
    for frame in SCAFFOLDING:
        clock.observe(frame)

    assert clock.first_at is None
    assert clock.last_at is None
    assert not clock.measured


def test_the_window_is_the_first_and_last_content_delta() -> None:
    clock, ticks = _clock()
    clock.observe(SCAFFOLDING[0])
    ticks[0] = 101.0
    clock.observe(_text_delta("one"))
    ticks[0] = 104.5
    clock.observe(SCAFFOLDING[1])
    clock.observe(_text_delta("two"))
    ticks[0] = 106.0
    clock.observe(_text_delta("three"))
    clock.observe(SCAFFOLDING[4])

    assert clock.first_at == 101.0
    assert clock.last_at == 106.0


@pytest.mark.parametrize("frame", [_thinking_delta("hmm"), _json_delta('"a"')])
def test_reasoning_and_tool_arguments_are_generation(frame: str) -> None:
    """The numerator counts them, so the denominator has to as well."""
    clock, ticks = _clock()
    clock.observe(frame)
    ticks[0] = 102.0
    clock.observe(_text_delta("answer"))

    assert clock.first_at == 100.0
    assert clock.last_at == 102.0


def test_a_signature_delta_is_not_the_window() -> None:
    """A withheld thinking block signs itself; a signature carries no tokens.

    On its own it leaves the window at a single instant, which no rate is
    computed from -- and that is the honest answer for the replies whose
    reasoning text never arrived (the 65,000-token successes).
    """

    signature = (
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","delta":'
        '{"type":"signature_delta","signature":"abc"}}\n\n'
    )
    clock, _ticks = _clock()
    clock.observe(signature)
    assert clock.measured
    assert clock.first_at == clock.last_at


def test_resetting_forgets_both_ends() -> None:
    clock, ticks = _clock()
    clock.observe(_text_delta("one"))
    ticks[0] = 103.0
    clock.reset()
    clock.observe(_text_delta("two"))

    assert clock.first_at == 103.0
    assert clock.last_at == 103.0


# ------------------------------------------------------------- the shape --


def test_the_shape_records_the_window_on_its_own_clock() -> None:
    shape = ResponseShape(started_at=10.0)
    shape.note_generation(10.25, 11.0)

    assert shape.gen_first_ms == 250
    assert shape.gen_last_ms == 1000
    assert shape.payload()["gen_first_ms"] == 250
    assert shape.payload()["gen_last_ms"] == 1000


def test_the_shape_refuses_an_inverted_window_and_stamps_never_negative() -> None:
    shape = ResponseShape(started_at=10.0)
    shape.note_generation(11.0, 10.5)
    assert shape.gen_first_ms is None

    shape.note_generation(9.0, 9.5)  # earlier than the reply's own start
    assert shape.gen_first_ms == 0
    assert shape.gen_last_ms == 0


def test_an_unmeasured_reply_carries_no_window_keys() -> None:
    shape = ResponseShape(started_at=10.0)
    payload = shape.payload()
    assert "gen_first_ms" not in payload
    assert "gen_last_ms" not in payload
    assert "estimated_tokens" not in payload


def test_an_estimated_count_is_marked() -> None:
    shape = ResponseShape(started_at=10.0)
    shape.note_estimated_tokens()
    assert shape.payload()["estimated_tokens"] is True


# -------------------------------------------------------------- the rules --


def test_both_minimums_are_the_shipped_pair() -> None:
    assert GEN_MIN_WINDOW_MS == 500
    assert GEN_MIN_TOKENS == 8


def test_a_window_below_the_minimum_is_not_a_rate() -> None:
    assert measured_generation_rate(40, 0.0, 499.0) is None
    assert measured_generation_rate(40, 0.0, 500.0) == pytest.approx(80.0)


def test_a_count_below_the_minimum_is_not_a_rate() -> None:
    assert measured_generation_rate(7, 0.0, 1000.0) is None
    assert measured_generation_rate(8, 0.0, 1000.0) == pytest.approx(8.0)


def test_half_a_window_is_never_a_rate() -> None:
    assert measured_generation_rate(40, 0.0, None) is None
    assert measured_generation_rate(40, None, 900.0) is None
    assert measured_generation_rate(None, 0.0, 900.0) is None
    assert measured_generation_rate(40, 900.0, 900.0) is None


def test_throughput_needs_only_the_two_halves() -> None:
    assert throughput_rate(400, 2_000.0) == pytest.approx(200.0)
    assert throughput_rate(0, 2_000.0) is None
    assert throughput_rate(400, 0) is None
    assert throughput_rate(None, 2_000.0) is None


# ---------------------------------------------------------- the stored row --

#: ``(gen_first_ms, gen_last_ms, tokens_out, window is a rate)``
_MEASURED = (1_000.0, 3_000.0, 400, True)
_SHORT = (1_000.0, 1_200.0, 400, False)
_FEW_TOKENS = (1_000.0, 3_000.0, 4, False)
_LEGACY = (None, None, 400, False)


def _attempt(**overrides: Any) -> RouteAttempt:
    defaults: dict[str, Any] = {
        "attempt": 0,
        "provider": "nvidia_nim",
        "model_ref": "nvidia_nim/fast",
        "outcome": RouteAttemptOutcome.SUCCEEDED,
        "duration_ms": 4_000.0,
        "ttft_ms": 200.0,
        "first_reasoning_ms": 50.0,
        "tokens_out": 400,
        "gen_first_ms": _MEASURED[0],
        "gen_last_ms": _MEASURED[1],
    }
    defaults.update(overrides)
    return RouteAttempt(**defaults)


def _record(request_id: str, attempts: tuple[RouteAttempt, ...]) -> RequestRecord:
    return RequestRecord(
        id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        requested_model="claude-sonnet-4-5",
        provider="nvidia_nim",
        resolved_model="test-model",
        stream=True,
        input_text="hello",
        output_text="world",
        tokens_in=10,
        tokens_out=400,
        ttft_ms=12.5,
        duration_ms=4_000.0,
        status="success",
        attempts=attempts,
    )


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    yield store
    store.close()


def test_the_attempt_row_carries_the_window_and_reads_back(store) -> None:
    store.enqueue(_record("req_window", (_attempt(),)))
    store.close()

    row = store.get_request("req_window")
    assert row is not None
    attempt = row["route_attempts"][0]
    assert attempt["gen_first_ms"] == 1_000.0
    assert attempt["gen_last_ms"] == 3_000.0


def test_old_rows_keep_both_columns_null_when_the_alter_completes(
    store, tmp_path
) -> None:
    """The T3 migration: an existing log gains the columns and stays as it was.

    Simulated the only way a test can: the row is written, the two columns are
    taken away to look like a log written by 7.122.1, and the next open is the
    upgrade. The old row must read NULL in both -- "not measured", never a
    zero-length window and never a backfilled guess -- and a second open must
    not try to add them again.
    """

    store.enqueue(
        _record("req_legacy", (_attempt(gen_first_ms=None, gen_last_ms=None),))
    )
    store.close()

    with sqlite3.connect(tmp_path / "requests.db") as conn:
        conn.execute("ALTER TABLE request_attempts DROP COLUMN gen_first_ms")
        conn.execute("ALTER TABLE request_attempts DROP COLUMN gen_last_ms")

    upgraded = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    try:
        row = upgraded.get_request("req_legacy")
        assert row is not None
        attempt = row["route_attempts"][0]
        assert attempt["gen_first_ms"] is None
        assert attempt["gen_last_ms"] is None
        # And nothing was fabricated from the duration it does have.
        group = upgraded.latency_by_model()[0]
        assert group["gen_rate"] is None
        assert group["throughput"] is not None
    finally:
        upgraded.close()

    again = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    try:
        with again._connection() as conn:
            columns = [
                str(row[1])
                for row in conn.execute("PRAGMA table_info(request_attempts)")
            ]
        assert columns.count("gen_first_ms") == 1
        assert columns.count("gen_last_ms") == 1
    finally:
        again.close()


def test_two_opens_of_the_same_log_add_the_columns_once(store, tmp_path) -> None:
    store.close()
    first = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    first.close()
    second = RequestLogStore(tmp_path / "requests.db", max_rows=100)
    try:
        with second._connection() as conn:
            columns = [
                str(row[1])
                for row in conn.execute("PRAGMA table_info(request_attempts)")
            ]
        assert columns.count("gen_first_ms") == 1
        assert columns.count("gen_last_ms") == 1
    finally:
        second.close()


def test_the_group_rate_comes_from_the_two_columns(store) -> None:
    store.enqueue(_record("req_rate", (_attempt(),)))
    store.close()

    rows = store.latency_by_model()
    assert len(rows) == 1
    row = rows[0]
    # 400 tokens over 2,000 ms.
    assert row["gen_rate"] == pytest.approx(200.0, rel=1e-6)
    assert row["gen_measured"] == 1
    assert row["gen_clock_measured"] == 1
    # The same tokens over the whole request.
    assert row["throughput"] == pytest.approx(100.0, rel=1e-6)
    # And the same rule, spelled in Python, agrees.
    assert measured_generation_rate(400, 1_000.0, 3_000.0) == pytest.approx(
        row["gen_rate"], rel=1e-6
    )
    assert throughput_rate(400, 4_000.0) == pytest.approx(row["throughput"], rel=1e-6)


def test_a_legacy_group_has_throughput_and_no_generation_rate(store) -> None:
    store.enqueue(
        _record(
            "req_legacy",
            (_attempt(gen_first_ms=None, gen_last_ms=None),),
        )
    )
    store.close()

    row = store.latency_by_model()[0]
    assert row["gen_rate"] is None
    assert row["gen_measured"] == 0
    assert row["gen_clock_measured"] == 0
    # The whole-request rate needs nothing new, which is the point of it.
    assert row["throughput"] == pytest.approx(100.0, rel=1e-6)


@pytest.mark.parametrize(
    ("first", "last", "tokens"),
    [(_SHORT[0], _SHORT[1], _SHORT[2]), (_FEW_TOKENS[0], _FEW_TOKENS[1], 4)],
)
def test_a_group_below_a_minimum_has_a_window_but_no_rate(
    store, first, last, tokens
) -> None:
    store.enqueue(
        _record(
            "req_short",
            (_attempt(gen_first_ms=first, gen_last_ms=last, tokens_out=tokens),),
        )
    )
    store.close()

    row = store.latency_by_model()[0]
    assert row["gen_clock_measured"] == 1
    assert row["gen_measured"] == 0
    assert row["gen_rate"] is None


def test_a_group_only_part_of_which_was_measured_has_no_rate(store) -> None:
    """Summing one attempt's tokens over another's window is not a rate."""

    store.enqueue(
        _record(
            "req_mixed",
            (
                _attempt(attempt=0),
                _attempt(attempt=1, gen_first_ms=None, gen_last_ms=None),
            ),
        )
    )
    store.close()

    row = store.latency_by_model()[0]
    assert row["attempts"] == 2
    assert row["gen_clock_measured"] == 1
    assert row["gen_rate"] is None


def test_the_export_derives_the_same_two_numbers() -> None:
    from my_claude_code.core.export import compute_attempt_detail_derived

    row: dict[str, Any] = {
        "tokens_out": 400,
        "duration_ms": 4_000.0,
        "gen_first_ms": 1_000.0,
        "gen_last_ms": 3_000.0,
        "outcome": "succeeded",
    }
    compute_attempt_detail_derived(row, ())

    assert row["generation_rate"] == pytest.approx(200.0)
    assert row["throughput"] == pytest.approx(100.0)


def test_a_legacy_export_row_leaves_both_rate_cells_empty() -> None:
    from my_claude_code.core.export import compute_attempt_detail_derived

    row: dict[str, Any] = {
        "tokens_out": 400,
        "duration_ms": 4_000.0,
        "outcome": "succeeded",
    }
    compute_attempt_detail_derived(row, ())

    assert row["generation_rate"] is None
    assert row["throughput"] == pytest.approx(100.0)


def test_the_sql_and_python_rules_agree_over_a_spread_of_windows(store) -> None:
    """One definition, two spellings: the aggregate and the export agree."""

    windows = [
        (0.0, 500.0, 8),
        (0.0, 499.0, 800),
        (0.0, 900.0, 7),
        (100.0, 1_100.0, 800),
        (2_000.0, 2_000.0, 800),
    ]
    for index, (first, last, tokens) in enumerate(windows):
        store.enqueue(
            _record(
                f"req_agree_{index}",
                (
                    _attempt(
                        model_ref=f"nvidia_nim/model-{index}",
                        gen_first_ms=first,
                        gen_last_ms=last,
                        tokens_out=tokens,
                    ),
                ),
            )
        )
    store.close()

    seen = {row["model_ref"]: row for row in store.latency_by_model()}
    for index, (first, last, tokens) in enumerate(windows):
        row = seen[f"nvidia_nim/model-{index}"]
        allowed = [first, last] if first is not None else [None, None]
        expected = measured_generation_rate(tokens, allowed[0], allowed[1])
        if expected is None:
            assert row["gen_rate"] is None, (first, last, tokens)
        else:
            assert row["gen_rate"] == pytest.approx(expected, rel=1e-6)
