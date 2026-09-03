"""What one tier resolves to, for one coding agent, right now.

Two callers need exactly the same answer and must not be able to disagree about
it. The router answers a request naming ``mcc/best``; the catalogue builder
writes the alias entry into thirteen coding agents' generated pickers, whose
display name says which model the tier currently points at. A picker that
promised one model while the router served another would be worse than having
no tiers at all, so the rule lives here and both import it.

The rule is pointer semantics, three states deep, and each step is a decision
that was made somewhere else and is only *applied* here:

1. the harness's own entry names a ``model`` -- its chain leads
   (``~/.fcc/harness_tiers.json``, ``config/harness_tiers``);
2. the harness has an entry with no ``model`` -- the global primary leads and
   the harness's own fallbacks follow it;
3. no entry, or no harness identity at all -- the global chain, which is byte
   for byte what Claude Code's own alias for the same route resolves to
   (``core/tier_refs.GLOBAL_TIER_SETTINGS``).

And under all three, the collapse: a global route whose ``MODEL_<TIER>`` is
unset falls through to ``MODEL`` -- primary, fallbacks and pause list together
-- exactly as ``claude-opus-5`` does today. That is deliberately visible rather
than repaired: inventing a different model for an unset tier would be MCC
choosing a model for the operator.
"""

from dataclasses import dataclass

from my_claude_code.config.harness_tiers import HarnessTiers
from my_claude_code.config.model_refs import parse_model_ref_list
from my_claude_code.config.settings import Settings
from my_claude_code.core.tier_refs import GLOBAL_TIER_SETTINGS, ModelTier

#: How a tier alias was answered. ``global`` is the default state of every
#: (harness, tier) pair: the alias followed the same chain Claude Code's own
#: alias follows.
TIER_SOURCE_GLOBAL = "global"
TIER_SOURCE_OVERRIDE = "override"


@dataclass(frozen=True, slots=True)
class TierChain:
    """One tier's resolved chain, and the provenance of that resolution.

    ``source`` is carried because the request log's ``requested_model`` alone
    cannot answer the question an operator actually asks -- "did *my* override
    fire, or did this agent quietly follow the global route?" -- and neither can
    ``resolved_model``, since an override naming the same ref as the global
    chain is indistinguishable from no override at all.
    """

    tier: ModelTier
    #: The harness this was resolved for, or ``None`` when the request carried
    #: no recognisable coding-agent identity.
    harness: str | None
    source: str
    #: The chain's refs, primary first, before any vision or pause policy.
    refs: tuple[str, ...]
    #: Refs switched off on this tier, for this harness.
    paused: tuple[str, ...]
    #: Where ``paused`` lives, so an all-paused tier can name the thing the
    #: reader has to change: an env var for the global chain, a path inside
    #: ``harness_tiers.json`` for a per-harness override.
    paused_label: str

    @property
    def used_override(self) -> bool:
        """Whether a per-harness entry led this chain."""

        return self.source == TIER_SOURCE_OVERRIDE

    @property
    def primary(self) -> str | None:
        """The ref this tier resolves to today, or ``None`` for an empty chain."""

        return self.refs[0] if self.refs else None


def harness_tier_pause_label(harness: str | None, tier: ModelTier) -> str:
    """Name the place a per-harness tier's pause list actually lives.

    A per-harness override's paused refs are not in an env var at all -- they
    are one list inside ``~/.fcc/harness_tiers.json`` -- so telling the operator
    to change ``MODEL_HAIKU_PAUSED`` would send them to a setting that has no
    effect on this agent.
    """

    if not harness:
        return GLOBAL_TIER_SETTINGS[tier].paused_env_var
    return f"harness_tiers.json:{harness}.{tier.value}.paused"


def global_tier_chain(settings: Settings, tier: ModelTier) -> TierChain:
    """Resolve one tier against the global routes alone."""

    spec = GLOBAL_TIER_SETTINGS[tier]
    configured = getattr(settings, spec.model_attr, None)
    if isinstance(configured, str) and configured.strip():
        primary = configured.strip()
        fallbacks = parse_model_ref_list(getattr(settings, spec.fallbacks_attr))
        paused = parse_model_ref_list(getattr(settings, spec.paused_attr))
        paused_label = spec.paused_env_var
    else:
        # The collapse. Not a special case: it is what ``_resolve_model_ref``
        # already does for ``claude-opus-5`` when ``MODEL_OPUS`` is blank, and
        # the pause list has to follow the route it collapsed onto or a ref
        # paused on MODEL would keep being tried under another name.
        primary = settings.model.strip()
        fallbacks = parse_model_ref_list(settings.model_fallbacks)
        paused = parse_model_ref_list(settings.model_paused)
        paused_label = GLOBAL_TIER_SETTINGS[ModelTier.BEST].paused_env_var
    return TierChain(
        tier=tier,
        harness=None,
        source=TIER_SOURCE_GLOBAL,
        refs=(primary, *fallbacks),
        paused=paused,
        paused_label=paused_label,
    )


def resolve_tier_chain(
    settings: Settings,
    tiers: HarnessTiers,
    harness: str | None,
    tier: ModelTier,
) -> TierChain:
    """Resolve one tier for one coding agent. See the module docstring."""

    inherited = global_tier_chain(settings, tier)
    override = tiers.override(harness, tier)
    if override is None or override.is_empty:
        return TierChain(
            tier=tier,
            harness=harness,
            source=TIER_SOURCE_GLOBAL,
            refs=inherited.refs,
            paused=inherited.paused,
            paused_label=inherited.paused_label,
        )

    global_primary = inherited.refs[0] if inherited.refs else ""
    global_fallbacks = inherited.refs[1:]
    primary = override.model or global_primary
    # An override that names its own primary owns its whole chain: silently
    # appending the global fallbacks would attach models the operator never
    # listed under a heading that says these are theirs. An override that names
    # only fallbacks is the middle state and keeps the global primary.
    fallbacks = override.fallbacks or (() if override.model else global_fallbacks)
    return TierChain(
        tier=tier,
        harness=harness,
        source=TIER_SOURCE_OVERRIDE,
        refs=tuple(ref for ref in (primary, *fallbacks) if ref),
        paused=override.paused,
        paused_label=harness_tier_pause_label(harness, tier),
    )


__all__ = [
    "TIER_SOURCE_GLOBAL",
    "TIER_SOURCE_OVERRIDE",
    "TierChain",
    "global_tier_chain",
    "harness_tier_pause_label",
    "resolve_tier_chain",
]
