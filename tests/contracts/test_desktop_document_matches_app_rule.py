"""What MCC writes, checked against what the application itself accepts.

Every desktop-app test MCC had until 6.83.0 asserted that the writer produced
what the registry declared -- 77 of them, all green, while every single app's
Configure was broken. ``tests/config/test_desktop_apply.py:691`` even asserted
that ``appliedId`` came out as ``mcc-9c2f4b18-…``: a perfect test of the exact
value that made Claude Desktop discard its whole configuration tier.

The invariant those tests were missing is the one here. A rule is extracted
from the application's own shipped code, vendored under
``tests/fixtures/app_rules/`` with the version it came from and the command
that read it (see ``versions.txt``), and MCC's *generated* document is checked
against it. ``tests/contracts/test_catalogue_schema_contract.py`` is the
precedent for apps that publish a schema; this is for the ones that do not.

Roo Code is the app this file starts with, because PR B is where MCC starts
writing it a real document. The rest of the servable rows are PR C's
(spec §3 fix 17), and this file is deliberately shaped so that adding one is
adding a fixture and a function.
"""

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import serialise
from my_claude_code.application.desktop_documents import (
    DEFAULT_MODEL_ID,
    overwritten_scalars,
    owned_block,
    sidecar_document,
)
from my_claude_code.config.desktop_apps import (
    DESKTOP_APPS,
    DesktopAppStatus,
    TokenForm,
    desktop_app,
)
from my_claude_code.config.harnesses import CRUSH_BASE_URL_SENTINEL
from tests.fixtures.live_catalogue import live_catalogue_models

RULES = Path(__file__).resolve().parents[1] / "fixtures" / "app_rules"

PROXY_ROOT = "http://127.0.0.1:8299"
TOKEN = "scratch-token-not-a-real-one"

MODELS: tuple[CatalogueModel, ...] = (
    CatalogueModel(
        gateway_id="mcc/best",
        provider_model_ref="openrouter/anthropic/claude-opus-5",
        display_name="MCC best",
        context_length=400_000,
    ),
    CatalogueModel(
        gateway_id="mcc/cheap",
        provider_model_ref="openrouter/anthropic/claude-haiku-4-5",
        display_name="MCC cheap",
        context_length=200_000,
    ),
)


def load_rule(name: str) -> dict[str, Any]:
    """Return one vendored rule, and prove the directory documents its source."""

    versions = (RULES / "versions.txt").read_text(encoding="utf-8", newline=None)
    assert name in versions, f"{name} is vendored without an extraction command"
    loaded = json.loads((RULES / name).read_text(encoding="utf-8", newline=None))
    assert isinstance(loaded, dict)
    return loaded


def test_every_vendored_rule_names_its_app_version_and_source():
    """A fixture without a version is a claim without a date."""

    files = sorted(path.name for path in RULES.glob("*.json"))
    assert files, "no app rules are vendored"
    for name in files:
        rule = load_rule(name)
        assert rule["app_version"], name
        assert rule["source_file"], name
        assert rule["extracted"], name


def roo_code_document() -> dict[str, Any]:
    document = sidecar_document(
        desktop_app("roo_code"),
        MODELS,
        proxy_root_url=PROXY_ROOT,
        auth_token=TOKEN,
    )
    assert document is not None, "Roo Code's Configure writes no document at all"
    return document


def test_roo_code_document_is_the_import_shape_the_extension_parses():
    rule = load_rule("roo-code-3.54.0.json")
    document = roo_code_document()

    for key in rule["import_document"]["required_keys"]:
        assert key in document, key
    allowed = set(rule["import_document"]["required_keys"]) | set(
        rule["import_document"]["optional_keys"]
    )
    assert set(document) <= allowed

    profiles = document["providerProfiles"]
    assert isinstance(profiles, dict)
    for key in rule["provider_profiles"]["required_keys"]:
        assert key in profiles, key
    assert profiles["currentApiConfigName"] in profiles["apiConfigs"], (
        "the current profile name has to be one of the profiles, or Roo "
        "imports a configuration nothing selects"
    )


