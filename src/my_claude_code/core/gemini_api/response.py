"""Assemble one ``GenerateContentResponse`` from a complete Anthropic message."""

from collections.abc import Mapping, Sequence
from typing import Any

from .events import finish_reason_for
from .usage import GeminiUsageLedger


def parts_from_anthropic_blocks(
    blocks: Sequence[Any],
    *,
    usage: GeminiUsageLedger,
    include_thoughts: bool,
) -> list[dict[str, Any]]:
    """Convert Anthropic content blocks into Gemini ``Content.parts``."""
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            if text := _text(block.get("text")):
                parts.append({"text": text})
        elif block_type == "thinking":
            if thought := _text(block.get("thinking")):
                usage.add_thought_text(thought)
                if include_thoughts:
                    parts.append({"text": thought, "thought": True})
        elif block_type == "tool_use":
            parts.append(function_call_part(block))
    return parts


def generate_content_response_from_anthropic_message(
    message: Mapping[str, Any],
    *,
    model: str,
    response_id: str,
    include_thoughts: bool,
) -> dict[str, Any]:
    """Convert an assembled Anthropic Messages body into a Gemini response.

    ``:generateContent`` is served by assembling the same internal SSE stream
    every other surface runs on, so this reads the *result* of that assembly
    rather than duplicating the translation. The one thing it must not do is
    invent a difference: a client that switches to ``:streamGenerateContent``
    has to see the same text, the same function calls and the same finish
    reason.
    """

    usage = GeminiUsageLedger()
    parts = parts_from_anthropic_blocks(
        message.get("content") or (),
        usage=usage,
        include_thoughts=include_thoughts,
    )
    usage.absorb(message.get("usage"))

    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "index": 0,
                "finishReason": finish_reason_for(message.get("stop_reason")),
            }
        ],
        "usageMetadata": usage.payload(),
        "modelVersion": model,
        "responseId": response_id,
    }


def thought_signature_from_extra_content(extra_content: Any) -> str | None:
    """Extract Google's ``thought_signature`` from ``extra_content`` if present."""
    if not isinstance(extra_content, Mapping):
        return None
    google = extra_content.get("google")
    if not isinstance(google, Mapping):
        return None
    signature = google.get("thought_signature")
    return signature if isinstance(signature, str) and signature else None


def function_call_part(block: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one Anthropic ``tool_use`` block into a Gemini ``functionCall`` part."""
    arguments = block.get("input")
    call: dict[str, Any] = {
        "name": _text(block.get("name")),
        "args": dict(arguments) if isinstance(arguments, Mapping) else {},
    }
    call_id = block.get("id")
    if isinstance(call_id, str) and call_id:
        call["id"] = call_id
    part: dict[str, Any] = {"functionCall": call}
    if sig := thought_signature_from_extra_content(block.get("extra_content")):
        part["thoughtSignature"] = sig
    return part


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
