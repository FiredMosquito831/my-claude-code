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
from my_claude_code.application.reasoning_budget import (
    bound_budget,
    budget_for_effort,
    effort_for_budget,
)
from my_claude_code.config.constants import REASONING_ANSWER_FLOOR_MAX
from my_claude_code.core.reasoning import (
    MINIMUM_BUDGET_TOKENS,
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
    combine_reasoning_adaptations,
)

# Declaration order is the documented ordering:
# minimal < low < medium < high < xhigh < max.
_EFFORT_ORDER: tuple[ReasoningEffort, ...] = tuple(ReasoningEffort)
_EFFORT_RANK: dict[ReasoningEffort, int] = {
    effort: rank for rank, effort in enumerate(_EFFORT_ORDER)
}


def adapt_reasoning_policy(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    *,
    max_tokens: int | None = None,
    output_limit: int | None = None,
    answer_floor_max: int = REASONING_ANSWER_FLOOR_MAX,
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

    if _publishes_no_control(capability):
        # models.dev spells this ``reasoning: true`` with ``reasoning_options:
        # []`` -- "reasoning is on, but the caller has no control" -- and 1,223
        # of its 5,230 reasoning models (23%) carry it. Without this branch
        # every guard below is skipped (effort control False, budget control
        # False, toggle control False) and the raw effort falls straight
        # through to the wire, into a field the model does not have.
        return _drop_controls(policy, model_ref)

    working = policy
    adaptations: list[ReasoningAdaptation] = []
    if working.budget_tokens is not None:
        working, adaptation = _adapt_budget(
            working, capability, max_tokens, output_limit, answer_floor_max, model_ref
        )
        adaptations.append(adaptation)
    if working.budget_tokens is None:
        working, adaptation = _adapt_effort(
            working, capability, max_tokens, output_limit, answer_floor_max, model_ref
        )
        adaptations.append(adaptation)
    return working, combine_reasoning_adaptations(*adaptations)


def _publishes_no_control(capability: ModelReasoningCapability) -> bool:
    """Return whether the model reasons but exposes no reasoning knob at all.

    All three ``supports_*_control`` flags explicitly ``False`` is a *stated*
    fact, not silence, and must never be confused with the unknown case where
    they are ``None`` -- unknown has to keep passing through untouched. The
    parser preserves that three-state distinction precisely so this branch can
    read it.
    """

    return capability.can_reason is not False and (
        capability.supports_effort_control is False
        and capability.supports_toggle_control is False
        and capability.supports_budget_control is False
    )


def _drop_controls(
    policy: ReasoningPolicy, model_ref: str
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Keep thinking on for a model that accepts no reasoning control.

    ``DROPPED`` rather than a new kind: it already means "the level was
    discarded, thinking stays on", which is exactly this, and is what the
    toggle-only case reports. A synonym would split one meaning across two
    values and force every consumer to learn both.
    """

    enabled = ReasoningPolicy.on()
    if policy == enabled:
        return enabled, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    if policy.effort is not None:
        asked = f"effort '{policy.effort.value}'"
    elif policy.budget_tokens is not None:
        asked = f"a {policy.budget_tokens}-token thinking budget"
    else:
        asked = "the requested reasoning controls"
    message = (
        f"REASONING LEVEL DROPPED: '{model_ref}' reasons but publishes no"
        f" reasoning control; enabling thinking and discarding {asked}"
    )
    logger.warning(message)
    return enabled, ReasoningAdaptation(ReasoningAdaptationKind.DROPPED, message)


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
    max_tokens: int | None,
    output_limit: int | None,
    answer_floor_max: int,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Handle an explicit numeric budget against a known capability."""

    budget = policy.budget_tokens
    if budget is None:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    if (
        capability.supports_budget_control is False
        and capability.supports_effort_control
    ):
        # R7: no vendor publishes a budget -> effort mapping, so the inverse of
        # FCC's own effort -> budget table is used: the strongest effort whose
        # budget still fits inside what the client asked for.
        derived = effort_for_budget(budget, _effective_output(max_tokens, output_limit))
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

    # Clamp whenever the model's output limit is known, not only when *budget
    # control* is known. ``supports_budget_control`` is ``None`` for most
    # models, so a client budget was passing through entirely unclamped on
    # every one of them even where ``limit.output`` was published: knowing what
    # the model can emit is already enough to know the budget cannot exceed it.
    if not capability.supports_budget_control and output_limit is None:
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)

    clamped = _clamp_budget(budget, output_limit, answer_floor_max)
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


def _adapt_effort(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    max_tokens: int | None,
    output_limit: int | None,
    answer_floor_max: int,
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
        budget = _budget_for_effort(effort, max_tokens, output_limit, answer_floor_max)
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


def _budget_for_effort(
    effort: ReasoningEffort,
    max_tokens: int | None,
    output_limit: int | None,
    answer_floor_max: int,
) -> int:
    """Synthesise a thinking budget for one effort, sized to this model."""

    effective_output = _effective_output(max_tokens, output_limit)
    if effective_output is None:
        return effort.budget_tokens
    return budget_for_effort(effort, effective_output, answer_floor_max)


def _effective_output(max_tokens: int | None, output_limit: int | None) -> int | None:
    """Return the output allowance this request has to share, if known.

    ``None`` when nothing publishes one and the client named none either, which
    is what leaves the flat last-resort table in charge.
    """

    candidates = [value for value in (max_tokens, output_limit) if value is not None]
    if not candidates:
        return None
    return min(candidates)


def _clamp_budget(budget: int, output_limit: int | None, answer_floor_max: int) -> int:
    if output_limit is None:
        return max(budget, MINIMUM_BUDGET_TOKENS)
    return bound_budget(budget, output_limit, answer_floor_max)