def selected_profile() -> dict[str, Any]:
    profiles = roo_code_document()["providerProfiles"]
    profile = profiles["apiConfigs"][profiles["currentApiConfigName"]]
    assert isinstance(profile, dict)
    return profile


def test_roo_code_profile_uses_a_provider_the_extension_knows():
    rule = load_rule("roo-code-3.54.0.json")
    profile = selected_profile()
    assert profile["apiProvider"] in rule["api_provider_values"]
    assert (
        profile["apiProvider"] == rule["openai_compatible_profile"]["api_provider"]
    ), (
        "an OpenAI-compatible proxy is apiProvider 'openai' -- not "
        "'openai-native', which is OpenAI's own API, and not "
        "'openai-compatible', which is not a value the enum has at all"
    )


def test_roo_code_profile_carries_every_key_the_route_needs():
    rule = load_rule("roo-code-3.54.0.json")
    profile = selected_profile()
    for key in rule["openai_compatible_profile"]["keys_mcc_must_write"]:
        assert key in profile, key
    unknown = set(profile) - set(rule["openai_compatible_profile"]["known_keys"])
    assert not unknown, f"Roo strips keys it does not know: {sorted(unknown)}"
    suffix = rule["openai_compatible_profile"]["base_url_must_end_with"]
    assert profile["openAiBaseUrl"].endswith(suffix)
    assert profile["openAiBaseUrl"].startswith(PROXY_ROOT)
    assert profile["openAiModelId"] == DEFAULT_MODEL_ID


def test_roo_code_custom_model_info_carries_both_required_fields():
    """The two fields whose absence makes Roo drop the profile without a word."""

    rule = load_rule("roo-code-3.54.0.json")
    info = selected_profile()["openAiCustomModelInfo"]
    assert isinstance(info, dict)
    types = {"number": (int, float), "boolean": bool}
    for key in rule["openai_custom_model_info"]["required_keys"]:
        assert key in info, key
        expected = rule["openai_custom_model_info"]["types"][key]
        assert isinstance(info[key], types[expected]), key
    assert not isinstance(info["contextWindow"], bool)


def test_roo_code_document_is_strict_json_and_carries_the_literal_credential():
    """Roo's importer is ``JSON.parse``, and its key field takes no reference."""

    document = roo_code_document()
    text = json.dumps(document, indent=2)
    assert json.loads(text) == document
    profile = selected_profile()
    assert profile["openAiApiKey"] == TOKEN
    assert "{" not in profile["openAiApiKey"]
    assert "$" not in profile["openAiApiKey"]


def test_the_settings_key_mcc_merges_is_the_one_the_extension_reads():
    rule = load_rule("roo-code-3.54.0.json")
    spec = desktop_app("roo_code")
    assert spec.document is not None
    assert spec.sidecar_path_key == rule["settings_key"]
    assert spec.document.owned_key_path == (rule["settings_key"],)
    assert spec.document.overwritten_keys == ((rule["settings_key"],),)


@pytest.mark.parametrize("app_id", ["roo_code"])
def test_the_generated_document_is_not_empty(app_id: str):
    """The regression that produced this file: Configure wrote nothing at all.

    ``sidecar_document`` returned ``None`` for Roo Code -- no
    ``catalogue_format_id``, no ``provider.constants``, no declared fields --
    so the file MCC was supposed to own was never created, and the settings key
    naming it was skipped too. The card read ``installed`` after a successful
    Configure and the only change on disk was the user's ``settings.json``
    re-indented from four spaces to two.
    """

    spec = desktop_app(app_id)
    document = sidecar_document(
        spec, MODELS, proxy_root_url=PROXY_ROOT, auth_token=TOKEN
    )
    assert document


# ---------------------------------------------------------------------------
# Spec section 5, the binding rule: a desktop app keeps its Configure button
# only where MCC's *generated document* is validated in CI against a rule
# extracted from that application's own shipped code or published schema.
#
# Everything below runs over the **live-shaped** catalogue capture rather than
# the two-model toy above, because the shape that broke OpenCode in an earlier
# release -- a model publishing no parameter list, no context window and no
# price -- had never been serialised inside a test at all.
# ---------------------------------------------------------------------------


