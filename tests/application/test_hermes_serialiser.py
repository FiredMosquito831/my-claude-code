"""Hermes' ``providers.mcc``: the five fields it reads, and nothing else.

Read out of the published ``hermes-agent`` 0.19.0 wheel. The shape is fixed by
the spec (``specs/INVESTIGATION-HARNESS-MODEL-LISTS.md`` §3.2) and by what the
shipped code consumes:

* ``api`` -- a provider entry with no parseable URL is dropped whole
  (``hermes_cli/config.py:5055-5075``);
* ``key_env`` -- the *name* of an environment variable, read with
  ``os.environ[key_env]`` at request time (``hermes_cli/config.py:5088-5091``);
* ``transport`` -- ``openai_chat`` | ``anthropic_messages`` | ``codex_responses``
  (``hermes_cli/providers.py:38``), and ``openai_chat`` is the one whose client
  appends ``chat/completions`` to the base URL;
* ``default_model`` and ``models`` -- the per-model mapping, whose only key the
  shipped code reads is ``context_length``
  (``get_custom_provider_context_length``, ``hermes_cli/config.py:5414-5477``).

Everything else a catalogue *could* carry -- an output ceiling, prices, tool
and vision flags, a reasoning toggle -- has nowhere honest to go here, and the
tests below fail if any of it appears.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.catalogues.hermes import (
    CLI_REQUIRED_KEYS,
    PROVIDER_REQUIRED_KEYS,
    PROVIDER_TRANSPORT,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harness_attribution import with_harness_id
from my_claude_code.config.harness_config_merge import with_base_url
from my_claude_code.config.harnesses import (
    HERMES_API_KEY_ENV,
    HERMES_BASE_URL_SENTINEL,
    HERMES_PROVIDER_ID,
    MCC_HARNESS_ID_SENTINEL,
    harness_spec,
)
from my_claude_code.core.client_fingerprint import HARNESS_HEADER
from my_claude_code.core.reasoning import ReasoningEffort

FORMAT = "hermes"
ROOT = "http://127.0.0.1:8082"

#: The provider block's keys, exactly. Anything added here is a key Hermes'
#: normaliser either warns about or ignores, so the list is the contract.
EXPECTED_PROVIDER_KEYS = frozenset(
    {"api", "key_env", "transport", "default_model", "models", "extra_headers"}
)

#: Keys a serialiser for a richer schema would write and this one must not.
FORBIDDEN_KEYS = (
    "maxTokens",
    "max_tokens",
    "max_output_tokens",
    "contextWindow",
    "cost",
    "input_price",
    "output_price",
    "cache_read_price",
    "cache_write_price",
    "input",
    "supports_tools",
    "supportsTools",
    "reasoning",
    "reasoning_effort",
    "answer_in_reasoning",
    "prompt_caching",
    "_mcc_defaulted",
)


def _provider(document: dict[str, Any]) -> dict[str, Any]:
    block = document["providers"][HERMES_PROVIDER_ID]
    assert isinstance(block, dict)
    return block


def _models() -> tuple[CatalogueModel, ...]:
    return (
        CatalogueModel(
            gateway_id="anthropic/openrouter/configured",
            provider_model_ref="openrouter/configured",
            display_name="Configured",
            context_length=400_000,
            max_output_tokens=32_000,
            supports_vision=True,
            input_price=1.25,
            output_price=10.0,
            reasoning=ModelReasoningCapability(
                can_reason=True,
                supports_effort_control=True,
                supported_efforts=frozenset({ReasoningEffort.HIGH}),
            ),
            is_primary_route=True,
        ),
        CatalogueModel(
            gateway_id="anthropic/openrouter/silent",
            provider_model_ref="openrouter/silent",
            display_name="Silent",
        ),
    )


def test_the_provider_block_is_exactly_the_fields_hermes_reads() -> None:
    document, _defaulted = serialise(FORMAT, _models())

    assert set(document) == {"providers"}
    block = _provider(document)
    assert set(block) == EXPECTED_PROVIDER_KEYS
    assert block["api"] == HERMES_BASE_URL_SENTINEL
    assert block["key_env"] == HERMES_API_KEY_ENV
    assert block["transport"] == PROVIDER_TRANSPORT == "openai_chat"
    assert block["default_model"] == "openrouter/configured"


def test_no_field_hermes_cannot_act_on_reaches_the_document() -> None:
    document, _defaulted = serialise(FORMAT, _models())

    serialised = repr(document)
    for key in FORBIDDEN_KEYS:
        assert key not in serialised, key
    # Prices and the output ceiling are known for the first model, so their
    # absence is a decision rather than a gap in the fixture.
    block = _provider(document)
    assert "400000" in repr(block["models"])
    assert "32000" not in repr(block)
    assert "1.25" not in repr(block)


def test_the_model_list_is_the_dict_form_keyed_by_the_bare_ref() -> None:
    """Hermes' normaliser converts a list to a dict; MCC writes the dict."""

    document, _defaulted = serialise(FORMAT, _models())

    models = _provider(document)["models"]
    assert isinstance(models, dict)
    assert list(models) == ["openrouter/configured", "openrouter/silent"]
    # The bare provider/model ref, never the Claude Code gateway id or a
    # ``claude-3-freecc-no-thinking/`` prefix.
    assert all("anthropic/" not in model_id for model_id in models)
    for meta in models.values():
        assert isinstance(meta, dict)


