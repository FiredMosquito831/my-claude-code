"""Codex's catalogue must carry the ladder's numbers, not a fixed placeholder."""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues.codex import (
    CLI_DOCUMENTED_DEFAULTS,
    build_codex_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.core.reasoning import ReasoningEffort


def _model(
    ref: str,
    *,
    context_length: int | None = None,
    max_output_tokens: int | None = None,
    supports_vision: bool | None = None,
    supports_tool_calls: bool | None = None,
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
        supports_tool_calls=supports_tool_calls,
        reasoning=reasoning,
        input_price=input_price,
        output_price=output_price,
    )


def _entries(models: list[CatalogueModel]) -> dict[str, dict[str, Any]]:
    document, _ = build_codex_catalogue(models)
    return {entry["slug"]: entry for entry in document["models"]}


def test_context_window_comes_from_ladder_not_two_hundred_k() -> None:
    entries = _entries(
        [
            _model("open_router/small", context_length=32768),
            _model("open_router/huge", context_length=1048576),
        ]
    )

    assert entries["open_router/small"]["context_window"] == 32768
    assert entries["open_router/small"]["max_context_window"] == 32768
    assert entries["open_router/huge"]["context_window"] == 1048576


def test_reasoning_levels_clamp_to_supported_efforts() -> None:
    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
    )

    entries = _entries([_model("open_router/two-rung", reasoning=capability)])
    efforts = [
        rung["effort"]
        for rung in entries["open_router/two-rung"]["supported_reasoning_levels"]
    ]

    assert efforts == ["low", "high"]
    # Codex's own preferred rung is not offered when the model never claimed it.
    assert entries["open_router/two-rung"]["default_reasoning_level"] == "low"


def test_xhigh_absent_when_model_does_not_support_it() -> None:
    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset(
            {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
        ),
    )

    entries = _entries([_model("open_router/no-xhigh", reasoning=capability)])
    efforts = [
        rung["effort"]
        for rung in entries["open_router/no-xhigh"]["supported_reasoning_levels"]
    ]

    assert "xhigh" not in efforts
    assert efforts == ["low", "medium", "high"]


def test_a_model_that_cannot_reason_advertises_no_rungs() -> None:
    entries = _entries(
        [
            _model(
                "open_router/plain",
                reasoning=ModelReasoningCapability(can_reason=False),
            )
        ]
    )

    entry = entries["open_router/plain"]
    assert entry["supported_reasoning_levels"] == []
    assert entry["supports_reasoning_summaries"] is False
    assert "default_reasoning_level" not in entry


def test_a_toggle_only_model_reasons_with_no_effort_list() -> None:
    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=False,
        supports_toggle_control=True,
        supported_efforts=None,
    )

    entry = _entries([_model("open_router/toggle", reasoning=capability)])[
        "open_router/toggle"
    ]

    assert entry["supported_reasoning_levels"] == []
    assert entry["supports_reasoning_summaries"] is True
    assert "default_reasoning_level" not in entry


def test_a_mandatory_reasoning_model_gets_no_key_codex_does_not_read() -> None:
    """``reasoning_required`` does not exist in Codex 0.151.0.

    ``grep -a -c reasoning_required codex.exe`` returns 0 and the name is
    absent from the ``ModelInfo`` serde field list, so writing it protected
    nothing: a model whose thinking cannot be turned off was never actually
    protected in Codex. Emitting a key the CLI ignores is worse than emitting
    none, because it reads as a guarantee. What Codex does read is the effort
    vocabulary, and that still comes from the model's own.
    """

    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.MEDIUM}),
        mandatory=True,
    )

    entry = _entries([_model("open_router/always-thinks", reasoning=capability)])[
        "open_router/always-thinks"
    ]

    assert "reasoning_required" not in entry
    assert [rung["effort"] for rung in entry["supported_reasoning_levels"]] == [
        "medium"
    ]


def test_defaulted_fields_are_recorded_in_the_file() -> None:
    document, defaulted = build_codex_catalogue([_model("open_router/unknown")])

    entry = document["models"][0]
    # Optional in 0.151.0, so an unknown window is omitted -- never the
    # ``200000`` this module exists to have removed -- and still recorded.
    assert "context_window" not in entry
    assert "max_context_window" not in entry
    assert "context_window" not in CLI_DOCUMENTED_DEFAULTS
    recorded = document["_mcc_defaulted"]["open_router/unknown"]
    assert "context_window" in recorded
    assert "max_context_window" in recorded
    assert "supported_reasoning_levels" in recorded
    assert "input_modalities" in recorded
    assert defaulted.model_count == 1


def test_known_vision_and_tool_support_are_not_recorded_as_defaults() -> None:
    document, _ = build_codex_catalogue(
        [
            _model(
                "open_router/seeing",
                context_length=200000,
                supports_vision=True,
                supports_tool_calls=True,
                reasoning=ModelReasoningCapability(can_reason=False),
            )
        ]
    )

    entry = document["models"][0]
    assert entry["input_modalities"] == ["text", "image"]
    # ``supports_parallel_tool_calls`` lives on ``RawMcpServerConfig`` and the
    # MCP tool-info struct in 0.151.0, not on ``ModelInfo``. It was the single
    # largest defaulted field in the generated document -- 63 of 142 models --
    # reporting substitutions into a key Codex does not read for a model.
    assert "supports_parallel_tool_calls" not in entry
    assert "_mcc_defaulted" not in document


def test_a_surviving_no_thinking_twin_is_listed_under_its_plain_ref() -> None:
    """The prefix is a Claude Code heuristic; Codex reads the effort list."""

    no_thinking = CatalogueModel(
        gateway_id="claude-3-freecc-no-thinking/open_router/m",
        provider_model_ref="open_router/m",
        display_name="open_router/m (no thinking)",
        force_no_thinking=True,
        context_length=64000,
    )

    document, _ = build_codex_catalogue([no_thinking])

    entry = document["models"][0]
    assert entry["slug"] == "open_router/m"
    assert entry["supported_reasoning_levels"] == []


def test_the_no_thinking_variant_is_dropped_when_the_normal_one_is_present() -> None:
    entries = _entries(
        [
            _model("open_router/m", context_length=64000),
            CatalogueModel(
                gateway_id="claude-3-freecc-no-thinking/open_router/m",
                provider_model_ref="open_router/m",
                display_name="open_router/m (no thinking)",
                force_no_thinking=True,
            ),
        ]
    )

    assert list(entries) == ["open_router/m"]
