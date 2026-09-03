"""The five tier names every coding agent's picker lists, in one place.

Claude Code has never had to name a model. It asks for ``claude-sonnet-5`` and
MCC maps that onto whatever ``MODEL_SONNET`` points at, so the operator moves a
route and every Claude Code session follows without touching the client. Every
*other* harness had to name a concrete ``provider/model`` ref, because that was
the only thing that existed for it -- measured on a real install: across 272,132
logged requests, the number of non-Claude-Code requests naming an alias is zero.

These are the alias names that close that gap:

===============  ==========================================================
``mcc/best``     the route MCC itself starts on -- ``MODEL``
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


#: Which global route each tier points at. ``MODEL_FABLE`` is deliberately not
#: a tier: ``MODEL`` *is* the route MCC starts on, which is what "Best" means
#: and what ``CatalogueModel.is_primary_route`` already marks. An operator who
#: sets ``MODEL`` and ``MODEL_FABLE`` to different refs gets Best on ``MODEL``
#: and Claude Code's ``claude-fable-*`` on ``MODEL_FABLE``; the Tiers card shows
#: both rather than reconciling them behind the operator's back.
GLOBAL_TIER_SETTINGS: dict[ModelTier, GlobalTierSettings] = {
    ModelTier.BEST: GlobalTierSettings(
        model_attr="model",
        fallbacks_attr="model_fallbacks",
        paused_attr="model_paused",
        env_var="MODEL",
        route_label="Default",
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

#: Which per-route reasoning setting a tier inherits, where one exists. Best and
#: Vision have none -- ``settings.py`` defines exactly four ``REASONING_*``
#: route overrides -- so both fall through to the global reasoning policy.
TIER_REASONING_SETTINGS: dict[ModelTier, str] = {
    ModelTier.GOOD: "reasoning_opus",
    ModelTier.MEDIUM: "reasoning_sonnet",
    ModelTier.CHEAP: "reasoning_haiku",
}


def tier_ref(tier: ModelTier) -> str:
    """Return the wire id for one tier, e.g. ``mcc/best``."""

    return f"{TIER_NAMESPACE}/{tier.value}"


def tier_refs() -> tuple[str, ...]:
    """Return every tier's wire id, in picker order."""

    return tuple(tier_ref(tier) for tier in TIER_ORDER)


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
    "TIER_LABELS",
    "TIER_NAMESPACE",
    "TIER_ORDER",
    "TIER_REASONING_SETTINGS",
    "GlobalTierSettings",
    "ModelTier",
    "is_tier_ref",
    "parse_tier_ref",
    "tier_ref",
    "tier_refs",
]
