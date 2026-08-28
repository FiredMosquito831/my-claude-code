"""Native Anthropic Messages request serialization for upstream providers."""

from copy import deepcopy
from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
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
    body.setdefault("max_tokens", ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS)
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
        body["thinking"] = {
            "type": "enabled",
            "budget_tokens": _budget_within_max_tokens(body, budget),
        }
        return

    requested = request.thinking
    if requested is not None and requested.type in {"adaptive", "enabled"}:
        return
    body["thinking"] = {"type": "adaptive"}


def _budget_within_max_tokens(body: dict[str, Any], budget: int) -> int:
    """Enforce Anthropic's ``budget_tokens < max_tokens`` on the wire body.

    Asserted here rather than only at gating time because the two numbers are
    still moving after gating: user configuration, per-model overrides and the
    per-model output budget are all applied later, and a violation at *this*
    point is what the provider answers with a 400. The commit boundary is where
    the protocol adapter owns the invariant.

    The thinking budget is what yields. Raising ``max_tokens`` to ``budget + 1``
    was the previous behaviour and it did so without consulting the model's own
    published limit at all, which is how a request ends up asking a
    16,384-token model for more output than it can emit. The single exception
    is a ``max_tokens`` too small to admit Anthropic's documented 1,024-token
    minimum: no legal budget exists below it, so the allowance is raised to the
    smallest value that admits one rather than sending a budget the API
    rejects.
    """

    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        return budget
    if max_tokens > budget:
        return budget
    if max_tokens > MINIMUM_BUDGET_TOKENS:
        return max_tokens - 1
    body["max_tokens"] = MINIMUM_BUDGET_TOKENS + 1
    return MINIMUM_BUDGET_TOKENS


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
