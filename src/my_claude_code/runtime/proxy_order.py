"""The loop that keeps each opted-in chain's fastest healthy proxy first (7.56.0).

Every ``PROXY_ORDER_TICK_SECONDS`` it looks at the chains that are **enabled**,
have "Keep the fastest healthy proxy first" on, and run ``failover`` or
``single``, and asks :func:`~my_claude_code.api.admin_proxy_routes.commit_speed_order`
whether a new order is due (spec §7.4: the first healthy entry changes, by a
clear margin, with enough samples, and not within ``PROXY_ORDER_RESORT_MINUTES``
of the last one). A chain whose order was written is republished through the
same scoped path a chain save uses, so that provider -- and only that provider
-- is rebuilt.

What this loop never does:

* touch a chain whose switch is off, which is every chain stored before
  7.56.0 until its operator turns it on;
* make a network request -- it reads the speed ledger and the health ledgers
  that checks and live traffic already fill;
* pause, remove or un-pause anything. "Pause all but the fastest N" is a
  button, never this loop.

The same guards as the other proxy timers: never overlap, never block the loop
(the store and ledger reads run in a worker thread), cancellable.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from loguru import logger

from my_claude_code.api.admin_proxy_routes import commit_speed_order
from my_claude_code.application.proxy_order import (
    ORDERABLE_POLICIES,
    PROXY_ORDER_TICK_SECONDS,
)
from my_claude_code.config.proxy_chains import load_proxy_chains


def orderable_chains() -> tuple[str, ...]:
    """The provider ids whose chain the loop may sort, in store order."""

    store = load_proxy_chains()
    return tuple(
        provider_id
        for provider_id, chain in store.chains.items()
        if chain.enabled and chain.order_by_speed and chain.policy in ORDERABLE_POLICIES
    )


class ProxyOrderTimer:
    """One loop; one look at every opted-in chain per tick."""

    def __init__(
        self,
        settings: Callable[[], object],
        republish: Callable[[Iterable[str]], Awaitable[None]],
        *,
        sleep: Callable[[float], object] | None = None,
        tick_seconds: float = PROXY_ORDER_TICK_SECONDS,
    ) -> None:
        self._settings = settings
        self._republish = republish
        self._sleep = sleep
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._task: asyncio.Task[None] | None = None
        self._ticking = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Start the loop. Idempotent; a chain with the switch off costs nothing."""

        if self.running:
            return True
        self._task = asyncio.create_task(self.run())
        return True

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        while True:
            await self._wait(self._tick_seconds)
            await self.tick()

    async def tick(self) -> tuple[str, ...]:
        """One look. Returns the providers whose chain got a new order."""

        if self._ticking:
            return ()
        self._ticking = True
        try:
            return await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Proxy speed order tick failed: exc_type={}", type(exc).__name__
            )
            return ()
        finally:
            self._ticking = False

    async def _tick(self) -> tuple[str, ...]:
        provider_ids = await asyncio.to_thread(orderable_chains)
        if not provider_ids:
            return ()
        settings = self._settings()
        written: list[str] = []
        for provider_id in provider_ids:
            outcome = await asyncio.to_thread(
                commit_speed_order, provider_id, settings, explicit=False
            )
            if outcome.get("written"):
                written.append(provider_id)
        if written:
            # One scoped republish for what was written this tick: each chain
            # is read by its own provider alone, so nothing else is rebuilt.
            await self._republish(frozenset(written))
        return tuple(written)

    async def _wait(self, seconds: float) -> None:
        if self._sleep is None:
            await asyncio.sleep(seconds)
            return
        outcome = self._sleep(seconds)
        if asyncio.iscoroutine(outcome):
            await outcome


__all__ = ["ProxyOrderTimer", "orderable_chains"]
