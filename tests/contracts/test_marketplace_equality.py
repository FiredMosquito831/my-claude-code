"""E1 (7.109.0): the marketplace moves no byte and no header of any other call.

The user's binding rule for InferHub (decisions of 2026-10-24 ~20:25):
"it shouldn't change break or alter any of my existing infra or algorithms --
it should be an addendum to them". ``marketplace_equality_baseline.json`` was
written by ``marketplace_equality_corpus.py`` run with the v7.108.0 code
(b0a7b1bd), before the marketplace existed: for each of the 432 requests of
``test_anthropic_foreign_thinking_equality``, every Chat host's SDK call (body
and whole call, ``extra_headers`` included, through the real
``preflight_stream`` and ``stream_response``) and the Messages and Responses
doors' HTTP requests (body bytes and every header MCC set) of the three hosts
that declare them.

This release must reproduce every one of those records:

* (a) with nothing set at all;
* (b) with a profile store, and InferHub's provider row and a model row
  naming a profile -- a profile assigned to *another* provider than every
  host recorded -- so the marketplace is configured and still moves nothing
  outside InferHub.

And InferHub itself, with nothing set, makes B.AI's calls exactly: the
generic profile, on its Chat door, for the same model id.

7.113.2's approved Go reasoning correction moves only the 264 Go Chat calls
recorded separately before production edits. Their old and corrected records
are both checked; the original marketplace baseline is never regenerated.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    parse_profile,
    save_marketplace_profiles,
)
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)
from tests.contracts.adaptive_expectations import moved as adaptive_moves
from tests.contracts.go_reasoning_expectations import corrections
from tests.contracts.marketplace_equality_corpus import compact, record_digest, snapshot
from tests.support.parallel_tool_calls_pin import (
    pin_parallel_tool_calls_off,
    unpin_parallel_tool_calls,
)

BASELINE_PATH = Path(__file__).with_name("marketplace_equality_baseline.json")


@pytest.fixture(autouse=True)
def _pre_8_15_0_responses_bodies(monkeypatch: pytest.MonkeyPatch):
    """Every record here was captured from a sender that sent ``false``.

    8.15.0 ships ``RESPONSES_PARALLEL_TOOL_CALLS`` on, which moves one field in
    every ``responses/*`` record -- and this file's whole claim is that the
    marketplace moved no byte of any *other* call. The pin keeps that claim
    exact and keeps the recorded move set at the two it names;
    ``tests/providers/test_responses_parallel_tool_calls.py`` proves the one
    cell 8.15.0 does move, on both doors.
    """

    pin_parallel_tool_calls_off(monkeypatch)
    try:
        yield
    finally:
        unpin_parallel_tool_calls()


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _assign_a_profile_to_inferhub() -> None:
    profile, problem = parse_profile(
        "cap", {"kind": "max_price", "max_input_usd": 1, "max_output_usd": 5}
    )
    assert profile is not None, problem
    save_marketplace_profiles(MarketplaceProfiles(profiles={"cap": profile}))
    save_model_overrides(
        ModelParameterOverrides.from_document(
            {
                "providers": {"inferhub": {"marketplace_profile": "cap"}},
                "models": {
                    "inferhub/cc/claude-opus-4-6": {"marketplace_profile": "cap"}
                },
            }
        )
    )


def _moved(now: dict[str, Any], base: dict[str, Any]) -> list[str]:
    assert now["keys"] == base["keys"]
    moved: list[str] = []
    for name, group in base["groups"].items():
        other = now["groups"][name]
        if group.get("extra") != other.get("extra"):
            moved.append(f"{name}/extra")
        for index, (was, is_) in enumerate(
            zip(group.get("calls", []), other.get("calls", []), strict=True)
        ):
            if was != is_:
                moved.append(f"{name}/{base['keys'][index]}")
    return moved


#: Maintenance hook, the same shape ``test_metadata_ladder_equality`` uses:
#: point it at a path and the run writes the adaptive move record there and
#: skips, instead of asserting. It exists because the record has to be
#: produced under THIS test's own fixtures -- an out-of-process run of the
#: corpus sees a different environment for the OpenCode hosts and records
#: moves that never happen here (measured: 3,944 recorded vs 3,776 real).
ADAPTIVE_WRITE = os.environ.get("MCC_ADAPTIVE_WRITE", "")


@pytest.mark.parametrize("scenario", ["nothing_set", "profile_for_inferhub_only"])
def test_every_recorded_call_is_byte_identical(scenario: str) -> None:
    if scenario == "profile_for_inferhub_only":
        _assign_a_profile_to_inferhub()
    base = _baseline()
    records = snapshot()
    now = compact(records)
    if ADAPTIVE_WRITE and scenario == "nothing_set":
        written: dict[str, dict[str, str]] = {}
        for name, group in base["groups"].items():
            if not name.startswith("chat/"):
                continue
            host = name.split("/", 1)[1]
            other = now["groups"][name]
            for index, was in enumerate(group.get("calls", [])):
                is_ = other["calls"][index]
                if was != is_:
                    written[f"{host}/{base['keys'][index]}"] = {
                        "before": was,
                        "after": is_,
                    }
        Path(ADAPTIVE_WRITE).write_text(
            json.dumps(written, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip(f"wrote {len(written)} adaptive moves")
    assert now["count"] == base["count"] + 1 + 432  # + chat/inferhub
    assert set(now["groups"]) - set(base["groups"]) == {"chat/inferhub"}
    expected = corrections("market")
    # 8.13.0 makes the Chat door express an ADAPTIVE policy instead of
    # silently sending nothing, so every ``*/adaptive/*`` call on every Chat
    # host moves. Those are recorded before/after in
    # ``adaptive_every_door_expectations.json`` -- the historical baseline is
    # not regenerated -- and nothing else may move.
    adaptive = adaptive_moves("marketplace")
    changed = {f"chat/opencode_go/{key}" for key in expected} | {
        f"chat/{name}" for name in adaptive
    }
    assert set(_moved(now, base)) == changed
    for index, key in enumerate(base["keys"]):
        entry = expected.get(key)
        if entry is None:
            continue
        assert (
            record_digest(entry["before"])
            == base["groups"]["chat/opencode_go"]["calls"][index]
        )
        if f"opencode_go/{key}" in adaptive:
            # An ADAPTIVE call 7.113.2 corrected is moved again by 8.13.0, so
            # the adaptive record owns its answer. The 7.113.2 ``before`` is
            # still asserted above -- it is the committed baseline's digest --
            # and the new answer is asserted in the loop below.
            continue
        assert records[f"chat/opencode_go/{key}"] == entry["after"], key
    for name, entry in adaptive.items():
        host, _, key = name.partition("/")
        index = base["keys"].index(key)
        # The recorded ``before`` must be the committed baseline's own digest,
        # so a baseline that silently drifted fails here instead of passing.
        assert entry["before"] == base["groups"][f"chat/{host}"]["calls"][index], name
        assert record_digest(records[f"chat/{host}/{key}"]) == entry["after"], name
        assert entry["after"] != entry["before"], name
    assert adaptive, "the recorded moves would prove nothing if empty"
    if scenario == "nothing_set":
        # InferHub unset is B.AI's call, byte for byte, on every request.
        bai = {k: v for k, v in records.items() if k.startswith("chat/bai/")}
        for key, value in bai.items():
            assert records[key.replace("chat/bai/", "chat/inferhub/", 1)] == value, key


def test_the_baseline_covers_every_host_and_every_request() -> None:
    base = _baseline()
    assert len(base["keys"]) == 432
    chat = [name for name in base["groups"] if name.startswith("chat/")]
    # Every v7.108.0 profile (42) and the 11 special Chat-family providers.
    assert len(chat) == 53
    assert {name for name in base["groups"] if not name.startswith("chat/")} == {
        f"{door}/{host}"
        for door in ("messages", "responses")
        for host in ("opencode", "opencode_go", "corpus_custom")
    }
    for group in base["groups"].values():
        assert len(group["calls"]) == 432
