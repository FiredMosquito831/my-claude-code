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

from my_claude_code.application.proxy_check import (
    PROXY_CHECK_MAX_CONCURRENCY,
    PROXY_CHECK_TIMEOUT_SECONDS,
    check_endpoints,
    check_target_providers,
    check_targets,
)
from my_claude_code.application.proxy_health_store import (
    flush_health,
    take_early_confirms,
)
from my_claude_code.application.proxy_speed_store import flush_speed
from my_claude_code.config.constants import (
    PROXY_CHECK_CONFIRM_ATTEMPTS_DEFAULT,
    PROXY_CHECK_CONFIRM_SPACING_SECONDS_DEFAULT,
)
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import load_proxy_chains
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY
from my_claude_code.runtime.timer_rearm import RearmableTimer

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


class ProxyCheckTimer(RearmableTimer):
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
            await self._loop_tick()

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


def _reprobe_concurrency(settings: object) -> int:
    return int(
        getattr(settings, "proxy_check_max_concurrency", PROXY_CHECK_MAX_CONCURRENCY)
    )


def _check_timeout(settings: object) -> float:
    return float(
        getattr(settings, "proxy_check_timeout_seconds", PROXY_CHECK_TIMEOUT_SECONDS)
    )


#: Seconds between passes of the health re-prober. Fixed rather than a setting:
#: the cadence an address is actually re-tested on is its own reachability tier
#: (60s, 5m, 1h), and this is only how often the loop looks for one whose tier
#: has run out. Thirty seconds makes the 60-second tier honest without being a
#: second number anybody has to reason about.
PROXY_REPROBE_TICK_SECONDS = 30.0


