"""The Responses API request build, with this backend's own choices filled in.

The translation moved to
:mod:`my_claude_code.providers.openai_responses.conversion` in 6.74.0 -- it was
never about ChatGPT, it reads the Responses protocol, and OpenCode Zen serves
two of its free models on the same wire. What stayed here is the part that
genuinely *is* about this backend: the four decisions below, and the refusal of
a caller ``extra_body``.

The body this produces is unchanged, key for key and in the same order, from
the one 6.73.2 sent. That is asserted rather than asserted-to:
``tests/providers/test_chatgpt_oauth.py`` builds it and compares field by
field, and the wire baseline compares the serialised bytes.
"""

from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_responses.conversion import (
    RESPONSES_DEFAULT_REASONING_EFFORT as CHATGPT_DEFAULT_REASONING_EFFORT,
)
from my_claude_code.providers.openai_responses.conversion import (
    RESPONSES_DEFAULT_REASONING_SUMMARY as CHATGPT_DEFAULT_REASONING_SUMMARY,
)
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)
from my_claude_code.providers.openai_responses.conversion import (
    responses_tool_call_to_anthropic as chatgpt_tool_call_to_anthropic,
)

__all__ = [
    "CHATGPT_DEFAULT_REASONING_EFFORT",
    "CHATGPT_DEFAULT_REASONING_SUMMARY",
    "build_chatgpt_oauth_request_body",
    "chatgpt_tool_call_to_anthropic",
]


def build_chatgpt_oauth_request_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
    default_max_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a ChatGPT Responses API request body from an Anthropic request."""
    if request.extra_body:
        raise InvalidRequestError(
            "ChatGPT OAuth provider does not support caller extra_body on requests."
        )
    # OpenCode's codex plugin clears maxOutputTokens to match the Codex CLI:
    # the ChatGPT/Codex Responses endpoint behaves best when the caller does
    # not impose an explicit output limit. ``default_max_tokens`` is kept in
    # the signature for backward compatibility with existing callers.
    _ = default_max_tokens
    return build_responses_request_body(
        request,
        reasoning=reasoning,
        store=False,
        stream=True,
        parallel_tool_calls=False,
        max_output_tokens=None,
        prompt_cache_key=None,
        include_encrypted_reasoning=True,
    )
