"""Provider-owned upstream rate limiting and retry policy."""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from loguru import logger

from my_claude_code.config.constants import (
    PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
)
from my_claude_code.core.credential_attribution import current_credential
from my_claude_code.core.failures import failure_kind_name, find_execution_failure
from my_claude_code.core.rate_limit import StrictSlidingWindowLimiter
from my_claude_code.core.trace import trace_event
from my_claude_code.core.upstream_ladder import (
    record_limiter_wait,
    record_upstream_try,
    record_upstream_wait,
)
from my_claude_code.core.waiting_clock import credit_waiting
from my_claude_code.providers.failure_policy import (
    ProviderFailureOverride,
    retryable_upstream_status,
    retryable_upstream_transport_error,
    upstream_status,
)

T = TypeVar("T")

UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS = 5
DEFAULT_UPSTREAM_MAX_RETRIES = UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS - 1


def _upstream_body(error: BaseException) -> Any:
    """The upstream's own answer, as it is already materialised on the error.

    Never a fresh read of a stream: by the time an exception reaches the retry
    frame the SDK has parsed its body (``openai`` puts it on ``.body``) or
    ``httpx`` has buffered the response text. Reading either is free.
    """
    body = getattr(error, "body", None)
    if body is not None:
        return body
    response = getattr(error, "response", None)
    return getattr(response, "text", None) if response is not None else None


def _record_ladder_try(
    *,
    error: BaseException,
    effective_error: BaseException,
    status: int | None,
    upstream_ms: float,
) -> None:
    """Write one failed upstream try into the request's ladder.

    Providers classify their own failures, so the status, the canonical kind
    and the published ``Retry-After`` are all extracted here and handed to
    ``core`` as primitives. A no-op outside a logged request.
    """
    failure = find_execution_failure(effective_error)
    index, label = current_credential()
    # The observed status, not the retry gate's answer: a 400 or a 401 is a
    # real thing the upstream said, and the ladder's job is to say what
    # happened rather than what to do about it.
    observed = upstream_status(effective_error)
    if observed is None:
        observed = status
    record_upstream_try(
        key_index=index,
        key_label=label,
        status=observed,
        kind=type(error).__name__ if observed is None else None,
        error_kind=failure_kind_name(effective_error),
        # What the provider PUBLISHED. ``None`` means it published none; the
        # operator's cooldown is never substituted in here.
        retry_after=None if failure is None else failure.retry_after_seconds,
        upstream_ms=upstream_ms,
        body=_upstream_body(error),
    )


