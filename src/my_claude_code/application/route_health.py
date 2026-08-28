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
    FALLBACK_EJECT_AFTER_FAILURES_DEFAULT,
    FALLBACK_EJECT_FAILURE_RATE_DEFAULT,
    FALLBACK_EJECT_MIN_SAMPLES_DEFAULT,
    FALLBACK_EJECT_SECONDS_DEFAULT,
    FALLBACK_EJECT_WINDOW_DEFAULT,
)
from my_claude_code.config.model_refs import parse_provider_type


@dataclass(slots=True)
class _ModelHealth:
    """Per-model ejection state, interpreted differently per mode."""

    # consecutive mode
    consecutive_failures: int = 0
    # rate_based mode: rolling window of recent outcomes (True = success).
    outcomes: deque[bool] = field(default_factory=deque)
    # shared
    ejected_until: float = 0.0


@dataclass(slots=True)
class RouteHealthRegistry:
    """Consecutive-failure or rate-based ejection for provider/model refs."""

    # When False, the registry is a no-op: every failure passes through
    # immediately and the chain advances to the next model. The provider's
    # own Retry-After / rate-limit skip is also bypassed (no throttling at all
    # when off). The per-failure error still gets recorded in the request log
    # so the analytics view can show the real upstream message.
    bench_enabled: bool = True
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
        retry_after_seconds: float | None = None,
    ) -> None:
        # When bench_enabled is False, this is a no-op -- the chain
        # advances to the next model on every failure with no throttling.
        # When True (the default), the bench duration depends on the kind:
        #   5xx / transient / unknown -> 1s (model is probably fine)
        #   rate_limit -> the provider signal (retry_after_seconds) if
        #     present, else eject_seconds
        #   auth / permission / quota -> eject_seconds (the credential is
        #     likely wrong; bench long enough for the user to fix)
        #   sustained rate-based -> eject_seconds (only after the
        #     rate-based signal fires)
        if not self.enabled:
            return
        health = self._models.setdefault(model_ref, _ModelHealth())
        if self.mode == "consecutive":
            self._record_consecutive(health)
        else:
            self._record_rate_based(
                health, model_ref, failure_kind, retry_after_seconds
            )

    def _record_consecutive(self, health: _ModelHealth) -> None:
        health.consecutive_failures += 1
        if health.consecutive_failures < self.eject_after_failures:
            return
        health.ejected_until = self.now() + self.eject_seconds
        ref = self._model_ref_for(health)
        logger.warning(
            "MODEL EJECTED (consecutive): '%s' failed %d times in a row; "
            "skipping it for %gs",
            ref,
            health.consecutive_failures,
            self.eject_seconds,
        )

    def _record_rate_based(
        self,
        health: _ModelHealth,
        model_ref: str,
        failure_kind: str | None = None,
        retry_after_seconds: float | None = None,
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
        # 5xx / transient get a short bench (the model is probably
        # fine). Rate-limit honors the provider signal. Auth and
        # quota use eject_seconds (longer, because the failure is
        # more permanent).
        duration = self.eject_seconds
        if failure_kind == "rate_limit" and retry_after_seconds:
            duration = max(retry_after_seconds, 1.0)
        elif failure_kind in (
            "rate_limit",
            "upstream",
            "timeout",
            "overloaded",
            "unavailable",
        ):
            duration = min(self.eject_seconds, 1.0)  # transient / 5xx-ish
        health.ejected_until = self.now() + duration
        logger.warning(
            "MODEL EJECTED (rate-based): '%s' at %d/%d failures (%.0f%%) in its last %d requests; skipping it for %gs (kind=%s)",
            model_ref,
            failures,
            len(health.outcomes),
            rate * 100,
            self.eject_window,
            duration,
            failure_kind or "unknown",
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
            return False
        return True

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
