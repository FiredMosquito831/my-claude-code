"""The Model Config rail headings and the router answer to the same names.

The ``mcc/*`` aliases are the ids every coding agent other than Claude
Code puts on the wire, and ``core/tier_refs.py`` owns them. The dashboard shows
them beside each routing rail, which is only useful if the alias shown and the
alias the router resolves are the same fact -- so this pins the join in both
directions:

* every tier's alias reaches the page, keyed by the route setting the rail
  edits; and
* the page carries nothing the tier table did not put there.

``config`` cannot read ``core`` (``tests/contracts/test_import_boundaries.py``
makes it a leaf), which is why the join happens in ``api`` rather than inside
``load_config_response``. A mirror inside ``config`` would be a second list,
and the first thing a second list does is disagree.
"""

import re
from pathlib import Path

from my_claude_code.api.admin_routes import _config_response
from my_claude_code.application.desktop_documents import DEFAULT_MODEL_ID
from my_claude_code.core.tier_refs import (
    DEFAULT_TIER,
    GLOBAL_TIER_SETTINGS,
    TIER_FAMILY_TIERS,
    TIER_ORDER,
    ModelTier,
    tier_alias_by_route_env_var,
    tier_ref,
)

#: ``var fo=[...]`` read out of the installed Claude Desktop 1.52386.0.0
#: ``app.asar``: the closed enum ``anthropicFamilyTier`` is validated against,
#: and the set of ``ANTHROPIC_DEFAULT_<TIER>_MODEL`` names the app exports into
#: its Code sessions.
CLAUDE_FAMILY_TIER_ENUM = frozenset({"sonnet", "opus", "haiku", "fable", "mythos"})

_STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)


def test_every_tier_reaches_the_page_under_the_route_it_names() -> None:
    aliases = tier_alias_by_route_env_var()

    assert aliases == {
        "MODEL_MYTHOS": "mcc/cyber",
        "MODEL_FABLE": "mcc/best",
        "MODEL_OPUS": "mcc/good",
        "MODEL_SONNET": "mcc/medium",
        "MODEL_HAIKU": "mcc/cheap",
        "MODEL_VISION": "mcc/vision",
    }
    for tier in TIER_ORDER:
        assert aliases[GLOBAL_TIER_SETTINGS[tier].env_var] == tier_ref(tier)


def test_the_map_carries_nothing_the_tier_table_did_not_put_there() -> None:
    """The other direction. In particular ``MODEL`` is not a tier.

    ``MODEL`` is the default every unset route falls back to, not a rung on the
    ladder. ``mcc/best`` names the Fable route, and an install that leaves
    ``MODEL_FABLE`` blank reaches ``MODEL`` through the ordinary collapse --
    which is a different sentence from "the Default rail is where mcc/best
    goes", and only one of them stays true when the operator fills Fable in.
    """

    aliases = tier_alias_by_route_env_var()

    assert "MODEL" not in aliases
    assert len(aliases) == len(TIER_ORDER)
    known = {GLOBAL_TIER_SETTINGS[tier].env_var for tier in TIER_ORDER}
    assert set(aliases) == known
    assert set(aliases.values()) == {tier_ref(tier) for tier in TIER_ORDER}


def test_the_config_payload_the_dashboard_fetches_carries_the_map() -> None:
    payload = _config_response()

    assert payload["route_tier_aliases"] == tier_alias_by_route_env_var()
    # And the fields the map keys are still fields the page renders, so a
    # renamed setting cannot leave an alias pointing at nothing.
    keys = {field["key"] for field in payload["fields"]}
    assert set(payload["route_tier_aliases"]) <= keys


def test_the_dashboard_script_holds_no_second_list_of_aliases() -> None:
    """A hard-coded ``mcc/...`` in admin.js is the failure this guards against.

    The page reads the aliases out of the payload it already fetches. A literal
    in the script would be a copy nobody updates, and it would be wrong the
    first time a tier is renamed.
    """

    script = (_STATIC / "admin.js").read_text(encoding="utf-8")
    literals = set(re.findall(r'"mcc/[a-z]+"', script)) | set(
        re.findall(r"'mcc/[a-z]+'", script)
    )
    assert not literals, (
        "the tier aliases must come from the config payload, which reads "
        f"core/tier_refs.py -- found hard-coded {sorted(literals)}"
    )


def test_the_default_tier_is_stated_once_and_is_not_the_first_in_the_order() -> None:
    """Ordering is a display fact; "which alias is the default" is a routing one.

    They were the same line of code -- ``TIER_ORDER[0]`` -- until 7.2.0, so
    putting a tier at the top of the picker would have re-pointed the primary
    entry of all thirteen generated harness catalogues, and the default model
    Configure writes into other applications' settings, at a route nobody has
    filled in. ``mcc/cyber`` leads the picker; ``mcc/best`` is still the
    default.
    """

    assert TIER_ORDER[0] is ModelTier.CYBER
    assert DEFAULT_TIER is ModelTier.BEST
    assert DEFAULT_TIER is not TIER_ORDER[0]
    assert DEFAULT_MODEL_ID == tier_ref(DEFAULT_TIER) == "mcc/best"


def test_every_family_tier_is_one_of_the_apps_own_enum_values() -> None:
    """``anthropicFamilyTier`` is a closed enum in Claude Desktop's own bundle.

    ``var fo=["sonnet","opus","haiku","fable","mythos"]`` at 1.52386.0.0; a
    value outside it is dropped, and the tier's ``ANTHROPIC_DEFAULT_<TIER>_
    MODEL`` pin goes unfilled.
    """

    for tier in TIER_ORDER:
        family, _default = TIER_FAMILY_TIERS[tier]
        assert family in CLAUDE_FAMILY_TIER_ENUM, tier


def test_no_two_tiers_claim_the_same_family_default() -> None:
    """The app warns and picks arbitrarily when two entries share a tier.

    Until 7.2.0 both ``mcc/best`` and ``mcc/good`` were advertised as ``opus``
    -- which fired that warning -- while ``fable`` and ``mythos``, valid tier
    values all along, went unused.
    """

    defaults = [
        family for family, is_default in TIER_FAMILY_TIERS.values() if is_default
    ]

    assert sorted(defaults) == sorted(set(defaults))
    assert set(defaults) == {"mythos", "fable", "opus", "sonnet", "haiku"}
    # And nothing is left un-advertised: every tier names a family.
    assert set(TIER_FAMILY_TIERS) == set(TIER_ORDER)
