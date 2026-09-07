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
from my_claude_code.application.catalogues import MODEL_ENTRY_PATHS, serialise
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

#: The model id Configure writes where an app's default model has to be named.
#: Only Codex takes it by necessity -- with a custom ``model_provider`` its
#: desktop app has no UI to pick a model, so a Configure that did not also name
#: one would leave a provider the user cannot select. Everywhere else it is
#: opt-in, because the default model is a preference MCC has no business
#: overwriting and is the value most likely to already hold something.
DEFAULT_MODEL_ID = "mcc/best"


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
    """Return what MCC writes where the app asks for a credential.

    Never a literal. Where the app resolves a reference form, that form is
    written with the variable's name substituted; where it takes only the name
    of a variable, the name goes in its own field and nothing goes here; and
    where it resolves nothing at all, the credential lives in a file MCC owns
    at mode 0600 and this returns nothing.
    """

    if spec.token_form is TokenForm.ENV_REFERENCE and spec.token_template:
        return spec.token_template.replace("{name}", spec.token_env_var)
    return ""


def owned_block(
    spec: DesktopAppSpec,
    models: Iterable[CatalogueModel],
    *,
    proxy_root_url: str,
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
        document: dict[str, Any] = {}
        for key, value in spec.sidecar.fields.items():
            if isinstance(value, str):
                document[key] = value.replace("{base_url}", base_url).replace(
                    "{token}", auth_token
                )
            else:
                document[key] = value
        if spec.sidecar.headers_key and spec.attribution_header_field:
            document[spec.sidecar.headers_key] = with_harness_id(
                {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}, spec.id
            )
        return document

    if not spec.catalogue_format_id:
        return None
    return owned_block(spec, models, proxy_root_url=proxy_root_url)


def overwritten_scalars(
    spec: DesktopAppSpec, *, set_default_model: bool = False
) -> dict[str, object]:
    """Return the top-level values Configure replaces, keyed as the spec names them.

    ``set_default_model`` is the opt-in checkbox. It is ignored where the spec
    already declares ``sets_default_model``, which is Codex alone and by
    necessity rather than by preference.
    """

    scalars: dict[str, object] = {}
    if spec.document is None:
        return scalars

    for key_path in spec.document.overwritten_keys:
        label = ".".join(key_path)
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
            case "roo-cline.autoImportSettingsPath":
                # Filled in by the caller, which is the only party that knows
                # where the sidecar landed on this machine.
                continue
            case _:
                continue
    return scalars