def live_models() -> tuple[CatalogueModel, ...]:
    """Return the live-shaped capture with MCC's five tier routes in front."""

    return (*MODELS, *live_catalogue_models())


def servable_app_ids() -> list[str]:
    return [
        spec.id
        for spec in DESKTOP_APPS
        if spec.status is DesktopAppStatus.SERVABLE and spec.document is not None
    ]


#: Which vendored rule covers which servable row. The test below fails when a
#: servable row is missing from here, which is the whole mechanism: a new app
#: cannot get a Configure button without an extracted rule, and an existing one
#: cannot keep it by having its fixture quietly deleted.
RULE_FOR_APP: dict[str, str] = {
    "codex_desktop": "codex-0.153.4.json",
    "goose_desktop": "goose-1.50.0.json",
    "opencode_desktop": "opencode-1.18.26.json",
    "crush_desktop": "crush-0.92.0.json",
    "roo_code": "roo-code-3.54.0.json",
    "commandcode": "command-code-1.50.0.json",
    "claude_desktop": "claude-desktop-1.46388.4.0.json",
}


def test_every_servable_app_has_a_vendored_rule():
    """The rule that stops the next one.

    Nine rows shipped a Configure button on the strength of documentation and
    every single one of them was broken. A row may now carry a button only
    with a fixture beside it naming the application version the rule was read
    from and the command that read it -- so a future row cannot be added the
    way these were.
    """

    missing = sorted(set(servable_app_ids()) - set(RULE_FOR_APP))
    assert not missing, (
        "these rows carry a Configure button with no rule extracted from the "
        f"application itself: {missing}. Either vendor one under "
        "tests/fixtures/app_rules/ or set the row to INSTRUCTIONS_ONLY with a "
        "dated instructions_reason."
    )
    stale = sorted(set(RULE_FOR_APP) - set(servable_app_ids()))
    assert not stale, f"rules mapped to rows that are no longer servable: {stale}"
    for app_id, name in RULE_FOR_APP.items():
        rule = load_rule(name)
        assert rule["app_id"] == app_id, name


def test_every_instructions_only_row_says_why_it_lost_its_button():
    """A demotion nobody can re-check is a decision nobody can reverse."""

    for spec in DESKTOP_APPS:
        if spec.status is not DesktopAppStatus.INSTRUCTIONS_ONLY:
            continue
        reason = spec.instructions_reason
        assert reason, f"{spec.id} is instructions-only with no reason"
        assert re.match(r"^\d{4}-\d{2}-\d{2}: ", reason), (
            f"{spec.id}'s instructions_reason has to open with the date it was "
            "decided, so it can be re-checked rather than believed"
        )
        assert spec.instruction_fields, (
            f"{spec.id} shows no button and no instructions, which is a card "
            "that tells the reader nothing"
        )


def generated_block(app_id: str) -> dict[str, Any]:
    spec = desktop_app(app_id)
    block = owned_block(
        spec, live_models(), proxy_root_url=PROXY_ROOT, auth_token=TOKEN
    )
    assert block is not None, f"{app_id} generates no block at all"
    return block


def generated_sidecar(app_id: str) -> dict[str, Any]:
    spec = desktop_app(app_id)
    document = sidecar_document(
        spec, live_models(), proxy_root_url=PROXY_ROOT, auth_token=TOKEN
    )
    assert document is not None, f"{app_id} generates no owned document at all"
    return document


# -- Codex ------------------------------------------------------------------


def test_codex_block_uses_only_fields_the_binary_declares():
    rule = load_rule("codex-0.153.4.json")
    block = generated_block("codex_desktop")
    unknown = set(block) - set(rule["provider_fields"])
    assert not unknown, f"not fields of Codex's ModelProviderInfo: {sorted(unknown)}"
    for key in rule["keys_mcc_must_write"]:
        assert key in block, key


