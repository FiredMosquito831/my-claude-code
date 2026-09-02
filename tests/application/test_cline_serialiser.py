"""What MCC states to Cline, and the one thing Cline's schema cannot hold.

Cline's ``providers.json`` has no per-model array: one provider entry carries
the numbers for the one model it names. So the serialiser writes every routable
model's resolved limits into an inert ``_mcc_models`` block and leaves the first
as the session default, and ``config/harness_cline.py`` promotes whichever one
the user named with ``-m`` before the file reaches disk. These tests pin both
halves, because a regression in either produces a document that loads cleanly
and reports the wrong context window.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.application.catalogues.cline import (
    CLI_DOCUMENTED_DEFAULTS,
    MODELS_KEY,
    PROVIDER_ID,
    UPDATED_AT,
    build_cline_catalogue,
)
from my_claude_code.config.harness_cline import (
    selected_model,
    strip_mcc_keys,
    with_api_key,
    with_selected_model,
)
from my_claude_code.config.harnesses import (
    CLINE_API_KEY_SENTINEL,
    CLINE_BASE_URL_SENTINEL,
)


def _model(
    *,
    gateway_id: str = "anthropic/openrouter/sonnet",
    provider_model_ref: str = "openrouter/sonnet",
    display_name: str = "Claude Sonnet 4.5",
    force_no_thinking: bool = False,
    context_length: int | None = None,
    max_output_tokens: int | None = None,
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=provider_model_ref,
        display_name=display_name,
        force_no_thinking=force_no_thinking,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
    )


def _settings(document: dict[str, Any]) -> dict[str, Any]:
    return document["providers"][PROVIDER_ID]["settings"]


def test_the_provider_is_openai_compatible_not_openai_native() -> None:
    # ``openai-native`` is OpenAI's own hosted entry and ``openai`` is an
    # alias for this one. Only ``openai-compatible`` takes an arbitrary base
    # URL and issues no model-discovery call.
    document, _ = build_cline_catalogue([_model(context_length=1)])

    assert PROVIDER_ID == "openai-compatible"
    assert _settings(document)["provider"] == PROVIDER_ID
    assert document["lastUsedProvider"] == PROVIDER_ID


def test_the_provider_entry_carries_the_timestamp_cline_requires() -> None:
    """Without it Cline discards the whole settings object on load.

    Measured on 3.0.61: an entry with ``settings`` and ``tokenSource`` but no
    ``updatedAt`` was rewritten as ``{"provider": ..., "model": "gpt-4o"}``,
    dropping the base URL and the key, and the next run reached
    ``api.openai.com``. The value is the epoch rather than "now" because a
    serialiser is a pure function -- a real clock would make the document
    differ from itself on every call.
    """

    document, _ = build_cline_catalogue([_model(context_length=1)])

    assert document["providers"][PROVIDER_ID]["updatedAt"] == UPDATED_AT


def test_the_base_url_sentinel_carries_the_v1_segment() -> None:
    # ``@ai-sdk/openai-compatible`` appends ``chat/completions`` and nothing
    # else, so a root without ``/v1`` would miss the route entirely.
    document, _ = build_cline_catalogue([_model(context_length=1)])

    assert _settings(document)["baseUrl"] == CLINE_BASE_URL_SENTINEL
    assert CLINE_BASE_URL_SENTINEL.endswith("/v1")


def test_limits_come_from_the_ladder_not_a_placeholder() -> None:
    document, defaulted = build_cline_catalogue(
        [_model(context_length=131_072, max_output_tokens=8_192)]
    )

    settings = _settings(document)
    assert settings["contextWindow"] == 131_072
    assert settings["maxTokens"] == 8_192
    assert not defaulted.by_model


def test_optional_unknown_fields_are_omitted_not_zeroed() -> None:
    document, defaulted = build_cline_catalogue([_model()])

    settings = _settings(document)
    assert "contextWindow" not in settings
    assert "maxTokens" not in settings
    assert defaulted.by_model["anthropic/openrouter/sonnet"] == [
        "contextWindow",
        "maxTokens",
    ]


def test_defaulted_fields_are_recorded_for_the_launcher_then_stripped() -> None:
    """Cline is the one harness whose file has no room for MCC's own record.

    Measured on 3.0.61: a single unrecognised root key -- ``_mcc_models``, or
    ``_mcc_defaulted`` on its own -- made Cline discard the provider settings
    it had just read and rewrite them with its own bundled default model,
    losing the base URL and the key with it. So the record reaches the
    launcher and the API and never the file.
    """

    document, _ = build_cline_catalogue([_model()])

    assert DEFAULTED_KEY in document
    assert MODELS_KEY in document

    on_disk = strip_mcc_keys(document)
    assert DEFAULTED_KEY not in on_disk
    assert MODELS_KEY not in on_disk
    assert set(on_disk) == {"version", "lastUsedProvider", "modes", "providers"}


def test_no_routable_model_means_no_provider_entry_at_all() -> None:
    # A provider entry with no ``model`` would tell Cline to run on its own
    # bundled default, which is a model MCC does not route.
    document, _ = build_cline_catalogue([])

    assert document["providers"] == {}
    assert model_entries("cline", document) == []


def test_every_routable_model_is_recorded_even_though_one_is_selected() -> None:
    document, _ = build_cline_catalogue(
        [
            _model(context_length=1_000),
            _model(
                gateway_id="anthropic/openrouter/haiku",
                provider_model_ref="openrouter/haiku",
                context_length=2_000,
            ),
        ]
    )

    assert set(document[MODELS_KEY]) == {
        "anthropic/openrouter/sonnet",
        "anthropic/openrouter/haiku",
    }
    # Cline's schema has no per-model array, so the countable entry is the
    # provider block, and there is exactly one of those.
    assert len(model_entries("cline", document)) == 1
    # The first routable model is the session default, which is the user's own
    # chain order rather than an arbitrary pick.
    assert _settings(document)["model"] == "anthropic/openrouter/sonnet"


def test_the_named_model_is_promoted_into_the_provider_block() -> None:
    document, _ = build_cline_catalogue(
        [
            _model(context_length=1_000, max_output_tokens=10),
            _model(
                gateway_id="anthropic/openrouter/haiku",
                provider_model_ref="openrouter/haiku",
                context_length=2_000,
                max_output_tokens=20,
            ),
        ]
    )

    promoted = with_selected_model(document, PROVIDER_ID, "anthropic/openrouter/haiku")

    settings = _settings(promoted)
    assert settings["model"] == "anthropic/openrouter/haiku"
    assert settings["contextWindow"] == 2_000
    assert settings["maxTokens"] == 20


def test_promoting_a_model_with_no_limits_clears_the_previous_ones() -> None:
    # Otherwise a model nobody published numbers for would inherit the
    # default's, which is the one lie this whole layer exists to prevent.
    document, _ = build_cline_catalogue(
        [
            _model(context_length=1_000, max_output_tokens=10),
            _model(
                gateway_id="anthropic/openrouter/haiku",
                provider_model_ref="openrouter/haiku",
            ),
        ]
    )

    promoted = with_selected_model(document, PROVIDER_ID, "anthropic/openrouter/haiku")

    settings = _settings(promoted)
    assert settings["model"] == "anthropic/openrouter/haiku"
    assert "contextWindow" not in settings
    assert "maxTokens" not in settings


def test_an_unrouted_model_leaves_the_document_alone() -> None:
    document, _ = build_cline_catalogue([_model(context_length=1_000)])

    promoted = with_selected_model(document, PROVIDER_ID, "some/other/model")

    assert _settings(promoted)["model"] == "anthropic/openrouter/sonnet"
    assert _settings(promoted)["contextWindow"] == 1_000


def test_the_key_is_a_placeholder_until_the_launcher_resolves_it() -> None:
    document, _ = build_cline_catalogue([_model(context_length=1)])

    assert _settings(document)["apiKey"] == CLINE_API_KEY_SENTINEL

    resolved = with_api_key(document, PROVIDER_ID, "real-token")
    assert _settings(resolved)["apiKey"] == "real-token"
    # The original is untouched: every helper in this layer is pure.
    assert _settings(document)["apiKey"] == CLINE_API_KEY_SENTINEL


def test_the_selected_model_is_read_out_of_either_flag_spelling() -> None:
    assert selected_model(["-m", "a/b/c"]) == "a/b/c"
    assert selected_model(["--model", "a/b/c"]) == "a/b/c"
    assert selected_model(["--model=a/b/c"]) == "a/b/c"
    # Last occurrence wins, which is what Cline's own parser does.
    assert selected_model(["-m", "first", "--model", "second"]) == "second"
    assert selected_model(["--json", "hello"]) is None
    # A trailing flag with no value is not a selection.
    assert selected_model(["-m"]) is None


def test_the_serialiser_is_reachable_through_the_registry() -> None:
    document, _ = serialise("cline", [_model(context_length=1)])

    assert document["providers"][PROVIDER_ID]["settings"]["provider"] == PROVIDER_ID


def test_the_documented_defaults_say_what_cline_does_instead() -> None:
    assert set(CLI_DOCUMENTED_DEFAULTS) == {"contextWindow", "maxTokens"}
    assert all(value is None for value in CLI_DOCUMENTED_DEFAULTS.values())
