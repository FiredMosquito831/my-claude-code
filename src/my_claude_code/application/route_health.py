"""Passive health tracking that keeps a known-bad model out of a chain.

A fallback chain without this pays the primary's full failure cost on *every*
request while it is down -- the timeout, the retries, then the hop. Ejecting a
model that has just failed repeatedly makes that hop free until it has had time
to recover, which is what turns a chain from "eventually correct" into "fast".

This is passive outlier detection, not a circuit breaker: nothing probes the
model, it is simply skipped while benched and tried again once the bench
expires. Providers already bench individual *credentials*; this benches the
provider/model pair a route points at, which is the thing a chain can route
around.

Two modes, selected by ``mode``:

* ``consecutive`` (legacy): a model is benched after ``eject_after_failures``
  consecutive failures for ``eject_seconds``. Preserved for
  ``FALLBACK_BEHAVIOR=legacy``.
* ``rate_based`` (default): a model is benched when at least
  ``eject_failure_rate`` of its last ``eject_window`` requests have failed,
  with at least ``eject_min_samples`` requests observed so the rate is
  meaningful. The bench lasts ``eject_seconds``. One-off blips on an
  otherwise-healthy model never trip it; a genuinely-down model trips fast.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from loguru import logger

from my_claude_code.config.constants import (
    FALLBACK_BEHAVIOR_DEFAULT,
    FALLBACK_BENCH_ENABLED_DEFAULT,
    FALLBACK_EJECT_AFTER_FAILURES_DEFAULT,
    FALLBACK_EJECT_FAILURE_RATE_DEFAULT,
    FALLBACK_EJECT_MIN_SAMPLES_DEFAULT,
    FALLBACK_EJECT_SECONDS_DEFAULT,
    FALLBACK_EJECT_WINDOW_DEFAULT,
)
from my_claude_code.config.model_refs import parse_provider_type
from my_claude_code.core.failures import FailureKind

#: Failure kinds that say something about the *model* rather than about the
#: request that reached it. An allow-list on purpose: the bench removes
#: capacity, so anything not yet classified must default to "does not bench"
#: rather than to "bench it". The deny-list this replaces is what produced the
#: incident -- a ~330k-token prompt that no model could hold 400-ed everywhere,
#: every 400 was counted, and the models with the largest context windows were
#: ejected first because they were tried first.
#:
#: * ``UPSTREAM`` -- the provider returned a 5xx for this model.
#: * ``OVERLOADED`` -- the provider said the model itself has no capacity.
#: * ``AUTHENTICATION`` / ``PERMISSION`` -- 401/403 from the provider. Counted
#:   deliberately: a route pointing at a model this key may not use will keep
#:   failing identically, and parking it is cheaper than re-proving it.
BENCH_COUNTING_KINDS = frozenset(
    {
        FailureKind.UPSTREAM,
        FailureKind.OVERLOADED,
        FailureKind.AUTHENTICATION,
        FailureKind.PERMISSION,
    }
)


def _as_failure_kind(name: str | None) -> FailureKind | None:
    """The canonical kind behind a recorded name, when there is one.

    Callers hand this a ``FailureKind`` value string, an exception class name
    for a failure no provider classified, or nothing at all. Only the first is
    a verdict on a model.
    """
    if not name:
        return None
    try:
        return FailureKind(name)
    except ValueError:
        return None


def failure_counts_toward_bench(kind: FailureKind) -> bool:
    """Whether one failure is evidence against the model, not the request.

    Explicitly *not* counted, and why:

    * ``TIMEOUT`` -- first-token, stall and budget deadlines are all shares of
      one request's clock. A chain that gives its third model four seconds
      benches it for being third.
    * ``RATE_LIMIT`` -- a 429 is about the key and the minute, and the
      provider's own cooldown already steps the chain over it.
    * ``CONTEXT_LENGTH`` / ``INVALID_REQUEST`` -- properties of the request.
      They fail identically on a healthy model and on a dead one.
    * ``UNAVAILABLE`` -- the executor's own no-credential sentinel shares this
      kind with a refused socket, so it cannot be read as a verdict on a model.

    A stream that thinks and never answers is classified ``TIMEOUT`` today
    (``_timeout_failure(reasoning_only=True)``), so it does not bench either.
    Splitting that outcome into its own kind is the prerequisite for counting
    it and is deliberately not done here.
    """
    return kind in BENCH_COUNTING_KINDS


@dataclass(frozen=True, slots=True)
class BenchReason:
    """Why a model is benched right now, in the terms that benched it.

    Carried into the skipped attempt's row so the request detail can say
    "benched: 5 upstream errors in the last 10 attempts (rate_based >= 50%),
    22 s left" instead of one fixed sentence about consecutive failures that
    was wrong in the mode that has been the default since 5.61.0.
    """

    mode: Literal["consecutive", "rate_based"]
    failures: int
    window: int | None
    rate: float | None
    last_kind: str
    last_status: int | None
    remaining_seconds: float
    since: float

    def as_dict(self) -> dict[str, object]:
        """The shape stored under an attempt's ``params.bench``."""
        return {
            "mode": self.mode,
            "failures": self.failures,
            "window": self.window,
            "rate": self.rate,
            "last_kind": self.last_kind,
            "last_status": self.last_status,
            "remaining_seconds": round(self.remaining_seconds, 1),
            "since": round(self.since, 1),
        }

    def sentence(self) -> str:
        """One line for ``error_message`` and for the modal."""
        left = f"{self.remaining_seconds:.0f} s left"
        plural = "" if self.failures == 1 else "s"
        last = self.last_kind
        if self.last_status is not None:
            last = f"{self.last_status} {self.last_kind}"
        if self.mode == "rate_based" and self.window:
            share = "" if self.rate is None else f" (rate_based >= {self.rate:.0%})"
            return (
                f"benched: {self.failures} {self.last_kind} error{plural} in the"
                f" last {self.window} attempts{share}, {left}"
            )
        return (
            f"benched: {self.failures} consecutive failure{plural}"
            f" (last: {last}), {left}"
        )


