"""32 requests at concurrency 8, with the shipped defaults.

This is the test that would have caught the 54-second tail. On v6.60.2, with
its shipped ``PROVIDER_RATE_LIMIT`` and its lottery limiter, 24 requests at
concurrency 8 measured p50 938 ms against p95 54 397 ms -- a 58x spread on one
batch, from one machine, against a fixed-cost upstream.

The upstream here is a fixed-cost coroutine rather than a socket: the defect
was never in the network, it was in what MCC did before the request left, and
a synthetic upstream is the only way to assert on a tail on a CI runner at all.
"""

import asyncio
import time
from statistics import median

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.providers.rate_limit import ProviderRateLimiter

#: What one upstream "request" costs, in seconds. Small, and identical for
#: every request, so every millisecond of spread is MCC's own.
UPSTREAM_SECONDS = 0.02

REQUESTS = 32
CONCURRENCY = 8


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


async def _run(limiter: ProviderRateLimiter) -> list[float]:
    gate = asyncio.Semaphore(CONCURRENCY)
    totals: list[float] = []

    async def one() -> None:
        started = time.monotonic()
        async with gate:
            await limiter.wait_if_blocked()
            async with limiter.concurrency_slot():
                await asyncio.sleep(UPSTREAM_SECONDS)
        totals.append((time.monotonic() - started) * 1000.0)

    await asyncio.gather(*(one() for _ in range(REQUESTS)))
    return totals


def _limiter_from_shipped_defaults() -> ProviderRateLimiter:
    settings = Settings()
    return ProviderRateLimiter(
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
    )


@pytest.mark.asyncio
async def test_p95_within_budget_at_conc_8() -> None:
    """With the shipped defaults, the tail is within 5x the median and 3 s.

    Both halves matter. The absolute bound says the batch finished; the ratio
    says no single request was singled out, which is the shape starvation has
    and queueing does not.
    """

    totals = await _run(_limiter_from_shipped_defaults())

    p50 = median(totals)
    p95 = _percentile(totals, 0.95)

    assert len(totals) == REQUESTS
    assert p95 < 3000.0, f"p95 {p95:.0f}ms"
    assert p95 < max(5.0 * p50, 250.0), f"p50 {p50:.0f}ms, p95 {p95:.0f}ms"


@pytest.mark.asyncio
async def test_the_shipped_pace_is_far_above_interactive_volume() -> None:
    """The soak above is only meaningful if the defaults are really the ones.

    6.62.0 answered the 54-second tail by shipping ``PROVIDER_RATE_LIMIT=0``
    -- no proactive pace at all. 6.68.0 ships 300 per 2 s instead, which is
    150 requests a second per provider: still far above anything a person
    at a keyboard can generate, so the soak above measures the same thing,
    while a runaway loop now meets a ceiling. What matters is not that the
    limiter is switched off but that the shipped pace cannot be the thing
    that holds an interactive request back, so that is what is asserted.
    """

    settings = Settings()

    per_second = settings.provider_rate_limit / settings.provider_rate_window
    assert per_second >= 100.0, (
        f"the shipped pace is {per_second:.1f} req/s per provider, which is "
        "low enough to throttle real traffic"
    )
    # The whole soak batch fits inside a single window, so not one of its
    # requests can be waiting on the pace rather than on the upstream.
    assert settings.provider_rate_limit > REQUESTS
    assert settings.provider_max_concurrency >= CONCURRENCY


@pytest.mark.asyncio
async def test_an_operator_who_turns_the_limit_on_gets_a_fair_queue() -> None:
    """The opt-in path: paced, but never a lottery.

    Four admissions per 100 ms window against 32 requests at concurrency 8.
    The batch is paced -- that is what was asked for -- and the tail stays
    within one window of the median request's position in the queue.
    """

    limiter = ProviderRateLimiter(rate_limit=4, rate_window=0.1, max_concurrency=8)

    totals = await _run(limiter)

    p50 = median(totals)
    p95 = _percentile(totals, 0.95)

    assert len(totals) == REQUESTS
    # 32 requests at 4 per 100 ms is 8 windows: the LAST request is expected
    # to wait ~0.8 s. What must not happen is one request waiting many times
    # that while others sail past, which is what a p95/p50 blow-out means.
    assert p95 < 2000.0, f"p95 {p95:.0f}ms"
    assert p95 < 4.0 * p50 + 200.0, f"p50 {p50:.0f}ms, p95 {p95:.0f}ms"
