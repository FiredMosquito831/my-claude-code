"""Replay the request that proved the request log was lying by omission.

``req_f3b018692ac14fb5b7c4562aa318472c`` spent 107 seconds making fifteen
upstream tries across three credentials -- twelve 429s and three 502s, with
96 seconds of it asleep in MCC's own backoff -- and the database recorded one
row: ``outcome=failed``, ``error_kind=upstream``, ``key_index=0``, one status.
The ladder replayed here is what the server log held and the database did not.
"""

import json
from pathlib import Path

import pytest

from my_claude_code.core.upstream_ladder import (
    AttemptLadder,
    CredentialDecision,
    LadderTry,
    ladder_payload,
    ladder_root_cause,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ladder_req_f3b018.json"

EXPECTED_ROOT_CAUSE = (
    "3 keys \N{MULTIPLICATION SIGN} 5 tries: 12\N{MULTIPLICATION SIGN}429, 3\N{MULTIPLICATION SIGN}502 — 96s of the 107s were MCC backoff "
    "sleeps; keys 0 and 1 benched 60s on 429 (no Retry-After); key 2 not "
    "charged (502 is not credential-shaped)"
)


@pytest.fixture(scope="module")
def replayed() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ladder = AttemptLadder(
        tries=[LadderTry(**entry) for entry in data["tries"]],
        decisions=[CredentialDecision(**entry) for entry in data["decisions"]],
        time_limiter_ms=data["time_limiter_ms"],
    )
    payload = ladder_payload(ladder)
    payload["root_cause"] = ladder_root_cause(
        payload,
        attempt_error_kind=data["attempt"]["error_kind"],
        attempt_duration_ms=data["attempt"]["duration_ms"],
    )
    return payload


def test_replayed_ladder_renders_the_expected_root_cause_line(replayed) -> None:
    assert replayed["root_cause"] == EXPECTED_ROOT_CAUSE


def test_replayed_ladder_summary_matches_the_measured_numbers(replayed) -> None:
    summary = replayed["summary"]

    assert summary["tries"] == 15
    assert summary["statuses_by_code"] == {"429": 12, "502": 3}
    assert summary["keys"] == 3
    # 2.7+4.0+9.0+16.1 + 2.2+4.9+8.5+16.7 + 2.2+4.9+8.4+16.5 = 96.1s
    assert summary["time_sleeping_ms"] == pytest.approx(96_100.0)
    assert summary["tries_dropped"] == 0


def test_every_key_the_request_touched_has_a_recorded_decision(replayed) -> None:
    """Two benched, one deliberately not charged -- and the log said neither."""
    decisions = {entry["key_index"]: entry for entry in replayed["credentials"]}

    assert set(decisions) == {0, 1, 2}
    assert decisions[0]["class"] == "rate_limit"
    assert decisions[0]["benched_for_s"] == 60.0
    assert decisions[2]["class"] is None
    assert decisions[2]["benched_for_s"] is None


def test_no_credential_material_survives_the_replay(replayed) -> None:
    """Key labels are masked; nothing else about a key is stored."""
    blob = json.dumps(replayed)

    assert "nvapi-" not in blob
    assert "sk-" not in blob
    for entry in replayed["credentials"]:
        assert entry["key_label"].startswith("user...")
