"""What MCC states to Aider, across the two documents Aider reads.

Aider is the only harness that splits its model facts over two files, and the
split is not arbitrary: the metadata JSON is LiteLLM's ``model_cost`` schema
and holds *what the model is*, while the settings YAML is a list of
``ModelSettings`` records and holds *what the model accepts*. The second is
constructed with ``ModelSettings(**entry)``, so an unrecognised key is a
``TypeError`` rather than an ignored line -- which is why the defaulted record
lives only in the first, and why the tests below pin the field names.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import (
    model_entries,
    serialise,
    serialise_sidecar,
)
from my_claude_code.application.catalogues.aider import (
    CHAT_MODE,
    CLI_DOCUMENTED_DEFAULTS,
    LITELLM_PROVIDER,
    build_aider_catalogue,
    build_aider_model_settings,
)
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.application.model_metadata import ModelReasoningCapability


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
    cache_read_price: float | None = None,
    cache_write_price: float | None = None,
    default_parameters: tuple[tuple[str, Any], ...] | None = None,
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
        cache_read_price=cache_read_price,
        cache_write_price=cache_write_price,
        default_parameters=default_parameters,
    )


def test_the_metadata_key_is_the_whole_prefixed_model_ref() -> None:
    # ``Model.get_model_info`` looks the model up by the exact string passed
    # to --model, so a bare gateway id here would never be found.
    document, _ = build_aider_catalogue([_model(context_length=200_000)])

    assert "openai/anthropic/openrouter/sonnet" in document
    entry = document["openai/anthropic/openrouter/sonnet"]
    assert entry["litellm_provider"] == LITELLM_PROVIDER
    assert entry["mode"] == CHAT_MODE


def test_limits_come_from_the_ladder_not_a_placeholder() -> None:
    document, defaulted = build_aider_catalogue(
        [
            _model(
                context_length=32_768,
                max_output_tokens=4_096,
                input_price=1.0,
                output_price=2.0,
                cache_read_price=0.5,
                cache_write_price=3.0,
                supports_vision=True,
            )
        ]
    )

    entry = document["openai/anthropic/openrouter/sonnet"]
    assert entry["max_input_tokens"] == 32_768
    assert entry["max_output_tokens"] == 4_096
    # LiteLLM's older spelling of the same ceiling, written from the same
    # figure so a stale registry row cannot answer for it.
    assert entry["max_tokens"] == 4_096
    assert not defaulted.by_model


def test_prices_are_per_token_not_per_million() -> None:
    # The ladder publishes USD per million; LiteLLM's schema is per token.
    document, _ = build_aider_catalogue(
        [_model(context_length=1, input_price=3.0, output_price=15.0)]
    )

    entry = document["openai/anthropic/openrouter/sonnet"]
    assert entry["input_cost_per_token"] == 3.0 / 1_000_000
    assert entry["output_cost_per_token"] == 15.0 / 1_000_000


def test_optional_unknown_fields_are_omitted_not_zeroed() -> None:
    document, defaulted = build_aider_catalogue([_model()])

    entry = document["openai/anthropic/openrouter/sonnet"]
    for key in (
        "max_input_tokens",
        "max_output_tokens",
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        "supports_vision",
    ):
        assert key not in entry, f"{key} was written for a model nobody published"
    assert defaulted.by_model["openai/anthropic/openrouter/sonnet"] == [
        "max_input_tokens",
        "max_output_tokens",
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        "supports_vision",
    ]


def test_defaulted_fields_are_recorded_in_the_file() -> None:
    document, _ = build_aider_catalogue([_model()])

    assert DEFAULTED_KEY in document
    assert "openai/anthropic/openrouter/sonnet" in document[DEFAULTED_KEY]


def test_the_defaulted_record_is_not_counted_as_a_model() -> None:
    # Aider's metadata file *is* the model map, so the record sits beside the
    # models. It has no ``mode: chat`` and Aider's own listing skips it; the
    # count MCC reports must skip it too.
    document, _ = build_aider_catalogue([_model()])

    assert len(model_entries("aider", document)) == 1


def test_vision_is_stated_only_when_the_ladder_knows() -> None:
    known, _ = build_aider_catalogue([_model(supports_vision=False)])
    assert known["openai/anthropic/openrouter/sonnet"]["supports_vision"] is False

    unknown, defaulted = build_aider_catalogue([_model()])
    assert "supports_vision" not in unknown["openai/anthropic/openrouter/sonnet"]
    assert "supports_vision" in defaulted.by_model["openai/anthropic/openrouter/sonnet"]


def test_the_settings_document_is_a_list_of_named_records() -> None:
    entries = build_aider_model_settings(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True, supports_effort_control=True
                )
            )
        ]
    )

    assert entries == [
        {
            "name": "openai/anthropic/openrouter/sonnet",
            "accepts_settings": ["reasoning_effort"],
        }
    ]


def test_a_model_with_nothing_to_declare_gets_no_settings_record() -> None:
    # ``ModelSettings`` has no "unknown" for its booleans, so writing a record
    # for a model the ladder cannot describe would state a default as a fact.
    assert build_aider_model_settings([_model(context_length=1)]) == []


def test_a_pinned_temperature_turns_the_temperature_setting_off() -> None:
    entries = build_aider_model_settings(
        [_model(default_parameters=(("temperature", 1.0),))]
    )

    assert entries == [
        {"name": "openai/anthropic/openrouter/sonnet", "use_temperature": False}
    ]


def test_budget_control_is_declared_as_thinking_tokens() -> None:
    entries = build_aider_model_settings(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supports_budget_control=True,
                )
            )
        ]
    )

    assert entries[0]["accepts_settings"] == ["reasoning_effort", "thinking_tokens"]


def test_a_no_thinking_variant_accepts_no_reasoning_settings() -> None:
    entries = build_aider_model_settings(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
                reasoning=ModelReasoningCapability(
                    can_reason=True, supports_effort_control=True
                ),
            )
        ]
    )

    assert entries == []


def test_the_settings_document_carries_no_defaulted_block() -> None:
    # ``ModelSettings(**entry)`` raises TypeError on an unknown key, so the
    # record has to stay in the metadata file, which is a plain dict.update.
    entries = build_aider_model_settings([_model()])

    assert all(DEFAULTED_KEY not in entry for entry in entries)


def test_the_serialiser_is_reachable_through_the_registry() -> None:
    document, _ = serialise("aider", [_model(context_length=1)])
    assert "openai/anthropic/openrouter/sonnet" in document
    assert serialise_sidecar("aider", [_model(context_length=1)]) == []


def test_the_documented_defaults_say_what_aider_does_instead() -> None:
    # Every value is None: Aider has no substitute number for any of these,
    # it simply behaves differently, and the docstring beside each says how.
    assert set(CLI_DOCUMENTED_DEFAULTS) == {
        "max_input_tokens",
        "max_output_tokens",
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        "supports_vision",
    }
    assert all(value is None for value in CLI_DOCUMENTED_DEFAULTS.values())
