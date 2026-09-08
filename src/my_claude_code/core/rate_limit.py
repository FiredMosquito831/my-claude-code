"""Shared rate limiting primitives: sliding-window limiter and reset parsing."""

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

#: ``PROVIDER_RATE_LIMIT`` value that means "do not pace requests at all".
#: Zero rather than a sentinel because every neighbouring limit already reads
#: 0 as "off", and an operator who wants no client-side pacing should not have
#: to discover a magic number to express it.
UNLIMITED_RATE_LIMIT = 0


class StrictSlidingWindowLimiter:
    """Strict sliding window limiter, admitting in arrival order.

    Guarantees: at most ``rate_limit`` acquisitions in any interval of length
    ``rate_window`` (seconds), and -- since 6.62.0 -- that a waiter is never
    overtaken by one that arrived after it.

    ``rate_limit`` of :data:`UNLIMITED_RATE_LIMIT` (0) turns the window off
    entirely: every acquisition is admitted immediately and nothing is
    recorded, which is what "no proactive limit" means.

    Implemented as an async context manager so call sites can do::

        async with limiter:
            ...

    Why the queue exists. Until 6.62.0 a blocked caller slept until the oldest
    timestamp expired and then raced every other blocked caller for a lock.
    Arrival order was discarded, so one caller could lose that race over and
    over: 24 concurrent requests measured a p50 of 938 ms against a p95 of
    54 397 ms on one batch, which is starvation rather than queueing. Waiters
    now take a ticket. The slot is reserved by the pump at the instant it
    becomes free and handed to the waiter that has held its ticket longest, so
    the wait is bounded by the queue ahead of it and by nothing else.
    """

    def __init__(self, rate_limit: int, rate_window: float) -> None:
        if rate_limit < 0:
            raise ValueError("rate_limit must be >= 0")
        if rate_window <= 0:
            raise ValueError("rate_window must be > 0")

        self._rate_limit = int(rate_limit)
        self._rate_window = float(rate_window)
        self._times: deque[float] = deque()
        self._waiters: deque[asyncio.Future[float]] = deque()
        self._timer: asyncio.TimerHandle | None = None

    @property
    def unlimited(self) -> bool:
        """Whether this limiter paces anything at all."""
        return self._rate_limit <= UNLIMITED_RATE_LIMIT

    async def acquire(self) -> None:
        await self._acquire(None)

    async def acquire_if(self, allowed: Callable[[], bool]) -> bool:
        """Record an acquisition only if ``allowed`` still holds at admission.

        Capacity is awaited first. The synchronous condition and timestamp write
        then run without yielding, so a rejected admission consumes no quota.
        """
        return await self._acquire(allowed)

    async def _acquire(self, allowed: Callable[[], bool] | None) -> bool:
        if self.unlimited:
            # Nothing to pace, so nothing to record either: an empty window
            # keeps the disabled limiter O(1) and keeps a later re-enable
            # from inheriting a backlog of stamps nobody was throttled by.
            return allowed is None or allowed()

        waiter: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        # Synchronous, and it may resolve ``waiter`` before the await below --
        # in which case awaiting a done future costs no loop hop at all, so the
        # uncontended path is as cheap as the lock it replaces.
        self._pump()
        try:
            stamp = await waiter
        except asyncio.CancelledError:
            # A cancellation that lands between the reservation and the resume
            # would otherwise burn a slot nobody used.
            if waiter.done() and not waiter.cancelled():
                self._release(waiter.result())
            raise
        if allowed is not None:
            if not allowed():
                self._release(stamp)
                return False
            # Re-stamped at the instant the condition committed, not at the
            # instant the pump reserved the slot. The two differ by one loop
            # resumption, and the window this admission occupies has to start
            # where the request really did.
            self._restamp(stamp)
        return True

    def _restamp(self, stamp: float) -> None:
        """Move a reserved slot to now, keeping the window in arrival order."""
        try:
            self._times.remove(stamp)
        except ValueError:
            return
        # Appended, never written in place: ``time.monotonic()`` cannot go
        # backwards, so the newest stamp belongs at the end and the deque
        # stays sorted -- which is what makes pruning from the left correct.
        self._times.append(time.monotonic())

    def _release(self, stamp: float) -> None:
        """Give back a reserved slot that was never used."""
        try:
            self._times.remove(stamp)
        except ValueError:
            return
        self._pump()

    def _prune(self, now: float) -> None:
        cutoff = now - self._rate_window
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()

    def _pump(self) -> None:
        """Hand every free slot to the waiters that have queued longest."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._prune(time.monotonic())
        while self._waiters and len(self._times) < self._rate_limit:
            waiter = self._waiters.popleft()
            if waiter.done():
                # Cancelled while queued. It holds no slot, so skip it.
                continue
            stamp = time.monotonic()
            self._times.append(stamp)
            waiter.set_result(stamp)
        if not self._waiters or not self._times:
            return
        # One timer for the whole queue, re-armed each time the head expires.
        # A timer per waiter is what produced the thundering herd this queue
        # exists to remove.
        delay = max(0.0, (self._times[0] + self._rate_window) - time.monotonic())
        self._timer = asyncio.get_running_loop().call_later(delay, self._pump)

    async def __aenter__(self) -> StrictSlidingWindowLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


# Headers providers actually use to say when a limit resets. ``Retry-After`` is
# the RFC 9110 standard; the ``x-ratelimit-reset-*`` family is the de-facto
# convention OpenAI, Anthropic, Groq, and Mistral all ship.
RATE_LIMIT_RESET_HEADERS: tuple[str, ...] = (
    "retry-after-ms",
    "retry-after",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "ratelimit-reset",
)

DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
MAX_RATE_LIMIT_COOLDOWN_SECONDS = 3600.0
#: Distinct models that must be rate-limited on one key at the same time before
#: the key itself is benched. Mirrors ``CREDENTIAL_MODEL_BENCH_ESCALATION`` in
#: the config layer, which core deliberately does not import.
DEFAULT_MODEL_BENCH_ESCALATION = 2

_DURATION_PATTERN = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?"
)


def parse_rate_limit_duration(name: str, raw: str) -> float | None:
    """Parse one rate-limit header value into seconds, or None if unparseable."""

    text = raw.strip()
    if not text:
        return None
    if name == "retry-after-ms":
        try:
            return float(text) / 1000.0
        except ValueError:
            return None
    # Values like "1s", "6m0s", "250ms" appear in the wild alongside plain
    # numbers, so parse the suffixed forms rather than discarding them.
    try:
        return float(text)
    except ValueError:
        pass
    match = _DURATION_PATTERN.fullmatch(text)
    if match and any(match.groups()):
        hours, minutes, seconds, millis = (float(g or 0) for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds + millis / 1000.0
    with contextlib.suppress(ValueError, TypeError):
        # Retry-After also permits an HTTP-date.
        when = parsedate_to_datetime(text)
        if when is not None:
            return max(
                0.0, (when - datetime.now(tz=when.tzinfo or UTC)).total_seconds()
            )
    return None


def retry_after_seconds(headers: object) -> float | None:
    """Seconds the upstream asked us to wait, or None when it did not say.

    Returning None rather than a default keeps "the server told us" separate
    from "we guessed", so callers can decide what an absent header means.
    """

    # Duck-typed rather than annotated as Mapping: callers hand us whatever
    # ``getattr(response, "headers", None)`` produced, which varies by client.
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for name in RATE_LIMIT_RESET_HEADERS:
        try:
            raw = getter(name)
        except TypeError:
            return None
        if raw is None:
            continue
        seconds = parse_rate_limit_duration(name, str(raw))
        if seconds is not None and seconds >= 0:
            return min(seconds, MAX_RATE_LIMIT_COOLDOWN_SECONDS)
    return None
