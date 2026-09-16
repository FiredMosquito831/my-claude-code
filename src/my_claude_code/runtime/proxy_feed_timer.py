"""The optional background loop that re-reads the feeds an operator chose.

Off by default, off on every install, and off even when it is on until the
operator has also switched on at least one feed. Two independent switches
rather than one, because they are two different consents: *may this install
talk to public proxy lists at all* (the feed switches, on the Proxying page)
and *may it do so on a timer without me pressing anything* (this loop's
setting, on Limits & Resilience). Neither implies the other and neither ships
armed.

Written to exactly the shape ``runtime/proxy_check_timer.py`` proved one
release ago, which was itself written to the shape ``discovery_timer`` proved,
because this codebase has been bitten repeatedly by a background loop that
turned out to be sitting in front of ``/v1/messages``:

1. **Never overlap.** A tick that lands while a pass is running is skipped,
   not queued. A pass is serial at a fifteen-second timeout per feed, so its
   worst case grows with however many lists the operator added, and a queued
   tick would make the next one worse.
2. **Never block the loop.** Every fetch is an ordinary ``await`` and ``ingest``
   yields between feeds.
3. **Never touch the request path.** The pass writes the candidate list. It
   constructs no provider, republishes no generation, changes no chain, and
   cannot put an address in front of a credential -- that takes an operator
   moving a row.
4. **Cancellable.** ``ApplicationRuntime.close()`` cancels it and
   ``CancelledError`` is re-raised rather than swallowed.

The interval is re-read every pass, so a change takes effect at the next tick.
``0`` -- or the switch turned off -- ends the loop.
"""

import asyncio
import time
from collections.abc import Callable

from loguru import logger

from my_claude_code.application.proxy_ingest import ingest
from my_claude_code.config.constants import PROXY_FEED_MINIMUM_MINUTES

# ``PROXY_FEED_MINIMUM_MINUTES`` is the floor under the configured interval and
# is re-exported here, where the loop that applies it lives. It is defined in
# ``config.constants`` because the Proxying page states the same number and the
# ``api`` package may not import this one.


def resolve_feed_interval(enabled: bool, minutes: float) -> float:
    """Seconds between passes, or ``0`` for "do not run".

    ``0`` minutes is off even when the switch is on, the same way the checker's
    interval and ``MODEL_DISCOVERY_REFRESH_SECONDS`` are: two ways to say "not
    now" is one more than an operator should need to find.
    """

    if not enabled or minutes <= 0:
        return 0.0
    return max(float(minutes), float(PROXY_FEED_MINIMUM_MINUTES)) * 60.0


class ProxyFeedTimer:
    """One loop, one pass at a time, cancelled with the runtime that owns it."""

    def __init__(
        self,
        interval_minutes: Callable[[], float],
        enabled: Callable[[], bool],
        *,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._interval_minutes = interval_minutes
        self._enabled = enabled
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._running_pass = False
        self._next_tick_at: float | None = None
        self._last_tick_at: float | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def next_refresh_at(self) -> float | None:
        return self._next_tick_at

    def start(self) -> bool:
        """Start the loop unless it is switched off. Idempotent."""

        if self.running:
            return True
        if resolve_feed_interval(self._enabled(), self._interval_minutes()) <= 0:
            logger.debug(
                "Scheduled proxy feed refresh is off (PROXY_FEED_REFRESH_ENABLED"
                "=false). The Fetch button on the Proxying page still works."
            )
            return False
        self._task = asyncio.create_task(self.run())
        return True

    async def close(self) -> None:
        task = self._task
        self._task = None
        self._next_tick_at = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        while True:
            interval = resolve_feed_interval(self._enabled(), self._interval_minutes())
            if interval <= 0:
                self._next_tick_at = None
                logger.info(
                    "Scheduled proxy feed refresh switched off; the loop is ending"
                )
                return
            self._next_tick_at = time.time() + interval
            await self._wait(interval)
            await self.tick()

    async def tick(self) -> int:
        """One pass. Returns how many addresses are on offer afterwards."""

        if self._running_pass:
            logger.debug("Proxy feed tick skipped: the previous pass is still running")
            return 0
        self._running_pass = True
        try:
            run = await ingest()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Scheduled proxy feed refresh failed: exc_type={}",
                type(exc).__name__,
            )
            return 0
        finally:
            self._running_pass = False
            self._last_tick_at = time.time()

        unreachable = [result.name for result in run.results if not result.ok]
        if unreachable:
            logger.info(
                "Proxy feeds: {} did not answer usefully this pass; the rest "
                "were merged",
                ", ".join(sorted(unreachable)),
            )
        return run.offered

    async def _wait(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
            return
        outcome = self._sleep(seconds)
        if asyncio.iscoroutine(outcome):
            await outcome


__all__ = [
    "PROXY_FEED_MINIMUM_MINUTES",
    "ProxyFeedTimer",
    "resolve_feed_interval",
]
