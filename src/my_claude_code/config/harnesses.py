"""The one registry of coding-agent harnesses MCC can launch.

A *harness* is a third-party coding-agent CLI that MCC serves: Claude Code,
Codex, Pi. It is not a *provider* -- ``config.provider_catalog`` names the
upstream gateways MCC buys tokens from, and several of those share a name with
a CLI (``opencode``, ``commandcode``, ``cline``). The two are unrelated and can
be on at once: a harness sits downstream of MCC, a provider upstream of it.
``harness_id`` and ``provider_id`` are therefore separate namespaces and must
not be joined.

The sharpest case of that is Command Code, because both halves ship in this
release. The *provider* ``commandcode`` (``config/provider_catalog.py``) is the
Command Code gateway MCC sends requests **to**, authenticated with a
``COMMANDCODE_API_KEY`` the user bought. The *harness* ``commandcode_cli``
below is the ``command-code`` CLI MCC serves requests **for**, over
``POST /v1/messages`` on this machine. The ids differ on purpose -- a shared id
would make one catalogue lookup silently answer for the other -- and a user can
run ``mcc-commandcode`` routed to ``anthropic`` with the ``commandcode``
provider switched off entirely.

Kimi Code is the second such pair and the one most likely to be misread,
because the two halves do not even share a spelling. The *providers* ``kimi``
and ``kimi_coding`` in ``config/provider_catalog.py`` are Moonshot endpoints
MCC sends requests **to**, paid for with a Moonshot key. The *harness*
``kimi_code`` below is Moonshot's ``kimi`` CLI, a Python tool published on
PyPI as ``kimi-cli``, which MCC serves requests **for** over
``POST /v1/messages``. ``mcc-kimi`` does not require a Moonshot account, a
Moonshot key or either of those providers being switched on: it declares a
provider of type ``anthropic`` pointed at this proxy, and every model in its
picker is whatever the ladder resolved. Reusing ``kimi`` as the harness id
would have made ``harness_spec("kimi")`` and ``PROVIDER_CATALOG["kimi"]``
resolve two different things under one name, which is exactly the class of bug
these separate namespaces exist to prevent.

Everything a harness needs stated once lives here, because the alternative is
what this module replaced: the same three ids written out by hand in
``pyproject.toml``, both installers, ``mcc-help``, the RTK state file, the RTK
CLI, the tray menu and the dashboard -- eight places to forget one. Contract
tests compare each of those surfaces back to this tuple.

This module holds *data only*. It lives in ``config`` rather than beside the
launchers because ``api`` may not import ``cli``
(``tests/contracts/test_import_boundaries.py``) and the dashboard has to be
able to list harnesses. The behaviour -- environment builders, argv builders,
binary probes -- stays in ``cli/launchers`` and is bound to a spec by
``cli/harnesses/registry.py``.
"""

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_claude_code.config.install_methods import (
    AIDER_METHODS,
    ANTIGRAVITY_NO_BUTTON_REASON,
    CLAUDE_METHODS,
    CLINE_METHODS,
    CODEX_METHODS,
    COMMANDCODE_METHODS,
    CRUSH_METHODS,
    DROID_METHODS,
    GEMINI_METHODS,
    GOOSE_METHODS,
    HERMES_METHODS,
    KILO_METHODS,
    KIMI_NO_BUTTON_REASON,
    OPENCLAW_METHODS,
    OPENCODE2_METHODS,
    OPENCODE_METHODS,
    PI_METHODS,
    QWEN_METHODS,
    InstallMethod,
)


class HarnessProtocol(StrEnum):
    """The inbound MCC route a harness talks to."""

    ANTHROPIC_MESSAGES = "anthropic_messages"
    OPENAI_RESPONSES = "openai_responses"
    #: The oldest OpenAI surface and the one every "OpenAI-compatible" client
    #: speaks. No harness in this registry targets it yet -- MCC serves it so
    #: that the clients which only speak it (IDE plugins, Cline, Aider, Goose)
    #: can be pointed here at all -- but the member exists because the Coding
    #: agents page names each harness's protocol from this enum, and a harness
    #: added later must not have to invent the word.
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    #: Google's own protocol. The clients that speak it speak nothing else --
    #: Gemini CLI, everything built on ``@google/genai`` -- which is the whole
    #: reason MCC serves it: an OpenAI-shaped endpoint is not an option for
    #: them at any price.
    GEMINI = "gemini"


#: The two variables the OpenCode-family generated config refers to through
#: OpenCode's own ``{env:VARIABLE}`` substitution, and that the launcher sets
#: in the child process only. They live here rather than beside either user
#: because the serialiser writes the placeholder and the launcher supplies the
#: value: a rename in one place and not the other would produce a config that
#: parses and cannot authenticate. ``cli`` may not import ``application``, so
#: ``config`` is the one module both are allowed to agree through.
OPENCODE_BASE_URL_ENV = "MCC_OPENCODE_BASE_URL"
OPENCODE_API_KEY_ENV = "MCC_OPENCODE_API_KEY"

#: The variable Command Code's ``providers.json`` refers to as ``"$VAR"``, and
#: that ``mcc-commandcode`` sets in the launched process only. It is named here
#: for the same reason as the two above: the serialiser writes the reference
#: and the launcher supplies the value, and ``cli`` may not import
#: ``application``, so ``config`` is the one module both may agree through.
#: Because the merged file *is* the user's own document, this indirection is
#: what keeps the proxy token off disk entirely.
COMMANDCODE_API_KEY_ENV = "MCC_COMMANDCODE_API_KEY"

#: Command Code validates ``provider.<id>.baseURL`` with ``new URL(...)`` and
#: skips the whole provider when it does not parse, and it applies no
#: substitution to that field -- only ``apiKey`` is expanded. The serialiser is
#: a pure function of the model records and does not know which port this
#: install listens on, so it writes this sentinel and the caller replaces it
#: with the real proxy root before the block reaches disk. It is a valid
#: absolute URL on purpose: a substitution that somehow did not happen leaves
#: a provider that fails loudly on connect rather than one Command Code drops
#: at load time with a warning nobody reads.
COMMANDCODE_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"

#: Kimi Code's two placeholders, replaced by the caller for the same reason
#: Command Code's ``baseURL`` one is: the serialiser is a pure function of the
#: model records and knows neither which port this install listens on nor what
#: the proxy token is.
#:
#: Kimi is the first harness where the *key* has to be a placeholder too.
#: ``kimi_cli.config.LLMProvider.api_key`` is a plain ``SecretStr`` -- there is
#: no ``"$VAR"``, ``"{env:VAR}"`` or ``"!command"`` form to write instead --
#: and ``kimi_cli.llm.augment_provider_with_env_vars`` overrides ``api_key``
#: from the environment only for provider types ``kimi``, ``openai_legacy``
#: and ``openai_responses``; an ``anthropic`` provider falls through its
#: ``case _: pass``. So the value has to be literal in the document, and
#: ``ARCHITECTURE.md`` records why that is acceptable here and nowhere else:
#: the document is MCC's own file under ``~/.mcc``, in the same directory as
#: ``~/.mcc/.env``, which already holds the identical ``ANTHROPIC_AUTH_TOKEN``
#: in clear. Nothing is written into a file the user owns.
KIMI_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"
KIMI_API_KEY_SENTINEL = "mcc-proxy-token-placeholder"

#: Qwen Code names an *environment variable* in its settings document --
#: ``modelProviders.anthropic[].envKey`` -- and reads ``process.env[envKey]``
#: at request time without ever storing the value. ``mcc-qwen`` sets this
#: variable in the launched process only, so the proxy token never lands on
#: disk even though MCC owns the settings file it points Qwen at.
QWEN_API_KEY_ENV = "MCC_QWEN_API_KEY"

#: Crush's own documented secret reference is the ``$VAR`` form -- its schema
#: gives ``"$OPENAI_API_KEY"`` as the example for ``providers.<id>.api_key``.
#: Same guarantee as Qwen's: the value exists only in the child process.
CRUSH_API_KEY_ENV = "MCC_CRUSH_API_KEY"

#: Base-URL placeholders for the two harnesses whose documents MCC owns whole.
#: Neither CLI substitutes anything into its base-URL field, and neither
#: serialiser knows which port this install listens on, so the sentinel is
#: written and ``config/harness_base_url.with_root_base_url`` resolves it on
#: the way to disk. They carry ``/v1`` on purpose: a substitution that somehow
#: did not happen leaves a URL that fails loudly on connect rather than one
#: the CLI quietly treats as a real host.
QWEN_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"
CRUSH_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"

#: VS Code appends the method path itself according to ``apiType``, so its
#: custom endpoint takes the proxy **root** -- a trailing ``/v1`` would produce
#: ``/v1/v1/chat/completions``. Same shape, different reason, as Crush's
#: anthropic provider above.
VSCODE_BASE_URL_SENTINEL = "https://base-url.mcc.invalid"

#: Goose splits host from path: this is the host half, and
#: ``GOOSE_BASE_PATH_VALUE`` above is the other.
GOOSE_BASE_URL_SENTINEL = "https://base-url.mcc.invalid"

#: Cline's own secret reference does not exist: ``providers.json`` stores a
#: plain ``apiKey`` string, and the runtime fallback to ``$OPENAI_API_KEY``
#: only fires when the key is *absent* -- which, measured on 3.0.61, leaves the
#: CLI blocking indefinitely rather than authenticating. So the value is
#: literal, and ``ARCHITECTURE.md`` records why that is acceptable here for the
#: same reason it is for Kimi Code: the document is MCC's own file under
#: ``~/.mcc/cline``, in the same directory tree as ``~/.mcc/.env``, which
#: already holds the identical ``ANTHROPIC_AUTH_TOKEN`` in clear. Nothing is
#: written into a file the user owns -- ``~/.cline`` is never read or touched,
#: because ``cline --config`` moves the whole configuration directory.
CLINE_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"
CLINE_API_KEY_SENTINEL = "mcc-proxy-token-placeholder"

#: The provider id MCC declares inside Cline's ``providers.json``. Cline's own
#: registry publishes ``openai-compatible`` as "OpenAI-compatible chat
#: completions endpoint" with no ``modelsSourceUrl``, which is exactly the
#: shape MCC needs: an arbitrary ``baseUrl`` and no discovery call to a route
#: this proxy does not serve under that provider. ``openai-native`` was the
#: spec's suggestion and is the wrong one -- it is OpenAI's own hosted
#: provider entry, and ``openai`` is merely an alias that normalises to
#: ``openai-compatible`` anyway.
CLINE_PROVIDER_ID = "openai-compatible"

#: The variables ``mcc-goose`` sets in the launched process, and nothing else.
#: Goose is the one harness in this release with no generated file at all:
#: its configuration directory is ``%APPDATA%\Block\goose\config`` (or
#: ``~/.config/goose``), it publishes no variable or flag that moves the config
#: file alone, and the only file-shaped mechanism -- a declarative provider
#: under ``custom_providers/`` -- would put an MCC-owned document *inside* the
#: user's own directory, beside their sessions and secrets. That is the one
#: thing this project does not do. Goose takes everything it needs from the
#: environment instead, so ``mcc-goose`` writes nothing anywhere.
GOOSE_PROVIDER_VALUE = "openai"
GOOSE_BASE_PATH_VALUE = "v1/chat/completions"

