"""Passive ejection of models that have just failed repeatedly."""

from typing import Literal

import pytest

from my_claude_code.application.route_health import (
    BENCH_COUNTING_KINDS,
    RouteHealthRegistry,
    failure_counts_toward_bench,
)
from my_claude_code.core.failures import FailureKind

#: What each kind means for the bench, asserted one member at a time. A new
#: FailureKind that nobody classified fails
#: ``test_every_failure_kind_is_classified`` with "classify X", which is the
#: point: the bench removes capacity, so a kind must be opted in deliberately
#: rather than inherited by whatever the default branch happens to be.
MODEL_SHAPED = FailureKind.UPSTREAM.value

KIND_BENCHES = {
    FailureKind.UPSTREAM: True,
    FailureKind.OVERLOADED: True,
    FailureKind.AUTHENTICATION: True,
    FailureKind.PERMISSION: True,
    FailureKind.TIMEOUT: False,
    FailureKind.RATE_LIMIT: False,
    FailureKind.CONTEXT_LENGTH: False,
    FailureKind.INVALID_REQUEST: False,
    FailureKind.UNAVAILABLE: False,
}


def _registry(
    clock: list[float],
    *,
    bench_enabled: bool = True,
    mode: Literal["consecutive", "rate_based"] = "consecutive",
    eject_after_failures: int = 3,
    eject_seconds: float = 30.0,
    eject_window: int = 10,
    eject_failure_rate: float = 0.5,
    eject_min_samples: int = 8,
) -> RouteHealthRegistry:
    return RouteHealthRegistry(
        bench_enabled=bench_enabled,
        mode=mode,
        eject_after_failures=eject_after_failures,
        eject_seconds=eject_seconds,
        eject_window=eject_window,
        eject_failure_rate=eject_failure_rate,
        eject_min_samples=eject_min_samples,
        now=lambda: clock[0],
    )


def test_a_model_is_ejected_only_after_the_configured_streak() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=3, eject_seconds=30.0)

    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    assert registry.is_ejected("a/one")


def test_one_success_clears_the_streak() -> None:
    """A model that answered is serving, whatever it did before."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)

    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_success("a/one")
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)

    assert not registry.is_ejected("a/one")


def test_ejection_expires_and_does_not_immediately_recur() -> None:
    """The streak resets with the bench, so recovery is not one failure from re-ejection."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    assert registry.is_ejected("a/one")

    clock[0] = 31.0
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    assert not registry.is_ejected("a/one")


def test_usable_indexes_skips_an_ejected_model() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)

    assert registry.usable_indexes(("a/one", "b/two", "c/three")) == (1, 2)


