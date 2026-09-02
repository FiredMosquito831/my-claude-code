"""What MCC states to Kimi Code, and what it refuses to state.

The mapping is narrower than every other harness's, because Kimi Code's own
schema is: there is no output-limit field, no tools capability and no
per-model reasoning-effort list. Each of those absences is asserted here so a
future edit that "restores" one has to explain itself.
"""

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.kimi import (
    CLI_DOCUMENTED_DEFAULTS,
    KIMI_CAPABILITIES,
    build_kimi_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import (
    KIMI_API_KEY_SENTINEL,
    KIMI_BASE_URL_SENTINEL,
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


def test_the_provider_block_declares_the_one_type_mcc_serves() -> None:
    document, _ = build_kimi_catalogue([_model(context_length=200_000)])

    provider = document["providers"]["mcc"]
    assert provider["type"] == "anthropic"
    assert provider["base_url"] == KIMI_BASE_URL_SENTINEL
    assert provider["api_key"] == KIMI_API_KEY_SENTINEL
    # Nothing of MCC's outside the three keys Kimi's own schema declares.
    assert set(provider) == {"type", "base_url", "api_key"}


def test_a_model_carries_the_ladders_context_and_the_wire_name() -> None:
    document, defaulted = build_kimi_catalogue(
        [_model(context_length=200_000, max_output_tokens=64_000)]
    )

    entry = document["models"]["mcc/openrouter/sonnet"]
    assert entry["provider"] == "mcc"
    assert entry["model"] == "openrouter/sonnet"
    assert entry["max_context_size"] == 200_000
    assert entry["display_name"] == "Claude Sonnet 4.5"
    assert defaulted.by_model == {
        "mcc/openrouter/sonnet": ["capabilities.image_in", "capabilities.thinking"]
    }


def test_no_output_limit_is_written_because_kimi_has_no_field_for_one() -> None:
    """``LLMModel`` has no ``max_output_size``; Kimi derives the cap itself.

    ``compute_max_completion_tokens`` takes ``max_context_size`` less the
    tokens already in the request, so a key MCC invented here would be
    ignored at best and rejected at worst -- and it is not recorded as
    defaulted either, because a field the CLI does not have is not a field the
    CLI guessed.
    """

    document, defaulted = build_kimi_catalogue([_model(max_output_tokens=64_000)])

    entry = document["models"]["mcc/openrouter/sonnet"]
    assert "max_output_size" not in entry
    assert "64000" not in str(document)
    assert "max_output_size" not in str(defaulted.by_model)


def test_an_unknown_context_is_kimis_own_zero_and_is_recorded() -> None:
    document, defaulted = build_kimi_catalogue([_model()])

    entry = document["models"]["mcc/openrouter/sonnet"]
    assert entry["max_context_size"] == CLI_DOCUMENTED_DEFAULTS["max_context_size"] == 0
    assert "max_context_size" in defaulted.by_model["mcc/openrouter/sonnet"]
    assert document["_mcc_defaulted"]["mcc/openrouter/sonnet"]


def test_capabilities_use_kimis_vocabulary_and_only_its_positives() -> None:
    document, defaulted = build_kimi_catalogue(
        [
            _model(
                context_length=200_000,
                supports_vision=True,
                supports_tool_calls=True,
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset({ReasoningEffort.HIGH}),
                ),
            )
        ]
    )

    entry = document["models"]["mcc/openrouter/sonnet"]
    assert entry["capabilities"] == ["image_in", "thinking"]
    assert set(entry["capabilities"]) <= set(KIMI_CAPABILITIES)
    # Kimi has no tools capability and no per-model effort field, so a model
    # that publishes both still produces neither.
    assert "tools" not in str(entry)
    assert "high" not in str(entry)
    assert defaulted.by_model == {}


def test_mandatory_thinking_is_stated_as_always_thinking() -> None:
    document, _ = build_kimi_catalogue(
        [
            _model(
                context_length=200_000,
                supports_vision=False,
                reasoning=ModelReasoningCapability(can_reason=True, mandatory=True),
            )
        ]
    )

    entry = document["models"]["mcc/openrouter/sonnet"]
    assert entry["capabilities"] == ["thinking", "always_thinking"]


def test_a_known_no_is_written_as_an_absence_and_never_recorded() -> None:
    """A set cannot say "not vision", so the absence is the whole statement."""

    document, defaulted = build_kimi_catalogue(
        [
            _model(
                context_length=200_000,
                supports_vision=False,
                reasoning=ModelReasoningCapability(can_reason=False),
            )
        ]
    )

    entry = document["models"]["mcc/openrouter/sonnet"]
    assert "capabilities" not in entry
    assert defaulted.by_model == {}


def test_a_no_thinking_variant_never_gets_a_thinking_capability() -> None:
    """Kimi re-adds thinking from a model's *name*, so the id matters here."""

    document, _ = build_kimi_catalogue(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
                context_length=200_000,
                supports_vision=True,
                reasoning=ModelReasoningCapability(can_reason=True),
            )
        ]
    )

    assert list(document["models"]) == ["mcc/openrouter/sonnet"]
    entry = document["models"]["mcc/openrouter/sonnet"]
    assert entry["capabilities"] == ["image_in"]
    assert entry["model"] == "openrouter/sonnet"


def test_video_is_never_claimed_because_nothing_upstream_publishes_it() -> None:
    document, defaulted = build_kimi_catalogue([_model(context_length=200_000)])

    assert "video_in" not in str(document)
    assert "video_in" not in str(defaulted.by_model)


def test_the_registered_format_finds_the_model_entries_by_its_declared_path() -> None:
    document, _ = serialise("kimi", [_model(context_length=200_000)])

    entries = model_entries("kimi", document)
    assert [entry["model"] for entry in entries] == ["openrouter/sonnet"]
