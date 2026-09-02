"""What MCC states to Crush, and what it has to guess.

Crush is the harness where "unknown stays unknown" costs the most: ten of its
per-model fields are *required* by its own published schema, so there is no key
to omit and an unknown has to become Crush's own value. The point of these
tests is that every such substitution is visible -- in ``_mcc_defaulted``, on
the launcher's stderr and on the dashboard card -- and that a resolved value is
never overwritten by one.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.crush import (
    CLI_DOCUMENTED_DEFAULTS,
    CRUSH_EFFORTS,
    PROVIDER_ID,
    build_crush_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import (
    CRUSH_API_KEY_ENV,
    CRUSH_BASE_URL_SENTINEL,
)
from my_claude_code.core.reasoning import ReasoningEffort

#: Crush's own required per-model fields, from ``crush schema``.
REQUIRED_FIELDS = {
    "id",
    "name",
    "cost_per_1m_in",
    "cost_per_1m_out",
    "cost_per_1m_in_cached",
    "cost_per_1m_out_cached",
    "context_window",
    "default_max_tokens",
    "can_reason",
    "supports_attachments",
}


def _model(
    *,
    gateway_id: str = "anthropic/openrouter/sonnet",
    provider_model_ref: str = "openrouter/sonnet",
    display_name: str = "Claude Sonnet 4.5",
    force_no_thinking: bool = False,
    context_length: int | None = None,
    max_output_tokens: int | None = None,
    supports_vision: bool | None = None,
    supports_tool_calls: bool | None = None,
    reasoning: ModelReasoningCapability | None = None,
    input_price: float | None = None,
    output_price: float | None = None,
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=provider_model_ref,
        display_name=display_name,
        force_no_thinking=force_no_thinking,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        supports_vision=supports_vision,
        supports_tool_calls=supports_tool_calls,
        reasoning=reasoning,
        input_price=input_price,
        output_price=output_price,
    )


def _entry(document: dict[str, Any]) -> dict[str, Any]:
    entries = model_entries("crush", document)
    assert len(entries) == 1
    return entries[0]


def test_the_provider_block_declares_the_one_type_mcc_serves() -> None:
    document, _ = build_crush_catalogue([_model(context_length=200_000)])

    provider = document["providers"][PROVIDER_ID]
    assert provider["type"] == "anthropic"
    assert provider["base_url"] == CRUSH_BASE_URL_SENTINEL
    assert provider["api_key"] == f"${CRUSH_API_KEY_ENV}"


def test_discovery_is_off_because_crush_would_ask_the_wrong_route() -> None:
    # Measured against v0.92.0: with discover_models on, Crush issues
    # GET <base_url>/models, which MCC does not serve, and the /v1 base URL
    # that would make it work breaks POST /v1/messages.
    document, _ = build_crush_catalogue([_model()])

    assert document["providers"][PROVIDER_ID]["discover_models"] is False


def test_a_selected_model_is_named_so_a_session_can_start() -> None:
    document, _ = build_crush_catalogue([_model(), _model(gateway_id="anthropic/a/b")])

    assert document["models"]["large"] == {
        "model": "anthropic/openrouter/sonnet",
        "provider": PROVIDER_ID,
    }
    assert document["models"]["small"] == document["models"]["large"]


def test_an_empty_catalogue_names_no_selected_model() -> None:
    document, _ = build_crush_catalogue([])

    assert "models" not in document


def test_every_required_field_is_present_on_every_entry() -> None:
    document, _ = build_crush_catalogue([_model()])

    assert set(_entry(document)) >= REQUIRED_FIELDS


def test_resolved_limits_come_from_the_ladder_not_from_a_default() -> None:
    document, defaulted = build_crush_catalogue(
        [_model(context_length=131_072, max_output_tokens=32_000)]
    )

    entry = _entry(document)
    assert entry["context_window"] == 131_072
    assert entry["default_max_tokens"] == 32_000
    recorded = defaulted.by_model.get("anthropic/openrouter/sonnet", [])
    assert "context_window" not in recorded
    assert "default_max_tokens" not in recorded


def test_unknown_limits_become_crushs_own_numbers_and_are_recorded() -> None:
    document, defaulted = build_crush_catalogue([_model()])

    entry = _entry(document)
    assert entry["context_window"] == CLI_DOCUMENTED_DEFAULTS["context_window"]
    # Measured: with 0 here Crush's own agent request went out with 4096, so
    # writing 0 would only move the guess somewhere less visible.
    assert entry["default_max_tokens"] == CLI_DOCUMENTED_DEFAULTS["default_max_tokens"]
    recorded = defaulted.by_model["anthropic/openrouter/sonnet"]
    assert "context_window" in recorded
    assert "default_max_tokens" in recorded


def test_prices_pass_through_and_cache_rates_are_always_crushs() -> None:
    document, defaulted = build_crush_catalogue(
        [_model(input_price=3.0, output_price=15.0)]
    )

    entry = _entry(document)
    assert entry["cost_per_1m_in"] == 3.0
    assert entry["cost_per_1m_out"] == 15.0
    # The ladder resolves no cache rates and deriving one would be invention.
    assert entry["cost_per_1m_in_cached"] == 0.0
    recorded = defaulted.by_model["anthropic/openrouter/sonnet"]
    assert "cost_per_1m_in" not in recorded
    assert "cost_per_1m_in_cached" in recorded
    assert "cost_per_1m_out_cached" in recorded


def test_attachments_follow_vision_and_an_unknown_is_recorded() -> None:
    known, _ = build_crush_catalogue([_model(supports_vision=True)])
    absent, absent_defaulted = build_crush_catalogue([_model(supports_vision=False)])
    unknown, unknown_defaulted = build_crush_catalogue([_model()])

    assert _entry(known)["supports_attachments"] is True
    assert _entry(absent)["supports_attachments"] is False
    assert "supports_attachments" not in absent_defaulted.by_model.get(
        "anthropic/openrouter/sonnet", []
    )
    assert _entry(unknown)["supports_attachments"] is False
    assert (
        "supports_attachments"
        in unknown_defaulted.by_model["anthropic/openrouter/sonnet"]
    )


def test_reasoning_levels_clamp_to_crushs_three_rungs() -> None:
    document, _ = build_crush_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset(
                        {ReasoningEffort.LOW, ReasoningEffort.HIGH}
                    ),
                )
            )
        ]
    )

    entry = _entry(document)
    assert entry["can_reason"] is True
    assert entry["reasoning_levels"] == ["low", "high"]
    assert CRUSH_EFFORTS == ("low", "medium", "high")


def test_a_rung_the_model_does_not_support_never_appears() -> None:
    document, _ = build_crush_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset({ReasoningEffort.LOW}),
                )
            )
        ]
    )

    assert _entry(document)["reasoning_levels"] == ["low"]


def test_a_mandatory_reasoner_starts_on_its_strongest_rung() -> None:
    document, _ = build_crush_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    mandatory=True,
                    supported_efforts=frozenset(
                        {
                            ReasoningEffort.LOW,
                            ReasoningEffort.MEDIUM,
                            ReasoningEffort.HIGH,
                        }
                    ),
                )
            )
        ]
    )

    # There is no "off" to fall back to, so the honest starting point is the
    # strongest rung rather than the middle one.
    assert _entry(document)["default_reasoning_effort"] == "high"


def test_a_model_known_not_to_reason_says_so_without_a_default() -> None:
    document, defaulted = build_crush_catalogue(
        [_model(reasoning=ModelReasoningCapability(can_reason=False))]
    )

    entry = _entry(document)
    assert entry["can_reason"] is False
    assert "reasoning_levels" not in entry
    assert "can_reason" not in defaulted.by_model.get("anthropic/openrouter/sonnet", [])


def test_unknown_reasoning_becomes_crushs_false_and_is_recorded() -> None:
    document, defaulted = build_crush_catalogue([_model()])

    assert _entry(document)["can_reason"] is False
    assert "can_reason" in defaulted.by_model["anthropic/openrouter/sonnet"]


def test_the_no_thinking_variant_states_reasoning_off() -> None:
    document, _ = build_crush_catalogue(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
            )
        ]
    )

    entry = _entry(document)
    assert entry["id"] == "claude-3-freecc-no-thinking/openrouter/sonnet"
    assert entry["can_reason"] is False


def test_the_no_thinking_variant_is_dropped_when_the_normal_one_exists() -> None:
    document, _ = build_crush_catalogue(
        [
            _model(),
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
            ),
        ]
    )

    assert len(model_entries("crush", document)) == 1


def test_defaulted_fields_are_recorded_in_the_file() -> None:
    document, _ = build_crush_catalogue([_model()])

    recorded = document["_mcc_defaulted"]["anthropic/openrouter/sonnet"]
    assert "context_window" in recorded


def test_the_registered_format_round_trips_through_model_entries() -> None:
    document, _ = serialise("crush", [_model(context_length=200_000)])

    entries = model_entries("crush", document)
    assert [entry["id"] for entry in entries] == ["anthropic/openrouter/sonnet"]
