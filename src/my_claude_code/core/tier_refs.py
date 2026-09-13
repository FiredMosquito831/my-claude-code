"""The six tier names every coding agent's picker lists, in one place.

Claude Code has never had to name a model. It asks for ``claude-sonnet-5`` and
MCC maps that onto whatever ``MODEL_SONNET`` points at, so the operator moves a
route and every Claude Code session follows without touching the client. Every
*other* harness had to name a concrete ``provider/model`` ref, because that was
the only thing that existed for it -- measured on a real install: across 272,132
logged requests, the number of non-Claude-Code requests naming an alias is zero.

These are the alias names that close that gap:

===============  ==========================================================
``mcc/cyber``    ``MODEL_MYTHOS``
``mcc/best``     ``MODEL_FABLE``
``mcc/good``     ``MODEL_OPUS``
``mcc/medium``   ``MODEL_SONNET``
``mcc/cheap``    ``MODEL_HAIKU``
``mcc/vision``   ``MODEL_VISION``
===============  ==========================================================

**Pointer semantics, exactly like the Claude aliases.** A tier is not a model:
it is a name for a route. Unset routes collapse onto ``MODEL`` here for the same
reason they do for ``claude-opus-5`` today -- ``_resolve_model_ref`` falls
through -- and the dashboard says so rather than hiding it, because MCC choosing
a distinct model for an unset tier would be MCC picking a model for the user.
That applies to ``mcc/best`` like every other tier: an install that never set
``MODEL_FABLE`` sends ``mcc/best`` down ``MODEL``'s chain, with ``MODEL``'s
fallbacks and ``MODEL``'s pause list, and the card reads "Inherits default".

Two segments, not three. ``mcc/tier/best`` would buy nothing and would make
Kimi's generated key ``mcc/mcc/tier/best``; two segments satisfy both hard shape
rules in the tree -- ``parse_model_name``'s ``split("/", 1)[1]`` and the bundled
Pi extension's "at least two non-empty segments after the gateway prefix".

Owned by ``core`` because all three of ``application`` (the router and the
catalogue serialisers), ``api`` (``/v1/models``) and ``cli`` (the launchers)
need the same answer, and ``core`` is the only owner the three share -- the same
argument ``core/catalogue_refs.py`` makes for itself.
"""

from enum import StrEnum
from typing import NamedTuple

from my_claude_code.core.gateway_model_ids import decode_gateway_model_id

#: The provider-shaped first segment of every tier alias. Reserved in
#: ``config/provider_registry`` so a user-created custom provider cannot claim
#: it and shadow all five names.
TIER_NAMESPACE = "mcc"


class ModelTier(StrEnum):
    """One tier name, as it appears on the wire after ``mcc/``."""

    CYBER = "cyber"
    BEST = "best"
    GOOD = "good"
    MEDIUM = "medium"
    CHEAP = "cheap"
    VISION = "vision"


#: Picker order: strongest first, with vision at the end because it is a
#: capability reservation rather than a rung on the same ladder. Cyber leads
#: because Mythos is the tier above Fable in Anthropic's own family list.
#:
#: Ordering is a *display* fact. Which alias an app is told to default to is a
#: *routing* fact, and the two were the same line of code by accident until
#: 7.2.0: ``TIER_ORDER[0]`` was read as "the default" in two places, so moving a
#: tier to the front of the picker would have silently re-pointed thirteen
#: generated harness catalogues -- and some apps' written default model -- at a
#: route nobody has set. :data:`DEFAULT_TIER` is that second fact, stated once.
TIER_ORDER: tuple[ModelTier, ...] = (
    ModelTier.CYBER,
    ModelTier.BEST,
    ModelTier.GOOD,
    ModelTier.MEDIUM,
    ModelTier.CHEAP,
    ModelTier.VISION,
)

#: The tier every "the default" reader means: the catalogue entry marked
#: ``is_primary_route``, and the model id Configure writes where an app's
#: default model has to be named. It is ``BEST`` -- the Fable route, the rung
#: an operator puts their strongest *configured* model on -- and it stays
#: ``BEST`` regardless of what leads :data:`TIER_ORDER`.
DEFAULT_TIER: ModelTier = ModelTier.BEST

