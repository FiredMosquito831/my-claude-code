"""Native Anthropic Messages request serialization for upstream providers."""

import json
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

#: The ``params.wire`` key recording that thinking another model wrote was
#: left out of the body sent to an Anthropic-protocol host. Counts only, never
#: text -- the rule ``history_thinking_omitted`` keeps on the Responses
#: surface. Deliberately not spelled ``thinking_*``: the wire recorder reads
#: any key with that prefix as a reasoning *instruction* (``is_reasoning_key``),
#: and a note about the history would then count as reasoning sent.
FOREIGN_THINKING_DROPPED = "foreign_thinking_dropped"


def build_anthropic_messages_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
    wire_notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one native Messages request without exposing FCC-only fields.

    ``wire_notes`` is a caller's mapping to receive what this build took out
    of the client's history -- ``foreign_thinking_dropped``, counts only -- so
    the sender records it beside the body it describes. Left untouched when
    nothing was removed.
    """
    body = request.model_dump(exclude_none=True)
    for field in _INTERNAL_FIELDS:
        body.pop(field, None)
    messages, dropped_blocks, dropped_chars = drop_foreign_thinking(
        [_native_message(message) for message in request.messages]
    )
    body["messages"] = messages
    if dropped_blocks and wire_notes is not None:
        noun = "block" if dropped_blocks == 1 else "blocks"
        wire_notes[FOREIGN_THINKING_DROPPED] = (
            f"dropped {dropped_blocks} thinking {noun} ({dropped_chars:,} chars) "
            "made by other models from earlier turns; Anthropic accepts only its "
            "own signed thinking"
        )
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


def drop_foreign_thinking(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Drop, from assistant turns, the thinking Anthropic did not write.

    Returns ``(messages, dropped_blocks, dropped_chars)``. Two kinds of block
    are provably not Anthropic's, and Anthropic answers a 400 for either:

    * a ``thinking`` block whose ``signature`` is absent, not a string, or
      blank. Anthropic signs every thinking block it mints; MCC opens the
      thinking of every other model with no signature at all, and Claude Code
      replays that as ``signature: ""``. A stream cut before its
      ``signature_delta`` lands here too, and is refused just the same;
    * a ``redacted_thinking`` block whose ``data`` is a JSON object or array.
      That is MCC's own wrapper for an OpenRouter-dialect reasoning detail;
      Anthropic's ``data`` is opaque ciphertext and never parses as one.

    Everything else is kept in its order, every non-blank signature included:
    a signature that is not Anthropic's cannot be told from one that is, and
    an unclassifiable block is kept. User turns are never read. An assistant
    turn left with no blocks is omitted -- it held only thinking, so no
    ``tool_result`` can reference it, and the Messages API combines the two
    turns either side of it into one.

    A pure function of the history, so the trimmed prefix is the same on every
    later turn and the prompt cache stays byte-stable. When nothing matches,
    the very list it was given comes back, so the body is byte-identical.
    """

    kept: list[dict[str, Any]] | None = None
    dropped_blocks = 0
    dropped_chars = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        foreign: list[Any] = []
        remaining: list[Any] = []
        if message.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                (foreign if _is_foreign_thinking(block) else remaining).append(block)
        if not foreign:
            if kept is not None:
                kept.append(message)
            continue
        if kept is None:
            kept = list(messages[:index])
        dropped_blocks += len(foreign)
        dropped_chars += sum(_thinking_chars(block) for block in foreign)
        if remaining:
            kept.append({**message, "content": remaining})
    if kept is None:
        return messages, 0, 0
    return kept, dropped_blocks, dropped_chars


def _is_foreign_thinking(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    if kind == "thinking":
        signature = block.get("signature")
        return not isinstance(signature, str) or not signature.strip()
    if kind == "redacted_thinking":
        return _is_json_container(block.get("data"))
    return False


def _is_json_container(data: Any) -> bool:
    """Whether ``data`` is a JSON object or array -- never Anthropic ciphertext."""

    if not isinstance(data, str) or data.lstrip()[:1] not in {"{", "["}:
        return False
    try:
        parsed = json.loads(data)
    except ValueError, RecursionError:
        return False
    return isinstance(parsed, dict | list)


def _thinking_chars(block: dict[str, Any]) -> int:
    text = block.get("thinking" if block.get("type") == "thinking" else "data")
    return len(text) if isinstance(text, str) else 0


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
