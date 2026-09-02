"""Chat Completions SSE framing and the stop-reason translation."""

import json
from collections.abc import Mapping
from typing import Any

OPENAI_CHAT_SSE_HEADERS: dict[str, str] = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

DONE_FRAME = "data: [DONE]\n\n"

#: Anthropic ``stop_reason`` -> OpenAI ``finish_reason``. ``pause_turn`` and
#: ``refusal`` have no OpenAI equivalent and read as an ordinary end of turn to
#: a client that only knows the four documented values.
_FINISH_REASONS: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "stop",
}


def format_chat_sse_data(data: Mapping[str, Any]) -> str:
    """Format one Chat Completions SSE frame.

    Unnamed on purpose: the Chat Completions stream is a bare ``data:`` stream
    with no ``event:`` lines, and clients that split on them see nothing.
    """
    return f"data: {json.dumps(data)}\n\n"


def finish_reason_for(stop_reason: Any, *, emitted_tool_calls: bool) -> str:
    """Translate an Anthropic stop reason into an OpenAI finish reason.

    A stream that carried tool calls and ended without a stop reason still
    finished for the tool calls, and a client that dispatches on
    ``finish_reason`` would otherwise never run them.
    """
    if isinstance(stop_reason, str) and stop_reason in _FINISH_REASONS:
        return _FINISH_REASONS[stop_reason]
    return "tool_calls" if emitted_tool_calls else "stop"