#: Aider takes its base URL and key from LiteLLM's own OpenAI variables. Both
#: are set in the launched process only, so the proxy token never lands on
#: disk even though MCC owns the two documents it points Aider at.
#: ``OPENAI_BASE_URL`` outranks ``OPENAI_API_BASE`` in LiteLLM's resolution
#: order, so both are written to the same value rather than one being left to
#: a stale inherited value.
AIDER_BASE_URL_ENV = "OPENAI_BASE_URL"
AIDER_LEGACY_BASE_URL_ENV = "OPENAI_API_BASE"
AIDER_API_KEY_ENV = "OPENAI_API_KEY"

#: Droid's own documented secret reference is the ``${VAR}`` form -- its BYOK
#: page gives it for ``customModels[].apiKey`` and the bundle carries an
#: ``expandSettingsEnvVarRefs`` pass with the matching regex. MCC writes the
#: reference and ``mcc-droid`` sets the variable in the child process only, so
#: the proxy token never lands on disk.
DROID_API_KEY_ENV = "MCC_DROID_API_KEY"
DROID_API_KEY_REFERENCE = "${MCC_DROID_API_KEY}"

#: OpenClaw's credential, the two constants that describe the block MCC writes,
#: and the three variables the CLI itself reads to find its file.
#:
#: ``openclaw`` resolves its configuration the way its own ``dist/*.mjs`` does:
#: ``OPENCLAW_CONFIG_PATH`` names the file outright, else ``OPENCLAW_STATE_DIR``
#: holds it, else it is ``<home>/.openclaw/openclaw.json`` with ``OPENCLAW_HOME``
#: outranking ``HOME`` and ``USERPROFILE``. MCC follows that order rather than
#: ``Path.home()`` for the reason ``config/harness_config_merge.py`` states:
#: writing one file while the CLI reads another fails silently, as a picker
#: that never shows MCC's models.
#:
#: The proxy token never lands on disk. OpenClaw expands a ``$VAR`` reference in
#: ``apiKey`` from the process environment, and ``mcc-openclaw`` sets this
#: variable in the child process only. The base URL beside it *is* written
#: literally -- a loopback address on the user's own machine, not a secret --
#: because OpenClaw validates it as a URL and substitutes nothing into it.
OPENCLAW_API_KEY_ENV = "MCC_OPENCLAW_API_KEY"
OPENCLAW_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"

#: The three variables OpenClaw itself reads to find its document, named here
#: so the merge spec and the documentation cannot disagree about their
#: spelling -- and so the docs guard, which only knows names the harness layer
#: declares, can see that they are real.
OPENCLAW_CONFIG_PATH_ENV = "OPENCLAW_CONFIG_PATH"
OPENCLAW_STATE_DIR_ENV = "OPENCLAW_STATE_DIR"
OPENCLAW_HOME_ENV = "OPENCLAW_HOME"

#: The provider id MCC owns inside ``models.providers``.
OPENCLAW_PROVIDER_ID = "mcc"

#: The one key MCC *replaces* rather than creates in the user's document:
#: OpenClaw's default model. Configure records what it held (a string, an
#: absent key, or an object with its own ``primary``), and Undo puts it back.
OPENCLAW_PRIMARY_MODEL_PATH: tuple[str, ...] = (
    "agents",
    "defaults",
    "model",
    "primary",
)


#: Hermes Agent's credential, the base-URL placeholder, and the variables the
#: CLI itself reads to find its document.
#:
#: Hermes is Nous Research's ``hermes-agent`` (PyPI, console scripts ``hermes``,
#: ``hermes-agent`` and ``hermes-acp``). Its configuration is
#: ``<home>/config.yaml``, where the home is ``HERMES_HOME`` when set, else
#: ``%LOCALAPPDATA%\\hermes`` on Windows, else ``~/.hermes``
#: (``hermes_constants.py:46-67`` of the published 0.19.0 wheel).
#:
#: The proxy token never lands on disk. ``providers.mcc.key_env`` carries the
#: *name* of an environment variable -- Hermes reads ``os.environ[key_env]`` at
#: request time and stores no value -- and ``mcc-hermes`` sets it in the child
#: process only. The coincidentally named ``MCC_HERMES_API_KEY`` is the one
#: ``mcc-hermes`` writes, and it is a reference in the same sense OpenClaw's
#: ``$MCC_OPENCLAW_API_KEY`` and Crush's ``$MCC_CRUSH_API_KEY`` are.
HERMES_API_KEY_ENV = "MCC_HERMES_API_KEY"

#: Written into ``providers.mcc.api`` and replaced on the way to disk with
#: ``<proxy root>/v1``: Hermes' OpenAI client builds its URL as
#: ``f"{base_url}{'/chat/completions'}"`` (``agent/agent_runtime_helpers.py:1626``),
#: so the ``/v1`` belongs in the value -- the same shape Cline's
#: ``base_url_shape = "v1"`` declares.
HERMES_BASE_URL_SENTINEL = "https://base-url.mcc.invalid/v1"

#: The provider id MCC owns inside ``providers``. Hermes addresses a model as
#: ``<provider>/<model>`` in its picker, so this id is also the prefix a reader
#: sees there.
HERMES_PROVIDER_ID = "mcc"

#: The variable Hermes itself reads to relocate its whole home (config,
#: sessions, skills, plugins and credentials). MCC follows it so the file it
#: writes is the file the CLI reads -- it never *sets* it.
HERMES_HOME_ENV = "HERMES_HOME"


#: Droid's base-URL placeholder. Resolved to the proxy **root** with no
#: ``/v1``: MCC's Droid entry declares ``provider: "anthropic"``, which
#: instantiates the bundled ``@anthropic-ai/sdk`` client, and that appends
#: ``/v1/messages`` itself.
DROID_BASE_URL_SENTINEL = "https://base-url.mcc.invalid"

#: ``$version`` in a Qwen settings document. Qwen migrates and *rewrites* any
#: settings file older than its own ``SETTINGS_VERSION``, and MCC's catalogue
#: is a file it would happily rewrite. Declaring the version keeps the
#: generated document exactly as generated -- observed: without it, Qwen Code
#: 0.15.11 rewrote MCC's file on first read.
QWEN_SETTINGS_VERSION = 4

#: The placeholder a catalogue serialiser writes wherever the generated
#: document declares MCC's own attribution header, and that the caller replaces
#: with the real harness id on the way to disk.
#:
#: The header itself is ``core.client_fingerprint.HARNESS_HEADER`` -- named
#: there, not here, because ``config`` describes the harnesses and ``core``
#: owns the wire vocabulary the proxy reads back. The *value* needs a sentinel
#: for the same reason the base URLs above do, and for one more: a serialiser
#: is a pure function of the model records
#: (``application/catalogues/__init__.py`` types it
#: ``Iterable[CatalogueModel] -> document``), so it cannot know whether the
#: document it is building is OpenCode's, OpenCode 2's or Kilo's -- all three
#: share one serialiser and only the *caller* holds the ``HarnessSpec``. So the
#: serialiser writes this and
#: ``config/harness_attribution.with_harness_id`` resolves it, in the two
#: places a document reaches disk: ``cli/harnesses/catalogue_client`` for a
#: launch and ``runtime/harness_catalogues`` for the background refresh.
#:
#: It is deliberately shaped so a substitution that did not happen is loud: it
#: is not a registry id, it does not look like one, and it survives into the
#: request log verbatim rather than being mistaken for a real harness.
MCC_HARNESS_ID_SENTINEL = "{{mcc_harness_id}}"

#: The harnesses that deliberately send **no** ``x-mcc-harness`` header, with
#: the reason, so the omission reads as a decision rather than an oversight.
#: Every one of them is attributed by user-agent instead
#: (``core/client_fingerprint._HARNESS_UA_TABLE``), which costs the certainty
#: an explicit header would give and nothing else.
#:
#: * ``kimi_code`` -- ``kimi_cli.config.LLMProvider`` has no header field at
#:   all; its ``anthropic`` provider carries a base URL, a key and a model.
#: * ``aider`` -- configured entirely through LiteLLM's OpenAI variables and
#:   two model-fact documents, neither of which describes a request header.
#: * ``droid`` -- ``customModels[]`` entries declare a provider, a base URL, a
#:   key reference and limits; the bundle exposes no per-model header map.
#: * ``goose`` -- MCC writes Goose no file at all (see
#:   ``GOOSE_PROVIDER_VALUE`` above) and Goose publishes no header variable.
#: * ``antigravity`` -- ``agy`` publishes no custom-header field on any of the
#:   three surfaces it reads (settings key, ``GEMINI_API_KEY``,
#:   ``GOOGLE_GEMINI_BASE_URL``), so there is nowhere to put one. Measured
#:   2026-09-07; see ``ANTIGRAVITY_UNAVAILABLE_REASON``.
#:
#: Stated as data rather than prose because a contract test asserts that the
#: generated document for each of these carries no header, so adding a hook
#: later means deleting a line here and watching that test tell you where.
HARNESSES_WITHOUT_ATTRIBUTION_HEADER: frozenset[str] = frozenset(
    {"kimi_code", "aider", "droid", "goose", "antigravity"}
)


#: Why there is no ``mcc-antigravity`` *launcher*, and where Antigravity is
#: served instead. Dated, because the previous version of this text was not
#: wrong when it was written and was wrong by the time anyone re-read it.
#:
#: **The 1.0.14 verdict has been superseded.** That measurement was taken
#: against a JavaScript bundle. Re-measured 2026-09-07 against the installed Go
#: build (187,601,560 bytes, mtime 2026-09-02), with ``HOME``/``USERPROFILE``
#: redirected to a scratch directory and a logging HTTP server standing in for
#: MCC on a scratch port. With ``~/.gemini/antigravity-cli/settings.json``
#: holding ``{"modelProvider": "gemini"}``, ``GEMINI_API_KEY`` set to a scratch
#: string and ``GOOGLE_GEMINI_BASE_URL`` pointed at that server, ``agy -p`` sent::
#:
#:     POST /v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse
#:     x-goog-api-key: <the scratch string>
#:
#: That is the **public** Gemini API -- the surface MCC already serves at
#: ``/v1beta`` (``api/gemini_routes.py``) -- and not the private
#: ``v1internal`` Code Assist protocol the old reason described. So the two
#: claims below are the ones that survive, and both were measured rather than
#: inferred:
#:
#: * ``modelProvider`` is the switch, not the base URL. With the key omitted,
#:   nothing reached the scratch server at all and ``agy`` answered from
#:   Google's own backend: on Windows its credential store survives a ``HOME``
#:   redirect, so the environment variables alone are not enough.
#: * ``agy`` validates ``--model`` against its own catalogue **before** issuing
#:   any request -- ``model mcc/best is not recognized as a known model or
#:   custom model in settings`` -- so the ``mcc/*`` tier aliases cannot be
#:   named. Models are chosen by ``agy``'s own ids and MCC routes them through
#:   the resolution ladder like any other inbound model name.
#:
#: **Why the Coding agents card and not a launcher.** Every other harness gets
#: an ``mcc-<agent>`` launcher because MCC can hand the CLI its endpoint and
#: token through the child process's environment and point it at a settings
#: document MCC owns -- Gemini CLI's ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` is the
#: template. ``agy`` publishes no equivalent override that has been verified
#: here, so a launcher would have to write the user's own
#: ``~/.gemini/antigravity-cli/settings.json`` -- which is exactly what the
#: **Desktop apps** card already does, with a one-time backup and a two-mode
#: undo. Adding a launcher would duplicate that mechanism without adding a
#: capability, so Antigravity is served there and this entry says so.
ANTIGRAVITY_UNAVAILABLE_REASON = (
    "Antigravity is served from the Desktop apps card rather than an "
    "mcc-antigravity launcher. Re-measured 2026-09-07 against the installed "
    'agy Go build: with modelProvider set to "gemini" in '
    "~/.gemini/antigravity-cli/settings.json, plus GEMINI_API_KEY and "
    "GOOGLE_GEMINI_BASE_URL, agy sends POST "
    "/v1beta/models/{model}:streamGenerateContent with x-goog-api-key -- the "
    "public Gemini API this proxy already serves at /v1beta. Two limits: "
    "modelProvider is the switch (without it agy uses Google's backend "
    "whatever the base URL says), and agy rejects the mcc/* tier aliases "
    "before sending, so models are named with agy's own ids. The 2026-09-02 "
    "verdict against agy 1.0.14 -- no API-key path, private v1internal "
    "protocol -- no longer holds for this build."
)


