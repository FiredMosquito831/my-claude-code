"""The OpenCode fragment carries the ladder's answers, and only those.

Three CLIs read this one document -- OpenCode, its v2 preview and Kilo -- so a
mistake here is a mistake three times. The cases below pin the two halves of
the contract: a capability MCC resolved reaches OpenCode's own key, and a
capability nobody published leaves no key behind for OpenCode to mistake for a
fact.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.application.catalogues.opencode import (
    API_KEY_ENV,
    BASE_URL_ENV,
    PROVIDER_ID,
    PROVIDER_NPM_PACKAGE,
    build_opencode_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import MCC_HARNESS_ID_SENTINEL
from my_claude_code.core.client_fingerprint import HARNESS_HEADER
from my_claude_code.core.reasoning import ReasoningEffort


def _model(**overrides: Any) -> CatalogueModel:
    fields: dict[str, Any] = {
        "gateway_id": "anthropic/openrouter/sonnet",
        "provider_model_ref": "openrouter/sonnet",
        "display_name": "openrouter/sonnet",
    }
    fields.update(overrides)
    return CatalogueModel(**fields)


def _models(document: dict[str, Any]) -> dict[str, Any]:
    return document["provider"][PROVIDER_ID]["models"]


def test_limit_context_and_output_come_from_ladder() -> None:
    document, defaulted = build_opencode_catalogue(
        [_model(context_length=1_048_576, max_output_tokens=64_000)]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["limit"] == {"context": 1_048_576, "output": 64_000}
    assert "limit.context" not in defaulted.by_model.get("openrouter/sonnet", [])


def test_genuinely_optional_unknown_fields_are_omitted_not_zeroed() -> None:
    """OpenCode fills an absent key with 0; MCC must not write the 0 itself.

    Narrowed to the keys that really are optional on their own. ``limit`` is
    not one of them: it is an optional *object with required members*, so
    "omit the key" applies to the whole object and never to half of it. That
    distinction is what this test used to blur, and blurring it is how the
    defect passed review.
    """

    document, _ = build_opencode_catalogue([_model()])

    entry = _models(document)["openrouter/sonnet"]
    assert "limit" not in entry
    assert "cost" not in entry
    assert "reasoning" not in entry
    assert "tool_call" not in entry
    assert "attachment" not in entry
    assert "modalities" not in entry
    assert "options" not in entry
    assert "variants" not in entry
    assert document[DEFAULTED_KEY]["openrouter/sonnet"] == [
        "limit.context",
        "limit.output",
        "reasoning",
        "tool_call",
        "attachment",
        "cost",
    ]


def test_a_half_known_limit_is_never_shipped() -> None:
    """The exact regression: OpenCode refuses the whole document over it.

    ``Missing key provider.mcc.models.<id>.limit.context`` was 52 of 142
    entries on a real install, and the launch listed zero models. The twin of
    ``test_a_half_known_price_is_no_price_at_all``, which existed; this one
    did not.
    """

    known_output, _ = build_opencode_catalogue([_model(max_output_tokens=131072)])
    known_context, _ = build_opencode_catalogue([_model(context_length=1048576)])

    for document in (known_output, known_context):
        limit = _models(document)["openrouter/sonnet"]["limit"]
        assert set(limit) == {"context", "output"}


def test_opencode_always_emits_both_limits_and_records_the_substitution() -> None:
    """The known half survives; the unknown half is OpenCode's own ``0``."""

    document, defaulted = build_opencode_catalogue([_model(max_output_tokens=131072)])

    entry = _models(document)["openrouter/sonnet"]
    assert entry["limit"] == {"context": 0, "output": 131072}
    recorded = defaulted.by_model["openrouter/sonnet"]
    assert "limit.context" in recorded
    assert "limit.output" not in recorded


def test_a_half_known_price_is_no_price_at_all() -> None:
    """OpenCode's cost block requires input and output together."""

    document, _ = build_opencode_catalogue([_model(input_price=3.0)])

    assert "cost" not in _models(document)["openrouter/sonnet"]


def test_prices_are_carried_in_the_units_opencode_reads() -> None:
    document, defaulted = build_opencode_catalogue(
        [_model(input_price=3.0, output_price=15.0)]
    )

    entry = _models(document)["openrouter/sonnet"]
    assert entry["cost"] == {"input": 3.0, "output": 15.0}
    # The two cache rates are optional inside ``cost``, so an unresolved one is
    # omitted and recorded rather than derived from the uncached rate.
    assert "cost.cache_read" in defaulted.by_model["openrouter/sonnet"]


