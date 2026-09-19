"""What each finished gesture cost the loop, and how the record says it.

``/health`` answers "busy, because <gesture>" while a hold is happening. This
is the other half: once the gesture is over, what the worst lateness was. The
readout on Limits & Resilience is built from exactly this.
"""

import time

import pytest

from my_claude_code.core.loop_health import (
    GESTURE_HISTORY,
    LoopHealth,
)


@pytest.fixture
def record() -> LoopHealth:
    health = LoopHealth()
    health.configure(interval_seconds=0.1, busy_lag_seconds=0.5)
    return health


def test_nothing_has_finished_until_something_does(record: LoopHealth) -> None:
    assert record.recent_gestures() == ()


def test_a_gesture_records_the_worst_lateness_measured_while_it_ran(
    record: LoopHealth,
) -> None:
    record.beat(0.0)
    with record.working("a route is being paused"):
        record.beat(0.2)
        record.beat(0.9)
        record.beat(0.1)
    gestures = record.recent_gestures()
    assert len(gestures) == 1
    assert gestures[0].reason == "a route is being paused"
    assert gestures[0].max_lag_seconds == pytest.approx(0.9, abs=0.05)


def test_a_gesture_that_held_the_loop_outright_still_measures_the_hold(
    record: LoopHealth,
) -> None:
    """No beat can run during a blocking hold, so the exit is the measurement.

    A wall-clock hold, not an iteration count: the thing under test is how long
    the loop went without its beat, which is a duration.
    """

    record.beat(0.0)
    with record.working("a blocking gesture"):
        time.sleep(0.35)
    gesture = record.recent_gestures()[-1]
    # 0.35 s of hold, less the 0.1 s the beat was entitled to.
    assert gesture.max_lag_seconds >= 0.2
    assert gesture.duration_seconds >= 0.35


def test_a_gesture_that_held_nothing_reports_that(record: LoopHealth) -> None:
    record.beat(0.0)
    with record.working("a fast gesture"):
        record.beat(0.001)
    gesture = record.recent_gestures()[-1]
    assert gesture.max_lag_seconds < 0.1


def test_nested_gestures_both_carry_the_lateness(record: LoopHealth) -> None:
    """A nested gesture holding the loop is the outer gesture holding it."""

    record.beat(0.0)
    with record.working("outer"):
        with record.working("inner"):
            record.beat(0.7)
        record.beat(0.0)
    by_reason = {gesture.reason: gesture for gesture in record.recent_gestures()}
    assert by_reason["inner"].max_lag_seconds == pytest.approx(0.7, abs=0.05)
    assert by_reason["outer"].max_lag_seconds == pytest.approx(0.7, abs=0.05)


def test_a_gesture_that_raised_is_still_recorded(record: LoopHealth) -> None:
    record.beat(0.0)
    with pytest.raises(RuntimeError), record.working("a gesture that failed"):
        record.beat(0.6)
        raise RuntimeError("no")
    assert record.recent_gestures()[-1].reason == "a gesture that failed"


def test_the_history_is_bounded(record: LoopHealth) -> None:
    record.beat(0.0)
    for index in range(GESTURE_HISTORY * 3):
        with record.working(f"gesture {index}"):
            record.beat(0.0)
    gestures = record.recent_gestures()
    assert len(gestures) == GESTURE_HISTORY
    assert gestures[-1].reason == f"gesture {GESTURE_HISTORY * 3 - 1}"


def test_the_body_fields_are_the_shape_the_dashboard_reads(record: LoopHealth) -> None:
    record.beat(0.0)
    with record.working("the model catalogue is being refreshed"):
        record.beat(1.25)
    fields = record.recent_gestures()[-1].as_body_fields()
    assert fields["reason"] == "the model catalogue is being refreshed"
    assert fields["max_lag_ms"] == 1250
    assert isinstance(fields["duration_ms"], int)
    assert str(fields["finished_at"]).endswith("+00:00")


def test_a_reset_forgets_the_history(record: LoopHealth) -> None:
    record.beat(0.0)
    with record.working("something"):
        record.beat(0.0)
    record.reset()
    assert record.recent_gestures() == ()


def test_the_busy_reason_still_names_the_innermost_running_gesture(
    record: LoopHealth,
) -> None:
    """The 7.27.0 contract, unchanged by the bookkeeping added beside it."""

    record.beat(0.0)
    with record.working("outer"):
        with record.working("inner"):
            record.beat(0.9)
            assert record.snapshot().busy_reason == "inner"
        record.beat(0.9)
        assert record.snapshot().busy_reason == "outer"


def test_the_dashboard_reads_its_own_route_not_the_status_payload() -> None:
    """A readout must not pull the whole admin status behind it.

    ``/admin/api/status`` rebuilds the provider status and the cached-model
    map; the page stopped polling it when the global status header went, and
    this card must not put it back. The keys are still on that payload for
    anything reading the server rather than the page.
    """

    from pathlib import Path

    from my_claude_code.api import admin_routes

    assert any(
        getattr(route, "path", "") == "/admin/api/loop-health"
        for route in admin_routes.router.routes
    )
    script = (
        Path(admin_routes.__file__).resolve().parent / "admin_static" / "admin.js"
    ).read_text(encoding="utf-8")
    assert 'api("/admin/api/loop-health")' in script
    assert 'api("/admin/api/status")' not in script
