"""Anthropic SSE parsing used by the Responses stream adapter.

The reader moved to ``core.openai_common.anthropic_sse`` when Chat Completions
arrived and needed to consume the identical upstream stream. It stays
importable from here because that is where this package's stream adapter has
always read it from.
"""

from my_claude_code.core.openai_common.anthropic_sse import (
    AnthropicSseEvent,
    iter_sse_events,
    parse_sse_event,
)

__all__ = ["AnthropicSseEvent", "iter_sse_events", "parse_sse_event"]
