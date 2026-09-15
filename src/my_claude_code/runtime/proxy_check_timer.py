"""The optional background loop that re-measures the addresses in a chain.

Off by default and off on every install until an operator turns it on. That is
the whole of the consent story: a user who never opens the Proxying page makes
no outbound request they did not ask for, and the Test button on that page is
the other half -- one request, per press, per address.

Written to the shape ``runtime/discovery_timer.py`` already proved, because
this codebase has been bitten three times by a background loop that turned out
to be sitting in front of ``/v1/messages``: the models.dev storm, the pause
rebuild, and the discovery sweep the timer above was written to replace. The
guards that follow are each one of those incidents:

1. **Never overlap.** A tick that lands while a sweep is running is skipped,
   not queued. A dozen addresses at a ten-second timeout is two minutes in the
   worst case, and a queued tick would make the next one worse.
2. **Never block the loop.** Every leg of a check is an ordinary ``await`` and
   ``check_endpoints`` yields between addresses. Nothing here is a thread and
   nothing here is synchronous I/O.
3. **Never touch the request path.** The loop writes the store and the two
   process-wide ledgers; it constructs no provider, republishes no generation,
   and holds no reference to one.
4. **Cancellable.** ``ApplicationRuntime.close()`` cancels it, and
   ``CancelledError`` is re-raised rather than swallowed.

The interval is re-read every pass, so a change takes effect at the next tick.
``0`` -- or the switch turned off -- ends the loop.
"""

import asyncio
import time
from collections.abc import Callable

from loguru import logger

from my_claude_code.application.proxy_check import check_endpoints

#: Floor under the configured interval. One sweep is one HEAD request per
#: address to a provider's own host; a mistyped ``1`` against a twelve-entry
#: catalogue would be a self-inflicted knock at somebody's door every minute.
PROXY_CHECK_MINIMUM_MINUTES = 5


def resolve_check_interval(enabled: bool, minutes: float) -> float:
    """Seconds between sweeps, or ``0`` for "do not run".

    ``0`` minutes is off even when the switch is on, the same way
    ``MODEL_DISCOVERY_REFRESH_SECONDS=0`` is: two ways to say "not now" is one
    more than an operator should need to find.
    """

    if not enabled or minutes <= 0:
        return 0.0
    return max(float(minutes), float(PROXY_CHECK_MINIMUM_MINUTES)) * 60.0


class ProxyCheckTimer:
    """One loop, one sweep at a time, cancelled with the runtime that owns it."""

    def __init__(
        self,
        targets: Callable[[], dict[str, str]],
        interval_minutes: Callable[[], float],
        enabled: Callable[[], bool],
        exit_ip_url: Callable[[], str],
        *,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._targets = targets
        self._interval_minutes = interval_minutes
        self._enabled = enabled
        self._exit_ip_url = exit_ip_url
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._sweeping = False
        self._next_tick_at: float | None = None
        self._last_tick_at: float | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def next_check_at(self) -> float | None:
        return self._next_tick_at

    def start(self) -> bool:
        """Start the loop unless it is switched off. Idempotent."""

        if self.running:
            return True
        if resolve_check_interval(self._enabled(), self._interval_minutes()) <= 0:
            logger.debug(
                "Background proxy checking is off (PROXY_CHECK_ENABLED=false). "
                "The Test button on the Proxying page still works."
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
            interval = resolve_check_interval(self._enabled(), self._interval_minutes())
            if interval <= 0:
                self._next_tick_at = None
                logger.info(
                    "Background proxy checking switched off; the loop is ending"
                )
                return
            self._next_tick_at = time.time() + interval
            await self._wait(interval)
            await self.tick()

    async def tick(self) -> int:
        """One sweep. Returns how many addresses were measured."""

        if self._sweeping:
            logger.debug(
                "Proxy check tick skipped: the previous sweep is still running"
            )
            return 0
        targets = self._targets()
        if not targets:
            self._last_tick_at = time.time()
            return 0
        self._sweeping = True
        try:
            outcomes = await check_endpoints(
                tuple(targets), targets, exit_ip_url=self._exit_ip_url().strip()
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Background proxy check failed: exc_type={}", type(exc).__name__
            )
            return 0
        finally:
            self._sweeping = False
            self._last_tick_at = time.time()

        refused = [outcome.label for outcome in outcomes.values() if outcome.refused]
        if refused:
            # Named at WARNING because it is the one outcome that changes what
            # the proxy will do rather than only what the page shows.
            logger.warning(
                "Proxy check: refusing {} -- the tunnel breaks certificate "
                "validation and is now held out of every chain",
                ", ".join(sorted(refused)),
            )
        dead = [
            outcome.label
            for outcome in outcomes.values()
            if not outcome.record.ok and not outcome.refused
        ]
        if dead:
            logger.info(
                "Proxy check: {} did not answer and walked the reachability "
                "ladder; their chains route around them",
                ", ".join(sorted(dead)),
            )
        return len(outcomes)

    async def _wait(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
            return
        outcome = self._sleep(seconds)
        if asyncio.iscoroutine(outcome):
            await outcome


__all__ = [
    "PROXY_CHECK_MINIMUM_MINUTES",
    "ProxyCheckTimer",
    "resolve_check_interval",
]
