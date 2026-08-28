"""Price one reasoning effort against the model's real output allowance.

Two things live here, and they are the same decision seen twice.

*Translation.* A named effort has to become a number for every dialect that
only speaks token budgets (Anthropic, Vertex, NIM, llama.cpp). That number must
come from the routed model's own output allowance -- "high" cannot mean 2,048
tokens on a 230,400-output model and 2,048 on a 16,384-output one, which is
exactly the model-independent behaviour the project forbids. The flat table in
``core.reasoning`` survives only as the last resort for a model nothing
publishes a limit for.

*Reconciliation.* Thinking tokens and answer tokens are spent from one
``max_tokens``. No project surveyed (LiteLLM, OpenCode, OpenRouter, vLLM,
llama.cpp, Ollama, Aider, Continue, LM Studio) reconciles the two -- OpenCode's
``max`` variant is literally ``output_limit - 1``, leaving one token for the
answer. So the split here is FCC's own (WORKING-NOTES 54)::

    answer_floor = min(REASONING_ANSWER_FLOOR_MAX, effective_output // 2)
    budget       = min(requested_budget, effective_output - answer_floor)
    invariant:   budget < max_tokens

``answer_floor`` is proportional on purpose: a flat 16,384 on a 16,384-output
model would leave a budget of zero and silently disable reasoning.

This module is pure arithmetic over numbers the caller already resolved. In
particular the *same* ``effective_output`` that became the request's
``max_tokens`` (see ``application.output_tokens``) has to be passed in --
recomputing it here is how the two would drift apart.
"""

from dataclasses import replace

from loguru import logger

from my_claude_code.config.constants import REASONING_ANSWER_FLOOR_MAX
from my_claude_code.core.reasoning import (
    MINIMUM_BUDGET_TOKENS,
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningEffort,
    ReasoningPolicy,
)

# Share of the effective output budget to spend on thinking, per named effort.
#
# These are the published industry ratios, not FCC inventions: OpenRouter
# documents ``budget_tokens = max(min(max_tokens * ratio, 128000), 1024)`` for
# translating a reasoning effort onto Anthropic-style budgets
# (https://openrouter.ai/docs/use-cases/reasoning-tokens), and Vercel's AI
# Gateway publishes the same ratios. ``max``/``xhigh`` stop at 0.95 rather than
# 1.00 because Anthropic requires the thinking budget to be strictly smaller
# than ``max_tokens``; the answer floor below usually binds first anyway.
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

_EFFORT_RANK: dict[ReasoningEffort, int] = {
    effort: rank for rank, effort in enumerate(ReasoningEffort)
}


def answer_floor_tokens(effective_output: int, floor_max: int) -> int:
    """Return the tokens held back for the visible answer."""

    return min(max(floor_max, 0), max(effective_output, 0) // 2)


def thinking_allowance(effective_output: int, floor_max: int) -> int:
    """Return the largest thinking budget that still leaves an answer."""

    return effective_output - answer_floor_tokens(effective_output, floor_max)


def bound_budget(
    budget: int, effective_output: int, floor_max: int = REASONING_ANSWER_FLOOR_MAX
) -> int:
    """Bound one thinking budget by the answer floor and by ``max_tokens``.

    Three bounds, applied in increasing order of hardness:

    1. the answer floor -- a preference, and the whole point of this module;
    2. the dialect minimum (1,024) -- a hard requirement wherever the allowance
       can afford one at all, so it overrides the floor. A 2,000-token
       allowance splits 1,024/976 rather than 1,000/1,000, because a 1,000-token
       budget is one Anthropic rejects outright;
    3. ``budget < max_tokens`` -- the invariant, enforced last so nothing above
       it can leave a body the provider will refuse.
    """

    allowance = thinking_allowance(effective_output, floor_max)
    bounded = min(budget, allowance)
    bounded = max(bounded, min(MINIMUM_BUDGET_TOKENS, effective_output - 1))
    return max(1, min(bounded, effective_output - 1))


def budget_for_effort(
    effort: ReasoningEffort,
    effective_output: int,
    floor_max: int = REASONING_ANSWER_FLOOR_MAX,
) -> int:
    """Return this effort priced against ``effective_output`` tokens."""

    return bound_budget(
        int(effective_output * EFFORT_BUDGET_RATIOS[effort]),
        effective_output,
        floor_max,
    )


def effort_for_budget(budget: int, effective_output: int | None) -> ReasoningEffort:
    """Return the strongest effort whose budget still fits inside ``budget``.

    The inverse of :func:`budget_for_effort`, for the one case that needs it: a
    model with a token budget the caller named but no budget control to send it
    through. No vendor publishes a budget -> effort mapping, so FCC's own table
    is inverted rather than invented twice.
    """

    order = tuple(ReasoningEffort)
    if effective_output is None:
        affordable = [effort for effort in order if effort.budget_tokens <= budget]
    else:
        affordable = [
            effort
            for effort in order
            if budget_for_effort(effort, effective_output) <= budget
        ]
    if affordable:
        return max(affordable, key=lambda effort: _EFFORT_RANK[effort])
    return order[0]


def reconcile_reasoning_budget(
    policy: ReasoningPolicy,
    *,
    effective_output: int | None,
    floor_max: int = REASONING_ANSWER_FLOOR_MAX,
    model_ref: str = "",
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Fit one policy's thinking budget inside the answer allowance.

    Called once ``max_tokens`` is final, which is why this is not part of
    capability gating: user configuration and the per-model output budget are
    both applied after routing, and reconciling against a stale number would be
    worse than not reconciling at all.

    ``effective_output`` of ``None`` -- nothing publishes an allowance and the
    client named none either -- changes nothing, leaving the flat last-resort
    table in charge exactly as before.
    """

    if effective_output is None or effective_output <= 0:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    if not policy.requests_reasoning:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    if policy.budget_tokens is not None:
        bounded = bound_budget(policy.budget_tokens, effective_output, floor_max)
        if bounded == policy.budget_tokens:
            return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
        message = (
            f"REASONING BUDGET CLAMPED: '{model_ref}' spends thinking and answer"
            f" from one {effective_output}-token allowance; sending {bounded}"
            f" thinking tokens instead of the requested {policy.budget_tokens}"
            f" so {answer_floor_tokens(effective_output, floor_max)} remain for"
            f" the answer"
        )
        logger.warning(message)
        return replace(policy, budget_tokens=bounded), ReasoningAdaptation(
            ReasoningAdaptationKind.CLAMPED, message
        )

    if policy.effort is None:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    derived = budget_for_effort(policy.effort, effective_output, floor_max)
    if derived == policy.effort_budget_tokens:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    # Not an adaptation: the effort the caller asked for is still the effort
    # being sent. Only its translation into tokens -- which most dialects never
    # see -- is now sized to this model rather than to a flat table.
    logger.debug(
        "REASONING BUDGET SIZED: '{}' prices effort '{}' at {} of {} output tokens",
        model_ref,
        policy.effort.value,
        derived,
        effective_output,
    )
    return replace(policy, effort_budget_tokens=derived), ReasoningAdaptation(
        ReasoningAdaptationKind.UNCHANGED, None
    )