def test_codex_wire_api_is_one_the_installed_codex_accepts():
    """One of the two assertions that would have prevented the bug report.

    ``wire_api = "chat"`` is not a provider that fails to answer: it is a
    ``config.toml`` that will not parse, so every unrelated Codex setting in
    the user's 25 KB file stops working too, in the CLI and in the desktop app.
    """

    rule = load_rule("codex-0.153.4.json")
    block = generated_block("codex_desktop")
    assert block["wire_api"] in rule["wire_api_accepted"]
    assert block["wire_api"] not in rule["wire_api_rejected"]


def test_codex_takes_the_literal_field_and_not_the_reference_one():
    rule = load_rule("codex-0.153.4.json")
    block = generated_block("codex_desktop")
    credential = rule["credential"]
    assert block[credential["literal_field"]] == TOKEN
    assert credential["reference_field"] not in block, (
        "env_key naming a variable nothing sets is a hard config-load error, "
        "not a fallback"
    )


def test_codex_base_url_carries_the_prefix_codex_appends_responses_to():
    rule = load_rule("codex-0.153.4.json")
    block = generated_block("codex_desktop")
    assert block["base_url"].endswith(rule["base_url_must_end_with"])
    assert block["base_url"].startswith(PROXY_ROOT)


# -- Claude Desktop ---------------------------------------------------------


def test_claude_desktop_entry_id_matches_the_app_regex():
    """The other assertion that would have prevented the bug report.

    ``tests/config/test_desktop_apply.py`` used to assert the *old* value --
    a perfect test of the exact string that made the app discard its entire
    local configuration tier.
    """

    rule = load_rule("claude-desktop-1.46388.4.0.json")
    spec = desktop_app("claude_desktop")
    assert spec.document is not None
    assert re.match(rule["entry_id_regex"], spec.document.match_value), (
        f"{spec.document.match_value} fails Claude Desktop's boot-time check"
    )
    scalars = overwritten_scalars(spec)
    assert re.match(rule["entry_id_regex"], str(scalars["appliedId"]))
    meta = rule["meta_document"]
    assert spec.document.overwritten_keys == ((meta["applied_id_key"],),)
    assert spec.document.owned_element_path == (meta["entries_key"],)
    assert spec.document.match_field == meta["entry_match_field"]


def test_claude_desktop_document_uses_only_keys_the_bundle_reads():
    rule = load_rule("claude-desktop-1.46388.4.0.json")
    document = generated_sidecar("claude_desktop")
    unknown = set(document) - set(rule["gateway_keys"])
    assert not unknown, f"keys the app's reader would drop: {sorted(unknown)}"
    assert document["inferenceProvider"] in rule["inference_provider_values"]
    assert (
        document["inferenceCredentialKind"] in rule["inference_credential_kind_values"]
    )
    assert "inferenceGatewayAuthScheme" not in document, (
        "the documented default is bearer and the working configuration on a "
        "real machine omits the key, so MCC omits it too"
    )


def test_claude_desktop_gets_the_literal_credential_and_the_root_url():
    rule = load_rule("claude-desktop-1.46388.4.0.json")
    document = generated_sidecar("claude_desktop")
    assert document[rule["credential"]["field"]] == TOKEN
    assert rule["credential"]["resolves_references"] is False
    assert document["inferenceGatewayBaseUrl"] == PROXY_ROOT, (
        "the app builds ${base}/v1/messages itself"
    )


# -- Goose ------------------------------------------------------------------


def test_goose_document_is_the_bare_struct_its_loader_deserialises():
    """Fix 11: one shape for one file, and it is Goose's own.

    Two shapes reached this file before 6.84.0 and *neither* was the struct
    ``load_custom_providers`` deserialises: the serialiser wrapped everything
    in ``custom_provider`` and the desktop writer used ``api_url`` and
    ``model_details``, which are not fields of it at all.
    """

    rule = load_rule("goose-1.50.0.json")
    document = generated_sidecar("goose_desktop")
    assert rule["document_is_bare_config"] is True
    assert "custom_provider" not in document
    for key in rule["required_keys"]:
        assert key in document, f"serde requires {key} and it is missing"
    allowed = set(rule["required_keys"]) | set(rule["optional_keys"])
    unknown = set(document) - allowed
    assert not unknown, f"not fields of DeclarativeProviderConfig: {sorted(unknown)}"
    assert document["engine"] in rule["engine_values"]


