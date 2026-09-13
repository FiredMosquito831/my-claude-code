"""Turn one desktop app's spec plus the model catalogue into what MCC writes.

``config/desktop_apply.py`` is the mechanism -- probe, plan, apply, undo -- and
takes the block it writes as an argument, because ``config/`` is a leaf and may
not import the catalogue serialisers. This module is the other half: given a
:class:`~my_claude_code.config.desktop_apps.DesktopAppSpec` and the resolved
models, it produces the three things the mechanism needs.

``block``
    The subtree MCC owns inside the app's own document: the provider envelope
    named by the spec's ``DesktopProvider``, filled in with this install's
    proxy root, the app's own token-reference form and the model entries the
    catalogue serialiser produced.

``scalars``
    The top-level values MCC replaces -- Codex's ``model`` and
    ``model_provider``, Goose's ``GOOSE_PROVIDER``, Antigravity's
    ``modelProvider``. These are the keys the restore record remembers.

``sidecar``
    The whole document MCC owns for an app that keeps its provider in a file
    of its own, rather than as a key inside the user's.

**Why the envelope is built here rather than taken from the serialiser.** The
serialisers were written for the *launch* path, where MCC starts the CLI and
can hand it a base URL and a token through the child process's environment.
Two of them emit ``{env:...}`` references on exactly that basis, and Codex's
emits a bare model list with no envelope at all. A desktop application started
from Explorer inherits no such environment, so the envelope has to be in the
file. Taking the model entries from the serialiser and the envelope keys from
the spec keeps both facts in the place that already owns them, and keeps this
function free of any question about which app it is looking at.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import (
    MODEL_ENTRY_PATHS,
    serialise,
    serialise_sidecar,
)
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from my_claude_code.config.desktop_apps import (
    CLAUDE_DESKTOP_CONFIG_ID,
    DESKTOP_PROVIDER_ID,
    BaseUrlShape,
    DesktopAppSpec,
    TokenForm,
)
from my_claude_code.config.harness_attribution import with_harness_id
from my_claude_code.config.harness_base_url import root_base_url, v1_base_url
from my_claude_code.config.harnesses import MCC_HARNESS_ID_SENTINEL
from my_claude_code.core.client_fingerprint import HARNESS_HEADER
from my_claude_code.core.tier_refs import DEFAULT_TIER, tier_ref

#: The model id Configure writes where an app's default model has to be named.
#: Only Codex takes it by necessity -- with a custom ``model_provider`` its
#: desktop app has no UI to pick a model, so a Configure that did not also name
#: one would leave a provider the user cannot select. Everywhere else it is
#: opt-in, because the default model is a preference MCC has no business
#: overwriting and is the value most likely to already hold something.
#:
#: It is :data:`~my_claude_code.core.tier_refs.DEFAULT_TIER`'s alias rather
#: than the first tier in the picker order, so that adding a tier above Fable
#: cannot silently change what Configure writes into another application's
#: settings file.
DEFAULT_MODEL_ID = tier_ref(DEFAULT_TIER)

#: The placeholder a ``DesktopSidecar.fields`` value uses to say "the model
#: list goes here". Unlike ``{base_url}`` and ``{token}`` it is never
#: interpolated into a longer string: what replaces it is a list, so the whole
#: value has to be the token and nothing else.
MODELS_TOKEN = "{models}"


def base_url_for(spec: DesktopAppSpec, proxy_root_url: str) -> str:
    """Return the URL this app wants written, in the shape it wants it.

    Four shapes reach a document, and they exist because four genuinely
    different things are wanted -- not because four apps are quirky. An
    Anthropic client appends ``/v1/messages`` itself and so takes the root; an
    OpenAI-shaped one wants the ``/v1`` prefix already present; Google's
    protocol lives under ``/v1beta``; and Goose keeps the path in a second
    field of its own, so the URL half is again the root.
    """

    match spec.base_url_shape:
        case BaseUrlShape.ROOT | BaseUrlShape.SPLIT_HOST_PATH:
            return root_base_url(proxy_root_url)
        case BaseUrlShape.V1:
            return v1_base_url(proxy_root_url)
        case BaseUrlShape.V1BETA:
            return f"{root_base_url(proxy_root_url)}/v1beta"


def token_reference(spec: DesktopAppSpec) -> str:
    """Return the *reference* MCC writes where the app asks for a credential.

    A reference only, and never a literal: this string is rendered on the card
    and printed by ``mcc-apps``. Where the app resolves a reference form to a
    variable MCC really sets, that form is written with the name substituted.
    Every other form -- the name-of-a-variable field, the literal in the app's
    own document, the file MCC owns outright -- returns nothing here, because
    what goes into the document in those cases is either handled elsewhere or
    is a secret.

    Note what is no longer produced: a reference to
    :data:`~my_claude_code.config.desktop_apps.DESKTOP_TOKEN_ENV_VAR` for an
    app that writes one into its file. Four rows did that until 6.67.0, naming
    a variable nothing has ever set, and every one of them failed on the wire.
    """

    if spec.token_form is TokenForm.ENV_REFERENCE and spec.token_template:
        return spec.token_template.replace("{name}", spec.token_env_var)
    return ""


def writes_literal_credential(spec: DesktopAppSpec) -> bool:
    """Return whether MCC puts the literal token in the app's own document.

    True for exactly the apps that resolve no usable reference, and the flag
    the writer reads to tighten that document's mode. It is deliberately a
    property of the declared token form rather than a scan of the block for
    something token-shaped: a scan cannot tell a credential from a model id.
    """

    return spec.token_form is TokenForm.LITERAL_IN_APP_FILE


def owned_block(
    spec: DesktopAppSpec,
    models: Iterable[CatalogueModel],
    *,
    proxy_root_url: str,
    auth_token: str = "",
) -> dict[str, Any] | None:
    """Return the subtree MCC writes into the app's document, or None.

    The envelope comes from the spec's :class:`DesktopProvider`, which names
    the keys this app uses; the models come from the catalogue serialiser,
    through ``model_entries`` so this function does not have to know where any
    given format keeps them. Nothing here asks which app it is looking at.

    ``None`` for an app whose whole footprint is its scalars -- Antigravity
    takes one switch and two environment variables and validates ``--model``
    against its own catalogue before sending anything, so an ``mcc/*`` model
    list would only produce an error message.
    """

    if not spec.catalogue_format_id:
        # An app whose owned block carries no model list at all. Claude
        # Desktop's element of ``_meta.json.entries`` is the case: a label and
        # the id the merge engine writes from the spec, and nothing else -- its
        # models live in the sidecar's own document, or come from the app's own
        # discovery call. Returning ``None`` here would leave the entry out of
        # the index and the app would never offer MCC's configuration.
        if not spec.provider.constants:
            return None
        return dict(spec.provider.constants)

    document, _defaulted = serialise(spec.catalogue_format_id, models)
    document = with_harness_id(document, spec.id)
    entries = _models_node(spec.catalogue_format_id, document)

    provider = spec.provider
    block: dict[str, Any] = dict(provider.constants)

    if provider.base_url_key:
        _set_dotted(block, provider.base_url_key, base_url_for(spec, proxy_root_url))

    if provider.env_name_key and spec.token_env_var:
        block[provider.env_name_key] = spec.token_env_var
    reference = token_reference(spec)
    if provider.api_key_key and reference:
        _set_dotted(block, provider.api_key_key, reference)
    elif provider.api_key_key and writes_literal_credential(spec) and auth_token:
        # The literal, for an app that cannot resolve a reference as MCC ships
        # it. ``config/desktop_apply`` tightens the document to 0600 where the
        # OS allows it, and the plan diff masks the field before rendering.
        # The credential reaches this function only from the admin route, and
        # only for a spec that declares this token form.
        _set_dotted(block, provider.api_key_key, auth_token)

    if provider.headers_key and spec.attribution_header_field:
        _set_dotted(
            block,
            provider.headers_key,
            with_harness_id({HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}, spec.id),
        )

    if provider.models_key:
        _set_dotted(block, provider.models_key, entries)

    return block


def _models_node(format_id: str, document: Mapping[str, Any]) -> Any:
    """Return a serialised document's model list in the shape its app wants.

    Deliberately *not* ``catalogues.model_entries``. That helper answers "how
    many models, and what is in each", so it flattens an id-keyed mapping down
    to its values -- which is right for counting and wrong for writing, because
    for OpenCode and Command Code the key **is** the model id. Taking the node
    verbatim means each app gets the shape its own serialiser already produces,
    list or mapping, and nothing here has to know which.

    The defaulted-fields record is dropped: it belongs at the document root
    where the dashboard reads it, not inside a provider's model list.
    """

    node: Any = document
    for key in MODEL_ENTRY_PATHS[format_id]:
        if not isinstance(node, Mapping):
            return []
        node = node.get(key)
    if isinstance(node, Mapping):
        return {key: value for key, value in node.items() if key != DEFAULTED_KEY}
    return list(node) if isinstance(node, list) else []


def _set_dotted(block: dict[str, Any], key: str, value: Any) -> None:
    """Set a possibly-dotted key, creating the levels above it."""

    parts = key.split(".")
    node = block
    for part in parts[:-1]:
        nested = node.get(part)
        if not isinstance(nested, dict):
            nested = {}
        node[part] = nested
        node = nested
    node[parts[-1]] = value


def sidecar_document(
    spec: DesktopAppSpec,
    models: Iterable[CatalogueModel],
    *,
    proxy_root_url: str,
    auth_token: str = "",
) -> dict[str, Any] | None:
    """Return the whole document MCC owns for an app that keeps one.

    Two shapes, both declared rather than branched on. Most owned files are a
    provider envelope plus a serialised model catalogue, which is exactly
    :func:`owned_block`. Claude Desktop's is a fixed set of settings --
    ``inferenceProvider``, the gateway URL, the credential, the credential
    kind, the discovery switch -- declared as ``DesktopSidecar.fields`` with
    ``{base_url}`` and ``{token}`` where this install's values go.

    ``auth_token`` is a *literal* credential and reaches only this function and
    only for a sidecar declaring ``holds_credential``; the file it lands in is
    written 0600 by ``config/desktop_apply._write_owned_file``, and the plan
    diff masks the field before any of it is rendered.
    """

    if spec.sidecar is None:
        return None

    if spec.sidecar.fields:
        base_url = base_url_for(spec, proxy_root_url)
        entries = (
            serialise_sidecar(spec.sidecar.models_format_id, models)
            if spec.sidecar.models_format_id
            else None
        )
        document = _substitute(
            dict(spec.sidecar.fields),
            base_url=base_url,
            auth_token=auth_token,
            entries=entries,
        )
        if not isinstance(document, dict):  # pragma: no cover - fields is a mapping
            return None
        if spec.sidecar.headers_key and spec.attribution_header_field:
            document[spec.sidecar.headers_key] = with_harness_id(
                {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}, spec.id
            )
        return document

    if not spec.catalogue_format_id:
        return None
    return owned_block(spec, models, proxy_root_url=proxy_root_url)


def _substitute(
    value: Any,
    *,
    base_url: str,
    auth_token: str,
    entries: object | None,
) -> Any:
    """Return a declared sidecar value with this install's values filled in.

    Recursive, because a declared document is not always flat. Claude Desktop's
    is six top-level keys; Roo Code's is the export format its own importer
    parses, where the base URL and the credential sit three levels down in
    ``providerProfiles.apiConfigs.<name>``. Walking the structure keeps that a
    property of the registry row rather than of a branch in here -- the same
    promise :class:`DesktopProvider` makes for the block.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _substitute(
                item, base_url=base_url, auth_token=auth_token, entries=entries
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _substitute(item, base_url=base_url, auth_token=auth_token, entries=entries)
            for item in value
        ]
    if value == MODELS_TOKEN:
        # The whole value is the token, so it is replaced rather than
        # interpolated: what goes here is a list, not a string.
        return entries if entries is not None else []
    if isinstance(value, str):
        return (
            value.replace("{base_url}", base_url)
            .replace("{token}", auth_token)
            .replace("{default_model}", DEFAULT_MODEL_ID)
        )
    return value


def overwritten_scalars(
    spec: DesktopAppSpec, *, set_default_model: bool = False, sidecar_path: str = ""
) -> dict[str, object]:
    """Return the top-level values Configure replaces, keyed as the spec names them.

    ``set_default_model`` is the opt-in checkbox. It is ignored where the spec
    already declares ``sets_default_model``, which is Codex alone and by
    necessity rather than by preference.

    ``sidecar_path`` is where the file MCC owns outright landed on *this*
    machine, which only the caller can know. The spec names the key it belongs
    in (:attr:`DesktopAppSpec.sidecar_path_key`); an app that declares one and
    a caller that passes nothing leaves the key out entirely rather than
    writing an empty path, because a hook pointing at "" is worse than no hook.
    """

    scalars: dict[str, object] = {}
    if spec.document is None:
        return scalars

    for key_path in spec.document.overwritten_keys:
        label = ".".join(key_path)
        if label and label == spec.sidecar_path_key:
            if sidecar_path:
                scalars[label] = sidecar_path
            continue
        match label:
            case "model":
                if spec.sets_default_model or set_default_model:
                    scalars[label] = DEFAULT_MODEL_ID
            case "model_provider" | "GOOSE_PROVIDER":
                scalars[label] = DESKTOP_PROVIDER_ID
            case "modelProvider":
                # Antigravity's switch. Measured 2026-09-07: with it set, agy
                # speaks the public Gemini API at GOOGLE_GEMINI_BASE_URL;
                # without it, it goes to Google's own backend regardless.
                scalars[label] = "gemini"
            case "appliedId":
                # Claude Desktop's configuration library loads whichever
                # document ``appliedId`` names at launch. It is the one foreign
                # key MCC touches here, and the reason this card has a restore
                # record at all: a user who had authored their own gateway
                # configuration had it applied, and Undo puts that id back.
                scalars[label] = CLAUDE_DESKTOP_CONFIG_ID
            case _:
                continue
    return scalars