@dataclass(slots=True)
class _ModelHealth:
    """Per-model ejection state, interpreted differently per mode."""

    # consecutive mode
    consecutive_failures: int = 0
    # rate_based mode: rolling window of recent outcomes (True = success).
    outcomes: deque[bool] = field(default_factory=deque)
    # shared
    ejected_until: float = 0.0
    #: What the failure that benched it was, for ``why()``.
    last_kind: str = "unknown"
    last_status: int | None = None
    #: Monotonic clock when the current bench started.
    benched_at: float = 0.0
    #: Failures counted in the window at the moment it was benched.
    benched_failures: int = 0


@dataclass(slots=True)
class RouteHealthRegistry:
    """Consecutive-failure or rate-based ejection for provider/model refs."""

    # When False, the registry is a no-op: every failure passes through
    # immediately and the chain advances to the next model. The provider's
    # own Retry-After / rate-limit skip is also bypassed (no throttling at all
    # when off). The per-failure error still gets recorded in the request log
    # so the analytics view can show the real upstream message.
    bench_enabled: bool = FALLBACK_BENCH_ENABLED_DEFAULT
    mode: Literal["consecutive", "rate_based"] = FALLBACK_BEHAVIOR_DEFAULT
    # consecutive mode
    eject_after_failures: int = FALLBACK_EJECT_AFTER_FAILURES_DEFAULT
    # rate_based mode
    eject_window: int = FALLBACK_EJECT_WINDOW_DEFAULT
    eject_failure_rate: float = FALLBACK_EJECT_FAILURE_RATE_DEFAULT
    eject_min_samples: int = FALLBACK_EJECT_MIN_SAMPLES_DEFAULT
    # shared
    eject_seconds: float = FALLBACK_EJECT_SECONDS_DEFAULT
    now: Callable[[], float] = time.monotonic
    _models: dict[str, _ModelHealth] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        if not self.bench_enabled:
            return False
        if self.eject_seconds <= 0:
            return False
        if self.mode == "consecutive":
            return self.eject_after_failures > 0
        return self.eject_window > 0

    def record_success(self, model_ref: str) -> None:
        """Record one good answer.

        In ``consecutive`` mode, a success clears the failure streak: a
        model that answered is serving again, whatever it did before.
        In ``rate_based`` mode, a success is appended to the window and
        trimmed: it is one data point in the rolling history, not a
        wipe. A model that is failing 50% of the time but succeeding the
        other 50% has its failures stay in the window until they age out,
        so the eject math reflects the actual recent state rather than
        being reset on every success.
        """
        health = self._models.get(model_ref)
        if health is None:
            return
        if self.mode == "consecutive":
            health.consecutive_failures = 0
            health.ejected_until = 0.0
        else:
            health.outcomes.append(True)
            if len(health.outcomes) > self.eject_window:
                health.outcomes.popleft()

    def record_failure(
        self,
        model_ref: str,
        *,
        failure_kind: str | None = None,
        status_code: int | None = None,
    ) -> None:
        # A no-op unless benching is switched on *and* this failure is one
        # that says something about the model -- see
        # ``failure_counts_toward_bench``. Everything that does count is
        # benched for exactly ``eject_seconds``: the one duration that was
        # never ours to choose, a provider's own Retry-After, belonged to
        # ``rate_limit``, and a 429 no longer benches anything.
        if not self.enabled:
            return
        kind = _as_failure_kind(failure_kind)
        if kind is None or not failure_counts_toward_bench(kind):
            # Not evidence about the model: a deadline, a 429, a prompt no
            # model could hold, or a failure that never reached provider
            # classification at all. Logged rather than silent, because "the
            # bench never fired" and "the bench ignored it" look identical
            # from the request log.
            logger.debug(
                "BENCH IGNORED: '{}' failed with kind={} which does not count towards benching",
                model_ref,
                failure_kind or "unclassified",
            )
            return
        health = self._models.setdefault(model_ref, _ModelHealth())
        health.last_kind = kind.value
        health.last_status = status_code
        if self.mode == "consecutive":
            self._record_consecutive(health)
        else:
            self._record_rate_based(health, model_ref, kind)

    def _record_consecutive(self, health: _ModelHealth) -> None:
        health.consecutive_failures += 1
        if health.consecutive_failures < self.eject_after_failures:
            return
        health.ejected_until = self.now() + self.eject_seconds
        health.benched_at = self.now()
        health.benched_failures = health.consecutive_failures
        ref = self._model_ref_for(health)
        # loguru formats with str.format, not %-interpolation: the %s/%d/%g
        # spellings printed the template literally and the numbers not at all,
        # so the one log line that said which model was benched said nothing.
        logger.warning(
            "MODEL EJECTED (consecutive): '{}' failed {} times in a row; "
            "skipping it for {:g}s",
            ref,
            health.consecutive_failures,
            self.eject_seconds,
        )

    def _record_rate_based(
        self,
        health: _ModelHealth,
        model_ref: str,
        failure_kind: FailureKind,
    ) -> None:
        health.outcomes.append(False)
        if len(health.outcomes) > self.eject_window:
            health.outcomes.popleft()
        if len(health.outcomes) < self.eject_min_samples:
            return
        failures = sum(1 for ok in health.outcomes if not ok)
        rate = failures / len(health.outcomes)
        if rate < self.eject_failure_rate:
            return
        # Benched for exactly what the operator configured. The number here
        # that was not -- a `min(eject_seconds, 1.0)` clamp for timeout/5xx --
        # silently overrode FALLBACK_EJECT_SECONDS with one second for the
        # most common failure classes, so a model that had just failed half
        # of its last ten requests was back in the rotation before the next
        # request arrived.
        duration = self.eject_seconds
        health.ejected_until = self.now() + duration
        health.benched_at = self.now()
        health.benched_failures = failures
        logger.warning(
            "MODEL EJECTED (rate-based): '{}' at {}/{} failures ({:.0f}%) in its"
            " last {} requests; skipping it for {:g}s (kind={})",
            model_ref,
            failures,
            len(health.outcomes),
            rate * 100,
            self.eject_window,
            duration,
            failure_kind.value,
        )

    def _model_ref_for(self, health: _ModelHealth) -> str:
        for model_ref, candidate in self._models.items():
            if candidate is health:
                return model_ref
        return "?"

    def is_ejected(self, model_ref: str) -> bool:
        health = self._models.get(model_ref)
        if health is None or health.ejected_until <= 0.0:
            return False
        if self.now() >= health.ejected_until:
            health.ejected_until = 0.0
            health.consecutive_failures = 0
            health.outcomes.clear()
            health.benched_at = 0.0
            health.benched_failures = 0
            return False
        return True

    def why(self, model_ref: str) -> BenchReason | None:
        """What benched this model, in the mode that benched it.

        ``None`` when the model is not benched -- including when the bench has
        just expired, because :meth:`is_ejected` is what clears it and this
        asks the same question first.
        """
        if not self.is_ejected(model_ref):
            return None
        health = self._models[model_ref]
        now = self.now()
        rate_based = self.mode == "rate_based"
        return BenchReason(
            mode=self.mode,
            failures=health.benched_failures,
            window=len(health.outcomes) if rate_based else None,
            rate=self.eject_failure_rate if rate_based else None,
            last_kind=health.last_kind,
            last_status=health.last_status,
            remaining_seconds=max(0.0, health.ejected_until - now),
            since=max(0.0, now - health.benched_at),
        )

    def usable_indexes(
        self,
        model_refs: tuple[str, ...],
        provider_lookup: Callable[[str], float | None] | None = None,
    ) -> tuple[int, ...]:
        """Indexes worth attempting, in order, given what is currently benched.

        Ejecting *every* candidate would turn a degraded route into a dead one,
        so when nothing survives the filter the chain is returned untouched and
        the request takes its chances. Skipping a bad model is an optimisation;
        refusing to try anything is an outage.

        In ``rate_based`` mode, models whose provider is in an active rate-limit
        cooldown (reported by ``provider_lookup``) are also skipped, so the chain
        steps over a provider that is only going to sleep inside its own limiter
        instead of paying the wait.
        """
        if not self.enabled:
            return tuple(range(len(model_refs)))
        usable = []
        for index, model_ref in enumerate(model_refs):
            if self.is_ejected(model_ref):
                continue
            if provider_lookup is not None and self.mode == "rate_based":
                provider_id = parse_provider_type(model_ref)
                try:
                    cooldown = provider_lookup(provider_id)
                except Exception:
                    cooldown = None
                # Guard the comparison: a provider's throttle_remaining() may
                # legally return None (unknown) or a non-numeric sentinel; treat
                # anything that isn't a real number as "unknown" rather than
                # raising a TypeError that aborts the whole chain.
                if (
                    cooldown is not None
                    and isinstance(cooldown, (int, float))
                    and cooldown > 0
                ):
                    continue
            usable.append(index)
        if usable:
            return tuple(usable)
        logger.warning(
            "MODEL EJECTION BYPASSED: every model on this route is benched; trying the chain in order anyway"
        )
        return tuple(range(len(model_refs)))
