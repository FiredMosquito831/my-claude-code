"""Constrain resolved reasoning intent to what one model actually accepts.

The resolved :class:`ReasoningPolicy` says what the user asked for; a
:class:`ModelReasoningCapability` says what the model can be told. This module
is the single place the two meet, and it produces another
:class:`ReasoningPolicy` -- never a wire field. Provider encoders keep sole
ownership of the wire shape, so a value this module cannot express through the
encoder that will receive it simply degrades to the nearest thing that encoder
already knew how to send.

The overriding rule is that an *unknown* capability changes nothing. Most
providers publish no reasoning metadata at all, and clamping on silence would
regress every one of them.
"""

from dataclasses import replace

from loguru import logger

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
)

# Declaration order is the documented ordering:
# minimal < low < medium < high < xhigh < max.
_EFFORT_ORDER: tuple[ReasoningEffort, ...] = tuple(ReasoningEffort)
_EFFORT_RANK: dict[ReasoningEffort, int] = {
    effort: rank for rank, effort in enumerate(_EFFORT_ORDER)
}

# Anthropic's documented minimum extended-thinking budget.
MINIMUM_BUDGET_TOKENS = 1024

# Severity order for merging several sub-adaptations into one descriptor. A
# request that is both substituted and clamped is reported at the worse of
# the two, so the admin UI never under-represents what gating did.
_ADAPTATION_SEVERITY: dict[ReasoningAdaptationKind, int] = {
    ReasoningAdaptationKind.UNCHANGED: 0,
    ReasoningAdaptationKind.SUBSTITUTED: 1,
    ReasoningAdaptationKind.CLAMPED: 2,
    ReasoningAdaptationKind.DROPPED: 3,
    ReasoningAdaptationKind.SUPPRESSED: 4,
}

# Share of the effective output budget to spend on thinking, per named effort.
#
# These are the published industry ratios, not FCC inventions: OpenRouter
# documents ``budget_tokens = max(min(max_tokens * ratio, 128000), 1024)`` for
# translating a reasoning effort onto Anthropic-style budgets
# (https://openrouter.ai/docs/use-cases/reasoning-tokens), and Vercel's AI
# Gateway publishes the same ratios. ``max``/``xhigh`` stop at 0.95 rather than
# 1.00 because Anthropic requires the thinking budget to be strictly smaller
# than ``max_tokens``.
#
# This is the ONLY place these numbers exist. Do not inline them elsewhere.
EFFORT_BUDGET_RATIOS: dict[ReasoningEffort, float] = {
    ReasoningEffort.MINIMAL: 0.10,
    ReasoningEffort.LOW: 0.20,
    ReasoningEffort.MEDIUM: 0.50,
    ReasoningEffort.HIGH: 0.80,
    ReasoningEffort.XHIGH: 0.95,
    ReasoningEffort.MAX: 0.95,
}