def test_a_known_context_window_is_written_and_an_unknown_one_is_omitted() -> None:
    document, defaulted = serialise(FORMAT, _models())

    models = _provider(document)["models"]
    assert models["openrouter/configured"] == {"context_length": 400_000}
    assert models["openrouter/silent"] == {}
    assert defaulted.as_document() == {"openrouter/silent": ["context_length"]}


@pytest.mark.parametrize("unknown", [None, 0])
def test_an_unknown_or_zero_window_is_never_written_as_a_number(
    unknown: int | None,
) -> None:
    """A zero window is a claim, not a fact -- and Hermes has a real fallback.

    With no ``context_length`` Hermes runs its own detection; with ``0`` it
    would believe the model can hold nothing at all.
    """

    model = CatalogueModel(
        gateway_id="anthropic/openrouter/zero",
        provider_model_ref="openrouter/zero",
        display_name="Zero",
        context_length=unknown,
    )

    document, defaulted = serialise(FORMAT, [model])

    assert _provider(document)["models"]["openrouter/zero"] == {}
    assert "0" not in repr(_provider(document)["models"])
    assert defaulted.as_document() == {"openrouter/zero": ["context_length"]}


def test_the_default_model_is_the_configured_route_when_there_is_one() -> None:
    free = CatalogueModel(
        gateway_id="anthropic/openrouter/free",
        provider_model_ref="openrouter/free",
        display_name="Free",
    )
    configured = CatalogueModel(
        gateway_id="anthropic/openrouter/configured",
        provider_model_ref="openrouter/configured",
        display_name="Configured",
        is_primary_route=True,
    )

    document, _ = serialise(FORMAT, [free, configured])
    assert _provider(document)["default_model"] == "openrouter/configured"

    document, _ = serialise(FORMAT, [free])
    assert _provider(document)["default_model"] == "openrouter/free"


def test_the_credential_is_a_variable_name_and_never_a_value() -> None:
    document, _ = serialise(FORMAT, _models())

    block = _provider(document)
    assert block["key_env"] == "MCC_HERMES_API_KEY"
    # The value that variable will hold is a proxy token; the document carries
    # the name only. Anything that looked like a secret would fail here.
    assert "Bearer" not in repr(document)
    assert "proxy-token" not in repr(document)
    assert "sk-" not in repr(document)


def test_the_transport_and_the_base_url_shape_come_from_the_registry() -> None:
    """``openai_chat`` means ``<root>/v1``; the two cannot disagree.

    ``with_base_url`` is the one thing that turns the serialiser's sentinel
    into a real address, and Hermes' OpenAI client appends
    ``/chat/completions`` to whatever it is given -- so the resolved value must
    end in ``/v1`` or every request 404s.
    """

    spec = harness_spec("hermes")
    assert spec.catalogue is not None and spec.catalogue.merge is not None
    merge = spec.catalogue.merge
    assert merge.base_url_key == "api"
    assert merge.base_url_sentinel == HERMES_BASE_URL_SENTINEL
    assert PROVIDER_TRANSPORT == "openai_chat"

    document, _ = serialise(FORMAT, _models())
    resolved = with_base_url(
        _provider(document),
        ROOT,
        key=merge.base_url_key,
        sentinel=merge.base_url_sentinel,
    )
    assert resolved["api"] == f"{ROOT}/v1"


