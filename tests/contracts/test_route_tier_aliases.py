"""The Model Config rail headings and the router answer to the same names.

The five ``mcc/*`` aliases are the ids every coding agent other than Claude
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
from my_claude_code.core.tier_refs import (
    GLOBAL_TIER_SETTINGS,
    TIER_ORDER,
    tier_alias_by_route_env_var,
    tier_ref,
)

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
