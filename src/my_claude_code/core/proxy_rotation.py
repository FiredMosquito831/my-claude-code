"""Health machinery for a provider's proxy chain.

The credential engine in :mod:`my_claude_code.core.credential_rotation` is
instantiated here a second time, with its own tuning, for a second resource:
the *connection*. That file's own docstring already makes the argument --
``UNAVAILABLE`` rotates a credential because "another key means another
connection" -- and a proxy chain is that sentence with the connection named.

``core/credential_rotation.py`` is imported and never edited. Its two presets
live beside each other in that file and a third would have matched its
convention, but the release that added this feature had to be able to say that
the credential engine's diff was empty, and a one-constant change there would
have cost a re-justification of every invariant test in the pool. The engine is
the shared thing; the tuning is not.

Two benches, deliberately mirroring the credential engine's own split between a
whole-key lockout and a (key, model) bench:

**Reachability** -- the proxy itself failed. A refused CONNECT, a connect
timeout, a ``407`` from the proxy, an ``httpx.ProxyError``. Escalating
:data:`PROXY_REACHABILITY_TIERS` (60s, 5m, 1h), and scoped to the **endpoint**
across every provider in the process, because a dead proxy is dead for
everybody. That is what :data:`PROXY_REACHABILITY` is: one table, not one per
pool.

**Trigger** -- the *upstream* answered with a failure class the operator armed.
The provider's own published ``Retry-After`` if there was one, else
:data:`PROXY_COOLDOWN_SECONDS_DEFAULT`, held in the engine's own
``model_benches`` map under a composite scope key, and therefore scoped exactly
as the operator asked: per address and provider under the default ``provider``
scope, per address, provider and credential under ``credential``.
``model_bench_escalation`` is ``0`` -- never escalate -- because two credentials
exhausting the same address is not evidence the address is broken.

Beside the two benches sits a third table that is not a bench at all.
:data:`PROXY_INTERCEPTION` holds the addresses the checker found terminating
TLS: a tunnel that presents a certificate this machine's trust store rejects is
reading the plaintext, and there is no tier after which that becomes acceptable.
Those addresses are refused rather than timed out -- held out of every chain in
the process until a later check says the destination's certificate verifies
again.

:data:`PROXY_HEALTH` is the read side of both: the page draws live per-entry
health out of it rather than reaching into the running provider tree, which
means an admin request never has to find a pool and a pool never has to be
findable.
"""

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from my_claude_code.core.credential_rotation import RotationTuning
from my_claude_code.core.failures import FailureKind

#: How long an address is benched for a triggering failure when the provider
#: published no wait of its own. Five minutes: long enough that a per-address
#: allowance has a chance of rolling over, short enough that a chain of two
#: recovers inside one working session.
PROXY_COOLDOWN_SECONDS_DEFAULT = 300.0

#: Cap on a published ``Retry-After`` for an address. An hour, not a day: the
#: credential pool's day-long cap exists because a *key* the host refused until
#: midnight really is refused until midnight, while an address is one of
#: several and the cheap move is to try it again.
PROXY_COOLDOWN_MAX_SECONDS = 3600.0

#: The reachability ladder: 60s, then 5m, then 1h, clamped at the last entry.
#: A free address that has just started refusing is usually back inside a
#: minute; one that has refused three times running is not coming back today.
PROXY_REACHABILITY_TIERS: tuple[float, ...] = (60.0, 300.0, 3600.0)

#: The proxy pool's tuning. Every field that differs from the engine's defaults
#: is named here and nowhere else.
PROXY_TUNING = RotationTuning(
    rate_limit_mode="fixed",
    rate_limit_seconds=PROXY_COOLDOWN_SECONDS_DEFAULT,
    rate_limit_max_seconds=PROXY_COOLDOWN_MAX_SECONDS,
    lockout_tiers=PROXY_REACHABILITY_TIERS,
    # 0 = never escalate. Two credentials exhausting one address says the
    # address is metered, which is the thing the operator configured, not that
    # the address is broken.
    model_bench_escalation=0,
    # A chain whose policy is ``single`` and whose first entry is benched has
    # nothing left to offer, and must say so rather than dispatching into a
    # bench. The credential pool answers the opposite way because a single-key
    # provider has to keep serving its one key; a one-entry chain never
    # reaches this engine at all.
    single_ignores_blocklist=False,
    single_key_forces_slot_zero=False,
)

#: The two failure classes a chain may never move on, enforced here as well as
#: at the API and in the store. Three places for one rule, which is one more
#: than the API and the store between them, and deliberate: this is the only
#: one on the request path. Rotating on a 401/403 burns the entire chain inside
#: a single request *and* earns a bench on every address it touched, and a new
#: address fixes neither -- the credential is the problem. A hand-edited store
#: or a caller past the route cannot arm it, because the thing that would act
#: on it will not.
PROXY_REFUSED_TRIGGER_KINDS: frozenset[str] = frozenset(
    {FailureKind.AUTHENTICATION.value, FailureKind.PERMISSION.value}
)

