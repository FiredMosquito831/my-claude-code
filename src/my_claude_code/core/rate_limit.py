"""Shared rate limiting primitives: sliding-window limiter and reset parsing."""

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class StrictSlidingWindowLimiter:
    """Strict sliding window limiter.

    Guarantees: at most ``rate_limit`` acquisitions in any interval of length
    ``rate_window`` (seconds).

    Implemented as an async context manager so call sites can do::

        async with limiter:
            ...
    """

    def __init__(self, rate_limit: int, rate_window: float) -> None:
        if rate_limit <= 0:
            raise ValueError("rate_limit must be > 0")
        if rate_window <= 0:
            raise ValueError("rate_window must be > 0")

        self._rate_limit = int(rate_limit)
        self._rate_window = float(rate_window)
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        await self._acquire(None)

    async def acquire_if(self, allowed: Callable[[], bool]) -> bool:
        """Record an acquisition only if ``allowed`` still holds at admission.

        Capacity is awaited first. The synchronous condition and timestamp write
        then run without yielding, so a rejected admission consumes no quota.
        """
        return await self._acquire(allowed)

    async def _acquire(self, allowed: Callable[[], bool] | None) -> bool:
        while True:
            wait_time = 0.0
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._rate_window

                while self._times and self._times[0] <= cutoff:
                    self._times.popleft()

                if len(self._times) < self._rate_limit:
                    if allowed is not None and not allowed():
                        return False
                    self._times.append(time.monotonic())
                    return True

                oldest = self._times[0]
                wait_time = max(0.0, (oldest + self._rate_window) - now)

            if wait_time > 0:
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0)

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
