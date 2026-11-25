"""Responses stream error mapping."""

from typing import Any

from my_claude_code.core.openai_common import openai_error_from_anthropic_error

__all__ = [
    "openai_error_from_anthropic_error",
    "replay_unsafe_function_call_error",
]


def replay_unsafe_function_call_error() -> dict[str, Any]:
    return {
        "message": (
            "Upstream function_call arguments were not a valid JSON object; "
            "refusing to emit replay-unsafe Responses output."
        ),
        "type": "api_error",
        "param": None,
        "code": None,
    }
