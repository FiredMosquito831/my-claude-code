"""What the watchdog costs when nothing is stuck, which is nearly always.

The claim the module docstring makes is that a healthy pass is a dictionary
snapshot, one counter read per request and a tuple comparison -- and that no
stack is built for a request that has not crossed the threshold. Both halves
are asserted: the second by counting, which is the one that would actually
regress if somebody moved the stack build above the threshold check.
"""

import time

import pytest

from my_claude_code.core import request_tasks
from my_claude_code.core.request_tasks import PHASE_AWAITING_FIRST_BYTE, RequestProgress
from my_claude_code.runtime.stall_watchdog import StallWatchdog

IN_FLIGHT = 100


@pytest.fixture
def hundred_requests():
    reads = {"count": 0}

    def reader() -> RequestProgress:
        reads["count"] += 1
        return RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, provider="p")

    for index in range(IN_FLIGHT):
        entry = request_tasks.register(
            request_id=f"req_{index}",
            endpoint="/v1/messages",
            protocol="anthropic",
            stream=True,
            harness="claude",
            requested_model="mcc/best",
            progress=reader,
        )
        assert entry is not None
        entry.last_progress_mono = 1000.0
    return reads


def _watchdog(stall: float) -> StallWatchdog:
    return StallWatchdog(
        stall_seconds=lambda: stall,
        interval_seconds=30,
        log_max_bytes=lambda: 0,
        retain_files=lambda: 0,
    )


def test_a_healthy_pass_over_a_hundred_requests_is_cheap(hundred_requests) -> None:
    watchdog = _watchdog(300.0)
    watchdog.sweep(now=1000.0)  # first look, establishes the baseline
    start = time.perf_counter()
    for tick in range(10):
        assert watchdog.sweep(now=1000.0 + tick) == []
    elapsed = time.perf_counter() - start
    # Ten passes over a hundred in-flight requests. It measures about 2 ms in
    # total here; the assertion is two orders of magnitude above that so a
    # loaded CI runner cannot make it flaky, and it would still catch a stack
    # build that had wandered above the threshold check.
    assert elapsed < 0.5, elapsed


def test_no_stack_is_built_for_a_request_that_has_not_crossed(
    hundred_requests, monkeypatch
) -> None:
    built = {"count": 0}
    import my_claude_code.core.stuck_requests as stuck

    real = stuck.describe_task

    def counted(task, **kwargs):
        built["count"] += 1
        return real(task, **kwargs)

    monkeypatch.setattr(stuck, "describe_task", counted)
    watchdog = _watchdog(300.0)
    for tick in range(10):
        watchdog.sweep(now=1000.0 + tick)
    assert built["count"] == 0


def test_the_progress_reader_is_called_exactly_once_per_request_per_pass(
    hundred_requests,
) -> None:
    watchdog = _watchdog(300.0)
    watchdog.sweep(now=1000.0)
    hundred_requests["count"] = 0
    watchdog.sweep(now=1001.0)
    assert hundred_requests["count"] == IN_FLIGHT
