"""Contracts every desktop-app row has to satisfy, whoever adds it.

These are the rules that keep the registry declarative. A row that breaks one
of them is a row that would have needed a branch in the writer, and the point
of the registry is that there are no branches in the writer.
"""

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import SERIALISERS
from my_claude_code.application.desktop_documents import (
    overwritten_scalars,
    owned_block,
    sidecar_document,
    token_reference,
)
from my_claude_code.config.desktop_apps import (
    CLAUDE_DESKTOP_GATEWAY_KEYS,
    DESKTOP_APPS,
    DESKTOP_APPS_BY_ID,
    DesktopAppSpec,
    DesktopAppStatus,
    TokenForm,
)
from my_claude_code.config.document_codecs import DocumentFormat
from my_claude_code.config.harnesses import HARNESSES_WITHOUT_ATTRIBUTION_HEADER

SERVABLE = [spec for spec in DESKTOP_APPS if spec.status is DesktopAppStatus.SERVABLE]

MODELS = (
    CatalogueModel(
        gateway_id="mcc/best",
        provider_model_ref="openrouter/anthropic/claude-sonnet-4-5",
        display_name="MCC best",
        context_length=200000,
    ),
)


def ids(specs) -> list[str]:
    return [spec.id for spec in specs]


def test_every_id_is_unique():
    assert len(DESKTOP_APPS_BY_ID) == len(DESKTOP_APPS)


@pytest.mark.parametrize("spec", DESKTOP_APPS, ids=ids(DESKTOP_APPS))
def test_every_spec_declares_a_doc_url(spec: DesktopAppSpec):
    """A row is a claim about somebody else's software; it cites its source."""

    assert spec.doc_url.startswith("https://"), spec.id


@pytest.mark.parametrize(
    "spec",
    [s for s in DESKTOP_APPS if s.status is DesktopAppStatus.NOT_ROUTABLE],
    ids=ids([s for s in DESKTOP_APPS if s.status is DesktopAppStatus.NOT_ROUTABLE]),
)
def test_a_not_routable_spec_states_a_dated_reason(spec: DesktopAppSpec):
    """The doctrine at ``harnesses.py``: a refusal carries a date to re-check.

    "Can I use X through this?" deserves an answer a reader can verify and
    challenge, not silence -- and a reason without a date decays invisibly,
    which is exactly what happened to the Antigravity entry this release
    corrects.
    """

    assert spec.unavailable_reason, spec.id
    assert "20" in spec.unavailable_reason, f"{spec.id} reason carries no date"


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_a_servable_spec_declares_a_document_and_a_detector(spec: DesktopAppSpec):
    assert spec.document is not None, spec.id
    assert spec.detect is not None and spec.detect.markers, spec.id


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_no_spec_writes_a_literal_token(spec: DesktopAppSpec):
    """Never a literal where the app takes a reference; a 0600 file otherwise.

    The reference forms are checked by shape rather than by name: a value that
    is neither an app's own reference syntax nor an environment-variable name
    is a credential in a user's document, which is the one thing this whole
    feature is not allowed to do.
    """

    reference = token_reference(spec)
    match spec.token_form:
        case TokenForm.ENV_REFERENCE:
            assert reference, spec.id
            assert spec.token_env_var in reference or "${input:" in reference, spec.id
        case TokenForm.ENV_NAME_FIELD:
            assert not reference, spec.id
            assert spec.provider.env_name_key, spec.id
        case TokenForm.ENV_ONLY:
            # Nothing is written anywhere: the app reads its own environment
            # or keyring. The card names the variable and the next status poll
            # reports whether it took.
            assert not reference, spec.id
            assert spec.token_env_var, spec.id
            assert not spec.provider.api_key_key, spec.id
        case TokenForm.MCC_OWNED_FILE:
            assert spec.sidecar is not None, spec.id
        case TokenForm.IN_APP:
            assert spec.document is None or spec.status is not DesktopAppStatus.SERVABLE


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_a_spec_without_a_header_field_writes_no_attribution_header(
    spec: DesktopAppSpec,
):
    """Goose and Antigravity have nowhere to put one, and must not fake it."""

    block = owned_block(spec, MODELS, proxy_root_url="http://127.0.0.1:8082")
    if spec.attribution_header_field:
        # The header goes wherever this app's settings live: in the provider
        # entry for an app whose block carries them, or in the file MCC owns
        # outright for one whose block is only an index entry -- Claude
        # Desktop, whose ``inferenceCustomHeaders`` sits beside the gateway URL
        # in the sidecar rather than in ``_meta.json``.
        assert spec.provider.headers_key or (
            spec.sidecar is not None and spec.sidecar.headers_key
        ), spec.id
        return
    assert "x-mcc-harness" not in str(block), spec.id


def test_the_harnesses_without_a_header_still_name_goose():
    """The desktop Goose document inherits the CLI Goose's own omission."""

    assert "goose" in HARNESSES_WITHOUT_ATTRIBUTION_HEADER


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_a_catalogue_format_is_one_that_exists(spec: DesktopAppSpec):
    if spec.catalogue_format_id:
        assert spec.catalogue_format_id in SERIALISERS, spec.id


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_the_owned_block_carries_the_proxy_url_in_the_shape_the_app_wants(
    spec: DesktopAppSpec,
):
    block = owned_block(spec, MODELS, proxy_root_url="http://127.0.0.1:8082")
    if block is None or not spec.provider.base_url_key:
        return
    assert "127.0.0.1:8082" in str(block), spec.id
    assert "base-url.mcc.invalid" not in str(block), spec.id


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_a_json_array_spec_names_the_field_ownership_is_matched_on(
    spec: DesktopAppSpec,
):
    assert spec.document is not None
    if spec.document.document_format is DocumentFormat.JSON_ARRAY:
        assert spec.document.match_field and spec.document.match_value, spec.id
    else:
        assert spec.document.owned_key_path or spec.document.overwritten_keys, spec.id


