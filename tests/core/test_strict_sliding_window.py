"""Direct tests for :class:`core.rate_limit.StrictSlidingWindowLimiter`."""

import asyncio
import time

import pytest

import my_claude_code.core.rate_limit as rate_limit_module
from my_claude_code.core.rate_limit import StrictSlidingWindowLimiter


@pytest.mark.asyncio
async def test_strict_window_allows_burst_then_blocks():
    lim = StrictSlidingWindowLimiter(rate_limit=2, rate_window=0.2)
    await lim.acquire()
    await lim.acquire()
    start = time.monotonic()
    await lim.acquire()
    assert time.monotonic() - start >= 0.15


@pytest.mark.asyncio
async def test_strict_window_async_context_manager():
    lim = StrictSlidingWindowLimiter(rate_limit=1, rate_window=0.15)

    async def run():
        async with lim:
            pass

    await run()
    start = time.monotonic()
    await run()
    assert time.monotonic() - start >= 0.1


@pytest.mark.asyncio
async def test_rejected_conditional_acquisition_does_not_consume_capacity():
    lim = StrictSlidingWindowLimiter(rate_limit=1, rate_window=60)

    assert await lim.acquire_if(lambda: False) is False

    await asyncio.wait_for(lim.acquire(), timeout=0.1)


@pytest.mark.asyncio
async def test_conditional_acquisition_records_predicate_commit_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window starts where the request committed, not where it queued.

    White-box on ``_times`` because that deque *is* the window: the slot is
    reserved by the pump before the condition runs, and the reservation has to
    be moved to the instant the condition said yes or the window would start
    one loop resumption early.
    """
    now = 0.0
    lim = StrictSlidingWindowLimiter(rate_limit=1, rate_window=10)

    def advance_during_condition() -> bool:
        nonlocal now
        now = 100.0
        return True

    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: now)

    assert await lim.acquire_if(advance_during_condition) is True

    assert list(lim._times) == [100.0]


def test_strict_window_rejects_invalid_config():
    with pytest.raises(ValueError):
        StrictSlidingWindowLimiter(rate_limit=-1, rate_window=1.0)
    with pytest.raises(ValueError):
        StrictSlidingWindowLimiter(rate_limit=1, rate_window=0.0)


@pytest.mark.asyncio
async def test_a_zero_limit_admits_immediately_and_records_nothing():
    """0 is the shipped default: no proactive pacing at all."""
    lim = StrictSlidingWindowLimiter(rate_limit=0, rate_window=60)

    assert lim.unlimited is True
    for _ in range(500):
        await asyncio.wait_for(lim.acquire(), timeout=0.5)
    assert list(lim._times) == []
    assert await lim.acquire_if(lambda: False) is False
