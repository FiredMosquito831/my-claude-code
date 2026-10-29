"""OpenClaw's model document: every field the ladder knows, and no invented ones.

The shape is OpenClaw's own -- read out of the schema the CLI generates for
itself -- so these tests are about the mapping table in the specification:
``context_length`` -> ``contextWindow``, ``max_output_tokens`` -> ``maxTokens``,
``supports_vision`` -> ``input``, the four ``*_price`` fields -> ``cost`` (USD
per million tokens on both sides), and the effort list -> ``compat``. The rule
underneath every one of them is ``application/catalogues/base.py``'s: a value
the ladder does not know is *omitted* and recorded, never zeroed.
"""

from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.openclaw import (
    API_KEY_REFERENCE,
    PROVIDER_API,
    PROVIDER_ID,
    build_openclaw_catalogue,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harness_attribution import with_harness_id
from my_claude_code.config.harnesses import (
    MCC_HARNESS_ID_SENTINEL,
    OPENCLAW_API_KEY_ENV,
    OPENCLAW_BASE_URL_SENTINEL,
    OPENCLAW_PRIMARY_MODEL_PATH,
)
from my_claude_code.core.client_fingerprint import HARNESS_HEADER
from my_claude_code.core.reasoning import ReasoningEffort


def _model(**overrides: Any) -> CatalogueModel:
    fields: dict[str, Any] = {
        "gateway_id": "anthropic/openrouter/sonnet",
        "provider_model_ref": "openrouter/sonnet",
        "display_name": "Sonnet (OpenRouter)",
        "context_length": 400_000,
        "max_output_tokens": 32_000,
        "supports_vision": True,
        "supports_tool_calls": True,
        "input_price": 1.25,
        "output_price": 10.0,
        "cache_read_price": 0.125,
        "cache_write_price": 1.5,
        "reasoning": ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
        ),
    }
    fields.update(overrides)
    return CatalogueModel(**fields)


def _block(models: list[CatalogueModel]) -> dict[str, Any]:
    document, _defaulted = build_openclaw_catalogue(models)
    return document["models"]["providers"][PROVIDER_ID]


def test_a_fully_known_model_maps_every_capability_it_publishes() -> None:
    entry = _block([_model()])["models"][0]

    assert entry == {
        "id": "openrouter/sonnet",
        "name": "Sonnet (OpenRouter)",
        "contextWindow": 400_000,
        "maxTokens": 32_000,
        "input": ["text", "image"],
        "reasoning": True,
        "cost": {
            "input": 1.25,
            "output": 10.0,
            "cacheRead": 0.125,
            "cacheWrite": 1.5,
        },
        "compat": {"supportedReasoningEfforts": ["low", "high"]},
    }


def test_the_required_keys_are_the_two_openclaw_requires() -> None:
    """``"required": ["id", "name"]`` in OpenClaw's own model schema."""

    entry = _block([_model(display_name="")])["models"][0]

    assert entry["id"] == "openrouter/sonnet"
    assert entry["name"] == "openrouter/sonnet", "a missing name falls back to id"


def test_an_unknown_capability_is_omitted_and_recorded_never_zeroed() -> None:
    silent = CatalogueModel(
        gateway_id="anthropic/custom_acme/silent",
        provider_model_ref="custom_acme/silent",
        display_name="custom_acme/silent",
    )

    entry = _block([silent])["models"][0]

    assert entry == {"id": "custom_acme/silent", "name": "custom_acme/silent"}
    _document, defaulted = build_openclaw_catalogue([silent])
    assert sorted(defaulted.by_model["custom_acme/silent"]) == [
        "contextWindow",
        "cost.cacheRead",
        "cost.cacheWrite",
        "cost.input",
        "cost.output",
        "input",
        "maxTokens",
        "reasoning",
    ]


def test_prices_go_across_in_usd_per_million_tokens_and_only_where_known() -> None:
    """MCC stores models.dev's rates, which are already per million tokens."""

    partial = _model(cache_read_price=None, cache_write_price=None)

    entry = _block([partial])["models"][0]

    assert entry["cost"] == {"input": 1.25, "output": 10.0}
    assert "cacheRead" not in entry["cost"]
    _document, defaulted = build_openclaw_catalogue([partial])
    assert defaulted.by_model["openrouter/sonnet"] == [
        "cost.cacheRead",
        "cost.cacheWrite",
    ]