def test_the_header_hermes_documents_carries_mccs_label() -> None:
    document, _ = serialise(FORMAT, _models())

    block = _provider(document)
    assert block["extra_headers"] == {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}

    resolved = with_harness_id(document, "hermes")
    assert _provider(resolved)["extra_headers"] == {HARNESS_HEADER: "hermes"}


def test_hermes_requires_a_base_url_on_the_provider_and_nothing_per_model() -> None:
    """The two requirement sets are different questions, so both are stated."""

    assert frozenset({"api"}) == PROVIDER_REQUIRED_KEYS
    assert frozenset() == CLI_REQUIRED_KEYS

    document, _ = serialise(FORMAT, _models())
    assert set(_provider(document)) >= PROVIDER_REQUIRED_KEYS
    for entry in model_entries(FORMAT, document):
        assert set(entry) <= {"context_length"}


def test_a_no_thinking_twin_is_projected_onto_its_plain_ref() -> None:
    twin = CatalogueModel(
        gateway_id="claude-3-freecc-no-thinking/openrouter/plain",
        provider_model_ref="openrouter/plain",
        display_name="Plain",
        force_no_thinking=True,
    )

    document, _ = serialise(FORMAT, [twin])

    assert list(_provider(document)["models"]) == ["openrouter/plain"]


def test_a_batch_ref_never_reaches_hermes_picker() -> None:
    models = (
        CatalogueModel(
            gateway_id="anthropic/openrouter/m",
            provider_model_ref="openrouter/m",
            display_name="m",
        ),
        CatalogueModel(
            gateway_id="anthropic/openrouter/m:batch",
            provider_model_ref="openrouter/m:batch",
            display_name="m:batch",
        ),
    )

    document, _ = serialise(FORMAT, models)

    assert list(_provider(document)["models"]) == ["openrouter/m"]


# ------------------------------------------------------- the extracted rule
#
# The rule beside this test was read out of the published wheel, not out of the
# vendor's documentation site: the site describes the wizard, the wheel is what
# reads the file MCC writes. `versions.txt` records the download, the extraction
# commands and why the wheel was never installed (requires_python <3.14).

RULES = Path(__file__).resolve().parents[1] / "fixtures" / "app_rules"
RULE_NAME = "hermes-agent-0.19.0.json"


def _rule() -> dict[str, Any]:
    versions = (RULES / "versions.txt").read_text(encoding="utf-8", newline=None)
    assert RULE_NAME in versions, "the rule is vendored without an extraction record"
    loaded = json.loads((RULES / RULE_NAME).read_text(encoding="utf-8", newline=None))
    assert isinstance(loaded, dict)
    return loaded


def test_the_generated_provider_block_uses_only_keys_the_wheel_declares() -> None:
    rule = _rule()
    document, _defaulted = serialise(FORMAT, _models())
    block = _provider(document)

    known = set(rule["provider_keys"])
    invented = sorted(set(block) - known)
    assert not invented, f"keys Hermes' normaliser warns about and ignores: {invented}"
    for key in rule["required_provider_keys"]:
        assert key in block, key
    assert block["transport"] in rule["transport"]["values"]
    # A variable *name*, never a value: the launcher supplies the value in the
    # child process and the rule records the form.
    assert block["key_env"] == "MCC_HERMES_API_KEY"
    assert block["key_env"].upper() == block["key_env"]


def test_the_generated_model_entries_use_only_the_one_key_the_wheel_consumes() -> None:
    rule = _rule()
    document, _defaulted = serialise(FORMAT, _models())

    consumed = set(rule["per_model_keys_consumed"])
    forbidden = set(rule["per_model_keys_mcc_must_not_write"])
    for model_id, meta in _provider(document)["models"].items():
        assert set(meta) <= consumed, model_id
        assert not set(meta) & forbidden, model_id
    assert consumed == {"context_length"}


def test_the_rule_records_the_two_sites_this_serialiser_rests_on() -> None:
    """A rule that does not cite its source cannot be re-checked."""

    rule = _rule()

    assert rule["app_version"] == "0.19.0"
    assert rule["app_id"] == "hermes"
    assert (
        "agent/agent_init.py:1729" in rule["per_model_keys_consumed"]["context_length"]
    )
    assert "hermes_cli/config.py:5015-5037" in rule["provider_keys_evidence"]
    assert rule["credential"]["field"] == "key_env"
    assert rule["rewrites_its_own_document"] is True
