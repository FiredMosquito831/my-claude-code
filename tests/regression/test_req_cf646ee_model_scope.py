"""Replay the request that proved a 429 was being charged to the wrong thing.

``req_cf646eed209a4c7f95064201d4a2a339`` (2026-08-31T14:10:35Z) asked for
``nvidia_nim/moonshotai/kimi-k3`` on a three-key round-robin pool, met fourteen
429s and one 502 in fifteen tries, and spent 51 of its 57 seconds asleep in
MCC's own backoff. All three keys were then benched sixty seconds -- and NIM
sends no ``Retry-After``, so sixty seconds was the operator's cooldown, not the
provider's ask. Attempt 7, ``nvidia/nemotron-3-ultra-550b-a55b``, is recorded
``skipped``: every NIM key was health-benched, so every NIM *model* was off the
route. The request was finally served 81 seconds in by a different provider,
and at 14:13:07Z nemotron answered 200 on key 0 with nothing changed but the
clock.

Direct probes taken the same minute: kimi-k3 429s on all three keys inside
0.1s while nemotron and minimax answer on those same keys in the same second.
The limit was the model's, and the pool charged the key.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from my_claude_code.core.credential_rotation import (
    PROVIDER_TUNING,
    PoolHealthState,
    RotationEngine,
)
from my_claude_code.core.upstream_ladder import (
    AttemptLadder,
    CredentialDecision,
    LadderTry,
    ladder_payload,
    ladder_root_cause,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ladder_req_cf646ee.json"

KIMI = "moonshotai/kimi-k3"
NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"

EXPECTED_ROOT_CAUSE = (
    "3 keys \N{MULTIPLICATION SIGN} 5 tries: 14\N{MULTIPLICATION SIGN}429, "
    "1\N{MULTIPLICATION SIGN}502 — 50s of the 57s were MCC backoff sleeps; "
    "keys 0, 1 and 2 benched 60s for moonshotai/kimi-k3 on 429 "
    "(no Retry-After); no key charged"
)


@pytest.fixture(scope="module")
def incident() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def replayed(incident) -> dict:
    ladder = AttemptLadder(
        tries=[LadderTry(**entry) for entry in incident["tries"]],
        decisions=[CredentialDecision(**entry) for entry in incident["decisions"]],
        time_limiter_ms=incident["time_limiter_ms"],
    )
    payload = ladder_payload(ladder)
    payload["root_cause"] = ladder_root_cause(
        payload,
        attempt_error_kind=incident["attempt"]["error_kind"],
        attempt_duration_ms=incident["attempt"]["duration_ms"],
    )
    return payload


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _pool() -> tuple[RotationEngine, _Clock]:
    """The incident's pool, on this version's default escalation of 2."""
    clock = _Clock()
    tuning = replace(PROVIDER_TUNING, rate_limit_seconds=60.0)
    assert tuning.model_bench_escalation == 2
    return RotationEngine(3, policy="round_robin", tuning=tuning, clock=clock), clock


def test_the_replayed_incident_matches_the_measured_numbers(replayed) -> None:
    summary = replayed["summary"]

    assert summary["tries"] == 15
    assert summary["statuses_by_code"] == {"429": 14, "502": 1}
    assert summary["keys"] == 3
    assert summary["time_upstream_ms"] == pytest.approx(6144.9)
    assert summary["time_sleeping_ms"] == pytest.approx(50_967.6)
    assert summary["time_limiter_ms"] == 0.0
    assert summary["tries_dropped"] == 0


def test_the_replayed_incident_benches_only_kimi_on_all_three_keys() -> None:
    engine, clock = _pool()

    # The fourteen 429s land on three keys; NIM published no Retry-After, so
    # each falls back to the operator's sixty seconds.
    for key in range(3):
        engine.fail(key, "rate_limit", retry_after=None, model=KIMI)

    for key in range(3):
        slot = engine.slot(key)
        # The whole point: the credential is not at fault and is not charged.
        assert slot.state is PoolHealthState.HEALTHY
        assert slot.cooldown_until == 0.0
        assert slot.model_benches == {KIMI: clock.now + 60.0}
    assert engine.model_benched_indexes(KIMI) == (0, 1, 2)
    assert engine.selectable_indexes(KIMI) == ()
    assert engine.shortest_bench_remaining(KIMI) == pytest.approx(60.0)


def test_nemotron_is_selectable_on_every_key_while_kimi_is_benched() -> None:
    """The incident's point, stated as engine state.

    Attempt 7 was skipped because 6.18.0 had benched all three credentials.
    Here the credentials are healthy and only kimi is unroutable, so the
    routing layer has three keys to offer nemotron the moment it asks.
    Routing itself does not change until the reactive block is removed --
    this PR is observability only -- but the pool's answer is now correct.
    """
    engine, _ = _pool()

    for key in range(3):
        engine.fail(key, "rate_limit", retry_after=None, model=KIMI)

    assert engine.selectable_indexes(NEMOTRON) == (0, 1, 2)
    assert engine.model_benched_indexes(NEMOTRON) == ()
    assert engine.shortest_bench_remaining(NEMOTRON) == 0.0
    assert engine.choose(model=NEMOTRON) in (0, 1, 2)
    # And nothing about the pool as a whole reads as benched either.
    assert engine.shortest_bench_remaining() == 0.0


def test_a_second_throttled_model_still_benches_the_key_itself() -> None:
    """The escape hatch against a genuinely key-wide limit, one 429 later."""
    engine, clock = _pool()

    engine.fail(0, "rate_limit", retry_after=None, model=KIMI)
    engine.fail(0, "rate_limit", retry_after=None, model=NEMOTRON)

    slot = engine.slot(0)
    assert slot.state is PoolHealthState.COOLDOWN
    assert slot.cooldown_until == pytest.approx(clock.now + 60.0)


def test_the_root_cause_line_names_the_model_and_says_no_key_was_charged(
    replayed,
) -> None:
    assert replayed["root_cause"] == EXPECTED_ROOT_CAUSE


def test_the_decisions_carry_the_pair_and_not_a_credential_wide_bench(
    replayed,
) -> None:
    decisions = {entry["key_index"]: entry for entry in replayed["credentials"]}

    assert set(decisions) == {0, 1, 2}
    for entry in decisions.values():
        assert entry["class"] == "rate_limit"
        assert entry["status"] == 429
        assert entry["retry_after"] is None
        assert entry["model"] == KIMI
        assert entry["model_benched_for_s"] == 60.0
        # The credential-wide bench is a different fact, and it did not happen.
        assert entry["benched_for_s"] is None


def test_no_credential_material_survives_the_replay(replayed) -> None:
    """Key labels are masked; nothing else about a key is stored."""
    blob = json.dumps(replayed)

    assert "nvapi-" not in blob
    assert "sk-" not in blob
    for entry in replayed["credentials"]:
        assert entry["key_label"].startswith("nvap...")
        assert len(entry["key_label"]) <= 12
