"""The background loop that keeps every provider's catalogue current.

A model a gateway added this morning used to stay invisible until somebody
restarted the proxy or pressed *Refresh models*, because discovery ran once at
startup and once per config apply and never again.

This is the answer to that, and it is written defensively on purpose.
``ProviderRuntimeManager.refresh_provider_models`` carries a docstring that
says, in as many words, *"a periodic sweep is deliberately not the answer,
because the sweep is what caused the race this replaces"* -- a periodic sweep
has already broken this codebase once, by racing a brand-new provider's own
first ``/models`` query. Every guard below exists because of that sentence:

1. **Never overlap.** A tick that lands while a sweep is running is skipped and
   logged at DEBUG, never queued. The manager's own single-flight guard is the
   one consulted; this loop adds no second task handle.
2. **Never race a config apply.** The tick goes through
   ``refresh_model_list_cache_periodic``, which takes ``_replace_lock`` and
   cancels any in-flight refresh first, so an apply always wins.
3. **Never block the event loop.** The per-provider ``await asyncio.sleep(0)``
   in ``providers/runtime/discovery.py`` is what keeps ``/v1/messages`` from
   stalling behind a 57-provider sweep and what lets a cancel land at all. The
   timer makes that yield fire far more often; it must not be weakened.
4. **Back off on auth failures.** A provider that answered 401 or 403 is
   skipped for an exponentially increasing number of ticks (1, 2, 4, 8, capped
   at 8), because asking a rejected credential again every hour is how a
   gateway decides to rate-limit the whole account.
5. **Cancellable.** ``ApplicationRuntime.close()`` cancels this task before the
   provider manager closes, and ``CancelledError`` is re-raised.

The interval is re-read every pass, so changing the setting takes effect at the
next tick rather than at the next restart; ``0`` stops the loop entirely.
"""

import asyncio
import time
from collections.abc import Callable

from loguru import logger

from my_claude_code.application.model_metadata import ProviderModelRefreshResult
from my_claude_code.config.constants import MODEL_DISCOVERY_REFRESH_MINIMUM_SECONDS

#: How many consecutive ticks an auth-failing provider is skipped for, walked
#: by consecutive failures and clamped at the last entry. The same shape as the
#: credential lockout ladder, for the same reason: the second failure means
#: something different from the first.
AUTH_BACKOFF_TICKS: tuple[int, ...] = (1, 2, 4, 8)

#: The statuses that mean "this credential is not welcome", as opposed to "this
#: request was wrong". Only these earn a backoff; a timeout or a 5xx is the
#: network's problem and is retried at the ordinary interval.
AUTH_FAILURE_STATUSES: frozenset[int] = frozenset({401, 403})


def resolve_refresh_interval(configured: float) -> float:
    """Return the interval a tick actually waits, or ``0`` for "off".

    ``0`` is the only value that means something other than a duration. A
    positive value below the floor is raised to it rather than rejected: one
    sweep is one upstream request per provider, and a mistyped ``30`` against
    57 upstreams is a self-inflicted denial of service.
    """

    if configured <= 0:
        return 0.0
    return max(float(configured), MODEL_DISCOVERY_REFRESH_MINIMUM_SECONDS)


class ProviderBackoff:
    """Which providers this loop is currently declining to ask, and for how long."""

    __slots__ = ("_failures", "_remaining")

    def __init__(self) -> None:
        self._remaining: dict[str, int] = {}
        self._failures: dict[str, int] = {}

    def skipped_provider_ids(self) -> frozenset[str]:
        """Providers currently held back, without consuming a tick."""

        return frozenset(self._remaining)

    def consume(self) -> frozenset[str]:
        """Spend one tick: return who is skipped *now*, then count it down.

        Read before decrement, deliberately. A provider benched for one tick
        has to actually miss one; counting down first would set a backoff of
        one to a backoff of none and make the first rung of the ladder a no-op.
        """

        skipped = frozenset(self._remaining)
        for provider_id in list(self._remaining):
            remaining = self._remaining[provider_id] - 1
            if remaining <= 0:
                del self._remaining[provider_id]
            else:
                self._remaining[provider_id] = remaining
        return skipped

    def note_success(self, provider_id: str) -> None:
        """A provider that answered has no reason to be held back."""

        self._remaining.pop(provider_id, None)
        self._failures.pop(provider_id, None)

    def note_auth_failure(self, provider_id: str) -> int:
        """Bench one provider for the next rung of the ladder."""

        step = min(self._failures.get(provider_id, 0), len(AUTH_BACKOFF_TICKS) - 1)
        self._failures[provider_id] = step + 1
        ticks = AUTH_BACKOFF_TICKS[step]
        self._remaining[provider_id] = ticks
        return ticks


