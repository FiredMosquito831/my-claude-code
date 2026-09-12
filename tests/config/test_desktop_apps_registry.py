"""Contracts every desktop-app row has to satisfy, whoever adds it.

These are the rules that keep the registry declarative. A row that breaks one
of them is a row that would have needed a branch in the writer, and the point
of the registry is that there are no branches in the writer.
"""

import json
from collections.abc import Mapping

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import SERIALISERS
from my_claude_code.application.desktop_documents import (
    overwritten_scalars,
    owned_block,
    sidecar_document,
    token_reference,
)
from my_claude_code.config.desktop_apply import _SECRET_KEYS, _mask
from my_claude_code.config.desktop_apps import (
    CLAUDE_DESKTOP_GATEWAY_KEYS,
    DESKTOP_APPS,
    DESKTOP_APPS_BY_ID,
    DesktopAppSpec,
    DesktopAppStatus,
    TokenForm,
)
from my_claude_code.config.document_codecs import DocumentFormat, mask_json_text
from my_claude_code.config.harnesses import (
    COMMANDCODE_API_KEY_ENV,
    HARNESSES_WITHOUT_ATTRIBUTION_HEADER,
)

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
    assert spec.detect is not None, spec.id
    assert spec.detect.markers or spec.detect.binaries, spec.id


#: Variables MCC itself sets somewhere a client can see them. There are
#: exactly two, and neither is set at *user* scope -- MCC has never written
#: ``HKCU\Environment`` and does not start doing so here. They are set in the
#: environment of a child process MCC launches, which is why the only rows
#: allowed to reference them are rows whose app MCC launches.
VARIABLES_MCC_SETS = frozenset({COMMANDCODE_API_KEY_ENV})


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_no_servable_app_references_a_variable_nobody_sets(spec: DesktopAppSpec):
    """The invariant 6.67.0 exists to add, and the one the old wording missed.

    "Never a literal where the app takes a reference" was the rule until
    6.67.0, and four rows kept it perfectly while failing completely: they
    wrote a reference to ``MCC_AUTH_TOKEN``, a variable nothing in MCC has ever
    set. A desktop app started from Explorer inherits only the user
    environment, so what actually went on the wire was the unexpanded reference
    as a bearer token -- HTTP 401, measured -- or, for Codex, a refusal to load
    the configuration file at all.

    So the test is no longer "is it a reference" but "can the app resolve it as
    MCC ships it": a reference may name only a variable MCC really sets, or a
    placeholder the app resolves *itself* (VS Code's ``${input:}`` prompts and
    keeps the answer in its own SecretStorage). Anything else must be a literal
    in a file, and then it is the writer's job to restrict that file.
    """

    reference = token_reference(spec)
    match spec.token_form:
        case TokenForm.ENV_REFERENCE:
            assert reference, spec.id
            if "${input:" in reference:
                assert not spec.token_env_var, spec.id
            else:
                assert spec.token_env_var in VARIABLES_MCC_SETS, spec.id
                assert spec.token_env_var in reference, spec.id
        case TokenForm.LITERAL_IN_APP_FILE:
            # No reference at all, and a field to put the literal in.
            assert not reference, spec.id
            assert not spec.token_env_var, spec.id
            assert spec.provider.api_key_key, spec.id
            assert not spec.provider.env_name_key, spec.id
        case TokenForm.ENV_NAME_FIELD:
            assert not reference, spec.id
            assert spec.provider.env_name_key, spec.id
            assert spec.token_env_var in VARIABLES_MCC_SETS, spec.id
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


