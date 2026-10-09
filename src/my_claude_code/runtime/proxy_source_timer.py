"""The schedule of a vendor source's list, when its operator switched one on (7.91.0).

A VPN account's server list or a proxy list's download link is fetched when
the operator presses *Fetch now* -- and, only if they chose one on that
source's card, every N hours. Every source starts with no schedule, so on
every install this loop reads the store once, finds nothing to do and ends.

Written to the shape of the other proxy loops (``proxy_feed_timer``,
``proxy_check_timer``), because a background loop in front of
``/v1/messages`` is the thing this codebase has been bitten by most:

1. **Never blocks the loop.** Reading the store and writing it run on a
   worker thread; the fetch is one bounded ``await`` (the feed timeout).
2. **Never overlaps.** One fetch per source at a time, shared with the
   button (``FETCHES_IN_FLIGHT``): a tick that finds one running skips it.
3. **Never touches the request path** beyond what a press of *Fetch now*
   does: it updates the source's offers, and rebuilds only a provider whose
   chain already uses an address whose URL changed.
4. **Sleeps until the next source is due**, re-reading the store at least
   hourly, and is re-armed by a save that may have changed a schedule.
5. **Cancellable**: ``ApplicationRuntime.close()`` cancels it.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime

from loguru import logger

from my_claude_code.api.admin_proxy_source_routes import fetch_source_now
from my_claude_code.application.proxy_vendor_sources import (
    due_source_ids,
    seconds_until_due,
)
from my_claude_code.config.proxy_sources import current_proxy_sources
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.timer_rearm import RearmableTimer

#: The longest the loop sleeps before reading the store again, so a hand-
#: edited store or a clock change is noticed within the hour.
MAX_SLEEP_SECONDS = 3600.0
#: The least it sleeps after a tick. A due source a tick could not fetch --
#: the button's fetch of it still running -- records nothing, and without
#: this pause the loop would ask again at once, and again.
MIN_SLEEP_SECONDS = 60.0


def _until_due() -> float | None:
    return seconds_until_due(current_proxy_sources(), datetime.now(UTC))


def _due_now() -> list[str]:
    return due_source_ids(current_proxy_sources(), datetime.now(UTC))


class ProxySourceTimer(RearmableTimer):
    """One loop for every scheduled source, cancelled with the runtime."""

    def __init__(
        self,
        settings: Callable[[], Settings],
        republish: Callable[[Iterable[str]], Awaitable[object]],
        *,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._settings = settings
        self._republish = republish
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._next_tick_at: float | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def next_fetch_at(self) -> float | None:
        return self._next_tick_at

    def start(self) -> bool:
        """Start the loop. It ends at once when no source has a schedule."""

        if self.running:
            return True
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
            wait = await asyncio.to_thread(_until_due)
            if wait is None:
                self._next_tick_at = None
                return
            if wait > 0:
                nap = min(wait, MAX_SLEEP_SECONDS)
                self._next_tick_at = time.time() + nap
                await self._wait(nap)
                if wait > MAX_SLEEP_SECONDS:
                    continue
            self._next_tick_at = None
            await self._loop_tick()
            self._next_tick_at = time.time() + MIN_SLEEP_SECONDS
            await self._wait(MIN_SLEEP_SECONDS)

    async def tick(self) -> int:
        """Fetch every source that is due. Returns how many were fetched."""

        due = await asyncio.to_thread(_due_now)
        fetched = 0
        for source_id in due:
            try:
                outcome = await fetch_source_now(
                    self._settings(), source_id, self._republish
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Scheduled fetch of proxy source {} failed: exc_type={}",
                    source_id,
                    type(exc).__name__,
                )
                continue
            fetched += 1 if outcome.ok else 0
        return fetched

    async def _wait(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
            return
        outcome = self._sleep(seconds)
        if asyncio.iscoroutine(outcome):
            await outcome


__all__ = ["MAX_SLEEP_SECONDS", "MIN_SLEEP_SECONDS", "ProxySourceTimer"]