class ProxyHealthTimer:
    """Re-test the addresses that failed, and write what is known to the store.

    Two jobs, one loop, and they are in one loop because they are two halves of
    the same sentence: since 7.19.0 an address that failed stays out of the
    rotation until a check **passes**, so something has to run that check and
    something has to remember the answer across a restart.

    * **Flush.** Every tick, whatever else happens, the reachability bench is
      written into the proxy store. Free when nothing changed, and it is the
      only reason the request path never touches the file.
    * **Re-probe.** When ``PROXY_HEALTH_REPROBE_ENABLED`` is on -- which it is
      by default -- every address in an **enabled** chain whose tier has run
      out is re-checked through
      :func:`~my_claude_code.application.proxy_check.check_endpoints`, against
      that provider's own host. A pass puts it back in rotation; a failure
      moves it one tier down the ladder.

    The consent story is the one the checker above states, one step narrower:
    this loop contacts only hosts the operator already routes to, only about
    addresses that have already failed on the operator's own traffic, and never
    about a chain that is switched off. An install with no chain, or with every
    chain off, makes no request from here at all -- and the flush half still
    runs, because writing a file is not traffic.

    The same four guards as ``ProxyCheckTimer``: never overlap, never block the
    loop, never touch the request path, cancellable.
    """

    def __init__(
        self,
        settings: Callable[[], object],
        enabled: Callable[[], bool],
        *,
        sleep: Callable[[float], object] | None = None,
        tick_seconds: float = PROXY_REPROBE_TICK_SECONDS,
    ) -> None:
        self._settings = settings
        self._enabled = enabled
        self._sleep = sleep
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._task: asyncio.Task[None] | None = None
        self._round: asyncio.Task[int] | None = None
        self._sweeping = False
        #: The labels the round in progress is re-testing, so an early confirm
        #: never tests the same address beside it.
        self._in_round: set[str] = set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Start the loop. Idempotent, and it starts even when probing is off.

        The flush half has to run either way: an operator who turned the
        re-prober off still wants a restart to remember which addresses are
        dead, or turning it off would silently turn persistence off too.
        """

        if self.running:
            return True
        self._task = asyncio.create_task(self.run())
        return True

    async def close(self) -> None:
        task = self._task
        self._task = None
        pending_round = self._round
        self._round = None
        if pending_round is not None and not pending_round.done():
            pending_round.cancel()
            await asyncio.gather(pending_round, return_exceptions=True)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # One last write on the way out, so a clean shutdown does not lose the
        # benches the final requests earned.
        await asyncio.to_thread(flush_health)
        await asyncio.to_thread(flush_speed)

    async def run(self) -> None:
        while True:
            await self._wait(self._tick_seconds)
            # A round of several tries takes minutes; it runs beside the loop
            # so the flush and the early confirms keep their 30-second beat.
            await self.tick(background=True)

    async def tick(self, *, background: bool = False) -> int:
        """One pass. Returns how many addresses were checked by it.

        Two kinds of check, both only for addresses in an **enabled** chain:

        * **Early confirm** (7.53.0): an address whose first failure arrived
          since the last tick -- a fresh 0 -> 1 on the ladder, read through the
          health store's listener -- and that is not yet due is tested once.
          A pass puts it back in rotation now, instead of after the 60-second
          rung; a failure changes nothing, so a quick re-test can never push
          an address down the ladder faster.
        * **The round** for every address whose rung has run out: up to
          ``PROXY_CHECK_CONFIRM_ATTEMPTS`` tries, spaced
          ``PROXY_CHECK_CONFIRM_SPACING_SECONDS`` apart. Any pass puts it back;
          only when every try failed does it move one rung down -- once.

        ``background`` runs the round as its own task (the loop does this);
        a caller that wants the answer, like a test, awaits it here.
        """

        await asyncio.to_thread(flush_health)
        # The speed ledger rides the same beat (7.54.0): written only when a
        # sample arrived since the last tick, never from the request path.
        await asyncio.to_thread(flush_speed)
        early_labels = take_early_confirms()
        if not self._enabled():
            return 0
        settings = self._settings()
        try:
            store = await asyncio.to_thread(load_proxy_chains)
        except Exception as exc:  # pragma: no cover - a read failure is logged
            logger.debug(
                "Proxy health re-probe could not read the store: exc_type={}",
                type(exc).__name__,
            )
            return 0
        targets = check_targets(settings, store, enabled_only=True)
        providers = check_target_providers(settings, store, enabled_only=True)
        due: list[str] = []
        early: list[str] = []
        for proxy_id in targets:
            endpoint = store.proxies.get(proxy_id)
            if endpoint is None:  # pragma: no cover - targets come from the store
                continue
            label = endpoint.label or mask_proxy_label(endpoint.url)
            if PROXY_REACHABILITY.due_for_reprobe(label):
                if label not in self._in_round:
                    due.append(proxy_id)
            elif (
                label in early_labels
                and label not in self._in_round
                and PROXY_REACHABILITY.failures(label) == 1
            ):
                early.append(proxy_id)
        checked = 0
        if early:
            checked += await self._early_confirm(early, targets, settings, providers)
        if not due or self._sweeping:
            return checked
        if background:
            self._round = asyncio.create_task(
                self._reprobe(due, targets, settings, providers)
            )
            return checked
        return checked + await self._reprobe(due, targets, settings, providers)

    async def _early_confirm(
        self,
        early: list[str],
        targets: dict[str, str],
        settings: object,
        providers: dict[str, str] | None = None,
    ) -> int:
        """One try each; a pass is applied, a failure is not (7.53.0)."""

        concurrency = _reprobe_concurrency(settings)
        try:
            outcomes = await check_endpoints(
                early,
                targets,
                timeout=_check_timeout(settings),
                exit_ip_url="",
                concurrency=concurrency,
                max_concurrency=concurrency,
                charge_failures=False,
                providers=providers,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Proxy health early confirm failed: exc_type={}", type(exc).__name__
            )
            return 0
        back = [outcome.label for outcome in outcomes.values() if outcome.record.ok]
        if back:
            logger.info(
                "Proxy health: {} passed an early re-check after a live failure "
                "and are back in rotation",
                ", ".join(sorted(back)),
            )
            await asyncio.to_thread(flush_health)
        return len(outcomes)

    async def _reprobe(
        self,
        due: list[str],
        targets: dict[str, str],
        settings: object,
        providers: dict[str, str] | None = None,
    ) -> int:
        """One round for every due address. Returns how many were checked."""

        store = await asyncio.to_thread(load_proxy_chains)
        self._in_round = {
            endpoint.label or mask_proxy_label(endpoint.url)
            for proxy_id in due
            if (endpoint := store.proxies.get(proxy_id)) is not None
        }
        # Both of the operator's check settings reach the re-probe (7.52.2).
        # Before, the timeout was the module constant -- ten seconds whatever
        # ``PROXY_CHECK_TIMEOUT_SECONDS`` said -- and the concurrency was
        # clamped to ``check_endpoints``' default ceiling of four, so raising
        # ``PROXY_CHECK_MAX_CONCURRENCY`` above four changed nothing. The
        # setting *is* the ceiling here, so it is passed as both.
        #
        # Since 7.53.0 each due address gets a round rather than one try: the
        # measured population passes about half its tries, so one unlucky try
        # used to push a working address a whole rung towards the hour.
        concurrency = _reprobe_concurrency(settings)
        self._sweeping = True
        try:
            outcomes = await check_endpoints(
                due,
                targets,
                timeout=_check_timeout(settings),
                exit_ip_url="",
                concurrency=concurrency,
                max_concurrency=concurrency,
                attempts=int(
                    getattr(
                        settings,
                        "proxy_check_confirm_attempts",
                        PROXY_CHECK_CONFIRM_ATTEMPTS_DEFAULT,
                    )
                ),
                spacing=float(
                    getattr(
                        settings,
                        "proxy_check_confirm_spacing_seconds",
                        PROXY_CHECK_CONFIRM_SPACING_SECONDS_DEFAULT,
                    )
                ),
                providers=providers,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Proxy health re-probe failed: exc_type={}", type(exc).__name__
            )
            return 0
        finally:
            self._sweeping = False
            self._in_round = set()
        back = [outcome.label for outcome in outcomes.values() if outcome.record.ok]
        if back:
            logger.info(
                "Proxy health: {} passed a re-check and are back in rotation",
                ", ".join(sorted(back)),
            )
        await asyncio.to_thread(flush_health)
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
    "PROXY_REPROBE_TICK_SECONDS",
    "ProxyCheckTimer",
    "ProxyHealthTimer",
    "resolve_check_interval",
]
