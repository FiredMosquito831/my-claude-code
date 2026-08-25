"""KeyPool rotation tests: parsing, masking, acquire policies, health transitions."""

import pytest

from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.core.rate_limit import MAX_RATE_LIMIT_COOLDOWN_SECONDS
from my_claude_code.websearch.rotation import (
    CIRCUIT_OPEN_SECONDS,
    LOCKOUT_BASE_SECONDS,
    RATE_LIMIT_COOLDOWN_SECONDS,
    KeyHealthState,
    KeyPool,
    default_rotation_policy,
    mask_key_label,
)
from tests.websearch.support import FakeClock


class TestParseAndMask:
    def test_parse_credential_keys_splits_and_strips(self) -> None:
        assert parse_credential_keys("k1,k2, k3 ,, ") == ("k1", "k2", "k3")

    @pytest.mark.parametrize("raw", [None, "", "   ", ",,"])
    def test_parse_credential_keys_empty(self, raw) -> None:
        assert parse_credential_keys(raw) == ()

    def test_mask_key_label_first4_last4(self) -> None:
        assert mask_key_label("sk-abcd1234wxyz") == "sk-a…wxyz"

    def test_mask_key_label_short_keys(self) -> None:
        assert mask_key_label("abcdef") == "…cdef"
        assert mask_key_label("abc") == "…"
        assert mask_key_label("") == ""

    def test_default_rotation_policy(self) -> None:
        assert default_rotation_policy(1) == "single"
        assert default_rotation_policy(2) == "failover"
        assert default_rotation_policy(9) == "failover"


class TestKeyPoolConstruction:
    def test_invalid_policy_rejected(self) -> None:
        with pytest.raises(ValueError, match="credential_rotation"):
            KeyPool(("k1",), policy="chaos")

    def test_empty_pool_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one key"):
            KeyPool((), policy="single")


class TestAcquirePolicies:
    def test_single_policy_only_uses_first_key(self) -> None:
        pool = KeyPool(("k0", "k1"), policy="single")
        assert pool.acquire() == (0, "k0")
        # Even excluding nothing, key 1 never serves under single.
        assert pool.acquire() == (0, "k0")
        # And once key 0 cools down, acquire dries up.
        pool.report_failure(0, kind="upstream")
        assert pool.acquire() is None

    def test_failover_prefers_lowest_usable_index(self) -> None:
        pool = KeyPool(("k0", "k1", "k2"), policy="failover")
        assert pool.acquire() == (0, "k0")
        assert pool.acquire(exclude=frozenset({0})) == (1, "k1")
        assert pool.acquire(exclude=frozenset({0, 1})) == (2, "k2")
        assert pool.acquire(exclude=frozenset({0, 1, 2})) is None

    def test_round_robin_cycles_keys(self) -> None:
        pool = KeyPool(("k0", "k1"), policy="round_robin")
        indices: list[int] = []
        for _ in range(4):
            acquired = pool.acquire()
            assert acquired is not None
            indices.append(acquired[0])
        assert indices == [0, 1, 0, 1]

    def test_round_robin_skips_unusable_and_excluded(self) -> None:
        pool = KeyPool(("k0", "k1", "k2"), policy="round_robin")
        pool.report_failure(0, kind="upstream")  # k0 cooldown
        assert pool.acquire() == (1, "k1")
        assert pool.acquire() == (2, "k2")
        assert pool.acquire(exclude=frozenset({2})) == (1, "k1")

    def test_least_used_picks_fewest_requests(self) -> None:
        pool = KeyPool(("k0", "k1", "k2"), policy="least_used")
        assert pool.acquire() == (0, "k0")  # tie -> lowest index
        assert pool.acquire() == (1, "k1")
        assert pool.acquire() == (2, "k2")
        assert pool.acquire() == (0, "k0")  # all at 1 -> lowest index again

    def test_least_used_skips_unusable(self) -> None:
        pool = KeyPool(("k0", "k1"), policy="least_used")
        pool.acquire()  # k0: 1 request
        pool.report_failure(0, kind="upstream")
        assert pool.acquire() == (1, "k1")