class ProviderRateLimiter:
    """
    Rate limiter owned by one provider instance.

    Blocks that provider's requests when a rate-limit error is encountered
    (reactive) and throttles its requests with a strict rolling window
    (proactive).

    Optionally enforces a max_concurrency cap: at most N provider streams
    may be open simultaneously, independent of the sliding window.

    Proactive limits - throttles requests to stay within API limits.
    Reactive limits - pauses all requests while a retry backoff is active.
    Concurrency limit - caps simultaneously open streams.

    Which failures earn a backoff changed in 6.20.0. A 5xx or a transport
    fault still walks the retry ladder, because the same key on the same
    model is the only place a transient gateway blip can be waited out. A
    429 does not, when ``routes_around_model`` is on: it is answered by the
    pool benching the (key, model) pair and the executor moving to another
    model, and the reactive block -- which no router can see -- is not set at
    all. A 5xx never sets one either; one bad gateway response used to
    throttle the credential provider-wide.
    """

    def __init__(
        self,
        rate_limit: int = 40,
        rate_window: float = 60.0,
        max_concurrency: int = 5,
        max_retries: int = DEFAULT_UPSTREAM_MAX_RETRIES,
        backoff_base_seconds: float = PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
        backoff_max_seconds: float = PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
        backoff_jitter_seconds: float = PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
        routes_around_model: bool = False,
    ):
        if rate_limit <= 0:
            raise ValueError("rate_limit must be > 0")
        if rate_window <= 0:
            raise ValueError("rate_window must be > 0")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")

        self._rate_limit = rate_limit
        self._rate_window = float(rate_window)
        self._max_concurrency = max_concurrency
        self._proactive_limiter = StrictSlidingWindowLimiter(
            self._rate_limit, self._rate_window
        )
        self._max_retries = max(0, max_retries)
        # The retry schedule is a deployment choice, not a protocol fact, so
        # it is configured once here rather than hardcoded at every call site.
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._backoff_jitter_seconds = backoff_jitter_seconds
        # False by default so a limiter constructed directly -- in a test, or
        # by a caller that has no settings in hand -- keeps 6.19.0 behaviour.
        # ``factory.py`` injects the operator's real value.
        self._routes_around_model = routes_around_model
        self._blocked_until: float = 0
        self._concurrency_sem = asyncio.Semaphore(max_concurrency)
        logger.info(
            "ProviderRateLimiter initialized "
            f"({rate_limit} req / {rate_window}s, max_concurrency={max_concurrency})"
        )

    async def wait_if_blocked(self) -> bool:
        """
        Wait if currently rate limited or throttle to meet quota.

        Returns:
            True if was reactively blocked and waited, False otherwise.
        """
        # A reactive deadline can be installed or extended while this task waits
        # for proactive capacity. Commit the proactive timestamp only if that
        # deadline is still clear, so retries neither burst nor consume unused quota.
        waited_reactively = False
        while True:
            waited_reactively = (
                await self._wait_for_reactive_block() or waited_reactively
            )
            if await self._proactive_limiter.acquire_if(lambda: not self.is_blocked()):
                return waited_reactively

    async def _wait_for_reactive_block(self) -> bool:
        waited = False
        while (wait_time := self.remaining_wait()) > 0:
            logger.warning(
                "Provider rate limit active (reactive), waiting {:.1f}s...",
                wait_time,
            )
            await asyncio.sleep(wait_time)
            # Recorded after the fact, where the seconds were actually spent.
            # This is the wait that made a 120s deadline expire without the
            # model ever being handed an accepted request.
            record_limiter_wait(wait_time)
            # Credited after the fact for the same reason it is recorded after
            # the fact: the executor re-arms its first-token wait by exactly
            # the seconds that were provably spent with no upstream listening.
            credit_waiting(wait_time)
            waited = True
        return waited

    def extend_reactive_block(self, seconds: float) -> None:
        """
        Extend this provider's reactive block by at least ``seconds`` from now.

        Args:
            seconds: Positive minimum duration for the resulting block.
        """
        if seconds <= 0:
            raise ValueError("reactive block duration must be > 0")
        now = time.monotonic()
        self._blocked_until = max(self._blocked_until, now + seconds)
        logger.warning(
            "Provider rate limit set for {:.1f}s (reactive)",
            max(0.0, self._blocked_until - now),
        )

    def is_blocked(self) -> bool:
        """Check if currently reactively blocked."""
        return time.monotonic() < self._blocked_until

    def remaining_wait(self) -> float:
        """Get remaining reactive wait time in seconds."""
        return max(0.0, self._blocked_until - time.monotonic())

    @asynccontextmanager
    async def concurrency_slot(self) -> AsyncIterator[None]:
        """Async context manager that holds one concurrency slot for a stream.

        Blocks until a slot is available (controlled by max_concurrency).
        """
        await self._concurrency_sem.acquire()
        try:
            yield
        finally:
            self._concurrency_sem.release()

    async def execute_with_retry(
        self,
        fn: Callable[..., Any],
        *args: Any,
        provider_failure_override: ProviderFailureOverride | None = None,
        max_retries: int | None = None,
        base_delay: float | None = None,
        max_delay: float | None = None,
        jitter: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute an async callable with rate limiting and retry on transient limits.

        Waits for the proactive limiter before each attempt.

        An upstream ``5xx`` and a pre-response transport error walk the retry
        ladder: exponential backoff with jitter, on the same key, because a
        transient gateway fault is the one thing a second knock can fix. The
        reactive block is never set for either -- one 502 used to throttle the
        whole credential.

        A ``429`` walks nothing when ``routes_around_model`` is on. It is
        re-raised on the first try so the pool can bench the (key, model)
        pair and the executor can move to another model: 51 of one measured
        request's 57 seconds were spent asleep between retries of a model
        whose 429 arrived in 0.2s, with a healthy fallback one chain slot
        away. With the setting off it retries and blocks exactly as 6.19.0
        did.

        Args:
            fn: Async callable to execute.
            provider_failure_override: Optional provider-specific semantic
                classifier applied before shared retry qualification.
            max_retries: Maximum number of retry attempts after the first failure.
            base_delay: Base delay in seconds for exponential backoff; the
                limiter's configured value when omitted.
            max_delay: Maximum delay cap in seconds; configured when omitted.
            jitter: Maximum random jitter in seconds added to each delay;
                configured when omitted.

        Returns:
            The result of the callable.

        Raises:
            The last exception if all retries are exhausted.
        """
        last_exc: Exception | None = None
        if max_retries is None:
            max_retries = self._max_retries
        if base_delay is None:
            base_delay = self._backoff_base_seconds
        if max_delay is None:
            max_delay = self._backoff_max_seconds
        if jitter is None:
            jitter = self._backoff_jitter_seconds
        total_attempts = 1 + max_retries

        for attempt in range(total_attempts):
            await self.wait_if_blocked()

            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:
                upstream_ms = (time.monotonic() - started) * 1000.0
                effective_error = (
                    provider_failure_override(e)
                    if provider_failure_override is not None
                    else None
                )
                if effective_error is None:
                    effective_error = e
                status = retryable_upstream_status(effective_error)
                transport_error = status is None and retryable_upstream_transport_error(
                    effective_error
                )
                # The ladder is written here because this is the only frame
                # that sees every try: the status, the credential in flight,
                # and the body the upstream sent back. Recording precedes no
                # decision -- the classification above and the control flow
                # below are byte-identical to what they were.
                _record_ladder_try(
                    error=e,
                    effective_error=effective_error,
                    status=status,
                    upstream_ms=upstream_ms,
                )
                if status is None and not transport_error:
                    raise
                if status == 429 and self._routes_around_model:
                    # A 429 on a pooled credential is answered by routing, not
                    # by waiting: the pool benches the (key, model) pair and
                    # the executor moves to another model. Sleeping here spent
                    # 51 of one measured request's 57 seconds on a model whose
                    # 429 arrives in 0.2s, with a healthy fallback one chain
                    # slot away. The try is already recorded above.
                    raise

                if status is None:
                    label = f"Provider transport error ({type(e).__name__})"
                else:
                    label = (
                        "Rate limited (429)"
                        if status == 429
                        else f"Upstream server error ({status})"
                    )
                last_exc = e
                if attempt >= max_retries:
                    logger.warning(
                        "{} retry exhausted after {} retries (attempts={})",
                        label,
                        max_retries,
                        total_attempts,
                    )
                    break

                delay = min(base_delay * (2**attempt), max_delay)
                delay += random.uniform(0, jitter)
                attempt_no = attempt + 1
                logger.warning(
                    "{}, attempt {}/{}. Retrying in {:.1f}s...",
                    label,
                    attempt_no,
                    total_attempts,
                    delay,
                )
                trace_event(
                    stage="provider",
                    event="provider.retry.scheduled",
                    source="provider",
                    status_code=status,
                    exc_type=type(e).__name__,
                    attempt=attempt_no,
                    max_attempts=total_attempts,
                    delay_s=round(delay, 3),
                )
                if status == 429 and not self._routes_around_model:
                    # Only a rate limit, and only when this limiter is the
                    # thing that answers one. A 5xx is the gateway failing,
                    # not the credential being throttled, and blocking every
                    # request on this provider for it spends a cooldown
                    # nobody asked for; a routed-around 429 has already been
                    # spent once, as the pool's (key, model) bench, and
                    # installing it again here would charge the same seconds
                    # twice -- once where a router can see them, once where
                    # it cannot.
                    self.extend_reactive_block(delay)
                await asyncio.sleep(delay)
                # Back-fills the try just recorded, so one ladder row carries
                # both the status and the sleep it bought.
                record_upstream_wait(delay)
                credit_waiting(delay)
            else:
                # The try that worked is a fact too: without it a ladder that
                # ends in a success reads as if the last 429 were the outcome.
                index, label = current_credential()
                record_upstream_try(
                    key_index=index,
                    key_label=label,
                    upstream_ms=(time.monotonic() - started) * 1000.0,
                )
                return result

        assert last_exc is not None
        raise last_exc
