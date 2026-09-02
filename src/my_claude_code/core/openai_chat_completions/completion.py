"""Assemble one ``chat.completion`` object from a complete Anthropic message."""

import json
import time
from collections.abc import Mapping
from typing import Any

from .events import finish_reason_for
from .ids import new_tool_call_id
from .models import OpenAIChatCompletionRequest
from .usage import ChatUsageLedger


def chat_completion_from_anthropic_message(
    message: Mapping[str, Any],
    request: OpenAIChatCompletionRequest,
    *,
    completion_id: str,
) -> dict[str, Any]:
    """Convert an assembled Anthropic Messages body into a chat completion.

    Non-streaming is served by assembling the same internal SSE stream every
    other surface runs on, so this reads the *result* of that assembly rather
    than duplicating the translation. The one thing it must not do is invent a
    difference: a client that flips ``stream`` has to see the same text, the
    same tool calls and the same finish reason.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in message.get("content") or ():
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(_text(block.get("text")))
        elif block_type == "thinking":
            reasoning_parts.append(_text(block.get("thinking")))
        elif block_type == "tool_use":
            tool_calls.append(_tool_call(block))

    usage = ChatUsageLedger()
    usage.absorb(message.get("usage"))
    for reasoning in reasoning_parts:
        usage.add_reasoning_text(reasoning)

    content = "".join(text_parts)
    choice_message: dict[str, Any] = {
        "role": "assistant",
        # ``null`` rather than ``""`` when the turn was only tool calls: that
        # is what OpenAI itself sends, and clients branch on it.
        "content": content if content or not tool_calls else None,
    }
    if reasoning := "".join(reasoning_parts):
        choice_message["reasoning_content"] = reasoning
        choice_message["reasoning"] = reasoning
    if tool_calls:
        choice_message["tool_calls"] = tool_calls

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": choice_message,
                "logprobs": None,
                "finish_reason": finish_reason_for(
                    message.get("stop_reason"),
                    emitted_tool_calls=bool(tool_calls),
                ),
            }
        ],
        "usage": usage.payload(),
    }


def _tool_call(block: Mapping[str, Any]) -> dict[str, Any]:
    arguments = block.get("input")
    return {
        "id": _text(block.get("id")) or new_tool_call_id(),
        "type": "function",
        "function": {
            "name": _text(block.get("name")),
            "arguments": json.dumps(
                arguments if isinstance(arguments, Mapping) else {},
                separators=(",", ":"),
            ),
        },
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)
