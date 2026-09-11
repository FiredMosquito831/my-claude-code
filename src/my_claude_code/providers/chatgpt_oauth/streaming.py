"""The Responses API stream translation, under the names this provider uses.

The translation itself moved to
:mod:`my_claude_code.providers.openai_responses.streaming` in 6.74.0, because
it was never about ChatGPT: it reads the Responses protocol, and OpenCode Zen
serves two of its free models on exactly the same frames. Nothing about the
behaviour changed with the move -- the names here are the same objects, and
this module exists so the callers and tests that learned them keep working.
"""

from my_claude_code.providers.openai_responses.streaming import (
    ResponsesStreamConverter as ChatGPTOAuthStreamConverter,
)
from my_claude_code.providers.openai_responses.streaming import (
    iter_responses_sse_events as iter_chatgpt_oauth_sse_events,
)
from my_claude_code.providers.openai_responses.streaming import (
    note_responses_event_shape,
)

__all__ = [
    "ChatGPTOAuthStreamConverter",
    "iter_chatgpt_oauth_sse_events",
    "note_responses_event_shape",
]
