"""Decide one request's ``max_tokens`` from the routed model's real capability.

The governing rule (WORKING-NOTES 54)::

    requested <= model maximum  ->  send what was requested
    requested >  model maximum  ->  send the MODEL'S MAXIMUM
    unknown                     ->  fall back, and say so

Three decisions live here and they are kept apart on purpose -- the fallback
for a model nobody describes, the operator's optional absolute ceiling, and the
clamp of a client's ask down to what the model published. Fusing them into one
``min()`` is how a fallback silently becomes a cap, which is the defect this
module was written to remove.

This sits in the application layer rather than in ``core`` because the numbers
it needs -- the model's published limit, its context window, the operator's
configuration -- come from settings and the model catalogue, neither of which
``core/anthropic/conversion.py`` is allowed to reach. The decision is made
once, here, and travels to the provider as the routed request's own
``max_tokens``.
"""

from dataclasses import dataclass

from loguru import logger

from my_claude_code.config.constants import (
    MAX_OUTPUT_TOKENS_CONTEXT_MARGIN,
    REASONING_ANSWER_FLOOR_MAX,
)


@dataclass(frozen=True, slots=True)
class OutputTokenLimits:
    """What is known about one resolved model's output capacity.

    ``limit`` and ``context_length`` are what a source actually published for
    this model. ``None`` means unknown -- never unlimited, and never zero.
    ``unknown_default``, ``ceiling`` and ``context_margin`` are operator
    configuration, carried alongside so :func:`resolve_max_output_tokens` stays
    a pure function of its arguments and can be tested without Settings.
    """

    limit: int | None = None
    context_length: int | None = None
    unknown_default: int | None = None
    ceiling: int | None = None
    context_margin: int = MAX_OUTPUT_TOKENS_CONTEXT_MARGIN
    # Thinking tokens come out of this same allowance, so the answer
    # reserve travels with it rather than in a parallel record: the two
    # numbers only mean anything together (WORKING-NOTES 54).
    answer_floor_max: int = REASONING_ANSWER_FLOOR_MAX


# "Nothing is known and nothing is configured." Shared because it is frozen
# and carries no state, and because a dataclass default may not be a call.
UNKNOWN_OUTPUT_TOKEN_LIMITS = OutputTokenLimits()


def resolve_max_output_tokens(
    requested: int | None,
    *,
    limits: OutputTokenLimits,
    input_tokens: int = 0,
    model_ref: str = "",
) -> int | None:
    """Return the ``max_tokens`` to send, or ``None`` to leave it unset.

    ``None`` comes back only when nothing at all is known and the client named
    nothing either, which leaves the provider profile's last-resort default in
    charge exactly as before.
    """

    resolved = _apply_model_limit(requested, limits.limit, model_ref)
    resolved = _fall_back_when_unknown(resolved, limits.unknown_default, model_ref)
    resolved = _apply_ceiling(resolved, limits.ceiling, model_ref)
    return _apply_context_headroom(resolved, limits, input_tokens, model_ref)


def _fall_back_when_unknown(
    resolved: int | None, unknown_default: int | None, model_ref: str
) -> int | None:
    """Supply a value when nobody -- client or catalogue -- named one.

    Reachable only when the client sent no ``max_tokens`` *and* no source
    published a limit for this model, so it can never lower an explicit
    request. A fallback that could do that would be an invented limit.
    """

    if resolved is not None or unknown_default is None:
        return resolved
    # Debug, not warning: on a provider that publishes no metadata at all this
    # is every single request, and a warning nobody can act on is noise.
    logger.debug(
        "MAX TOKENS UNKNOWN: nothing publishes an output limit for '{}';"
        " falling back to MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT={}",
        model_ref,
        unknown_default,
    )
    return unknown_default


def _apply_model_limit(
    requested: int | None, limit: int | None, model_ref: str
) -> int | None:
    """Bound the client's ask by the model's own published limit.

    ``requested is None`` -- not falsy -- is what "the client named nothing"
    means. A client that explicitly sends ``max_tokens: 0`` said something, and
    replacing it would answer a different question than the one asked.
    """

    if requested is None:
        return limit
    if limit is None or requested <= limit:
        return requested
    logger.warning(
        "MAX TOKENS CLAMPED: '{}' can emit at most {} output tokens;"
        " sending {} instead of the requested {}",
        model_ref,
        limit,
        limit,
        requested,
    )
    return limit


def _apply_ceiling(
    resolved: int | None, ceiling: int | None, model_ref: str
) -> int | None:
    """Apply the operator's absolute guard, which is unset by default."""

    if resolved is None or ceiling is None or resolved <= ceiling:
        return resolved
    logger.warning(
        "MAX TOKENS CEILING: '{}' is allowed {} output tokens by its own"
        " capability, but MAX_OUTPUT_TOKENS_CEILING caps it at {}",
        model_ref,
        resolved,
        ceiling,
    )
    return ceiling


def _apply_context_headroom(
    resolved: int | None,
    limits: OutputTokenLimits,
    input_tokens: int,
    model_ref: str,
) -> int | None:
    """Bound the budget by what is left of the context window after the prompt.

    1,117 of 7,440 models.dev entries publish ``limit.output ==
    limit.context``; on those, asking for the full output leaves no room for
    the messages. Where the remaining context is already larger than the
    budget -- the usual case -- nothing happens.

    A headroom of zero or less is left alone deliberately. Sending a zero or
    negative ``max_tokens`` turns a prompt that is merely too long into a
    malformed request, and the provider's own error names the real window far
    better than a guess made here can.
    """

    context_length = limits.context_length
    if resolved is None or context_length is None:
        return resolved
    headroom = context_length - input_tokens - limits.context_margin
    if headroom <= 0 or headroom >= resolved:
        return resolved
    logger.warning(
        "MAX TOKENS BOUNDED BY CONTEXT: '{}' has a {}-token context and the"
        " prompt uses {}; sending {} output tokens instead of {}",
        model_ref,
        context_length,
        input_tokens,
        headroom,
        resolved,
    )
    return headroom
