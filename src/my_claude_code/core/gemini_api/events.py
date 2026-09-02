"""Gemini SSE framing and the finish-reason translation.

Google's streaming form is ``?alt=sse``: a bare ``data:`` stream of whole
``GenerateContentResponse`` objects, with no ``event:`` lines and no terminal
sentinel. There is deliberately no ``[DONE]`` frame here -- that is OpenAI's
convention, and a client built on ``@google/genai`` would try to ``JSON.parse``
it.
"""

import json
from typing import Any

GEMINI_SSE_HEADERS: dict[str, str] = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

#: Anthropic ``stop_reason`` -> Gemini ``finishReason``.
#:
#: ``tool_use`` is ``STOP`` on purpose: Gemini does not have a separate finish
#: reason for a turn that called a function. It reports ``STOP`` and puts the
#: ``functionCall`` parts in the candidate, and every Gemini client dispatches
#: on the *parts*, not on the reason. Emitting anything else here would make a
#: well-formed tool turn look like an abnormal stop.
#:
#: ``refusal`` maps to ``SAFETY`` because that is the only member of Google's
#: enum that means "the model declined on content grounds"; ``pause_turn`` has
#: no equivalent at all and reads as an ordinary end of turn.
_FINISH_REASONS: dict[str, str] = {
    "end_turn": "STOP",
    "stop_sequence": "STOP",
    "max_tokens": "MAX_TOKENS",
    "tool_use": "STOP",
    "pause_turn": "STOP",
    "refusal": "SAFETY",
}


def format_gemini_sse_data(data: Any) -> str:
    """Format one Gemini SSE frame."""

    return f"data: {json.dumps(data)}\n\n"


def finish_reason_for(stop_reason: Any) -> str:
    """Translate an Anthropic stop reason into a Gemini finish reason."""

    if isinstance(stop_reason, str) and stop_reason in _FINISH_REASONS:
        return _FINISH_REASONS[stop_reason]
    return "STOP"
