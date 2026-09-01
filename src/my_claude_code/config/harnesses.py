"""The one registry of coding-agent harnesses MCC can launch.

A *harness* is a third-party coding-agent CLI that MCC serves: Claude Code,
Codex, Pi. It is not a *provider* -- ``config.provider_catalog`` names the
upstream gateways MCC buys tokens from, and several of those share a name with
a CLI (``opencode``, ``commandcode``, ``cline``). The two are unrelated and can
be on at once: a harness sits downstream of MCC, a provider upstream of it.
``harness_id`` and ``provider_id`` are therefore separate namespaces and must
not be joined.

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


PROTOCOL_LABELS: dict[HarnessProtocol, str] = {
    HarnessProtocol.ANTHROPIC_MESSAGES: "Anthropic Messages (POST /v1/messages)",
    HarnessProtocol.OPENAI_RESPONSES: "OpenAI Responses (POST /v1/responses)",
}


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

    @property
    def command(self) -> str:
        """Return the native ``mcc-`` command name."""

        return f"mcc-{self.suffix}"

    @property
    def legacy_command(self) -> str | None:
        """Return the legacy ``fcc-`` alias, when this command has one."""

        return f"fcc-{self.suffix}" if self.legacy_alias else None


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

    @property
    def writes_file(self) -> bool:
        """Whether MCC materialises a catalogue file for this harness."""

        return self.filename is not None


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


def catalogue_specs() -> tuple[HarnessSpec, ...]:
    """Return the harnesses MCC generates a model catalogue for."""

    return tuple(spec for spec in HARNESS_SPECS if spec.catalogue is not None)
