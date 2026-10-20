"""A short 400 for a body a model door cannot accept, never an echo of it.

RESUMED-SESSION-422 (spec ``INVESTIGATION-RESUMED-SESSION-422.md`` §1.3, B1/B3,
decisions of 2026-10-20 16:03). FastAPI answers a body that does not fit the
request model with ``422 {"detail": [...]}``, and every item of that list
carries ``input`` -- the exact value that failed. For a conversation that is
the conversation: a resumed Claude Code session got 190,145 characters of its
own text back, on screen and in its transcript.

The model doors (``/v1/messages``, ``/v1/messages/count_tokens``,
``/v1/chat/completions``, ``/v1/responses``) answer instead the way the vendor
they speak for answers a malformed body -- HTTP 400 ``invalid_request_error``
in that vendor's envelope -- with one sentence naming each failing field and
pydantic's reason for it:

    messages.1.content.3: Input tag 'compaction' found using 'type' does not
    match any of the expected tags: 'text', 'image', ...

That wording is pydantic's own, unchanged: Claude Code 2.1.304 matches
``Input tag 'tool_(addition|removal)'`` in a 400 or 422 to fall back by itself
for a block type a server does not know. Nothing of the input is repeated: the
``input`` and ``ctx`` of each error are never serialised, a long input string
that a reason happens to quote is replaced by its length, and the sentence is
bounded (:data:`MAX_REASONS` fields, :data:`MAX_REASON_CHARS` each).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.core.anthropic import anthropic_error_payload
from my_claude_code.core.openai_common import openai_error_payload

from .wire_surfaces import is_openai_shaped, wire_api_for_path

#: The status Anthropic and OpenAI document for a malformed request body.
MODEL_DOOR_VALIDATION_STATUS = 400
#: Fields named in the sentence; the rest are counted ("and N more").
MAX_REASONS = 3
#: One reason's length cap (pydantic's tag list for the Messages content
#: union is ~300 characters, and is kept whole).
MAX_REASON_CHARS = 400
#: One path segment's length cap.
MAX_SEGMENT_CHARS = 64
#: An input string at least this long is never repeated inside a reason.
_ECHO_MIN_CHARS = 32
#: Pydantic names the member a smart union tried in ``loc`` ("str" for
#: ``str | list[...]``); a member that is a generic type carries brackets
#: ("list[tagged-union[...]]"). Neither is a field of the request.
_UNION_MEMBER_LABELS = frozenset({"str", "int", "float", "bool", "bytes", "none"})


@dataclass(frozen=True, slots=True)
class ValidationReason:
    """One failing field of a request body, without its value."""

    path: str
    reason: str


def describe_validation_errors(
    errors: Sequence[Mapping[str, Any]],
) -> list[ValidationReason]:
    """Turn pydantic's errors into one reason per failing field, in order.

    Union members that failed at the same field are one field ("a valid string
    or a valid list"), and a field whose deeper part also failed is reported
    only by that deeper part -- for ``content: str | list[block]`` an unknown
    block is ``content.3``, not also "``content`` should be a string".
    """
    by_path: dict[str, list[str]] = {}
    for error in errors:
        path = _error_path(error)
        reasons = by_path.setdefault(path, [])
        reason = _error_reason(error)
        if reason not in reasons:
            reasons.append(reason)
    paths = list(by_path)
    return [
        ValidationReason(path=path, reason=" or ".join(by_path[path]))
        for path in paths
        if not any(other.startswith(f"{path}.") for other in paths)
    ]


def validation_error_message(reasons: Sequence[ValidationReason]) -> str:
    """The one sentence a model door answers with."""
    if not reasons:
        return "body: the request body is not valid"
    shown = [f"{item.path}: {item.reason}" for item in reasons[:MAX_REASONS]]
    hidden = len(reasons) - MAX_REASONS
    if hidden > 0:
        shown.append(f"and {hidden} more")
    return "; ".join(shown)


def model_door_validation_payload(
    path: str, reasons: Sequence[ValidationReason], *, request_id: str | None
) -> dict[str, Any]:
    """The 400 body for ``path``, in the envelope of the vendor it speaks for."""
    message = validation_error_message(reasons)
    if is_openai_shaped(wire_api_for_path(path)):
        return openai_error_payload(
            message=message,
            error_type="invalid_request_error",
            param=reasons[0].path if reasons else None,
        )
    return anthropic_error_payload(
        error_type="invalid_request_error",
        message=message,
        request_id=request_id,
    )


def _error_path(error: Mapping[str, Any]) -> str:
    if error.get("type") == "json_invalid":
        # The location is a character offset into text that is not JSON.
        return "body"
    loc = list(error.get("loc") or ())
    if loc and loc[0] == "body":
        loc = loc[1:]
    parts: list[str] = []
    for segment in loc:
        if isinstance(segment, int):
            parts.append(str(segment))
            continue
        text = str(segment)
        if "[" in text or text in _UNION_MEMBER_LABELS:
            continue
        parts.append(_bounded(text, MAX_SEGMENT_CHARS))
    return ".".join(parts) or "body"


def _error_reason(error: Mapping[str, Any]) -> str:
    reason = str(error.get("msg") or "is not valid")
    value = error.get("input")
    if isinstance(value, str) and len(value) >= _ECHO_MIN_CHARS and value in reason:
        reason = reason.replace(value, f"<{len(value)} characters>")
    ctx = error.get("ctx")
    tag = ctx.get("tag") if isinstance(ctx, Mapping) else None
    if isinstance(tag, str) and len(tag) > MAX_SEGMENT_CHARS:
        # An unknown block's ``type`` is the client's text; a sane one is a
        # short identifier and is kept whole, so Claude Code can match it.
        reason = reason.replace(tag, _bounded(tag, MAX_SEGMENT_CHARS))
    return _bounded(reason, MAX_REASON_CHARS)


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


__all__ = [
    "MAX_REASONS",
    "MAX_REASON_CHARS",
    "MODEL_DOOR_VALIDATION_STATUS",
    "ValidationReason",
    "describe_validation_errors",
    "model_door_validation_payload",
    "validation_error_message",
]
