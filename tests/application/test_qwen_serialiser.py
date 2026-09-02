"""What MCC states to Qwen Code, and what it refuses to state.

Qwen Code's settings schema carries less per model than Crush's and more than
Kimi Code's: a context window and a modality set, but no output ceiling, no
tools capability and no *list* of allowed reasoning efforts -- only the one a
session starts at. Every one of those absences is asserted here so a future
edit that "restores" one has to explain itself.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.qwen import (
    CLI_DOCUMENTED_DEFAULTS,
    QWEN_EFFORTS,
    build_qwen_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import (
    QWEN_API_KEY_ENV,
    QWEN_BASE_URL_SENTINEL,
    QWEN_SETTINGS_VERSION,
)
from my_claude_code.core.reasoning import ReasoningEffort


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
    )


def _entry(document: dict[str, Any]) -> dict[str, Any]:
    entries = model_entries("qwen", document)
    assert len(entries) == 1
    return entries[0]


def test_the_provider_entry_names_a_variable_not_a_token() -> None:
    document, _ = build_qwen_catalogue([_model(context_length=200_000)])

    entry = _entry(document)
    assert entry["baseUrl"] == QWEN_BASE_URL_SENTINEL
    assert entry["envKey"] == QWEN_API_KEY_ENV
    # The token itself never appears: Qwen reads process.env[envKey].
    assert "apiKey" not in entry


def test_the_document_declares_the_settings_version_it_was_written_for() -> None:
    # Without it Qwen treats the file as pre-migration and rewrites MCC's own
    # catalogue in place on first read.
    document, _ = build_qwen_catalogue([_model(context_length=200_000)])

    assert document["$version"] == QWEN_SETTINGS_VERSION


def test_a_model_carries_the_ladders_context_and_the_wire_name() -> None:
    document, defaulted = build_qwen_catalogue([_model(context_length=131_072)])

    entry = _entry(document)
    # Qwen passes modelProviders[].id straight through as the request's model,
    # so this is MCC's gateway id verbatim.
    assert entry["id"] == "anthropic/openrouter/sonnet"
    assert entry["name"] == "Claude Sonnet 4.5"
    assert entry["generationConfig"]["contextWindowSize"] == 131_072
    assert "contextWindowSize" not in defaulted.by_model.get(
        "anthropic/openrouter/sonnet", []
    )


def test_an_unknown_context_length_omits_the_key_and_is_recorded() -> None:
    document, defaulted = build_qwen_catalogue([_model()])

    entry = _entry(document)
    generation = entry.get("generationConfig", {})
    assert "contextWindowSize" not in generation
    assert "contextWindowSize" in defaulted.by_model["anthropic/openrouter/sonnet"]
    assert CLI_DOCUMENTED_DEFAULTS["contextWindowSize"] is None


def test_vision_writes_a_modality_block_only_when_it_is_known_present() -> None:
    known, _ = build_qwen_catalogue([_model(supports_vision=True)])
    absent, _ = build_qwen_catalogue([_model(supports_vision=False)])
    unknown, defaulted = build_qwen_catalogue([_model()])

    assert _entry(known)["generationConfig"]["modalities"] == {"image": True}
    # Known-absent and unknown both omit the block, because Qwen's modalities
    # are opt-in booleans -- but only the unknown case is recorded.
    assert "modalities" not in _entry(absent).get("generationConfig", {})
    assert "modalities" not in _entry(unknown).get("generationConfig", {})
    assert "modalities" in defaulted.by_model["anthropic/openrouter/sonnet"]


def test_reasoning_efforts_clamp_to_qwens_three_rungs() -> None:
    document, _ = build_qwen_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset(
                        {ReasoningEffort.LOW, ReasoningEffort.MEDIUM}
                    ),
                )
            )
        ]
    )

    # Qwen carries one starting effort, not a list: the strongest rung the
    # model actually supports.
    assert _entry(document)["generationConfig"]["reasoning"] == {"effort": "medium"}
    assert QWEN_EFFORTS == ("low", "medium", "high")


def test_xhigh_folds_down_to_high_because_qwen_clamps_it_anyway() -> None:
    document, _ = build_qwen_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset({ReasoningEffort.XHIGH}),
                )
            )
        ]
    )

    assert _entry(document)["generationConfig"]["reasoning"] == {"effort": "high"}


def test_a_model_known_not_to_reason_says_so() -> None:
    document, defaulted = build_qwen_catalogue(
        [_model(reasoning=ModelReasoningCapability(can_reason=False))]
    )

    assert _entry(document)["generationConfig"]["reasoning"] is False
    # Known-absent is a statement, not an omission: nothing is recorded for
    # the reasoning key, whatever else this bare model leaves unknown.
    assert "reasoning" not in defaulted.by_model["anthropic/openrouter/sonnet"]


def test_a_reasoning_model_with_no_knob_gets_an_empty_block_not_an_effort() -> None:
    document, _ = build_qwen_catalogue(
        [
            _model(
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=False,
                )
            )
        ]
    )

    assert _entry(document)["generationConfig"]["reasoning"] == {}


def test_a_surviving_no_thinking_twin_is_listed_under_its_plain_ref() -> None:
    """A twin that survives is not a second model -- it is a model whose
    provider said it cannot think, so the normal variant was never emitted.
    ``visible_entries`` re-projects it onto its plain ref: the
    ``claude-3-freecc-no-thinking/`` prefix is a Claude Code heuristic and
    means nothing to a CLI that reads ``reasoning: false`` instead.
    """

    document, _ = build_qwen_catalogue(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
            )
        ]
    )

    entry = _entry(document)
    assert entry["id"] == "anthropic/openrouter/sonnet"
    assert entry["generationConfig"]["reasoning"] is False


def test_the_no_thinking_variant_is_dropped_when_the_normal_one_exists() -> None:
    document, _ = build_qwen_catalogue(
        [
            _model(),
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
            ),
        ]
    )

    assert len(model_entries("qwen", document)) == 1


def test_the_registered_format_round_trips_through_model_entries() -> None:
    document, _ = serialise("qwen", [_model(context_length=200_000)])

    entries = model_entries("qwen", document)
    assert [entry["id"] for entry in entries] == ["anthropic/openrouter/sonnet"]
