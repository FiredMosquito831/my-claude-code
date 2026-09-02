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

from dataclasses import dataclass
from enum import StrEnum


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
#: the document is MCC's own file under ``~/.fcc``, in the same directory as
#: ``~/.fcc/.env``, which already holds the identical ``ANTHROPIC_AUTH_TOKEN``
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

#: ``$version`` in a Qwen settings document. Qwen migrates and *rewrites* any
#: settings file older than its own ``SETTINGS_VERSION``, and MCC's catalogue
#: is a file it would happily rewrite. Declaring the version keeps the
#: generated document exactly as generated -- observed: without it, Qwen Code
#: 0.15.11 rewrote MCC's file on first read.
QWEN_SETTINGS_VERSION = 4


PROTOCOL_LABELS: dict[HarnessProtocol, str] = {
    HarnessProtocol.ANTHROPIC_MESSAGES: "Anthropic Messages (POST /v1/messages)",
    HarnessProtocol.OPENAI_RESPONSES: "OpenAI Responses (POST /v1/responses)",
    HarnessProtocol.OPENAI_CHAT_COMPLETIONS: (
        "OpenAI Chat Completions (POST /v1/chat/completions)"
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
    #: form, ``legacy`` the ``fcc-`` alias, ``rtk`` a token-optimizer toggle.
    kind: str = "primary"


@dataclass(frozen=True, slots=True)
class HarnessCommand:
    """One installed console script that launches a harness."""

    #: Suffix after ``mcc-`` / ``fcc-``; ``"codex"`` gives ``mcc-codex``.
    suffix: str
    #: ``module:function`` target, exactly as ``pyproject.toml`` spells it.
    target: str
    #: Whether a legacy ``fcc-<suffix>`` alias exists. Only the harnesses that
    #: shipped before this registry have one: no install in the world carries
    #: an ``fcc-`` alias for a harness added later, so inventing one would
    #: publish a command that never had users.
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
        """Return the legacy ``fcc-`` alias, when this command has one."""

        return f"fcc-{self.suffix}" if self.legacy_alias else None


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

    @property
    def owned_key_label(self) -> str:
        """Return the owned key as a reader would write it, e.g. ``provider.mcc``."""

        return ".".join(self.owned_key_path)


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
    #: File written under ``~/.fcc``, or ``None`` for a process-local delivery.
    filename: str | None = None
    #: Whether the server creates this file at startup even though nothing has
    #: launched the harness yet. False by default and deliberately so: writing
    #: a catalogue for a CLI the user does not use leaves MCC's files behind
    #: for a tool they never installed, so a launcher-owned catalogue is
    #: created on the first ``mcc-<id>`` run and only *refreshed* thereafter.
    #: True only where a consumer exists that has no launcher to create it.
    created_at_startup: bool = False
    #: The CLI's own documented environment variable naming a config file it
    #: should read *in addition to* the user's own. Set for a harness that
    #: takes its provider block from a file rather than from argv or env: MCC
    #: then owns a file of its own under ``~/.fcc`` and never edits, merges
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

    @property
    def writes_file(self) -> bool:
        """Whether MCC materialises a catalogue file for this harness."""

        return self.filename is not None

    @property
    def delivery(self) -> str:
        """Return how this harness receives its model list.

        ``file`` -- MCC owns a document under ``~/.fcc`` and the CLI is told
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
    #: Executable looked up with ``shutil.which``. MCC never installs it.
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

    @property
    def command(self) -> str:
        """Return the harness's headline ``mcc-`` command."""

        for entry in self.commands:
            if entry.primary:
                return entry.command
        return f"mcc-{self.id}"

    def install_hint_for(self, platform: str) -> str:
        """Return the install hint appropriate to one ``sys.platform`` value."""

        if platform == "win32" and self.install_hint_windows is not None:
            return self.install_hint_windows
        return self.install_hint


HARNESS_SPECS: tuple[HarnessSpec, ...] = (
    HarnessSpec(
        id="claude",
        display_name="Claude Code",
        binary="claude",
        protocol=HarnessProtocol.ANTHROPIC_MESSAGES,
        install_hint=(
            "Install Claude Code with: npm install -g @anthropic-ai/claude-code"
        ),
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
            # The Codex *App* reads this same file from a persistent
            # config.toml and has no launcher to create it, so this one
            # catalogue is written at server startup as well.
            created_at_startup=True,
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
            format_id="opencode",
            filename="kilo-config.json",
            config_env_var="KILO_CONFIG",
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
