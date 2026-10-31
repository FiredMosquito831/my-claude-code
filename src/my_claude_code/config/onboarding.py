"""Get-Started onboarding checklist state, derived from real configuration.

A step is "done" because a credential, model, or platform is actually
configured, not because a user ticked a checkbox. The only things this module
persists to disk are whether the checklist has been dismissed, which "guide"
steps have been visited and (7.122.0) which optional cards the user hid with
"Not for me"; every step is recomputed live.
"""

import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .admin.values import load_value_state
from .desktop_apply import DesktopApplyError, is_installed
from .desktop_apply import probe as probe_desktop
from .desktop_apps import DESKTOP_APPS, DesktopAppState, DesktopAppStatus
from .harnesses import harness_specs
from .mcp_media import parse_mcp_media_apps
from .model_refs import parse_provider_type
from .owner_only import write_owner_only_bytes
from .paths import onboarding_state_path
from .provider_catalog import PROVIDER_CATALOG
from .provider_registry import get_provider_registry
from .restore_record import read_mcp_record
from .websearch_catalog import WEBSEARCH_CATALOG


class OnboardingError(Exception):
    """Raised when the persisted onboarding state cannot be written."""


@dataclass(frozen=True)
class OnboardingAgent:
    """One coding agent on this machine, as the Get Started step lists it.

    The same detection the Coding agents page uses, and deliberately the same
    functions rather than a second implementation: a checklist that disagreed
    with the page it links to about whether Codex is installed would be worse
    than no checklist. CLIs are a PATH lookup, desktop apps are a marker-path
    probe, and neither is a guess about what the user has done.
    """

    id: str
    display_name: str
    #: ``cli`` (MCC launches it) or ``desktop`` (MCC writes its file).
    kind: str
    installed: bool
    #: For a desktop app, the probe's own badge; for a CLI, "" -- a CLI is
    #: configured by the launcher at the moment it runs, so there is no file
    #: state to report between runs.
    state: str = ""
    #: The command a human types, for a CLI.
    command: str = ""
    #: Whether this app's card offers a Configure button right now.
    configurable: bool = False
    #: Requests this agent sent in the last seven days.
    requests_7d: int = 0

    @property
    def connected(self) -> bool:
        """Whether this agent counts towards completing the step."""

        return self.requests_7d > 0 or self.state in {"configured", "drifted"}


@dataclass(frozen=True)
class OnboardingStep:
    id: str
    label: str
    description: str
    view: str
    optional: bool
    done: bool
    # Concrete, ordered click-path the user follows to complete the step —
    # not a restatement of `description`, which only says what the step is.
    instructions: tuple[str, ...] = ()
    # Optional DOM target the "Go to..." button scrolls to and highlights:
    # an id selector ("#someId") or an attribute selector
    # ('[data-key="ENV_KEY"]') for a dynamically-rendered field. None means
    # the button only switches views.
    target: str | None = None
    # Guide anchors this step links to, as ``id`` values of ``guide-*``
    # headings. The checklist renders one small link per entry, through the
    # same helper every other surface uses.
    guide_anchors: tuple[str, ...] = ()
    # The detected agents this step lists, for the one step that lists any.
    agents: tuple[OnboardingAgent, ...] = field(default_factory=tuple)
    # 7.122.0: whether MCC can tell if this step is done at all. False only for
    # "Check it worked" while the request log is off -- MCC then sees no
    # request, so the step is shown as "not measured" and is left out of the
    # essentials count instead of staying "to do" forever.
    measurable: bool = True


@dataclass(frozen=True)
class OnboardingState:
    dismissed: bool
    steps: list[OnboardingStep]
    required_total: int
    required_done: int
    complete: bool
    # 7.122.0: the optional cards the user hid with "Not for me", as the
    # dashboard's own catalogue ids. Kept here, in MCC's config folder, so the
    # desktop app and every browser show the same page.
    skipped: tuple[str, ...] = ()


#: The shape of an id the dashboard may hide. The ids are the dashboard's own
#: catalogue ids, so the server checks their form rather than their meaning:
#: a short lowercase word, never markup and never a path.
SKIPPED_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
#: More than the dashboard has cards; a cap so a stray client cannot grow the
#: file without bound.
MAX_SKIPPED = 128


