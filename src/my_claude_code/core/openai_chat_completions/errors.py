"""Errors for OpenAI Chat Completions compatibility.

The envelope and the failure-kind mapping are shared with the Responses
surface and live in ``core.openai_common.errors``; only the conversion error
is specific to this protocol.
"""

from my_claude_code.core.openai_common import (
    openai_error_from_failure,
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)

__all__ = [
    "ChatCompletionsConversionError",
    "openai_error_from_failure",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]


class ChatCompletionsConversionError(ValueError):
    """Raised when a Chat Completions request cannot be converted.

    Carries an optional ``param``: the OpenAI SDK surfaces ``error.param`` in
    the exception a client catches, and "which field" is the whole content of a
    400 to somebody wiring a new client up.
    """

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param