def test_goose_model_entries_carry_every_key_serde_requires():
    rule = load_rule("goose-1.50.0.json")
    document = generated_sidecar("goose_desktop")
    entries = document["models"]
    assert isinstance(entries, list) and entries
    allowed = set(rule["model_required_keys"]) | set(rule["model_optional_keys"])
    for entry in entries:
        assert isinstance(entry, dict)
        for key in rule["model_required_keys"]:
            # Present, not necessarily non-null: these are Option<T> without
            # #[serde(default)], so serde refuses a document that omits the
            # key and accepts one that sets it to null.
            assert key in entry, f"{entry.get('name')} omits {key}"
        unknown = set(entry) - allowed
        assert not unknown, f"not fields of Goose's ModelInfo: {sorted(unknown)}"


def test_goose_prices_are_per_token_the_way_goose_reads_them():
    """MCC's ladder is per million and Goose's field is per token."""

    document = generated_sidecar("goose_desktop")
    priced = [
        entry for entry in document["models"] if entry["input_token_cost"] is not None
    ]
    assert priced, "the live capture has to carry at least one priced model"
    for entry in priced:
        assert entry["input_token_cost"] < 1, (
            f"{entry['name']} prices its input at {entry['input_token_cost']} "
            "per token, which is a per-million number in a per-token field"
        )


def test_goose_writes_a_key_name_and_never_a_key():
    rule = load_rule("goose-1.50.0.json")
    document = generated_sidecar("goose_desktop")
    assert rule["credential"]["is_a_key_name_not_a_value"] is True
    assert document["api_key_env"] == desktop_app("goose_desktop").token_env_var
    assert TOKEN not in json.dumps(document), (
        "a literal written anywhere in this document is ignored -- Goose "
        "resolves api_key_env through its own secret store"
    )


def test_goose_splits_the_host_from_the_path_the_way_its_client_does():
    rule = load_rule("goose-1.50.0.json")
    document = generated_sidecar("goose_desktop")
    assert document["base_url"] == PROXY_ROOT
    assert rule["base_url_and_path"]["base_path_overrides_derived_path"] is True
    assert document["base_path"] == rule["base_url_and_path"]["derived_default"]


# -- Crush ------------------------------------------------------------------


def test_crush_desktop_and_cli_documents_agree():
    """Fix 14: MCC shipped two disagreeing documents for one application."""

    rule = load_rule("crush-0.92.0.json")
    block = generated_block("crush_desktop")
    cli_document, _defaulted = serialise("crush", live_models())
    cli_block = cli_document["providers"]["mcc"]

    assert block["type"] == rule["provider_type"]
    assert cli_block["type"] == rule["provider_type"]
    assert block["base_url"] == PROXY_ROOT
    assert cli_block["base_url"] == CRUSH_BASE_URL_SENTINEL, (
        "the CLI document carries a sentinel the launcher replaces with the "
        "same proxy root, so the two shapes are the same shape"
    )
    assert block["discover_models"] is False
    assert [entry["id"] for entry in block["models"]] == [
        entry["id"] for entry in cli_block["models"]
    ]


def test_crush_block_validates_against_the_binarys_own_schema():
    rule = load_rule("crush-0.92.0.json")
    schema = load_schema(rule["schema_file"])
    document = _embed(rule["embed_at"], generated_block("crush_desktop"))
    jsonschema.validate(document, schema)


def test_crush_takes_the_literal_because_it_cannot_resolve_mccs_reference():
    rule = load_rule("crush-0.92.0.json")
    block = generated_block("crush_desktop")
    assert rule["credential"]["accepts_literal"] is True
    assert block["api_key"] == TOKEN


# -- OpenCode ---------------------------------------------------------------


def test_opencode_block_validates_against_the_published_schema():
    rule = load_rule("opencode-1.18.26.json")
    schema = load_schema(rule["schema_file"])
    document = _embed(rule["embed_at"], generated_block("opencode_desktop"))
    jsonschema.validate(document, schema)


