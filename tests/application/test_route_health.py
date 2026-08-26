"""Passive ejection of models that have just failed repeatedly."""

from typing import Literal

from my_claude_code.application.route_health import RouteHealthRegistry


def _registry(
    clock: list[float],
    *,
    mode: Literal["consecutive", "rate_based"] = "consecutive",
    eject_after_failures: int = 3,
    eject_seconds: float = 30.0,
    eject_window: int = 10,
    eject_failure_rate: float = 0.5,
    eject_min_samples: int = 8,
) -> RouteHealthRegistry:
    return RouteHealthRegistry(
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

    registry.record_failure("a/one")
    registry.record_failure("a/one")
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one")
    assert registry.is_ejected("a/one")


def test_one_success_clears_the_streak() -> None:
    """A model that answered is serving, whatever it did before."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)

    registry.record_failure("a/one")
    registry.record_success("a/one")
    registry.record_failure("a/one")

    assert not registry.is_ejected("a/one")


def test_ejection_expires_and_does_not_immediately_recur() -> None:
    """The streak resets with the bench, so recovery is not one failure from re-ejection."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)
    registry.record_failure("a/one")
    registry.record_failure("a/one")
    assert registry.is_ejected("a/one")

    clock[0] = 31.0
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one")
    assert not registry.is_ejected("a/one")


def test_usable_indexes_skips_an_ejected_model() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one")

    assert registry.usable_indexes(("a/one", "b/two", "c/three")) == (1, 2)


def test_a_fully_ejected_chain_is_returned_intact() -> None:
    """Skipping a bad model is an optimisation; refusing to try anything is an outage."""
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=1, eject_seconds=30.0)
    registry.record_failure("a/one")
    registry.record_failure("b/two")

    assert registry.usable_indexes(("a/one", "b/two")) == (0, 1)


def test_ejection_can_be_switched_off() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=0, eject_seconds=30.0)
    for _ in range(10):
        registry.record_failure("a/one")

    assert not registry.is_ejected("a/one")
    assert registry.usable_indexes(("a/one", "b/two")) == (0, 1)


def test_failures_are_tracked_per_model_not_per_provider() -> None:
    clock = [0.0]
    registry = _registry(clock, eject_after_failures=2, eject_seconds=30.0)
    registry.record_failure("a/one")
    registry.record_failure("a/two")

    assert registry.usable_indexes(("a/one", "a/two")) == (0, 1)


# --------------------------------------------------------------- rate-based --


def test_rate_based_does_not_trip_below_min_samples() -> None:
    """One failure on a low-traffic model never benches it. The 8-sample floor
    exists precisely to absorb a single blip without ejecting a working model.
    """
    clock = [0.0]
    registry = _registry(clock, mode="rate_based", eject_min_samples=8)

    registry.record_failure("rare/model")
    assert not registry.is_ejected("rare/model")


def test_rate_based_trips_when_failure_rate_crosses_threshold() -> None:
    """Eight consecutive failures (8/8 = 100% >= 50%): bench."""
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    for _ in range(7):
        registry.record_failure("sick/model")
    # 7 samples < min_samples=8, so still unbenched.
    assert not registry.is_ejected("sick/model")

    registry.record_failure("sick/model")  # 8/8 = 100%
    assert registry.is_ejected("sick/model")


def test_rate_based_does_not_trip_under_threshold() -> None:
    """A 40% failure rate over the full window stays unbenched."""
    clock = [0.0]
    registry = _registry(clock, mode="rate_based")

    # Build a 4f/6s window. Successes are appended (not a wipe), so the
    # window reflects the last 10 outcomes exactly.
    for _ in range(4):
        registry.record_failure("blip/model")
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

    registry.record_failure("a/one")
    registry.record_failure("a/one")
    assert not registry.is_ejected("a/one")

    registry.record_failure("a/one")
    assert registry.is_ejected("a/one")
