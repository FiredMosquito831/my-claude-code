"""The desktop applications MCC can point at itself, declared as data.

``config/harnesses.py`` describes a *CLI*: a binary MCC launches, with aliases,
an install hint, passthrough rules and a launcher command. A desktop app has
none of those. It is already running, or it is not; MCC never starts it, never
stops it, and reaches it only through the one file it reads at startup. So this
is a separate registry rather than six permanently-``None`` fields on
:class:`~my_claude_code.config.harnesses.HarnessSpec` and a ``command``
property that would have to lie.

Everything an app-specific behaviour would otherwise be is declared here:
where its file lives per OS, what format the file is, which keys MCC owns,
which keys MCC *overwrites* (the ones the two-mode Undo has to remember), what
shape its base URL takes, how it wants a token spelled, whether it has a place
for an attribution header, and whether it needs a restart. The engine in
``config/desktop_apply.py`` reads only those fields, so adding an app is adding
a row and its tests -- never a branch.

**Three statuses, and the doctrine behind the third.**

``SERVABLE``
    MCC can write the app's file and the app will then route through MCC.
    These get a Configure button.

``INSTRUCTIONS_ONLY``
    The app *can* be pointed at MCC, but not by editing a file MCC can find.
    Claude Desktop is the case: its gateway settings are entered in a dialog
    and Anthropic documents the dialog, not a persistence path. The card
    carries the exact values with copy buttons; MCC does not guess a path.

``NOT_ROUTABLE``
    The app cannot be pointed at MCC at all, and ``unavailable_reason`` says
    why, with the date and version it was measured -- the same doctrine as
    ``config/harnesses.py:531-540``, which exists so "can I use X through
    this?" gets a dated answer a reader can re-check rather than silence.

**Base-URL shapes are not a hack.** Five genuinely different things are wanted
by the six v1 apps: the proxy root (Anthropic Messages), root + ``/v1``
(everything OpenAI-shaped), root + ``/v1beta`` (Google's protocol, which is
what Antigravity turned out to speak), a host/path *split* (Goose puts the host
in one variable and the path in another), and one full endpoint URL (Junie).
Collapsing them would mean a per-app branch in the writer, which is the thing
this file exists to prevent.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from my_claude_code.config.document_codecs import DocumentFormat
from my_claude_code.config.harnesses import HarnessProtocol


class DesktopAppStatus(StrEnum):
    """Whether MCC can configure an app, only describe it, or neither."""

    SERVABLE = "servable"
    INSTRUCTIONS_ONLY = "instructions_only"
    NOT_ROUTABLE = "not_routable"


class BaseUrlShape(StrEnum):
    """What the app wants written where it asks for a base URL."""

    #: The proxy root, e.g. ``http://127.0.0.1:8082``. Anthropic Messages.
    ROOT = "root"
    #: Root plus ``/v1``. Every OpenAI-shaped client.
    V1 = "v1"
    #: Root plus ``/v1beta``. Google's protocol.
    V1BETA = "v1beta"
    #: Host and path in separate fields. Goose.
    SPLIT_HOST_PATH = "split_host_path"


class TokenForm(StrEnum):
    """How the app wants a credential spelled, never a literal where it takes a reference."""

    #: The app has a field for the *name* of an environment variable. Best
    #: case: the token never enters the file at all. Codex's ``env_key``.
    ENV_NAME_FIELD = "env_name_field"
    #: The app expands a reference inside the value: ``$VAR``, ``{env:VAR}``,
    #: ``${input:...}``. The template carries the exact spelling.
    ENV_REFERENCE = "env_reference"
    #: The app reads the credential from its own environment or keyring and
    #: has no field for it in any document. MCC writes nothing at all and the
    #: card says which variable to export. Goose (whose config keys are
    #: *ignored* in favour of its keyring) and Antigravity (GEMINI_API_KEY).
    ENV_ONLY = "env_only"
    #: The app resolves no reference and MCC owns a whole file of its own,
    #: written 0600, so the token never reaches a document the user edits.
    #: The Kimi precedent.
    MCC_OWNED_FILE = "mcc_owned_file"
    #: The token is entered by a human in the app's own UI. MCC shows it.
    IN_APP = "in_app"


class DesktopAppState(StrEnum):
    """What a probe found. These are exactly the badges the card renders."""

    NOT_INSTALLED = "not_installed"
    NOT_ROUTABLE = "not_routable"
    INSTALLED = "installed"
    CONFIGURED = "configured"
    DRIFTED = "drifted"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class DesktopPath:
    """One file, resolved the way the *app* resolves it, per platform.

    ``env_vars`` is the app's own lookup order, not Python's. Codex reads
    ``CODEX_HOME`` first and only then the home directory; ``Path.home()`` on
    Windows reads ``USERPROFILE`` while several of these apps prefer ``HOME``.
    Following the app is the only way the file MCC writes is the file the app
    reads -- the mistake would be invisible, showing up as a model picker that
    never lists MCC.
    """

    #: Environment variables naming the *directory*, in the app's order.
    env_vars: tuple[str, ...]
    #: Path parts under whichever of those resolved, or under home.
    relative_parts: tuple[str, ...]
    #: Platforms this path applies to. Empty means every platform.
    platforms: tuple[str, ...] = ()
    #: Resolve under MCC's own configuration directory rather than the user's
    #: home. Set for a file MCC owns outright, so that the directory's name
    #: stays the single responsibility of ``config/paths`` -- a contract test
    #: asserts no other module spells it, precisely so a rename can never
    #: strand a file MCC wrote.
    in_mcc_config_dir: bool = False


@dataclass(frozen=True, slots=True)
class DesktopDetect:
    """Marker paths any one of which proves the app is installed.

    ``shutil.which`` cannot see a desktop app -- that is the structural bug in
    ``api/admin_harness_routes.py:300`` for anything without a binary on PATH
    -- so detection is a path question. A marker is a directory the app creates
    on first run, never the config file itself: an app that has run but never
    been configured must read as *installed*, not as missing.
    """

    markers: tuple[DesktopPath, ...]


@dataclass(frozen=True, slots=True)
class DesktopDocument:
    """The document MCC merges into, and exactly what it owns inside it."""

    #: Where the file is, per platform.
    paths: tuple[DesktopPath, ...]
    #: How it is spelled to a human, on the card and in the docs.
    display_path: str
    #: The document's shape.
    document_format: DocumentFormat
    #: The one subtree MCC owns, outermost first. Empty for ``JSON_ARRAY``,
    #: which owns an element rather than a key.
    owned_key_path: tuple[str, ...] = ()
    #: For ``JSON_ARRAY``: the field and value identifying MCC's element.
    match_field: str = ""
    match_value: str = ""
    #: Scalars MCC *replaces* rather than creates. These are the keys the
    #: restore record remembers and the RESTORE undo mode puts back. Every one
    #: of them is a value a user may legitimately have set to something else.
    overwritten_keys: tuple[tuple[str, ...], ...] = ()
    #: Suffix of the one-time copy taken before MCC's first edit.
    backup_suffix: str = ".mcc-backup"
    #: Whether Configure may create the file when it is absent.
    create_if_missing: bool = True


@dataclass(frozen=True, slots=True)
class DesktopSidecar:
    """A whole file MCC owns outright beside the document it merges into.

    Goose is the reason this exists: keys placed in Goose's own ``config.yaml``
    are *ignored*, and its custom providers are separate JSON documents in a
    directory. Roo Code is the second: it publishes an import hook, so MCC can
    own the settings file entirely and touch the user's only to name it.

    A file MCC owns whole needs no merge, no backup and no restore record --
    Undo deletes it. It is also the only place a literal token may ever land,
    and then only at mode 0600.
    """

    paths: tuple[DesktopPath, ...]
    display_path: str
    document_format: DocumentFormat
    #: Whether this file may contain a literal credential. Only true where the
    #: app resolves no reference form at all.
    holds_credential: bool = False


@dataclass(frozen=True, slots=True)
class DesktopProvider:
    """The keys one app uses inside its provider entry, named as data.

    The catalogue serialisers in ``application/catalogues/`` were written for
    the *launch* path, where MCC starts the CLI and can hand it a base URL and
    a token through the child process's environment. Two of them emit
    ``{env:...}`` references on that basis and Codex's emits a bare model list
    with no envelope at all. A desktop application started from Explorer
    inherits no such environment, so the envelope has to be in the file -- and
    naming its keys here, rather than branching on the app in the writer, is
    what keeps the promise that adding an app is adding a row.

    An empty field name means "this app has no such key", and the writer omits
    it rather than inventing one.
    """

    #: Where the endpoint URL goes, e.g. ``base_url`` or ``options.baseURL``.
    #: Dotted, and created as needed.
    base_url_key: str = ""
    #: Where the credential reference goes. Empty where the app takes only the
    #: *name* of an environment variable instead -- see ``env_name_key``.
    api_key_key: str = ""
    #: Where the *name* of an environment variable goes, for an app that has a
    #: field for it. Codex's ``env_key``: the best case in the set, because the
    #: token then never enters the document at all.
    env_name_key: str = ""
    #: Where the request-header map goes, for attribution.
    headers_key: str = ""
    #: Where the model list goes. The *shape* is not declared here: each
    #: catalogue serialiser already emits its own app's shape, list or id-keyed
    #: mapping, and the writer carries that node across verbatim.
    models_key: str = ""
    #: Constant fields the app requires in the entry, e.g. Crush's
    #: ``{"type": "openai-compat"}``. Every value here came from that app's own
    #: documentation and is quoted in the registry entry that declares it.
    constants: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DesktopAppSpec:
    """One desktop application MCC knows how to talk about."""

    id: str
    display_name: str
    summary: str
    status: DesktopAppStatus
    #: The official page every claim in this row came from.
    doc_url: str
    #: Required when NOT_ROUTABLE, and dated, so it can be re-checked.
    unavailable_reason: str = ""
    detect: DesktopDetect | None = None
    document: DesktopDocument | None = None
    sidecar: DesktopSidecar | None = None
    protocol: HarnessProtocol = HarnessProtocol.OPENAI_CHAT_COMPLETIONS
    base_url_shape: BaseUrlShape = BaseUrlShape.V1
    token_form: TokenForm = TokenForm.ENV_REFERENCE
    #: How the app spells a reference to an environment variable, with
    #: ``{name}`` where the variable's name goes. Empty for the other forms.
    token_template: str = ""
    #: The environment variable the user must export for the app to see the
    #: token. MCC never sets it at user scope -- the card says what to export
    #: and the next status poll verifies it.
    token_env_var: str = ""
    #: The key holding a request-header map, or empty where the app has none.
    attribution_header_field: str = ""
    #: Whether the app must be restarted. Stated on the card, never performed.
    restart_required: bool = True
    #: The catalogue serialiser supplying this app's model list.
    catalogue_format_id: str = ""
    #: The keys this app's provider entry uses. See :class:`DesktopProvider`.
    provider: DesktopProvider = field(default_factory=DesktopProvider)
    #: Whether Configure also sets the app's default model to ``mcc/best``.
    #: True only where *not* doing it leaves a provider the user cannot pick.
    sets_default_model: bool = False
    #: How a human opens the app. MCC never runs it.
    open_command: str = ""
    #: Extra lines the card shows verbatim, for anything not mechanisable.
    notes: tuple[str, ...] = ()
    #: For INSTRUCTIONS_ONLY: the field/value pairs a human types, in order.
    instruction_fields: tuple[tuple[str, str], ...] = ()

    @property
    def owned_key_label(self) -> str:
        """Return the owned key as a reader would write it, e.g. ``provider.mcc``."""

        if self.document is None:
            return ""
        if self.document.document_format is DocumentFormat.JSON_ARRAY:
            return f'{self.document.match_field} == "{self.document.match_value}"'
        return ".".join(self.document.owned_key_path)


#: The provider id MCC claims inside every desktop app's document. One word,
#: the same everywhere, so a user grepping their configs for "mcc" finds every
#: trace of it in one search -- and so Undo has one thing to look for.
DESKTOP_PROVIDER_ID = "mcc"

#: The display name MCC's entry carries where an app shows a provider name to
#: a human, and the value ownership is matched on in VS Code's keyless array.
DESKTOP_PROVIDER_LABEL = "My Claude Code"

#: The environment variable every v1 app is told to read its token from. One
#: name rather than one per app: the user exports it once, and a card that
#: verifies "is it exported?" is checking a single fact.
DESKTOP_TOKEN_ENV_VAR = "MCC_AUTH_TOKEN"


#: Why ``antigravity`` is now a servable desktop app rather than a refusal.
#:
#: The 6.32.0 verdict was measured against ``agy`` 1.0.14, a JS bundle, and it
#: has decayed. Re-measured 2026-09-07 against the installed Go build
#: (187,601,560 bytes, mtime 2026-09-02), with ``HOME``/``USERPROFILE``
#: redirected to a scratch directory and a logging HTTP server standing in for
#: MCC. With ``~/.gemini/antigravity-cli/settings.json`` holding
#: ``{"modelProvider": "gemini"}``, ``GEMINI_API_KEY`` set to a scratch string
#: and ``GOOGLE_GEMINI_BASE_URL`` pointed at the scratch server, ``agy -p``
#: sent::
#:
#:     POST /v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse
#:     x-goog-api-key: <the scratch string>
#:
#: which is the *public* Gemini API, and exactly the surface MCC has served at
#: ``/v1beta`` since it added ``api/gemini_routes.py``. No new inbound door.
#:
#: Two limits the card has to state, both measured rather than inferred:
#:
#: * ``modelProvider`` is the switch. With it omitted, agy sent nothing to the
#:   scratch server and answered from the Google Code Assist backend instead --
#:   on Windows its credential store survives a ``HOME`` redirect.
#: * agy validates ``--model`` against its own list *before* any request, so
#:   the ``mcc/*`` tiers cannot be named: ``model mcc/best is not recognized as
#:   a known model or custom model in settings``. Models are chosen by agy's
#:   own ids and MCC routes them through the resolution ladder.
ANTIGRAVITY_ROUTING_NOTE = (
    "Verified 2026-09-07 against the installed agy Go build: with "
    'modelProvider set to "gemini", agy sends '
    "POST /v1beta/models/{model}:streamGenerateContent with x-goog-api-key, "
    "which is the public Gemini API MCC already serves at /v1beta. Models are "
    "named with agy's own ids -- it rejects mcc/* before sending anything -- "
    "and MCC routes them through the resolution ladder."
)


def _windows_appdata(*parts: str) -> DesktopPath:
    return DesktopPath(
        env_vars=("APPDATA",), relative_parts=parts, platforms=("win32",)
    )


def _windows_localappdata(*parts: str) -> DesktopPath:
    return DesktopPath(
        env_vars=("LOCALAPPDATA",), relative_parts=parts, platforms=("win32",)
    )


def _home(
    *parts: str, env_vars: tuple[str, ...] = ("HOME", "USERPROFILE")
) -> DesktopPath:
    return DesktopPath(env_vars=env_vars, relative_parts=parts)


DESKTOP_APPS: tuple[DesktopAppSpec, ...] = (
    DesktopAppSpec(
        id="codex_desktop",
        display_name="Codex desktop",
        summary=(
            "OpenAI's Codex desktop app and CLI share one config document and "
            "one provider table, so pointing either here points both."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://github.com/openai/codex/blob/main/docs/config.md",
        detect=DesktopDetect(
            markers=(
                _windows_localappdata("OpenAI", "Codex"),
                _home(".codex", env_vars=("CODEX_HOME",)),
            )
        ),
        document=DesktopDocument(
            paths=(
                DesktopPath(env_vars=("CODEX_HOME",), relative_parts=("config.toml",)),
                _home(".codex", "config.toml"),
            ),
            display_path="~/.codex/config.toml",
            document_format=DocumentFormat.TOML,
            owned_key_path=("model_providers", DESKTOP_PROVIDER_ID),
            overwritten_keys=(("model_provider",), ("model",)),
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        token_form=TokenForm.ENV_NAME_FIELD,
        token_env_var=DESKTOP_TOKEN_ENV_VAR,
        attribution_header_field="http_headers",
        catalogue_format_id="codex",
        # Codex's ``model_providers.<id>`` entry, from its own config.md:
        # ``name``, ``base_url``, ``env_key`` (the *name* of the variable
        # holding the key -- so no token reaches the file), ``wire_api`` and
        # ``http_headers``.
        provider=DesktopProvider(
            base_url_key="base_url",
            env_name_key="env_key",
            headers_key="http_headers",
            models_key="",
            constants={"name": DESKTOP_PROVIDER_LABEL, "wire_api": "chat"},
        ),
        sets_default_model=True,
        open_command="codex",
        notes=(
            "Codex has no UI for picking a model once a custom model_provider "
            'is set, so Configure also writes model = "mcc/best". That is the '
            "one value here MCC overwrites by necessity, and the reason the "
            "restore mode of Undo exists.",
        ),
    ),
    DesktopAppSpec(
        id="goose_desktop",
        display_name="Goose desktop",
        summary=(
            "Block's Goose reads custom providers as separate JSON documents, "
            "so MCC owns a whole file and touches the user's config.yaml only "
            "to name it."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://block.github.io/goose/docs/getting-started/providers",
        detect=DesktopDetect(
            markers=(
                _windows_appdata("Block", "goose"),
                _home(".config", "goose"),
            )
        ),
        document=DesktopDocument(
            paths=(
                _windows_appdata("Block", "goose", "config", "config.yaml"),
                _home(".config", "goose", "config.yaml"),
            ),
            display_path="%APPDATA%\\Block\\goose\\config\\config.yaml",
            document_format=DocumentFormat.YAML,
            owned_key_path=(),
            overwritten_keys=(("GOOSE_PROVIDER",),),
        ),
        sidecar=DesktopSidecar(
            paths=(
                _windows_appdata(
                    "Block", "goose", "config", "custom_providers", "mcc.json"
                ),
                _home(".config", "goose", "custom_providers", "mcc.json"),
            ),
            display_path=(
                "%APPDATA%\\Block\\goose\\config\\custom_providers\\mcc.json"
            ),
            document_format=DocumentFormat.JSON,
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.SPLIT_HOST_PATH,
        token_form=TokenForm.ENV_ONLY,
        token_env_var=DESKTOP_TOKEN_ENV_VAR,
        attribution_header_field="",
        catalogue_format_id="goose",
        # Goose's custom-provider file, which MCC owns whole. ``api_url`` is
        # the host half of the split and ``base_path`` the other; no key field,
        # because a key written here is ignored -- Goose reads its keyring.
        provider=DesktopProvider(
            base_url_key="api_url",
            models_key="model_details",
            constants={
                "name": DESKTOP_PROVIDER_ID,
                "display_name": DESKTOP_PROVIDER_LABEL,
                "base_path": "v1/chat/completions",
                "api_key_env": DESKTOP_TOKEN_ENV_VAR,
            },
        ),
        open_command="goose",
        notes=(
            "Goose ignores API keys written into config.yaml -- it reads them "
            "from its keyring, or from secrets.yaml when GOOSE_DISABLE_KEYRING "
            "is set. A literal there would silently not work, so MCC writes "
            "none.",
            "Goose publishes no per-provider request-header field, so it is "
            "attributed by user-agent rather than x-mcc-harness.",
        ),
    ),
    DesktopAppSpec(
        id="opencode_desktop",
        display_name="OpenCode desktop",
        summary=(
            "OpenCode's desktop build reads the same provider map as its CLI "
            "and expands {env:...} references, so no token reaches the file."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://opencode.ai/docs/config",
        detect=DesktopDetect(
            markers=(
                _windows_appdata("ai.opencode.desktop"),
                _windows_appdata("opencode"),
                _home(".config", "opencode"),
            )
        ),
        document=DesktopDocument(
            paths=(
                DesktopPath(
                    env_vars=("OPENCODE_CONFIG_DIR",),
                    relative_parts=("opencode.json",),
                ),
                _windows_appdata("opencode", "opencode.json"),
                _home(".config", "opencode", "opencode.json"),
            ),
            display_path="%APPDATA%\\opencode\\opencode.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=("provider", DESKTOP_PROVIDER_ID),
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        token_form=TokenForm.ENV_REFERENCE,
        token_template="{env:{name}}",
        token_env_var=DESKTOP_TOKEN_ENV_VAR,
        attribution_header_field="headers",
        catalogue_format_id="opencode",
        # OpenCode's ``provider.<id>`` entry. ``options.baseURL`` and
        # ``options.apiKey`` are dotted because that is where OpenCode reads
        # them; ``models`` is a mapping keyed by model id, not a list.
        provider=DesktopProvider(
            base_url_key="options.baseURL",
            api_key_key="options.apiKey",
            headers_key="options.headers",
            models_key="models",
            constants={
                "npm": "@ai-sdk/openai-compatible",
                "name": DESKTOP_PROVIDER_LABEL,
            },
        ),
        open_command="opencode",
        notes=("OpenCode's sidecar restarts on its own; the desktop shell does not.",),
    ),
    DesktopAppSpec(
        id="vscode_copilot",
        display_name="VS Code (Copilot custom endpoint)",
        summary=(
            "VS Code's custom chat endpoints live in a bare JSON array. MCC "
            "owns exactly one element and reorders nothing else."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://code.visualstudio.com/docs/copilot/customization/language-models",
        detect=DesktopDetect(
            markers=(
                _windows_appdata("Code", "User"),
                _home(".config", "Code", "User"),
            )
        ),
        document=DesktopDocument(
            paths=(
                _windows_appdata("Code", "User", "chatLanguageModels.json"),
                _home(".config", "Code", "User", "chatLanguageModels.json"),
            ),
            display_path="%APPDATA%\\Code\\User\\chatLanguageModels.json",
            document_format=DocumentFormat.JSON_ARRAY,
            match_field="name",
            match_value=DESKTOP_PROVIDER_LABEL,
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.ROOT,
        token_form=TokenForm.ENV_REFERENCE,
        token_template="${input:mcc_token}",
        token_env_var=DESKTOP_TOKEN_ENV_VAR,
        attribution_header_field="requestHeaders",
        catalogue_format_id="vscode",
        # One element of ``chatLanguageModels.json``. ``name`` is both the
        # picker label and the field ownership is matched on, so the merge
        # engine writes it from the spec rather than from here.
        provider=DesktopProvider(
            base_url_key="url",
            api_key_key="apiKey",
            headers_key="requestHeaders",
            models_key="models",
            constants={"vendor": "customendpoint", "apiType": "openai"},
        ),
        restart_required=False,
        open_command="code",
        notes=(
            "VS Code appends the method path itself according to apiType, so "
            "this one takes the proxy root rather than a /v1 suffix.",
            "${input:mcc_token} makes VS Code prompt once and keep the value "
            "in its own SecretStorage, so no token is written to the file.",
        ),
    ),
    DesktopAppSpec(
        id="crush_desktop",
        display_name="Crush",
        summary=(
            "Charm's Crush -- also the official client for Hyper/HyperCharm, "
            "which ships no client of its own -- expands $VAR in its config."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://github.com/charmbracelet/crush",
        detect=DesktopDetect(
            markers=(
                _windows_localappdata("crush"),
                _home(".config", "crush"),
            )
        ),
        document=DesktopDocument(
            paths=(
                DesktopPath(
                    env_vars=("CRUSH_GLOBAL_CONFIG",), relative_parts=("crush.json",)
                ),
                _home(".config", "crush", "crush.json"),
            ),
            display_path="~/.config/crush/crush.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=("providers", DESKTOP_PROVIDER_ID),
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        token_form=TokenForm.ENV_REFERENCE,
        token_template="${name}",
        token_env_var=DESKTOP_TOKEN_ENV_VAR,
        attribution_header_field="extra_headers",
        catalogue_format_id="crush",
        # Crush's ``providers.<id>`` entry, from ``crush schema``. ``type`` is
        # ``openai-compat`` for a custom OpenAI-compatible endpoint, and
        # ``discover_models`` has to be off: discovery would GET ``/models``
        # rather than ``/v1/models`` and find nothing.
        provider=DesktopProvider(
            base_url_key="base_url",
            api_key_key="api_key",
            headers_key="extra_headers",
            models_key="models",
            constants={
                "id": DESKTOP_PROVIDER_ID,
                "name": DESKTOP_PROVIDER_LABEL,
                "type": "openai-compat",
                "discover_models": False,
            },
        ),
        open_command="crush",
        notes=(
            "Crush's provider type for an OpenAI-compatible endpoint is openai-compat.",
        ),
    ),
    DesktopAppSpec(
        id="antigravity",
        display_name="Antigravity (agy CLI)",
        summary=(
            "Google's agy CLI speaks the public Gemini API when its "
            "modelProvider is set to gemini -- the surface MCC already serves."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://antigravity.google/docs/cli/install/",
        detect=DesktopDetect(
            markers=(
                _windows_localappdata("agy"),
                _home(".gemini", "antigravity-cli"),
            )
        ),
        document=DesktopDocument(
            paths=(_home(".gemini", "antigravity-cli", "settings.json"),),
            display_path="~/.gemini/antigravity-cli/settings.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=(),
            overwritten_keys=(("modelProvider",),),
        ),
        protocol=HarnessProtocol.GEMINI,
        base_url_shape=BaseUrlShape.V1BETA,
        token_form=TokenForm.ENV_ONLY,
        token_env_var="GEMINI_API_KEY",
        attribution_header_field="",
        catalogue_format_id="",
        open_command="agy",
        notes=(
            ANTIGRAVITY_ROUTING_NOTE,
            "Two variables have to be exported before agy is started: "
            "GEMINI_API_KEY and GOOGLE_GEMINI_BASE_URL. MCC never sets a "
            "user-scope variable; the card shows the exports and the next "
            "status poll reports whether they took.",
            "The Antigravity IDE lists custom endpoints as unsupported. This "
            "card is about the agy CLI only.",
        ),
    ),
    DesktopAppSpec(
        id="roo_code",
        display_name="Roo Code (VS Code)",
        summary=(
            "Roo Code publishes an import hook, so MCC owns a settings file "
            "outright and adds one key to VS Code's settings.json naming it."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://docs.roocode.com/features/settings-management",
        detect=DesktopDetect(
            markers=(
                _windows_appdata("Code", "User"),
                _home(".config", "Code", "User"),
            )
        ),
        document=DesktopDocument(
            paths=(
                _windows_appdata("Code", "User", "settings.json"),
                _home(".config", "Code", "User", "settings.json"),
            ),
            display_path="%APPDATA%\\Code\\User\\settings.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=("roo-cline.autoImportSettingsPath",),
            overwritten_keys=(("roo-cline.autoImportSettingsPath",),),
        ),
        sidecar=DesktopSidecar(
            paths=(
                DesktopPath(
                    env_vars=(),
                    relative_parts=("roo-code-settings.json",),
                    in_mcc_config_dir=True,
                ),
            ),
            display_path="<MCC config dir>/roo-code-settings.json",
            document_format=DocumentFormat.JSON,
            holds_credential=True,
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        token_form=TokenForm.MCC_OWNED_FILE,
        attribution_header_field="",
        catalogue_format_id="",
        open_command="code",
        notes=(
            "Roo Code's export format carries the key in plaintext and "
            "resolves no reference, so MCC keeps it in a file of its own at "
            "mode 0600 rather than in a document the user edits.",
            "Reload the VS Code window for the import to run.",
        ),
    ),
    DesktopAppSpec(
        id="commandcode",
        display_name="Command Code",
        summary=(
            "Already configured by MCC's shipped provider merge. This card "
            "reports its state and how to open it; it adds no new mechanism."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://docs.commandcode.ai/",
        detect=DesktopDetect(markers=(_home(".commandcode"),)),
        document=DesktopDocument(
            paths=(_home(".commandcode", "providers.json"),),
            display_path="~/.commandcode/providers.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=("provider", DESKTOP_PROVIDER_ID),
            create_if_missing=False,
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        token_form=TokenForm.ENV_REFERENCE,
        token_template="{env:{name}}",
        token_env_var=DESKTOP_TOKEN_ENV_VAR,
        attribution_header_field="headers",
        catalogue_format_id="commandcode",
        # Unchanged from the shipped 6.27.0 merge, and asserted byte-identical
        # by a regression test: this card reports, it does not re-mechanise.
        provider=DesktopProvider(
            base_url_key="baseURL",
            api_key_key="apiKey",
            headers_key="headers",
            models_key="models",
            constants={"name": DESKTOP_PROVIDER_LABEL},
        ),
        open_command="mcc-commandcode",
        notes=(
            "Command Code ships no desktop application. The CLI is what is "
            "installed, and mcc-commandcode has configured it since 6.27.0.",
        ),
    ),
    DesktopAppSpec(
        id="claude_desktop",
        display_name="Claude Desktop",
        summary=(
            "Claude Desktop has a native gateway mode, entered in a dialog "
            "rather than a file. MCC shows the values; a human types them."
        ),
        status=DesktopAppStatus.INSTRUCTIONS_ONLY,
        doc_url="https://claude.com/docs/third-party/claude-desktop/gateway",
        detect=DesktopDetect(
            markers=(
                _windows_appdata("Claude"),
                _home("Library", "Application Support", "Claude"),
            )
        ),
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        base_url_shape=BaseUrlShape.ROOT,
        token_form=TokenForm.IN_APP,
        token_env_var="ANTHROPIC_AUTH_TOKEN",
        attribution_header_field="inferenceCustomHeaders",
        catalogue_format_id="",
        open_command="",
        notes=(
            "Help -> Troubleshooting -> Enable Developer Mode, then "
            "Developer -> Configure Third-Party Inference.",
            "The desktop app does not honour ANTHROPIC_BASE_URL. Its Code tab "
            "reads ~/.claude/settings.json, which Configure Claude Code "
            "already covers.",
            "MCC does not guess a persistence path for these settings. "
            "Anthropic documents the dialog, not a file, and writing a merge "
            "into a guessed path is how a merge engine gets its first "
            "data-loss bug. A button follows once the file is established.",
        ),
        instruction_fields=(
            ("Connection", "Gateway"),
            ("Base URL", "{root}"),
            ("Auth scheme", "Bearer"),
            ("API key", "the value of ANTHROPIC_AUTH_TOKEN"),
            ("Models", "mcc/best, mcc/good, mcc/medium, mcc/cheap"),
            ("Custom headers", "x-mcc-harness: claude_desktop"),
        ),
    ),
    DesktopAppSpec(
        id="kimi_desktop",
        display_name="Kimi desktop",
        summary="Account-login Electron app with no endpoint field.",
        status=DesktopAppStatus.NOT_ROUTABLE,
        doc_url="https://www.kimi.com/",
        unavailable_reason=(
            "Kimi's desktop app signs in to a Moonshot account and exposes no "
            "base-URL or API-key field anywhere in its UI -- verified "
            "2026-09-07 against the build installed on this machine. Its CLI "
            "is routable and MCC ships it as mcc-kimi."
        ),
        detect=DesktopDetect(markers=(_windows_localappdata("Programs", "kimi"),)),
    ),
    DesktopAppSpec(
        id="qwen_desktop",
        display_name="Qwen desktop",
        summary="Account-login Electron app with no endpoint field.",
        status=DesktopAppStatus.NOT_ROUTABLE,
        doc_url="https://chat.qwen.ai/",
        unavailable_reason=(
            "Qwen's desktop app signs in to an Alibaba account and publishes "
            "no custom-endpoint setting -- verified 2026-09-07 against the "
            "build installed on this machine. Its CLI is routable and MCC "
            "ships it as mcc-qwen."
        ),
        detect=DesktopDetect(markers=(_windows_localappdata("Programs", "Qwen"),)),
    ),
    DesktopAppSpec(
        id="lm_studio",
        display_name="LM Studio",
        summary="A server, not a client.",
        status=DesktopAppStatus.NOT_ROUTABLE,
        doc_url="https://lmstudio.ai/docs",
        unavailable_reason=(
            "LM Studio serves models; it does not consume a remote endpoint. "
            "There is nothing to point at MCC, and there never will be -- it "
            "is on the other side of the same wire. Verified 2026-09-07."
        ),
        detect=DesktopDetect(markers=(_home(".lmstudio"),)),
    ),
    DesktopAppSpec(
        id="warp",
        display_name="Warp",
        summary="Rejects loopback and private addresses.",
        status=DesktopAppStatus.NOT_ROUTABLE,
        doc_url="https://docs.warp.dev/",
        unavailable_reason=(
            "Warp's custom-model endpoint validator rejects 127.0.0.1 and "
            "private IP ranges outright, so a local proxy cannot be named at "
            "all without a public tunnel. Verified 2026-09-07."
        ),
        detect=DesktopDetect(markers=(_windows_localappdata("Programs", "Warp"),)),
    ),
)


DESKTOP_APPS_BY_ID: Mapping[str, DesktopAppSpec] = {
    spec.id: spec for spec in DESKTOP_APPS
}


def desktop_app(app_id: str) -> DesktopAppSpec:
    """Return one spec by id, or raise KeyError naming what is registered."""

    try:
        return DESKTOP_APPS_BY_ID[app_id]
    except KeyError:
        known = ", ".join(sorted(DESKTOP_APPS_BY_ID))
        raise KeyError(f"unknown desktop app: {app_id}. Known: {known}") from None
