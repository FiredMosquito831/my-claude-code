"""Assemble one ``GenerateContentResponse`` from a complete Anthropic message."""

from collections.abc import Mapping
from typing import Any

from .events import finish_reason_for
from .usage import GeminiUsageLedger


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

    parts: list[dict[str, Any]] = []
    usage = GeminiUsageLedger()

    for block in message.get("content") or ():
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
            parts.append(_function_call(block))

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


def _function_call(block: Mapping[str, Any]) -> dict[str, Any]:
    arguments = block.get("input")
    return {
        "functionCall": {
            "name": _text(block.get("name")),
            "args": dict(arguments) if isinstance(arguments, Mapping) else {},
        }
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
