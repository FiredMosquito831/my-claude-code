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
from tests.contracts.go_reasoning_expectations import corrections
from tests.contracts.marketplace_equality_corpus import compact, record_digest, snapshot

BASELINE_PATH = Path(__file__).with_name("marketplace_equality_baseline.json")


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


@pytest.mark.parametrize("scenario", ["nothing_set", "profile_for_inferhub_only"])
def test_every_recorded_call_is_byte_identical(scenario: str) -> None:
    if scenario == "profile_for_inferhub_only":
        _assign_a_profile_to_inferhub()
    base = _baseline()
    records = snapshot()
    now = compact(records)
    assert now["count"] == base["count"] + 1 + 432  # + chat/inferhub
    assert set(now["groups"]) - set(base["groups"]) == {"chat/inferhub"}
    expected = corrections("market")
    changed = {f"chat/opencode_go/{key}" for key in expected}
    assert set(_moved(now, base)) == changed
    for index, key in enumerate(base["keys"]):
        entry = expected.get(key)
        if entry is None:
            continue
        assert (
            record_digest(entry["before"])
            == base["groups"]["chat/opencode_go"]["calls"][index]
        )
        assert records[f"chat/opencode_go/{key}"] == entry["after"], key
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
