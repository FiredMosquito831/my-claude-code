"""Multi-credential rotation engine shared by rotating provider wrappers.

Thin async adapter over the consolidated engine in
``my_claude_code.core.credential_rotation`` (:data:`PROVIDER_TUNING`): this
layer owns asyncio lock semantics and decides which upstream failures are
*about the credential*. Every health-transition rule lives once in the engine.

One rule, two questions
-----------------------

**A key's health changes only on a key-shaped signal.** There are exactly two:

- ``401``/``403`` -- the credential was rejected. It walks the escalating
  lockout ladder (``CREDENTIAL_LOCKOUT_TIERS``, 5min -> 1h -> 24h by default).
- ``429`` -- the credential is throttled. It is benched for exactly the window
  the provider published in its own ``Retry-After`` / ``x-ratelimit-reset-*``
  header, or for ``RATE_LIMIT_COOLDOWN_SECONDS`` when it published none, capped
  at one hour. No ladder, no tier escalation, no circuit breaker.

Everything else -- timeouts, 5xx, 410 model gone, overloaded, 400 invalid
request, context length, transport faults, anything unclassified -- leaves the
credential's health record byte-identical. Those are properties of the model,
the request, or the moment, and the same three keys serve every model in a
fallback chain: charging them walked live keys up an invented 10/30/60/120s
ladder and tripped a breaker at three in a row. Measured on one live install,
that produced "All API keys for this provider are in cooldown" 1,529 times in
a day, driven by a ``410 model gone`` on one chain entry and by first-token
timeouts. A model that does not answer is the model's problem: the fallback
chain moves to the next *model*, not the next key.

**Rotation is a separate question from health.** Trying another credential can
only help when the failure is about this credential or its connection, so
rotation happens for auth, for 429, and for transport-level faults -- and for
nothing else. A timeout, a 5xx, a 410 or any other 4xx raise out of the
rotating loop so the executor advances the fallback chain instead of burning
the remaining keys on a model that is not answering.

Health model:
  - HEALTHY: serving requests.
  - COOLDOWN: rate-limited, for exactly as long as the provider asked.
  - LOCKED_OUT: auth failure (401/403); escalating lockout ladder.

Policies:
  - ``single``: always the first key.
  - ``round_robin``: spread requests across healthy keys in turn.
  - ``least_used``: healthy key with the fewest requests goes first.
  - ``failover`` (alias ``on_error``): stick to the first healthy key until it
    fails, then move to the next.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import httpx
import openai

from my_claude_code.core.credential_rotation import (
    PROVIDER_TUNING,
    RotationEngine,
)
from my_claude_code.core.failures import FailureKind, find_execution_failure
from my_claude_code.core.upstream_ladder import record_credential_decision
from my_claude_code.providers.failure_policy import (
    retryable_upstream_transport_error,
)

logger = logging.getLogger(__name__)

ROTATION_POLICIES = frozenset(
    {"single", "round_robin", "least_used", "failover", "on_error"}
)

AUTH_STATUS_CODES = (401, 403)

#: The only canonical kinds that say anything about the credential that carried
#: the request. An allow-list on purpose: the previous deny-list grew a new
#: exemption every time a class of route-shaped failure was found charging
#: healthy keys, and each addition left the default -- "charge it" -- wrong for
#: everything not yet enumerated.
CREDENTIAL_SHAPED_KINDS = frozenset(
    {FailureKind.AUTHENTICATION, FailureKind.PERMISSION, FailureKind.RATE_LIMIT}
)

#: Kinds that justify handing this request to another credential.
#: ``UNAVAILABLE`` is how providers classify a dead socket or a refused
#: connection: it says nothing about the credential, but another key means
#: another connection, so the attempt is worth making -- for free, because
#: rotation and health accounting are separate decisions.
ROTATING_KINDS = CREDENTIAL_SHAPED_KINDS | {FailureKind.UNAVAILABLE}


def _status_from_error(error: BaseException) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    if isinstance(error, openai.APIStatusError):
        return error.status_code
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _uncharged_reason(status: int | None, failure: BaseException | None) -> str:
    """Why this failure left the credential's health record untouched."""
    if status is not None:
        return f"{status} is not credential-shaped"
    kind = getattr(failure, "kind", None)
    if kind is not None:
        return f"{kind} is not credential-shaped"
    return "the failure is not credential-shaped"


