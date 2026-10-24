"""7.109.0: the marketplace profile store and the 15th override key."""

import json
from typing import Any

import pytest

from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    current_marketplace_profiles,
    load_marketplace_profiles,
    parse_profile,
    save_marketplace_profiles,
)
from my_claude_code.config.model_overrides import (
    MARKETPLACE_PROFILE_OVERRIDE,
    NON_BODY_OVERRIDE_PARAMETERS,
    ModelParameterOverrides,
    StatedMarketplaceProfile,
    apply_model_parameter_overrides,
)
from my_claude_code.config.paths import marketplace_profiles_path


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ({"kind": "nope"}, "kind must be one of"),
        ({"kind": "inferhub_decides", "percent": 5}, "takes no percent"),
        ({"kind": "pct_of_official"}, "needs percent"),
        ({"kind": "pct_of_official", "percent": 0}, "greater than zero"),
        ({"kind": "pct_of_official", "percent": 101}, "at most 100"),
        ({"kind": "pct_of_official", "percent": True}, "greater than zero"),
        ({"kind": "max_price"}, "needs max_input_usd or max_output_usd"),
        (
            {"kind": "max_price", "max_input_usd": 1, "max_usd_per_request": 1},
            "needs both",
        ),
        ({"kind": "cheapest_level_min_accounts"}, "needs min_accounts"),
        ({"kind": "cheapest_level_min_accounts", "min_accounts": 0}, "at least 1"),
        (
            {"kind": "cheapest_level_now", "cap_percent": 10, "cap_input_usd": 1},
            "not both",
        ),
        ({"kind": "cheapest_level_now", "exclude_upstreams": "cc"}, "list of upstream"),
        (
            {"kind": "cheapest_level_now", "exclude_upstreams": ["c c"]},
            "not an upstream",
        ),
        ({"kind": "max_price", "max_input_usd": 1, "temperature": 1}, "takes no"),
    ],
)
def test_a_profile_that_cannot_be_used_says_why(raw: dict[str, Any], why: str) -> None:
    profile, problem = parse_profile("p", raw)
    assert profile is None
    assert why in problem


def test_a_name_has_one_shape() -> None:
    assert parse_profile("Bad Name", {"kind": "inferhub_decides"})[0] is None
    assert parse_profile("opus-cap_2", {"kind": "inferhub_decides"})[0] is not None


def test_the_store_round_trips_owner_only_and_drops_what_it_cannot_use() -> None:
    path = marketplace_profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "cap": {"kind": "pct_of_official", "percent": 10},
                    "broken": {"kind": "pct_of_official"},
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_marketplace_profiles()
    assert set(loaded.profiles) == {"cap"}
    assert "needs percent" in loaded.problems["broken"]
    save_marketplace_profiles(loaded)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "profiles": {"cap": {"kind": "pct_of_official", "percent": 10}},
    }
    assert current_marketplace_profiles().get("cap") is not None


def test_no_file_is_no_profiles() -> None:
    assert current_marketplace_profiles() == MarketplaceProfiles()


# ------------------------------------------------------- the 15th override key


def test_the_15th_key_is_a_non_body_key() -> None:
    assert MARKETPLACE_PROFILE_OVERRIDE in NON_BODY_OVERRIDE_PARAMETERS
    assert len(NON_BODY_OVERRIDE_PARAMETERS) == 6


def _table(document: dict[str, Any]) -> ModelParameterOverrides:
    return ModelParameterOverrides.from_document(document)


def test_a_name_and_null_are_kept_an_object_and_a_bad_name_are_dropped() -> None:
    table = _table(
        {
            "providers": {"inferhub": {"marketplace_profile": "opus-cap"}},
            "models": {
                "inferhub/cc/a": {"marketplace_profile": None},
                "inferhub/cc/b": {"marketplace_profile": {"kind": "max_price"}},
                "inferhub/cc/c": {"marketplace_profile": "Not A Name"},
            },
        }
    )
    assert table.marketplace_profile_rows("inferhub", "inferhub/cc/a") == (
        StatedMarketplaceProfile(None),
        StatedMarketplaceProfile("opus-cap"),
    )
    assert table.marketplace_profile_rows("inferhub", "inferhub/cc/b") == (
        None,
        StatedMarketplaceProfile("opus-cap"),
    )
    assert table.marketplace_profile_rows("inferhub", "inferhub/cc/c")[0] is None


def test_the_15th_key_never_reaches_a_body() -> None:
    table = _table(
        {
            "providers": {
                "inferhub": {"marketplace_profile": "opus-cap", "top_p": 0.9}
            },
            "models": {"inferhub/cc/a": {"marketplace_profile": None}},
        }
    )
    for model in ("cc/a", "cc/b"):
        # cc/a: its own null row; cc/b: the provider row's name reaches it.
        body: dict[str, Any] = {"model": model}
        applied = apply_model_parameter_overrides(
            body, provider_id="inferhub", model_ref=f"inferhub/{model}", overrides=table
        )
        assert body == {"model": model, "top_p": 0.9}
        assert applied == {"top_p": 0.9}
