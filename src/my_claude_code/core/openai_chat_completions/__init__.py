"""OpenAI Chat Completions protocol adapter.

The oldest and most widely implemented OpenAI surface, and the one every
"OpenAI-compatible" IDE plugin and coding agent speaks. It is served through
exactly the same router, executor, fallback chain and request log as
``/v1/messages`` and ``/v1/responses``: this package only translates.
"""

from .adapter import OpenAIChatCompletionsAdapter
from .errors import (
    ChatCompletionsConversionError,
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from .models import OpenAIChatCompletionRequest

__all__ = [
    "ChatCompletionsConversionError",
    "OpenAIChatCompletionRequest",
    "OpenAIChatCompletionsAdapter",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]