def _charged_reason(
    failure_class: str,
    status: int | None,
    retry_after: float | None,
    benched_for: float,
    auth_failures: int,
) -> str:
    """What the pool did to this credential, in the pool's own numbers."""
    named = str(status) if status is not None else failure_class
    if failure_class == "rate_limit":
        if retry_after is not None:
            return f"{named} with Retry-After {retry_after:g}s"
        return f"{named}, no Retry-After -- operator cooldown {benched_for:.0f}s"
    return f"{named} -- lockout tier {auth_failures}"


def credential_failure_class(error: BaseException) -> str | None:
    """Name the key-shaped signal in ``error``, or ``None`` if there is none.

    ``"auth"`` and ``"rate_limit"`` are the only two the provider pool acts on.
    Providers classify their own SDK/HTTP failures before the wrapper sees
    them, so the canonical kind is read first; raw SDK and ``httpx`` errors
    that reach this layer unclassified are matched on their status code.
    """
    failure = find_execution_failure(error)
    if failure is not None:
        if failure.kind is FailureKind.RATE_LIMIT:
            return "rate_limit"
        if failure.kind in CREDENTIAL_SHAPED_KINDS:
            return "auth"
    if isinstance(error, openai.AuthenticationError | openai.PermissionDeniedError):
        return "auth"
    if isinstance(error, openai.RateLimitError):
        return "rate_limit"
    # A status carried without a matching kind: a provider that reported a 401
    # or a 429 under a coarser classification still described the credential.
    status = _status_from_error(error) if failure is None else failure.status_code
    if status in AUTH_STATUS_CODES:
        return "auth"
    if status == 429:
        return "rate_limit"
    return None


def error_justifies_rotation(error: BaseException) -> bool:
    """Whether trying a different credential could resolve this failure.

    True for the two key-shaped signals and for transport faults, where a
    different key means a different connection. False for everything else --
    a timeout, a 5xx, a 410, any 4xx -- because every key in the pool talks to
    the same model and would meet the same answer. Those raise out of the
    rotating loop so the *fallback chain* gets its turn instead.
    """
    failure = find_execution_failure(error)
    if failure is not None:
        return (
            failure.kind in ROTATING_KINDS
            or failure.status_code in AUTH_STATUS_CODES
            or failure.status_code == 429
        )
    if credential_failure_class(error) is not None:
        return True
    if isinstance(
        error, openai.APITimeoutError | httpx.TimeoutException | TimeoutError
    ):
        # ``openai.APITimeoutError`` subclasses ``APIConnectionError``, so the
        # transport check below would otherwise read a model that never
        # answered as a broken socket and spend the rest of the pool on it.
        return False
    return retryable_upstream_transport_error(error)


