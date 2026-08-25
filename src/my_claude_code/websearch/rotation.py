"""In-memory rotation health (KeyPool) over the shared credential engine.

KeyPool is a thin synchronous adapter over the consolidated engine in
``my_claude_code.core.credential_rotation`` (:data:`WEBSEARCH_TUNING`). Key
material, masking, and the admin snapshot shape live here; every transition
rule -- ladders, thresholds, expiry, selection policies -- lives once in the
shared engine.

KeyPool health semantics (in-memory only):

- ``HEALTHY`` -> ``COOLDOWN`` on consecutive failures with tiered backoff
  (10s / 30s / 60s / 120s).
- ``CIRCUIT_OPEN`` when the 4th consecutive failure lands (fixed 60s window);
  failures beyond the threshold fall back to the 120s cooldown tier.
- 401/403 (and quota) failures -> ``LOCKED_OUT`` for 5 minutes, doubling on
  each repeated lockout (capped at one hour); a later success resets it.
- 429 -> dedicated rate-limit cooldown (60s default), honoring the
  provider's own Retry-After, tracked separately from the ladder.

Expired states lazily return to ``HEALTHY`` on the next acquire.
"""

import time
from collections.abc import Callable
from typing import Any

from my_claude_code.config.credentials import mask_key_label
from my_claude_code.core.credential_rotation import (
    WEBSEARCH_TUNING,
    PoolHealthState,
    PoolSlot,
    RotationEngine,
)

__all__ = [
    "ROTATION_POLICIES",
    "KeyHealth",
    "KeyHealthState",
    "KeyPool",
    "default_rotation_policy",
    "mask_key_label",
]

ROTATION_POLICIES: tuple[str, ...] = ("single", "round_robin", "least_used", "failover")
DEFAULT_SINGLE_KEY_POLICY = "single"
DEFAULT_MULTI_KEY_POLICY = "failover"

# Historical constant names, now derived from the shared tuning preset.
COOLDOWN_TIER_SECONDS: tuple[float, ...] = WEBSEARCH_TUNING.cooldown_tiers
CIRCUIT_OPEN_FAILURES = WEBSEARCH_TUNING.circuit_threshold
CIRCUIT_OPEN_SECONDS = WEBSEARCH_TUNING.circuit_fixed_seconds
RATE_LIMIT_COOLDOWN_SECONDS = WEBSEARCH_TUNING.rate_limit_seconds
LOCKOUT_BASE_SECONDS = WEBSEARCH_TUNING.lockout_tiers[0]
LOCKOUT_MAX_SECONDS = max(WEBSEARCH_TUNING.lockout_tiers)


def default_rotation_policy(key_count: int) -> str:
    """Default policy: failover across multiple keys, single for one key."""

    return DEFAULT_MULTI_KEY_POLICY if key_count > 1 else DEFAULT_SINGLE_KEY_POLICY


#: Canonical pool states; the websearch surface uses lowercase values.
KeyHealthState = PoolHealthState


class KeyHealth:
    """Live read-only view over one pooled key's runtime health.

    The historical pool handed out its own mutable record from
    ``health_at``, so a reference held across further transitions keeps
    reading current values rather than a point-in-time copy; this facade
    preserves that contract over the shared engine's slot.
    """

    def __init__(self, *, key: str, slot: PoolSlot) -> None:
        self._key = key
        self._slot = slot

    @property
    def key(self) -> str:
        return self._key

    @property
    def requests(self) -> int:
        return self._slot.requests

    @property
    def successes(self) -> int:
        return self._slot.successes

    @property
    def failures(self) -> int:
        return self._slot.failures

    @property
    def consecutive_failures(self) -> int:
        return self._slot.consecutive_failures

    @property
    def rate_limits(self) -> int:
        return self._slot.rate_limits

    @property
    def lockouts(self) -> int:
        return self._slot.lockouts

    @property
    def state(self) -> KeyHealthState:
        return self._slot.state

    @property
    def state_until(self) -> float:
        # Monotonic deadline; 0 while healthy.
        if self._slot.state is KeyHealthState.HEALTHY:
            return 0.0
        return self._slot.deadline

    @property
    def last_error(self) -> str | None:
        return self._slot.last_error

    @property
    def last_used_at(self) -> float | None:
        return self._slot.last_used_at

    def __repr__(self) -> str:
        names = (
            "key",
            "requests",
            "successes",
            "failures",
            "consecutive_failures",
            "rate_limits",
            "lockouts",
            "state",
            "state_until",
            "last_error",
            "last_used_at",
        )
        fields = ", ".join(f"{name}={getattr(self, name)!r}" for name in names)
        return f"KeyHealth({fields})"


