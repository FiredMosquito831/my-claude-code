"""Get-Started onboarding checklist state, derived from real configuration.

A step is "done" because a credential, model, or platform is actually
configured, not because a user ticked a checkbox. The only thing this module
persists to disk is whether the checklist has been dismissed and which
"guide" steps have been visited; every other step is recomputed live.
"""

import json
import os
from dataclasses import dataclass

from .admin.values import load_value_state
from .model_refs import parse_provider_type
from .paths import onboarding_state_path
from .provider_catalog import PROVIDER_CATALOG
from .provider_registry import get_provider_registry
from .websearch_catalog import WEBSEARCH_CATALOG


class OnboardingError(Exception):
    """Raised when the persisted onboarding state cannot be written."""


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


@dataclass(frozen=True)
class OnboardingState:
    dismissed: bool
    steps: list[OnboardingStep]
    required_total: int
    required_done: int
    complete: bool


def load_persisted() -> tuple[bool, list[str]]:
    """Load the persisted onboarding state; never raises.

    Returns ``(dismissed, visited)``. A missing or malformed state file is
    treated as a fresh, undismissed checklist with nothing visited yet.
    """

    path = onboarding_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return (False, [])

    try:
        data = json.loads(raw)
    except ValueError, TypeError:
        return (False, [])

    if not isinstance(data, dict):
        return (False, [])

    dismissed = bool(data.get("dismissed", False))
    visited_raw = data.get("visited", [])
    if not isinstance(visited_raw, list):
        return (dismissed, [])
    visited = [item for item in visited_raw if isinstance(item, str)]
    return (dismissed, visited)


def save_persisted(*, dismissed: bool, visited: list[str]) -> None:
    """Persist onboarding state atomically. Raises :class:`OnboardingError` on failure."""

    path = onboarding_state_path()
    payload = json.dumps({"dismissed": dismissed, "visited": visited})

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
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


def build_state(
    *, claude_settings_configured: bool, has_requests: bool
) -> OnboardingState:
    """Build the current onboarding checklist state from live configuration."""

    dismissed, visited = load_persisted()

    steps = [
        OnboardingStep(
            id="provider",
            label="Connect a model provider",
            description=(
                "Add a free API key from a supported provider so MCC has a "
                "model to route your Claude Code requests to."
            ),
            view="providers",
            optional=False,
            done=_provider_credential_configured(),
            instructions=(
                "Open the Providers page from the left nav.",
                "Pick a provider with a free tier — Cerebras, Groq, and "
                "NVIDIA NIM all have one.",
                "Type its name into 'Search providers' to jump straight to its card.",
                "Press Configure, then paste the API key into 'Add key' and press it.",
                "Press Refresh models to confirm the key works.",
            ),
            target="#providersSections",
        ),
        OnboardingStep(
            id="models",
            label="Set your fallback model",
            description=(
                "Pick which provider/model handles requests by default. "
                "Per-tier overrides (Opus, Sonnet, Haiku, Fable) are optional "
                "and can be set later."
            ),
            view="model_config",
            optional=False,
            done=_fallback_model_configured(),
            instructions=(
                "Open the Model Config page from the left nav.",
                "Set Default Model to a model from the provider you just configured.",
                "Click Apply at the bottom of the page to save it.",
            ),
            target='[data-key="MODEL"]',
        ),
        OnboardingStep(
            id="client",
            label="Point Claude Code at MCC",
            description=(
                "Apply MCC's proxy settings to your Claude Code settings.json "
                "so the CLI sends requests through MCC."
            ),
            view="claude",
            optional=False,
            done=claude_settings_configured,
            instructions=(
                "Open the Configure Claude Code page from the left nav.",
                "Click Configure to write MCC's URL and token into settings.json.",
            ),
            target="#claudeSettingsPanel",
        ),
        OnboardingStep(
            id="websearch",
            label="Enable web search (optional)",
            description=(
                "Add a web search provider key so Claude can look things up "
                "live instead of relying on training data alone."
            ),
            view="web_search",
            optional=True,
            done=_websearch_credential_configured(),
            instructions=(
                "Open the Web Search page from the left nav.",
                "Set Web Search Provider to one with a free tier, such as "
                "Brave or Tavily.",
                "Paste its API key into the field that appears below.",
                "Click Apply at the bottom of the page to save it.",
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
            id="analytics",
            label="Send your first request (optional)",
            description=(
                "Run a Claude Code command through MCC and watch it show up "
                "in the requests log."
            ),
            view="requests",
            optional=True,
            done=has_requests,
            instructions=(
                "Finish the Point Claude Code at MCC step above so a real "
                "request has somewhere to go.",
                "Run any Claude Code command in your terminal.",
                "Open the Analytics page from the left nav to see it land "
                "in the requests log.",
            ),
            target="#reqStatsCards",
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

    required_total = sum(1 for step in steps if not step.optional)
    required_done = sum(1 for step in steps if not step.optional and step.done)

    return OnboardingState(
        dismissed=dismissed,
        steps=steps,
        required_total=required_total,
        required_done=required_done,
        complete=required_done == required_total,
    )
