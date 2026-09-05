"""Native Anthropic Messages request serialization for upstream providers."""

from copy import deepcopy
from typing import Any

from loguru import logger

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.settings import configured_default_max_output_tokens
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import (
    MINIMUM_BUDGET_TOKENS,
    ReasoningControl,
    ReasoningPolicy,
)

_INTERNAL_FIELDS = frozenset(
    {
        "original_model",
        "resolved_provider_model",
        "extra_body",
        "betas",
    }
)
_CANONICAL_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "stream",
        "thinking",
    }
)


def build_anthropic_messages_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
) -> dict[str, Any]:
    """Build one native Messages request without exposing FCC-only fields."""
    body = request.model_dump(exclude_none=True)
    for field in _INTERNAL_FIELDS:
        body.pop(field, None)
    body["messages"] = [_native_message(message) for message in request.messages]
    body["stream"] = True
    if body.get("max_tokens") is None:
        # The operator's last resort, reached only when routing never bound
        # this request to a published limit. 0 means "send none at all", which
        # leaves the answer's length to Anthropic's own default.
        configured = configured_default_max_output_tokens()
        if configured is None:
            body.pop("max_tokens", None)
        else:
            body["max_tokens"] = configured
    _apply_reasoning(body, request, reasoning)
    _merge_extra_body(body, request.extra_body)
    return body


def _native_message(message: Message) -> dict[str, Any]:
    role = "user" if message.role == "system" else message.role
    return {
        "role": role,
        "content": _native_content(message.content),
    }


def _native_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    return [
        {
            key: deepcopy(value)
            for key, value in block.model_dump(exclude_none=True).items()
            if key != "reasoning_content"
        }
        for block in content
    ]


def _apply_reasoning(
    body: dict[str, Any],
    request: MessagesRequest,
    policy: ReasoningPolicy,
) -> None:
    if policy.control is ReasoningControl.OFF:
        body.pop("thinking", None)
        return
    if policy.control is ReasoningControl.ADAPTIVE:
        # An explicit adaptive tier overrides whatever the client asked for.
        body["thinking"] = {"type": "adaptive"}
        return
    if not policy.requests_reasoning:
        return

    budget = policy.numeric_budget_tokens
    if budget is not None:
        bounded = _budget_within_max_tokens(body, budget)
        if bounded is None:
            # No legal budget exists inside this allowance. Sending the answer
            # without thinking is the only outcome that is neither a 400 nor a
            # max_tokens the client never asked for.
            body.pop("thinking", None)
            return
        body["thinking"] = {"type": "enabled", "budget_tokens": bounded}
        return

    requested = request.thinking
    if requested is not None and requested.type in {"adaptive", "enabled"}:
        return
    body["thinking"] = {"type": "adaptive"}


def _budget_within_max_tokens(body: dict[str, Any], budget: int) -> int | None:
    """Enforce Anthropic's ``budget_tokens < max_tokens`` on the wire body.

    Asserted here rather than only at gating time because the two numbers are
    still moving after gating: user configuration, per-model overrides and the
    per-model output budget are all applied later, and a violation at *this*
    point is what the provider answers with a 400. The commit boundary is where
    the protocol adapter owns the invariant.

    **The thinking budget is what yields, always.** ``max_tokens`` is never
    touched here. Raising it to ``budget + 1`` was the behaviour before 5.64.0
    and it did so without consulting the model's own published limit, which is
    how a request ends up asking a 16,384-token model for more output than it
    can emit; the last remaining raise -- to 1,025 whenever the allowance could
    not admit Anthropic's documented 1,024-token minimum -- went the same way in
    6.47.0. This function has no access to the routed model's limit, so any
    raise it performs is unbounded by capability by construction, and there is
    no bounded version of it to keep.

    ``None`` comes back for that case instead: an allowance at or below 1,024
    admits no legal budget at all, since the budget must be both at least 1,024
    and strictly smaller than ``max_tokens``. The caller drops ``thinking``
    rather than send a body the API refuses. Reaching it means the allowance
    moved after ``application.reasoning_budget.bound_budget`` reconciled the
    two, which on a routed request it does not: ``bound_budget`` already
    guarantees ``budget <= max_tokens - 1``.
    """

    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        return budget
    if max_tokens > budget:
        return budget
    if max_tokens > MINIMUM_BUDGET_TOKENS:
        return max_tokens - 1
    logger.debug(
        "ANTHROPIC_REQUEST: max_tokens={} admits no thinking budget (Anthropic's"
        " minimum is {} and the budget must stay below max_tokens); sending the"
        " request without thinking rather than raising an allowance the client"
        " set",
        max_tokens,
        MINIMUM_BUDGET_TOKENS,
    )
    return None


def _merge_extra_body(body: dict[str, Any], extra_body: Any) -> None:
    if extra_body in (None, {}):
        return
    if not isinstance(extra_body, dict):
        raise InvalidRequestError(
            "Anthropic Messages extra_body must be an object when provided."
        )
    conflicts = sorted(str(key) for key in extra_body if key in _CANONICAL_FIELDS)
    if conflicts:
        raise InvalidRequestError(
            "Anthropic Messages extra_body cannot override canonical fields: "
            f"{conflicts}."
        )
    body.update({str(key): deepcopy(value) for key, value in extra_body.items()})
