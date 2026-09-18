"""Shared rate limiting primitives: sliding-window limiter and reset parsing."""

import asyncio
import contextlib
import json
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
#: The bound on a reset the host published in its *body* rather than a header.
#:
#: One hour is the right sanity cap for a header: ``Retry-After`` is a
#: per-request courtesy and a value beyond an hour is almost always a bug or a
#: hostile number. A JSON reset is a different statement. The OpenCode free
#: tier publishes ``retryAfter`` as the seconds remaining until the next UTC
#: midnight -- its daily allowance, computed by the vendor's own limiter as
#: ``ceil((86_400_000 - now % 86_400_000) / 1000)`` -- so clamping it to an
#: hour means retrying a model the host has already said will refuse for the
#: rest of the day.
#:
#: Still bounded, and bounded at a day: a value read out of a response body is
#: somebody else's number controlling MCC's scheduler, and the semantics that
#: justify reading it never exceed one day.
MAX_HOST_STATED_COOLDOWN_SECONDS = 86_400.0

#: The body fields a host uses to say when, in JSON rather than in a header.
#: ``retryAfter`` is the one measured -- see ``providers/openai_chat/
#: identity_enforcement.py``, which has parsed it into a diagnostic fact since
#: 6.69.0 without anything acting on it. The snake_case spellings are the same
#: field under the other naming convention.
RETRY_AFTER_BODY_FIELDS: tuple[str, ...] = (
    "retryafter",
    "retry_after",
    "retryafterseconds",
    "retry_after_seconds",
)
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


def retry_after_seconds(
    headers: object, max_seconds: float | None = MAX_RATE_LIMIT_COOLDOWN_SECONDS
) -> float | None:
    """Seconds the upstream asked us to wait, or None when it did not say.

    Returning None rather than a default keeps "the server told us" separate
    from "we guessed", so callers can decide what an absent header means.

    ``max_seconds`` is the ceiling one header may request. It defaults to
    :data:`MAX_RATE_LIMIT_COOLDOWN_SECONDS`, which is what every caller got
    before it was a parameter; ``None`` removes the ceiling entirely. The
    operator's ``RATE_LIMIT_COOLDOWN_MAX_SECONDS`` reaches here through
    :class:`RateLimitCooldown`, and nowhere else invents a bound of its own.
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
            return seconds if max_seconds is None else min(seconds, max_seconds)
    return None


def retry_after_from_body(body: object, depth: int = 0) -> float | None:
    """Seconds the upstream published in its JSON body, or ``None``.

    The counterpart to :func:`retry_after_seconds` for hosts that answer a 429
    with a document instead of a header. Read only after the headers, so a host
    that sends both keeps the header's precision.

    Walks the small set of envelopes error bodies actually arrive in --
    ``{"error": {...}}``, ``{"data": {...}}``, ``{"detail": {...}}`` -- to a
    fixed depth, and accepts a number or a numeric string. A negative, a bool,
    or anything else is not an answer, and the caller falls back to its
    default rather than to a value it had to guess at.

    The result is capped at :data:`MAX_HOST_STATED_COOLDOWN_SECONDS`.
    """

    if depth > 3:
        return None
    if isinstance(body, str | bytes):
        try:
            return retry_after_from_body(json.loads(body), depth + 1)
        except ValueError:
            return None
    if not isinstance(body, Mapping):
        return None
    for key, value in body.items():
        if str(key).strip().lower() not in RETRY_AFTER_BODY_FIELDS:
            continue
        seconds = _positive_seconds(value)
        if seconds is not None:
            return min(seconds, MAX_HOST_STATED_COOLDOWN_SECONDS)
    for key in ("error", "data", "detail"):
        nested = retry_after_from_body(body.get(key), depth + 1)
        if nested is not None:
            return nested
    return None


def _positive_seconds(value: object) -> float | None:
    """A non-negative number of seconds, from a number or a numeric string."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if seconds != seconds or seconds < 0:  # NaN or negative
        return None
    return seconds


