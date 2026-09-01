"""The Command Code fragment carries the ladder's answers, and only those.

Command Code's provider block is unusual in one respect that these cases keep
honest: it is merged into a file the *user* owns, so a key MCC writes with an
invented value is a number a user would reasonably read as their provider's
answer. Every field below therefore either comes from the ladder or is absent
and recorded.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.application.catalogues.commandcode import (
    API_KEY_REFERENCE,
    BASE_URL_SENTINEL,
    COMMANDCODE_EFFORTS,
    PROVIDER_API,
    PROVIDER_ID,
    build_commandcode_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import COMMANDCODE_API_KEY_ENV
from my_claude_code.core.reasoning import ReasoningEffort


def _model(**overrides: Any) -> CatalogueModel:
    fields: dict[str, Any] = {
        "gateway_id": "anthropic/openrouter/sonnet",
        "provider_model_ref": "openrouter/sonnet",
        "display_name": "openrouter/sonnet",
    }
    fields.update(overrides)
    return CatalogueModel(**fields)


def _provider(document: dict[str, Any]) -> dict[str, Any]:
    return document["provider"][PROVIDER_ID]


def _models(document: dict[str, Any]) -> dict[str, Any]:
    return _provider(document)["models"]


def test_context_window_and_max_output_come_from_the_ladder() -> None:
    document, defaulted = build_commandcode_catalogue(
        [_model(context_length=1_048_576, max_output_tokens=64_000)]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["contextWindow"] == 1_048_576
    assert entry["maxOutput"] == 64_000
    assert defaulted.by_model.get("openrouter/sonnet", []) == [
        "reasoning",
        "reasoningEfforts",
        "cost",
    ]


def test_optional_unknown_fields_are_omitted_not_zeroed() -> None:
    """An absent maxOutput makes Command Code guess; a wrong one makes it lie."""

    document, _ = build_commandcode_catalogue([_model()])

    entry = _models(document)["openrouter/sonnet"]
    assert entry == {"name": "openrouter/sonnet"}
    assert document[DEFAULTED_KEY]["openrouter/sonnet"] == [
        "contextWindow",
        "maxOutput",
        "reasoning",
        "reasoningEfforts",
        "cost",
    ]


def test_reasoning_efforts_clamp_to_the_clis_own_vocabulary() -> None:
    document, _ = build_commandcode_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset(
                        {ReasoningEffort.MINIMAL, ReasoningEffort.HIGH}
                    ),
                )
            )
        ]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["reasoning"] is True
    # "minimal" has no Command Code rung and folds down to "low"; the result is
    # in Command Code's own order, and carries nothing the model never claimed.
    assert entry["reasoningEfforts"] == ["low", "high"]
    assert set(entry["reasoningEfforts"]) <= set(COMMANDCODE_EFFORTS)


def test_xhigh_is_absent_when_the_model_does_not_support_it() -> None:
    document, _ = build_commandcode_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset({ReasoningEffort.MEDIUM}),
                )
            )
        ]
    )

    assert _models(document)["openrouter/sonnet"]["reasoningEfforts"] == ["medium"]


def test_a_model_with_no_effort_knob_gets_an_empty_list_not_an_omission() -> None:
    """Empty is a statement: it removes every rung from /effort."""

    document, defaulted = build_commandcode_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True, supports_effort_control=False
                )
            )
        ]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["reasoning"] is True
    assert entry["reasoningEfforts"] == []
    assert "reasoningEfforts" not in defaulted.by_model.get("openrouter/sonnet", [])


def test_a_model_known_not_to_reason_says_so() -> None:
    document, _ = build_commandcode_catalogue(
        [_model(reasoning=ModelReasoningCapability(can_reason=False))]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["reasoning"] is False
    assert "reasoningEfforts" not in entry


def test_prices_are_carried_in_the_units_command_code_reads() -> None:
    document, defaulted = build_commandcode_catalogue(
        [_model(input_price=3.0, output_price=15.0)]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["cost"] == {"input": 3.0, "output": 15.0}
    # The two cache rates are Command Code's problem, not MCC's: nothing in
    # the ladder resolves them and deriving them would be inventing a number.
    assert defaulted.by_model["openrouter/sonnet"] == [
        "contextWindow",
        "maxOutput",
        "reasoning",
        "reasoningEfforts",
        "cost.cacheRead",
        "cost.cacheWrite",
    ]


def test_the_provider_block_authenticates_by_reference_never_by_value() -> None:
    """The merged file is the user's own document; the token must not be in it."""

    document, _ = build_commandcode_catalogue([_model()])

    provider = _provider(document)
    assert provider["api"] == PROVIDER_API == "anthropic-messages"
    assert provider["apiKey"] == API_KEY_REFERENCE == f"${COMMANDCODE_API_KEY_ENV}"
    assert provider["baseURL"] == BASE_URL_SENTINEL


def test_the_no_thinking_variant_keeps_its_prefix_and_declares_no_reasoning() -> None:
    document, _ = build_commandcode_catalogue(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                provider_model_ref="openrouter/sonnet",
                force_no_thinking=True,
            )
        ]
    )

    entry = _models(document)["claude-3-freecc-no-thinking/openrouter/sonnet"]
    assert entry["reasoning"] is False


def test_the_no_thinking_variant_is_dropped_when_the_normal_one_exists() -> None:
    document, _ = build_commandcode_catalogue(
        [
            _model(),
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
            ),
        ]
    )

    assert list(_models(document)) == ["openrouter/sonnet"]


def test_pinned_gateway_parameters_reach_the_options_block() -> None:
    document, _ = build_commandcode_catalogue(
        [_model(default_parameters=(("temperature", 1.0),))]
    )

    assert _models(document)["openrouter/sonnet"]["options"] == {"temperature": 1.0}


def test_the_registry_finds_the_serialiser_and_its_model_entries() -> None:
    document, _ = serialise("commandcode", [_model(context_length=200)])

    assert len(model_entries("commandcode", document)) == 1