class ProviderDiscoveryTimer:
    """One loop, one sweep at a time, cancelled with the runtime that owns it."""

    def __init__(
        self,
        sweep: Callable[[frozenset[str]], object],
        interval_seconds: Callable[[], float],
        *,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._sweep = sweep
        self._interval_seconds = interval_seconds
        self._sleep = sleep
        self._backoff = ProviderBackoff()
        self._task: asyncio.Task[None] | None = None
        self._last_tick_at: float | None = None
        self._next_tick_at: float | None = None

    @property
    def running(self) -> bool:
        """Whether the loop is live."""

        return self._task is not None and not self._task.done()

    @property
    def next_refresh_at(self) -> float | None:
        """Epoch seconds the next tick is due, or ``None`` when off."""

        return self._next_tick_at

    def start(self) -> bool:
        """Start the loop, unless the setting turns it off. Idempotent."""

        if self.running:
            return True
        if resolve_refresh_interval(self._interval_seconds()) <= 0:
            logger.info(
                "Automatic model catalogue refresh is off "
                "(MODEL_DISCOVERY_REFRESH_SECONDS=0)"
            )
            return False
        self._task = asyncio.create_task(self.run())
        return True

    async def close(self) -> None:
        """Cancel the loop and wait for it, re-raising nothing."""

        task = self._task
        self._task = None
        self._next_tick_at = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        """Sleep, sweep, repeat -- until cancelled or switched off."""

        while True:
            interval = resolve_refresh_interval(self._interval_seconds())
            if interval <= 0:
                self._next_tick_at = None
                logger.info(
                    "Automatic model catalogue refresh switched off; the loop is ending"
                )
                return
            self._next_tick_at = time.time() + interval
            await self._wait(interval)
            await self.tick()

    async def tick(self) -> ProviderModelRefreshResult:
        """Run one sweep, honouring the backoff and updating it."""

        skipped = self._backoff.consume()
        if skipped:
            logger.debug(
                "Periodic model discovery is backing off from: {}",
                ", ".join(sorted(skipped)),
            )
        try:
            result = await self._call_sweep(skipped)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Periodic model discovery sweep failed: exc_type={}",
                type(exc).__name__,
            )
            return ProviderModelRefreshResult()
        self._last_tick_at = time.time()
        self._apply_backoff(result)
        return result

    def _apply_backoff(self, result: ProviderModelRefreshResult) -> None:
        for provider_id in result.refreshed_provider_ids:
            self._backoff.note_success(provider_id)
        for failure in result.failures:
            if failure.status_code not in AUTH_FAILURE_STATUSES:
                continue
            ticks = self._backoff.note_auth_failure(failure.provider_id)
            logger.warning(
                "Periodic model discovery: {} answered {} -- skipping it for the "
                "next {} tick(s) rather than asking a rejected credential again",
                failure.provider_id,
                failure.status_code,
                ticks,
            )

    async def _call_sweep(self, skipped: frozenset[str]) -> ProviderModelRefreshResult:
        outcome = self._sweep(skipped)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if isinstance(outcome, ProviderModelRefreshResult):
            return outcome
        return ProviderModelRefreshResult()

    async def _wait(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
            return
        outcome = self._sleep(seconds)
        if asyncio.iscoroutine(outcome):
            await outcome