def test_resolved_cache_rates_reach_the_cost_block() -> None:
    """models.dev publishes both, and now the ladder carries them."""

    document, defaulted = build_opencode_catalogue(
        [
            _model(
                input_price=3.0,
                output_price=15.0,
                cache_read_price=0.3,
                cache_write_price=3.75,
            )
        ]
    )

    assert _models(document)["openrouter/sonnet"]["cost"] == {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    }
    assert "openrouter/sonnet" not in defaulted.by_model or (
        "cost.cache_read" not in defaulted.by_model["openrouter/sonnet"]
    )


def test_vision_sets_both_attachment_and_the_input_modalities() -> None:
    seeing, _ = build_opencode_catalogue([_model(supports_vision=True)])
    blind, _ = build_opencode_catalogue([_model(supports_vision=False)])

    assert _models(seeing)["openrouter/sonnet"]["attachment"] is True
    assert _models(seeing)["openrouter/sonnet"]["modalities"]["input"] == [
        "text",
        "image",
    ]
    assert _models(blind)["openrouter/sonnet"]["attachment"] is False
    assert _models(blind)["openrouter/sonnet"]["modalities"]["input"] == ["text"]


def test_reasoning_variants_clamp_to_supported_efforts() -> None:
    document, _ = build_opencode_catalogue(
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

    entry = _models(document)["openrouter/sonnet"]
    assert entry["reasoning"] is True
    assert list(entry["variants"]) == ["low", "medium"]
    assert entry["variants"]["low"] == {"reasoningEffort": "low"}


def test_a_model_with_no_effort_knob_gets_reasoning_on_and_no_variants() -> None:
    document, _ = build_opencode_catalogue(
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
    assert "variants" not in entry


def test_a_surviving_no_thinking_twin_is_listed_under_its_plain_ref() -> None:
    """A twin that survives is not a second model -- it is a model whose
    provider said it cannot think, so the normal variant was never emitted.
    ``visible_entries`` re-projects it onto its plain ref: the
    ``claude-3-freecc-no-thinking/`` prefix is a Claude Code heuristic and
    means nothing to a CLI that reads ``reasoning: false`` instead.
    """

    document, _ = build_opencode_catalogue(
        [
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
                reasoning=ModelReasoningCapability(can_reason=True),
            )
        ]
    )

    assert list(_models(document)) == ["openrouter/sonnet"]
    entry = _models(document)["openrouter/sonnet"]
    assert entry["reasoning"] is False
    assert "variants" not in entry


def test_the_no_thinking_variant_is_dropped_when_the_normal_one_is_present() -> None:
    document, _ = build_opencode_catalogue(
        [
            _model(),
            _model(
                gateway_id="claude-3-freecc-no-thinking/openrouter/sonnet",
                force_no_thinking=True,
            ),
        ]
    )

    assert list(_models(document)) == ["openrouter/sonnet"]


def test_the_document_names_env_placeholders_rather_than_the_token() -> None:
    """The proxy token must not reach disk; OpenCode substitutes it at load."""

    document, _ = build_opencode_catalogue([_model()])

    provider = document["provider"][PROVIDER_ID]
    assert provider["npm"] == PROVIDER_NPM_PACKAGE
    # One credential, in one place. ``@ai-sdk/anthropic`` sends ``apiKey`` as
    # ``x-api-key``, which MCC has read since 6.27.0; the second copy this
    # document used to carry as an explicit Authorization header was justified
    # by a comment that had been wrong for nine minor versions.
    assert provider["options"] == {
        "baseURL": f"{{env:{BASE_URL_ENV}}}",
        "apiKey": f"{{env:{API_KEY_ENV}}}",
        # The header block is back, and it is not what PR #258 removed. That
        # was an ``Authorization`` override carrying a second copy of the
        # proxy token; this is one non-secret label naming the harness. The
        # assertion is on the whole mapping so a credential cannot be added
        # beside it without this test saying so.
        "headers": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL},
    }
    assert "authorization" not in {
        key.lower() for key in provider["options"]["headers"]
    }
    assert "sk-" not in str(document)


def test_the_provider_block_carries_the_harness_attribution_sentinel() -> None:
    """The value is a sentinel, not an id: one serialiser, three harnesses."""

    document, _ = build_opencode_catalogue([_model()])

    headers = document["provider"][PROVIDER_ID]["options"]["headers"]
    assert headers == {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}
    # Nested, never at the root. Kilo CLI rejects an unknown *root* key and
    # ignores an unknown nested one, and this document is Kilo's too.
    assert HARNESS_HEADER not in document


def test_pinned_default_parameters_become_opencode_options() -> None:
    document, _ = build_opencode_catalogue(
        [_model(default_parameters=(("temperature", 1.0),))]
    )

    assert _models(document)["openrouter/sonnet"]["options"] == {"temperature": 1.0}


def test_the_registered_format_id_resolves_to_this_serialiser() -> None:
    document, _ = serialise("opencode", [_model(context_length=200_000)])

    assert len(model_entries("opencode", document)) == 1
    assert document["$schema"] == "https://opencode.ai/config.json"