def test_a_fully_ejected_chain_is_returned_intact() -> None:
    """Skipping a bad model is an optimisation; refusing to try anything is an outage."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_failure("b/two", failure_kind=MODEL_SHAPED)

    assert registry.usable_indexes(("a/one", "b/two")) == (0, 1)


def test_ejection_can_be_switched_off() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=0, eject_seconds=30.0)
    for _ in range(10):
        registry.record_failure("a/one", failure_kind=MODEL_SHAPED)

    assert not registry.is_ejected("a/one")
    assert registry.usable_indexes(("a/one", "b/two")) == (0, 1)


def test_failures_are_tracked_per_model_not_per_provider() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_failure("a/two", failure_kind=MODEL_SHAPED)

    assert registry.usable_indexes(("a/one", "a/two")) == (0, 1)


# --------------------------------------------------------------- rate-based --


def test_rate_based_does_not_trip_below_min_samples() -> None:
    """One failure on a low-traffic model never benches it. The 8-sample floor
    exists precisely to absorb a single blip without ejecting a working model.
    """
    clock = [0.0]
    registry = _registry(clock, mode="rate_based", eject_min_samples=8)

    registry.record_failure("rare/model", failure_kind=MODEL_SHAPED)
    assert not registry.is_ejected("rare/model")


def test_rate_based_trips_when_failure_rate_crosses_threshold() -> None:
    """Eight consecutive failures (8/8 = 100% >= 50%): bench."""
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    for _ in range(7):
        registry.record_failure("sick/model", failure_kind=MODEL_SHAPED)
    # 7 samples < min_samples=8, so still unbenched.
    assert not registry.is_ejected("sick/model")

    registry.record_failure("sick/model", failure_kind=MODEL_SHAPED)  # 8/8 = 100%
    assert registry.is_ejected("sick/model")


def test_rate_based_does_not_trip_under_threshold() -> None:
    """A 40% failure rate over the full window stays unbenched."""
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    # Build a 4f/6s window. Successes are appended (not a wipe), so the
    # window reflects the last 10 outcomes exactly.
    for _ in range(4):
        registry.record_failure("blip/model", failure_kind=MODEL_SHAPED)
    for _ in range(6):
        registry.record_success("blip/model")
    # 4 failures / 10 samples = 40% < 50% threshold: stays unbenched.
    assert not registry.is_ejected("blip/model")


def test_rate_based_provider_lookup_skips_throttled_models() -> None:
    """In rate_based mode, a model whose provider is currently rate-limited is
    skipped at plan-build time, so the chain doesn't pay the wait.
    """
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    def lookup(provider_id: str) -> float | None:
        return 7.0 if provider_id == "rate_limited" else 0.0

    usable = registry.usable_indexes(
        ("rate_limited/model", "healthy/model"),
        provider_lookup=lookup,
    )
    assert usable == (1,)


def test_consecutive_mode_is_preserved() -> None:
    """The legacy consecutive-count behavior still works when mode=consecutive,
    so a server pinned to FALLBACK_BEHAVIOR=legacy is unchanged.
    """
    clock = [0.0]
    registry = _registry(
        clock, mode="consecutive", eject_after_failures=3, eject_seconds=30.0
    )

    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    assert registry.is_ejected("a/one")


def test_a_counted_bench_lasts_the_configured_eject_window() -> None:
    """The 1s clamp silently overrode FALLBACK_EJECT_SECONDS.

    Timeout/5xx/overloaded/unavailable -- between them almost every real
    ejection -- were benched for ``min(eject_seconds, 1.0)``, so a model that
    had just failed half of its last ten requests was back in the rotation
    before the next request arrived and the setting the operator configured
    did nothing. Asserted here on ``upstream``, which is what still counts.
    """
    clock = [0.0]
    registry = _registry(clock, mode="rate_based", eject_seconds=30.0)

    for _ in range(8):
        registry.record_failure("sick/model", failure_kind="upstream")

    assert registry.is_ejected("sick/model")
    clock[0] = 29.0
    assert registry.is_ejected("sick/model"), "the bench ended a second in"
    clock[0] = 31.0
    assert not registry.is_ejected("sick/model")


# ----------------------------------------------------------- what counts --


def test_every_failure_kind_is_classified() -> None:
    """A new kind must be opted in or out on purpose, never by default."""
    missing = sorted(kind.value for kind in FailureKind if kind not in KIND_BENCHES)
    assert not missing, f"classify {', '.join(missing)} in KIND_BENCHES"


@pytest.mark.parametrize("kind", sorted(FailureKind, key=lambda k: k.value))
def test_only_model_shaped_failures_count_toward_the_bench(kind: FailureKind) -> None:
    assert failure_counts_toward_bench(kind) is KIND_BENCHES[kind]
    assert (kind in BENCH_COUNTING_KINDS) is KIND_BENCHES[kind]


@pytest.mark.parametrize(
    "kind",
    [
        FailureKind.TIMEOUT,
        FailureKind.RATE_LIMIT,
        FailureKind.CONTEXT_LENGTH,
        FailureKind.INVALID_REQUEST,
        FailureKind.UNAVAILABLE,
    ],
)
def test_a_request_shaped_failure_never_benches_a_model(kind: FailureKind) -> None:
    """A whole window of them leaves the model in the chain."""
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    for _ in range(20):
        registry.record_failure("wrongly/blamed", failure_kind=kind.value)

    assert not registry.is_ejected("wrongly/blamed")
    assert registry.usable_indexes(("wrongly/blamed", "b/two")) == (0, 1)


def test_an_unclassified_exception_never_benches_a_model() -> None:
    """A raw httpx/RuntimeError that never reached provider classification."""
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    for _ in range(20):
        registry.record_failure("raw/failure", failure_kind="ReadTimeout")
    for _ in range(20):
        registry.record_failure("no/kind")

    assert not registry.is_ejected("raw/failure")
    assert not registry.is_ejected("no/kind")


def test_the_incident_replayed_leaves_every_capable_model_in_the_chain() -> None:
    """req_dfc8ac9da90d458fa5dc396b8eed0b1d, 2026-08-31 01:01Z.

    A ~330k-token prompt that no model on the route could hold 400-ed on every
    one of them, and NIM answered 429. Under the old counting that is eight
    request-shaped failures per model -- enough to bench all four at the
    operator's own window/rate/min-samples -- and the ninth request was served
    by the one model still standing, which answered 400 context_length. The
    replay asserts the ninth request still sees the whole chain.
    """
    clock = [0.0]
    registry = _registry(
        clock,
        mode="rate_based",
        eject_window=10,
        eject_failure_rate=0.5,
        eject_min_samples=8,
        eject_seconds=30.0,
    )
    chain = (
        "moonshot/kimi-k3",
        "zai/glm-5.3-flash",
        "minimax/minimax-m3-free",
        "deepseek/deepseek-v4-flash",
    )
    incident = ["context_length"] * 6 + ["rate_limit", "timeout"]
    for model_ref in chain:
        for kind in incident:
            registry.record_failure(model_ref, failure_kind=kind)

    assert [registry.is_ejected(ref) for ref in chain] == [False] * 4
    assert registry.usable_indexes(chain) == (0, 1, 2, 3)


# ------------------------------------------------------------------- why --


def test_why_reports_the_rate_based_evidence() -> None:
    clock = [0.0]
    registry = _registry(clock, mode="rate_based", eject_seconds=30.0)
    for _ in range(8):
        registry.record_failure("sick/model", failure_kind="upstream", status_code=502)

    clock[0] = 8.0
    reason = registry.why("sick/model")
    assert reason is not None
    assert reason.mode == "rate_based"
    assert reason.failures == 8
    assert reason.window == 8
    assert reason.rate == 0.5
    assert reason.last_kind == "upstream"
    assert reason.last_status == 502
    assert reason.remaining_seconds == 22.0
    assert reason.since == 8.0
    assert reason.sentence() == (
        "benched: 8 upstream errors in the last 8 attempts"
        " (rate_based >= 50%), 22 s left"
    )
    assert reason.as_dict()["remaining_seconds"] == 22.0


def test_why_reports_the_consecutive_evidence() -> None:
    clock = [0.0]
    registry = _registry(clock, mode="consecutive", eject_after_failures=3)
    for _ in range(3):
        registry.record_failure("sick/model", failure_kind="upstream", status_code=502)

    clock[0] = 18.0
    reason = registry.why("sick/model")
    assert reason is not None
    assert reason.sentence() == (
        "benched: 3 consecutive failures (last: 502 upstream), 12 s left"
    )


def test_why_is_none_for_a_model_that_is_not_benched() -> None:
    clock = [0.0]
    registry = _registry(clock, mode="rate_based", eject_seconds=30.0)
    assert registry.why("never/failed") is None

    for _ in range(8):
        registry.record_failure("sick/model", failure_kind="upstream")
    clock[0] = 31.0
    assert registry.why("sick/model") is None


def test_benching_off_is_the_shipped_default() -> None:
    """The chain tries every model every time unless the operator says otherwise."""
    registry = RouteHealthRegistry()
    assert registry.bench_enabled is False
    assert registry.enabled is False

    for _ in range(20):
        registry.record_failure("sick/model", failure_kind="upstream")

    assert not registry.is_ejected("sick/model")
    assert registry.why("sick/model") is None
    assert registry.usable_indexes(("sick/model", "b/two")) == (0, 1)


# ------------------------------------------------------------------- pause --
# Pausing is the operator's own decision rather than this registry's guess, so
# it is honoured on two counts the bench is not: it applies with benching
# switched off, and it is never restored by the all-benched bypass.


def test_a_paused_model_is_dropped_from_the_order() -> None:
    registry = _registry([0.0])

    assert registry.usable_indexes(
        ("a/one", "b/two", "c/three"), paused=frozenset({"b/two"})
    ) == (0, 2)


def test_pause_applies_even_with_benching_switched_off() -> None:
    """The master switch governs automatic ejection, not a hand-flipped switch."""
    registry = _registry([0.0], bench_enabled=False)

    assert registry.usable_indexes(("a/one", "b/two"), paused=frozenset({"a/one"})) == (
        1,
    )


def test_pause_does_not_trigger_the_ejection_bypass() -> None:
    """The bypass restores what the bench removed, never what the user paused."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)
    registry.record_failure("b/two", failure_kind=MODEL_SHAPED)

    # Every model benched, one of them also paused: the bypass hands back the
    # chain, minus the paused entry.
    assert registry.usable_indexes(("a/one", "b/two"), paused=frozenset({"a/one"})) == (
        1,
    )


def test_a_route_with_every_model_paused_yields_nothing_to_try() -> None:
    """An all-paused route is an error, not a route to bypass into."""
    registry = _registry([0.0])

    assert (
        registry.usable_indexes(
            ("a/one", "b/two"), paused=frozenset({"a/one", "b/two"})
        )
        == ()
    )


def test_pause_changes_nothing_when_nothing_is_paused() -> None:
    """The regression floor: the default argument is the old behaviour."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one", failure_kind=MODEL_SHAPED)

    assert registry.usable_indexes(("a/one", "b/two", "c/three")) == (1, 2)
    assert registry.usable_indexes(
        ("a/one", "b/two", "c/three"), paused=frozenset()
    ) == (1, 2)