#: What a human calls each tier, in a picker and on the dashboard.
#:
#: Cyber's label is "Mythos", not "Cyber": the setting behind it is
#: ``MODEL_MYTHOS`` and every other rail's label is its route's name, so a rail
#: called "Cyber" would be the one place the page and the ``.env`` disagree.
#: The alias chip beside the heading renders the wire name, so the rail reads
#: ``Mythos (mcc/cyber)`` and neither half has to be memorised.
TIER_LABELS: dict[ModelTier, str] = {
    ModelTier.CYBER: "Mythos",
    ModelTier.BEST: "Best",
    ModelTier.GOOD: "Good",
    ModelTier.MEDIUM: "Medium",
    ModelTier.CHEAP: "Cheap",
    ModelTier.VISION: "Vision",
}


class GlobalTierSettings:
    """The four ``Settings`` attribute names one tier resolves through."""

    __slots__ = (
        "env_var",
        "fallbacks_attr",
        "model_attr",
        "paused_attr",
        "route_label",
    )

    def __init__(
        self,
        *,
        model_attr: str,
        fallbacks_attr: str,
        paused_attr: str,
        env_var: str,
        route_label: str,
    ) -> None:
        self.model_attr = model_attr
        self.fallbacks_attr = fallbacks_attr
        self.paused_attr = paused_attr
        self.env_var = env_var
        self.route_label = route_label

    @property
    def paused_env_var(self) -> str:
        """The env var name holding this tier's paused refs."""

        return f"{self.env_var}_PAUSED"

    @property
    def fallbacks_env_var(self) -> str:
        """The env var name holding this tier's fallback chain."""

        return f"{self.env_var}_FALLBACKS"


#: Which global route each tier points at.
#:
#: ``mcc/best`` names ``MODEL_FABLE``, and ``mcc/cyber`` names ``MODEL_MYTHOS``.
#: The tiers are one ladder whose rungs are the routes an operator actually
#: fills in -- the Fable route, the one Claude Code's own ``claude-fable-*``
#: already reaches, and above it the Mythos route reached by ``claude-mythos-*``
#: (7.2.0; before that, a mythos request matched no route keyword at all and was
#: served silently by ``MODEL``).
#: ``MODEL`` is not a rung: it is the *default*, the thing every unset
#: route falls back to, and naming it "Best" made the ladder's top step and its
#: floor the same setting. An install that never sets ``MODEL_FABLE`` sees no
#: change at all -- the collapse in ``application/tier_chains`` sends
#: ``mcc/best`` down ``MODEL``'s chain, exactly as it always did.
GLOBAL_TIER_SETTINGS: dict[ModelTier, GlobalTierSettings] = {
    ModelTier.CYBER: GlobalTierSettings(
        model_attr="model_mythos",
        fallbacks_attr="model_mythos_fallbacks",
        paused_attr="model_mythos_paused",
        env_var="MODEL_MYTHOS",
        route_label="Mythos",
    ),
    ModelTier.BEST: GlobalTierSettings(
        model_attr="model_fable",
        fallbacks_attr="model_fable_fallbacks",
        paused_attr="model_fable_paused",
        env_var="MODEL_FABLE",
        route_label="Fable",
    ),
    ModelTier.GOOD: GlobalTierSettings(
        model_attr="model_opus",
        fallbacks_attr="model_opus_fallbacks",
        paused_attr="model_opus_paused",
        env_var="MODEL_OPUS",
        route_label="Opus",
    ),
    ModelTier.MEDIUM: GlobalTierSettings(
        model_attr="model_sonnet",
        fallbacks_attr="model_sonnet_fallbacks",
        paused_attr="model_sonnet_paused",
        env_var="MODEL_SONNET",
        route_label="Sonnet",
    ),
    ModelTier.CHEAP: GlobalTierSettings(
        model_attr="model_haiku",
        fallbacks_attr="model_haiku_fallbacks",
        paused_attr="model_haiku_paused",
        env_var="MODEL_HAIKU",
        route_label="Haiku",
    ),
    ModelTier.VISION: GlobalTierSettings(
        model_attr="model_vision",
        fallbacks_attr="model_vision_fallbacks",
        paused_attr="model_vision_paused",
        env_var="MODEL_VISION",
        route_label="Vision",
    ),
}

#: Which per-route reasoning setting a tier inherits, where one exists. Vision
#: has none -- ``settings.py`` defines exactly five ``REASONING_*`` route
#: overrides and none of them is the adapter's -- so it falls through to the
#: global reasoning policy. Best inherits ``REASONING_FABLE``, because it is
#: the Fable route: a tier that routed through a setting but ignored that
#: setting's reasoning override would be two different answers to "where does
#: mcc/best go".
TIER_REASONING_SETTINGS: dict[ModelTier, str] = {
    ModelTier.CYBER: "reasoning_mythos",
    ModelTier.BEST: "reasoning_fable",
    ModelTier.GOOD: "reasoning_opus",
    ModelTier.MEDIUM: "reasoning_sonnet",
    ModelTier.CHEAP: "reasoning_haiku",
}