def test_a_model_with_no_price_writes_no_cost_block_at_all() -> None:
    """Never a zero: a zero reads as "free", which is a claim MCC cannot make."""

    unpriced = _model(
        input_price=None,
        output_price=None,
        cache_read_price=None,
        cache_write_price=None,
    )

    entry = _block([unpriced])["models"][0]

    assert "cost" not in entry
    assert "0" not in repr(_block([unpriced])).replace("400000", "").replace(
        "32000", ""
    )


def test_the_provider_block_names_the_token_by_reference_only() -> None:
    """The proxy token never lands in a file the user owns."""

    block = _block([_model()])

    assert block["apiKey"] == API_KEY_REFERENCE == f"${OPENCLAW_API_KEY_ENV}"
    assert block["baseUrl"] == OPENCLAW_BASE_URL_SENTINEL
    assert block["api"] == PROVIDER_API == "openai-completions"


def test_the_starting_model_is_stated_outside_mccs_block() -> None:
    """The one value Configure replaces, and Undo puts back."""

    plain = _model()
    configured = _model(
        gateway_id="anthropic/openrouter/configured",
        provider_model_ref="openrouter/configured",
        display_name="Configured",
        is_primary_route=True,
    )

    document, _defaulted = build_openclaw_catalogue([plain, configured])

    node: Any = document
    for key in OPENCLAW_PRIMARY_MODEL_PATH:
        node = node[key]
    assert node == "openrouter/configured"
    # ...and it is a ref the models list actually carries.
    assert "openrouter/configured" in [
        entry["id"] for entry in _block([plain, configured])["models"]
    ]


def test_no_models_means_no_primary_model_line() -> None:
    document, _defaulted = build_openclaw_catalogue([])

    assert "agents" not in document
    assert document["models"]["providers"][PROVIDER_ID]["models"] == []


def test_the_attribution_header_is_a_sentinel_the_caller_resolves() -> None:
    document, _defaulted = build_openclaw_catalogue([_model()])

    block = document["models"]["providers"][PROVIDER_ID]
    assert block["headers"] == {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}

    resolved = with_harness_id(document, "openclaw")
    assert resolved["models"]["providers"][PROVIDER_ID]["headers"] == {
        HARNESS_HEADER: "openclaw"
    }


def test_a_non_reasoning_model_is_a_statement_not_a_default() -> None:
    thinkless = _model(reasoning=ModelReasoningCapability(can_reason=False))

    entry = _block([thinkless])["models"][0]

    assert entry["reasoning"] is False
    assert "compat" not in entry
    _document, defaulted = build_openclaw_catalogue([thinkless])
    assert defaulted.by_model == {}, "nothing above was guessed"


def test_tool_support_is_written_only_when_it_is_stated_negative() -> None:
    no_tools = _model(supports_tool_calls=False)

    entry = _block([no_tools])["models"][0]

    assert entry["compat"]["supportsTools"] is False
    # ...and the effort list survives beside it rather than being replaced.
    assert entry["compat"]["supportedReasoningEfforts"] == ["low", "high"]


def test_the_effort_list_never_names_a_rung_the_model_did_not_publish() -> None:
    only_high = _model(
        reasoning=ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supported_efforts=frozenset({ReasoningEffort.HIGH}),
        )
    )

    entry = _block([only_high])["models"][0]

    assert entry["compat"] == {"supportedReasoningEfforts": ["high"]}


def test_vision_support_drives_the_modality_list_both_ways() -> None:
    text_only = _model(supports_vision=False)

    assert _block([text_only])["models"][0]["input"] == ["text"]
    assert _block([_model()])["models"][0]["input"] == ["text", "image"]


def test_the_model_entry_path_reaches_the_models_the_block_declares() -> None:
    """The dashboard counts them without knowing the schema."""

    document, _defaulted = serialise(
        "openclaw",
        [
            _model(),
            _model(
                gateway_id="anthropic/openrouter/opus",
                provider_model_ref="openrouter/opus",
                display_name="Opus",
            ),
        ],
    )

    assert len(model_entries("openclaw", document)) == 2
