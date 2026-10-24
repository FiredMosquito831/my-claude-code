"""A retried 429 with a zero backoff takes the ladder's ordinary decision (7.111.2).

Up to v7.111.1 the retry ladder in ``providers/rate_limit.py`` handed its own
computed delay to ``extend_reactive_block`` for a 429 with
``RATE_LIMIT_ROUTES_AROUND_MODEL`` off. With ``PROVIDER_RETRY_BACKOFF_*`` at
0 that delay is 0, ``extend_reactive_block`` refuses a block of 0 seconds,
and the ``ValueError`` left the ladder: the request ended in an internal error
instead of the retry the same inputs get with any positive backoff.

``rate_limit_ladder_baseline.json`` is the ladder's whole decision table
recorded with the v7.111.1 code (``c7265d49``) by
``rate_limit_ladder_corpus.py``. The fix must reproduce every cell of it
except the cells that crashed, and each of those must now equal the same
inputs with the smallest positive backoff, minus that backoff's block and
sleep.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

from tests.providers.rate_limit_ladder_corpus import (
    COOLDOWNS,
    EPSILON,
    cells,
    crashed,
    expand,
    record_digest,
    snapshot,
    without_the_sleep,
)

BASELINE_PATH = Path(__file__).with_name("rate_limit_ladder_baseline.json")

#: The cells v7.111.1 crashed in: every retryable 429 x route-around off x a
#: cooldown mode that benches x a retry left to take (PROVIDER_RETRY_ATTEMPTS
#: 2 or 3) x a zero computed delay, for both sequences and all three limiters.
CRASHED_CELLS = 2400


@cache
def _baseline() -> dict[str, dict[str, Any]]:
    return expand(json.loads(BASELINE_PATH.read_text(encoding="utf-8")))


@cache
def _specs() -> dict[str, dict[str, Any]]:
    return dict(cells())


def _zero_delay(spec: dict[str, Any]) -> bool:
    base, cap, jitter = spec["backoff"]
    return min(base, cap) == 0 and jitter * spec["draw"] == 0


def _the_defect(spec: dict[str, Any]) -> bool:
    return (
        spec["error"].startswith("429/")
        and spec["error"] != "429/failure-not-retryable"
        and not spec["routes_around"]
        and COOLDOWNS[spec["cooldown"]].benches
        and spec["attempts"] >= 2
        and _zero_delay(spec)
    )


def test_the_baseline_crashed_exactly_where_the_defect_says() -> None:
    """The recorded base: a ValueError in these cells and in no other."""

    baseline = _baseline()
    specs = _specs()
    crashed_keys = {key for key, record in baseline.items() if crashed(record)}
    assert len(crashed_keys) == CRASHED_CELLS
    assert crashed_keys == {key for key, spec in specs.items() if _the_defect(spec)}


def test_every_cell_is_the_v7_111_1_ladder_except_the_ones_that_crashed() -> None:
    """Decision-table equality: only the crashed cells move, and only to the fix."""

    baseline = _baseline()
    now = snapshot()
    assert now.keys() == baseline.keys()
    moved = {
        key
        for key, record in now.items()
        if record_digest(record) != record_digest(baseline[key])
    }
    crashed_keys = {key for key, record in baseline.items() if crashed(record)}
    assert moved == crashed_keys, sorted(moved ^ crashed_keys)[:20]

    smallest_positive = snapshot(epsilon=EPSILON, only=frozenset(crashed_keys))
    wrong = [
        key
        for key in sorted(crashed_keys)
        if now[key] != without_the_sleep(smallest_positive[key])
    ]
    assert wrong == [], wrong[:20]
    assert not any(crashed(record) for record in now.values())


def _users_row(attempts: int, sequence: str, error: str) -> str:
    # The user's shape: deadlines 0 (the executor's, not read here), route-
    # around off, the shipped cooldown policy, a zero backoff.
    return (
        f"plain|{error}|{sequence}|cooldown=provider-60-3600|routes_around=off"
        f"|attempts={attempts}|backoff=0,0,0|draw=0"
    )


def test_the_users_configuration_retries_instead_of_crashing() -> None:
    """Deadlines 0, route-around off, backoff 0, PROVIDER_RETRY_ATTEMPTS 1/2/3."""

    rows = {
        (attempts, sequence, error): _users_row(attempts, sequence, error)
        for attempts in (1, 2, 3)
        for sequence in ("always", "once-then-ok")
        for error in ("429/sdk-retry-after-15", "429/sdk-none")
    }
    baseline = _baseline()
    now = snapshot(only=frozenset(rows.values()))
    for (attempts, sequence, _error), key in rows.items():
        was, is_ = baseline[key], now[key]
        if attempts == 1:
            # One try, nothing to retry: never reached the block, never moved.
            assert not crashed(was)
            assert is_ == was
            continue
        assert crashed(was), key
        assert is_["blocks"] == []
        statuses = [try_[0] for try_ in is_["tries"]]
        if sequence == "always":
            # Every try on the same key, then the 429 itself goes up to the
            # pool and the chain -- the decision a positive backoff gets.
            assert is_["calls"] == attempts
            assert statuses == [429] * attempts
            assert is_["outcome"] == "raise:RateLimitError"
            assert is_["raised_try"] == attempts - 1
            assert is_["sleeps"] == [0.0] * (attempts - 1)
        else:
            assert is_["calls"] == 2
            assert statuses == [429, None]
            assert is_["outcome"] == "ok:served"
            assert is_["sleeps"] == [0.0]


def test_the_shipped_backoff_still_blocks_the_provider_for_its_delay() -> None:
    """The block itself is untouched wherever the delay is positive."""

    key = (
        "plain|429/sdk-retry-after-15|always|cooldown=provider-60-3600"
        "|routes_around=off|attempts=2|backoff=2,5,0.5|draw=0.5"
    )
    record = snapshot(only=frozenset({key}))[key]
    assert record == _baseline()[key]
    assert record["blocks"] == [2.25]
    assert record["sleeps"] == [2.25]