@dataclass(frozen=True)
class PersistedOnboarding:
    """What ``onboarding.json`` holds: the only state the checklist keeps."""

    dismissed: bool = False
    visited: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def is_skippable_id(value: object) -> bool:
    """Whether ``value`` has the form of a card id the dashboard may hide."""

    return isinstance(value, str) and SKIPPED_ID_PATTERN.fullmatch(value) is not None


def normalize_skipped(items: Iterable[object]) -> list[str]:
    """The hidden list as stored: well-formed ids only, once each, sorted, capped.

    Sorted so the file and the payload never depend on the order clicks came
    in; anything that is not a well-formed id is dropped rather than kept, so
    a hand-edited or damaged file can only ever hide less, never break the page.
    """

    return sorted({item for item in items if is_skippable_id(item)})[:MAX_SKIPPED]


def read_persisted() -> PersistedOnboarding:
    """Load everything ``onboarding.json`` holds; never raises.

    A missing or malformed file is a fresh, undismissed checklist with nothing
    visited and nothing hidden. A file written before 7.122.0 has no
    ``skipped`` key and reads as "nothing hidden".
    """

    path = onboarding_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return PersistedOnboarding()

    try:
        data = json.loads(raw)
    except ValueError, TypeError:
        return PersistedOnboarding()

    if not isinstance(data, dict):
        return PersistedOnboarding()

    dismissed = bool(data.get("dismissed", False))
    visited_raw = data.get("visited", [])
    visited = (
        tuple(item for item in visited_raw if isinstance(item, str))
        if isinstance(visited_raw, list)
        else ()
    )
    skipped_raw = data.get("skipped", [])
    skipped = (
        tuple(normalize_skipped(skipped_raw)) if isinstance(skipped_raw, list) else ()
    )
    return PersistedOnboarding(dismissed=dismissed, visited=visited, skipped=skipped)


def load_persisted() -> tuple[bool, list[str]]:
    """Load the persisted onboarding state; never raises.

    Returns ``(dismissed, visited)``. A missing or malformed state file is
    treated as a fresh, undismissed checklist with nothing visited yet.
    """

    persisted = read_persisted()
    return (persisted.dismissed, list(persisted.visited))


def save_persisted(
    *, dismissed: bool, visited: list[str], skipped: list[str] | None = None
) -> None:
    """Persist onboarding state atomically. Raises :class:`OnboardingError` on failure.

    ``skipped`` is the whole hidden list when given; ``None`` keeps the list the
    file already holds, so a save that is only about ``dismissed`` or
    ``visited`` can never un-hide a card. Written owner-only (7.122.0): the same
    staged write ``proxy_chains.json`` uses, restricted before a byte lands.
    """

    path = onboarding_state_path()
    kept = list(read_persisted().skipped) if skipped is None else skipped
    payload = json.dumps(
        {
            "dismissed": dismissed,
            "visited": visited,
            "skipped": normalize_skipped(kept),
        }
    )

    try:
        write_owner_only_bytes(path, payload.encode("utf-8"))
    except OSError as exc:
        raise OnboardingError(f"Failed to save onboarding state: {exc}") from exc


def _provider_credential_configured() -> bool:
    """Return True when at least one non-local provider credential is set."""

    state = load_value_state()
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.local or descriptor.credential_env is None:
            continue
        value = str(state.get(descriptor.credential_env, {}).get("value", ""))
        if value.strip():
            return True
    # A custom provider's keys never reach ``.env`` by design, so iterating the
    # static catalog alone reported "no credential configured" on an install
    # whose only provider was a working custom one -- while the sibling check
    # below, on the same install, took that provider at face value. The two
    # answered differently about one machine.
    return any(
        entry.enabled and entry.api_keys
        for entry in get_provider_registry().list_custom()
    )


def _websearch_credential_configured() -> bool:
    """Return True when at least one web search provider credential is set."""

    state = load_value_state()
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None:
            continue
        value = str(state.get(descriptor.credential_env, {}).get("value", ""))
        if value.strip():
            return True
    return False


def _messaging_platform_configured() -> bool:
    """Return True when the configured messaging platform has its bot token set."""

    state = load_value_state()
    platform = str(state.get("MESSAGING_PLATFORM", {}).get("value", "")).strip()
    if platform == "telegram":
        return bool(str(state.get("TELEGRAM_BOT_TOKEN", {}).get("value", "")).strip())
    if platform == "discord":
        return bool(str(state.get("DISCORD_BOT_TOKEN", {}).get("value", "")).strip())
    return False