#: The Claude family tier each ``mcc/*`` alias is advertised as on
#: ``GET /v1/models``, and whether it is that family's default entry.
#:
#: Claude Desktop's gateway mode auto-discovers models from ``GET /v1/models``
#: and "shows only models whose IDs are recognizably Claude"; a gateway serving
#: a Claude model under an opaque alias opts it in by returning
#: ``anthropic_family_tier`` -- "a Claude tier name such as ``sonnet`` or
#: ``opus``" -- optionally with ``is_family_default: true`` where several
#: models map to one tier
#: (https://claude.com/docs/third-party/claude-desktop/gateway, "Models").
#: ``mcc/best`` is exactly such an opaque alias, so without this the five tiers
#: were filtered out of the picker of the one desktop app MCC now configures.
#:
#: The mapping is the ladder's own, not an invention: ``mcc/good`` *is* the
#: Opus route and ``mcc/medium`` the Sonnet route, so each alias is advertised
#: as the family whose route it names. ``mcc/best`` is the Fable route and is
#: advertised as ``fable``; ``mcc/cyber`` is the Mythos route and is advertised
#: as ``mythos``. Vision is a capability reservation rather than a rung and
#: rides on ``sonnet``, un-defaulted.
#:
#: Until 7.2.0 ``mcc/best`` claimed ``opus`` with a comment saying "the two
#: documented tier names do not include a stronger one". That was stale:
#: Claude Desktop 1.52386.0.0 carries ``["sonnet","opus","haiku","fable",
#: "mythos"]`` as a closed enum for ``anthropicFamilyTier`` and pins one
#: ``ANTHROPIC_DEFAULT_<TIER>_MODEL`` per tier from the entry flagged
#: ``isFamilyDefault``. Two entries claiming ``opus`` made the app warn and
#: pick one of them arbitrarily, while ``fable`` and ``mythos`` -- both valid
#: all along -- went unused. One tier per family, one default each.
TIER_FAMILY_TIERS: dict[ModelTier, tuple[str, bool]] = {
    ModelTier.CYBER: ("mythos", True),
    ModelTier.BEST: ("fable", True),
    ModelTier.GOOD: ("opus", True),
    ModelTier.MEDIUM: ("sonnet", True),
    ModelTier.CHEAP: ("haiku", True),
    ModelTier.VISION: ("sonnet", False),
}


class ClaudeDesktopModel(NamedTuple):
    """One row of Claude Desktop's ``inferenceModels`` array, declared."""

    #: The ``name`` the app sends on the wire and MCC's router resolves.
    name: str
    #: ``labelOverride`` -- what the model picker shows. Display only.
    label: str
    #: ``anthropicFamilyTier``. Must be a member of the app's closed enum
    #: ``["sonnet","opus","haiku","fable","mythos"]`` and must agree with
    #: :data:`TIER_FAMILY_TIERS`, which is what ``GET /v1/models`` answers.
    family_tier: str


#: How each tier is named to an Anthropic-shaped desktop app that lists its
#: models explicitly, rather than discovering them.
#:
#: **Why these names and not the ``mcc/*`` aliases.** Claude Desktop
#: 1.52386.0.0 runs every ``inferenceModels[].name`` through a filter
#: (``ES``/``MS`` in the shipped bundle, vendored as
#: ``tests/fixtures/app_rules/claude-desktop-1.52386.0.0.json``): a name is
#: accepted only if it matches ``^(sonnet|opus|haiku|fable|mythos)(-[\d.]+)?$``
#: or contains one of ``claude``/``sonnet``/``opus``/``haiku``/``fable``/
#: ``mythos``/``anthropic``, and is not in a sixty-term foreign-vendor
#: denylist. ``mcc/best`` and its four siblings satisfy none of that, so until
#: 7.3.0 every entry MCC wrote was reported in the app's validation UI as
#: *"is not an Anthropic model and was removed from the list"* -- the list
#: survived only because the filter refuses to empty a list entirely. These
#: five names all pass, and they are the five the user's own proven-working
#: entry carries.
#:
#: **Why the full ids and not the bare tier aliases.** With
#: ``modelDiscoveryEnabled: false`` the app flags a bare ``"mythos"`` or
#: ``"sonnet"`` with *"Aliases like 'sonnet' are resolved via model discovery.
#: Use the full model ID."* -- a check that is new in 1.52386.0.0.
#:
#: The names are display aliases MCC resolves itself: the app never checks
#: them against a catalogue, only against the filter above, and each one
#: reaches its own tier through ``application/routing``'s keyword table
#: (``mythos``, ``fable``, ``opus``, ``sonnet``, ``haiku``).
#:
#: Vision is deliberately absent. ``MODEL_VISION`` is a server-side diversion
#: MCC applies when a chain carries images its model cannot take; offering it
#: in a picker asks the user to choose an adapter MCC chooses for them, and
#: the working entry on a real machine has no vision row.
CLAUDE_DESKTOP_TIER_MODELS: dict[ModelTier, ClaudeDesktopModel] = {
    ModelTier.CYBER: ClaudeDesktopModel("claude-mythos-5.1", "Mythos 5.1", "mythos"),
    ModelTier.BEST: ClaudeDesktopModel("claude-fable-5.1", "Fable 5.1", "fable"),
    ModelTier.GOOD: ClaudeDesktopModel("claude-opus-5", "Opus 5", "opus"),
    ModelTier.MEDIUM: ClaudeDesktopModel("claude-sonnet-5", "Sonnet 5", "sonnet"),
    ModelTier.CHEAP: ClaudeDesktopModel("claude-haiku-4.5", "Haiku 4.5", "haiku"),
}


