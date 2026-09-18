"""The option list the Models page draws its two preference controls from.

Every option is derived from the row's own resolved capability and its host's
dialect -- the same two objects gating intersects -- so these are unit tests
of that derivation and need no server. The catalogue publishes genuinely
heterogeneous vocabularies (115 models spell five rungs, 76 spell three, 271
hosts parse no effort field at all), and a fixed six-option select would be
wrong on most rows; that is what is being guarded.
"""

from my_claude_code.api.model_admin import (
    MANDATORY_OFF_NOTE,
    MAX_WIRE_NOTE,
    NO_EFFORT_FIELD_NOTE,
    NO_REASONING_NOTE,
    TOGGLE_ONLY_NOTE,
    UNVERIFIED_RUNG_NOTE,
    merged_override_row,
    model_preferences_payload,
    provider_preferences_payload,
    reasoning_preference_options,
    with_override_row,
)
from my_claude_code.config.model_overrides import (
    MAX_OUTPUT_TOKENS_OVERRIDE,
    REASONING_PREFERENCE_OVERRIDE,
    ModelParameterOverrides,
)


def sourced(value):
    return {"value": value}


def capability(**fields):
    base = {
        "can_reason": True,
        "mandatory": False,
        "supported_efforts": ["high", "low", "medium"],
        "supports_effort_control": True,
        "supports_toggle_control": True,
        "supports_budget_control": False,
        "default_enabled": None,
    }
    base.update(fields)
    return {name: sourced(value) for name, value in base.items()}


def dialect(**fields):
    base = {
        "known": True,
        "effort_values": ["high", "low", "medium"],
        "adaptive": False,
        "toggle": True,
        "budget": False,
        "off": True,
    }
    base.update(fields)
    return base


def values(options):
    return [entry["value"] for entry in options]


def by_value(options, value):
    return next(entry for entry in options if entry["value"] == value)


def test_the_option_list_is_the_capability_and_dialect_intersection():
    options = reasoning_preference_options(capability(), dialect())

    assert values(options) == ["client", "off", "adaptive", "low", "medium", "high"]


def test_a_rung_the_model_does_not_spell_is_not_offered_at_all():
    """Offering it would invite ``nearest_effort`` to clamp UP, which surprises
    everyone who tries it once (WORKING-NOTES 54)."""

    options = reasoning_preference_options(
        capability(supported_efforts=["high", "xhigh"]),
        dialect(effort_values=["high", "xhigh"]),
    )

    assert values(options) == ["client", "off", "adaptive", "high", "xhigh"]


def test_off_is_unavailable_on_a_mandatory_model_with_a_stated_reason():
    options = reasoning_preference_options(capability(mandatory=True), dialect())

    assert by_value(options, "off")["available"] is False
    assert by_value(options, "off")["reason"] == MANDATORY_OFF_NOTE