def test_only_codex_sets_a_default_model_without_being_asked():
    """Everywhere else the default model is a preference MCC does not own."""

    assert [spec.id for spec in DESKTOP_APPS if spec.sets_default_model] == [
        "codex_desktop"
    ]


def test_the_opt_in_checkbox_adds_a_default_model_where_one_is_declared():
    codex = DESKTOP_APPS_BY_ID["codex_desktop"]
    assert overwritten_scalars(codex)["model"] == "mcc/best"
    assert overwritten_scalars(codex, set_default_model=False)["model"] == "mcc/best"


def test_a_sidecar_is_only_declared_where_the_app_reads_a_file_of_its_own():
    with_sidecar = {spec.id for spec in DESKTOP_APPS if spec.sidecar is not None}
    assert with_sidecar == {"goose_desktop", "roo_code", "claude_desktop"}


def test_the_goose_sidecar_is_the_whole_provider_document():
    goose = DESKTOP_APPS_BY_ID["goose_desktop"]
    document = sidecar_document(goose, MODELS, proxy_root_url="http://127.0.0.1:8082")
    assert document is not None
    assert document["api_url"] == "http://127.0.0.1:8082"
    assert document["base_path"] == "v1/chat/completions"


def test_antigravity_is_servable_again_and_says_what_changed():
    """The 1.0.14 refusal was re-measured on 2026-09-07 and no longer holds."""

    agy = DESKTOP_APPS_BY_ID["antigravity"]
    assert agy.status is DesktopAppStatus.SERVABLE
    assert agy.base_url_shape.value == "v1beta"
    assert overwritten_scalars(agy) == {"modelProvider": "gemini"}
    assert any("2026-09-07" in note for note in agy.notes)


def test_claude_desktop_writes_the_file_anthropic_documents():
    """The 6.55.0 card said no file existed. Anthropic's MDM page names one.

    ``%LOCALAPPDATA%\\Claude-3p\\configLibrary\\`` is the documented local
    configuration source, and this machine already had a gateway configuration
    authored in it. MCC owns a whole document there and merges exactly one
    foreign key of ``_meta.json``.
    """

    claude = DESKTOP_APPS_BY_ID["claude_desktop"]
    assert claude.status is DesktopAppStatus.SERVABLE
    assert claude.document is not None
    assert claude.document.display_path.endswith("configLibrary\\_meta.json")
    assert claude.document.owned_element_path == ("entries",)
    assert claude.document.overwritten_keys == (("appliedId",),)
    assert claude.sidecar is not None
    assert claude.sidecar.holds_credential
    # No card may still claim Anthropic documents no persistence path.
    assert not any("does not guess a persistence path" in n for n in claude.notes)


def test_claude_desktop_writes_exactly_the_six_documented_gateway_keys():
    claude = DESKTOP_APPS_BY_ID["claude_desktop"]
    document = sidecar_document(
        claude, MODELS, proxy_root_url="http://127.0.0.1:8082", auth_token="scratch"
    )
    assert document is not None
    assert set(document) == set(CLAUDE_DESKTOP_GATEWAY_KEYS)
    assert document["inferenceProvider"] == "gateway"
    # The proxy ROOT: the gateway must serve POST /v1/messages, which the app
    # appends itself.
    assert document["inferenceGatewayBaseUrl"] == "http://127.0.0.1:8082"
    assert document["inferenceCredentialKind"] == "static"
    assert document["modelDiscoveryEnabled"] is True
    assert document["inferenceCustomHeaders"] == {"x-mcc-harness": "claude_desktop"}
    assert document["inferenceGatewayApiKey"] == "scratch"


def test_claude_desktops_instruction_fallback_uses_the_dialogs_own_labels():
    """Kept for a machine with no configuration library, or a managed one."""

    fields = dict(DESKTOP_APPS_BY_ID["claude_desktop"].instruction_fields)
    assert fields["Inference provider"] == "Gateway"
    assert fields["Gateway base URL"] == "{root}"
    assert fields["Credential kind"] == "Static API key"
    # ANTHROPIC_AUTH_TOKEN is MCC's own proxy token, and naming it as the
    # thing to type is right; what was wrong in 6.55.0 was the live
    # "not exported yet" warning beside it, for a variable this flow never
    # reads. ``token_env_var`` is empty now, so no card renders one.
    assert DESKTOP_APPS_BY_ID["claude_desktop"].token_env_var == ""


def test_extension_cards_require_the_extension_not_the_editor():
    """%APPDATA%\\Code\\User proves VS Code, and says nothing about an extension."""

    for app_id, publisher in (
        ("vscode_copilot", "github.copilot"),
        ("roo_code", "rooveterinaryinc.roo-cline-"),
    ):
        detect = DESKTOP_APPS_BY_ID[app_id].detect
        assert detect is not None, app_id
        markers = detect.markers
        assert markers, app_id
        for marker in markers:
            assert marker.glob, app_id
            assert "extensions" in marker.relative_parts, app_id
        assert any(
            publisher in part for marker in markers for part in marker.relative_parts
        ), app_id


def test_opencode_targets_the_only_path_opencode_documents():
    """https://opencode.ai/docs/config names ~/.config/opencode and no other."""

    opencode = DESKTOP_APPS_BY_ID["opencode_desktop"]
    assert opencode.document is not None
    assert opencode.document.display_path == "~/.config/opencode/opencode.json"
    assert not any("APPDATA" in path.env_vars for path in opencode.document.paths)
