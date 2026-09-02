"""Wire primitives shared by every OpenAI-compatible ingress protocol.

MCC serves two OpenAI-shaped surfaces -- ``POST /v1/responses`` and
``POST /v1/chat/completions`` -- and they agree on more than they differ on:
the same error envelope, the same failure-kind to ``error.type`` mapping, the
same best-effort token estimate for reasoning usage. Those live here rather
than in either protocol package, so that neither adapter has to import the
other's internals to say the same thing.
"""

from .anthropic_sse import AnthropicSseEvent, iter_sse_events, parse_sse_event
from .errors import (
    openai_error_from_failure,
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from .usage import estimate_text_tokens

__all__ = [
    "AnthropicSseEvent",
    "estimate_text_tokens",
    "iter_sse_events",
    "openai_error_from_failure",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
    "parse_sse_event",
]