def test_opencode_options_carry_no_key_the_client_ignores():
    rule = load_rule("opencode-1.18.26.json")
    block = generated_block("opencode_desktop")
    options = block["options"]
    unknown = set(options) - set(rule["options_known_keys"])
    assert not unknown, (
        "ProviderConfig.options omits additionalProperties: false, so an "
        f"unknown key validates and is then dropped silently: {sorted(unknown)}"
    )
    assert "headers" not in options
    assert options["apiKey"] == TOKEN
    assert options["baseURL"].endswith(rule["base_url_must_end_with"])


# -- Command Code -----------------------------------------------------------


def test_command_code_writes_a_reference_it_refuses_to_take_a_literal_for():
    rule = load_rule("command-code-1.50.0.json")
    spec = desktop_app("commandcode")
    block = generated_block("commandcode")
    assert rule["credential"]["rejects_literals"] is True
    assert block["apiKey"] != TOKEN
    assert block["apiKey"] == f"${spec.token_env_var}"
    assert "$ENV_VAR" in rule["credential"]["accepted_reference_forms"]
    for key in rule["keys_mcc_must_write"]:
        assert key in block, key


def test_command_code_card_says_the_user_still_has_to_pick_the_provider():
    """Fix 16, taken as the spec's second option, and said out loud.

    The selection is two keys in a *different* document, and Command Code's
    own merge drops ``modelProvider`` from any layer that sets ``model``
    without it -- so the pair cannot be half-written. MCC does not reach into
    that file, so the card has to name both keys.
    """

    rule = load_rule("command-code-1.50.0.json")
    selection = rule["selection"]
    assert selection["mcc_writes_it"] is False
    notes = " ".join(desktop_app("commandcode").notes)
    for key in selection["keys"]:
        assert key in notes, f"the card never mentions {key}"
    assert selection["document"] in notes


# -- The credential rule, for every row -------------------------------------


@pytest.mark.parametrize("app_id", servable_app_ids())
def test_no_servable_app_writes_a_credential_reference_to_an_unset_variable(
    app_id: str,
):
    """Either MCC sets the variable, or the row must not use a reference form.

    Four rows named ``MCC_AUTH_TOKEN`` until 6.67.0 and nothing in MCC has
    ever set it, so two apps sent the unexpanded reference as a bearer token
    (401, measured) and one refused to load its configuration at all. A
    reference to a variable nobody exports is not protection.
    """

    spec = desktop_app(app_id)
    if spec.token_form is not TokenForm.ENV_REFERENCE:
        return
    assert spec.token_env_var_set_by_mcc, (
        f"{app_id} writes a reference to {spec.token_env_var}, which nothing "
        "in MCC sets. Use the literal in the app's own file, or a file MCC "
        "owns outright."
    )


@pytest.mark.parametrize("app_id", servable_app_ids())
def test_a_row_that_names_a_variable_can_report_it_unresolved(app_id: str):
    """Fix 8's registry half: the state exists only where it can be true."""

    spec = desktop_app(app_id)
    if not spec.token_env_var or spec.token_env_var_set_by_mcc:
        return
    assert spec.token_form in {TokenForm.ENV_ONLY, TokenForm.ENV_REFERENCE}, (
        f"{app_id} names {spec.token_env_var} for a token form that does not "
        "read one, so the card would ask for an export that changes nothing"
    )


def _embed(path: list[str], block: dict[str, Any]) -> dict[str, Any]:
    """Return the app's whole document with MCC's block at ``path``.

    A block alone validates against nothing: the schemas describe a config
    file. This is what spec section 5 means by extending the vendored-schema
    precedent "to the envelope".
    """

    document: dict[str, Any] = {}
    node = document
    for key in path[:-1]:
        node[key] = {}
        node = node[key]
    node[path[-1]] = block
    return document


def load_schema(name: str) -> dict[str, Any]:
    """Return one of the two applications' own published schemas."""

    schemas = RULES.parent / "schemas"
    loaded = json.loads((schemas / name).read_text(encoding="utf-8", newline=None))
    assert isinstance(loaded, dict)
    return loaded
