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
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.desktop_documents import (
    DEFAULT_MODEL_ID,
    sidecar_document,
)
from my_claude_code.config.desktop_apps import desktop_app

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