def _fallback_model_configured() -> bool:
    """Return True when the fallback ``MODEL`` points at a provider you can use.

    ``MODEL`` ships with a template value, so "is it non-empty" is true on a
    brand-new install and would show this step as already done for someone who
    has configured nothing. The question worth answering is whether the fallback
    will actually serve a request, so this checks that the model ref names a
    provider that has a credential — which is also why keeping the shipped
    default is legitimately "done" once you configure that provider.

    Local providers need no credential, so a local model ref counts as usable.
    """

    state = load_value_state()
    model_ref = str(state.get("MODEL", {}).get("value", "")).strip()
    if not model_ref:
        return False

    provider_type = parse_provider_type(model_ref)
    descriptor = PROVIDER_CATALOG.get(provider_type)
    if descriptor is None:
        # A custom or unrecognised provider prefix: the user chose it
        # deliberately, so take the choice at face value rather than calling a
        # working setup incomplete.
        return True
    if descriptor.local or descriptor.credential_env is None:
        return True
    return bool(str(state.get(descriptor.credential_env, {}).get("value", "")).strip())


def _media_server_added() -> bool:
    """Return True once any app has MCC's media MCP server (7.99.0).

    The same setting the Coding agents buttons write; derived, never a visit.
    Since 7.100.0 also an entry MCC added to an app's own file from its
    Desktop apps card, which writes that file rather than the setting -- the
    record MCC keeps of it is what says so, and Remove forgets it.
    """

    state = load_value_state()
    if parse_mcp_media_apps(str(state.get("MCP_MEDIA_APPS", {}).get("value", ""))):
        return True
    return any(
        read_mcp_record(spec.id) is not None
        for spec in DESKTOP_APPS
        if spec.mcp is not None
    )


def detect_agents(
    *,
    env: Mapping[str, str] | None = None,
    harness_requests: Mapping[str, int] | None = None,
) -> tuple[OnboardingAgent, ...]:
    """Return every coding agent this machine actually has, CLIs first.

    Never raises. A probe that cannot resolve a path for this platform is
    dropped rather than allowed to take the whole checklist down with it --
    the checklist is a nudge, and an exception in it would break the first
    page a new user sees.
    """

    environment = env if env is not None else os.environ
    usage = dict(harness_requests or {})
    agents: list[OnboardingAgent] = []

    for spec in harness_specs():
        if not spec.available:
            continue
        agents.append(
            OnboardingAgent(
                id=spec.id,
                display_name=spec.display_name,
                kind="cli",
                installed=shutil.which(spec.binary) is not None,
                command=spec.command,
                requests_7d=int(usage.get(spec.id, 0)),
            )
        )

    for spec in DESKTOP_APPS:
        if spec.status is DesktopAppStatus.NOT_ROUTABLE:
            continue
        try:
            installed = is_installed(spec, environment)
            probe = probe_desktop(spec, env=environment)
            state = probe.state
        except DesktopApplyError, OSError:
            continue
        agents.append(
            OnboardingAgent(
                id=spec.id,
                display_name=spec.display_name,
                kind="desktop",
                installed=installed,
                state=state.value,
                configurable=(
                    spec.status is DesktopAppStatus.SERVABLE
                    and installed
                    and state is not DesktopAppState.MANAGED
                ),
                requests_7d=int(usage.get(spec.id, 0)),
            )
        )

    return tuple(agents)