#: The three answers to "what does a 429 cost the credential that met it".
#:
#: ``provider`` -- honour the wait the host published, under the operator's
#: ceiling, and fall back to ``RATE_LIMIT_COOLDOWN_SECONDS`` when it published
#: none. Every release up to 7.21.0 did exactly this and nothing else.
#: ``fixed`` -- always ``RATE_LIMIT_COOLDOWN_SECONDS``, whatever the host says.
#: ``off`` -- a 429 benches nothing at all.
RATE_LIMIT_COOLDOWN_MODES: tuple[str, ...] = ("provider", "fixed", "off")
DEFAULT_RATE_LIMIT_COOLDOWN_MODE = "provider"


@dataclass(frozen=True, slots=True)
class RateLimitCooldown:
    """The operator's 429 cooldown policy, and the one place it is applied.

    Three numbers an operator sets on *Limits & Resilience*, carried together
    because they only mean anything together:

    - ``mode`` -- one of :data:`RATE_LIMIT_COOLDOWN_MODES`.
    - ``fallback_seconds`` -- ``RATE_LIMIT_COOLDOWN_SECONDS``: the bench used
      when the host published no wait, and the *only* bench in ``fixed`` mode.
      0 means "do not pause", which it has meant since the setting existed.
    - ``max_seconds`` -- ``RATE_LIMIT_COOLDOWN_MAX_SECONDS``: the ceiling on a
      wait the host published in a *header*. Defaults to the 3600 that was
      hard-coded until 7.22.0; 0 removes the ceiling.

    :meth:`resolve` is the rule. Every bench, every (key, model) bench and
    every reactive block in the codebase asks this one method how long, so
    there is no second copy of the policy to drift from this one.

    Deliberately **not** applied to a wait the host published in its response
    *body*. That number is bounded by
    :data:`MAX_HOST_STATED_COOLDOWN_SECONDS` (one day) for the reason recorded
    there: a body-stated ``retryAfter`` is a statement about the account's
    daily allowance, not a per-request courtesy, and clamping it to an hour is
    the defect 7.6.3 was raised to fix. An operator who wants a body-stated
    day-long wait ignored says so with ``fixed`` or ``off``, which do read the
    body path -- lowering the header ceiling must not silently re-create
    hammering a host that already said "not until midnight".
    """

    mode: str = DEFAULT_RATE_LIMIT_COOLDOWN_MODE
    fallback_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    max_seconds: float = MAX_RATE_LIMIT_COOLDOWN_SECONDS

    @property
    def benches(self) -> bool:
        """Whether a 429 costs the credential anything at all."""
        return self.mode != "off"

    @property
    def honours_provider(self) -> bool:
        """Whether a wait the host published is read at all."""
        return self.mode != "fixed" and self.benches

    @property
    def header_cap(self) -> float | None:
        """The ceiling for :func:`retry_after_seconds`; ``None`` is uncapped."""
        return None if self.max_seconds <= 0 else self.max_seconds

    def bound(self, seconds: float) -> float:
        """Clamp one host-stated wait to the operator's ceiling."""
        cap = self.header_cap
        return seconds if cap is None else min(seconds, cap)

    def resolve(self, stated: float | None) -> float:
        """How long one 429 pauses, given what the host published (or None).

        The single owner of the rule. ``off`` is 0 -- and every caller checks
        :attr:`benches` before it records anything, so 0 here never reads back
        as "benched for no time at all".

        ``stated`` has already met the ceiling, in the reader that produced it
        (:func:`retry_after_seconds` for a header, and only there). Clamping
        again here would apply the header ceiling to a *body*-stated wait too,
        which is the one thing this policy must not do.
        """
        if not self.benches:
            return 0.0
        if stated is None or not self.honours_provider:
            return self.fallback_seconds
        return stated


#: 7.21.0 behaviour exactly: honour the host, ceiling at one hour, fall back to
#: sixty seconds. What a caller with no settings in hand gets.
DEFAULT_RATE_LIMIT_COOLDOWN = RateLimitCooldown()
