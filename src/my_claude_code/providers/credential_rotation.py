"""Multi-credential rotation engine shared by rotating provider wrappers.

Thin async adapter over the consolidated engine in
``my_claude_code.core.credential_rotation`` (:data:`PROVIDER_TUNING`): this
layer owns asyncio lock semantics and classifies SDK/HTTP errors into pool
failure classes. Every health-transition rule -- ladders, thresholds, probe
admission, selection policies -- lives once in the shared engine.

Health model:
  - HEALTHY: serving requests.
  - COOLDOWN: briefly benched after an error (tiered 10s -> 30s -> 60s -> 120s).
  - CIRCUIT_OPEN: 3+ consecutive failures; benched until cooldown elapses.
  - HALF_OPEN: recovering; a single probe request is allowed through.
  - LOCKED_OUT: auth failure (401/403); escalating lockout 5min -> 1h -> 24h,
    then a half-open probe before full reuse.

Policies:
  - ``single``: always the first key.
  - ``round_robin``: spread requests across healthy keys in turn.
  - ``least_used``: healthy key with the fewest requests goes first.
  - ``failover`` (alias ``on_error``): stick to the first healthy key until it
    fails, then move to the next.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import openai

from my_claude_code.core.credential_rotation import (
    PROVIDER_TUNING,
    RotationEngine,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.providers.failure_policy import (
    retryable_transient_status,
    retryable_upstream_transport_error,
)

ROTATION_POLICIES = frozenset(
    {"single", "round_robin", "least_used", "failover", "on_error"}
)

# Historical names, now derived from the shared tuning preset.
COOLDOWN_TIERS_SECONDS = PROVIDER_TUNING.cooldown_tiers
AUTH_LOCKOUT_TIERS_SECONDS = PROVIDER_TUNING.lockout_tiers
CIRCUIT_OPEN_THRESHOLD = PROVIDER_TUNING.circuit_threshold

STATE_HEALTHY = "HEALTHY"
STATE_COOLDOWN = "COOLDOWN"
STATE_CIRCUIT_OPEN = "CIRCUIT_OPEN"
STATE_HALF_OPEN = "HALF_OPEN"
STATE_LOCKED_OUT = "LOCKED_OUT"


AUTH_STATUS_CODES = (401, 403)


def error_justifies_rotation(error: BaseException) -> bool:
    """Return True when trying a different credential may resolve the failure.

    Rotating is worthwhile for authentication problems, rate limits, upstream
    5xx/overload responses, and transport errors. A plain 400 invalid request
    will fail identically with every key, so it is not rotated.
    """
    if isinstance(error, openai.AuthenticationError):
        return True
    if (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in AUTH_STATUS_CODES
    ):
        return True
    # Providers classify their own SDK/HTTP failures before the wrapper sees
    # them, so a rejected credential arrives as ExecutionFailure(retryable=
    # False) rather than a raw SDK error. ``retryable`` there means "safe to
    # retry the same credential", which a 401 never is -- but a *different*
    # credential may well succeed, and that is exactly what rotation is for.
    # Without this branch a revoked or exhausted key fails the request instead
    # of failing over, defeating multi-key rotation in its main use case.
    if isinstance(error, ExecutionFailure) and error.status_code in AUTH_STATUS_CODES:
        return True
    if retryable_transient_status(error) is not None:
        return True
    return retryable_upstream_transport_error(error)


def _status_from_error(error: BaseException) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    if isinstance(error, openai.APIStatusError):
        return error.status_code
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _failure_class(status: int | None) -> str:
    """Map a classified upstream status onto the shared engine's classes."""
    if status in AUTH_STATUS_CODES:
        return "auth"
    if status == 429:
        return "rate_limit"
    return "transient"


class CredentialRotationState:
    """Pick which credential serves each request under a rotation policy."""

    def __init__(
        self,
        key_count: int,
        policy: str = "single",
        circuit_threshold: int = CIRCUIT_OPEN_THRESHOLD,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy == "on_error":
            policy = "failover"
        canonical = policy if policy in ROTATION_POLICIES else "single"
        tuning = replace(PROVIDER_TUNING, circuit_threshold=circuit_threshold)
        self._engine = RotationEngine(
            key_count, policy=canonical, tuning=tuning, clock=clock
        )
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

    def release_probe(self, index: int) -> None:
        """Clear a half-open probe reservation without judging the credential."""
        self._engine.release_probe(index)

    async def report_success(self, index: int) -> None:
        """Mark a credential as healthy after a successful request."""
        async with self._lock:
            self._engine.succeed(index)

    async def report_failure(self, index: int, error: BaseException) -> bool:
        """Record a failure for one credential; return whether to rotate.

        The return value tells the caller whether trying the next credential
        could resolve this request (auth/rate-limit/5xx/transport errors),
        as opposed to a plain 400 that would fail identically on every key.
        """
        rotate = error_justifies_rotation(error)
        failure_class = _failure_class(_status_from_error(error))
        async with self._lock:
            self._engine.fail(index, failure_class)
        return rotate

    async def report_rate_limit(self, index: int) -> None:
        """Bump the escalation tier without changing health state."""
        async with self._lock:
            self._engine.note_rate_limit(index)

    async def reset_key(self, index: int) -> bool:
        """Manually restore one credential to HEALTHY."""
        async with self._lock:
            return self._engine.restore(index)

    async def reset_all(self) -> int:
        """Restore every non-healthy credential to HEALTHY."""
        async with self._lock:
            return self._engine.restore_all()

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
                "consecutive_failures": slot.consecutive_failures,
                "auth_failures": slot.auth_failures,
                "tier": slot.tier,
                "cooldown_remaining": max(0.0, slot.cooldown_until - now),
                "lockout_remaining": max(0.0, slot.lockout_until - now),
                "is_probing": slot.is_probing,
            }
            for slot in self._engine.slots()
        ]