def build_state(
    *,
    claude_settings_configured: bool,
    has_requests: bool,
    agents: tuple[OnboardingAgent, ...] = (),
    request_log_enabled: bool = True,
) -> OnboardingState:
    """Build the current onboarding checklist state from live configuration.

    ``claude_settings_configured`` is whether ANY Claude Code settings file
    this machine has (the native one, ``CLAUDE_CONFIG_DIR``'s, a WSL
    distribution's) points at this server; ``has_requests`` whether the request
    log holds at least one request; ``request_log_enabled`` whether MCC is
    recording requests at all, which decides whether "Check it worked" can be
    measured.
    """

    persisted = read_persisted()
    dismissed, visited = persisted.dismissed, list(persisted.visited)

    # 7.122.0 (the user's decision 2): an agent is connected when ANY agent
    # reaches MCC -- a launcher such as mcc-claude, Claude Code inside WSL,
    # Codex, a desktop app -- or when Claude Code's settings point here. Until
    # then this step read only the native settings.json, so everyone who used
    # a launcher, WSL or another agent could never finish it, and the
    # dashboard reopened this page on every load. A request in the log counts
    # however old it is: an agent that worked last month was connected, and an
    # idle week must not bring the page back.
    client_done = (
        claude_settings_configured
        or has_requests
        or any(agent.connected for agent in agents)
    )

    steps = [
        OnboardingStep(
            id="provider",
            label="Add a model provider",
            description=(
                "Give MCC at least one place to send requests: a provider key, "
                "a subscription sign-in, a local server, or any "
                "OpenAI-compatible URL. Every coding agent you point at this "
                "server shares it, Claude Code included."
            ),
            view="providers",
            optional=False,
            done=_provider_credential_configured(),
            instructions=(
                "Open Providers and type a name into 'Search providers' — "
                "Groq, Cerebras and NVIDIA NIM have free tiers.",
                "Press Configure on its card, paste the key into 'Add key' and "
                "press it; the key is saved at once.",
                "Press Refresh models: a model count means the key works.",
                "For anything that is not in the list — a local server or any "
                "OpenAI-compatible URL — use 'Add custom provider' and give it "
                "a base URL and a key.",
            ),
            target="#providersSections",
        ),
        OnboardingStep(
            id="models",
            label="Choose a model for each tier",
            description=(
                "Claude Code asks for Opus, Sonnet, Haiku, Fable and Mythos; "
                "other agents ask for mcc/best, mcc/good, mcc/medium, "
                "mcc/cheap, mcc/cyber and mcc/vision. Each tier goes where you "
                "point it, and the Default model answers anything you have not "
                "mapped."
            ),
            view="model_config",
            optional=False,
            done=_fallback_model_configured(),
            instructions=(
                "Open Model Config: each tier is a rail, its model first and "
                "then the models tried after it.",
                "Set the Default model to one from a provider you added; a "
                "tier left empty follows the Default.",
                "Press Apply at the bottom of the page to save.",
            ),
            target='[data-key="MODEL"]',
        ),
        OnboardingStep(
            id="client",
            label="Connect a coding agent",
            description=(
                "Point at least one agent at this server. This is done as soon "
                "as any agent has sent MCC a request, or Claude Code's settings "
                "or a desktop app's file point here."
            ),
            view="claude",
            optional=False,
            done=client_done,
            instructions=(
                "Run an agent through its launcher in a terminal — mcc-claude, "
                "mcc-codex, mcc-opencode, mcc-gemini… — it points that run at "
                "MCC and changes nothing of yours.",
                "Or write Claude Code's settings once: open Configure Claude "
                "Code and press Configure; it lists every settings file it "
                "found, WSL's included.",
                "Or press Configure on a desktop app's card under Coding agents: "
                "it shows the change to that app's own file first, and Undo "
                "puts it back.",
            ),
            target="#claudeSettingsPanel",
        ),
        OnboardingStep(
            id="connect_agents",
            label="Connect your coding agents (optional)",
            description=(
                "Claude Code is not the only client MCC serves. This step "
                "lists the agents actually on this machine - the CLIs MCC can "
                "launch and the desktop applications whose config file it can "
                "write - with what each one's state is right now."
            ),
            view="coding_agents",
            optional=True,
            # Derived, not remembered. Until 6.56.0 both this step and its
            # sibling were "done" because the user had *visited* the page,
            # which is a fact about navigation rather than about anything
            # being connected. An agent counts when it has actually sent a
            # request through MCC in the last seven days, or when its file is
            # currently pointed here -- the two ways an agent can be connected.
            done=any(agent.connected for agent in agents),
            instructions=(
                "Find an agent below that says Installed.",
                "For a CLI, copy its command and run it in a terminal; the "
                "launcher writes that agent's config for you.",
                "For a desktop app, press Configure on its card - it shows a "
                "real diff of your real file first, backs the file up once, "
                "and Undo takes it back out.",
                "Agents other than Claude Code pick models by tier, as "
                "mcc/cyber, mcc/best, mcc/good, mcc/medium, mcc/cheap and "
                "mcc/vision.",
            ),
            target="#codingAgentsList",
            guide_anchors=("guide-cli", "guide-desktop-apps"),
            agents=agents,
        ),
        OnboardingStep(
            # 7.99.0, the user's 2026-10-18 12:53 amendment: the media server
            # is referenced here too (it replaces "no Get Started media step",
            # 2026-10-04 decision 8). Optional, and done only once an app
            # really has the server.
            id="media_tools",
            label="Let your coding agents make media (optional)",
            description=(
                "MCC's media server gives a coding agent tools for the media "
                "rails you filled in - pictures, speech, transcripts and video - "
                "so it can make them itself instead of being told how. It is "
                "added to one agent at a time with a button, and taken out "
                "with another."
            ),
            view="coding_agents",
            optional=True,
            done=_media_server_added(),
            instructions=(
                "Fill a media rail first: Model Config, the Image, Speech, "
                "Transcription or Video rail.",
                "Open the Coding agents page and press Add MCP server on the "
                "card of the agent that should have the tools.",
                "Start a new session of that agent. It lists media_status and "
                "one tool per filled rail, and asks you before any tool that "
                "spends money.",
                "For an app you start from its own icon - Codex desktop, "
                "OpenCode desktop, Command Code - press Add MCP server on its "
                "card under Desktop apps instead: it shows the change to that "
                "app's own file first, keeps a one-time backup, and Undo takes "
                "it back out.",
            ),
            target="#mcpMediaServerCard",
            guide_anchors=("guide-media-mcp",),
        ),
        OnboardingStep(
            id="websearch",
            label="Enable web search (optional)",
            # 7.122.0: web search answers with no key at all (DuckDuckGo is
            # the keyless default), and keys are added from Manage keys, saved
            # at once -- the old text asked for a key first and described a
            # field and an Apply that no longer exist. `done` still means "a
            # key for a keyed service is set", exactly as before.
            description=(
                "Claude Code's web search and web fetch are answered here. They "
                "work with no key — DuckDuckGo is the keyless default — and a "
                "key adds another service to choose from."
            ),
            view="web_search",
            optional=True,
            done=_websearch_credential_configured(),
            instructions=(
                "Open Web Search: the card at the top says which service "
                "answers right now.",
                "For a keyed service, press Manage keys on its card and paste "
                "the key into 'Add key'; it is saved at once.",
                "To try several services in order, put them on the Search chain card.",
            ),
            target='[data-key="WEB_SEARCH_PROVIDER"]',
        ),
        OnboardingStep(
            id="messaging",
            label="Connect messaging (optional)",
            description=(
                "Link a Telegram or Discord bot so you can chat with Claude "
                "Code from your phone, not just the terminal."
            ),
            view="messaging",
            optional=True,
            done=_messaging_platform_configured(),
            instructions=(
                "Open the Messaging page from the left nav.",
                "Set Messaging Platform to Telegram or Discord.",
                "Paste that platform's bot token into the field that appears.",
                "Click Apply at the bottom of the page to save it.",
            ),
            target='[data-key="MESSAGING_PLATFORM"]',
        ),
        OnboardingStep(
            # 7.122.0 (decision 2): the fourth essential. Measured only while
            # the request log is on -- with it off MCC sees no request, so the
            # step says so and is left out of the count.
            id="analytics",
            label="Check it worked — see your first request in Analytics",
            description=(
                "Ask your agent anything. The request appears in Analytics with "
                "the model that answered, the agent that asked, its tokens and "
                "its cost."
            ),
            view="requests",
            optional=False,
            done=has_requests,
            instructions=(
                "Send any prompt from the agent you connected in the step above.",
                "Open Analytics: the request is at the top of the list, with "
                "the route it took.",
                "A bare 'say OK' may be answered by MCC itself and is then "
                "hidden while Local answers is on Hide — use a real prompt, or "
                "set that filter to Show.",
            ),
            target="#reqStatsCards",
            measurable=request_log_enabled,
        ),
        OnboardingStep(
            id="guide",
            label="Take the tour (optional)",
            description="Skim the quick guide to see what MCC's dashboard can do.",
            view="guide",
            optional=True,
            done="guide" in visited,
            instructions=(
                "Open the Guide page from the left nav and skim through it.",
            ),
        ),
    ]

    # A step MCC cannot measure is neither done nor to do, so it is out of the
    # count: the essentials are complete when every measurable one is.
    counted = [step for step in steps if not step.optional and step.measurable]
    required_total = len(counted)
    required_done = sum(1 for step in counted if step.done)

    return OnboardingState(
        dismissed=dismissed,
        steps=steps,
        required_total=required_total,
        required_done=required_done,
        complete=required_done == required_total,
        skipped=persisted.skipped,
    )
