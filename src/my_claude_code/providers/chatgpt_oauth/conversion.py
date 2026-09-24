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
from my_claude_code.providers.openai_responses import (
    RESPONSES_TOOL_SCHEMA_DIALECT,
    ToolSchemaDialect,
)
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
    "CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT",
    "build_chatgpt_oauth_request_body",
    "chatgpt_tool_call_to_anthropic",
]


#: What this backend's validator refuses in a tool schema. Declared here, where
#: this backend's other body decisions are, and declared as the Responses
#: default on purpose: it is the host that refused ``pattern`` lookaround 216
#: times on 2026-09-20, and it is the validator that default was measured on.
CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT = RESPONSES_TOOL_SCHEMA_DIALECT


def build_chatgpt_oauth_request_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
    default_max_tokens: int | None = None,
    tool_schema_dialect: ToolSchemaDialect = CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT,
    wire_notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a ChatGPT Responses API request body from an Anthropic request.

    ``wire_notes`` receives what the declared schema dialect took out of the
    client's tools, for the sender to record beside the body.
    """
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
        tool_schema_dialect=tool_schema_dialect,
        wire_notes=wire_notes,
    )
