"""The Models page names the provider rung for its own numbers and flags (7.83.0).

The record's context window and two uncached prices hold either the provider's
own number or models.dev's discovery-time fill, which records no rung, so the
page has said "provider /models or models.dev" for them. Where the provider's
own row stated the very value the record holds, it now says "provider /models"
and the tier the record was found at. The reasoning row reads the provider's
thinking flag exactly as routing does, and tool support takes the row's own
capability word when it publishes no parameter list.
"""

from my_claude_code.api.model_admin import capability_payload
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.core.model_ids import ResolutionTier

NOVITA = ProviderModelDeclaration(
    context_length=1048576,
    max_output_tokens=131072,
    input_price=0.15,
    output_price=0.5,
    reasoning=True,
    tool_calls=True,
)


def _novita_record() -> ProviderModelInfo:
    return ProviderModelInfo(
        "zai-org/glm-5.3-flash",
        supports_thinking=True,
        context_length=1048576,
        input_price=0.15,
        output_price=0.5,
        max_output_tokens=131072,
        declared=NOVITA,
    )


def test_the_providers_own_numbers_name_the_provider_rung() -> None:
    payload = capability_payload("novita", "zai-org/glm-5.3-flash", _novita_record())

    for field, value in (
        ("context_length", 1048576),
        ("input_price", 0.15),
        ("output_price", 0.5),
        ("max_output_tokens", 131072),
    ):
        row = payload[field]
        assert row["value"] == value, field
        assert row["source"] == "provider", field
        assert row["source_label"] == "provider /models", field
        assert row["tier"] == 1, field
        assert row["tier_label"] == "provider /models, exact id", field
    # The context row keeps its explanatory note, exactly as before.
    assert payload["context_length"]["note"]
    assert payload["supports_tool_calls"]["value"] is True
    assert payload["supports_tool_calls"]["source"] == "provider"
    assert payload["reasoning"]["can_reason"]["value"] is True
    assert payload["reasoning"]["can_reason"]["source"] == "provider"


def test_a_tag_stripped_record_reports_tier_two() -> None:
    payload = capability_payload(
        "novita",
        "zai-org/glm-5.3-flash:free",
        _novita_record(),
        provider_tier=ResolutionTier.PROVIDER_TAG_STRIPPED,
    )
    assert payload["input_price"]["tier"] == 2
    assert payload["context_length"]["tier"] == 2


def test_a_value_the_row_did_not_state_keeps_its_old_label() -> None:
    """A record whose number came from models.dev's fill says so, as before."""

    filled_by_models_dev = ProviderModelInfo(
        "zai-org/glm-5.3-flash",
        context_length=200000,
        input_price=9.0,
        declared=ProviderModelDeclaration(model_type="chat"),
    )
    payload = capability_payload(
        "novita", "zai-org/glm-5.3-flash", filled_by_models_dev
    )

    for field in ("context_length", "input_price"):
        assert payload[field]["source"] == "provider_or_models_dev", field
        assert payload[field]["tier"] is None, field
    # And the same numbers with no declaration at all read identically.
    no_declaration = ProviderModelInfo(
        "zai-org/glm-5.3-flash", context_length=200000, input_price=9.0
    )
    bare = capability_payload("novita", "zai-org/glm-5.3-flash", no_declaration)
    for field in ("context_length", "input_price", "output_price"):
        assert bare[field] == payload[field], field


def test_the_reasoning_row_reads_the_flag_routing_reads() -> None:
    """``supports_thinking`` with no reasoning block is the provider's answer."""

    flag_only = ProviderModelInfo("m", supports_thinking=False)
    payload = capability_payload("open_router", "m", flag_only)
    assert payload["reasoning"]["can_reason"]["value"] is False
    assert payload["reasoning"]["can_reason"]["source"] == "provider"

    block_without_answer = ProviderModelInfo(
        "m",
        supports_thinking=True,
        reasoning_capability=ModelReasoningCapability(supports_effort_control=True),
    )
    payload = capability_payload("open_router", "m", block_without_answer)
    assert payload["reasoning"]["can_reason"]["value"] is True
    assert payload["reasoning"]["supports_effort_control"]["value"] is True


def test_a_published_parameter_list_still_answers_tool_support_first() -> None:
    record = ProviderModelInfo(
        "m",
        supported_parameters=frozenset({"max_tokens"}),
        declared=ProviderModelDeclaration(tool_calls=True),
    )
    payload = capability_payload("vercel", "m", record)
    assert payload["supports_tool_calls"]["value"] is False