def adapt_reasoning_policy(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    *,
    max_tokens: int | None = None,
    output_limit: int | None = None,
    model_ref: str = "",
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Return the policy this model can actually be told, and what changed.

    The second element describes the adaptation so the request log and admin
    UI can surface the warning that this function currently only emits to the
    server log. ``capability`` of ``None`` -- no models.dev row and no provider
    opinion -- returns ``policy`` itself, unchanged and by identity, with an
    ``UNCHANGED`` adaptation, so the request built from it is byte-identical to
    the one built before this gating existed.
    """

    if capability is None:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    if policy.control is ReasoningControl.OFF:
        # An explicit "off" is already the least surprising thing to send, and
        # every encoder has a disabled path for it. Never rewrite it -- unless
        # the model is known to reject disabled thinking outright, in which
        # case OFF would fail the whole request and the floor is the honest
        # nearest thing to what was asked for.
        if capability.mandatory is True:
            return _mandatory_off_rewrite(policy, capability, model_ref)
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    if capability.can_reason is False:
        return _suppress(policy, model_ref)
    if not policy.requests_reasoning:
        # Nothing was asked for; there is nothing to clamp, and inventing a
        # request here would send reasoning nobody wanted.
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    working = policy
    adaptations: list[ReasoningAdaptation] = []
    if working.budget_tokens is not None:
        working, adaptation = _adapt_budget(
            working, capability, output_limit, model_ref
        )
        if adaptation.kind is not ReasoningAdaptationKind.UNCHANGED:
            adaptations.append(adaptation)
    if working.budget_tokens is None:
        working, adaptation = _adapt_effort(
            working, capability, max_tokens, output_limit, model_ref
        )
        if adaptation.kind is not ReasoningAdaptationKind.UNCHANGED:
            adaptations.append(adaptation)
    return working, _merge_adaptations(policy, working, adaptations)


def _merge_adaptations(
    requested: ReasoningPolicy,
    applied: ReasoningPolicy,
    adaptations: list[ReasoningAdaptation],
) -> ReasoningAdaptation:
    """Collapse zero or more sub-adaptations into the single descriptor.

    The R7 path can substitute a budget for an effort *and* then clamp that
    effort, so the caller accumulates every sub-step and this collapses them:
    one message per step, joined, and a ``kind`` severe enough to cover all of
    them. With no steps the policy passed through untouched.
    """

    if not adaptations:
        return ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    messages = [adaptation.message for adaptation in adaptations if adaptation.message]
    kind = max(adaptations, key=lambda a: _ADAPTATION_SEVERITY[a.kind]).kind
    return ReasoningAdaptation(kind, " ".join(messages))


def _mandatory_off_rewrite(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Rewrite an OFF request for a model that cannot disable thinking.

    The model rejects ``effort: "none"`` outright, so an honest "off" the wire
    would 400. The closest thing the model can express is thinking on at the
    floor: the lowest effort it advertises when it has a vocabulary, or
    ``ReasoningControl.ADAPTIVE`` when it does not (lets the model itself pick
    the floor). The SUBSTITUTED warning surfaces in the request log so the
    operator sees why the off they asked for is not what was sent.
    """

    vocabulary = capability.supported_efforts
    if vocabulary:
        floor = min(vocabulary, key=lambda effort: _EFFORT_RANK[effort])
        rewritten = ReasoningPolicy.on(effort=floor)
        message = (
            f"REASONING OFF SUBSTITUTED: '{model_ref}' cannot run with thinking"
            f" disabled; sending effort '{floor.value}' (its lowest) instead"
        )
    else:
        rewritten = ReasoningPolicy.adaptive()
        message = (
            f"REASONING OFF SUBSTITUTED: '{model_ref}' cannot run with thinking"
            f" disabled; sending adaptive thinking instead"
        )
    logger.warning(message)
    return rewritten, ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)


def _suppress(
    policy: ReasoningPolicy, model_ref: str
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Drop every reasoning control for a model known not to reason."""

    suppressed = ReasoningPolicy.provider_default()
    if policy == suppressed:
        return suppressed, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    message = (
        f"REASONING SUPPRESSED: '{model_ref}' is known not to support reasoning;"
        f" dropping the requested reasoning controls"
    )
    logger.warning(message)
    return suppressed, ReasoningAdaptation(ReasoningAdaptationKind.SUPPRESSED, message)


def _adapt_budget(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    output_limit: int | None,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Handle an explicit numeric budget against a known capability."""

    budget = policy.budget_tokens
    if budget is None:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    if capability.supports_budget_control:
        clamped = _clamp_budget(budget, output_limit)
        if clamped == budget:
            return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
        message = (
            f"REASONING BUDGET CLAMPED: '{model_ref}' accepts"
            f" {MINIMUM_BUDGET_TOKENS}..{output_limit if output_limit is not None else 'unbounded'}"
            f" thinking tokens; sending {clamped} instead of the requested {budget}"
        )
        logger.warning(message)
        return replace(policy, budget_tokens=clamped), ReasoningAdaptation(
            ReasoningAdaptationKind.CLAMPED, message
        )

    if (
        capability.supports_budget_control is False
        and capability.supports_effort_control
    ):
        # R7: no vendor publishes a budget -> effort mapping, so the inverse of
        # FCC's own effort -> budget table is used: the strongest effort whose
        # FCC budget still fits inside what the client asked for.
        derived = _effort_for_budget(budget)
        message = (
            f"REASONING BUDGET SUBSTITUTED: '{model_ref}' has no thinking-token"
            f" budget; sending effort '{derived.value}' instead of the requested"
            f" {budget} tokens"
        )
        logger.warning(message)
        return ReasoningPolicy(
            control=policy.control,
            effort=derived,
            budget_tokens=None,
        ), ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)

    # Budget support unknown: behave exactly as before.
    return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)