def test_an_unknown_capability_offers_every_rung_marked_unverified():
    """Unknown never adds a restriction -- the pipeline's own rule."""

    options = reasoning_preference_options(
        capability(supported_efforts=None), dialect(effort_values=None)
    )

    assert values(options) == [
        "client",
        "off",
        "adaptive",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert UNVERIFIED_RUNG_NOTE in by_value(options, "low")["reason"]
    assert all(entry["available"] for entry in options if entry["value"] != "adaptive")


def test_a_no_reasoning_model_offers_no_preference():
    options = reasoning_preference_options(capability(can_reason=False), dialect())

    assert values(options) == ["client"]
    assert options[0]["available"] is False
    assert options[0]["reason"] == NO_REASONING_NOTE


def test_a_host_that_parses_no_effort_field_says_a_level_has_no_effect():
    """271 of 1,191 models sit behind one; a rung there sends nothing."""

    options = reasoning_preference_options(capability(), dialect(effort_values=None))

    assert NO_EFFORT_FIELD_NOTE in by_value(options, "high")["reason"]


def test_a_toggle_only_model_still_offers_rungs_and_says_what_they_do():
    options = reasoning_preference_options(
        capability(supports_effort_control=False, supports_toggle_control=True),
        dialect(),
    )

    assert by_value(options, "high")["available"] is True
    assert TOGGLE_ONLY_NOTE in by_value(options, "high")["reason"]


def test_max_carries_a_wire_note_about_the_hosts_own_top_rung():
    options = reasoning_preference_options(
        capability(supported_efforts=["high", "low", "max"]),
        dialect(effort_values=["high", "low", "max"]),
    )

    assert by_value(options, "max")["reason"] == MAX_WIRE_NOTE


def test_max_names_the_hosts_own_word_when_the_host_published_one():
    options = reasoning_preference_options(
        capability(supported_efforts=["high", "low", "max"]),
        dialect(effort_values=["high", "low", "max"]),
        wire_word_for_max="ultra",
    )

    assert by_value(options, "max")["reason"] == 'max is sent as "ultra" on this host'


def test_a_host_without_an_adaptive_channel_greys_adaptive():
    options = reasoning_preference_options(capability(), dialect(adaptive=False))

    assert by_value(options, "adaptive")["available"] is False


def test_the_output_control_carries_the_limit_and_its_ladder_tier():
    payload = model_preferences_payload(
        "open_router",
        {
            "reasoning": capability(),
            "reasoning_dialect": dialect(),
            "max_output_tokens": {
                "value": 128_000,
                "source_label": "models.dev",
                "tier_label": "models.dev bucket, exact id",
            },
        },
        {},
    )[MAX_OUTPUT_TOKENS_OVERRIDE]

    assert payload["limit"] == 128_000
    assert payload["limit_tier_label"] == "models.dev bucket, exact id"
    assert payload["state"] == "inherit"


def test_a_model_with_no_published_limit_still_offers_the_control():
    """116 models publish none; the operator's number becomes the limit."""

    payload = model_preferences_payload(
        "open_router",
        {
            "reasoning": capability(),
            "reasoning_dialect": dialect(),
            "max_output_tokens": {"value": None},
        },
        {},
    )[MAX_OUTPUT_TOKENS_OVERRIDE]

    assert payload["limit"] is None
    assert "becomes the limit" in payload["note"]


def test_the_three_states_survive_into_the_payload():
    capabilities = {
        "reasoning": capability(),
        "reasoning_dialect": dialect(),
        "max_output_tokens": {"value": 128_000},
    }

    absent = model_preferences_payload("p", capabilities, {})
    forced = model_preferences_payload(
        "p", capabilities, {REASONING_PREFERENCE_OVERRIDE: "high"}
    )
    unset = model_preferences_payload(
        "p", capabilities, {REASONING_PREFERENCE_OVERRIDE: None}
    )

    assert absent[REASONING_PREFERENCE_OVERRIDE]["state"] == "inherit"
    assert forced[REASONING_PREFERENCE_OVERRIDE]["state"] == "value"
    assert forced[REASONING_PREFERENCE_OVERRIDE]["value"] == "high"
    assert unset[REASONING_PREFERENCE_OVERRIDE]["state"] == "unset"


def test_the_provider_control_offers_the_whole_vocabulary_and_says_why():
    payload = provider_preferences_payload({})[REASONING_PREFERENCE_OVERRIDE]

    assert values(payload["options"]) == [
        "off",
        "client",
        "adaptive",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert "clamps this" in payload["note"]


def test_the_overrides_route_saves_a_reasoning_preference():
    """The editor's submission path is key-agnostic, but only for keys it
    admits -- and it admitted nine before this release."""

    row = merged_override_row({}, {REASONING_PREFERENCE_OVERRIDE: "high"})

    assert row == {REASONING_PREFERENCE_OVERRIDE: "high"}


def test_the_overrides_route_saves_a_max_output_tokens_preference():
    row = merged_override_row({}, {MAX_OUTPUT_TOKENS_OVERRIDE: 4096})

    assert row == {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}


def test_the_inherit_sentinel_clears_a_preference():
    row = merged_override_row(
        {REASONING_PREFERENCE_OVERRIDE: "high"},
        {REASONING_PREFERENCE_OVERRIDE: "inherit"},
    )

    assert row == {}


def test_the_editor_still_refuses_response_surface():
    """A strict subset of the non-body set, not the whole of it: the surface
    machinery owns that key and it has no cell in this grid."""

    assert merged_override_row({}, {"response_surface": "responses"}) == {}


def test_a_preference_row_round_trips_through_the_page_payload():
    table = with_override_row(
        ModelParameterOverrides(),
        scope="model",
        key="open_router/z-ai/glm-5",
        updates={
            REASONING_PREFERENCE_OVERRIDE: "max",
            MAX_OUTPUT_TOKENS_OVERRIDE: 32_768,
        },
    )
    reread = ModelParameterOverrides.from_document(table.as_document())

    assert reread.reasoning_preference("open_router", "open_router/z-ai/glm-5") == "max"
    assert reread.max_output_tokens("open_router", "open_router/z-ai/glm-5") == 32_768
