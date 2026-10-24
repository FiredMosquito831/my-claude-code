"""7.109.0: the price ceiling one InferHub attempt sends, and every refusal of it."""

from typing import Any

import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.marketplace import (
    MAX_REFUSAL_BYTES,
    check_request_cost,
    market_snapshot_cache,
    marketplace_choice,
    no_offer_message,
)
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    parse_profile,
    save_marketplace_profiles,
)
from my_claude_code.config.marketplaces import INFERHUB_MARKETPLACE, snapshot_key
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)

DIALECT = INFERHUB_MARKETPLACE
OPUS = "cc/claude-opus-4-6"
ALIAS = "claude-opus-4-6"

#: The research's snapshot numbers for cc/claude-opus-4-6 (RES §1): official
#: $5 / $25, cheapest level $0.75 / $3.75 with 38 accounts, dearest $2.50 /
#: $12.50 with 3; 52 accounts over 7 levels.
OPUS_LEVELS_IN = [
    [0.75, 38],
    [1.0, 4],
    [1.25, 3],
    [1.5, 2],
    [2.0, 1],
    [2.25, 1],
    [2.5, 3],
]
OPUS_LEVELS_OUT = [
    [3.75, 38],
    [5.0, 4],
    [6.25, 3],
    [7.5, 2],
    [10.0, 1],
    [11.25, 1],
    [12.5, 3],
]


def _snapshot(models: dict[str, Any]) -> None:
    assert market_snapshot_cache().write(
        DIALECT.snapshot_entry,
        key=snapshot_key(DIALECT),
        payload={"fetched_at": "2026-10-24T01:07:35Z", "models": models},
        computed_at=0.0,
    )


def _opus_snapshot() -> None:
    _snapshot(
        {
            OPUS: {
                "official": [5.0, 25.0],
                "levels_in": OPUS_LEVELS_IN,
                "levels_out": OPUS_LEVELS_OUT,
                "min": [0.75, 3.75],
            },
            # An alias row carries no pricing at all (RES §3).
            ALIAS: {"min": [0.5, 2.5]},
        }
    )


def _profiles(**profiles: dict[str, Any]) -> None:
    parsed = {}
    for name, raw in profiles.items():
        profile, problem = parse_profile(name, raw)
        assert profile is not None, problem
        parsed[name] = profile
    save_marketplace_profiles(MarketplaceProfiles(profiles=parsed))


def _rows(provider: dict[str, Any] | None = None, **models: dict[str, Any]) -> None:
    document: dict[str, Any] = {"providers": {}, "models": {}}
    if provider is not None:
        document["providers"]["inferhub"] = provider
    for ref, row in models.items():
        document["models"][ref] = row
    save_model_overrides(ModelParameterOverrides.from_document(document))


def _choice(model: str = OPUS) -> Any:
    return marketplace_choice(DIALECT, "inferhub", model)


def test_no_dialect_reads_nothing_and_sends_nothing() -> None:
    _profiles(cap={"kind": "pct_of_official", "percent": 10})
    _rows({"marketplace_profile": "cap"})
    assert marketplace_choice(None, "inferhub", OPUS) is None


def test_no_row_sends_nothing() -> None:
    _opus_snapshot()
    assert _choice() is None


def test_a_null_row_is_the_marketplace_deciding() -> None:
    _opus_snapshot()
    _profiles(cap={"kind": "pct_of_official", "percent": 10})
    _rows(
        {"marketplace_profile": "cap"},
        **{f"inferhub/{OPUS}": {"marketplace_profile": None}},
    )
    assert _choice() is None


def test_inferhub_decides_sends_nothing() -> None:
    _profiles(free={"kind": "inferhub_decides"})
    _rows({"marketplace_profile": "free"})
    assert _choice() is None


def test_pct_of_official_converts_with_the_models_own_official_price() -> None:
    _opus_snapshot()
    _profiles(cap={"kind": "pct_of_official", "percent": 20})
    _rows({"marketplace_profile": "cap"})
    choice = _choice()
    assert choice is not None
    assert dict(choice.headers) == {"x-max-input-price": "1", "x-max-output-price": "5"}
    assert choice.origin == "provider_override"
    assert choice.wire_note()["marketplace"]["derived_from"].startswith(
        "20 % of official $5 / $25"
    )


def test_the_model_row_beats_the_provider_row() -> None:
    _opus_snapshot()
    _profiles(
        cap={"kind": "pct_of_official", "percent": 20},
        dollars={"kind": "max_price", "max_input_usd": 0.9, "max_output_usd": 4.5},
    )
    _rows(
        {"marketplace_profile": "cap"},
        **{f"inferhub/{OPUS}": {"marketplace_profile": "dollars"}},
    )
    choice = _choice()
    assert choice is not None and choice.origin == "model_override"
    assert dict(choice.headers) == {
        "x-max-input-price": "0.9",
        "x-max-output-price": "4.5",
    }


def test_an_empty_side_is_not_capped_and_never_zero() -> None:
    _opus_snapshot()
    _profiles(out_only={"kind": "max_price", "max_output_usd": 4})
    _rows({"marketplace_profile": "out_only"})
    choice = _choice()
    assert choice is not None
    assert dict(choice.headers) == {"x-max-output-price": "4"}


