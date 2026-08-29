"""One parameterized credential-rotation engine shared by every key pool.

Both rotation frontends delegate here instead of maintaining divergent copies
of the same mechanism:

- ``providers/credential_rotation.py`` -- the async, lock-guarded provider
  engine (adds SDK failure classification on top).
- ``websearch/rotation.py`` -- the synchronous websearch ``KeyPool`` (adds key
  material, masking, and admin snapshots on top).

The mechanism -- health states, cooldown/lockout ladders, the circuit breaker,
and the selection policies -- exists once. Every point of divergence between
the two pools is a :class:`RotationTuning` field.

The presets are no longer variations on one policy. :data:`WEBSEARCH_TUNING`
keeps the failure ladder and the breaker: a search key that keeps erroring is
worth resting. :data:`PROVIDER_TUNING` deliberately does not. A model
provider's pool only ever hears about *credential-shaped* signals -- auth
rejections and 429s -- so its slots are benched for exactly as long as the
provider asked and never for a fault the credential did not cause. The
provider adapter enforces that by never classifying anything as
``"transient"``; the generic ladder below is reachable only from websearch.

There is no half-open probe state. It existed to re-admit a single request to
a credential coming back from a long bench, and it leaked: a slot reserved on
acquire stayed reserved whenever the request ended without reporting either
success or failure, leaving the key permanently unselectable until someone
reset it by hand. Waking straight to HEALTHY costs at most one extra failed
request and cannot strand a key.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from my_claude_code.core.rate_limit import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
)

POLICIES: frozenset[str] = frozenset(
    {"single", "round_robin", "least_used", "failover"}
)


class PoolHealthState(StrEnum):
    """Canonical health states for one pooled credential."""

    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"
    LOCKED_OUT = "locked_out"


@dataclass(frozen=True, slots=True)
class RotationTuning:
    """Behavioral configuration for :class:`RotationEngine`.

    Defaults reproduce the historical generic engine; each preset below names
    the fields it means to differ on.
    """

    cooldown_tiers: tuple[float, ...] = (10.0, 30.0, 60.0, 120.0)
    #: Auth/quota lockout ladder, indexed by consecutive auth failures and
    #: clamped at the last entry.
    lockout_tiers: tuple[float, ...] = (300.0, 3600.0, 86400.0)
    #: Consecutive generic failures that trip the circuit breaker.
    circuit_threshold: int = 3
    #: False: trip on ``>= threshold`` with a cooldown-tier open window.
    #: True: trip exactly on the threshold-th failure with one fixed window,
    #: and fall back to the cooldown ladder for failures beyond it.
    circuit_exact: bool = False
    #: Open-window length in ``circuit_exact`` mode.
    circuit_fixed_seconds: float = 60.0
    #: Whether an auth failure resets the generic consecutive-failure counter
    #: (websearch) or climbs alongside it (provider).
    lockout_resets_consecutive: bool = False
    #: ``ladder``: 429s escalate the shared cooldown tier ladder and never trip
    #: the breaker; a standalone rate-limit note only bumps the tier.
    #: ``fixed``: 429s bench the slot for a flat window (honoring the
    #: provider's own Retry-After), outside the failure ladder.
    rate_limit_mode: Literal["ladder", "fixed"] = "ladder"
    #: Fixed-mode default window when the provider published no Retry-After.
    rate_limit_seconds: float = 60.0
    #: Fixed-mode cap so a hostile header cannot bench a slot indefinitely.
    rate_limit_max_seconds: float | None = None
    #: ``single`` serves slot 0 unconditionally, ignoring blocklists and
    #: health (provider analytics contract); when False, a benched slot 0
    #: dries the pool up.
    single_ignores_blocklist: bool = True
    #: One-slot pools serve slot 0 under any policy (provider); when False,
    #: normal usability rules apply.
    single_key_forces_slot_zero: bool = True


#: The model-provider credential pool. A 429 benches the slot for exactly the
#: window the provider published, falling back to the operator's
#: ``RATE_LIMIT_COOLDOWN_SECONDS`` when it published none, under the same
#: one-hour sanity cap the rest of the stack applies to a header. The
#: ``cooldown_tiers``/``circuit_threshold`` defaults are inherited but
#: unreachable: nothing in the provider path classifies a failure as generic.
PROVIDER_TUNING = RotationTuning(
    rate_limit_mode="fixed",
    rate_limit_seconds=DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    rate_limit_max_seconds=MAX_RATE_LIMIT_COOLDOWN_SECONDS,
)

#: Reproduces the websearch KeyPool: doubling lockout capped at one hour, the
#: circuit tripping exactly on the 4th consecutive failure for a fixed minute,
#: flat rate-limit windows honoring Retry-After, and strict single-pool
#: exhaustion.
WEBSEARCH_TUNING = RotationTuning(
    lockout_tiers=(300.0, 600.0, 1200.0, 2400.0, 3600.0),
    circuit_threshold=4,
    circuit_exact=True,
    circuit_fixed_seconds=60.0,
    lockout_resets_consecutive=True,
    rate_limit_mode="fixed",
    rate_limit_seconds=60.0,
    rate_limit_max_seconds=3600.0,
    single_ignores_blocklist=False,
    single_key_forces_slot_zero=False,
)


@dataclass(slots=True)
class PoolSlot:
    """Mutable per-credential runtime health tracked by :class:`RotationEngine`.

    Superset record: adapters project the fields their surfaces expose. Raw
    credential material deliberately stays out of the core; pools keep their
    own key storage aligned by index.
    """

    # Usage counters.
    requests: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    rate_limits: int = 0
    lockouts: int = 0
    # Escalation indices: auth_failures walks lockout_tiers, tier walks
    # cooldown_tiers. They escalate independently so unrelated 5xx/transport
    # errors cannot inflate a lockout tier and vice versa.
    auth_failures: int = 0
    tier: int = 0
    # Health machinery. Only one deadline is live at a time; the inactive one
    # is stale until the next transition overwrites it.
    state: PoolHealthState = PoolHealthState.HEALTHY
    cooldown_until: float = 0.0
    lockout_until: float = 0.0
    last_used_at: float | None = None
    last_error: str | None = None

    @property
    def deadline(self) -> float:
        """The live benching deadline for the current state."""
        if self.state is PoolHealthState.LOCKED_OUT:
            return self.lockout_until
        return self.cooldown_until


class RotationEngine:
    """Synchronous rotation machinery over ``key_count`` pooled credentials.

    Adapters add concurrency control (the provider engine serializes access
    with an asyncio lock), error classification, and presentation. All clock
    reads go through ``clock`` so tests can travel in time.
    """

    def __init__(
        self,
        key_count: int,
        *,
        policy: str,
        tuning: RotationTuning,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if key_count <= 0:
            raise ValueError("key_count must be > 0")
        if policy not in POLICIES:
            raise ValueError(
                f"policy must be one of {sorted(POLICIES)}, got {policy!r}"
            )
        self._count = key_count
        self._policy = policy
        self._tuning = tuning
        self._clock = clock
        self._circuit_threshold = max(1, tuning.circuit_threshold)
        self._slots = [PoolSlot() for _ in range(key_count)]
        self._cursor = 0

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def tuning(self) -> RotationTuning:
        return self._tuning

    @property
    def circuit_threshold(self) -> int:
        return self._circuit_threshold

    def slot(self, index: int) -> PoolSlot:
        return self._slots[index]

    def slots(self) -> list[PoolSlot]:
        return self._slots

    def refresh(self) -> None:
        """Expire benched slots whose deadline has passed, straight to HEALTHY.

        Deadlines are left stale -- projections gate on the live state,
        matching the historical engines.
        """
        now = self._clock()
        for slot in self._slots:
            if slot.state is PoolHealthState.HEALTHY:
                continue
            deadline = (
                slot.lockout_until
                if slot.state is PoolHealthState.LOCKED_OUT
                else slot.cooldown_until
            )
            if deadline > 0 and now >= deadline:
                slot.state = PoolHealthState.HEALTHY

    def selectable(self, index: int) -> bool:
        """Whether slot ``index`` may take new work right now."""
        return self._slots[index].state is PoolHealthState.HEALTHY

    def _forced_single(self) -> bool:
        """Whether the policy serves slot 0 regardless of blocklists and health."""
        return (self._policy == "single" and self._tuning.single_ignores_blocklist) or (
            self._count == 1 and self._tuning.single_key_forces_slot_zero
        )

    def selectable_indexes(self) -> tuple[int, ...]:
        """Slots :meth:`choose` could hand out right now.

        Answers the same question as ``choose`` without consuming a policy
        turn, and -- unlike per-slot :meth:`selectable` -- it honors the
        forced-single overrides. A caller that skipped them would decide a
        single-key pool could not serve while ``choose`` was still handing out
        slot 0, and would bench a provider that was in fact working.
        """
        self.refresh()
        if self._forced_single():
            return (0,)
        candidates = tuple(
            index for index in range(self._count) if self.selectable(index)
        )
        if self._policy == "single":
            return (0,) if 0 in candidates else ()
        return candidates

    def choose(self, blocked: frozenset[int] = frozenset()) -> int | None:
        """Pure policy choice among selectable slots, skipping ``blocked``.

        Performs the lazy expiry refresh; performs no bookkeeping. Returns
        None when nothing qualifies.
        """
        self.refresh()
        if self._forced_single():
            # Still reported like any other policy: usage must be counted, or
            # per-credential analytics stay empty for the default pool.
            return 0
        candidates = [
            index
            for index in range(self._count)
            if index not in blocked and self.selectable(index)
        ]
        if self._policy == "single":
            # The single policy may only ever serve from slot 0.
            candidates = [index for index in candidates if index == 0]
        if not candidates:
            return None
        return self._select(candidates)

    def mark_acquired(self, index: int) -> None:
        """Record that slot ``index`` now serves one new request."""
        slot = self._slots[index]
        now = self._clock()
        slot.requests += 1
        slot.last_used_at = now

    def succeed(self, index: int) -> None:
        """Mark a credential fully healthy after a successful request."""
        if not (0 <= index < self._count):
            return
        slot = self._slots[index]
        slot.successes += 1
        self._clear_benching(slot)

    def fail(
        self,
        index: int,
        failure_class: str,
        *,
        message: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Record one classified failure.

        ``failure_class`` is one of ``"auth"`` (lockout ladder), ``"rate_limit"``
        (tuning-dependent), or ``"transient"`` (cooldown ladder / breaker).
        Classification from raw errors belongs to the adapters; the engine owns
        what each class means for health.

        ``retry_after`` is the wait the upstream published with its 429. A
        fixed-mode pool benches for exactly that, under the cap; a ladder-mode
        pool has no flat window to put it in and ignores it.
        """
        if not (0 <= index < self._count):
            return
        slot = self._slots[index]
        now = self._clock()
        slot.failures += 1
        slot.last_error = message
        if failure_class == "auth":
            self._auth_failure(slot, now)
        elif failure_class == "rate_limit":
            if self._tuning.rate_limit_mode == "fixed":
                self._fixed_rate_window(slot, now, retry_after)
            else:
                self._escalate_tier_window(slot, now)
        else:
            self._generic_failure(slot, now)

    def note_rate_limit(
        self,
        index: int,
        *,
        message: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Record a 429 observed outside the failure pipeline.

        Fixed mode benches the slot (honoring ``retry_after`` under the cap);
        ladder mode only escalates the tier, leaving state untouched.
        """
        if not (0 <= index < self._count):
            return
        slot = self._slots[index]
        if self._tuning.rate_limit_mode == "fixed":
            slot.failures += 1
            slot.last_error = message
            self._fixed_rate_window(slot, self._clock(), retry_after)
        else:
            slot.tier = min(slot.tier + 1, len(self._tuning.cooldown_tiers))

    def shortest_bench_remaining(self) -> float:
        """Seconds until the soonest non-healthy slot may serve again."""
        self.refresh()
        now = self._clock()
        remaining = [
            max(slot.cooldown_until, slot.lockout_until) - now
            for slot in self._slots
            if slot.state is not PoolHealthState.HEALTHY
        ]
        positives = [value for value in remaining if value > 0]
        return min(positives) if positives else 0.0

    def restore(self, index: int) -> bool:
        """Manually restore one credential to HEALTHY."""
        if not (0 <= index < self._count):
            return False
        self._clear_benching(self._slots[index])
        return True

    def restore_all(self) -> int:
        """Restore every non-healthy credential to HEALTHY; return how many."""
        count = 0
        for slot in self._slots:
            if slot.state is not PoolHealthState.HEALTHY:
                self._clear_benching(slot)
                count += 1
        return count

    def _select(self, candidates: list[int]) -> int:
        if self._policy == "round_robin":
            ordered = sorted(candidates)
            index = next((i for i in ordered if i >= self._cursor), ordered[0])
            self._cursor = (index + 1) % self._count
            return index
        if self._policy == "least_used":
            return min(
                candidates,
                key=lambda i: (
                    self._slots[i].requests,
                    self._slots[i].last_used_at
                    if self._slots[i].last_used_at is not None
                    else 0.0,
                    i,
                ),
            )
        # failover: stick to the lowest selectable index until it fails.
        return min(candidates)

    def _auth_failure(self, slot: PoolSlot, now: float) -> None:
        slot.auth_failures += 1
        slot.lockouts += 1
        if self._tuning.lockout_resets_consecutive:
            # Auth failures escalate their own ladder; sharing the generic
            # counter would let a quota blip inflate the breaker path.
            slot.consecutive_failures = 0
        else:
            slot.consecutive_failures += 1
        tiers = self._tuning.lockout_tiers
        tier_index = min(slot.auth_failures, len(tiers)) - 1
        slot.state = PoolHealthState.LOCKED_OUT
        slot.lockout_until = now + tiers[tier_index]

    def _generic_failure(self, slot: PoolSlot, now: float) -> None:
        tuning = self._tuning
        slot.consecutive_failures += 1
        slot.tier = min(slot.tier + 1, len(tuning.cooldown_tiers))
        consecutive = slot.consecutive_failures
        if tuning.circuit_exact:
            opened = consecutive == self._circuit_threshold
            window = tuning.circuit_fixed_seconds
        else:
            opened = consecutive >= self._circuit_threshold
            window = tuning.cooldown_tiers[slot.tier - 1]
        if opened:
            # Beyond the threshold (exact mode) the ladder's deepest tier
            # takes over; the breaker window itself stays the fixed value.
            slot.state = PoolHealthState.CIRCUIT_OPEN
        else:
            slot.state = PoolHealthState.COOLDOWN
            window = tuning.cooldown_tiers[slot.tier - 1]
        slot.cooldown_until = now + window

    def _escalate_tier_window(self, slot: PoolSlot, now: float) -> None:
        # A rate limit means the credential is throttled, not broken, so it
        # escalates the cooldown ladder without counting toward the breaker.
        slot.tier = min(slot.tier + 1, len(self._tuning.cooldown_tiers))
        slot.cooldown_until = now + self._tuning.cooldown_tiers[slot.tier - 1]
        slot.state = PoolHealthState.COOLDOWN

    def _fixed_rate_window(
        self, slot: PoolSlot, now: float, retry_after: float | None
    ) -> None:
        value = (
            retry_after
            if retry_after is not None and retry_after >= 0
            else self._tuning.rate_limit_seconds
        )
        cap = self._tuning.rate_limit_max_seconds
        if cap is not None:
            value = min(value, cap)
        slot.rate_limits += 1
        slot.state = PoolHealthState.COOLDOWN
        slot.cooldown_until = now + value

    @staticmethod
    def _clear_benching(slot: PoolSlot) -> None:
        slot.state = PoolHealthState.HEALTHY
        slot.consecutive_failures = 0
        slot.auth_failures = 0
        # The counter doubles as the live escalation index in both historical
        # engines (backoff keyed off it, zeroed on success), so a recovery
        # resets it rather than preserving a lifetime total.
        slot.lockouts = 0
        slot.tier = 0
        slot.cooldown_until = 0.0
        slot.lockout_until = 0.0
