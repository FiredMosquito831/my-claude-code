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
    No app is in this state today. Claude Desktop was, on the belief that
    Anthropic documented a dialog and not a persistence path; that was wrong
    -- the MDM documentation names
    ``%LOCALAPPDATA%\\Claude-3p\\configLibrary\\`` (macOS:
    ``~/Library/Application Support/Claude-3p/configLibrary/``) as the local
    configuration source -- so it is SERVABLE, and its instruction fields
    survive as the fallback the card shows when that directory does not exist
    or a managed profile outranks it.

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
from my_claude_code.config.harnesses import COMMANDCODE_API_KEY_ENV, HarnessProtocol


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
    #: The literal credential goes into the app's *own* configuration document,
    #: tightened to 0600 where the OS allows it.
    #:
    #: This form exists because the older reading of "never a literal where the
    #: app takes a reference" cost a release. Codex, OpenCode and Crush all
    #: publish a reference syntax, so MCC wrote one -- naming
    #: ``MCC_AUTH_TOKEN``, a variable **nothing in MCC has ever set**. A
    #: desktop application started from Explorer inherits the user environment,
    #: which does not have it either, so OpenCode and Crush sent the literal
    #: string ``{env:MCC_AUTH_TOKEN}`` / ``$MCC_AUTH_TOKEN`` as their bearer
    #: token (measured: HTTP 401) and Codex refused to load its config file at
    #: all (``Missing environment variable``). A reference to a variable that
    #: is never set is not protection; it is a guaranteed failure. So the test
    #: is not "does this app have a syntax for references" but "can this app
    #: *resolve* the reference as MCC ships it", and where it cannot, the
    #: literal goes in the file the app reads.
    LITERAL_IN_APP_FILE = "literal_in_app_file"
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
    #: MCC's configuration is in the file and the credential it names cannot
    #: be resolved on this machine, so the app will fail the moment it is
    #: started. Deliberately **not** ``CONFIGURED``: the probe used to report
    #: green here and hang the one true fact -- "not exported yet" -- in a
    #: details table under a "Configured by MCC" badge, which is how four apps
    #: shipped for months sending an unexpanded reference as a bearer token
    #: and getting 401 (measured). A state where the credential cannot resolve
    #: is not configured, and this is the badge that says so.
    CREDENTIAL_UNRESOLVED = "credential_unresolved"
    #: The restore record and the backup are both here, MCC has not been
    #: undone, and MCC's keys are gone from the document anyway -- so the
    #: application removed them. Until 6.84.0 this was indistinguishable from
    #: "installed, never configured", because ``undo()`` left no mark and a
    #: stale record meant nothing. It is the only state that can surface an
    #: app which rewrites its own configuration file, which is the failure
    #: mode the Cline lesson already cost one release.
    REMOVED_BY_APP = "removed_by_app"
    #: A higher-precedence source -- a policy key, a managed profile -- owns
    #: this app's configuration, so the file MCC would write is ignored. The
    #: card names the source and offers no button, because a button that wrote
    #: a file with no effect would be worse than none.
    MANAGED = "managed"


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
    #: Whether ``relative_parts`` is a glob pattern rather than a literal path.
    #: Two installs force this: an MSIX package directory carries a publisher
    #: hash Microsoft assigns (``Packages/Claude_pzs8sxrjxfjjc``), and a VS Code
    #: extension directory carries the extension's *version*
    #: (``github.copilot-chat-0.32.4``). Both are real, both change under the
    #: user, and neither can be spelled as a constant without being wrong on
    #: the next update. A glob path resolves to the first match in sorted
    #: order, or to nothing at all when there is no match -- which is the whole
    #: point for a detection marker: no match means not installed.
    glob: bool = False
    #: Resolve under MCC's own configuration directory rather than the user's
    #: home. Set for a file MCC owns outright, so that the directory's name
    #: stays the single responsibility of ``config/paths`` -- a contract test
    #: asserts no other module spells it, precisely so a rename can never
    #: strand a file MCC wrote.
    in_mcc_config_dir: bool = False


@dataclass(frozen=True, slots=True)
class DesktopDetect:
    """What proves this application is on the machine: a program, never its data.

    **A marker is the program.** Until 6.83.0 a marker was "a directory the app
    creates on first run", on the reasoning that an app which has run but has
    never been configured must still read as installed. That reasoning was
    right and the implementation of it was not, and the cost was measured: on
    the machine this was written for, ``%LOCALAPPDATA%\\crush`` and
    ``%APPDATA%\\Block\\goose`` both existed, both made their cards read
    *installed* with a Configure button -- and **no ``crush.exe`` or
    ``goose.exe`` exists anywhere on that machine**. Both directories had been
    created by MCC's *own* launchers, ``mcc-crush.exe`` and ``mcc-goose.exe``,
    the one time each was run. MCC was reading its own footprint as evidence
    that somebody else's application was installed, offering to configure it,
    and (before the install gate in ``desktop_apply.apply``) writing the file.

    So a marker now has to be the program itself: an executable, an application
    bundle, an installed package, or -- for an editor extension, which has no
    binary of its own -- the extension directory, which no other program
    creates. A data directory is evidence that something ran once, and the
    something may have been MCC.

    Two kinds, either of which is enough:

    ``binaries``
        Executable names looked up on the ``PATH`` the app would be started
        with, through ``shutil.which``, which applies ``PATHEXT`` on Windows.
        This is how every command-line install of Crush, Goose, Codex and
        OpenCode arrives, whatever package manager put it there. MCC's own
        launchers are all named ``mcc-*``, so they can never satisfy one of
        these.

    ``markers``
        Paths, for a program that is not on ``PATH``: an ``.exe`` under a
        per-user install directory, a macOS ``.app`` bundle, an MSIX package
        directory the OS creates only for an installed package, a VS Code
        extension directory.
    """

    markers: tuple[DesktopPath, ...] = ()
    #: Executable names that prove the program is installed, looked up on PATH.
    binaries: tuple[str, ...] = ()


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
    #: A list *nested inside an object document* of which MCC owns exactly one
    #: element, identified by ``match_field``/``match_value``. Claude Desktop's
    #: ``_meta.json`` is the case: it is an object (``appliedId`` plus
    #: ``entries``), and MCC's configuration is one element of ``entries``
    #: beside however many the user authored in the app's own window. Distinct
    #: from ``JSON_ARRAY``, where the whole document is the list, and from
    #: ``owned_key_path``, which owns a mapping rather than a list element.
    owned_element_path: tuple[str, ...] = ()
    #: Scalars MCC *replaces* rather than creates. These are the keys the
    #: restore record remembers and the RESTORE undo mode puts back. Every one
    #: of them is a value a user may legitimately have set to something else.
    overwritten_keys: tuple[tuple[str, ...], ...] = ()
    #: Suffix of the one-time copy taken before MCC's first edit.
    backup_suffix: str = ".mcc-backup"
    #: Whether Configure may create the file when it is absent.
    create_if_missing: bool = True
    #: Copy the whole *directory* this document lives in, timestamped, into
    #: MCC's own configuration directory before the first edit -- in addition
    #: to the single-file backup.
    #:
    #: Claude Desktop is the reason, and the reason is a real incident. Its
    #: configuration library is a directory of documents plus an index, and
    #: what MCC edits (``_meta.json``) is only the index: a per-file backup of
    #: the index cannot restore a library. On 2026-09-08 Configure moved
    #: ``appliedId`` onto an id the app rejects at boot and the user's own
    #: gateway configuration stopped being applied; ``_meta.json.mcc-backup``
    #: was the only copy of anything, and it covered the one file that was
    #: easiest to reconstruct. The copy goes under MCC's directory rather than
    #: beside the original on purpose: the app globs ``*.json`` out of that
    #: library, so a backup left inside it is a document the app may try to
    #: read.
    backup_directory: bool = False


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
    #: The whole content of the file, declared, for an app whose owned file is
    #: a fixed set of settings rather than a serialised model catalogue.
    #: ``{base_url}`` and ``{token}`` are substituted by
    #: ``application/desktop_documents``; every other value is written as it
    #: stands. Claude Desktop is the case, and declaring the six keys here
    #: rather than branching on the app in the writer is what keeps the promise
    #: that adding an app is adding a row.
    fields: Mapping[str, object] = field(default_factory=dict)
    #: Where the attribution header map goes inside :attr:`fields`, or empty.
    headers_key: str = ""
    #: The serialiser producing the model *list* substituted for ``{models}``
    #: in :attr:`fields`, looked up in
    #: ``application/catalogues.SIDECAR_SERIALISERS``. Empty for a file whose
    #: settings are all fixed. Claude Desktop is the case: its picker is filled
    #: from an ``inferenceModels`` array, and the array is a serialised model
    #: list like any other, so it comes from the package that owns those rather
    #: than from a shape spelled out in the writer.
    models_format_id: str = ""


