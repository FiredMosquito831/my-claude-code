"""Gemini CLI's settings document says exactly what the ladder knows.

Every key asserted here was read out of Gemini CLI 0.49.0's own settings schema
and merge implementation; the two that carry the whole harness are
``security.auth.selectedType`` (without which non-interactive startup dies in
``validateAuthMethod``) and ``modelConfigs.customAliases`` (which merges with,
rather than replaces, the CLI's built-in presets).
"""

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.application.catalogues.gemini_cli import (
    build_gemini_cli_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.core.reasoning import ReasoningEffort


def _model(gateway_id: str, **kwargs) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=gateway_id.removeprefix("anthropic/"),
        display_name=gateway_id,
        **kwargs,
    )


def test_the_document_selects_the_api_key_auth_type() -> None:
    """The one key that stops getAuthTypeFromEnv choosing "gateway".

    ``GOOGLE_GEMINI_BASE_URL`` alone makes the CLI infer ``gateway``, and
    ``validateAuthMethod`` refuses that value with "Invalid auth method
    selected." before a single request is made. Naming the auth type
    short-circuits the environment sniff entirely.
    """

    document, _ = build_gemini_cli_catalogue([_model("anthropic/openrouter/gpt-5")])

    assert document["security"]["auth"]["selectedType"] == "gemini-api-key"


def test_the_document_turns_googles_own_telemetry_off() -> None:
    """ClearcutLogger posts to play.googleapis.com and has no env var."""

    document, _ = build_gemini_cli_catalogue([_model("anthropic/openrouter/gpt-5")])

    assert document["privacy"]["usageStatisticsEnabled"] is False


def test_no_credential_and_no_base_url_reach_the_document() -> None:
    """Both are environment variables the launcher sets in the child only."""

    document, _ = build_gemini_cli_catalogue([_model("anthropic/openrouter/gpt-5")])
    rendered = repr(document)

    assert "GEMINI_API_KEY" not in rendered
    assert "http" not in rendered
    assert "baseUrl" not in rendered


def test_the_first_visible_model_becomes_the_session_default() -> None:
    document, _ = build_gemini_cli_catalogue(
        [_model("anthropic/openrouter/gpt-5"), _model("anthropic/openrouter/other")]
    )

    assert document["model"]["name"] == "anthropic/openrouter/gpt-5"


def test_aliases_are_written_under_custom_aliases_not_aliases() -> None:
    """``aliases`` defaults to the built-in preset chain; naming it wipes it."""

    document, _ = build_gemini_cli_catalogue([_model("anthropic/openrouter/gpt-5")])

    assert "aliases" not in document["modelConfigs"]
    entry = document["modelConfigs"]["customAliases"]["anthropic/openrouter/gpt-5"]
    assert entry["extends"] == "chat-base"
    assert entry["modelConfig"]["model"] == "anthropic/openrouter/gpt-5"


def test_a_known_output_limit_is_published_and_an_unknown_one_is_omitted() -> None:
    document, defaulted = build_gemini_cli_catalogue(
        [
            _model("anthropic/p/known", max_output_tokens=8192),
            _model("anthropic/p/unknown"),
        ]
    )
    aliases = document["modelConfigs"]["customAliases"]

    known = aliases["anthropic/p/known"]["modelConfig"]["generateContentConfig"]
    assert known["maxOutputTokens"] == 8192
    unknown = aliases["anthropic/p/unknown"]["modelConfig"].get(
        "generateContentConfig", {}
    )
    assert "maxOutputTokens" not in unknown
    assert "maxOutputTokens" in defaulted.by_model["anthropic/p/unknown"]
    assert document[DEFAULTED_KEY]["anthropic/p/unknown"]


def test_a_reasoning_model_publishes_its_strongest_rung_as_a_thinking_level() -> None:
    document, _ = build_gemini_cli_catalogue(
        [
            _model(
                "anthropic/p/thinker",
                reasoning=ModelReasoningCapability(
                    can_reason=True,
                    supports_effort_control=True,
                    supported_efforts=frozenset(
                        {ReasoningEffort.LOW, ReasoningEffort.MAX}
                    ),
                ),
            )
        ]
    )

    config = document["modelConfigs"]["customAliases"]["anthropic/p/thinker"][
        "modelConfig"
    ]["generateContentConfig"]
    assert config["thinkingConfig"] == {
        "thinkingLevel": "HIGH",
        "includeThoughts": True,
    }


def test_a_model_known_not_to_reason_gets_a_zero_budget() -> None:
    """``thinkingBudget: 0`` is Google's own spelling of "do not think"."""

    document, _ = build_gemini_cli_catalogue(
        [
            _model(
                "anthropic/p/plain",
                reasoning=ModelReasoningCapability(can_reason=False),
            )
        ]
    )

    config = document["modelConfigs"]["customAliases"]["anthropic/p/plain"][
        "modelConfig"
    ]["generateContentConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 0}


def test_a_surviving_no_thinking_twin_states_a_zero_budget_under_its_plain_ref() -> (
    None
):
    """The prefix is a Claude Code heuristic; Gemini CLI reads the budget."""

    document, defaulted = build_gemini_cli_catalogue(
        [
            CatalogueModel(
                gateway_id="claude-3-freecc-no-thinking/p/m",
                provider_model_ref="p/m",
                display_name="p/m (no thinking)",
                force_no_thinking=True,
            )
        ]
    )

    aliases = document["modelConfigs"]["customAliases"]
    assert list(aliases) == ["anthropic/p/m"]
    config = aliases["anthropic/p/m"]["modelConfig"]["generateContentConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in defaulted.by_model.get("anthropic/p/m", [])


def test_an_unknown_reasoning_capability_omits_the_key_and_records_it() -> None:
    document, defaulted = build_gemini_cli_catalogue([_model("anthropic/p/silent")])

    config = document["modelConfigs"]["customAliases"]["anthropic/p/silent"][
        "modelConfig"
    ].get("generateContentConfig", {})
    assert "thinkingConfig" not in config
    assert "thinkingConfig" in defaulted.by_model["anthropic/p/silent"]


def test_the_registry_reaches_the_serialiser_and_counts_its_entries() -> None:
    """``model_entries`` is what the launcher and the dashboard both count."""

    document, _ = serialise(
        "gemini_cli",
        [_model("anthropic/p/one"), _model("anthropic/p/two")],
    )

    assert len(model_entries("gemini_cli", document)) == 2