def test_claude_desktop_writes_the_keys_the_working_configuration_carries():
    """Reconciled against a configuration proven to work, not against prose.

    Every expectation below is a key-for-key comparison with the entry the user
    built by hand and confirmed working on 2026-09-09, read from a backup on
    2026-09-10 (``specs/CLAUDE-DESKTOP-CONFIG-REFERENCE.md``). That comparison
    is what turned up the three defects this release fixes on this card:
    ``inferenceModels`` missing entirely, ``modelDiscoveryEnabled`` hard-coded
    the other way, and an ``inferenceCustomHeaders`` value no run of the app
    has ever been watched accepting.
    """

    claude = DESKTOP_APPS_BY_ID["claude_desktop"]
    # The tier aliases, which is what ``build_catalogue_models`` appends to
    # every catalogue and what Claude Desktop's picker is filled from. A
    # ``provider/model`` ref is deliberately in the list too and deliberately
    # not in the output.
    models = (
        *MODELS,
        CatalogueModel(
            gateway_id="anthropic/mcc/best",
            provider_model_ref="mcc/best",
            display_name="Best (openrouter/anthropic/claude-sonnet-4-5)",
            context_length=1_000_000,
        ),
    )
    document = sidecar_document(
        claude, models, proxy_root_url="http://127.0.0.1:8082", auth_token="scratch"
    )
    assert document is not None
    assert set(document) == set(CLAUDE_DESKTOP_GATEWAY_KEYS)
    assert document["inferenceProvider"] == "gateway"
    # The proxy ROOT: the gateway must serve POST /v1/messages, which the app
    # appends itself.
    assert document["inferenceGatewayBaseUrl"] == "http://127.0.0.1:8082"
    assert document["inferenceCredentialKind"] == "static"
    assert document["inferenceGatewayApiKey"] == "scratch"
    # Off, with the models named. On, with nothing named, made the picker's
    # contents depend on a network call finishing and on
    # ``settings.harness_tier_aliases`` being set.
    assert document["modelDiscoveryEnabled"] is False
    assert "inferenceCustomHeaders" not in document
    models = document["inferenceModels"]
    assert isinstance(models, list) and models
    for entry in models:
        assert set(entry) == {
            "name",
            "labelOverride",
            "supports1m",
            "prefer1m",
            "anthropicFamilyTier",
            "isFamilyDefault",
        }
        # The wire ids MCC really routes, not a display name MCC invented.
        assert entry["name"].startswith("mcc/")


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


def _credential_keys(spec: DesktopAppSpec) -> set[str]:
    """Return the key names this row can put a real credential under.

    Derived from the row rather than listed by hand, which is the whole point:
    a denylist of secret-looking names is only ever as good as its last entry,
    and the entry that was missing was found by looking at a rendered plan.
    """

    keys: set[str] = set()
    if spec.provider.api_key_key and spec.token_form in {
        TokenForm.LITERAL_IN_APP_FILE,
        TokenForm.ENV_REFERENCE,
    }:
        keys.add(spec.provider.api_key_key.split(".")[-1])
    if spec.sidecar is not None and spec.sidecar.holds_credential:
        keys |= _token_fields(spec.sidecar.fields)
    return keys


def _token_fields(node: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, Mapping):
        for key, value in node.items():
            if value == "{token}":
                keys.add(str(key))
                continue
            keys |= _token_fields(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _token_fields(item)
    return keys


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
def test_every_key_that_can_hold_a_credential_is_masked(spec: DesktopAppSpec):
    """A missed mask is a leaked key, in a diff rendered into a browser.

    The one that was missed: Roo Code's ``openAiApiKey``, which is not
    ``apikey`` as a whole-key match, so the plan preview for a real Configure
    rendered MCC's proxy token in full. Deriving the set from the registry
    means the next row to declare a credential field cannot ship without it.
    """

    for key in _credential_keys(spec):
        assert key.lower().replace("-", "_") in _SECRET_KEYS, f"{spec.id}: {key}"


def test_a_rendered_plan_for_every_app_hides_the_token(tmp_path):
    """The same property, end to end, through the masker the card renders."""

    token = "a-token-that-must-not-appear-anywhere"
    for spec in SERVABLE:
        document = sidecar_document(
            spec, MODELS, proxy_root_url="http://127.0.0.1:8299", auth_token=token
        )
        block = owned_block(
            spec, MODELS, proxy_root_url="http://127.0.0.1:8299", auth_token=token
        )
        for payload in (document, block):
            if payload is None:
                continue
            rendered = json.dumps(_mask(payload), indent=2)
            assert token not in rendered, spec.id
            assert (
                mask_json_text(json.dumps(payload, indent=2), _SECRET_KEYS).count(token)
                == 0
            ), spec.id


@pytest.mark.parametrize("spec", SERVABLE, ids=ids(SERVABLE))
@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_every_servable_row_is_detectable_on_every_platform(
    spec: DesktopAppSpec, platform: str
):
    """A row MCC will write for has to be a row MCC can *find* first.

    Detection became a question about the program in 6.83.0, and the first cut
    of it left Claude Desktop with Windows and macOS markers and nothing at all
    for Linux -- which the tests could not see on Windows and CI found
    immediately. Either an executable name, which is looked up on PATH wherever
    the app runs, or a marker path this platform can resolve.
    """

    assert spec.detect is not None
    if spec.detect.binaries:
        return
    applicable = [
        marker
        for marker in spec.detect.markers
        if not marker.platforms or platform in marker.platforms
    ]
    assert applicable, f"{spec.id} cannot be detected on {platform}"