@dataclass(frozen=True, slots=True)
class DesktopLegacyEntry:
    """An identity MCC used to write here, and now has to repair on sight.

    Declared rather than branched on, for the same reason everything else in
    this module is: a repair that lives as an ``if spec.id == "claude_desktop"``
    in the writer is a repair the next app cannot reuse and the registry tests
    cannot see.

    The case that produced it: MCC named its Claude Desktop configuration
    ``mcc-9c2f4b18-…``, and the app validates that id against
    ``/^[a-f0-9-]{36}$/`` **at boot, before reading the file**. MCC's id is 40
    characters and contains an ``m``, so the check failed, the loader returned
    ``undefined``, and the app discarded its entire local configuration tier --
    including the gateway configuration the user had authored by hand. Fixing
    the constant alone would leave every existing user in that state forever,
    because nothing would ever remove the entry already on disk. So the probe
    repairs it once, and a library that has already been repaired -- by MCC or
    by a user who did it themselves -- has to come out of that repair
    untouched, which is why every step below is conditioned on finding the
    legacy value rather than on asserting the new one.
    """

    #: The value ``DesktopDocument.match_field`` used to carry. An element
    #: holding it is MCC's, and is removed; an overwritten scalar holding it is
    #: MCC's, and is re-pointed at the current ``match_value``.
    match_value: str
    #: Files MCC used to own outright under the old identity. Each is copied to
    #: the current sidecar path when that path is absent -- so a repaired
    #: install keeps working without a second Configure -- and then deleted.
    sidecar_paths: tuple[DesktopPath, ...] = ()
    #: One line, shown on the card, saying what was repaired and why.
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DesktopManagedSource:
    """A configuration source that outranks the file MCC would write.

    Claude Desktop is the reason this exists, and it is the reason it is data
    rather than a branch. Its local configuration library is the *lowest*
    precedence source it has: a machine or user policy under
    ``SOFTWARE\\Policies\\Claude``, or a macOS managed preferences plist,
    replaces it wholesale and makes the app's own configuration window
    read-only. A Configure that wrote the library anyway would leave a file
    that does nothing and a card that said "Configured by MCC" about a machine
    routing somewhere else -- the same invisible failure a wrong path is, one
    layer up.

    This is the desktop-card twin of
    :func:`my_claude_code.config.claude_settings._detect_overrides`, which does
    the same job for Claude Code's enterprise ``managed-settings.json``.
    """

    #: How the card names it, e.g. ``Machine policy (HKLM\\SOFTWARE\\Policies\\Claude)``.
    label: str
    #: ``HKLM`` or ``HKCU`` for a Windows registry source, else empty.
    registry_hive: str = ""
    #: The subkey under that hive. Values must sit *directly* under it: the app
    #: never reads a value nested in a deeper subkey.
    registry_subkey: str = ""
    #: A file source, when this one is a file rather than a registry key.
    path: DesktopPath | None = None
    #: Value names that do not, on their own, mean the source has taken over.
    #: Claude Desktop documents exactly such a group -- update, relaunch,
    #: config-recheck and proxy keys -- which a fleet may set without making
    #: the whole configuration managed.
    exempt_keys: tuple[str, ...] = ()


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
    #: Required when INSTRUCTIONS_ONLY, and dated, for the same reason.
    #:
    #: This field is the honesty pass of 6.84.0 made checkable. Spec §5's
    #: binding rule is that a row keeps its Configure button only where MCC's
    #: *generated document* is validated in CI against a rule extracted from
    #: that application's own shipped code or published schema, vendored under
    #: ``tests/fixtures/app_rules/`` with the version and the extraction
    #: command. A row that cannot meet that bar is demoted here rather than
    #: given a validator invented to justify the button -- and it has to say,
    #: with a date, what was not proven, so the demotion can be re-checked
    #: instead of believed. ``tests/config/test_desktop_apps_registry.py``
    #: asserts every INSTRUCTIONS_ONLY row carries one and that it is dated.
    instructions_reason: str = ""
    detect: DesktopDetect | None = None
    document: DesktopDocument | None = None
    sidecar: DesktopSidecar | None = None
    #: Sources that outrank the file MCC writes. Empty for every app whose
    #: config file is the only place its settings can come from.
    managed_sources: tuple[DesktopManagedSource, ...] = ()
    #: Identities MCC wrote in an earlier release and now repairs on sight.
    legacy_entries: tuple[DesktopLegacyEntry, ...] = ()
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
    #: Whether :attr:`token_env_var` is a variable **MCC itself sets** in the
    #: process it launches, rather than one a human has to export.
    #:
    #: Command Code is the only true case: ``mcc-commandcode`` sets
    #: ``MCC_COMMANDCODE_API_KEY`` in the child it starts, so the reference in
    #: ``providers.json`` resolves every time that launcher is used and the
    #: variable's absence from the dashboard's own environment says nothing at
    #: all. Without this field the credential-unresolved state of fix 8 would
    #: have painted that card red on every poll for a configuration that works
    #: -- which is the same class of lie in the other direction.
    token_env_var_set_by_mcc: bool = False
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
    #: The overwritten scalar that receives the *resolved path* of the file
    #: MCC owns outright, for an app whose hook into its own settings is
    #: "read my configuration from over there".
    #:
    #: Declared rather than branched on, and the branch it replaced is the
    #: reason. ``application/desktop_documents.overwritten_scalars`` matched on
    #: the key name and answered ``continue`` with the comment "filled in by
    #: the caller" -- and no caller filled it, so Roo Code's Configure wrote
    #: neither the settings key nor the sidecar, and its only effect on disk
    #: was to re-indent the user's ``settings.json``.
    sidecar_path_key: str = ""
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
        if self.document.owned_element_path:
            element = ".".join(self.document.owned_element_path)
            return (
                f"{element}[{self.document.match_field} == "
                f'"{self.document.match_value}"]'
            )
        return ".".join(self.document.owned_key_path)


#: The provider id MCC claims inside every desktop app's document. One word,
#: the same everywhere, so a user grepping their configs for "mcc" finds every
#: trace of it in one search -- and so Undo has one thing to look for.
DESKTOP_PROVIDER_ID = "mcc"

#: The display name MCC's entry carries where an app shows a provider name to
#: a human, and the value ownership is matched on in VS Code's keyless array.
DESKTOP_PROVIDER_LABEL = "My Claude Code"

#: The environment variable an app that reads its credential *only* from the
#: environment is told to export. One name rather than one per app: the user
#: exports it once, and a card that verifies "is it exported?" is checking a
#: single fact.
#:
#: **Nothing in MCC sets it**, which is exactly why it may no longer be written
#: into a document as a reference. Until 6.67.0 it was, for Codex, OpenCode,
#: Crush and Command Code, and every one of them failed: the apps sent the
#: unexpanded reference as a bearer token (HTTP 401, measured) or refused to
#: start. It now names only what a card *asks a human to export* for an app
#: whose credential cannot reach it any other way -- Goose, which ignores keys
#: written to its configuration in favour of its keyring, and Antigravity,
#: whose base URL and key are environment-only. See
#: :attr:`TokenForm.LITERAL_IN_APP_FILE` for what replaced the references.
DESKTOP_TOKEN_ENV_VAR = "MCC_AUTH_TOKEN"