PROTOCOL_LABELS: dict[HarnessProtocol, str] = {
    HarnessProtocol.ANTHROPIC_MESSAGES: "Anthropic Messages (POST /v1/messages)",
    HarnessProtocol.OPENAI_RESPONSES: "OpenAI Responses (POST /v1/responses)",
    HarnessProtocol.OPENAI_CHAT_COMPLETIONS: (
        "OpenAI Chat Completions (POST /v1/chat/completions)"
    ),
    HarnessProtocol.GEMINI: (
        "Google Gemini (POST /v1beta/models/{model}:generateContent)"
    ),
}


@dataclass(frozen=True, slots=True)
class HarnessInvocation:
    """One documented way of calling an installed console script.

    The Coding agents page lists these verbatim with a copy button, so the
    string has to be the line a user can paste. ``arguments`` is appended to
    the command name; an empty string is the bare command itself.
    """

    arguments: str = ""
    help_text: str = ""


@dataclass(frozen=True, slots=True)
class HarnessCommandLine:
    """One copyable command line, with the sentence explaining what it does."""

    command: str
    help_text: str
    #: ``primary`` is the headline command, ``flag`` a documented argument
    #: form, ``legacy`` the retired ``fcc-`` name, ``rtk`` a token-optimizer toggle.
    kind: str = "primary"


@dataclass(frozen=True, slots=True)
class HarnessCommand:
    """One installed console script that launches a harness."""

    #: Suffix after ``mcc-``; ``"codex"`` gives ``mcc-codex``.
    suffix: str
    #: ``module:function`` target, exactly as ``pyproject.toml`` spells it.
    target: str
    #: Whether a retired ``fcc-<suffix>`` name exists. Only the harnesses that
    #: shipped before this registry have one: no install in the world carries
    #: an ``fcc-`` name for a harness added later, so inventing one would
    #: publish a tombstone nobody ever needed. Since 7.0.0 the name is
    #: registered only so that typing it prints what replaced it.
    legacy_alias: bool = True
    #: Whether this is the harness's headline command in generated help.
    primary: bool = True
    #: Trailing text for the generated ``mcc-help`` line.
    help_text: str = ""
    #: Documented argument forms beyond the bare command. Listed on the
    #: dashboard so the answer to "what else can I type" is in the registry
    #: rather than in three prose pages that drift apart.
    invocations: tuple[HarnessInvocation, ...] = ()

    @property
    def command(self) -> str:
        """Return the native ``mcc-`` command name."""

        return f"mcc-{self.suffix}"

    @property
    def legacy_command(self) -> str | None:
        """Return the retired ``fcc-`` name, when this command has one."""

        return f"fcc-{self.suffix}" if self.legacy_alias else None


@dataclass(frozen=True, slots=True)
class MergePathRung:
    """One step of a CLI's own search for its configuration document.

    The four fields on :class:`HarnessConfigMerge` before this one
    (``path_env_var``, ``state_env_var``, ``home_override_env_var`` and
    ``home_env_vars``) describe every merge target whose document is
    *home-relative the same way on every platform*: Command Code's
    ``~/.commandcode/providers.json`` and OpenClaw's
    ``~/.openclaw/openclaw.json`` both are. Hermes is not -- its home is
    ``%LOCALAPPDATA%\\hermes`` on Windows and ``~/.hermes`` everywhere else,
    with ``HERMES_HOME`` outranking both -- and a platform-dependent path with
    no declared home for it to fall back to is the one thing a resolver must
    not guess at (see ``_detection_home``'s note on writing one file while the
    CLI reads another).

    So a spec declares its own ordered rungs instead: the first one that
    resolves wins, exactly as the CLI resolves them. It is data rather than a
    method on the spec so the same table feeds the launcher, the background
    refresh and the dashboard card, and so a test can assert the order.
    """

    #: The environment variable this rung consults, or ``None`` for the
    #: operating system's own home folder.
    env_var: str | None = None
    #: The document's path below the base this rung names. Always spelled in
    #: full -- a rung is a whole path from its base, not a fragment.
    parts: tuple[str, ...] = ()
    #: ``sys.platform`` values this rung applies to; empty means every one.
    #: Order carries the precedence, so a platform-specific rung listed before
    #: a universal one is "on this platform, take this; everywhere else, the
    #: next rung".
    platforms: tuple[str, ...] = ()

    def applies_to(self, platform: str) -> bool:
        """Return whether this rung is one this platform walks at all."""

        return not self.platforms or platform in self.platforms


#: Hermes Agent's own search order for ``config.yaml``, transcribed from
#: ``hermes_constants.py:46-67`` of the published 0.19.0 wheel:
#:
#: * ``HERMES_HOME`` when set -- the CLI's own relocation variable, which wins
#:   over everything below;
#: * on Windows, ``%LOCALAPPDATA%\\hermes``;
#: * on Windows with ``LOCALAPPDATA`` unset, ``~/AppData/Local/hermes`` -- the
#:   wheel's own fallback for that case, kept because a service account can
#:   genuinely have no ``LOCALAPPDATA``;
#: * everywhere else, ``~/.hermes``.
#:
#: The two Windows rungs precede the universal one on purpose: the rung list is
#: ordered, so on Windows the universal ``~/.hermes`` rung is never reached, and
#: on every other platform the Windows rungs are filtered out by ``platforms``.
HERMES_PATH_RUNGS: tuple[MergePathRung, ...] = (
    MergePathRung(env_var=HERMES_HOME_ENV, parts=("config.yaml",)),
    MergePathRung(
        env_var="LOCALAPPDATA", parts=("hermes", "config.yaml"), platforms=("win32",)
    ),
    MergePathRung(
        parts=("AppData", "Local", "hermes", "config.yaml"), platforms=("win32",)
    ),
    MergePathRung(parts=(".hermes", "config.yaml")),
)


@dataclass(frozen=True, slots=True)
class HarnessConfigMerge:
    """How MCC edits a CLI that reads exactly one file it owns.

    Declared only for a harness that offers no alternative. Command Code is
    the first: its bundled ``dist/cli.mjs`` resolves ``providers.json`` from
    ``$HOME/.commandcode`` (``USERPROFILE`` as fallback) and reads no other
    document, takes no ``--config`` path and honours no config environment
    variable. Where that is true, MCC writes one key and leaves every other
    byte of the user's file alone; the mechanics and their guarantees live in
    ``config/harness_config_merge.py``.
    """

    #: Path under the user's home, as the CLI itself spells it.
    relative_parts: tuple[str, ...]
    #: The one key MCC owns inside the document, outermost first.
    owned_key_path: tuple[str, ...]
    #: How the file is named to a human, on the card and in the docs.
    display_path: str
    #: Suffix of the one-time copy taken before MCC's first edit.
    backup_suffix: str = ".mcc-backup"
    #: The environment variables the CLI reads to find home, in *its* order.
    #: Python's ``Path.home()`` prefers ``USERPROFILE`` on Windows while
    #: Command Code prefers ``HOME``; following the CLI is the only way the
    #: file MCC writes is the file the CLI reads.
    home_env_vars: tuple[str, ...] = ("HOME", "USERPROFILE")
    #: A variable that names *this file* outright, when the CLI publishes one.
    #: OpenClaw's ``OPENCLAW_CONFIG_PATH`` is the first: it wins over
    #: everything below, exactly as it does inside the CLI.
    path_env_var: str | None = None
    #: A variable that names the *directory* the document sits in, when the
    #: CLI publishes one. OpenClaw's ``OPENCLAW_STATE_DIR``. Consulted after
    #: ``path_env_var`` and before the home-relative default.
    state_env_var: str | None = None
    #: A variable that outranks the OS home for this CLI's own resolution.
    #: OpenClaw's ``OPENCLAW_HOME``. Empty by default, because a CLI that
    #: invents a home variable MCC ignores is a CLI MCC configures invisibly.
    home_override_env_var: str | None = None
    #: The key inside MCC's block that carries the proxy base URL. Command
    #: Code spells it ``baseURL``; OpenClaw spells it ``baseUrl``.
    base_url_key: str = "baseURL"
    #: The placeholder the serialiser writes there, replaced with this
    #: install's own proxy root on the way to disk.
    base_url_sentinel: str = COMMANDCODE_BASE_URL_SENTINEL
    #: Whether the *document* is edited as text through
    #: ``config/document_codecs`` -- so every byte MCC does not own survives,
    #: comments and layout included -- rather than rebuilt canonically from
    #: its parsed form the way Command Code's always has been. OpenClaw is the
    #: first harness whose file is a JSON5 document users actually hand-edit.
    preserve_bytes: bool = False
    #: Keys MCC *replaces* rather than creates inside the same document, with
    #: the path the serialiser states each new value at. Recorded for Undo
    #: exactly as the desktop Configure engine records ``overwritten_keys``.
    overwritten_key_paths: tuple[tuple[str, ...], ...] = ()
    #: How the document itself is encoded: ``json`` -- JSON5, and the default
    #: because every merge target before Hermes is one -- or ``yaml``. It is a
    #: property of the *file*, not of the serialiser, exactly as
    #: ``HarnessCatalogue.document_format`` is for a document MCC owns, and
    #: ``config/harness_config_merge.py`` parses and edits through it. YAML
    #: support arrived with the nested-table engine in 7.117.0; this field is
    #: the declaration that lets a spec use it.
    document_format: str = "json"
    #: The CLI's own search order, for a document whose location the four rungs
    #: above cannot state (Hermes'). Empty means those four rungs are the whole
    #: story, which is every merge target before Hermes.
    path_rungs: tuple[MergePathRung, ...] = ()
    #: Whether Configure records the document's hash even when it replaced no
    #: value, so ``RESTORE`` has something to compare against. False for every
    #: merge target whose block also replaces a value -- Command Code and
    #: OpenClaw both write one, and their record is written for that reason.
    #: Hermes' ``providers.mcc`` only ever *creates* keys, so without this its
    #: ``--restore`` would always answer "no record of what MCC replaced" --
    #: and the hash guard is exactly what a CLI that rewrites its own YAML
    #: (``save_config`` -> ``utils.atomic_yaml_write``) needs.
    records_document_hash: bool = False

    @property
    def owned_key_label(self) -> str:
        """Return the owned key as a reader would write it, e.g. ``provider.mcc``."""

        return ".".join(self.owned_key_path)

    def resolve(self, env: Mapping[str, str], platform: str) -> Path:
        """Return the document this CLI reads, resolved the way the CLI does.

        The rung list when the spec declares one, else the four home/state
        fields, else the OS home. Kept here rather than in
        ``config/harness_config_merge.py`` because the dashboard card's
        detection needs the same answer and ``config`` may not import the
        engine's callers back; the engine's own :func:`merge_config_path` is a
        one-line delegation to this.

        ``platform`` is passed rather than read from ``sys.platform`` so the
        Windows and POSIX answers can both be asserted from one machine -- the
        ``%LOCALAPPDATA%`` rung is otherwise unreachable in a test on Linux and
        untested on the machine that most needs it.
        """

        if self.path_rungs:
            for rung in self.path_rungs:
                if not rung.applies_to(platform):
                    continue
                if rung.env_var is None:
                    return Path.home().joinpath(*rung.parts)
                value = env.get(rung.env_var, "").strip()
                if not value:
                    continue
                return Path(value).expanduser().joinpath(*rung.parts)
            # A rung list that resolves nothing still has to answer with the
            # path the CLI would try last, and the last rung is the only
            # candidate that can be that.
            return Path.home().joinpath(*self.path_rungs[-1].parts)

        if self.path_env_var is not None:
            value = env.get(self.path_env_var, "").strip()
            if value:
                return Path(value).expanduser()
        if self.state_env_var is not None:
            value = env.get(self.state_env_var, "").strip()
            if value:
                return Path(value).expanduser().joinpath(self.relative_parts[-1])
        if self.home_override_env_var is not None:
            value = env.get(self.home_override_env_var, "").strip()
            if value:
                return Path(value).expanduser().joinpath(*self.relative_parts)
        for name in self.home_env_vars:
            value = env.get(name, "").strip()
            if value:
                return Path(value).joinpath(*self.relative_parts)
        return Path.home().joinpath(*self.relative_parts)