def _adapt_effort(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    max_tokens: int | None,
    output_limit: int | None,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Handle a named effort (or a bare "on") against a known capability."""

    effort = policy.effort

    if capability.supports_effort_control:
        supported = capability.supported_efforts
        if not supported or effort is None or effort in supported:
            return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
        clamped = _clamp_effort(effort, supported)
        message = (
            f"REASONING EFFORT CLAMPED: '{model_ref}' does not accept effort"
            f" '{effort.value}'; sending '{clamped.value}' instead"
        )
        logger.warning(message)
        return replace(policy, effort=clamped), ReasoningAdaptation(
            ReasoningAdaptationKind.CLAMPED, message
        )

    if capability.supports_effort_control is not False or effort is None:
        # Effort support unknown, or nothing to translate.
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    if capability.supports_budget_control:
        budget = _budget_for_effort(effort, max_tokens, output_limit)
        message = (
            f"REASONING EFFORT SUBSTITUTED: '{model_ref}' has no effort control;"
            f" sending a {budget}-token thinking budget for effort '{effort.value}'"
        )
        logger.warning(message)
        # The effort is kept alongside the budget on purpose: an encoder with
        # no budget field of its own still has the effort to fall back on.
        return ReasoningPolicy(
            control=ReasoningControl.ON,
            effort=effort,
            budget_tokens=budget,
        ), ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)

    if capability.supports_toggle_control:
        message = (
            f"REASONING LEVEL DROPPED: '{model_ref}' can only switch thinking on"
            f" or off; enabling thinking and discarding effort '{effort.value}'"
        )
        logger.warning(message)
        return ReasoningPolicy.on(), ReasoningAdaptation(
            ReasoningAdaptationKind.DROPPED, message
        )

    return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)


def _clamp_effort(
    requested: ReasoningEffort, supported: frozenset[ReasoningEffort]
) -> ReasoningEffort:
    """Return the closest supported effort at or below ``requested``.

    A request below everything the model offers clamps *up* to the model's
    lowest effort: the user asked for reasoning, and only an explicit OFF may
    take it away.
    """

    at_or_below = [
        effort
        for effort in supported
        if _EFFORT_RANK[effort] <= _EFFORT_RANK[requested]
    ]
    if at_or_below:
        return max(at_or_below, key=lambda effort: _EFFORT_RANK[effort])
    return min(supported, key=lambda effort: _EFFORT_RANK[effort])


def _effort_for_budget(budget: int) -> ReasoningEffort:
    """Return the strongest effort whose FCC budget fits inside ``budget``."""

    affordable = [effort for effort in _EFFORT_ORDER if effort.budget_tokens <= budget]
    if affordable:
        return max(affordable, key=lambda effort: _EFFORT_RANK[effort])
    return _EFFORT_ORDER[0]


def _budget_for_effort(
    effort: ReasoningEffort, max_tokens: int | None, output_limit: int | None
) -> int:
    """Synthesise a thinking budget for one effort (see EFFORT_BUDGET_RATIOS)."""

    effective_max = _effective_max_tokens(max_tokens, output_limit)
    return _clamp_budget(
        int(effective_max * EFFORT_BUDGET_RATIOS[effort]), output_limit
    )


def _effective_max_tokens(max_tokens: int | None, output_limit: int | None) -> int:
    candidates = [value for value in (max_tokens, output_limit) if value is not None]
    if not candidates:
        return MINIMUM_BUDGET_TOKENS
    return min(candidates)


def _clamp_budget(budget: int, output_limit: int | None) -> int:
    clamped = max(budget, MINIMUM_BUDGET_TOKENS)
    if output_limit is not None:
        clamped = min(clamped, output_limit)
    return clamped
