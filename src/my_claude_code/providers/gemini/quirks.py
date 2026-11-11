"""Gemini request-body quirks for the shared OpenAI-chat provider."""

from copy import deepcopy
from typing import Any, cast

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy

GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"

#: The ``params.wire`` key recording that the skip-validator sentinel was
#: written into a tool call (8.19.0). Counts only -- the rule
#: ``foreign_thinking_dropped`` and ``history_thinking_omitted`` keep -- and
#: deliberately not spelled ``thinking_*`` or ``reasoning_*``: the wire
#: recorder reads any key with those prefixes as a reasoning *instruction*
#: (``is_reasoning_key``), and a note about a signature would then count as
#: reasoning sent.
#:
#: Why it exists: the sentinel is an escape hatch, not an implementation.
#: Google validates a ``thoughtSignature`` on the current turn's first tool
#: call, and MCC writes the sentinel whenever it has none to replay -- which
#: is every tool loop that spans a restart, and every loop whose signature was
#: never captured. Until this note existed an operator could not tell a
#: working Gemini tool loop from one that was silently bypassing validation.
#: The note changes no body byte; it only makes the degradation countable.
GEMINI_THOUGHT_SIGNATURE_SKIPPED = "gemini_thought_signature_skipped"


def apply_gemini_request_quirks(
    body: dict[str, Any],
    request_data: MessagesRequest,
    reasoning: ReasoningPolicy,
    *,
    tool_call_extra_content_by_id: dict[str, dict[str, Any]] | None = None,
    wire_notes: dict[str, Any] | None = None,
) -> None:
    """Apply Google-specific request extensions after common OpenAI conversion.

    ``wire_notes`` is a caller's mapping to receive what this build recorded
    about the body it just built -- ``gemini_thought_signature_skipped``,
    counts only. Left untouched when nothing was skipped, so every other
    request's recorded params are exactly what they were.
    """
    extra_body: dict[str, Any] = {}
    request_extra = request_data.extra_body
    if isinstance(request_extra, dict):
        extra_body.update(deepcopy(request_extra))

    if reasoning.requests_reasoning:
        _apply_thinking_config(extra_body)

    if extra_body:
        body["extra_body"] = extra_body

    _apply_gemini_tool_call_signatures(
        body,
        tool_call_extra_content_by_id=tool_call_extra_content_by_id,
        wire_notes=wire_notes,
    )


def _ensure_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    nested: dict[str, Any] = {}
    container[key] = nested
    return nested


def _apply_thinking_config(extra_body: dict[str, Any]) -> None:
    # OpenAI's SDK merges its ``extra_body`` argument into the request JSON.
    # Google expects its extension fields under a literal JSON ``extra_body`` key.
    literal_extra_body = _ensure_dict(extra_body, "extra_body")
    google_section = _ensure_dict(literal_extra_body, "google")
    thinking_cfg = _ensure_dict(google_section, "thinking_config")
    thinking_cfg.setdefault("include_thoughts", True)


def _thought_signature_from_extra_content(extra_content: Any) -> str | None:
    if not isinstance(extra_content, dict):
        return None
    google = extra_content.get("google")
    if not isinstance(google, dict):
        return None
    signature = google.get("thought_signature")
    return signature if isinstance(signature, str) and signature else None


def _tool_call_thought_signature(tool_call: dict[str, Any]) -> str | None:
    return _thought_signature_from_extra_content(tool_call.get("extra_content"))


def _set_tool_call_thought_signature(tool_call: dict[str, Any], signature: str) -> None:
    extra_content = tool_call.get("extra_content")
    if not isinstance(extra_content, dict):
        extra_content = {}
        tool_call["extra_content"] = extra_content
    google = extra_content.get("google")
    if not isinstance(google, dict):
        google = {}
        extra_content["google"] = google
    google["thought_signature"] = signature


def _message_has_standard_user_content(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and bool(part["text"].strip())
            for part in content
        )
    return False


def _current_turn_start_index(messages: list[Any]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and _message_has_standard_user_content(message):
            return index
    return -1


def _apply_cached_tool_call_signatures(
    messages: list[Any], tool_call_extra_content_by_id: dict[str, dict[str, Any]]
) -> None:
    if not tool_call_extra_content_by_id:
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict) or _tool_call_thought_signature(
                tool_call
            ):
                continue
            tool_call_id = tool_call.get("id")
            if tool_call_id is None:
                continue
            cached_extra_content = tool_call_extra_content_by_id.get(str(tool_call_id))
            if not cached_extra_content:
                continue
            cached_signature = _thought_signature_from_extra_content(
                cached_extra_content
            )
            if cached_signature:
                tool_call["extra_content"] = deepcopy(cached_extra_content)


def _apply_missing_current_turn_signatures(messages: list[Any]) -> int:
    """Write the sentinel where a signature is missing; return how many.

    The count is the note's whole content: one sentinel per assistant turn
    with tool calls and no captured signature, so a loop that bypasses
    validation on every turn reads as a number that climbs, and a loop that
    replays real signatures reads as nothing at all.
    """

    written = 0
    start_index = _current_turn_start_index(messages)
    for message in messages[start_index + 1 :]:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        first_tool_call = tool_calls[0]
        if not isinstance(first_tool_call, dict):
            continue
        if _tool_call_thought_signature(first_tool_call):
            continue
        _set_tool_call_thought_signature(
            first_tool_call, GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR
        )
        written += 1
    return written


def thought_signature_skipped_note(count: int) -> dict[str, str]:
    """The ``gemini_thought_signature_skipped`` record for ``count`` writes.

    ``{}`` for a count of zero, which keeps every other request's recorded
    params exactly as they were -- the rule ``empty_system_messages_note``
    states. Counts only: no signature, no tool name, no tool-call id.
    """

    if count <= 0:
        return {}
    noun = "tool call" if count == 1 else "tool calls"
    return {
        GEMINI_THOUGHT_SIGNATURE_SKIPPED: (
            f"wrote the skip-validator sentinel into {count} {noun} with no "
            "captured thoughtSignature; Google will not validate them"
        )
    }


def _apply_gemini_tool_call_signatures(
    body: dict[str, Any],
    *,
    tool_call_extra_content_by_id: dict[str, dict[str, Any]] | None,
    wire_notes: dict[str, Any] | None = None,
) -> None:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    _apply_cached_tool_call_signatures(messages, tool_call_extra_content_by_id or {})
    skipped = _apply_missing_current_turn_signatures(messages)
    if wire_notes is not None:
        wire_notes.update(thought_signature_skipped_note(skipped))