#: The id of the configuration document MCC owns inside Claude Desktop's local
#: configuration library, and the name the app's own picker shows for it.
#:
#: The library is a directory of ``<uuid>.json`` documents plus a ``_meta.json``
#: index -- read off this machine on 2026-09-07, key names only -- so MCC owns
#: a whole file of its own and merges exactly one foreign key, ``appliedId``,
#: plus one element of ``entries``. A fixed id rather than a generated one:
#: Undo has to find the same file a year later, and a second Configure must
#: replace MCC's document rather than litter the user's picker with a new entry
#: on every run.
#: **The id must be a bare 36-character lowercase UUID and nothing else.**
#: Claude Desktop 1.46388.4.0 validates ``_meta.json``'s ``appliedId`` against
#: ``/^[a-f0-9-]{36}$/`` at boot, *before* it reads the document -- main-process
#: bundle ``.vite/build/index.chunk--WuAOADe.js``, regex at line 35967, loader
#: ``$je`` at 36626, extracted from
#: ``C:\Program Files\WindowsApps\Claude_1.46388.4.0_x64__pzs8sxrjxfjjc\app\
#: resources\app.asar``. A failing id makes the loader return ``undefined``,
#: ``nMe`` (36642) substitutes ``{}``, and the app discards its **entire** local
#: configuration tier and starts in ordinary first-party mode with no message
#: anywhere naming the id.
#:
#: Until 6.67.0 MCC prefixed the UUID with ``mcc-``, which made it 40
#: characters and put an ``m`` in it, so the check failed. Because Configure
#: also moves ``appliedId``, pressing the button did not merely fail to
#: configure Claude Desktop -- it **un**configured a gateway the user had set
#: up by hand. The prefix is gone; the UUID underneath is deliberately the same
#: one, so a library configured by either release is recognisably MCC's.
#: :data:`CLAUDE_DESKTOP_LEGACY_CONFIG_ID` is what the repair looks for.
CLAUDE_DESKTOP_CONFIG_ID = "9c2f4b18-0f4a-4a1e-9a3e-5b1d0c7e6a20"

#: The id MCC wrote before 6.67.0. Kept so the one-time repair can find what
#: is already on disk; never written again.
CLAUDE_DESKTOP_LEGACY_CONFIG_ID = "mcc-9c2f4b18-0f4a-4a1e-9a3e-5b1d0c7e6a20"

CLAUDE_DESKTOP_CONFIG_NAME = "My Claude Code (MCC)"

#: The registry key Claude Desktop reads managed configuration from, under both
#: ``HKEY_LOCAL_MACHINE`` and ``HKEY_CURRENT_USER``. Values sitting *directly*
#: under it outrank the local configuration library entirely; a subkey does not
#: count. Documented at
#: https://claude.com/docs/third-party/claude-desktop/mdm section
#: "4. Deploy the configuration".
CLAUDE_DESKTOP_POLICY_SUBKEY = "SOFTWARE\\Policies\\Claude"

#: Claude Desktop's own name for the gateway settings MCC writes, from
#: https://claude.com/docs/third-party/claude-desktop/configuration. Named here
#: so the card, the writer and the tests read one list.
#: The "app-behavior" keys a fleet may set from a managed profile *without*
#: taking over the whole configuration. Verbatim from
#: https://claude.com/docs/third-party/claude-desktop/mdm, section
#: "Update keys and managed precedence": a profile setting only keys from this
#: group leaves the device's locally authored configuration in force and the
#: app's configuration window editable -- so MCC's Configure is still honest.
CLAUDE_DESKTOP_APP_BEHAVIOR_KEYS: tuple[str, ...] = (
    "disableAutoUpdates",
    "autoUpdaterEnforcementHours",
    "updateViaUpdatesHost",
    "relaunchEnforcementHours",
    "configRecheckIntervalMinutes",
    "egressProxyUrl",
    "egressProxyPacUrl",
)

#: ``inferenceCustomHeaders`` is a real key of the app's and is deliberately
#: **not** here: MCC no longer writes it (see the ``claude_desktop`` sidecar).
#: ``inferenceModels`` is here because MCC now does.
CLAUDE_DESKTOP_GATEWAY_KEYS: tuple[str, ...] = (
    "inferenceProvider",
    "inferenceGatewayBaseUrl",
    "inferenceGatewayApiKey",
    "inferenceCredentialKind",
    "modelDiscoveryEnabled",
    "inferenceModels",
)

#: The app's own preference keys MCC ships as defaults in the document it
#: owns, with the values from the configuration the user built by hand and
#: proved working on 2026-09-09 (specs/CLAUDE-DESKTOP-CONFIG-REFERENCE.md
#: section 1.3). Every one of them is a real ``flatKey`` of the same document,
#: declared in the shipped bundle with ``scopes: ["3p"]`` -- read out of the
#: installed 1.52386.0.0 ``app.asar`` and vendored, offset by offset, in
#: ``tests/fixtures/app_rules/claude-desktop-1.52386.0.0.json``.
#:
#: These are *defaults for a document MCC creates*, not edits to a document
#: the user owns. MCC's entry is its own file under its own id; the user's own
#: entry is never read, never written and never compared against this. Undo
#: deletes MCC's file whole, so Undo owes the user nothing here either.
#:
#: ``banner`` is a real key of the same group and is deliberately **not**
#: here. It carries an organisation's own name and colours -- on this machine,
#: "Danube Labs" -- and a router writing someone's branding into their app is
#: a different product.
CLAUDE_DESKTOP_APP_DEFAULTS: Mapping[str, object] = {
    "isDesktopExtensionEnabled": True,
    "modelPrefer1mContext": True,
    "disableEssentialTelemetry": True,
    "disableNonessentialTelemetry": True,
    "autoModeEnabled": True,
    "skipWebFetchPreflight": True,
    "claudeAiImport": {
        "enabled": True,
        "automatic3pImport": True,
        "exportEnabled": True,
    },
}


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


#: Variables that name a *platform* directory rather than an app's own config
#: location. The distinction decides a tie: a variable an app defines for
#: itself -- ``CODEX_HOME``, ``OPENCODE_CONFIG_DIR``, ``CRUSH_GLOBAL_CONFIG``
#: -- is the user saying "my config is here", and it wins outright even when
#: the file does not exist yet. ``APPDATA`` is always set on Windows and says
#: nothing about where any particular app keeps anything, so a path built from
#: it may lose to a sibling that exists. Without that line, "prefer the path
#: that already exists" would quietly override an explicit ``CODEX_HOME``.
PLATFORM_ENV_VARS: frozenset[str] = frozenset(
    {
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "USERPROFILE",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
    }
)


def _windows_appdata(*parts: str) -> DesktopPath:
    return DesktopPath(
        env_vars=("APPDATA",), relative_parts=parts, platforms=("win32",)
    )


def _windows_localappdata(*parts: str, glob: bool = False) -> DesktopPath:
    return DesktopPath(
        env_vars=("LOCALAPPDATA",),
        relative_parts=parts,
        platforms=("win32",),
        glob=glob,
    )


def _windows_program_files(*parts: str) -> DesktopPath:
    return DesktopPath(
        env_vars=("PROGRAMFILES",), relative_parts=parts, platforms=("win32",)
    )


def _claude_library_file(filename: str) -> tuple[DesktopPath, ...]:
    """Return one file of Claude Desktop's configuration library, per platform.

    Spelled once because it is spelled three times over: the index MCC merges,
    the document MCC owns, and the document MCC owned under its old id and now
    repairs. Three copies of the same three paths is three places for them to
    drift apart.
    """

    return (
        DesktopPath(
            env_vars=("LOCALAPPDATA",),
            relative_parts=("Claude-3p", "configLibrary", filename),
            platforms=("win32",),
        ),
        DesktopPath(
            env_vars=("HOME", "USERPROFILE"),
            relative_parts=(
                "Library",
                "Application Support",
                "Claude-3p",
                "configLibrary",
                filename,
            ),
            platforms=("darwin",),
        ),
        DesktopPath(
            env_vars=("HOME", "USERPROFILE"),
            relative_parts=(".config", "Claude-3p", "configLibrary", filename),
            platforms=("linux",),
        ),
    )


