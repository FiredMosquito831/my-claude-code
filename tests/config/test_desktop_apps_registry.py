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
        assert spec.provider.headers_key, spec.id
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
    assert with_sidecar == {"goose_desktop", "roo_code"}


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


def test_claude_desktop_is_an_instruction_card_with_no_guessed_path():
    """Anthropic documents the dialog, not a file. MCC does not invent one."""

    claude = DESKTOP_APPS_BY_ID["claude_desktop"]
    assert claude.status is DesktopAppStatus.INSTRUCTIONS_ONLY
    assert claude.document is None
    fields = dict(claude.instruction_fields)
    assert fields["Connection"] == "Gateway"
    assert "ANTHROPIC_AUTH_TOKEN" in fields["API key"]
    assert "mcc/best" in fields["Models"]