def tier_ref(tier: ModelTier) -> str:
    """Return the wire id for one tier, e.g. ``mcc/best``."""

    return f"{TIER_NAMESPACE}/{tier.value}"


def tier_refs() -> tuple[str, ...]:
    """Return every tier's wire id, in picker order."""

    return tuple(tier_ref(tier) for tier in TIER_ORDER)


def tier_alias_by_route_env_var() -> dict[str, str]:
    """Map each global route's env var onto the tier alias that names it.

    ``{"MODEL": "mcc/best", "MODEL_OPUS": "mcc/good", ...}``.

    This is what the Model Config page's route headings read, so the alias a
    heading shows and the alias the router answers to are the same fact rather
    than two lists that agree until one is edited. Note what is *not* here:
    ``MODEL`` has no entry, because it is not a tier -- it is the default every
    unset route falls back to, and printing ``mcc/best`` beside the Default rail
    would tell a reader that editing that rail is what moves ``mcc/best``, when
    what moves it is ``MODEL_FABLE`` (see ``GLOBAL_TIER_SETTINGS`` above).
    """

    return {GLOBAL_TIER_SETTINGS[tier].env_var: tier_ref(tier) for tier in TIER_ORDER}


def parse_tier_ref(model_name: str | None) -> ModelTier | None:
    """Return the tier a model name asks for, in either wire spelling.

    Both spellings must parse because the harnesses genuinely split across two:
    Cline, Crush, Droid, Gemini CLI, Qwen and Aider put the gateway id
    ``anthropic/<provider>/<model>`` on the wire, while Codex, Command Code,
    OpenCode, Pi and Kimi put the bare ``<provider>/<model>``. So ``mcc/best``
    arrives as ``mcc/best`` from one half of the fleet and as
    ``anthropic/mcc/best`` from the other, and the router has to answer the same
    way to both.

    The tier segment is matched **exactly**, never as a substring. The old
    ``_matched_route`` in the router is a substring match -- any model name
    *containing* ``opus`` lands on the Opus rail -- and repeating that hazard
    with five more names would be a routing bug nobody could see.
    """

    if not model_name:
        return None
    candidate = model_name.strip()
    decoded = decode_gateway_model_id(candidate)
    if decoded is not None:
        candidate = f"{decoded.provider_id}/{decoded.provider_model}"
    namespace, separator, tier_name = candidate.partition("/")
    if not separator or namespace.lower() != TIER_NAMESPACE:
        return None
    try:
        return ModelTier(tier_name.strip().lower())
    except ValueError:
        return None


def is_tier_ref(model_name: str | None) -> bool:
    """Whether a model name is one of the tier aliases."""

    return parse_tier_ref(model_name) is not None


__all__ = [
    "CLAUDE_DESKTOP_TIER_MODELS",
    "DEFAULT_TIER",
    "GLOBAL_TIER_SETTINGS",
    "TIER_FAMILY_TIERS",
    "TIER_LABELS",
    "TIER_NAMESPACE",
    "TIER_ORDER",
    "TIER_REASONING_SETTINGS",
    "ClaudeDesktopModel",
    "GlobalTierSettings",
    "ModelTier",
    "is_tier_ref",
    "parse_tier_ref",
    "tier_alias_by_route_env_var",
    "tier_ref",
    "tier_refs",
]
