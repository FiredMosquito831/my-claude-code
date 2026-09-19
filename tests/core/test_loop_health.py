"""The loop-lag monitor: what it measures, and what it says while the loop is held.

The record is what turns "the server did not answer" into "the server is alive
and working". These tests pin the three things that has to be true of it: a
loop that is keeping up is never called busy, a loop that is held is called busy
*while it is held* rather than only afterwards, and the window it reports starts
when the loop went late rather than when somebody happened to ask.
"""

import asyncio
import time

import pytest

from my_claude_code.core.loop_health import (
    BUSY_MARKER_HEADER,
    BUSY_MARKER_VALUE,
    DEFAULT_BUSY_REASON,
    LoopHealth,
)
from my_claude_code.runtime.loop_heartbeat import (
    LoopHeartbeat,
    busy_health_answer,
    cached_health_answer,
    reset_health_answer,
)


def _record(*, interval: float = 0.1, busy_lag: float = 0.5) -> LoopHealth:
    record = LoopHealth()
    record.configure(interval_seconds=interval, busy_lag_seconds=busy_lag)
    return record


def test_a_record_nobody_is_beating_never_claims_the_loop_is_late() -> None:
    """The guard that keeps /health honest in a process with no monitor.

    Without it the lag is "how long since a beat that never happened", which is
    the process's whole uptime, and every answer would say busy.
    """

    record = _record()

    assert record.armed is False
    assert record.snapshot().busy is False

    record.beat(3.0)
    assert record.armed is True
    assert record.snapshot().busy is True

    record.disarm()
    assert record.snapshot().busy is False


def test_a_loop_that_is_keeping_up_is_never_busy() -> None:
    record = _record()

    for _ in range(20):
        record.beat(0.004)

    snapshot = record.snapshot()
    assert snapshot.busy is False
    assert snapshot.lag_seconds < 0.5
    assert snapshot.busy_since == ""
    assert snapshot.busy_reason == ""
    assert snapshot.as_body_fields() == {}


def test_a_beat_later_than_the_threshold_marks_the_answer_busy() -> None:
    record = _record()

    record.beat(0.9)

    snapshot = record.snapshot()
    assert snapshot.busy is True
    assert snapshot.lag_seconds == pytest.approx(0.9, abs=0.05)
    assert snapshot.busy_reason == DEFAULT_BUSY_REASON
    fields = snapshot.as_body_fields()
    assert fields["busy"] is True
    assert int(str(fields["busy_lag_ms"])) >= 500
    assert str(fields["busy_since"]).endswith("+00:00")


def test_the_lag_counts_the_beat_that_has_not_happened_yet() -> None:
    """The load-bearing one.

    While the loop is held the beat task is not running, so the *recorded* lag
    is whatever the last healthy beat wrote -- zero. A reader served at the far
    end of a hold has to be told about the hold in progress, not about the last
    one that finished.
    """

    record = _record(interval=0.01, busy_lag=0.05)
    record.beat(0.0)
    assert record.snapshot().busy is False

    time.sleep(0.2)

    snapshot = record.snapshot()
    assert snapshot.busy is True
    assert snapshot.lag_seconds >= 0.05


def test_the_busy_window_starts_when_the_loop_went_late() -> None:
    record = _record(interval=0.1, busy_lag=0.2)

    record.beat(3.0)
    first = record.snapshot().busy_since
    record.beat(3.0)

    assert first != ""
    # Still the same window: the loop has not caught up in between.
    assert record.snapshot().busy_since == first


def test_a_loop_that_catches_up_closes_the_window() -> None:
    record = _record(interval=0.1, busy_lag=0.2)
    record.beat(3.0)
    assert record.snapshot().busy is True

    record.beat(0.001)

    snapshot = record.snapshot()
    assert snapshot.busy is False
    assert snapshot.busy_since == ""


def test_zero_turns_the_marker_off_without_stopping_the_measurement() -> None:
    record = _record(busy_lag=0.0)

    lag = record.beat(9.0)

    assert lag == pytest.approx(9.0, abs=0.05)
    assert record.snapshot().busy is False


def test_the_gesture_that_is_running_is_what_a_busy_answer_names() -> None:
    record = _record(interval=0.1, busy_lag=0.2)

    with record.working("300 proxy address(es) are being tested"):
        record.beat(3.0)
        assert record.snapshot().busy_reason == "300 proxy address(es) are being tested"

    # Still named after it ends, because a gesture that held the loop outright
    # is always over by the time anything can read the record -- that first
    # answer is the one that has to explain the gap.
    record.beat(3.0)
    assert record.snapshot().busy_reason == "300 proxy address(es) are being tested"

    # And forgotten the moment the loop catches up.
    record.beat(0.001)
    record.beat(3.0)
    assert record.snapshot().busy_reason == DEFAULT_BUSY_REASON


def test_a_gesture_that_raises_still_stops_naming_itself() -> None:
    record = _record(interval=0.1, busy_lag=0.2)

    with pytest.raises(RuntimeError), record.working("a chain is being saved"):
        raise RuntimeError("upstream said no")

    # It is no longer *running* -- which is what the stack is for -- but it is
    # still what a busy window opened during it is named after.
    record.beat(3.0)
    assert record.snapshot().busy_reason == "a chain is being saved"
    with record.working("something else"):
        assert record.snapshot().busy_reason == "something else"


def test_the_innermost_gesture_is_the_one_reported() -> None:
    record = _record(interval=0.1, busy_lag=0.2)

    with record.working("a bulk add"), record.working("a republish"):
        record.beat(3.0)
        assert record.snapshot().busy_reason == "a republish"


def test_the_cached_answer_is_the_body_every_release_has_returned() -> None:
    reset_health_answer()

    body, headers = cached_health_answer()

    assert body == b'{"status":"healthy"}'
    assert (b"content-type", b"application/json") in headers
    assert (b"content-length", b"20") in headers
    assert not any(key == BUSY_MARKER_HEADER.encode("ascii") for key, _ in headers)


def test_the_busy_answer_adds_the_marker_and_never_removes_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from my_claude_code.core import loop_health as module

    record = _record(interval=0.1, busy_lag=0.2)
    monkeypatch.setattr(module, "_LOOP_HEALTH", record)
    with record.working("a bulk add of 300 addresses"):
        record.beat(2.0)
        body, headers = busy_health_answer()

    fields = json.loads(body)
    assert fields["status"] == "healthy"
    assert fields["busy"] is True
    assert fields["busy_reason"] == "a bulk add of 300 addresses"
    assert (
        BUSY_MARKER_HEADER.encode("ascii"),
        BUSY_MARKER_VALUE.encode("ascii"),
    ) in headers


@pytest.mark.asyncio
async def test_the_heartbeat_measures_a_loop_that_a_blocking_call_held() -> None:
    """The instrument, end to end, against a real hold on a real loop."""

    from my_claude_code.core import loop_health as module

    record = _record(interval=0.02, busy_lag=0.2)
    original = module._LOOP_HEALTH
    module._LOOP_HEALTH = record
    heartbeat = LoopHeartbeat(interval_seconds=0.02)
    try:
        heartbeat.start()
        await asyncio.sleep(0.1)
        assert record.snapshot().busy is False

        time.sleep(0.6)  # the hold: a blocking call on the loop thread
        await asyncio.sleep(0)

        snapshot = record.snapshot()
        assert snapshot.busy is True
        assert snapshot.lag_seconds >= 0.2
    finally:
        await heartbeat.close()
        module._LOOP_HEALTH = original
