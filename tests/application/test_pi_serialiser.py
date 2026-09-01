"""Pi's model list must carry real windows, real costs and honest defaults."""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.pi import (
    CLI_DOCUMENTED_DEFAULTS,
    build_pi_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability


def _model(
    ref: str,
    *,
    context_length: int | None = None,
    max_output_tokens: int | None = None,
    supports_vision: bool | None = None,
    reasoning: ModelReasoningCapability | None = None,
    input_price: float | None = None,
    output_price: float | None = None,
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=f"anthropic/{ref}",
        provider_model_ref=ref,
        display_name=ref,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        supports_vision=supports_vision,
        reasoning=reasoning,
        input_price=input_price,
        output_price=output_price,
    )


def _entries(models: list[CatalogueModel]) -> dict[str, dict[str, Any]]:
    document, _ = build_pi_catalogue(models)
    return {entry["id"]: entry for entry in document["models"]}


def test_context_window_and_max_tokens_come_from_the_ladder() -> None:
    entries = _entries(
        [
            _model("open_router/small", context_length=32768, max_output_tokens=4096),
            _model("open_router/big", context_length=1048576, max_output_tokens=65536),
        ]
    )

    assert entries["open_router/small"]["contextWindow"] == 32768
    assert entries["open_router/small"]["maxTokens"] == 4096
    assert entries["open_router/big"]["contextWindow"] == 1048576
    assert entries["open_router/big"]["maxTokens"] == 65536


def test_costs_come_from_published_prices_instead_of_zeros() -> None:
    entry = _entries(
        [
            _model(
                "open_router/priced",
                context_length=128000,
                max_output_tokens=8192,
                input_price=0.000003,
                output_price=0.000015,
            )
        ]
    )["open_router/priced"]

    assert entry["cost"]["input"] == 0.000003
    assert entry["cost"]["output"] == 0.000015


def test_unknown_fields_take_pi_defaults_and_are_recorded() -> None:
    document, defaulted = build_pi_catalogue([_model("open_router/unknown")])

    entry = document["models"][0]
    assert entry["contextWindow"] == CLI_DOCUMENTED_DEFAULTS["contextWindow"]
    assert entry["maxTokens"] == CLI_DOCUMENTED_DEFAULTS["maxTokens"]
    assert entry["cost"] == CLI_DOCUMENTED_DEFAULTS["cost"]
    recorded = document["_mcc_defaulted"]["open_router/unknown"]
    assert {"contextWindow", "maxTokens", "cost", "reasoning", "input"} <= set(recorded)
    assert defaulted.model_count == 1


def test_reasoning_reflects_the_ladder_not_the_id_prefix() -> None:
    entries = _entries(
        [
            _model(
                "open_router/thinker",
                context_length=200000,
                max_output_tokens=8192,
                reasoning=ModelReasoningCapability(can_reason=True),
            ),
            _model(
                "open_router/plain",
                context_length=200000,
                max_output_tokens=8192,
                reasoning=ModelReasoningCapability(can_reason=False),
            ),
        ]
    )

    assert entries["open_router/thinker"]["reasoning"] is True
    assert entries["open_router/plain"]["reasoning"] is False


def test_vision_support_widens_the_input_modalities() -> None:
    entries = _entries(
        [
            _model(
                "open_router/seeing",
                context_length=200000,
                max_output_tokens=8192,
                supports_vision=True,
                reasoning=ModelReasoningCapability(can_reason=False),
            ),
            _model(
                "open_router/blind",
                context_length=200000,
                max_output_tokens=8192,
                supports_vision=False,
                reasoning=ModelReasoningCapability(can_reason=False),
            ),
        ]
    )

    assert entries["open_router/seeing"]["input"] == ["text", "image"]
    assert entries["open_router/blind"]["input"] == ["text"]


def test_pi_strips_both_gateway_prefixes_back_to_the_provider_ref() -> None:
    no_thinking = CatalogueModel(
        gateway_id="claude-3-freecc-no-thinking/open_router/only",
        provider_model_ref="open_router/only",
        display_name="open_router/only (no thinking)",
        force_no_thinking=True,
        context_length=64000,
        max_output_tokens=4096,
    )

    document, _ = build_pi_catalogue([no_thinking])

    assert document["models"][0]["id"] == "open_router/only"
    assert document["models"][0]["reasoning"] is False
