"""What MCC states to Droid, including the protocol it chose and why.

Droid is the one harness in this batch that reaches MCC over Anthropic
Messages. Its ``customModels[].provider`` accepts ``anthropic`` with an
arbitrary ``baseUrl``, which means the agent talks to the router in the
router's own protocol with no translation in between. The first test pins that
choice so a future edit back to ``generic-chat-completion-api`` has to be
deliberate -- and has to change the base-URL shape with it.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.application.catalogues.droid import (
    AUTH_MODE,
    CLI_DOCUMENTED_DEFAULTS,
    PROVIDER,
    build_droid_catalogue,
    droid_model_id,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import (
    DROID_API_KEY_REFERENCE,
    DROID_BASE_URL_SENTINEL,
)


def _model(
    *,
    gateway_id: str = "anthropic/openrouter/sonnet",
    provider_model_ref: str = "openrouter/sonnet",
    display_name: str = "Claude Sonnet 4.5",
    force_no_thinking: bool = False,
    context_length: int | None = None,
    max_output_tokens: int | None = None,
    supports_vision: bool | None = None,
    reasoning: ModelReasoningCapability | None = None,
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=provider_model_ref,
        display_name=display_name,
        force_no_thinking=force_no_thinking,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        supports_vision=supports_vision,
        reasoning=reasoning,
    )


def _entry(document: dict[str, Any]) -> dict[str, Any]:
    entries = model_entries("droid", document)
    assert len(entries) == 1
    return entries[0]


def test_the_provider_is_anthropic_and_the_base_url_carries_no_v1() -> None:
    # The bundled @anthropic-ai/sdk appends /v1/messages itself, so a
    # sentinel ending in /v1 would produce POST /v1/v1/messages.
    document, _ = build_droid_catalogue([_model(context_length=1)])

    entry = _entry(document)
    assert entry["provider"] == PROVIDER == "anthropic"
    assert entry["baseUrl"] == DROID_BASE_URL_SENTINEL
    assert not DROID_BASE_URL_SENTINEL.endswith("/v1")


def test_the_entry_names_a_variable_not_a_token() -> None:
    document, _ = build_droid_catalogue([_model(context_length=1)])

    entry = _entry(document)
    assert entry["apiKey"] == DROID_API_KEY_REFERENCE
    assert entry["apiKey"].startswith("${")
    # Bearer rather than the SDK's default x-api-key: both reach this proxy,
    # and Bearer is the header every other page of the docs names.
    assert entry["authMode"] == AUTH_MODE == "bearer"


def test_limits_come_from_the_ladder_not_a_placeholder() -> None:
    document, defaulted = build_droid_catalogue(
        [
            _model(
                context_length=262_144,
                max_output_tokens=32_768,
                supports_vision=True,
                reasoning=ModelReasoningCapability(can_reason=True),
            )
        ]
    )

    entry = _entry(document)
    assert entry["maxContextLimit"] == 262_144
    assert entry["maxOutputTokens"] == 32_768
    assert entry["enableThinking"] is True
    assert not defaulted.by_model


def test_optional_unknown_fields_are_omitted_not_zeroed() -> None:
    document, defaulted = build_droid_catalogue([_model()])

    entry = _entry(document)
    for key in (
        "maxContextLimit",
        "maxOutputTokens",
        "enableThinking",
        "noImageSupport",
    ):
        assert key not in entry, f"{key} was written for a model nobody published"
    assert defaulted.by_model["anthropic/openrouter/sonnet"] == [
        "maxContextLimit",
        "maxOutputTokens",
        "enableThinking",
        "noImageSupport",
    ]


def test_defaulted_fields_are_recorded_in_the_file() -> None:
    document, _ = build_droid_catalogue([_model()])

    assert DEFAULTED_KEY in document
    assert "anthropic/openrouter/sonnet" in document[DEFAULTED_KEY]


def test_no_image_support_is_an_opt_out_written_only_when_known_absent() -> None:
    absent, _ = build_droid_catalogue([_model(supports_vision=False)])
    assert _entry(absent)["noImageSupport"] is True

    present, _ = build_droid_catalogue([_model(supports_vision=True)])
    assert "noImageSupport" not in _entry(present)


def test_a_model_known_not_to_reason_says_so() -> None:
    document, _ = build_droid_catalogue(
        [_model(reasoning=ModelReasoningCapability(can_reason=False))]
    )

    assert _entry(document)["enableThinking"] is False


def test_a_mandatory_reasoning_model_is_never_switched_off() -> None:
    # ``mandatory`` with ``can_reason`` unset would otherwise be written as
    # False, which Droid would honour into a request the provider rejects.
    document, defaulted = build_droid_catalogue(
        [_model(reasoning=ModelReasoningCapability(can_reason=False, mandatory=True))]
    )

    assert "enableThinking" not in _entry(document)
    assert "enableThinking" in defaulted.by_model["anthropic/openrouter/sonnet"]


def test_a_no_thinking_variant_is_switched_off_as_a_fact() -> None:
    document, _ = build_droid_catalogue(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/haiku",
                provider_model_ref="openrouter/haiku",
                force_no_thinking=True,
            )
        ]
    )

    assert _entry(document)["enableThinking"] is False


def test_the_cli_model_id_carries_droids_own_custom_prefix() -> None:
    assert droid_model_id(_model()) == "custom:anthropic/openrouter/sonnet"
    # And the prefix is not part of the document's own ``model`` field.
    document, _ = build_droid_catalogue([_model()])
    assert _entry(document)["model"] == "anthropic/openrouter/sonnet"


def test_the_serialiser_is_reachable_through_the_registry() -> None:
    document, _ = serialise("droid", [_model(context_length=1)])

    assert len(model_entries("droid", document)) == 1


def test_the_documented_defaults_say_what_droid_does_instead() -> None:
    assert set(CLI_DOCUMENTED_DEFAULTS) == {
        "maxContextLimit",
        "maxOutputTokens",
        "enableThinking",
        "noImageSupport",
    }
    assert all(value is None for value in CLI_DOCUMENTED_DEFAULTS.values())