@dataclass(frozen=True, slots=True)
class HarnessCatalogue:
    """How a harness learns which models MCC can route for it.

    ``filename`` is ``None`` for a harness that receives its model list in
    process and never has a file on disk -- Pi's bundled extension registers
    the provider from memory on every launch, so nothing can go stale and
    nothing is left behind for a user who stops using it.
    """

    #: Serialiser key under ``application/catalogues``.
    format_id: str
    #: File written under ``~/.mcc``, or ``None`` for a process-local delivery.
    filename: str | None = None
    #: The CLI's own documented environment variable naming a config file it
    #: should read *in addition to* the user's own. Set for a harness that
    #: takes its provider block from a file rather than from argv or env: MCC
    #: then owns a file of its own under ``~/.mcc`` and never edits, merges
    #: into or backs up the document the user wrote.
    config_env_var: str | None = None
    #: The CLI's own command-line option naming a config file, for a harness
    #: that publishes a flag where the OpenCode family publishes a variable.
    #: Kimi Code is the first: it reads ``KIMI_SHARE_DIR`` for its *share*
    #: directory -- sessions, credentials, plugins, background state -- and
    #: redirecting that to serve one config file would hide every session the
    #: user has. ``--config-file`` moves the config alone, which is the only
    #: thing MCC has any business moving. Mutually exclusive with ``merge``
    #: for the same reason ``config_env_var`` is.
    config_flag: str | None = None
    #: Whether the generated document carries MCC's ``_mcc_defaulted`` record
    #: at its root. True everywhere but Kilo CLI, whose config validator
    #: rejects unknown root keys outright -- so for that one harness the record
    #: has to reach the launcher's summary and the dashboard card from
    #: ``GET /admin/api/catalogue-models`` instead of from the file. Stated
    #: here so the card can say the file carries no record rather than
    #: reporting a count of zero it did not measure.
    carries_defaulted_record: bool = True
    #: What the number in the generated document actually counts, for the one
    #: format where it is not models. Cline validates ``providers.json`` as a
    #: whole and discards it on a single unrecognised *root* key -- measured on
    #: 3.0.61, losing the base URL and the key with it -- so MCC's per-model
    #: block is stripped by ``config/harness_cline.strip_mcc_keys`` before the
    #: file reaches disk, and what is left is one provider entry. The card used
    #: to report that as "Models: 1" beside twelve harnesses reporting 140,
    #: which reads as a defect rather than as Cline's own schema. The full set
    #: is still published at ``GET /admin/api/catalogue-models`` and is what
    #: ``mcc-cline -m <id>`` selects from.
    model_count_label: str = "Models"
    #: A sentence the card shows under that number where it needs one.
    model_count_note: str = ""
    #: How the generated document is encoded. ``json`` for every harness that
    #: reads JSON; ``toml`` for Kimi Code, whose ``config.toml`` is parsed with
    #: ``tomlkit``. It is a property of the *file format*, not of the
    #: serialiser, which emits the same neutral mapping either way.
    document_format: str = "json"
    #: Set only where a CLI publishes neither an extra-config variable nor a
    #: command-line provider form, so the sole way to declare a provider is to
    #: edit the document the user wrote. Mutually exclusive with
    #: ``config_env_var``: a CLI that offers a variable never needs a merge.
    merge: HarnessConfigMerge | None = None
    #: The placeholder the serialiser writes wherever the CLI wants MCC's base
    #: URL, for a harness whose config format substitutes nothing of its own.
    #: ``config/harness_base_url.with_root_base_url`` replaces it with the
    #: proxy root -- no ``/v1`` -- on the way to disk. ``None`` for a harness
    #: that names an environment variable instead, as the OpenCode family
    #: does, or that has no base URL in its document at all.
    base_url_sentinel: str | None = None
    #: Which form the sentinel resolves to. ``root`` is the proxy root with no
    #: ``/v1``, which is what an Anthropic SDK wants because it appends
    #: ``/v1/messages`` itself. ``v1`` is ``<root>/v1``, which is what every
    #: OpenAI SDK wants because it appends ``chat/completions`` and nothing
    #: else. Getting this wrong produces ``/v1/v1/...`` or ``/messages``, both
    #: of which fail loudly rather than quietly -- but only after a launch.
    base_url_shape: str = "root"
    #: A second document the same harness reads, for a CLI that splits its
    #: model facts across two files. Aider is the only one: limits and prices
    #: go in the LiteLLM-shaped ``--model-metadata-file`` JSON, while what the
    #: model *accepts* -- reasoning effort, thinking tokens, whether
    #: ``temperature`` may be sent -- goes in the ``--model-settings-file``
    #: YAML. Two flags, two schemas, one set of records.
    sidecar_filename: str | None = None
    #: The CLI's own option naming that second document.
    sidecar_config_flag: str | None = None

    @property
    def writes_file(self) -> bool:
        """Whether MCC materialises a catalogue file for this harness."""

        return self.filename is not None

    @property
    def delivery(self) -> str:
        """Return how this harness receives its model list.

        ``file`` -- MCC owns a document under ``~/.mcc`` and the CLI is told
        its path. ``process_local`` -- registered in memory at launch, nothing
        on disk. ``merge`` -- the CLI reads only its own document, so MCC owns
        one key inside it.
        """

        if self.merge is not None:
            return "merge"
        return "file" if self.filename is not None else "process_local"


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """Everything MCC states about one coding-agent CLI it can launch."""

    id: str
    display_name: str
    #: Executable looked up with ``shutil.which``. The installer installs MCC
    #: and its prerequisites, nothing else; an agent is installed only by the
    #: Coding agents page's Install button you press (``install_methods``).
    binary: str
    protocol: HarnessProtocol
    #: Printed verbatim when the binary is missing, right before exit 127.
    install_hint: str
    #: Platform override for the hint; ``None`` when one line serves both.
    install_hint_windows: str | None = None
    #: Further names the same executable is published under, tried in order
    #: when ``binary`` is not on PATH. Command Code installs four shims for
    #: one entry point and a Windows user may have only ``cmdc`` on PATH;
    #: printing "install Command Code" for an install that is already there
    #: is worse than one extra ``shutil.which``.
    binary_aliases: tuple[str, ...] = ()
    commands: tuple[HarnessCommand, ...] = ()
    catalogue: HarnessCatalogue | None = None
    #: Subcommands and flags that must reach the CLI unchanged, without MCC
    #: injecting any provider configuration.
    passthrough_commands: frozenset[str] = frozenset()
    passthrough_flags: frozenset[str] = frozenset()
    #: Strings that must appear in ``<binary> --help`` for the executable to be
    #: the CLI MCC means. Empty when the binary name is unambiguous enough.
    identity_help_markers: tuple[str, ...] = ()
    #: Whether RTK's shell-tool wrapper is known to apply. ``False`` is the
    #: default on purpose: RTK wraps an agent's own shell tool, and claiming
    #: support MCC has not verified would install hooks into a config file for
    #: an agent that ignores them.
    rtk_agent: bool = False
    rtk_enable_args: tuple[str, ...] = ()
    rtk_uninstall_args: tuple[str, ...] = ()
    #: One line for the dashboard card and the generated docs.
    summary: str = ""
    #: Short line for the Get Started card.
    tagline: str = ""
    #: Whether MCC can actually serve this CLI. ``False`` is for a client MCC
    #: has *tried* and found unservable, and it is a deliberate registry entry
    #: rather than an omission: the question "can I use Antigravity through
    #: this?" gets asked, and an answer with a date and a version on it is
    #: worth more than silence. An unavailable harness publishes no console
    #: script, no launcher and no catalogue.
    available: bool = True
    #: Why not, stated so a reader can check it. Rendered verbatim on the
    #: Coding agents card. Required when ``available`` is False.
    unavailable_reason: str = ""
    #: How the Coding agents page's Install button installs this CLI, per OS
    #: (7.102.0; ``config/install_methods.py``). The ``install_hint`` lines
    #: above stay what the launchers print; these are what the button runs,
    #: after a plan the user reads and confirms. Empty means no button, and
    #: :attr:`install_unavailable_reason` says why.
    install_methods: tuple[InstallMethod, ...] = ()
    install_unavailable_reason: str = ""
    #: Where an install's own configuration lives, home-relative, tried in
    #: order. Consulted by the Coding agents card **only when the binary is not
    #: on PATH**, and reported as "config found, CLI not on PATH" -- a
    #: different fact from "not installed", and one the user can act on.
    #:
    #: OpenClaw is the first: its document survives an uninstall of the CLI, a
    #: PATH that does not carry npm's global folder, and a shell started before
    #: the install. dsh and Hermes declare their own between them.
    detection_paths: tuple[tuple[str, ...], ...] = ()

    @property
    def command(self) -> str:
        """Return the harness's headline ``mcc-`` command, or "" if it has none."""

        for entry in self.commands:
            if entry.primary:
                return entry.command
        return "" if not self.commands else f"mcc-{self.id}"

    def install_hint_for(self, platform: str) -> str:
        """Return the install hint appropriate to one ``sys.platform`` value."""

        if platform == "win32" and self.install_hint_windows is not None:
            return self.install_hint_windows
        return self.install_hint

    def config_probe_path(self, env: Mapping[str, str]) -> str | None:
        """Return the first declared configuration path that exists, or None.

        The card's second rung, asked only when the binary is not on PATH: an
        install whose npm global folder is not in *this* shell, or whose CLI
        was removed while its file stayed, is a different fact from "not
        installed" and the one a user can act on. Resolved through the
        harness's own home rules -- its merge spec's, when it has one -- so the
        path reported is the path the CLI would read.
        """

        if not self.detection_paths:
            return None
        base = self._detection_home(env)
        for parts in self.detection_paths:
            try:
                if base.joinpath(*parts).exists():
                    return str(base.joinpath(*parts))
            except OSError:
                continue
        return None

    def _detection_home(self, env: Mapping[str, str]) -> Path:
        """Return the home this CLI resolves, in the CLI's own order.

        A spec that declares its own ``path_rungs`` has told us exactly where
        its document lives, so the base the declared paths hang off is the
        directory that document is in -- Hermes' home *is*
        ``%LOCALAPPDATA%\\hermes``, and its ``config.yaml`` sits at the root of
        it. Every other merge target is home-relative the way the four fields
        say, and keeps the answer it always had.
        """

        merge = self.catalogue.merge if self.catalogue is not None else None
        if merge is not None and merge.path_rungs:
            return merge.resolve(env, sys.platform).parent
        override = merge.home_override_env_var if merge is not None else None
        if override is not None:
            value = env.get(override, "").strip()
            if value:
                return Path(value).expanduser()
        names = merge.home_env_vars if merge is not None else ("HOME", "USERPROFILE")
        for name in names:
            value = env.get(name, "").strip()
            if value:
                return Path(value)
        return Path.home()


