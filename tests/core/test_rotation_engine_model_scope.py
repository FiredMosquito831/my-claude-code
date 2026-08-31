"""The rotation engine's (key, model) 429 bench, driven on an injected clock.

Measured defect this pins (``req_cf646eed209a4c7f95064201d4a2a339``,
2026-08-31T14:10:35Z): NVIDIA NIM rate-limits ``moonshotai/kimi-k3`` on all
three keys inside 0.1s while ``nvidia/nemotron-3-ultra-550b-a55b`` and
``minimax`` answer on those same keys in the same second, and NIM sends no
``Retry-After``. Charging the whole key for that removed every NIM model from
the route for 60s and cost one request 81 seconds.

The engine has no timer. These tests travel in time by moving the clock the
engine was handed, exactly as ``refresh()`` expects.
"""

from dataclasses import replace

import pytest

from my_claude_code.core.credential_rotation import (
    MAX_MODEL_BENCHES_PER_SLOT,
    PROVIDER_TUNING,
    WEBSEARCH_TUNING,
    PoolHealthState,
    RotationEngine,
)

KIMI = "moonshotai/kimi-k3"
NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"
MINIMAX = "minimaxai/minimax-m2"


class _Clock:
    """A hand-wound monotonic clock."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _engine(
    keys: int = 3,
    *,
    escalation: int = 2,
    policy: str = "round_robin",
    rate_limit_seconds: float = 60.0,
) -> tuple[RotationEngine, _Clock]:
    clock = _Clock()
    tuning = replace(
        PROVIDER_TUNING,
        rate_limit_seconds=rate_limit_seconds,
        model_bench_escalation=escalation,
    )
    return RotationEngine(keys, policy=policy, tuning=tuning, clock=clock), clock


def test_a_429_with_a_model_benches_only_that_model_and_leaves_the_slot_healthy() -> (
    None
):
    engine, clock = _engine()

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.HEALTHY
    assert slot.rate_limits == 1
    assert slot.model_benches == {KIMI: clock.now + 60.0}
    # The whole-key deadline was never written, so nothing else on this key
    # is affected.
    assert slot.cooldown_until == 0.0
    assert engine.selectable(0, KIMI) is False
    assert engine.selectable(0, NEMOTRON) is True
    assert engine.selectable(0) is True


def test_a_second_model_429ing_on_the_same_key_benches_the_whole_key() -> None:
    engine, clock = _engine(escalation=2)

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)
    assert engine.slot(0).state is PoolHealthState.HEALTHY

    engine.fail(0, "rate_limit", retry_after=None, model=NEMOTRON)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.COOLDOWN
    assert slot.cooldown_until == pytest.approx(clock.now + 60.0)
    assert engine.selectable(0, MINIMAX) is False


def test_escalation_uses_the_longest_live_model_bench_as_the_key_window() -> None:
    """The engine may not take back time it has already published."""
    engine, clock = _engine(escalation=2)

    engine.fail(0, "rate_limit", retry_after=300.0, model=KIMI)
    clock.advance(10.0)
    # This 429 only asks for 30s, but kimi is still benched for 290s.
    engine.fail(0, "rate_limit", retry_after=30.0, model=NEMOTRON)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.COOLDOWN
    assert slot.cooldown_until == pytest.approx(1_000.0 + 300.0)
    assert slot.cooldown_until - clock.now == pytest.approx(290.0)


def test_escalation_floors_the_key_window_at_what_this_429_asked_for() -> None:
    """And it cannot invent time either: the ask is the floor, not the roof."""
    engine, clock = _engine(escalation=2)

    engine.fail(0, "rate_limit", retry_after=5.0, model=KIMI)
    clock.advance(4.0)
    engine.fail(0, "rate_limit", retry_after=120.0, model=NEMOTRON)

    assert engine.slot(0).cooldown_until == pytest.approx(clock.now + 120.0)


def test_escalation_of_one_restores_the_whole_key_bench_on_every_429() -> None:
    """The documented no-redeploy rollback to 6.18.0 behaviour."""
    engine, clock = _engine(escalation=1)

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.COOLDOWN
    assert slot.cooldown_until == pytest.approx(clock.now + 60.0)
    assert slot.model_benches == {}


def test_escalation_of_zero_never_benches_the_key() -> None:
    engine, _ = _engine(escalation=0)

    for model in (KIMI, NEMOTRON, MINIMAX):
        engine.fail(0, "rate_limit", retry_after=None, model=model)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.HEALTHY
    assert set(slot.model_benches) == {KIMI, NEMOTRON, MINIMAX}


def test_a_model_bench_expires_on_its_own_deadline_without_touching_the_slot() -> None:
    """Lazy expiry, the same mechanism ``refresh()`` already used."""
    engine, clock = _engine()

    engine.fail(0, "rate_limit", retry_after=30.0, model=KIMI)
    assert engine.selectable(0, KIMI) is False

    clock.advance(29.0)
    assert engine.selectable(0, KIMI) is False

    clock.advance(2.0)
    assert engine.selectable(0, KIMI) is True
    # refresh() is the other reader, and it drops the entry outright.
    engine.refresh()
    assert engine.slot(0).model_benches == {}
    assert engine.slot(0).state is PoolHealthState.HEALTHY


def test_selectable_indexes_skip_a_key_benched_for_the_model_and_keep_it_for_others() -> (
    None
):
    engine, _ = _engine(keys=3)

    engine.fail(1, "rate_limit", retry_after=None, model=KIMI)

    assert engine.selectable_indexes(KIMI) == (0, 2)
    assert engine.selectable_indexes(NEMOTRON) == (0, 1, 2)
    assert engine.selectable_indexes() == (0, 1, 2)
    assert engine.model_benched_indexes(KIMI) == (1,)
    assert engine.model_benched_indexes(NEMOTRON) == ()
    assert engine.choose(model=KIMI) in (0, 2)


def test_a_success_clears_every_model_bench_on_that_key() -> None:
    """A success on any model is evidence the credential is fine."""
    engine, _ = _engine(escalation=0)

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)
    engine.fail(0, "rate_limit", retry_after=None, model=NEMOTRON)
    engine.succeed(0)

    assert engine.slot(0).model_benches == {}
    assert engine.selectable(0, KIMI) is True


def test_restore_and_restore_all_clear_model_benches() -> None:
    engine, _ = _engine(keys=2, escalation=0)

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)
    engine.fail(1, "rate_limit", retry_after=None, model=KIMI)

    assert engine.restore(0) is True
    assert engine.slot(0).model_benches == {}
    # Slot 1 is HEALTHY, so restore_all restores nothing -- but a reset that
    # left it unroutable for kimi would be lying to the operator.
    assert engine.restore_all() == 0
    assert engine.slot(1).model_benches == {}


def test_model_benches_are_capped_and_evict_the_soonest_to_expire() -> None:
    engine, clock = _engine(escalation=0)

    for index in range(MAX_MODEL_BENCHES_PER_SLOT + 5):
        # Ascending windows, so model 0 is the soonest to expire.
        engine.fail(0, "rate_limit", retry_after=100.0 + index, model=f"m{index}")

    # The trim happens on the next read, the same lazy contract as expiry.
    engine.refresh()
    benches = engine.slot(0).model_benches
    assert len(benches) == MAX_MODEL_BENCHES_PER_SLOT
    assert "m0" not in benches
    assert "m4" not in benches
    assert "m5" in benches
    assert engine.slot(0).state is PoolHealthState.HEALTHY
    assert clock.now == 1_000.0


def test_forced_single_still_serves_slot_zero_with_a_live_model_bench() -> None:
    """A one-key pool must never report itself unable to serve.

    The regression ``selectable_indexes``' forced-single short-circuit exists
    for: throttle_remaining() would otherwise claim a working provider was
    benched.
    """
    engine, _ = _engine(keys=1, policy="round_robin")

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)

    assert engine.selectable_indexes(KIMI) == (0,)
    assert engine.choose(model=KIMI) == 0


def test_note_rate_limit_still_benches_the_whole_slot() -> None:
    """The websearch entry point carries no model and must stay whole-key."""
    engine, clock = _engine(escalation=2)

    engine.note_rate_limit(0, retry_after=None)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.COOLDOWN
    assert slot.cooldown_until == pytest.approx(clock.now + 60.0)
    assert slot.model_benches == {}


def test_websearch_tuning_never_scopes_a_429_to_a_model() -> None:
    """Search quotas have no per-model concept and none is invented for them."""
    assert WEBSEARCH_TUNING.model_bench_escalation == 1

    clock = _Clock()
    engine = RotationEngine(2, policy="failover", tuning=WEBSEARCH_TUNING, clock=clock)

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.COOLDOWN
    assert slot.model_benches == {}


def test_shortest_bench_remaining_reads_a_model_bench_on_a_healthy_slot() -> None:
    engine, clock = _engine(keys=2)

    engine.fail(0, "rate_limit", retry_after=45.0, model=KIMI)
    engine.fail(1, "rate_limit", retry_after=90.0, model=KIMI)
    clock.advance(5.0)

    # Nothing is unhealthy, so the unscoped answer is still zero.
    assert engine.shortest_bench_remaining() == 0.0
    assert engine.shortest_bench_remaining(KIMI) == pytest.approx(40.0)
    assert engine.shortest_bench_remaining(NEMOTRON) == 0.0


def test_the_provider_preset_scopes_by_default_and_websearch_does_not() -> None:
    assert PROVIDER_TUNING.model_bench_escalation == 2
    assert WEBSEARCH_TUNING.model_bench_escalation == 1
