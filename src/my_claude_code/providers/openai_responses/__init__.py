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
    RESPONSES_DEFAULT_REASONING_EFFORT,
    RESPONSES_DEFAULT_REASONING_SUMMARY,
    build_responses_request_body,
    responses_tool_call_to_anthropic,
)
from .streaming import (
    ResponsesStreamConverter,
    iter_responses_sse_events,
    note_responses_event_shape,
)

__all__ = [
    "RESPONSES_DEFAULT_REASONING_EFFORT",
    "RESPONSES_DEFAULT_REASONING_SUMMARY",
    "ResponsesStreamConverter",
    "build_responses_request_body",
    "iter_responses_sse_events",
    "note_responses_event_shape",
    "responses_tool_call_to_anthropic",
]