HARNESS_SPECS: tuple[HarnessSpec, ...] = (
    HarnessSpec(
        id="claude",
        display_name="Claude Code",
        binary="claude",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint=(
            "Install Claude Code with: npm install -g @anthropic-ai/claude-code"
        ),
        install_methods=CLAUDE_METHODS,
        commands=(
            HarnessCommand(
                suffix="claude",
                target="my_claude_code.cli.launchers.claude:launch",
                help_text="Launch Claude Code through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments="--discover-models",
                        help_text=(
                            "Also enable the model picker from the catalog "
                            "(MCC's own flag; stripped before Claude Code sees it)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='-p "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively; every other "
                            "argument is passed to Claude Code unchanged"
                        ),
                    ),
                ),
            ),
            HarnessCommand(
                suffix="claude-old",
                target="my_claude_code.cli.launchers.claude:launch_legacy",
                primary=False,
                help_text="Legacy launcher: full proxy environment, auto-compact",
            ),
        ),
        # Claude Code discovers models itself from GET /v1/models behind
        # CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY; MCC writes it nothing.
        catalogue=None,
        rtk_agent=True,
        rtk_enable_args=("init", "-g", "--auto-patch"),
        rtk_uninstall_args=("init", "-g", "--uninstall"),
        summary="Anthropic's Claude Code, pointed here with two environment variables.",
        tagline="Anthropic's Claude Code, served through this proxy.",
    ),
    HarnessSpec(
        id="codex",
        display_name="Codex CLI",
        binary="codex",
        protocol=HarnessProtocol.OPENAI_RESPONSES,
        install_hint="Install Codex with: npm install -g @openai/codex",
        install_methods=CODEX_METHODS,
        commands=(
            HarnessCommand(
                suffix="codex",
                target="my_claude_code.cli.launchers.codex:launch",
                help_text="Launch Codex through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='exec "<prompt>"',
                        help_text="Run Codex non-interactively on one prompt",
                    ),
                    HarnessInvocation(
                        arguments="resume --last",
                        help_text="Continue the most recent Codex session",
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="codex",
            filename="codex-model-catalog.json",
        ),
        rtk_agent=True,
        rtk_enable_args=("init", "-g", "--codex"),
        rtk_uninstall_args=("init", "--uninstall", "-g", "--codex"),
        summary=(
            "OpenAI's Codex CLI, configured with ephemeral -c assignments so "
            "your own config.toml is never rewritten."
        ),
        tagline="OpenAI's Codex CLI, served through this proxy.",
    ),
    HarnessSpec(
        id="pi",
        display_name="Pi",
        binary="pi",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint="Install Pi with: curl -fsSL https://pi.dev/install.sh | sh",
        install_hint_windows=(
            'Install Pi with: powershell -c "irm https://pi.dev/install.ps1 | iex"'
        ),
        install_methods=PI_METHODS,
        commands=(
            HarnessCommand(
                suffix="pi",
                target="my_claude_code.cli.launchers.pi:launch",
                help_text="Launch Pi through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments="--version",
                        help_text=(
                            "Passed straight to Pi: MCC injects no provider "
                            "for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(format_id="pi"),
        passthrough_commands=frozenset(
            {"config", "install", "list", "remove", "uninstall", "update"}
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        identity_help_markers=("--extension", "--models"),
        rtk_agent=True,
        rtk_enable_args=("init", "-g", "--agent", "pi"),
        rtk_uninstall_args=("init", "--uninstall", "-g", "--agent", "pi"),
        summary=(
            "The Pi coding agent, registered process-locally by a bundled "
            "extension so nothing under ~/.pi is rewritten."
        ),
        tagline="The Pi coding agent, served through this proxy.",
    ),
    HarnessSpec(
        id="opencode",
        display_name="OpenCode",
        binary="opencode",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint="Install OpenCode with: npm install -g opencode-ai",
        install_methods=OPENCODE_METHODS,
        commands=(
            HarnessCommand(
                suffix="opencode",
                target="my_claude_code.cli.launchers.opencode:launch",
                legacy_alias=False,
                help_text="Launch OpenCode through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='run "<prompt>"',
                        help_text="Run OpenCode non-interactively on one prompt",
                    ),
                    HarnessInvocation(
                        arguments="models mcc",
                        help_text=(
                            "List the models MCC published, with the limits "
                            "and prices the ladder resolved"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="-m mcc/<provider>/<model>",
                        help_text="Start on one specific MCC-routed model",
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="opencode",
            filename="opencode-config.json",
            config_env_var="OPENCODE_CONFIG",
        ),
        passthrough_commands=frozenset({"upgrade", "uninstall", "completion"}),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        summary=(
            "OpenCode, pointed at an MCC-owned config file through its own "
            "OPENCODE_CONFIG variable so your opencode.json is never edited."
        ),
        tagline="The OpenCode agent, served through this proxy.",
    ),
    HarnessSpec(
        id="opencode2",
        display_name="OpenCode 2",
        binary="opencode2",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint=("Install OpenCode 2 with: npm install -g @opencode-ai/cli@beta"),
        install_methods=OPENCODE2_METHODS,
        commands=(
            HarnessCommand(
                suffix="opencode2",
                target="my_claude_code.cli.launchers.opencode:launch_v2",
                legacy_alias=False,
                help_text="Launch the OpenCode 2 preview through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='run --standalone "<prompt>"',
                        help_text=(
                            "Run one prompt in a private server. Prefer "
                            "--standalone: v2's background service keeps the "
                            "config it started with, so a config MCC has since "
                            "refreshed does not reach an already-running one"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="models",
                        help_text="List every model OpenCode 2 can see, MCC's included",
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="opencode",
            filename="opencode2-config.json",
            config_env_var="OPENCODE_CONFIG",
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        summary=(
            "The OpenCode 2 preview, which installs beside v1 under its own "
            "opencode2 binary and reads the same config schema."
        ),
        tagline="The OpenCode 2 preview, served through this proxy.",
    ),
    HarnessSpec(
        id="kilo",
        display_name="Kilo CLI",
        binary="kilo",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint="Install Kilo CLI with: npm install -g @kilocode/cli",
        install_methods=KILO_METHODS,
        commands=(
            HarnessCommand(
                suffix="kilo",
                target="my_claude_code.cli.launchers.opencode:launch_kilo",
                legacy_alias=False,
                help_text="Launch Kilo CLI through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='run "<prompt>"',
                        help_text="Run Kilo non-interactively on one prompt",
                    ),
                    HarnessInvocation(
                        arguments="models mcc",
                        help_text="List the models MCC published to Kilo",
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="kilo",
            filename="kilo-config.json",
            config_env_var="KILO_CONFIG",
            carries_defaulted_record=False,
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        summary=(
            "Kilo CLI, a fork of OpenCode that reads the same config schema "
            "through its own KILO_CONFIG variable."
        ),
        tagline="Kilo CLI, served through this proxy.",
    ),
    HarnessSpec(
        # ``commandcode_cli``, not ``commandcode``: the latter is the upstream
        # gateway in ``config/provider_catalog.py``. See the module docstring.
        id="commandcode_cli",
        display_name="Command Code",
        binary="command-code",
        # npm installs four names for the same entry point -- ``cmd``, ``cmdc``,
        # ``command-code`` and ``commandcode``. ``cmd`` is unusable as a probe
        # on Windows, where it resolves to the system shell; the other two are
        # unambiguous, and either satisfies the launcher.
        binary_aliases=("cmdc",),
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint="Install Command Code with: npm install -g command-code",
        install_methods=COMMANDCODE_METHODS,
        commands=(
            HarnessCommand(
                suffix="commandcode",
                target="my_claude_code.cli.launchers.commandcode:launch",
                legacy_alias=False,
                help_text="Launch Command Code through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='-p "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively and print the "
                            "answer (add --output-format json for the event "
                            "stream)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--list-models",
                        help_text=(
                            "List every model Command Code can see, MCC's "
                            "included, with the limits the ladder resolved"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="-m mcc/<provider>/<model>",
                        help_text="Start on one specific MCC-routed model",
                    ),
                    HarnessInvocation(
                        arguments="--local-only",
                        help_text=(
                            "Command Code's own flag: use BYOK providers only, "
                            "with no Command Code traffic. Note that its "
                            "headless -p mode still checks you are signed in "
                            "to Command Code before it runs, whatever model "
                            "you asked for"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--disconnect",
                        help_text=(
                            "MCC's own flag: remove MCC's provider.mcc key "
                            "from ~/.commandcode/providers.json and exit, "
                            "leaving every other key untouched"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="commandcode",
            # No file of MCC's own: Command Code reads one document and MCC
            # merges one key into it. The path below is the CLI's, not MCC's.
            filename=None,
            merge=HarnessConfigMerge(
                relative_parts=(".commandcode", "providers.json"),
                owned_key_path=("provider", "mcc"),
                display_path="~/.commandcode/providers.json",
            ),
        ),
        passthrough_commands=frozenset(
            {
                "info",
                "status",
                "help",
                "whoami",
                "update",
                "feedback",
                "issue",
                "login",
                "logout",
                "mcp",
                "skills",
                "mods",
                "taste",
                "learn-taste",
            }
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        summary=(
            "Command Code, which reads one providers.json and no override "
            "file, so MCC merges a single provider.mcc key into it and backs "
            "the document up first."
        ),
        tagline="The Command Code agent, served through this proxy.",
    ),
    HarnessSpec(
        id="openclaw",
        display_name="OpenClaw",
        binary="openclaw",
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        install_hint=(
            "Install OpenClaw with: npm install -g openclaw "
            "(it needs Node.js 24.22.1 or newer)"
        ),
        install_methods=OPENCLAW_METHODS,
        commands=(
            HarnessCommand(
                suffix="openclaw",
                target="my_claude_code.cli.launchers.openclaw:launch",
                legacy_alias=False,
                help_text="Launch OpenClaw through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments="--models",
                        help_text=(
                            "MCC's own flag: list the models MCC published to "
                            "~/.openclaw/openclaw.json and exit"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--disconnect",
                        help_text=(
                            "MCC's own flag: remove MCC's models.providers.mcc "
                            "block from ~/.openclaw/openclaw.json, put back any "
                            "default model it replaced, and exit"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--restore",
                        help_text=(
                            "MCC's own flag: the same removal, but refusing if "
                            "the file changed since MCC configured it, and "
                            "consuming the record of what MCC replaced"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="openclaw",
            # No file of MCC's own: OpenClaw reads one document and MCC merges
            # one key into it. Every other lever was measured and rejected --
            # ``OPENCLAW_CONFIG_PATH`` would *replace* the user's channels and
            # gateway settings for that process, and ``$include`` resolves only
            # inside ``~/.openclaw``, which is the tool's own directory.
            filename=None,
            carries_defaulted_record=False,
            merge=HarnessConfigMerge(
                relative_parts=(".openclaw", "openclaw.json"),
                owned_key_path=("models", "providers", OPENCLAW_PROVIDER_ID),
                display_path="~/.openclaw/openclaw.json",
                path_env_var=OPENCLAW_CONFIG_PATH_ENV,
                state_env_var=OPENCLAW_STATE_DIR_ENV,
                home_override_env_var=OPENCLAW_HOME_ENV,
                # OpenClaw spells the field ``baseUrl`` and its OpenAI chat
                # client appends ``chat/completions`` to ``<baseUrl>/v1``.
                base_url_key="baseUrl",
                base_url_sentinel=OPENCLAW_BASE_URL_SENTINEL,
                # The user's own JSON5 document: comments, indentation and key
                # order survive because the edit is made to the text.
                preserve_bytes=True,
                overwritten_key_paths=(OPENCLAW_PRIMARY_MODEL_PATH,),
            ),
        ),
        passthrough_commands=frozenset(
            {
                "help",
                "status",
                "doctor",
                "config",
                "models",
                "gateway",
                "logs",
                "update",
                "onboard",
                "plugins",
                "agents",
                "channels",
                "cron",
                "devices",
                "node",
                "security",
                "sessions",
            }
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        # Binary-first, then the document: an install whose npm global folder
        # is not on this shell's PATH has a config file and no binary, and the
        # card says so instead of "not installed".
        detection_paths=(
            (".openclaw", "openclaw.json"),
            (".openclaw",),
        ),
        summary=(
            "OpenClaw, which reads one JSON5 document and no extra-config "
            "variable, so MCC merges a single models.providers.mcc block into "
            "~/.openclaw/openclaw.json and backs the document up first. Every "
            "other byte, comment and key is left exactly as it was."
        ),
        tagline="The OpenClaw gateway, served through this proxy.",
    ),
    HarnessSpec(
        id="hermes",
        display_name="Hermes Agent",
        # ``hermes`` is the session CLI. The wheel also installs
        # ``hermes-agent`` (one-shot runner) and ``hermes-acp`` (ACP adapter);
        # neither is what a user means by "start Hermes".
        binary="hermes",
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        # A Python tool on PyPI (``hermes-agent``), and its own config code
        # lists ``pip`` and ``homebrew`` as *unsupported* install methods
        # (``hermes_cli/config.py:578`` of 0.19.0), so neither is the line to
        # print. Nous Research's installer is.
        install_hint=(
            "Install Hermes Agent with Nous Research's installer: "
            "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        ),
        install_hint_windows=(
            "Install Hermes Agent with Nous Research's installer: "
            'powershell -c "irm https://hermes-agent.nousresearch.com/install.ps1 | iex"'
        ),
        install_methods=HERMES_METHODS,
        commands=(
            HarnessCommand(
                suffix="hermes",
                target="my_claude_code.cli.launchers.hermes:launch",
                legacy_alias=False,
                help_text="Launch Hermes Agent through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments="--models",
                        help_text=(
                            "MCC's own flag: list the models MCC published to "
                            "the providers.mcc block in your config.yaml and exit"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--disconnect",
                        help_text=(
                            "MCC's own flag: remove MCC's providers.mcc block "
                            "from your config.yaml and exit, leaving every other "
                            "section untouched"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--restore",
                        help_text=(
                            "MCC's own flag: the same removal, but refusing if "
                            "the file changed since MCC configured it, and "
                            "consuming the record"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='-z "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively and print only the "
                            "answer (Hermes' own --oneshot)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="-m mcc/<provider>/<model> --provider mcc",
                        help_text=(
                            "Start on one specific MCC-routed model. Hermes' own "
                            "-m/--model applies to -z and --tui"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="model",
                        help_text=(
                            "Hermes' own model picker, which lists the provider "
                            "mcc and every model MCC published"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="hermes",
            # No file of MCC's own: Hermes reads exactly one config.yaml and
            # MCC merges one key into it. ``HERMES_HOME`` was measured and
            # rejected as an override -- it moves sessions, skills, plugins and
            # credentials too, which is the argument that rejected Kimi's
            # ``KIMI_SHARE_DIR``.
            filename=None,
            # Hermes warns about any key inside a provider entry that its own
            # normaliser does not know (``hermes_cli/config.py:5037-5041``), so
            # MCC's bookkeeping never goes into the block: the card and the
            # launcher read the count from GET /admin/api/catalogue-models.
            carries_defaulted_record=False,
            merge=HarnessConfigMerge(
                # ``HERMES_HOME`` is the base, so the document is its direct
                # child; the remaining rungs are in ``path_rungs``.
                relative_parts=("config.yaml",),
                owned_key_path=("providers", HERMES_PROVIDER_ID),
                display_path="~/.hermes/config.yaml",
                # Hermes' document is YAML, and its provider block is a nested
                # mapping: the case ``document_codecs`` gained in 7.117.0.
                document_format="yaml",
                path_rungs=HERMES_PATH_RUNGS,
                # Hermes spells the field ``api`` (``base_url`` and ``url`` are
                # accepted aliases) and its OpenAI client appends
                # ``/chat/completions`` to it, so the value carries ``/v1``.
                base_url_key="api",
                base_url_sentinel=HERMES_BASE_URL_SENTINEL,
                # The user's own config.yaml: comments, quoting and key order
                # survive because the edit is made to the text.
                preserve_bytes=True,
                # Hermes re-dumps config.yaml itself, so the only honest Undo
                # for the values question is the hash-guarded one -- and the
                # block replaces nothing, so the record exists for its hash.
                records_document_hash=True,
            ),
        ),
        # ``chat`` is deliberately absent: it runs an agent and needs the
        # provider. ``model`` and ``fallback`` are absent for a different
        # reason -- they list the providers and models MCC has published, so
        # passing them through would show a picker with no ``mcc`` in it.
        passthrough_commands=frozenset(
            {
                "help",
                "setup",
                "logout",
                "auth",
                "config",
                "sessions",
                "logs",
                "console",
                "update",
                "dashboard",
                "debug",
            }
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-V"}),
        # Binary-first, then the document MCC would write: an install whose
        # script folder is not on this shell's PATH has a config.yaml and no
        # binary, and the card says "Config found, CLI not on PATH".
        detection_paths=(("config.yaml",),),
        summary=(
            "Nous Research's Hermes Agent, which reads one config.yaml and no "
            "extra-config variable, so MCC merges a single providers.mcc block "
            "into it -- comments, layout and every other section left byte for "
            "byte, and your proxy token never written at all: the block names "
            "an environment variable the launcher sets in the child process."
        ),
        tagline="Nous Research's Hermes Agent, served through this proxy.",
    ),
    HarnessSpec(
        # ``kimi_code``, not ``kimi``: the latter is an upstream Moonshot
        # gateway in ``config/provider_catalog.py``. See the module docstring.
        id="kimi_code",
        display_name="Kimi Code",
        binary="kimi",
        # ``uv tool install kimi-cli`` installs two executables for one entry
        # point, ``kimi`` and ``kimi-cli``. Either satisfies the launcher.
        binary_aliases=("kimi-cli",),
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        # A Python tool on PyPI, not an npm package: ``uv tool install`` is
        # Moonshot's own first line and ``pipx install kimi-cli`` is the
        # documented alternative. MCC installs neither -- it prints this and
        # exits 127.
        install_hint=(
            "Install Kimi Code with: uv tool install kimi-cli "
            "(or: pipx install kimi-cli)"
        ),
        # User decision 6 (2026-10-18): no Install button until a check that
        # mcc-kimi works with Moonshot's new Kimi Code has passed.
        install_unavailable_reason=KIMI_NO_BUTTON_REASON,
        commands=(
            HarnessCommand(
                suffix="kimi",
                target="my_claude_code.cli.launchers.kimi:launch",
                legacy_alias=False,
                help_text="Launch Kimi Code through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments="-m mcc/<provider>/<model>",
                        help_text=(
                            "Start on one specific MCC-routed model. Kimi Code "
                            "has no model-list subcommand and MCC states no "
                            "default model, so either pass this or pick one "
                            "with /model once the session is up"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='--print -p "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively (add "
                            "--output-format stream-json for the event stream)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='--quiet -p "<prompt>"',
                        help_text=(
                            "Kimi Code's own alias for --print "
                            "--output-format text --final-message-only"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="info",
                        help_text=(
                            "Passed straight to Kimi Code: MCC injects no "
                            "provider for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="kimi",
            filename="kimi-code-config.toml",
            document_format="toml",
            config_flag="--config-file",
        ),
        # ``term``, ``acp``, ``web`` and ``vis`` are omitted deliberately:
        # the first three run an agent and do need MCC's provider, and ``vis``
        # is the tracing viewer, which does not.
        passthrough_commands=frozenset(
            {"login", "logout", "info", "export", "mcp", "plugin", "vis"}
        ),
        # ``-V``, not ``-v``: Kimi Code spells its version flag ``--version``
        # / ``-V`` and has no ``-v`` at all.
        passthrough_flags=frozenset({"--help", "-h", "--version", "-V"}),
        summary=(
            "Kimi Code, pointed at an MCC-owned config.toml through its own "
            "--config-file flag so ~/.kimi/config.toml is never edited."
        ),
        tagline="Moonshot's Kimi Code CLI, served through this proxy.",
    ),
    HarnessSpec(
        # ``qwen_code``, not ``qwen``: ``qwencloud`` and ``qwencloud_coding``
        # in ``config/provider_catalog.py`` are upstream Alibaba gateways MCC
        # sends requests **to**. This is the ``qwen`` CLI MCC serves requests
        # **for**. See the module docstring.
        id="qwen_code",
        display_name="Qwen Code",
        binary="qwen",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint=("Install Qwen Code with: npm install -g @qwen-code/qwen-code"),
        install_methods=QWEN_METHODS,
        commands=(
            HarnessCommand(
                suffix="qwen",
                target="my_claude_code.cli.launchers.qwen:launch",
                legacy_alias=False,
                help_text="Launch Qwen Code through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='"<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively. Qwen Code's "
                            "positional query is one-shot by default; add "
                            "-i to stay interactive afterwards"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="-m anthropic/<provider>/<model>",
                        help_text=(
                            "Start on one specific MCC-routed model. Qwen "
                            "Code has no model-list subcommand; /model lists "
                            "everything MCC wrote into its settings"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='-o json "<prompt>"',
                        help_text=(
                            "Same run, machine-readable (stream-json is also accepted)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="mcp",
                        help_text=(
                            "Passed straight to Qwen Code: MCC injects no "
                            "provider for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="qwen",
            filename="qwen-code-settings.json",
            config_env_var="QWEN_CODE_SYSTEM_SETTINGS_PATH",
            base_url_sentinel=QWEN_BASE_URL_SENTINEL,
        ),
        passthrough_commands=frozenset(
            {"mcp", "extensions", "auth", "hooks", "hook", "channel"}
        ),
        passthrough_flags=frozenset(
            {"--help", "-h", "--version", "-v", "--list-extensions", "-l"}
        ),
        summary=(
            "Qwen Code, pointed at an MCC-owned settings document through its "
            "own QWEN_CODE_SYSTEM_SETTINGS_PATH variable, with the auth type "
            "selected by --auth-type so ~/.qwen/settings.json is never read "
            "for it and never written."
        ),
        tagline="Alibaba's Qwen Code CLI, served through this proxy.",
    ),
    HarnessSpec(
        # ``crush`` collides with nothing: MCC has no upstream provider of
        # that name. The id is still spelled out here rather than derived, so
        # that a future ``crush`` gateway cannot silently take it over.
        id="crush",
        display_name="Crush",
        binary="crush",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint=(
            "Install Crush with: npm install -g @charmland/crush "
            "(or: brew install charmbracelet/tap/crush)"
        ),
        install_methods=CRUSH_METHODS,
        commands=(
            HarnessCommand(
                suffix="crush",
                target="my_claude_code.cli.launchers.crush:launch",
                legacy_alias=False,
                help_text="Launch Crush through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='run "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively and exit "
                            "(--quiet hides the spinner)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="models",
                        help_text=(
                            "List every model Crush can see; MCC's appear as "
                            "mcc/anthropic/<provider>/<model>"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="dirs",
                        help_text=(
                            "Show which config directory this launch uses -- "
                            "MCC's own, not ~/.config/crush"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="update-providers",
                        help_text=(
                            "Passed straight to Crush: MCC injects no "
                            "provider for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="crush",
            # A directory of MCC's own, because CRUSH_GLOBAL_CONFIG names a
            # *directory* and Crush looks for ``crush.json`` inside it.
            filename="crush/crush.json",
            config_env_var="CRUSH_GLOBAL_CONFIG",
            base_url_sentinel=CRUSH_BASE_URL_SENTINEL,
        ),
        passthrough_commands=frozenset(
            {
                "completion",
                "help",
                "login",
                "logout",
                "logs",
                "projects",
                "update-providers",
            }
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        summary=(
            "Crush, pointed at an MCC-owned crush.json through its own "
            "CRUSH_GLOBAL_CONFIG variable, so ~/.config/crush is never read "
            "for a provider and never written."
        ),
        tagline="Charm's Crush CLI, served through this proxy.",
    ),
    HarnessSpec(
        # ``cline_cli``, not ``cline``: the latter is an upstream gateway in
        # ``config/provider_catalog.py``. See the module docstring.
        id="cline_cli",
        display_name="Cline",
        binary="cline",
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        install_hint="Install Cline with: npm install -g cline",
        install_methods=CLINE_METHODS,
        commands=(
            HarnessCommand(
                suffix="cline",
                target="my_claude_code.cli.launchers.cline:launch",
                legacy_alias=False,
                help_text="Launch Cline through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='"<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively. Cline's bare "
                            "positional argument is its headless form -- note "
                            "that -p is --plan, not --print"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='--json "<prompt>"',
                        help_text="Same run, as a stream of JSON events",
                    ),
                    HarnessInvocation(
                        arguments="-m anthropic/<provider>/<model>",
                        help_text=(
                            "Start on one specific MCC-routed model, with the "
                            "context window and output ceiling the ladder "
                            "resolved for it"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="-i",
                        help_text="Open Cline's interactive terminal UI",
                    ),
                    HarnessInvocation(
                        arguments="config",
                        help_text=(
                            "Show the configuration this launch uses -- MCC's "
                            "own directory, not ~/.cline"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="doctor",
                        help_text=(
                            "Passed straight to Cline: MCC injects no provider "
                            "for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="cline",
            # ``cline --config`` names a *directory*, and Cline derives its
            # data directory from the settings file inside it, so the
            # filename spells the whole tree MCC owns.
            filename="cline/data/settings/providers.json",
            config_flag="--config",
            base_url_sentinel=CLINE_BASE_URL_SENTINEL,
            base_url_shape="v1",
            model_count_label="Provider blocks",
            model_count_note=(
                "Cline's providers.json has no per-model array: its schema "
                "carries the numbers for the one model the provider is "
                "configured with, and it discards the whole document on any "
                "unrecognised root key. So this counts provider blocks, not "
                "models. MCC still resolves the full catalogue -- see "
                "/admin/api/catalogue-models -- and mcc-cline -m <id> promotes "
                "whichever model you name into the block before Cline reads it."
            ),
        ),
        passthrough_commands=frozenset(
            {"doctor", "plugin", "skill", "mcp", "schedule", "hook", "connect"}
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-V", "--update"}),
        summary=(
            "Cline, pointed at a configuration directory this proxy owns "
            "through its own --config flag, so ~/.cline is never read for a "
            "provider and never written."
        ),
        tagline="The Cline agent, served through this proxy.",
    ),
    HarnessSpec(
        id="goose",
        display_name="Goose",
        binary="goose",
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        install_hint=(
            "Install Goose with: curl -fsSL "
            "https://github.com/block/goose/releases/download/stable/download_cli.sh | bash"
        ),
        install_hint_windows=(
            "Install Goose from https://github.com/block/goose/releases "
            "(goose-x86_64-pc-windows-msvc.zip)"
        ),
        install_methods=GOOSE_METHODS,
        commands=(
            HarnessCommand(
                suffix="goose",
                target="my_claude_code.cli.launchers.goose:launch",
                legacy_alias=False,
                help_text="Launch Goose through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='run -t "<prompt>" --no-session',
                        help_text=(
                            "Run one prompt non-interactively and leave no "
                            "session behind (-q hides the spinner)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="session",
                        help_text="Start an interactive Goose session",
                    ),
                    HarnessInvocation(
                        arguments='run --model anthropic/<provider>/<model> -t "<prompt>"',
                        help_text=(
                            "Run on one specific MCC-routed model. Goose's own "
                            "--model outranks the GOOSE_MODEL this launcher sets"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="info -v",
                        help_text=(
                            "Show the provider, model and context limit this "
                            "launch resolved, and the config file Goose would "
                            "read -- which MCC leaves alone"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="configure",
                        help_text=(
                            "Goose's own setup wizard. Its model picker lists "
                            "what GET /v1/models publishes, because Goose asks "
                            "the host it was pointed at"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="update",
                        help_text=(
                            "Passed straight to Goose: MCC injects no provider "
                            "for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        # Nothing on disk, and nothing in process either: Goose takes the
        # host, the path, the key, the provider, the model and the context
        # limit from environment variables this launcher sets, and discovers
        # the model list from GET /v1/models itself.
        catalogue=None,
        passthrough_commands=frozenset(
            {"update", "completion", "help", "recipe", "plugin", "local-models"}
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-V"}),
        summary=(
            "Goose, pointed here with five environment variables -- seven "
            "when it also pins a model and a context limit -- and no file "
            "at all, because its only file-shaped mechanism would put an "
            "MCC-owned document inside Goose's own config directory."
        ),
        tagline="Block's Goose agent, served through this proxy.",
    ),
    HarnessSpec(
        id="aider",
        display_name="Aider",
        binary="aider",
        protocol=HarnessProtocol.OPENAI_CHAT_COMPLETIONS,
        install_hint=(
            "Install Aider with: uv tool install aider-chat "
            "(or: python -m pip install aider-chat)"
        ),
        install_methods=AIDER_METHODS,
        commands=(
            HarnessCommand(
                suffix="aider",
                target="my_claude_code.cli.launchers.aider:launch",
                legacy_alias=False,
                help_text="Launch Aider through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='--message "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively and exit "
                            "(--yes-always accepts every confirmation)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--model openai/anthropic/<provider>/<model>",
                        help_text=(
                            "Start on one specific MCC-routed model. The "
                            "openai/ prefix is LiteLLM's provider selector, "
                            "not part of the model id"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--list-models openai/anthropic",
                        help_text=(
                            "List every MCC-routed model Aider can see, out of "
                            "the metadata file this proxy generates"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--exit --verbose",
                        help_text=(
                            "Start, print the resolved configuration and exit "
                            "without sending anything"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="--version",
                        help_text=(
                            "Passed straight to Aider: MCC injects no provider "
                            "for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="aider",
            filename="aider-model-metadata.json",
            config_flag="--model-metadata-file",
            sidecar_filename="aider-model-settings.yml",
            sidecar_config_flag="--model-settings-file",
        ),
        passthrough_flags=frozenset(
            {"--help", "-h", "--version", "--just-check-update", "--upgrade"}
        ),
        summary=(
            "Aider, pointed at a LiteLLM-shaped metadata file and a model "
            "settings file this proxy owns, through Aider's own two flags, so "
            "no .aider.model.* file of yours is read or written."
        ),
        tagline="Aider, served through this proxy.",
    ),
    HarnessSpec(
        id="droid",
        display_name="Droid",
        # Droid reaches MCC over Anthropic Messages, not Chat Completions.
        # The brief expected ``generic-chat-completion-api``; measured on
        # 0.210.0, ``provider: "anthropic"`` accepts an arbitrary ``baseUrl``
        # and instantiates the bundled ``@anthropic-ai/sdk`` against it, so
        # MCC's own native protocol is the one used. See the launcher.
        binary="droid",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint=("Install Droid with: curl -fsSL https://app.factory.ai/cli | sh"),
        install_hint_windows=(
            'Install Droid with: powershell -c "irm https://app.factory.ai/cli/windows | iex"'
        ),
        install_methods=DROID_METHODS,
        commands=(
            HarnessCommand(
                suffix="droid",
                target="my_claude_code.cli.launchers.droid:launch",
                legacy_alias=False,
                help_text="Launch Droid through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='exec "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively. No Factory "
                            "account is needed for an MCC-routed model: Droid "
                            "classifies it as BYOK and does not gate on login"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='exec --model custom:anthropic/<provider>/<model> "<prompt>"',
                        help_text=(
                            "Run on one specific MCC-routed model. Droid names "
                            "a custom model custom:<model>, so the prefix is "
                            "part of the id you type"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='exec --output-format json "<prompt>"',
                        help_text="Same run, machine-readable",
                    ),
                    HarnessInvocation(
                        arguments="doctor",
                        help_text=(
                            "Show the configuration, connectivity and auth "
                            "state of this launch, including which settings "
                            "file the overlay came from"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="update",
                        help_text=(
                            "Passed straight to Droid: MCC injects no provider "
                            "for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="droid",
            filename="droid-settings.json",
            config_flag="--settings",
            base_url_sentinel=DROID_BASE_URL_SENTINEL,
        ),
        passthrough_commands=frozenset(
            {"update", "mcp", "plugin", "computer", "doctor", "help", "search", "find"}
        ),
        passthrough_flags=frozenset({"--help", "-h", "--version", "-v"}),
        summary=(
            "Factory's Droid, pointed at a runtime settings overlay this proxy "
            "owns through Droid's own --settings flag, so ~/.factory is never "
            "edited. It speaks Anthropic Messages, not Chat Completions."
        ),
        tagline="Factory's Droid agent, served through this proxy.",
    ),
    HarnessSpec(
        # ``gemini_cli``, not ``gemini``: ``gemini`` is an upstream gateway in
        # ``config/provider_catalog.py`` -- Google's own OpenAI-compatible
        # endpoint, which MCC buys tokens *from*. This entry is the CLI MCC
        # serves requests *for*, over its own Gemini surface on this machine.
        # See the module docstring; the two namespaces must not be joined.
        id="gemini_cli",
        display_name="Gemini CLI",
        binary="gemini",
        protocol=HarnessProtocol.GEMINI,
        install_hint=("Install Gemini CLI with: npm install -g @google/gemini-cli"),
        install_methods=GEMINI_METHODS,
        commands=(
            HarnessCommand(
                suffix="gemini",
                target="my_claude_code.cli.launchers.gemini:launch",
                legacy_alias=False,
                help_text="Launch Gemini CLI through the proxy",
                invocations=(
                    HarnessInvocation(
                        arguments='-p "<prompt>"',
                        help_text=(
                            "Run one prompt non-interactively and exit; add "
                            "-i to stay interactive afterwards"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="-m anthropic/<provider>/<model>",
                        help_text=(
                            "Start on one specific MCC-routed model. Gemini "
                            "CLI's own picker lists Google's models only, so "
                            "-m is how MCC's are reached"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='-o json -p "<prompt>"',
                        help_text=(
                            "Same run, machine-readable (stream-json is also accepted)"
                        ),
                    ),
                    HarnessInvocation(
                        arguments='--skip-trust -p "<prompt>"',
                        help_text=(
                            "Gemini CLI refuses a headless run in a folder it "
                            "has not been told to trust; --skip-trust is its "
                            "own opt-out and MCC never sets it for you"
                        ),
                    ),
                    HarnessInvocation(
                        arguments="mcp",
                        help_text=(
                            "Passed straight to Gemini CLI: MCC injects no "
                            "provider for a non-session command"
                        ),
                    ),
                ),
            ),
        ),
        catalogue=HarnessCatalogue(
            format_id="gemini_cli",
            filename="gemini-cli-settings.json",
            config_env_var="GEMINI_CLI_SYSTEM_SETTINGS_PATH",
        ),
        passthrough_commands=frozenset({"mcp", "extensions", "budget"}),
        passthrough_flags=frozenset(
            {"--help", "-h", "--version", "-v", "--list-extensions", "-l"}
        ),
        summary=(
            "Gemini CLI, pointed at an MCC-owned settings document through its "
            "own GEMINI_CLI_SYSTEM_SETTINGS_PATH variable and at this proxy "
            "through GOOGLE_GEMINI_BASE_URL, so ~/.gemini/settings.json is "
            "never written and the OAuth tokens beside it are never read."
        ),
        tagline="Google's Gemini CLI, served through this proxy.",
    ),
    HarnessSpec(
        id="antigravity",
        display_name="Antigravity",
        binary="agy",
        protocol=HarnessProtocol.GEMINI,
        install_hint="Install Antigravity from https://antigravity.google",
        install_unavailable_reason=ANTIGRAVITY_NO_BUTTON_REASON,
        available=False,
        unavailable_reason=ANTIGRAVITY_UNAVAILABLE_REASON,
        summary=(
            "Google's Antigravity CLI does route through this proxy, as of a "
            "2026-09-07 measurement -- but through the Desktop apps card, "
            "which writes its one settings key, rather than through an "
            "mcc-antigravity launcher. Configure it there."
        ),
        tagline="Served from the Desktop apps card, not a launcher.",
    ),
)


@dataclass(frozen=True, slots=True)
class HarnessNotYetCard:
    """A coding agent MCC has assessed and deliberately does not configure.

    A third state beside "servable" and ``HarnessSpec.available=False``. An
    unavailable row is a client MCC *tried* and cannot serve; this one is an
    agent whose model list MCC has already mapped but whose configuration
    hook cannot be used without taking something away from the user. It gets a
    card -- the question "can I use this through MCC?" gets asked, and a dated
    answer somebody can re-check beats silence -- and nothing else: no
    launcher, no console script, no Install method, no detection, no
    catalogue, and no write of any kind into the agent's own files.

    It is **not** a ``HarnessSpec`` on purpose. A spec row is registered as a
    harness everywhere -- ``harness_ids``, the install targets, the catalogue
    fan-out, the RTK toggles -- and none of that is true of an agent MCC has
    decided not to touch this cycle.
    """

    #: Stable id the card is keyed by; never a harness id (``tests`` assert
    #: the two vocabularies do not overlap).
    id: str
    display_name: str
    #: The vendor's package as published, which is what a reader would install
    #: or look up. Rendered verbatim.
    package: str
    #: The date MCC recorded this decision, ISO 8601, rendered on the card.
    assessed_on: str
    #: What it is, in one line.
    summary: str
    #: Why MCC does not configure it today, with the measurement, its date and
    #: the version it was taken on, so the claim can be re-checked.
    reason: str
    #: What would change the decision, stated as a fact about the agent's own
    #: released configuration surface rather than as a promise about MCC.
    would_change: str
    #: The vendor's own page for this tool, when there is one to cite. https,
    #: and only ever rendered as a link, never as a write target.
    doc_url: str = ""


#: Assessed, mapped, and deliberately not configured. See
#: :class:`HarnessNotYetCard`; the bar for adding a row here is a *measurement*
#: that the agent's configuration surface would make MCC's write destructive,
#: not a lack of time.
NOT_YET_CARDS: tuple[HarnessNotYetCard, ...] = (
    HarnessNotYetCard(
        id="dsh",
        display_name="DeepSeek Harness",
        package="@deepseek-ai/dsh",
        assessed_on="2026-10-30",
        summary=(
            "DeepSeek's open-source agent runtime, published on npm as "
            "@deepseek-ai/dsh with the dsh command. MCC has mapped how it takes "
            "a model list and does not configure it yet."
        ),
        reason=(
            "Assessed 2026-10-30, and MCC does not configure it today. Measured "
            "2026-10-29 on @deepseek-ai/dsh 0.2.1-rc.2: the only hook MCC can "
            "use is a plugin row in dsh's YAML patch list (id: llm-pi-ai), and "
            "a patch replaces the targeted row's whole config rather than "
            "merging into it. With one provider written into the profile patch "
            "and MCC's written into the home patch, the composed configuration "
            "contained only MCC's -- the provider the user had configured "
            "through dsh's own Models page was gone. A write that silently "
            "hides a provider you configured is worse than no write, so MCC "
            "writes nothing into dsh's files."
        ),
        would_change=(
            "What would change this: a released dsh version whose "
            "configuration hook merges into the existing provider map, or "
            "offers a per-key patch form, instead of owning the whole plugin "
            "row. dsh is a 0.2.1 release candidate, so the hook's shape can "
            "still move; re-measure it first, and the rest is one serialiser."
        ),
        doc_url="https://www.npmjs.com/package/@deepseek-ai/dsh",
    ),
)


def harness_specs() -> tuple[HarnessSpec, ...]:
    """Return every registered harness, in presentation order."""

    return HARNESS_SPECS


def harness_ids() -> tuple[str, ...]:
    """Return every registered harness id."""

    return tuple(spec.id for spec in HARNESS_SPECS)


def harness_spec(harness_id: str) -> HarnessSpec:
    """Return one registered harness by id."""

    for spec in HARNESS_SPECS:
        if spec.id == harness_id:
            return spec
    raise KeyError(f"unknown harness: {harness_id}")


def harness_display_name(harness_id: str) -> str:
    """Return the human label for one harness id, or the id itself.

    ``harness_spec`` raises on an unknown id, which is right for a launcher
    resolving what to run and wrong for a label: the request log's ``harness``
    column also holds ids for clients that are not launchable agents at all (a
    bare SDK, a curl one-liner, ``unknown``), and one of those arriving must
    render as a name rather than take a page down. Returning the raw id is the
    honest fallback -- it is what the column actually says.

    ``core.client_fingerprint`` owns the labels for that second vocabulary and
    ``config`` may not import ``core``, so the caller that has both packages in
    scope consults it; this function answers only for the registry.
    """

    for spec in HARNESS_SPECS:
        if spec.id == harness_id:
            return spec.display_name
    return harness_id


def rtk_capable_ids() -> tuple[str, ...]:
    """Return the harnesses RTK's shell-tool wrapper is confirmed to apply to."""

    return tuple(spec.id for spec in HARNESS_SPECS if spec.rtk_agent)


def harness_commands() -> tuple[HarnessCommand, ...]:
    """Return every console script the registry owns, across all harnesses."""

    return tuple(entry for spec in HARNESS_SPECS for entry in spec.commands)


def harness_command_lines(spec: HarnessSpec) -> tuple[HarnessCommandLine, ...]:
    """Return every command line a user can type for one harness.

    Generated, never written out: the Coding agents page, the docs and the
    installer summary all render this, so an argument form documented in one
    place cannot go missing from the other two. The order is the order a
    reader wants it -- the bare command, then its documented arguments, then
    the legacy alias, then the token-optimizer toggles.
    """

    lines: list[HarnessCommandLine] = []
    for command in spec.commands:
        lines.append(
            HarnessCommandLine(
                command=command.command,
                help_text=command.help_text,
                kind="primary" if command.primary else "flag",
            )
        )
        lines.extend(
            HarnessCommandLine(
                command=f"{command.command} {invocation.arguments}".rstrip(),
                help_text=invocation.help_text,
                kind="flag",
            )
            for invocation in command.invocations
        )
    lines.extend(
        HarnessCommandLine(
            command=legacy,
            help_text=f"Legacy alias for {command.command}",
            kind="legacy",
        )
        for command in spec.commands
        if (legacy := command.legacy_command) is not None
    )
    if spec.rtk_agent:
        lines.append(
            HarnessCommandLine(
                command=f"mcc-rtk enable {spec.id}",
                help_text=f"Wrap {spec.display_name}'s shell tool with the token optimizer",
                kind="rtk",
            )
        )
        lines.append(
            HarnessCommandLine(
                command=f"mcc-rtk disable {spec.id}",
                help_text=f"Remove the token optimizer from {spec.display_name}",
                kind="rtk",
            )
        )
    return tuple(lines)


def catalogue_specs() -> tuple[HarnessSpec, ...]:
    """Return the harnesses MCC generates a model catalogue for."""

    return tuple(spec for spec in HARNESS_SPECS if spec.catalogue is not None)


def launchable_specs() -> tuple[HarnessSpec, ...]:
    """Return every harness MCC can actually launch.

    The one place that filters on availability, so a consumer that installs
    shims, prints help or wires the tray cannot accidentally publish a command
    for a client this proxy has measured and cannot serve.
    """

    return tuple(spec for spec in HARNESS_SPECS if spec.available)


def not_yet_cards() -> tuple[HarnessNotYetCard, ...]:
    """Return the agents MCC has assessed and deliberately does not configure.

    Served on the Coding agents page beside the launchable harnesses and
    deliberately separate from them: nothing here is a harness id, so no code
    path that installs a shim, writes a catalogue or looks a launcher up can
    ever see one by accident.
    """

    return NOT_YET_CARDS