#: Hard bound on both ledgers below. They are process-lifetime tables keyed by
#: strings an operator supplies, so they are capped and pruned oldest-first
#: rather than trusted to stay small.
MAX_TRACKED_ENDPOINTS = 512


@dataclass(slots=True)
class ReachabilityRecord:
    """What is known about one address's willingness to carry traffic."""

    #: Consecutive reachability failures; the index into the ladder.
    failures: int = 0
    #: Deadline on the ledger's clock. ``0.0`` means selectable.
    until: float = 0.0
    reason: str = ""
    last_seen_at: float = 0.0


class ReachabilityLedger:
    """Process-wide bench for addresses that would not carry a request.

    Global on purpose, and it is the one part of this feature that is not
    scoped per provider: an address that refuses a CONNECT refuses it for every
    provider that would have used it, and discovering that once per provider is
    three connect timeouts instead of one.

    **What a tier expiring means changed in 7.19.0.** It used to mean the
    address was selectable again, so a dead free proxy was re-tried on a live
    request every minute for as long as it stayed in the chain, and every one
    of those re-tries cost the operator a connect timeout inside a real
    request. It now means the address is *due for a re-probe*: a failed address
    is unhealthy until a check **passes**, and the only things that clear it
    are :meth:`note_success` -- which the checker calls on a pass and the
    request path calls when an address really did carry a request -- and
    :meth:`clear`. :meth:`remaining` still answers "how long until the next
    re-check", which is what the page shows and what the re-prober schedules
    on; :meth:`is_unhealthy` is the question selection asks.
    """

    def __init__(
        self,
        tiers: tuple[float, ...] = PROXY_REACHABILITY_TIERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tiers = tiers or PROXY_REACHABILITY_TIERS
        self._clock = clock
        self._records: dict[str, ReachabilityRecord] = {}
        self._listener: Callable[[str], None] | None = None

    def set_listener(self, listener: Callable[[str], None] | None) -> None:
        """Register one callback told which address's record just moved.

        The ledger lives in ``core`` and durability lives above it, so the
        ledger does not know what a store is: it names the address that
        changed and the layer that owns the file decides what to do about it.
        Exactly one listener, replaced rather than appended, so a test that
        installs one cannot leave a second behind.
        """

        self._listener = listener

    def _announce(self, endpoint: str) -> None:
        listener = self._listener
        if listener is None:
            return
        # A durability writer must never be able to fail a request. The ledger
        # is the request path's; the file is somebody else's problem.
        with contextlib.suppress(Exception):
            listener(endpoint)

    def is_unhealthy(self, endpoint: str) -> bool:
        """Whether this address may not be selected for a live request.

        True from the first failure until something *passes*. The deadline is
        not part of this answer, which is the whole of the 7.19.0 change.
        """

        record = self._records.get(endpoint)
        return record is not None and record.failures > 0

    def due_for_reprobe(self, endpoint: str) -> bool:
        """Whether this address has failed and its re-check window has passed."""

        record = self._records.get(endpoint)
        return (
            record is not None and record.failures > 0 and record.until <= self._clock()
        )

    def due_endpoints(self) -> tuple[str, ...]:
        """Every address a re-probe is owed, oldest deadline first."""

        now = self._clock()
        due = [
            (record.until, endpoint)
            for endpoint, record in self._records.items()
            if record.failures > 0 and record.until <= now
        ]
        return tuple(endpoint for _, endpoint in sorted(due))

    def failures(self, endpoint: str) -> int:
        record = self._records.get(endpoint)
        return 0 if record is None else record.failures

    def restore(
        self, endpoint: str, failures: int, remaining: float, reason: str = ""
    ) -> None:
        """Re-arm one address's bench from a durable record.

        Used once at startup. ``remaining`` is seconds still to wait, already
        worked out against wall-clock time by the caller that read the file,
        because this ledger's own clock is monotonic and means nothing across a
        restart. A non-positive ``remaining`` re-arms the address as unhealthy
        and immediately due, which is the honest reading of a bench that
        expired while the process was down: it has not passed a check.
        """

        if not endpoint or failures <= 0:
            return
        now = self._clock()
        record = self._records.get(endpoint)
        if record is None:
            record = ReachabilityRecord()
            self._records[endpoint] = record
            self._prune(now)
        record.failures = failures
        record.reason = reason
        record.last_seen_at = now
        record.until = now + max(0.0, remaining)

    def note_failure(self, endpoint: str, reason: str = "") -> float:
        """Bench ``endpoint`` one tier deeper; return the seconds it now waits."""

        if not endpoint:
            return 0.0
        now = self._clock()
        record = self._records.get(endpoint)
        if record is None:
            record = ReachabilityRecord()
            self._records[endpoint] = record
            self._prune(now)
        record.failures += 1
        record.reason = reason
        record.last_seen_at = now
        window = self._tiers[min(record.failures, len(self._tiers)) - 1]
        record.until = now + window
        self._announce(endpoint)
        return window

    def note_success(self, endpoint: str) -> None:
        """Forget an address's bench: something about it just passed.

        The **only** way back into rotation. Called by the checker on a pass
        and by the request path when an address really carried a request.
        """

        record = self._records.get(endpoint)
        if record is None:
            return
        changed = record.failures > 0
        record.failures = 0
        record.until = 0.0
        record.reason = ""
        record.last_seen_at = self._clock()
        if changed:
            self._announce(endpoint)

    def remaining(self, endpoint: str) -> float:
        """Seconds before ``endpoint`` may be tried again; 0 while it may."""

        record = self._records.get(endpoint)
        if record is None:
            return 0.0
        return max(0.0, record.until - self._clock())

    def reason(self, endpoint: str) -> str:
        """Why this address is out, for as long as it is out.

        Tied to ``failures`` rather than to the deadline since 7.19.0: an
        address whose window has expired is still out, and a row that stopped
        saying why the moment the countdown hit zero was the most confusing
        part of the old reading.
        """

        record = self._records.get(endpoint)
        return "" if record is None or record.failures <= 0 else record.reason

    def state(self, endpoint: str) -> tuple[int, float, str]:
        """``(failures, seconds still to wait, reason)`` for the writer."""

        record = self._records.get(endpoint)
        if record is None or record.failures <= 0:
            return (0, 0.0, "")
        return (record.failures, self.remaining(endpoint), record.reason)

    def clear(self) -> None:
        """Forget everything. Tests, and the admin reset.

        The listener is deliberately kept: it belongs to whoever wired the
        process up, and a test that resets the measurements is not asking for
        durability to be unwired.
        """

        self._records.clear()

    def _prune(self, now: float) -> None:
        overflow = len(self._records) - MAX_TRACKED_ENDPOINTS
        if overflow <= 0:
            return
        for endpoint, _ in sorted(
            self._records.items(), key=lambda item: item[1].last_seen_at
        )[:overflow]:
            del self._records[endpoint]


#: The one reachability table this process has.
PROXY_REACHABILITY = ReachabilityLedger()


class InterceptionLedger:
    """Addresses the checker found terminating TLS, and therefore refused.

    A different thing from a bench, which is why it is a different table. A
    benched address is one that failed and will be tried again when its tier
    expires; a refused one is an address whose tunnel presented a certificate
    this machine's trust store rejects, which means something between here and
    the provider is reading the plaintext. There is no tier after which that
    becomes acceptable. It is held out of every chain in the process until a
    later check says the certificate verifies again -- which is the only way
    out, and it is a measurement rather than a timer.

    Keyed by the same ``host:port`` label the other two tables use, so one
    address refused while checking one provider is refused for all of them: a
    machine that reads one tunnel reads them all.
    """

    def __init__(self) -> None:
        self._refused: dict[str, str] = {}

    def mark(self, endpoint: str, detail: str = "") -> None:
        if endpoint:
            self._refused[endpoint] = detail

    def clear_endpoint(self, endpoint: str) -> None:
        """A later check verified this address's tunnel; take the refusal off."""

        self._refused.pop(endpoint, None)

    def is_refused(self, endpoint: str) -> bool:
        return bool(endpoint) and endpoint in self._refused

    def detail(self, endpoint: str) -> str:
        return self._refused.get(endpoint, "")

    def labels(self) -> frozenset[str]:
        return frozenset(self._refused)

    def clear(self) -> None:
        self._refused.clear()


#: The one interception table this process has.
PROXY_INTERCEPTION = InterceptionLedger()


@dataclass(slots=True)
class ProxyHealthRecord:
    """One address's record for one provider, as the page reads it."""

    requests: int = 0
    successes: int = 0
    failures: int = 0
    #: Deadline of the *trigger* bench, on the ledger's clock. The reachability
    #: bench is global and read from :data:`PROXY_REACHABILITY` instead, so a
    #: card can say which of the two is holding an entry out.
    benched_until: float = 0.0
    last_error: str | None = None
    last_used_at: float = 0.0
    paused: bool = False


class ProxyHealthLedger:
    """What every proxy pool in this process has actually measured.

    Written by the pools, read by ``/admin/api/proxy-chains``. A registry
    rather than a walk of the live provider tree: the page asks one question
    about one address, the answer outlives the generation replace that a chain
    edit performs, and nothing on the request path has to be reachable from an
    admin request.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._records: dict[tuple[str, str], ProxyHealthRecord] = {}

    def record(self, provider_id: str, endpoint: str) -> ProxyHealthRecord:
        key = (provider_id, endpoint)
        record = self._records.get(key)
        if record is None:
            record = ProxyHealthRecord()
            self._records[key] = record
            self._prune()
        return record

    def note_acquired(self, provider_id: str, endpoint: str) -> None:
        record = self.record(provider_id, endpoint)
        record.requests += 1
        record.last_used_at = self._clock()

    def note_success(self, provider_id: str, endpoint: str) -> None:
        record = self.record(provider_id, endpoint)
        record.successes += 1
        record.benched_until = 0.0
        record.last_error = None

    def note_failure(
        self,
        provider_id: str,
        endpoint: str,
        *,
        benched_for: float = 0.0,
        reason: str | None = None,
    ) -> None:
        record = self.record(provider_id, endpoint)
        record.failures += 1
        record.last_error = reason
        if benched_for > 0:
            record.benched_until = self._clock() + benched_for

    def snapshot(self, provider_id: str, endpoint: str) -> dict[str, object]:
        """One entry's live health, in the shape the card renders.

        ``state`` is the single word the row shows. ``checked`` is False only
        when nothing has ever gone through this address for this provider,
        which is the honest reading of a store the checker has not run against
        -- the page says "not checked yet" and means it.
        """

        record = self._records.get((provider_id, endpoint))
        # Two different numbers now. ``unhealthy`` is whether the address may be
        # selected at all -- true from the first failure until a check passes --
        # and ``unreachable`` is only how long until the next re-probe is owed,
        # which is what the row counts down.
        unhealthy = bool(endpoint) and PROXY_REACHABILITY.is_unhealthy(endpoint)
        unreachable = PROXY_REACHABILITY.remaining(endpoint) if endpoint else 0.0
        due = bool(endpoint) and PROXY_REACHABILITY.due_for_reprobe(endpoint)
        benched = (
            0.0 if record is None else max(0.0, record.benched_until - self._clock())
        )
        if PROXY_INTERCEPTION.is_refused(endpoint):
            # Ahead of every other state on purpose. An intercepted address may
            # also be benched, unchecked or perfectly fast, and none of that is
            # the thing the operator needs to read on its row.
            state = "intercepted"
        elif unhealthy:
            state = "unreachable"
        elif benched > 0:
            state = "cooldown"
        elif record is None or not record.requests:
            state = "unknown"
        elif record.successes:
            state = "healthy"
        else:
            state = "failing"
        return {
            "state": state,
            "checked": record is not None and bool(record.requests),
            "requests": 0 if record is None else record.requests,
            "successes": 0 if record is None else record.successes,
            "failures": 0 if record is None else record.failures,
            "cooldown_remaining": round(max(unreachable, benched), 1),
            # True when the address has failed and its window has run out: it
            # is waiting on a check, not on a clock. The row says so rather
            # than showing "0s" and implying it is about to come back by
            # itself.
            "due_for_recheck": due,
            "refused": PROXY_INTERCEPTION.is_refused(endpoint),
            "reason": (
                PROXY_INTERCEPTION.detail(endpoint)
                or "This address breaks certificate validation."
                if PROXY_INTERCEPTION.is_refused(endpoint)
                else PROXY_REACHABILITY.reason(endpoint)
                if unhealthy
                else (None if record is None else record.last_error)
            ),
        }

    def clear(self) -> None:
        self._records.clear()

    def _prune(self) -> None:
        overflow = len(self._records) - MAX_TRACKED_ENDPOINTS
        if overflow <= 0:
            return
        for key, _ in sorted(
            self._records.items(), key=lambda item: item[1].last_used_at
        )[:overflow]:
            del self._records[key]


#: The one health table this process has.
PROXY_HEALTH = ProxyHealthLedger()


def reset_proxy_health() -> None:
    """Forget every measurement. Used by tests and by a chain edit's republish."""

    PROXY_REACHABILITY.clear()
    PROXY_HEALTH.clear()
    PROXY_INTERCEPTION.clear()


__all__ = [
    "MAX_TRACKED_ENDPOINTS",
    "PROXY_COOLDOWN_MAX_SECONDS",
    "PROXY_COOLDOWN_SECONDS_DEFAULT",
    "PROXY_HEALTH",
    "PROXY_INTERCEPTION",
    "PROXY_REACHABILITY",
    "PROXY_REACHABILITY_TIERS",
    "PROXY_REFUSED_TRIGGER_KINDS",
    "PROXY_TUNING",
    "InterceptionLedger",
    "ProxyHealthLedger",
    "ProxyHealthRecord",
    "ReachabilityLedger",
    "ReachabilityRecord",
    "reset_proxy_health",
]
