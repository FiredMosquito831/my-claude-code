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
) -> ReasoningPolicy:
    """Return the policy this model can actually be told, warning on changes.

    ``capability`` of ``None`` -- no models.dev row and no provider opinion --
    returns ``policy`` itself, unchanged and by identity, so the request built
    from it is byte-identical to the one built before this gating existed.
    """

    if capability is None:
        return policy
    if policy.control is ReasoningControl.OFF:
        # An explicit "off" is already the least surprising thing to send, and
        # every encoder has a disabled path for it. Never rewrite it.
        return policy
    if capability.can_reason is False:
        return _suppress(policy, model_ref)
    if not policy.requests_reasoning:
        # Nothing was asked for; there is nothing to clamp, and inventing a
        # request here would send reasoning nobody wanted.
        return policy

    working = policy
    if working.budget_tokens is not None:
        working = _adapt_budget(working, capability, output_limit, model_ref)
    if working.budget_tokens is None:
        working = _adapt_effort(
            working, capability, max_tokens, output_limit, model_ref
        )
    return working


def _suppress(policy: ReasoningPolicy, model_ref: str) -> ReasoningPolicy:
    """Drop every reasoning control for a model known not to reason."""

    suppressed = ReasoningPolicy.provider_default()
    if policy != suppressed:
        logger.warning(
            "REASONING SUPPRESSED: '{}' is known not to support reasoning;"
            " dropping the requested reasoning controls",
            model_ref,
        )
    return suppressed


def _adapt_budget(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    output_limit: int | None,
    model_ref: str,
) -> ReasoningPolicy:
    """Handle an explicit numeric budget against a known capability."""

    budget = policy.budget_tokens
    if budget is None:
        return policy

    if capability.supports_budget_control:
        clamped = _clamp_budget(budget, output_limit)
        if clamped == budget:
            return policy
        logger.warning(
            "REASONING BUDGET CLAMPED: '{}' accepts {}..{} thinking tokens;"
            " sending {} instead of the requested {}",
            model_ref,
            MINIMUM_BUDGET_TOKENS,
            output_limit if output_limit is not None else "unbounded",
            clamped,
            budget,
        )
        return replace(policy, budget_tokens=clamped)

    if (
        capability.supports_budget_control is False
        and capability.supports_effort_control
    ):
        # R7: no vendor publishes a budget -> effort mapping, so the inverse of
        # FCC's own effort -> budget table is used: the strongest effort whose
        # FCC budget still fits inside what the client asked for.
        derived = _effort_for_budget(budget)
        logger.warning(
            "REASONING BUDGET SUBSTITUTED: '{}' has no thinking-token budget;"
            " sending effort '{}' instead of the requested {} tokens",
            model_ref,
            derived.value,
            budget,
        )
        return ReasoningPolicy(
            control=policy.control,
            effort=derived,
            budget_tokens=None,
        )

    # Budget support unknown: behave exactly as before.
    return policy


def _adapt_effort(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    max_tokens: int | None,
    output_limit: int | None,
    model_ref: str,
) -> ReasoningPolicy:
    """Handle a named effort (or a bare "on") against a known capability."""

    effort = policy.effort

    if capability.supports_effort_control:
        supported = capability.supported_efforts
        if not supported or effort is None or effort in supported:
            return policy
        clamped = _clamp_effort(effort, supported)
        logger.warning(
            "REASONING EFFORT CLAMPED: '{}' does not accept effort '{}';"
            " sending '{}' instead",
            model_ref,
            effort.value,
            clamped.value,
        )
        return replace(policy, effort=clamped)

    if capability.supports_effort_control is not False or effort is None:
        # Effort support unknown, or nothing to translate.
        return policy

    if capability.supports_budget_control:
        budget = _budget_for_effort(effort, max_tokens, output_limit)
        logger.warning(
            "REASONING EFFORT SUBSTITUTED: '{}' has no effort control;"
            " sending a {}-token thinking budget for effort '{}'",
            model_ref,
            budget,
            effort.value,
        )
        # The effort is kept alongside the budget on purpose: an encoder with
        # no budget field of its own still has the effort to fall back on.
        return ReasoningPolicy(
            control=ReasoningControl.ON,
            effort=effort,
            budget_tokens=budget,
        )

    if capability.supports_toggle_control:
        logger.warning(
            "REASONING LEVEL DROPPED: '{}' can only switch thinking on or off;"
            " enabling thinking and discarding effort '{}'",
            model_ref,
            effort.value,
        )
        return ReasoningPolicy.on()

    return policy


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