class CredentialRotationState:
    """Pick which credential serves each request under a rotation policy."""

    def __init__(
        self,
        key_count: int,
        policy: str = "single",
        *,
        rate_limit_seconds: float,
        lockout_tiers: Sequence[float],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy == "on_error":
            policy = "failover"
        canonical = policy if policy in ROTATION_POLICIES else "single"
        tuning = replace(
            PROVIDER_TUNING,
            rate_limit_seconds=rate_limit_seconds,
            lockout_tiers=tuple(lockout_tiers),
        )
        self._engine = RotationEngine(
            key_count, policy=canonical, tuning=tuning, clock=clock
        )
        # Kept so a bench the engine just installed can be read back as a
        # duration for the request log, on the engine's own clock.
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> str:
        return self._engine.policy

    async def acquire(self, avoid: frozenset[int] = frozenset()) -> int:
        """Return the index of the credential to use for one new request.

        ``avoid`` lists credentials that are healthy but cannot serve right now
        -- rate-limited, or out of daily budget. They are skipped when anything
        else is available, and fall back to normal selection when every
        credential is in that state, so a fully throttled pool still queues on
        its limiter rather than hard-failing.
        """
        async with self._lock:
            selected = self._engine.choose(avoid)
            if selected is None and avoid:
                selected = self._engine.choose(frozenset())
            if selected is None:
                return -1
            self._engine.mark_acquired(selected)
            return selected

    async def report_success(self, index: int) -> None:
        """Mark a credential as healthy after a successful request."""
        async with self._lock:
            self._engine.succeed(index)

    async def report_failure(
        self, index: int, error: BaseException, *, model: str | None = None
    ) -> bool:
        """Record a failure for one credential; return whether to rotate.

        The two answers are independent. Health moves only for a key-shaped
        signal (401/403, or a 429 and the window the provider asked for).
        Rotation additionally covers transport faults. Everything else returns
        ``False`` with the credential untouched, which raises out of the
        rotating loop and lets the fallback chain try the next model -- the
        outcome the user asked for and the one the numbers support.
        """
        rotate = error_justifies_rotation(error)
        failure_class = credential_failure_class(error)
        failure = find_execution_failure(error)
        status = (
            failure.status_code if failure is not None else _status_from_error(error)
        )
        if failure_class is None:
            logger.debug(
                "Credential %d health unchanged: failure is not credential-shaped "
                "(kind=%s, status=%s, model=%s, rotate=%s)",
                index,
                getattr(failure, "kind", None),
                _status_from_error(error),
                model or "unknown",
                rotate,
            )
            # "Health unchanged" was a DEBUG line and nothing else, so a key
            # the pool deliberately did not charge looked identical to one it
            # never saw. Recorded as a decision with a null class: the absence
            # of a bench is the finding.
            record_credential_decision(
                key_index=index,
                cls=None,
                status=status,
                reason=_uncharged_reason(status, failure),
            )
            return rotate
        # ``None`` means the provider published no Retry-After, which the
        # engine answers with the operator's RATE_LIMIT_COOLDOWN_SECONDS --
        # never with a number invented here.
        retry_after = (
            failure.retry_after_seconds
            if failure_class == "rate_limit" and failure is not None
            else None
        )
        async with self._lock:
            self._engine.fail(index, failure_class, retry_after=retry_after)
            # Read the bench back out of the engine that just decided it. The
            # number is never recomputed from tuning here: an operator cooldown,
            # a published Retry-After and a lockout tier all land in the same
            # two deadlines, and only the engine knows which one applied.
            slot = self._engine.slot(index)
            now = self._clock()
            benched_for = max(slot.cooldown_until, slot.lockout_until) - now
        record_credential_decision(
            key_index=index,
            cls=failure_class,
            benched_for_s=benched_for if benched_for > 0 else None,
            status=status,
            retry_after=retry_after,
            reason=_charged_reason(
                failure_class, status, retry_after, benched_for, slot.auth_failures
            ),
        )
        return rotate

    async def reset_key(self, index: int) -> bool:
        """Manually restore one credential to HEALTHY."""
        async with self._lock:
            return self._engine.restore(index)

    async def reset_all(self) -> int:
        """Restore every non-healthy credential to HEALTHY."""
        async with self._lock:
            return self._engine.restore_all()

    def selectable_indexes(self) -> tuple[int, ...]:
        """Credentials the policy could hand out this instant.

        Synchronous and lock-free on purpose: routing asks this question from
        ``throttle_remaining()``, which is a plain method on the provider
        interface. It only reads slot state and expires elapsed benches, the
        same thing ``get_metrics`` already does outside the lock.
        """
        return self._engine.selectable_indexes()

    def bench_remaining_now(self) -> float:
        """Synchronous view of :meth:`shortest_cooldown_remaining`."""
        return self._engine.shortest_bench_remaining()

    async def shortest_cooldown_remaining(self) -> float:
        """Seconds until the soonest non-healthy credential may serve again."""
        async with self._lock:
            return self._engine.shortest_bench_remaining()

    def get_metrics(self) -> list[dict[str, Any]]:
        """Return per-credential health snapshots for dashboards."""
        now = time.monotonic()
        return [
            {
                "state": slot.state.name,
                "request_count": slot.requests,
                "failure_count": slot.failures,
                "auth_failures": slot.auth_failures,
                "rate_limits": slot.rate_limits,
                "cooldown_remaining": max(0.0, slot.cooldown_until - now),
                "lockout_remaining": max(0.0, slot.lockout_until - now),
            }
            for slot in self._engine.slots()
        ]