def _home(
    *parts: str,
    env_vars: tuple[str, ...] = ("HOME", "USERPROFILE"),
    platforms: tuple[str, ...] = (),
    glob: bool = False,
) -> DesktopPath:
    return DesktopPath(
        env_vars=env_vars, relative_parts=parts, platforms=platforms, glob=glob
    )


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
        # The program, not its data directory. ``%LOCALAPPDATA%\\OpenAI\\Codex``
        # and ``~/.codex`` are both written by the *first run* of anything that
        # speaks Codex, MCC's own ``mcc-codex`` launcher included, so neither
        # proves OpenAI's app is here. The versioned ``bin/<hash>/codex.exe``
        # is the engine the MSIX app and the npm CLI both unpack, and it is the
        # same 0.153.4 binary in either case.
        detect=DesktopDetect(
            binaries=("codex",),
            markers=(
                _windows_localappdata("Packages", "OpenAI.Codex_*", glob=True),
                _windows_localappdata(
                    "OpenAI", "Codex", "bin", "*", "codex.exe", glob=True
                ),
                _home(".local", "bin", "codex", platforms=("darwin", "linux")),
            ),
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
        # Codex resolves ``env_key`` from the process environment and **hard
        # errors** when the variable is unset -- ``ERROR: Missing environment
        # variable: '…'``, with no fallback to the ChatGPT OAuth in
        # ``auth.json`` -- so the reference form it publishes is unusable for
        # an app started from the Start Menu. ``experimental_bearer_token``
        # takes the literal and works; it is the field CLIProxyAPI's own
        # Codex-App recipe uses, for exactly this reason.
        token_form=TokenForm.LITERAL_IN_APP_FILE,
        token_env_var="",
        attribution_header_field="http_headers",
        catalogue_format_id="codex",
        # Codex's ``model_providers.<id>`` entry. The field list is not from
        # prose: it was recovered from the shipped binary's own serde table
        # (``codex.strings.txt:1405750``, 17 fields) and each value below was
        # exercised against ``codex 0.153.4`` under a scratch ``CODEX_HOME``.
        #
        # ``wire_api`` is the value that made this a bug report rather than a
        # misconfiguration. MCC wrote ``"chat"``; 0.153.4 answers
        #
        #     Error loading config.toml: `wire_api = "chat"` is no longer
        #     supported. How to fix: set `wire_api = "responses"` in your
        #     provider config.
        #
        # and exits 1. That is not a provider that fails to answer -- it is a
        # ``config.toml`` that will not parse, so every unrelated Codex setting
        # in the user's file stops working too, in the CLI *and* in the desktop
        # app (the ChatGPT app embeds the same engine and reads the same file).
        # ``"responses"`` is the only accepted value and the default when the
        # key is omitted.
        #
        # ``base_url`` must carry ``/v1``: Codex appends ``/responses``.
        # Unknown keys are ignored (no ``deny_unknown_fields``); *retired
        # known* keys are the exception, and ``wire_api = "chat"`` was one.
        provider=DesktopProvider(
            base_url_key="base_url",
            api_key_key="experimental_bearer_token",
            headers_key="http_headers",
            models_key="",
            constants={"name": DESKTOP_PROVIDER_LABEL, "wire_api": "responses"},
        ),
        sets_default_model=True,
        open_command="codex",
        notes=(
            "Codex has no UI for picking a model once a custom model_provider "
            'is set, so Configure also writes model = "mcc/best". That is the '
            "one value here MCC overwrites by necessity, and the reason the "
            "restore mode of Undo exists.",
            "MCC's proxy token is written into ~/.codex/config.toml as "
            "experimental_bearer_token, and the file is tightened to 0600 "
            "where the OS allows it. Codex's env_key form hard-errors when the "
            "variable is unset, and an app started from the Start Menu has no "
            "variable MCC could have set -- so the literal in the file is the "
            "only form that works. Undo removes it.",
            "The Codex desktop app (the ChatGPT app, MSIX OpenAI.Codex) "
            "embeds the same engine as the CLI and reads the same file, so "
            "this card configures both. Its model picker is reported to "
            "degrade with any custom provider (openai/codex#29156): the "
            "session runs on MCC, but switching models from the picker may "
            "not work. Not measured here.",
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
        # ``%APPDATA%\\Block\\goose`` used to be the marker, and on the machine
        # this was written for it contained one directory of empty log files
        # written by MCC's own ``mcc-goose.exe`` launcher on 2026-09-02 -- no
        # ``goose.exe`` or ``goosed.exe`` exists anywhere on that machine, on
        # PATH, in Program Files, scoop, cargo, go/bin or the uninstall
        # registry. The card said "installed" and offered to configure it.
        detect=DesktopDetect(
            binaries=("goose", "goosed"),
            markers=(
                DesktopPath(
                    env_vars=(),
                    relative_parts=("/Applications", "Goose.app"),
                    platforms=("darwin",),
                ),
                _home("Applications", "Goose.app", platforms=("darwin",)),
                _home(".local", "bin", "goose", platforms=("darwin", "linux")),
            ),
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
        # Goose's custom-provider file, which MCC owns whole, keyed exactly as
        # Goose's own ``DeclarativeProviderConfig`` is keyed -- read out of
        # ``goose-source-v1.50.0.zip`` and vendored as
        # ``tests/fixtures/app_rules/goose-1.50.0.json``.
        #
        # Until 6.84.0 this row wrote ``api_url`` and ``model_details``, and
        # neither is a field of that struct: the URL is ``base_url``
        # (``api_url`` is the name of the parameter Goose's own
        # create-provider API takes before assigning it to ``base_url``) and
        # the models are ``models``. ``engine`` has no serde default and was
        # missing entirely, so the document could not deserialise at all --
        # ``load_custom_providers`` would have reported
        # ``Failed to parse …: missing field `engine` `` and dropped it.
        #
        # ``base_path`` is the other half of ``SPLIT_HOST_PATH``: Goose's
        # OpenAI client splits ``base_url`` into host and path and lets this
        # field override the path. ``requires_auth`` is Goose's own default
        # and is stated so the failure is loud rather than an unauthenticated
        # request. No key field, because a key written here is ignored --
        # ``api_key_env`` names a *keyring* entry, resolved through
        # ``Config::get_secret``, never the process environment.
        provider=DesktopProvider(
            base_url_key="base_url",
            models_key="models",
            constants={
                "name": DESKTOP_PROVIDER_ID,
                "engine": "openai",
                "display_name": DESKTOP_PROVIDER_LABEL,
                "base_path": "v1/chat/completions",
                "api_key_env": DESKTOP_TOKEN_ENV_VAR,
                "requires_auth": True,
            },
        ),
        open_command="goose",
        notes=(
            "Goose ignores API keys written into config.yaml or into this "
            "provider file -- api_key_env names an entry in its keyring, or "
            "in secrets.yaml when GOOSE_DISABLE_KEYRING is set. A literal "
            "would silently not work, so MCC writes none and this card is "
            "green only once MCC_AUTH_TOKEN can actually be resolved.",
            "Goose publishes no per-provider request-header field, so it is "
            "attributed by user-agent rather than x-mcc-harness.",
        ),
    ),
    DesktopAppSpec(
        id="opencode_desktop",
        display_name="OpenCode desktop",
        summary=(
            "OpenCode's desktop build reads the same provider map as its CLI, "
            "and takes the key as a plain string in that file."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://opencode.ai/docs/config",
        # The desktop build's own executable, or the CLI on PATH -- the two
        # programs that read this file. ``%APPDATA%\\ai.opencode.desktop`` and
        # ``~/.config/opencode`` are state and configuration directories, and
        # the second one is the very file MCC writes, so it proved nothing but
        # that somebody had configured something.
        detect=DesktopDetect(
            binaries=("opencode",),
            markers=(
                _windows_localappdata(
                    "Programs", "@opencode-aidesktop", "OpenCode.exe"
                ),
                DesktopPath(
                    env_vars=(),
                    relative_parts=("/Applications", "OpenCode.app"),
                    platforms=("darwin",),
                ),
                _home(".opencode", "bin", "opencode", platforms=("darwin", "linux")),
            ),
        ),
        document=DesktopDocument(
            paths=(
                DesktopPath(
                    env_vars=("OPENCODE_CONFIG_DIR",),
                    relative_parts=("opencode.json",),
                ),
                # ~/.config, on every platform including Windows. OpenCode's
                # own documentation states exactly one global location --
                # "Place your global OpenCode config in
                # ~/.config/opencode/opencode.json" (https://opencode.ai/docs/config)
                # -- and names no %APPDATA% form at all. MCC used to declare
                # the %APPDATA% path first, so on Windows Configure created a
                # brand-new file OpenCode never reads and reported success:
                # the invisible failure this module exists to prevent.
                _home(".config", "opencode", "opencode.json"),
            ),
            display_path="~/.config/opencode/opencode.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=("provider", DESKTOP_PROVIDER_ID),
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        # ``{env:MCC_AUTH_TOKEN}`` until 6.67.0. OpenCode does expand
        # ``{env:…}``, but nothing sets that variable and a desktop app
        # inherits only the user environment -- so what actually went on the
        # wire was ``Authorization: Bearer {env:MCC_AUTH_TOKEN}``, which MCC
        # answers with 401 (measured against a scratch server). OpenCode's own
        # ``ProviderConfig`` takes ``options.apiKey`` as a plain string, and
        # ``createOpenAICompatible`` sends it as the bearer verbatim.
        token_form=TokenForm.LITERAL_IN_APP_FILE,
        token_template="",
        token_env_var="",
        # No attribution header. MCC wrote one at ``options.headers`` until
        # 6.83.0 and OpenCode ignored it silently: its ``ProviderConfig.options``
        # declares ``apiKey, baseURL, enterpriseUrl, setCacheKey, timeout,
        # headerTimeout, chunkTimeout`` and nothing else -- checked against the
        # vendored real schema, ``tests/fixtures/schemas/opencode-config.schema.json``,
        # where the only two ``headers`` properties in the whole document
        # belong to MCP server entries. The block validated only because
        # ``options`` omits ``additionalProperties: false``. A key an app
        # provably drops is not attribution, it is noise in the user's file;
        # OpenCode is attributed by its user-agent fingerprint, exactly as
        # Goose is.
        attribution_header_field="",
        catalogue_format_id="opencode",
        # OpenCode's ``provider.<id>`` entry. ``options.baseURL`` and
        # ``options.apiKey`` are dotted because that is where OpenCode reads
        # them; ``models`` is a mapping keyed by model id, not a list.
        provider=DesktopProvider(
            base_url_key="options.baseURL",
            api_key_key="options.apiKey",
            headers_key="",
            models_key="models",
            constants={
                "npm": "@ai-sdk/openai-compatible",
                "name": DESKTOP_PROVIDER_LABEL,
            },
        ),
        open_command="opencode",
        notes=(
            "OpenCode's sidecar restarts on its own; the desktop shell does not.",
            "MCC's proxy token is written into opencode.json as "
            "options.apiKey, and the file is tightened to 0600 where the OS "
            "allows it. Undo removes it.",
        ),
    ),
    DesktopAppSpec(
        id="vscode_copilot",
        display_name="VS Code (Copilot custom endpoint)",
        summary=(
            "VS Code's custom chat endpoints live in a bare JSON array. The "
            "values are listed here for you to add by hand; MCC will not "
            "write a file no program on this machine has been seen reading."
        ),
        status=DesktopAppStatus.INSTRUCTIONS_ONLY,
        instructions_reason=(
            "2026-09-12: demoted from a Configure button. Spec section 5 lets "
            "a row keep its button only where MCC's generated document is "
            "validated in CI against a rule extracted from the application's "
            "own shipped code or published schema. The program that reads "
            "chatLanguageModels.json is GitHub Copilot Chat, and it is not "
            "installed on any machine this was developed against -- 39 "
            "extensions in ~/.vscode/extensions, none matching github.copilot* "
            "-- so there is no shipped code to extract the element's accepted "
            "keys, its vendor/apiType enums or its credential handling from. "
            "Everything MCC knows about this file came from documentation, "
            "which is exactly what produced the bug report this release "
            "answers. Copilot's own BYOK key is entered in its UI and kept in "
            "VS Code SecretStorage in any case, so a file MCC writes could "
            "never carry the credential. Re-check by installing Copilot Chat "
            "and extracting the rule from its bundle."
        ),
        doc_url="https://code.visualstudio.com/docs/copilot/customization/language-models",
        # The *extension* directory, not %APPDATA%\Code\User. That directory
        # proves VS Code is installed and says nothing about Copilot, so the
        # card read "Installed, not configured" and offered Configure on a
        # machine with 33 extensions and no Copilot among them. An extension
        # directory carries the extension's version, so it is a glob.
        detect=DesktopDetect(
            markers=(
                _home(".vscode", "extensions", "github.copilot-chat-*", glob=True),
                _home(".vscode", "extensions", "github.copilot-*", glob=True),
                _home(
                    ".vscode-server",
                    "extensions",
                    "github.copilot-chat-*",
                    glob=True,
                ),
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
        # No variable. ``${input:…}`` is not an environment reference: VS Code
        # resolves it itself by prompting once and keeping the answer in its
        # own SecretStorage. The card used to name MCC_AUTH_TOKEN here and tell
        # the reader to export it, which was an instruction to do something
        # that has no effect on this app.
        token_env_var="",
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
        # The element a human adds to the array by hand, in the order the
        # documentation lists the fields. These are the same values the
        # provider block above would have produced, which is deliberate: the
        # demotion is about who writes them, not about what they are.
        instruction_fields=(
            ("File", "%APPDATA%\\Code\\User\\chatLanguageModels.json"),
            ("name", DESKTOP_PROVIDER_LABEL),
            ("vendor", "customendpoint"),
            ("apiType", "openai"),
            ("url", "{root}"),
            ("apiKey", "${input:mcc_token}"),
            ("models", "the mcc/* routes you want listed"),
        ),
    ),
    DesktopAppSpec(
        id="crush_desktop",
        display_name="Crush",
        summary=(
            "Charm's Crush -- also the official client for Hyper/HyperCharm, "
            "which ships no client of its own -- takes an OpenAI-compatible "
            "provider block in its own JSON config."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://github.com/charmbracelet/crush",
        # ``%LOCALAPPDATA%\\crush`` used to be the marker. On the machine this
        # was written for it holds a catwalk provider catalogue and a
        # ``projects.json`` naming two sessions rooted in an MCC scratchpad --
        # downloaded by MCC's own ``mcc-crush.exe`` launcher -- and there is no
        # ``crush.exe`` anywhere on the machine. Crush installs through
        # homebrew, winget, scoop, npm, nix or ``go install``, and every one of
        # them puts the binary on PATH; ``go install`` with no PATH entry is
        # the one case the path markers cover.
        detect=DesktopDetect(
            binaries=("crush",),
            markers=(
                _home("go", "bin", "crush.exe", platforms=("win32",)),
                _home("go", "bin", "crush", platforms=("darwin", "linux")),
                _home(".local", "bin", "crush", platforms=("darwin", "linux")),
            ),
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
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        # The **root**, not ``/v1``, and this is the half of fix 14 that
        # matters. MCC shipped two different Crush documents for one
        # application: the launcher's ``~/.mcc/crush/crush.json`` wrote
        # ``type: "anthropic"`` with a bare root ``base_url``, and this row
        # wrote ``type: "openai-compat"`` with ``/v1``. Only one of the two was
        # ever put on the wire, and it was the launcher's: Crush's Anthropic
        # provider is ``anthropic-sdk-go``, which appends ``/v1/messages``
        # itself, so a root base URL produced ``POST /v1/messages`` and a
        # ``/v1`` one would have produced ``POST /v1/v1/messages``
        # (``application/catalogues/crush.py``, module docstring -- measured
        # against a local endpoint). The desktop row now writes the document
        # that was measured rather than the one that was assumed, so the two
        # agree key for key and the model ids -- ``models[].id``, MCC's
        # gateway id verbatim -- already did.
        base_url_shape=BaseUrlShape.ROOT,
        # ``$MCC_AUTH_TOKEN`` until 6.67.0, and nothing sets that variable, so
        # Crush sent ``Authorization: Bearer $MCC_AUTH_TOKEN`` -- 401,
        # measured. ``api_key`` takes a plain string.
        token_form=TokenForm.LITERAL_IN_APP_FILE,
        token_template="",
        token_env_var="",
        attribution_header_field="extra_headers",
        catalogue_format_id="crush",
        # Crush's ``providers.<id>`` entry, from ``crush schema`` v0.92.0
        # (vendored at ``tests/fixtures/schemas/crush.schema.json``).
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
                "type": "anthropic",
                "discover_models": False,
            },
        ),
        open_command="crush",
        notes=(
            "Crush reaches MCC through its Anthropic provider type, which "
            "appends /v1/messages to the base URL itself -- the same document "
            "mcc-crush writes, so the CLI and this card configure one "
            "application the same way.",
            "MCC's proxy token is written into crush.json as api_key, and the "
            "file is tightened to 0600 where the OS allows it. Undo removes it.",
        ),
    ),
    DesktopAppSpec(
        id="antigravity",
        display_name="Antigravity (agy CLI)",
        summary=(
            "Google's agy CLI speaks the public Gemini API when its "
            "modelProvider is set to gemini -- the surface MCC already serves "
            "-- but the endpoint and the key are environment variables, so "
            "this card shows the three values rather than writing one of them."
        ),
        status=DesktopAppStatus.INSTRUCTIONS_ONLY,
        instructions_reason=(
            "2026-09-12: demoted from a Configure button, and the file is the "
            "reason rather than the excuse. Read out of the shipped agy build "
            "(189,485,208 bytes, mtime 2026-09-07; strings only -- the binary "
            "is never executed, because a HOME redirect does not isolate its "
            "credential store and two control runs reached a real Google "
            "account), agy's own error string is: 'modelProvider is set to "
            "%q in settings.json, but the %s environment variable is not set. "
            'Set %s to your Gemini API key, or remove "modelProvider" from '
            "settings.json to use the default backend.' Two of the three "
            "values this app needs -- GEMINI_API_KEY and "
            "GOOGLE_GEMINI_BASE_URL -- have no field in any file, so a "
            "Configure that wrote the one key it can write would leave agy "
            "strictly worse than it found it: with modelProvider set and the "
            "variable unset, agy refuses to start against the default "
            "backend it was working with. MCC never sets a user-scope "
            "variable, so there is nothing here a button can honestly do."
        ),
        doc_url="https://antigravity.google/docs/cli/install/",
        # The binary, not ``~/.gemini/antigravity-cli`` -- which is a settings
        # directory, and the one MCC itself writes into.
        detect=DesktopDetect(
            binaries=("agy",),
            markers=(
                _windows_localappdata("agy", "bin", "agy.exe"),
                _home(".agy", "bin", "agy", platforms=("darwin", "linux")),
            ),
        ),
        # The path is agy's own, and it is the one MCC already declared. Spec
        # section 2.11 recorded that agy's strings "call ~/.gemini/
        # antigravity-cli/ a fixed bug" and name ~/.gemini/config/ as the
        # global configuration directory; re-extracting the strings on
        # 2026-09-12 does not support that. The binary's own embedded
        # documentation says, verbatim, "The CLI is configured via
        # `~/.gemini/antigravity-cli/settings.json`", and every ~/.gemini/
        # config/ string in the binary names MCP configuration, workflows or
        # skills -- mcp_config.json, workflows.json, global_workflows/,
        # skills/ -- never the CLI's settings. So the path stands; what is
        # wrong with this row is that no path can carry the credential or the
        # endpoint at all. See ``instructions_reason``.
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
            "Set modelProvider only together with the two variables. agy's "
            "own message is that modelProvider without GEMINI_API_KEY stops "
            "it using the backend it was signed in to, so half a "
            "configuration is worse than none.",
        ),
        # The three values, in the order agy's own changelog gives them.
        instruction_fields=(
            ("~/.gemini/antigravity-cli/settings.json", '{"modelProvider": "gemini"}'),
            ("GEMINI_API_KEY", "your MCC proxy token"),
            ("GOOGLE_GEMINI_BASE_URL", "{root}"),
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
        # Same correction as vscode_copilot: the marker has to be the
        # extension, not the editor that could host it.
        detect=DesktopDetect(
            markers=(
                _home(
                    ".vscode", "extensions", "rooveterinaryinc.roo-cline-*", glob=True
                ),
                _home(
                    ".vscode-server",
                    "extensions",
                    "rooveterinaryinc.roo-cline-*",
                    glob=True,
                ),
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
            # Roo Code's own import document, and every key below was read out
            # of the installed extension rather than out of prose: Roo Code
            # 3.54.0, ``~/.vscode/extensions/rooveterinaryinc.roo-cline-3.54.0/
            # dist/extension.js``. ``roo-cline.autoImportSettingsPath`` (``jpi``,
            # :5360, called from ``activate()`` at :5370) hands this file to
            # ``providerSettingsManager.import()``, which parses
            # ``z.object({providerProfiles, globalSettings})``. Everything else
            # lives in VS Code SecretStorage (``class TMe`` at :4236,
            # ``roo_cline_config_api_config`` in a DPAPI-encrypted
            # ``state.vscdb``), which no outside process can write -- so this
            # file plus that one settings key is the only route in, and it runs
            # at activation, hence the window reload on the card.
            #
            # ``apiProvider`` must be the literal ``"openai"`` -- not
            # ``openai-native``, not ``openai-compatible``. Both fields of
            # ``openAiCustomModelInfo`` (``AP``, :28) are **required**: a
            # profile missing either is skipped silently, which is the failure
            # mode this row exists to avoid. Unknown keys in a profile are
            # stripped rather than rejected. The base URL must carry ``/v1``
            # (the SDK appends ``/chat/completions``) and the key is sent as
            # ``Authorization: Bearer``; an unset key goes out as the literal
            # ``not-provided``.
            #
            # ``contextWindow`` is Roo's own context budget for the model, not
            # a claim about the endpoint, and MCC's resolution ladder is what
            # actually enforces a limit; 400000 is the value the extraction in
            # specs/PR-DESKTOP-APPS-CONFIGURE-SPEC.md §2.3 recorded for an
            # ``mcc/*`` tier. ``supportsPromptCache`` is false because the
            # tiers are refs that may resolve to any upstream, and claiming a
            # cache that is not there costs correctness rather than money.
            fields={
                "providerProfiles": {
                    "currentApiConfigName": DESKTOP_PROVIDER_LABEL,
                    "apiConfigs": {
                        DESKTOP_PROVIDER_LABEL: {
                            "apiProvider": "openai",
                            "openAiBaseUrl": "{base_url}",
                            "openAiApiKey": "{token}",
                            "openAiModelId": "{default_model}",
                            "openAiStreamingEnabled": True,
                            "openAiCustomModelInfo": {
                                "contextWindow": 400000,
                                "supportsPromptCache": False,
                            },
                        }
                    },
                }
            },
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        token_form=TokenForm.MCC_OWNED_FILE,
        attribution_header_field="",
        catalogue_format_id="",
        sidecar_path_key="roo-cline.autoImportSettingsPath",
        open_command="code",
        notes=(
            "Roo Code's export format carries the key in plaintext and "
            "resolves no reference, so MCC keeps it in a file of its own at "
            "mode 0600 rather than in a document the user edits.",
            "Reload the VS Code window for the import to run: Roo Code reads "
            "the imported file at activation only.",
            "The import re-runs on every window it activates in, so the "
            "profile comes back if it is deleted from Roo's own UI. Undo "
            "removes the settings key and the file together.",
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
        # ``~/.commandcode`` is where MCC's own ``mcc-commandcode`` launcher
        # writes the provider merge, so its existence can be MCC's doing. The
        # binary is the program.
        detect=DesktopDetect(binaries=("commandcode",)),
        document=DesktopDocument(
            paths=(_home(".commandcode", "providers.json"),),
            display_path="~/.commandcode/providers.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=("provider", DESKTOP_PROVIDER_ID),
            create_if_missing=False,
        ),
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        base_url_shape=BaseUrlShape.V1,
        # Command Code is the one app here that *refuses* a literal: its
        # ``parseProvider`` rejects a raw key with "raw secrets don't belong in
        # providers.json" and then leaves the provider with no key at all. So
        # it keeps a reference -- but the one the CLI half already uses.
        # ``MCC_COMMANDCODE_API_KEY`` is set by ``mcc-commandcode`` in the
        # child process it launches, so it is a variable MCC really does set;
        # ``MCC_AUTH_TOKEN``, which this row named until 6.67.0, is not set by
        # anything, and writing it here also disagreed with what the launcher
        # had written into the very same file.
        token_form=TokenForm.ENV_REFERENCE,
        token_template="${name}",
        token_env_var=COMMANDCODE_API_KEY_ENV,
        # And MCC is what sets it: ``mcc-commandcode`` exports it into the
        # process it launches. So its absence from the dashboard's own
        # environment is not evidence of anything, and this card must not
        # report ``credential_unresolved`` the way Goose's does.
        token_env_var_set_by_mcc=True,
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
            # Fix 16, and the honest half of it. The provider block lands and
            # nothing routes through it until Command Code is told to use it,
            # and the selection lives in a *different* document: read out of
            # the shipped bundle (command-code 1.50.0,
            # dist/cli.mjs), the config layers are merged by a loop that
            # treats "model" and "modelProvider" as one pair -- a layer
            # setting "model" without "modelProvider" deletes the inherited
            # "modelProvider" outright. So the two keys have to be written
            # together, in ~/.commandcode/config.json, which is not the
            # document this row owns. MCC will not reach into a second file to
            # overwrite the model a user picked, so the card says what to
            # pick instead.
            "Command Code does not switch to a provider just because it is "
            "declared. Open it and pick a My Claude Code model, or set both "
            '"modelProvider": "mcc" and "model" together in '
            "~/.commandcode/config.json -- Command Code drops modelProvider "
            "from any layer that sets model without it, so one without the "
            "other does nothing.",
            "Command Code refuses a raw key in providers.json -- its own "
            "message is that raw secrets do not belong there -- and accepts a "
            '"$ENV_VAR", "{env:VAR}" or "!command" reference. MCC writes '
            "$MCC_COMMANDCODE_API_KEY, and mcc-commandcode is what sets it.",
        ),
    ),
    DesktopAppSpec(
        id="claude_desktop",
        display_name="Claude Desktop",
        summary=(
            "Claude Desktop's native gateway mode is stored in a local "
            "configuration library. MCC owns one document there and merges a "
            "single key of the index; a managed profile outranks both."
        ),
        status=DesktopAppStatus.SERVABLE,
        doc_url="https://claude.com/docs/third-party/claude-desktop/mdm",
        detect=DesktopDetect(
            # Anthropic ships Windows and macOS builds; the community Linux
            # packages install a ``claude-desktop`` executable. Deliberately
            # **not** ``claude``, which on any machine with MCC on it is Claude
            # Code's own CLI and says nothing at all about the desktop app.
            binaries=("claude-desktop",),
            markers=(
                # The Microsoft Store / MSIX install, which is the shape most
                # Windows users get. A packaged app virtualises AppData, so its
                # %APPDATA%\Claude really lives under LocalCache\Roaming -- and
                # the package directory carries a publisher hash Microsoft
                # assigns, hence the glob.
                _windows_localappdata("Packages", "Claude_*", glob=True),
                # The Squirrel (per-user .exe) install, and the machine-wide
                # one. Both are program directories.
                _windows_localappdata("AnthropicClaude"),
                _windows_program_files("Claude"),
                DesktopPath(
                    env_vars=(),
                    relative_parts=("/Applications", "Claude.app"),
                    platforms=("darwin",),
                ),
                # Not ``%LOCALAPPDATA%\\Claude-3p``, ``%APPDATA%\\Claude`` or
                # their macOS and Linux twins, which were markers until 6.83.0:
                # those are the app's *data* directories, and the first of them
                # is the very configuration library MCC writes into -- so a
                # library MCC had created would have proved the app installed.
            ),
        ),
        # ``_meta.json`` is the library's index, and the only file here that is
        # the user's: MCC merges ``appliedId`` (the document the app loads on
        # next launch) and adds one element to ``entries`` beside whatever the
        # user authored in the app's own window. Everything else MCC writes
        # goes in the sidecar, which is a file of its own.
        document=DesktopDocument(
            paths=_claude_library_file("_meta.json"),
            display_path="%LOCALAPPDATA%\\Claude-3p\\configLibrary\\_meta.json",
            document_format=DocumentFormat.JSON,
            owned_key_path=(),
            owned_element_path=("entries",),
            match_field="id",
            match_value=CLAUDE_DESKTOP_CONFIG_ID,
            overwritten_keys=(("appliedId",),),
            # The library is a directory, and what MCC edits here is only its
            # index. A per-file backup of the index cannot restore a library,
            # which is what the 2026-09-08 incident proved.
            backup_directory=True,
        ),
        legacy_entries=(
            DesktopLegacyEntry(
                match_value=CLAUDE_DESKTOP_LEGACY_CONFIG_ID,
                sidecar_paths=_claude_library_file(
                    f"{CLAUDE_DESKTOP_LEGACY_CONFIG_ID}.json"
                ),
                reason=(
                    "MCC's configuration was stored under the id "
                    f"{CLAUDE_DESKTOP_LEGACY_CONFIG_ID}, which Claude Desktop "
                    "rejects at startup because it is not a 36-character "
                    "UUID -- and while it was the applied id the app ignored "
                    "its whole local configuration library. It has been moved "
                    f"to {CLAUDE_DESKTOP_CONFIG_ID}. Relaunch Claude Desktop."
                ),
            ),
        ),
        sidecar=DesktopSidecar(
            paths=_claude_library_file(f"{CLAUDE_DESKTOP_CONFIG_ID}.json"),
            display_path=(
                "%LOCALAPPDATA%\\Claude-3p\\configLibrary\\"
                f"{CLAUDE_DESKTOP_CONFIG_ID}.json"
            ),
            document_format=DocumentFormat.JSON,
            holds_credential=True,
            # Thirteen keys, and every one of them is a key the *working*
            # configuration on a real machine carries. The list used to come
            # from https://claude.com/docs/third-party/claude-desktop/configuration
            # alone; it is now reconciled key-for-key against the entry the user
            # built by hand and confirmed working on 2026-09-09, read from a
            # backup on 2026-09-10 (specs/CLAUDE-DESKTOP-CONFIG-REFERENCE.md).
            # Three differences came out of that reconciliation:
            #
            # * ``inferenceModels`` was **missing entirely**. It is what fills
            #   the app's model picker when discovery is off, and MCC listed
            #   nothing.
            # * ``modelDiscoveryEnabled`` was hard-coded ``True``. It is now
            #   ``False``, with the models named explicitly: a picker that
            #   depends on a network call finishing -- and on
            #   ``settings.harness_tier_aliases`` being on, which is what puts
            #   the ``mcc/*`` refs in ``/v1/models`` at all -- is a picker that
            #   can silently come up empty.
            # * ``inferenceCustomHeaders`` is **gone**. The key is real and
            #   current (``index.chunk--WuAOADe.js:30126``), but the working
            #   entry does not carry it, no run of the app has been watched
            #   accepting MCC's value, and the only thing it bought was
            #   attribution that the user-agent fingerprint already provides.
            #   A key nobody has watched an app read is what produced this bug
            #   report; it does not get written on documentation alone.
            #
            # ``static`` and ``gateway`` are the documentation's own enum
            # members and match the working entry exactly.
            # ``inferenceGatewayAuthScheme`` is still not written: the
            # documented default is ``bearer``, the app applies it, and the
            # working entry omits it too.
            #
            # 7.3.0 closed the last gap between this document and the entry
            # that was proven to work: the seven app preference keys in
            # ``CLAUDE_DESKTOP_APP_DEFAULTS`` are now shipped defaults of the
            # file MCC creates, and ``inferenceModels`` names the five
            # ``claude-*`` models rather than five ``mcc/*`` aliases the app's
            # own name filter rejects. ``banner`` stays out; see the constant.
            fields={
                "inferenceProvider": "gateway",
                "inferenceGatewayBaseUrl": "{base_url}",
                "inferenceGatewayApiKey": "{token}",
                "inferenceCredentialKind": "static",
                "modelDiscoveryEnabled": False,
                "inferenceModels": "{models}",
                **CLAUDE_DESKTOP_APP_DEFAULTS,
            },
            headers_key="",
            models_format_id="claude_desktop",
        ),
        managed_sources=(
            DesktopManagedSource(
                label="Machine policy (HKLM\\SOFTWARE\\Policies\\Claude)",
                registry_hive="HKLM",
                registry_subkey=CLAUDE_DESKTOP_POLICY_SUBKEY,
                exempt_keys=CLAUDE_DESKTOP_APP_BEHAVIOR_KEYS,
            ),
            DesktopManagedSource(
                label="User policy (HKCU\\SOFTWARE\\Policies\\Claude)",
                registry_hive="HKCU",
                registry_subkey=CLAUDE_DESKTOP_POLICY_SUBKEY,
                exempt_keys=CLAUDE_DESKTOP_APP_BEHAVIOR_KEYS,
            ),
            DesktopManagedSource(
                label=(
                    "Managed preferences "
                    "(/Library/Managed Preferences/"
                    "com.anthropic.claudefordesktop.plist)"
                ),
                path=DesktopPath(
                    env_vars=(),
                    relative_parts=(
                        "/Library",
                        "Managed Preferences",
                        "com.anthropic.claudefordesktop.plist",
                    ),
                    platforms=("darwin",),
                ),
                exempt_keys=CLAUDE_DESKTOP_APP_BEHAVIOR_KEYS,
            ),
            DesktopManagedSource(
                label="Managed settings (/etc/claude-desktop/managed-settings.json)",
                path=DesktopPath(
                    env_vars=(),
                    relative_parts=(
                        "/etc",
                        "claude-desktop",
                        "managed-settings.json",
                    ),
                    platforms=("linux",),
                ),
                exempt_keys=CLAUDE_DESKTOP_APP_BEHAVIOR_KEYS,
            ),
        ),
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        base_url_shape=BaseUrlShape.ROOT,
        # The store resolves no reference form: ``inferenceGatewayApiKey`` is
        # documented as a plain string, and the only indirection the app offers
        # is ``inferenceCredentialKind: helper-script``, which runs a script
        # rather than expanding a variable. So the literal goes into a file MCC
        # owns outright, written 0600 -- the Kimi and Roo Code precedent -- and
        # never into a document the user edits.
        token_form=TokenForm.MCC_OWNED_FILE,
        token_env_var="",
        # Empty since 6.67.0. ``inferenceCustomHeaders`` exists and is current,
        # but the configuration proven to work on a real machine does not carry
        # it and no run of the app has been watched accepting MCC's value, so
        # this card is attributed by user-agent like Goose rather than by a key
        # written on the strength of documentation.
        attribution_header_field="",
        catalogue_format_id="",
        # MCC's element of ``_meta.json.entries``. ``id`` is written by the
        # merge engine from ``match_value``, so only the label belongs here --
        # it is what the app's own configuration picker shows beside whatever
        # the user has authored.
        provider=DesktopProvider(constants={"name": CLAUDE_DESKTOP_CONFIG_NAME}),
        open_command="",
        notes=(
            "Configure writes MCC's own document into Claude Desktop's local "
            "configuration library and points the library's appliedId at it. "
            "Your own saved configurations are left exactly where they are, "
            "and Undo deletes MCC's document and puts appliedId back.",
            "The local library is the *lowest*-precedence source. A managed "
            "profile under HKLM or HKCU\\SOFTWARE\\Policies\\Claude (macOS: "
            "/Library/Managed Preferences) silently outranks it and makes the "
            "app's own configuration window read-only, so MCC checks for one "
            "and refuses to write rather than leaving you a file that does "
            "nothing.",
            "Relaunch Claude Desktop to load it: the app reads this library at "
            "startup. Help -> Troubleshooting -> Enable Developer Mode, then "
            "Developer -> Configure Third-Party Inference shows what it read.",
            "The desktop app does not honour ANTHROPIC_BASE_URL. Its Code tab "
            "reads ~/.claude/settings.json, which Configure Claude Code "
            "already covers.",
            "The model picker is filled from five models MCC names in the "
            "configuration -- Mythos 5.1, Fable 5.1, Opus 5, Sonnet 5 and "
            "Haiku 4.5 -- rather than from a discovery call: model discovery "
            "is written off, so the picker cannot come up empty because a "
            "request was slow. Each name is a display alias MCC resolves "
            "itself: claude-mythos-5.1 goes to mcc/cyber, claude-fable-5.1 to "
            "mcc/best, claude-opus-5 to mcc/good, claude-sonnet-5 to "
            "mcc/medium and claude-haiku-4.5 to mcc/cheap. Claude Desktop "
            "accepts only names that look like Claude models, which is why "
            "they are not spelled mcc/best.",
            "MCC's own document also carries this app's preference keys "
            "(desktop extensions, 1M context, telemetry off, Auto mode, the "
            "WebFetch preflight, Claude.ai import) at the values a working "
            "configuration uses. They are defaults of a file MCC creates, not "
            "edits to yours, and Undo deletes the file whole. Your "
            "organisation banner is never written.",
            "Before its first edit MCC copies the whole configuration library "
            "into a timestamped folder under MCC's own configuration "
            "directory. The library is a directory of documents and the file "
            "MCC merges is only its index, so a per-file backup could not "
            "have restored it.",
            "If an earlier release of MCC configured this app, the next status "
            "poll repairs it: the configuration MCC stored under an id Claude "
            "Desktop rejects at startup is moved to a valid one, appliedId is "
            "corrected, and the old file is deleted. A library that has "
            "already been repaired -- including by hand -- is left untouched.",
        ),
        # Kept for the two cases where no button can help: the configuration
        # library is absent because the app has never run in third-party mode,
        # or a managed profile owns the device. The labels are the dialog's own
        # (https://claude.com/docs/third-party/claude-desktop/in-app-configuration).
        instruction_fields=(
            ("Inference provider", "Gateway"),
            ("Gateway base URL", "{root}"),
            ("Gateway auth scheme", "Bearer"),
            (
                "Gateway API key",
                "MCC's proxy auth token -- the ANTHROPIC_AUTH_TOKEN value in "
                "MCC's own .env, which Configure Claude Code writes for you",
            ),
            ("Credential kind", "Static API key"),
            (
                "Model discovery",
                "Off, and name claude-mythos-5.1, claude-fable-5.1, "
                "claude-opus-5, claude-sonnet-5 and claude-haiku-4.5 as the "
                "models -- with tier aliases mythos, fable, opus, sonnet and "
                "haiku respectively, each marked the default for its tier",
            ),
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
