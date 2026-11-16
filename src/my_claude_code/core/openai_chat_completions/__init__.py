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
from .tools import ChatWebSearchSpec, extract_chat_web_search_spec
from .web_search_response import (
    build_chat_completion_from_loop,
    build_nested_chat_annotations,
    collect_chat_final_text,
    iter_chat_sse_from_loop,
)

__all__ = [
    "ChatCompletionsConversionError",
    "ChatWebSearchSpec",
    "OpenAIChatCompletionRequest",
    "OpenAIChatCompletionsAdapter",
    "build_chat_completion_from_loop",
    "build_nested_chat_annotations",
    "collect_chat_final_text",
    "extract_chat_web_search_spec",
    "iter_chat_sse_from_loop",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]
