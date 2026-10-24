"""DECISION TABLE: with no search chain set, one failure never takes search down.

User decision Q8 (specs/INVESTIGATION-WEBSEARCH-ROUTING.md, 2026-10-25 ~21:10),
with the rules of Q3: the search route used without ``WEB_SEARCH_CHAIN``
follows the model rules. A key is benched only for a rejected key (401/403),
a 429 (for the stated wait) or an exact out-of-credits phrase; a 5xx, a
timeout, a refused connection or an unreadable body leaves it untouched; the
only key of a provider is never benched into an outage (IV.4), and inside a
429's wait the search is answered "rate limited, retry after N s" with no
call made.

How: ``websearch_no_chain_pool_world.py`` drives the product's own search
entry (``outbound._run_web_search``) over a real adapter on an
``httpx.MockTransport``, three searches per cell (t = 0, 0.5 s, 8 s). Its
baseline, ``websearch_no_chain_pool_baseline.json``, was written by the BASE
tree (7.112.0, before the fix). This test reads the live table against it:

* everything the fix must not touch is equal to the baseline -- the first
  search of every cell (which keys are tried, in which order, how it ends,
  its ``error_code``), every success, every 400, and every key-shaped charge
  on a pool that still has another key to serve from;
* the cells that change are EXACTLY the ones the decision names, and in each
  one the live values say what the decision says;
* every failed search tells the client what happened, in MCC's own words.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.contracts.websearch_no_chain_pool_world import (
    ANSWERS,
    BASELINE_NAME,
    SETUPS,
    Setup,
    build_table,
    cell_id,
)

BASELINE_PATH = Path(__file__).with_name(BASELINE_NAME)

#: Failures that say nothing about the key: never charged.
NOT_KEY_SHAPED = frozenset(
    {
        "s500",
        "s503",
        "timeout",
        "connect",
        "bad_json",
        "raw_error",
        # The model path's rules, read through the chain's translation: a 402
        # naming no credits phrase is unsure, and unsure never benches; a 403
        # whose words refuse the caller's country is about the address.
        "s402_bare",
        "s403_region",
    }
)
#: Failures that charge the key, exactly as before.
KEY_SHAPED = frozenset({"s401", "s403", "s402_credits", "s429", "s429_ra7"})
RATE_LIMITS = frozenset({"s429", "s429_ra7"})
#: A rejected query: never charged, never rotated -- untouched by Q8.
REQUEST_SHAPED = frozenset({"s400", "s400_credits"})

_SEARCHES = ("s1", "s2", "s3")
_TEXT_FIELDS = frozenset(f"{search}_text" for search in _SEARCHES)
_OLD_GENERIC_TEXT = "Web tool request failed."


@pytest.fixture(scope="module")
def live() -> dict[str, dict[str, Any]]:
    return asyncio.run(build_table())


@pytest.fixture(scope="module")
def baseline() -> dict[str, dict[str, Any]]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _setup(name: str) -> Setup:
    return next(setup for setup in SETUPS if setup.name == name)


def _all_fail(setup: Setup) -> bool:
    return setup.keys > 0 and len(setup.failing) == setup.keys


def _expected_changed_cells() -> set[str]:
    """The cells Q8 changes, from the decision's own words.

    * every never-charged failure on every pool (the key is no longer
      benched, so the next search finds it usable);
    * every key-shaped failure on a keyless pool (nothing to bench);
    * every key-shaped failure on a pool that serves from one slot (no
      outage: the next search tries the key again, or -- inside a 429's wait
      -- answers how long to wait);
    * every key-shaped failure on a pool whose every key failed (the no-call
      refusal now says which keys are benched and for how long, and a pool
      benched only by 429s answers ``too_many_requests``).
    """
    changed = set()
    for answer in ANSWERS:
        for setup in SETUPS:
            name = answer.name
            if name in NOT_KEY_SHAPED or (
                name in KEY_SHAPED and (setup.one_slot or _all_fail(setup))
            ):
                changed.add(cell_id(answer, setup))
    return changed


def test_the_baseline_covers_every_cell(baseline, live) -> None:
    assert set(baseline) == set(live)
    assert len(live) == len(ANSWERS) * len(SETUPS)


def test_the_first_search_of_every_cell_is_unchanged(baseline, live) -> None:
    """Rotation is untouched: same keys, same order, same ending, same code."""
    for cell, record in live.items():
        for field in ("s1_calls", "s1_result", "s1_code", "s1_message"):
            assert record[field] == baseline[cell][field], (cell, field)


def test_every_success_and_every_rejected_query_is_unchanged(baseline, live) -> None:
    for cell, record in live.items():
        answer = cell.split("|", 1)[0]
        if answer == "ok":
            assert record == baseline[cell], cell
        if answer in REQUEST_SHAPED:
            for field, value in record.items():
                if field not in _TEXT_FIELDS:
                    assert value == baseline[cell][field], (cell, field)


def test_the_changed_cells_are_exactly_the_decided_ones(baseline, live) -> None:
    """Ignoring the client sentence (its own test below), these and no others."""
    changed = {
        cell
        for cell, record in live.items()
        if any(
            value != baseline[cell][field]
            for field, value in record.items()
            if field not in _TEXT_FIELDS
        )
    }
    expected = _expected_changed_cells()
    assert changed - expected == set(), "changed without a decision"
    assert expected - changed == set(), "decided but not changed"


def test_a_failure_that_is_not_about_the_key_never_benches_it(live) -> None:
    for cell, record in live.items():
        answer = cell.split("|", 1)[0]
        if answer in NOT_KEY_SHAPED:
            states = {key[0] for key in record["s1_keys_after"]}
            assert states == {"healthy"}, (cell, record["s1_keys_after"])


def test_a_key_shaped_failure_charges_exactly_as_before(baseline, live) -> None:
    """Same state, same seconds, same lockout tier -- on every keyed pool."""
    for cell, record in live.items():
        answer, setup = cell.split("|", 1)
        if answer in KEY_SHAPED and _setup(setup).keys > 0:
            assert record["s1_keys_after"] == baseline[cell]["s1_keys_after"], cell
            assert any(key[0] != "healthy" for key in record["s1_keys_after"]), cell


def test_a_keyless_pool_is_never_benched(live) -> None:
    for cell, record in live.items():
        if cell.endswith("|keyless"):
            for search in _SEARCHES:
                assert record[f"{search}_keys_after"] == [["healthy", 0, 0]], cell
            assert record["s2_calls"] == [0], cell


def test_a_one_slot_pool_is_never_benched_into_an_outage(live) -> None:
    """The next search calls -- unless a 429 asked for a wait it is still in."""
    for cell, record in live.items():
        answer, setup_name = cell.split("|", 1)
        setup = _setup(setup_name)
        if not setup.one_slot or answer == "ok":
            continue
        if answer in RATE_LIMITS and setup.keys > 0:
            assert record["s2_calls"] == [], cell
            assert record["s2_code"] == "too_many_requests", cell
            assert "retry after" in record["s2_text"], cell
            if answer == "s429_ra7":
                # t = 8 s: the 7 s wait is over, the key is called again.
                assert record["s3_calls"] == [0], cell
                assert record["s3_result"] == "results", cell
            continue
        assert record["s2_calls"] == [0], cell
        assert record["s2_result"] == "results", cell


def test_a_pool_benched_only_by_429s_answers_too_many_requests(live) -> None:
    for cell, record in live.items():
        answer, setup_name = cell.split("|", 1)
        if answer in RATE_LIMITS and _all_fail(_setup(setup_name)):
            assert record["s2_calls"] == [], cell
            assert record["s2_code"] == "too_many_requests", cell
            assert "retry after" in record["s2_text"], cell


def test_every_failed_search_says_what_happened(baseline, live) -> None:
    """Never the old catch-all; a success still has no sentence."""
    for cell, record in live.items():
        for search in _SEARCHES:
            text = record[f"{search}_text"]
            result = record[f"{search}_result"]
            if result == "results":
                assert text is None, (cell, search)
            elif result.startswith("RuntimeError"):
                # Not a search failure: the generic sentence, as before.
                assert text == baseline[cell][f"{search}_text"], (cell, search)
            else:
                assert text != _OLD_GENERIC_TEXT, (cell, search)
                assert text.startswith("web_search "), (cell, search)