class KeyPool:
    """In-memory rotation pool over one provider's API keys."""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        policy: str = DEFAULT_MULTI_KEY_POLICY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if policy not in ROTATION_POLICIES:
            raise ValueError(
                f"credential_rotation must be one of {ROTATION_POLICIES}, got {policy!r}"
            )
        if not keys:
            raise ValueError("KeyPool requires at least one key slot")
        self._keys = tuple(keys)
        self._clock = clock
        self._engine = RotationEngine(
            len(self._keys), policy=policy, tuning=WEBSEARCH_TUNING, clock=clock
        )

    @property
    def policy(self) -> str:
        return self._engine.policy

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def key_at(self, index: int) -> str:
        return self._keys[index]

    def health_at(self, index: int) -> KeyHealth:
        return self._view(index, self._engine.slot(index))

    def _view(self, index: int, slot: PoolSlot) -> KeyHealth:
        return KeyHealth(key=self._keys[index], slot=slot)

    def acquire(
        self, *, exclude: frozenset[int] = frozenset()
    ) -> tuple[int, str] | None:
        """Return the next usable ``(index, key)`` per policy, or None when exhausted."""

        index = self._engine.choose(exclude)
        if index is None:
            return None
        self._engine.mark_acquired(index)
        return index, self._keys[index]

    def report_success(self, index: int) -> None:
        self._engine.succeed(index)

    def report_failure(
        self, index: int, *, kind: str, message: str | None = None
    ) -> None:
        """Record a non-429 failure; auth/quota lock out, others climb the ladder."""

        if kind == "rate_limit":
            self.report_rate_limit(index, message=message)
            return
        failure_class = "auth" if kind in ("auth", "quota") else "transient"
        self._engine.fail(index, failure_class, message=message)

    def report_rate_limit(
        self,
        index: int,
        *,
        message: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Record a 429, honouring the provider's own reset when it sent one.

        A fixed cooldown either benches a key that resets in a second or keeps
        hammering one that needs an hour. ``retry_after_seconds`` is whatever
        the provider published; falling back to the default only when it said
        nothing.
        """

        self._engine.note_rate_limit(
            index, message=message, retry_after=retry_after_seconds
        )

    def snapshot(self) -> dict[str, Any]:
        """Health snapshot for admin UI / diagnostics (keys masked)."""

        self._engine.refresh()
        now = self._clock()
        keys: list[dict[str, Any]] = []
        for index, slot in enumerate(self._engine.slots()):
            healthy = slot.state is KeyHealthState.HEALTHY
            keys.append(
                {
                    "index": index,
                    "key_label": mask_key_label(self._keys[index]),
                    "state": slot.state.value,
                    "state_remaining_seconds": (
                        round(max(0.0, slot.deadline - now), 3) if not healthy else 0.0
                    ),
                    "requests": slot.requests,
                    "successes": slot.successes,
                    "failures": slot.failures,
                    "consecutive_failures": slot.consecutive_failures,
                    "rate_limits": slot.rate_limits,
                    "lockouts": slot.lockouts,
                    "last_error": slot.last_error,
                }
            )
        return {"policy": self._engine.policy, "keys": keys}
