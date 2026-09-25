"""The proxy speed ledger (7.54.0): spec §7.2-7.3 and the §6.1 labels."""

from typing import Any

import pytest

from my_claude_code.core.proxy_speed import (
    KIND_CHECK,
    KIND_DIAL,
    LIVE_TTFT_MIN_SAMPLES,
    MAX_KEYS,
    MODEL_TTFT_BASELINE_MIN,
    SAMPLE_RING,
    SCORE_WINDOW_SECONDS,
    TTFT_FACTOR_MAX,
    ProxySpeedLedger,
    SpeedSample,
    dial_sample,
    success_rate,
)

NOW = 1_800_000_000.0
F_MS = 5000.0
SLOW_MS = 3000.0


def _check(ok: bool, at: float = NOW, **timings: Any) -> SpeedSample:
    return SpeedSample(kind=KIND_CHECK, at=at, ok=ok, **timings)


def _score(ledger: ProxySpeedLedger, address: str = "203.0.113.7:1080"):
    return ledger.score(
        address, "opencode", failure_cost_ms=F_MS, slow_ms=SLOW_MS, now=NOW
    )


def test_success_rate_is_smoothed() -> None:
    """One pass is not 100 %, and no sample is not 0 %."""

    assert success_rate(1, 1) == pytest.approx(2 / 3)
    assert success_rate(0, 0) == pytest.approx(0.5)
    assert success_rate(4, 5) == pytest.approx(5 / 7)

    ledger = ProxySpeedLedger()
    ledger.note("203.0.113.7:1080", "opencode", _check(True, connect_ms=100))
    score = _score(ledger)
    assert (score.successes, score.samples) == (1, 1)
    assert score.success_rate == pytest.approx(2 / 3)
    # Below three samples a clean record is working even though 2/3 < 0.8:
    # the smoothing makes 0.8 unreachable before the third sample.
    assert score.state == "working"


def test_expected_setup_penalises_failures() -> None:
    """E = s + (1 - r) / r * F, with F the live connect timeout."""

    reliable, unreliable = ProxySpeedLedger(), ProxySpeedLedger()
    for _ in range(4):
        reliable.note(
            "a:1", "opencode", _check(True, connect_ms=400, tunnel_ms=300, tls_ms=300)
        )
    unreliable.note("a:1", "opencode", _check(True, connect_ms=200, tunnel_ms=100))
    for _ in range(3):
        unreliable.note("a:1", "opencode", _check(False, failure="connect_timeout"))

    fast_but_flaky = _score(unreliable, "a:1")
    slow_but_sure = _score(reliable, "a:1")

    assert fast_but_flaky.setup_ms == pytest.approx(300.0)
    rate = 2 / 6
    assert fast_but_flaky.expected_ms == pytest.approx(300 + (1 - rate) / rate * F_MS)
    assert fast_but_flaky.state == "flaky"
    assert slow_but_sure.setup_ms == pytest.approx(1000.0)
    assert slow_but_sure.expected_ms == pytest.approx(1000 + (1 / 6) / (5 / 6) * F_MS)
    # The quick address that fails three times in four ranks behind.
    assert slow_but_sure.rank_key < fast_but_flaky.rank_key
    assert slow_but_sure.state == "working"


def test_samples_older_than_24h_ignored() -> None:
    ledger = ProxySpeedLedger()
    old = NOW - SCORE_WINDOW_SECONDS - 60
    for _ in range(5):
        ledger.note("a:1", "opencode", _check(False, at=old))
    ledger.note("a:1", "opencode", _check(True, at=NOW - 60, connect_ms=900))

    score = _score(ledger, "a:1")
    assert (score.successes, score.samples) == (1, 1)
    # And they are not written back either.
    document = ledger.snapshot(now=NOW)
    assert len(document["keys"][0]["samples"]) == 1


def test_ledger_is_bounded() -> None:
    ledger = ProxySpeedLedger()
    for index in range(SAMPLE_RING + 5):
        ledger.note("a:1", "opencode", _check(True, at=NOW - 100 + index))
    assert len(ledger.samples("a:1", "opencode")) == SAMPLE_RING

    for index in range(MAX_KEYS + 10):
        ledger.note(f"10.0.{index // 256}.{index % 256}:80", "opencode", _check(True))
    assert len(ledger) == MAX_KEYS
    # Oldest-first: the first key written is the one that went.
    assert ledger.samples("a:1", "opencode") == []
    assert ledger.samples(
        f"10.0.{(MAX_KEYS + 9) // 256}.{(MAX_KEYS + 9) % 256}:80", "opencode"
    )


