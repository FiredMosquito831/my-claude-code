"""The five tier names every coding agent's picker lists, in one place.

Claude Code has never had to name a model. It asks for ``claude-sonnet-5`` and
MCC maps that onto whatever ``MODEL_SONNET`` points at, so the operator moves a
route and every Claude Code session follows without touching the client. Every
*other* harness had to name a concrete ``provider/model`` ref, because that was
the only thing that existed for it -- measured on a real install: across 272,132
logged requests, the number of non-Claude-Code requests naming an alias is zero.

These are the alias names that close that gap:

===============  ==========================================================
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

from my_claude_code.core.gateway_model_ids import decode_gateway_model_id

#: The provider-shaped first segment of every tier alias. Reserved in
#: ``config/provider_registry`` so a user-created custom provider cannot claim
#: it and shadow all five names.
TIER_NAMESPACE = "mcc"


class ModelTier(StrEnum):
    """One tier name, as it appears on the wire after ``mcc/``."""

    BEST = "best"
    GOOD = "good"
    MEDIUM = "medium"
    CHEAP = "cheap"
    VISION = "vision"


#: Picker order: strongest first, with vision at the end because it is a
#: capability reservation rather than a rung on the same ladder.
TIER_ORDER: tuple[ModelTier, ...] = (
    ModelTier.BEST,
    ModelTier.GOOD,
    ModelTier.MEDIUM,
    ModelTier.CHEAP,
    ModelTier.VISION,
)

#: What a human calls each tier, in a picker and on the dashboard.
TIER_LABELS: dict[ModelTier, str] = {
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
#: ``mcc/best`` names ``MODEL_FABLE``. The five tiers are one ladder and the top
#: rung has to be the route an operator puts their strongest model on -- which
#: is the Fable route, the one Claude Code's own ``claude-fable-*`` already
#: reaches. ``MODEL`` is not a rung: it is the *default*, the thing every unset
#: route falls back to, and naming it "Best" made the ladder's top step and its
#: floor the same setting. An install that never sets ``MODEL_FABLE`` sees no
#: change at all -- the collapse in ``application/tier_chains`` sends
#: ``mcc/best`` down ``MODEL``'s chain, exactly as it always did.
GLOBAL_TIER_SETTINGS: dict[ModelTier, GlobalTierSettings] = {
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
#: has none -- ``settings.py`` defines exactly four ``REASONING_*`` route
#: overrides and none of them is the adapter's -- so it falls through to the
#: global reasoning policy. Best inherits ``REASONING_FABLE``, because it is
#: the Fable route: a tier that routed through a setting but ignored that
#: setting's reasoning override would be two different answers to "where does
#: mcc/best go".
TIER_REASONING_SETTINGS: dict[ModelTier, str] = {
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
#: as the family whose route it names. ``mcc/best`` is the Fable route, which
#: is the strongest rung, and is advertised as ``opus`` and marked default
#: because the two documented tier names do not include a stronger one. Vision
#: is a capability reservation rather than a rung and rides on ``sonnet``.
TIER_FAMILY_TIERS: dict[ModelTier, tuple[str, bool]] = {
    ModelTier.BEST: ("opus", True),
    ModelTier.GOOD: ("opus", False),
    ModelTier.MEDIUM: ("sonnet", True),
    ModelTier.CHEAP: ("haiku", True),
    ModelTier.VISION: ("sonnet", False),
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
    """Whether a model name is one of the five tier aliases."""

    return parse_tier_ref(model_name) is not None


__all__ = [
    "GLOBAL_TIER_SETTINGS",
    "TIER_FAMILY_TIERS",
    "TIER_LABELS",
    "TIER_NAMESPACE",
    "TIER_ORDER",
    "TIER_REASONING_SETTINGS",
    "GlobalTierSettings",
    "ModelTier",
    "is_tier_ref",
    "parse_tier_ref",
    "tier_alias_by_route_env_var",
    "tier_ref",
    "tier_refs",
]
