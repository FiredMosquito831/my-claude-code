"""Native Anthropic Messages request serialization for upstream providers."""

from copy import deepcopy
from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import ReasoningControl, ReasoningPolicy

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
        max_tokens = body.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens <= budget:
            body["max_tokens"] = budget + 1
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return

    requested = request.thinking
    if requested is not None and requested.type in {"adaptive", "enabled"}:
        return
    body["thinking"] = {"type": "adaptive"}


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