def test_key_is_source_agnostic() -> None:
    """A feed check, a typed address's Test and a live dial land on one key."""

    ledger = ProxySpeedLedger()
    ledger.note("198.51.100.9:8080", "OpenCode", _check(True, connect_ms=100))
    ledger.note_dial_rows(
        "opencode",
        [
            {
                "proxy": "198.51.100.9:8080",
                "connect_ms": 80.0,
                "handshake_ms": 40.0,
                "outcome": "answered",
            },
            {"proxy": "direct", "outcome": "answered"},
        ],
        now=NOW,
    )
    score = _score(ledger, "198.51.100.9:8080")
    assert (score.successes, score.samples) == (2, 2)
    # Another provider's samples are its own.
    assert _score(ledger, "198.51.100.9:8080").samples == 2
    other = ledger.score(
        "198.51.100.9:8080",
        "nvidia_nim",
        failure_cost_ms=F_MS,
        slow_ms=SLOW_MS,
        now=NOW,
    )
    assert other.state == "untested"


def test_dial_rows_map_to_samples() -> None:
    assert dial_sample({"outcome": "dialing"}, at=NOW) is None
    assert dial_sample({"outcome": "switched"}, at=NOW) is None
    failed = dial_sample({"outcome": "switched", "reason": "ConnectTimeout"}, at=NOW)
    assert failed is not None and failed.kind == KIND_DIAL and not failed.ok
    answered = dial_sample(
        {"outcome": "answered", "connect_ms": 50.0, "handshake_ms": 200.0}, at=NOW
    )
    assert answered is not None and answered.ok and answered.setup_ms == 250.0


def test_slow_is_its_own_label() -> None:
    ledger = ProxySpeedLedger()
    for _ in range(3):
        ledger.note("a:1", "opencode", _check(True, connect_ms=2500, tls_ms=900))
    assert _score(ledger, "a:1").state == "slow"


def test_live_ttft_is_normalised_per_model() -> None:
    """A proxy is rated against its model's own median first token."""

    ledger = ProxySpeedLedger()
    # The model's baseline, from direct attempts: 2 s typical.
    for ttft in (2000.0, 1800.0, 2200.0):
        assert ledger.note_ttft("opencode", "m", ttft, now=NOW) is None
    # A different, slower model on the same provider does not move it.
    for ttft in (30000.0, 31000.0, 29000.0):
        ledger.note_ttft("opencode", "slow-model", ttft, now=NOW)
    ratios = [
        ledger.note_ttft("opencode", "m", 6000.0, address="a:1", now=NOW)
        for _ in range(LIVE_TTFT_MIN_SAMPLES)
    ]
    assert ratios[0] == pytest.approx(3.0)
    score = _score(ledger, "a:1")
    assert score.live_samples == LIVE_TTFT_MIN_SAMPLES
    assert 1.0 < score.ttft_factor <= TTFT_FACTOR_MAX
    # TTFT samples do not count as reliability samples.
    assert score.samples == 0


def test_ttft_needs_a_baseline_first() -> None:
    ledger = ProxySpeedLedger()
    for _ in range(MODEL_TTFT_BASELINE_MIN):
        assert ledger.note_ttft("opencode", "m", 1000.0, address="a:1", now=NOW) is None
    assert ledger.note_ttft("opencode", "m", 500.0, address="a:1", now=NOW) == 0.5


def test_snapshot_round_trips_and_bad_documents_are_empty() -> None:
    ledger = ProxySpeedLedger()
    ledger.note("a:1", "opencode", _check(True, connect_ms=120, tunnel_ms=80))
    for ttft in (1000.0, 1000.0, 1000.0, 3000.0):
        ledger.note_ttft("opencode", "m", ttft, address="a:1", now=NOW)
    assert ledger.dirty
    document = ledger.snapshot(now=NOW)
    assert not ledger.dirty

    copy = ProxySpeedLedger()
    assert copy.restore(document) == 1
    assert _score(copy, "a:1") == _score(ledger, "a:1")

    for bad in (None, [], {"keys": "x"}, {"keys": [{"address": "", "provider": "p"}]}):
        assert copy.restore(bad) == 0
        assert len(copy) == 0