class TestHealthTransitions:
    def test_cooldown_tiers_escalate_with_consecutive_failures(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        for expected in (10.0, 30.0, 60.0):
            pool.report_failure(0, kind="upstream")
            health = pool.health_at(0)
            assert health.state is KeyHealthState.COOLDOWN
            assert health.state_until == pytest.approx(clock.now + expected)
            assert pool.acquire() is None
            clock.advance(expected)
            assert pool.acquire() is not None  # expired cooldown -> usable

    def test_fourth_consecutive_failure_opens_circuit(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        for tier in (10.0, 30.0, 60.0):
            pool.report_failure(0, kind="upstream")
            clock.advance(tier)
        pool.report_failure(0, kind="upstream")
        health = pool.health_at(0)
        assert health.state is KeyHealthState.CIRCUIT_OPEN
        assert health.state_until == pytest.approx(clock.now + CIRCUIT_OPEN_SECONDS)
        assert pool.acquire() is None
        clock.advance(CIRCUIT_OPEN_SECONDS)
        assert pool.acquire() is not None

    def test_beyond_threshold_uses_120s_cooldown_tier(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        for tier in (10.0, 30.0, 60.0, CIRCUIT_OPEN_SECONDS):
            pool.report_failure(0, kind="upstream")
            clock.advance(tier)
        pool.report_failure(0, kind="upstream")  # 5th consecutive
        health = pool.health_at(0)
        assert health.state is KeyHealthState.COOLDOWN
        assert health.state_until == pytest.approx(clock.now + 120.0)

    def test_auth_failure_locks_out_and_escalates(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        pool.report_failure(0, kind="auth")
        health = pool.health_at(0)
        assert health.state is KeyHealthState.LOCKED_OUT
        assert health.lockouts == 1
        assert health.state_until == pytest.approx(clock.now + LOCKOUT_BASE_SECONDS)
        clock.advance(LOCKOUT_BASE_SECONDS)
        pool.report_failure(0, kind="auth")
        assert health.state_until == pytest.approx(clock.now + LOCKOUT_BASE_SECONDS * 2)
        clock.advance(LOCKOUT_BASE_SECONDS * 2)
        pool.report_failure(0, kind="quota")  # quota also locks out
        assert health.state is KeyHealthState.LOCKED_OUT
        assert health.lockouts == 3
        assert health.state_until == pytest.approx(clock.now + LOCKOUT_BASE_SECONDS * 4)

    def test_lockout_escalation_is_capped(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        for _ in range(10):
            pool.report_failure(0, kind="auth")
            clock.advance(10_000)
        health = pool.health_at(0)
        assert health.state_until - clock.now <= 3600.0

    def test_success_resets_failures_and_lockouts(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        pool.report_failure(0, kind="auth")
        clock.advance(LOCKOUT_BASE_SECONDS)
        pool.report_success(0)
        health = pool.health_at(0)
        assert health.state is KeyHealthState.HEALTHY
        assert health.consecutive_failures == 0
        assert health.lockouts == 0
        assert health.last_error is None
        # Next auth failure starts the escalation from scratch.
        pool.report_failure(0, kind="auth")
        assert health.state_until == pytest.approx(clock.now + LOCKOUT_BASE_SECONDS)

    def test_rate_limit_uses_fixed_cooldown_outside_failure_ladder(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        pool.report_rate_limit(0)
        health = pool.health_at(0)
        assert health.state is KeyHealthState.COOLDOWN
        assert health.state_until == pytest.approx(
            clock.now + RATE_LIMIT_COOLDOWN_SECONDS
        )
        assert health.rate_limits == 1
        assert health.consecutive_failures == 0  # 429 does not climb the ladder

    def test_rate_limit_honours_provider_supplied_retry_after(self) -> None:
        """The provider's own reset beats our default guess."""

        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        pool.report_rate_limit(0, retry_after_seconds=7.5)
        health = pool.health_at(0)
        assert health.state is KeyHealthState.COOLDOWN
        assert health.state_until == pytest.approx(clock.now + 7.5)

    def test_rate_limit_retry_after_is_capped(self) -> None:
        """A hostile or misparsed header cannot bench a key indefinitely."""

        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        pool.report_rate_limit(0, retry_after_seconds=999_999.0)
        health = pool.health_at(0)
        assert health.state_until == pytest.approx(
            clock.now + MAX_RATE_LIMIT_COOLDOWN_SECONDS
        )

    def test_report_failure_with_rate_limit_kind_delegates(self) -> None:
        clock = FakeClock()
        pool = KeyPool(("k0",), policy="failover", clock=clock)
        pool.report_failure(0, kind="rate_limit")
        health = pool.health_at(0)
        assert health.rate_limits == 1
        assert health.state_until == pytest.approx(
            clock.now + RATE_LIMIT_COOLDOWN_SECONDS
        )


class TestSnapshot:
    def test_snapshot_masks_keys_and_reports_state(self) -> None:
        clock = FakeClock()
        pool = KeyPool(
            ("sk-aaaa1111bbbb", "sk-cccc2222dddd"), policy="failover", clock=clock
        )
        pool.acquire()
        pool.report_failure(0, kind="upstream", message="boom")
        snapshot = pool.snapshot()
        assert snapshot["policy"] == "failover"
        first, second = snapshot["keys"]
        assert first["key_label"] == "sk-a…bbbb"
        assert first["state"] == "cooldown"
        assert first["state_remaining_seconds"] == pytest.approx(10.0)
        assert first["requests"] == 1
        assert first["failures"] == 1
        assert first["last_error"] == "boom"
        assert second["state"] == "healthy"
        assert second["requests"] == 0

    def test_snapshot_does_not_leak_raw_keys(self) -> None:
        pool = KeyPool(("super-secret-key-material",), policy="single")
        rendered = str(pool.snapshot())
        assert "super-secret-key-material" not in rendered
        assert "supe…rial" in rendered
