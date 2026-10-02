"""The freeze line fires at >= 2.0 s and not below (item 1, fix F4 / decision 8).

``LoopHealth._note_busy_locked`` opens a busy window while the loop is late by
at least ``busy_lag_seconds`` (0.5 s), and closes it the moment a beat or a
snapshot measures a lag back under that. ``take_closed_freeze`` is how
``runtime/loop_heartbeat.py`` learns a window just closed and logs one INFO
line -- but only when the window lasted at least
:data:`FREEZE_LOG_THRESHOLD_SECONDS` (2.0 s); a shorter hold still only gets
the existing DEBUG line.
"""

from my_claude_code.core.loop_health import FREEZE_LOG_THRESHOLD_SECONDS, LoopHealth


def test_a_hold_of_at_least_two_seconds_is_reported_as_a_closed_freeze() -> None:
    record = LoopHealth()
    record.configure(interval_seconds=0.1, busy_lag_seconds=0.5)

    # Open a busy window well past the freeze threshold...
    record.beat(lag_seconds=2.5)
    assert record.take_closed_freeze() is None, "the window is still open"

    # ...then close it. The whole 2.5 s (plus the tiny real elapsed time
    # between these two calls) is what should be reported.
    record.beat(lag_seconds=0.0)
    closed = record.take_closed_freeze()

    assert closed is not None
    assert closed.duration_seconds >= FREEZE_LOG_THRESHOLD_SECONDS
    assert closed.started_at


def test_a_hold_under_two_seconds_is_not_reported_as_a_closed_freeze() -> None:
    """Above busy_lag_seconds (so DEBUG still fires), below the freeze line."""
    record = LoopHealth()
    record.configure(interval_seconds=0.1, busy_lag_seconds=0.5)

    record.beat(lag_seconds=1.0)
    record.beat(lag_seconds=0.0)

    assert record.take_closed_freeze() is None


def test_take_closed_freeze_is_consumed_exactly_once() -> None:
    record = LoopHealth()
    record.configure(interval_seconds=0.1, busy_lag_seconds=0.5)

    record.beat(lag_seconds=3.0)
    record.beat(lag_seconds=0.0)

    assert record.take_closed_freeze() is not None
    assert record.take_closed_freeze() is None