def test_cheapest_level_now_follows_the_snapshot() -> None:
    _opus_snapshot()
    _profiles(follow={"kind": "cheapest_level_now"})
    _rows({"marketplace_profile": "follow"})
    choice = _choice()
    assert choice is not None
    assert dict(choice.headers) == {
        "x-max-input-price": "0.75",
        "x-max-output-price": "3.75",
    }
    assert "lowest level" in choice.derived_from


def test_cheapest_level_with_depth_counts_accounts_at_or_below() -> None:
    _opus_snapshot()
    _profiles(deep={"kind": "cheapest_level_min_accounts", "min_accounts": 44})
    _rows({"marketplace_profile": "deep"})
    choice = _choice()
    # 38 at 0.75, 42 at <= 1.0, 45 at <= 1.25: the first level reaching 44.
    assert choice is not None
    assert dict(choice.headers) == {
        "x-max-input-price": "1.25",
        "x-max-output-price": "6.25",
    }


def test_a_cap_clamps_a_derived_ceiling_and_says_so() -> None:
    _opus_snapshot()
    _profiles(
        deep={
            "kind": "cheapest_level_min_accounts",
            "min_accounts": 44,
            "cap_percent": 22,
        }
    )
    _rows({"marketplace_profile": "deep"})
    choice = _choice()
    assert choice is not None
    # 22 % of $5 / $25 = $1.10 / $5.50 is below the $1.25 / $6.25 level.
    assert dict(choice.headers) == {
        "x-max-input-price": "1.1",
        "x-max-output-price": "5.5",
    }
    assert "capped at $1.1 / $5.5" in choice.derived_from


def test_exclusion_rides_its_own_header() -> None:
    _opus_snapshot()
    _profiles(
        cap={
            "kind": "pct_of_official",
            "percent": 20,
            "exclude_upstreams": ["kiro", "CB"],
        }
    )
    _rows({"marketplace_profile": "cap"})
    choice = _choice()
    assert choice is not None
    assert list(choice.headers) == [
        "x-max-input-price",
        "x-max-output-price",
        "x-ignore-upstream",
    ]
    assert choice.headers["x-ignore-upstream"] == "kiro,cb"


def _refusal(model: str = OPUS) -> str:
    with pytest.raises(InvalidRequestError) as raised:
        _choice(model)
    message = raised.value.message
    assert len(message.encode("utf-8")) <= MAX_REFUSAL_BYTES
    return message


def test_a_row_naming_a_deleted_profile_refuses() -> None:
    _profiles(cap={"kind": "pct_of_official", "percent": 20})
    _rows({"marketplace_profile": "gone"})
    assert "profile 'gone' not found" in _refusal()


def test_pct_of_official_with_no_official_price_refuses() -> None:
    _opus_snapshot()
    _profiles(cap={"kind": "pct_of_official", "percent": 20})
    _rows({"marketplace_profile": "cap"})
    assert "No official InferHub price is known" in _refusal(ALIAS)


def test_a_derived_kind_with_no_snapshot_refuses() -> None:
    _profiles(follow={"kind": "cheapest_level_now"})
    _rows({"marketplace_profile": "follow"})
    assert "cannot follow the market" in _refusal()


def test_excluding_the_models_own_upstream_refuses() -> None:
    _opus_snapshot()
    _profiles(
        cap={"kind": "pct_of_official", "percent": 20, "exclude_upstreams": ["cc"]}
    )
    _rows({"marketplace_profile": "cap"})
    assert "excludes the upstream 'cc'" in _refusal()


def test_a_ceiling_under_the_cheapest_level_refuses_before_sending() -> None:
    _opus_snapshot()
    _profiles(tight={"kind": "pct_of_official", "percent": 10})
    _rows({"marketplace_profile": "tight"})
    message = _refusal()
    assert message.startswith(
        "No InferHub offer is under your ceiling for cc/claude-opus-4-6"
    )
    assert "$0.5 / $2.5 per Mtok" in message
    assert "cheapest now $0.75 / $3.75" in message


def test_the_per_request_limit_refuses_a_worst_case_above_it() -> None:
    _opus_snapshot()
    _profiles(
        tight={
            "kind": "max_price",
            "max_input_usd": 1,
            "max_output_usd": 5,
            "max_usd_per_request": 0.05,
        }
    )
    _rows({"marketplace_profile": "tight"})
    choice = _choice()
    assert choice is not None
    # 10,000 in at $1 + 4,000 out at $5 per Mtok = $0.03: allowed.
    check_request_cost(choice, model_id=OPUS, input_tokens=10_000, max_tokens=4_000)
    with pytest.raises(InvalidRequestError) as raised:
        # 10,000 in + 10,000 out = $0.06.
        check_request_cost(
            choice, model_id=OPUS, input_tokens=10_000, max_tokens=10_000
        )
    assert "could cost up to $0.06" in raised.value.message
    assert len(raised.value.message.encode("utf-8")) <= MAX_REFUSAL_BYTES


def test_no_offer_message_names_both_cases_shortly() -> None:
    mine = no_offer_message(DIALECT, model_id=OPUS, choice=None, minimums=(0.75, 3.75))
    assert "the limit set on your InferHub account" in mine
    assert "$0.75 / $3.75" in mine
    assert len(mine.encode("utf-8")) <= MAX_REFUSAL_BYTES
