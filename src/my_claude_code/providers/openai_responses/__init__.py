"""The OpenAI Responses API, as a protocol rather than as one backend.

Two providers speak it: ``chatgpt_oauth``, which has since 4.x, and the
OpenCode family, whose gateway serves some of its models here and the rest on
Chat Completions. Everything both of them share -- the Anthropic request to
Responses body conversion and the Responses SSE to Anthropic ledger
translation -- lives here; everything either of them decides for itself (base
URL, credential, ``store``, the output allowance, the cache key) stays with the
provider.
"""

from .conversion import (
    HISTORY_THINKING_OMITTED,
    RESPONSES_DEFAULT_REASONING_EFFORT,
    RESPONSES_DEFAULT_REASONING_SUMMARY,
    RESPONSES_REASONING_REPLAY,
    alias_responses_body_tool_names,
    build_responses_request_body,
    history_thinking_marker,
    responses_tool_call_to_anthropic,
    responses_tool_name_codec,
)
from .streaming import (
    ResponsesStreamConverter,
    iter_responses_sse_events,
    note_responses_event_shape,
)
from .tool_schema_dialect import (
    PERMISSIVE_TOOL_SCHEMA_DIALECT,
    RESPONSES_TOOL_SCHEMA_DIALECT,
    ToolSchemaDialect,
)

__all__ = [
    "HISTORY_THINKING_OMITTED",
    "PERMISSIVE_TOOL_SCHEMA_DIALECT",
    "RESPONSES_DEFAULT_REASONING_EFFORT",
    "RESPONSES_DEFAULT_REASONING_SUMMARY",
    "RESPONSES_REASONING_REPLAY",
    "RESPONSES_TOOL_SCHEMA_DIALECT",
    "ResponsesStreamConverter",
    "ToolSchemaDialect",
    "alias_responses_body_tool_names",
    "build_responses_request_body",
    "history_thinking_marker",
    "iter_responses_sse_events",
    "note_responses_event_shape",
    "responses_tool_call_to_anthropic",
    "responses_tool_name_codec",
]
