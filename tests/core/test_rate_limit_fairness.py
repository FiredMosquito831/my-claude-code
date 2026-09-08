"""The proactive window admits in arrival order, and 0 turns it off.

Before 6.62.0 :class:`StrictSlidingWindowLimiter` was a sleep-and-re-contend
loop: every blocked caller slept to the same instant and then raced for one
lock, so arrival order was discarded and a caller could lose that race
repeatedly. On the machine that reported the bug, 24 concurrent requests
measured p50 938 ms against p95 54 397 ms -- a 58x spread on one batch, which
is starvation rather than queueing. These are the tests that fail if the
lottery ever comes back.
"""

import asyncio
import time
from itertools import pairwise

import pytest

from my_claude_code.core.rate_limit import (
    UNLIMITED_RATE_LIMIT,
    StrictSlidingWindowLimiter,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter


@pytest.mark.asyncio
async def test_waiters_are_admitted_in_arrival_order() -> None:
    """32 waiters on a 4-per-window limiter, admitted in the order they queued.

    The rate is unchanged and still binding: with four slots per window, 32
    waiters take eight windows to drain, and the spec's "no waiter waits more
    than one window" is measured the only way it can be against a real rate --
    no waiter waits more than one window longer than the waiter that arrived
    before it. That is exactly the property starvation broke.
    """

    window = 0.1
    limiter = StrictSlidingWindowLimiter(rate_limit=4, rate_window=window)
    admitted: list[int] = []
    waits: list[float] = []
    started = time.monotonic()

    async def waiter(index: int) -> None:
        await limiter.acquire()
        admitted.append(index)
        waits.append(time.monotonic() - started)

    tasks = []
    for index in range(32):
        tasks.append(asyncio.create_task(waiter(index)))
        # One loop turn between arrivals, so "arrival order" is a fact about
        # the queue rather than about how the event loop happened to schedule
        # a batch created in the same tick.
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)

    assert admitted == list(range(32))
    # Nobody is overtaken, and nobody is left behind a whole extra window.
    # The generous slack is timer resolution, not headroom for unfairness:
    # the property under test is the gap between CONSECUTIVE arrivals, which
    # under the old lottery reached 54 seconds on a 60-second window.
    for earlier, later in pairwise(waits):
        assert later >= earlier - 1e-6
        assert later - earlier < window * 1.5


@pytest.mark.asyncio
async def test_no_waiter_waits_more_than_one_window_within_two_batches() -> None:
    """Twice the limit, so every waiter's slot exists inside one window."""

    window = 0.05
    limiter = StrictSlidingWindowLimiter(rate_limit=4, rate_window=window)
    started = time.monotonic()

    async def waiter() -> float:
        await limiter.acquire()
        return time.monotonic() - started

    waits = await asyncio.gather(*(waiter() for _ in range(8)))

    assert max(waits) < window * 2


@pytest.mark.asyncio
async def test_zero_limit_disables_proactive_window() -> None:
    """0 is the shipped default: admitted immediately, and nothing recorded."""

    limiter = StrictSlidingWindowLimiter(
        rate_limit=UNLIMITED_RATE_LIMIT, rate_window=60
    )
    started = time.monotonic()
    for _ in range(200):
        await limiter.acquire()

    assert limiter.unlimited is True
    assert list(limiter._times) == []
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_a_positive_limit_is_still_honoured_exactly() -> None:
    """Fairness changed the order of admission, never the rate (invariant 5)."""

    window = 0.2
    limiter = StrictSlidingWindowLimiter(rate_limit=3, rate_window=window)
    stamps: list[float] = []

    async def waiter() -> None:
        await limiter.acquire()
        stamps.append(time.monotonic())

    await asyncio.gather(*(waiter() for _ in range(6)))

    stamps.sort()
    for index in range(len(stamps) - 3):
        assert stamps[index + 3] - stamps[index] >= window - 0.02


@pytest.mark.asyncio
async def test_reactive_block_still_applies_when_disabled() -> None:
    """Fix 1 turns off the proactive window only (invariant 4).

    A real 429 carries a ``Retry-After``; the reactive block is what obeys it,
    and it is not part of the proactive window at all.
    """

    limiter = ProviderRateLimiter(rate_limit=UNLIMITED_RATE_LIMIT, rate_window=60)
    limiter.extend_reactive_block(0.15)

    assert limiter.is_blocked() is True
    started = time.monotonic()
    waited = await limiter.wait_if_blocked()
    elapsed = time.monotonic() - started

    assert waited is True
    assert elapsed >= 0.1
    assert limiter.is_blocked() is False


@pytest.mark.asyncio
async def test_a_cancelled_waiter_gives_its_slot_back() -> None:
    """A slot reserved for a caller that went away must not be burnt."""

    limiter = StrictSlidingWindowLimiter(rate_limit=1, rate_window=30)
    await limiter.acquire()

    blocked = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked

    # One slot is held by the first acquisition and one waiter has gone; the
    # window must be back to exactly one recorded admission.
    assert len(limiter._times) == 1
