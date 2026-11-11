"""Google thought-signature replay for OpenAI-compatible tool calls."""

from copy import deepcopy
from typing import Any

GOOGLE_SKIP_THOUGHT_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"

#: The ``params.wire`` key recording that the skip-validator sentinel was
#: written into a tool call (8.19.0). The same key and the same sentence as
#: ``providers/gemini/quirks.py``'s -- one fact, one spelling, whichever of the
#: two Google doors produced it. De-duplicating the two modules is a separate
#: refactor (spec §2 N7) and deliberately not done here.
GOOGLE_THOUGHT_SIGNATURE_SKIPPED = "gemini_thought_signature_skipped"


def apply_google_thought_signatures(
    body: dict[str, Any],
    *,
    tool_call_extra_content_by_id: dict[str, dict[str, Any]] | None = None,
    wire_notes: dict[str, Any] | None = None,
) -> None:
    """Restore Google tool-call signatures required for conversation replay.

    ``wire_notes`` is a caller's mapping to receive what this build recorded
    about the body it just built -- ``gemini_thought_signature_skipped``,
    counts only. Left untouched when nothing was skipped.
    """

    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    _apply_cached_tool_call_signatures(messages, tool_call_extra_content_by_id or {})
    skipped = _apply_missing_current_turn_signatures(messages)
    if wire_notes is not None:
        wire_notes.update(thought_signature_skipped_note(skipped))


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
    """Write the sentinel where a signature is missing; return how many."""

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
            first_tool_call, GOOGLE_SKIP_THOUGHT_SIGNATURE_VALIDATOR
        )
        written += 1
    return written


def thought_signature_skipped_note(count: int) -> dict[str, str]:
    """The ``gemini_thought_signature_skipped`` record for ``count`` writes.

    Byte-identical to ``providers/gemini/quirks.py``'s, on purpose: an
    operator reading a request detail must not be able to tell which of the
    two Google doors produced the line, because the fact is the same one.
    ``{}`` for a count of zero. Counts only: no signature, no tool name, no
    tool-call id.
    """

    if count <= 0:
        return {}
    noun = "tool call" if count == 1 else "tool calls"
    return {
        GOOGLE_THOUGHT_SIGNATURE_SKIPPED: (
            f"wrote the skip-validator sentinel into {count} {noun} with no "
            "captured thoughtSignature; Google will not validate them"
        )
    }
