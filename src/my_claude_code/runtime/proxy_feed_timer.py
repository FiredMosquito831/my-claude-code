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
   not queued -- and since 7.21.0 that includes a pass an *operator* started,
   because both go through one job slot. A pass reads every enabled list and
   then tests every address they offered, so its worst case grows with both the
   number of lists and the length of them, and a queued tick would make the
   next one worse.
2. **Never block the loop.** Every fetch is an ordinary ``await``; the read
   yields between feeds and the sweep yields after every address it tests.
   The sweep's parallelism is a fixed pool of workers, so the number of open
   sockets is bounded by ``PROXY_FETCH_TEST_CONCURRENCY`` and nothing else.
3. **Never touch the request path.** The pass writes the candidate list. It
   constructs no provider, republishes no generation, changes no chain, and
   cannot put an address in front of a credential -- that takes an operator
   moving a row. It does not charge the request path's reachability ladder for
   addresses no chain references either; see ``apply_fetch_outcome``.
4. **Cancellable.** ``ApplicationRuntime.close()`` cancels it and
   ``CancelledError`` is re-raised rather than swallowed.

The interval is re-read every pass, so a change takes effect at the next tick.
``0`` -- or the switch turned off -- ends the loop.
"""

import asyncio
import time
from collections.abc import Callable

from loguru import logger

from my_claude_code.application.proxy_check import PROXY_CHECK_TIMEOUT_SECONDS
from my_claude_code.application.proxy_fetch import (
    FetchAlreadyRunning,
    FetchRun,
    start_fetch,
    wait_for_fetch,
)
from my_claude_code.config.constants import PROXY_FEED_MINIMUM_MINUTES
from my_claude_code.config.proxy_chains import current_proxy_chains
from my_claude_code.config.settings import Settings

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
        settings: Callable[[], Settings] | None = None,
    ) -> None:
        self._interval_minutes = interval_minutes
        self._enabled = enabled
        self._sleep = sleep
        self._settings = settings
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
        """One pass. Returns how many addresses are **working** afterwards.

        The same fetch the button runs, through the same job slot: read the
        enabled feeds, test everything they offered against the chosen
        provider's own host, keep only what passed. A timer that stored
        untested addresses while the button stored tested ones would be two
        products in one page, and the candidate list would mean different
        things depending on which of them last wrote it.

        Going through the job slot is also the interlock: a tick that lands
        while an operator is watching a fetch is skipped rather than run beside
        it, so the two cannot between them double the outbound load.

        A pass with no https destination available does **nothing at all** --
        not even the reading half. Storing addresses it could not test is the
        one thing this release removed, and a background loop is the last place
        to put it back.
        """

        if self._running_pass:
            logger.debug("Proxy feed tick skipped: the previous pass is still running")
            return 0
        self._running_pass = True
        try:
            run = await self._fetch()
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

        if run is None:
            return 0
        unreachable = [result.name for result in run.results if not result.ok]
        if unreachable:
            logger.info(
                "Proxy feeds: {} did not answer usefully this pass; the rest "
                "were merged",
                ", ".join(sorted(unreachable)),
            )
        return run.working

    async def _fetch(self) -> FetchRun | None:
        """One pass through the job slot the Fetch button uses.

        Imported here rather than at module import time: ``runtime`` may depend
        on ``api``, but doing it at the top would pull the whole admin router
        in for a loop that is off on every install.
        """

        from my_claude_code.api.admin_proxy_routes import pick_fetch_destination

        if self._settings is None:  # pragma: no cover - always wired in the runtime
            logger.debug("Scheduled proxy feed refresh: no settings wired; skipping")
            return None
        settings = self._settings()
        store = current_proxy_chains()
        if not store.enabled_feed_ids:
            logger.debug("Scheduled proxy feed refresh: no feeds are switched on")
            return None
        chosen = pick_fetch_destination(settings, store)
        if chosen is None:
            logger.info(
                "Scheduled proxy feed refresh: no provider has an https base "
                "URL to test addresses against, so nothing was read. MCC does "
                "not store addresses it could not measure."
            )
            return None
        try:
            await start_fetch(
                provider_id=str(chosen["provider_id"]),
                destination=str(chosen["base_url"]).strip(),
                concurrency=int(settings.proxy_fetch_test_concurrency),
                connect_timeout=float(settings.proxy_fetch_connect_timeout_seconds),
                timeout=PROXY_CHECK_TIMEOUT_SECONDS,
                limit=int(settings.proxy_candidates_max),
                exit_ip_url=settings.proxy_check_exit_ip_url.strip(),
            )
        except FetchAlreadyRunning as exc:
            # An operator is watching one on the page. Skipped, never queued --
            # the same rule this loop has always had about its own passes.
            logger.debug(
                "Scheduled proxy feed refresh skipped: {} is already running",
                exc.job_id,
            )
            return None
        return await wait_for_fetch()

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
