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

from my_claude_code.application.catalogues.codex import CODEX_BASE_INSTRUCTIONS
from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import CHATGPT_OAUTH_SERVICE_TIER_ALIASES
from my_claude_code.config.settings import (
    configured_chatgpt_oauth_service_tier,
    configured_reasoning_adaptive,
    configured_responses_parallel_tool_calls,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import ReplayOrigin
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
)
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
    build_responses_passthrough_body,
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
    tool_name_max_length: int | None = None,
    origin: ReplayOrigin | None = None,
    plaintext_replay: bool = False,
    prompt_cache_key: str | None = None,
    unspecified_effort: str | None = None,
    drop_reasoning_items: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    """Build a ChatGPT Responses API request body from an Anthropic request.

    ``wire_notes`` receives what the declared schema dialect took out of the
    client's tools, for the sender to record beside the body.

    ``tool_name_max_length`` is the ceiling this backend has *stated* in a
    rejection and MCC learned (7.59.0); ``None`` -- nothing declared, nothing
    learned -- sends every name exactly as the client wrote it, which is
    every byte this function built before.

    ``origin`` is this backend as five strings (8.16.0), or ``None`` -- which is
    what the operator's ``RESPONSES_CARRIER_REPLAY`` being off produces, and is
    what keeps this body byte-identical to every release before 8.16.0. Present,
    the history's carriers are decoded, matched against it on all five fields
    and replayed as the ``reasoning`` items they were minted from.

    ``plaintext_replay`` is the operator's ``RESPONSES_PLAINTEXT_REPLAY``
    (8.17.0), or ``False`` -- its own default, so a caller that says nothing
    gets the bytes every release before 8.17.0 sent. Present, the history's
    readable reasoning is replayed as summary-only ``reasoning`` items. It is
    a **separate** switch from ``origin`` on purpose: an operator whose host
    refuses a summary item can turn this off and keep carriers.

    ``prompt_cache_key`` is the bounded (``<= 64`` char) per-conversation cache
    key derived when ``CHATGPT_OAUTH_SESSION_AFFINITY`` is enabled (8.28.0),
    or ``None`` -- omitted so v8.27.0 request bodies stay byte-identical.
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
    adaptive_mode = configured_reasoning_adaptive()
    effective_unspecified = unspecified_effort
    if (
        effective_unspecified is None
        and adaptive_mode != "off"
        and (
            reasoning.control is ReasoningControl.ADAPTIVE
            or (
                adaptive_mode == "on"
                and reasoning.control is ReasoningControl.ON
                and reasoning.effort is None
                and request.thinking is not None
                and request.thinking.type == "adaptive"
            )
        )
    ):
        effective_unspecified = ReasoningEffort.MAX.value
    parallel = configured_responses_parallel_tool_calls()
    default_service_tier = configured_chatgpt_oauth_service_tier()
    if request.responses_envelope is not None:
        body = build_responses_passthrough_body(
            request,
            reasoning=reasoning,
            store=False,
            stream=True,
            parallel_tool_calls=parallel,
            prompt_cache_key=prompt_cache_key,
            tool_name_max_length=tool_name_max_length,
            tool_schema_dialect=tool_schema_dialect,
            wire_notes=wire_notes,
            origin=origin,
            plaintext_replay=plaintext_replay,
            unspecified_effort=effective_unspecified,
            drop_reasoning_items=drop_reasoning_items,
            compact=compact,
            default_instructions=CODEX_BASE_INSTRUCTIONS,
            default_service_tier=default_service_tier,
        )
    else:
        body = build_responses_request_body(
            request,
            reasoning=reasoning,
            store=False,
            stream=True,
            # 8.15.0: the adapter-level switch, read here so a dashboard save
            # reaches the next request. Before it, this line hard-coded ``False``.
            parallel_tool_calls=parallel,
            max_output_tokens=None,
            prompt_cache_key=prompt_cache_key,
            include_encrypted_reasoning=True,
            tool_name_max_length=tool_name_max_length,
            tool_schema_dialect=tool_schema_dialect,
            wire_notes=wire_notes,
            origin=origin,
            plaintext_replay=plaintext_replay,
            unspecified_effort=effective_unspecified,
            default_instructions=CODEX_BASE_INSTRUCTIONS,
            default_service_tier=default_service_tier,
        )
    tier = body.get("service_tier")
    if isinstance(tier, str):
        aliased_tier = CHATGPT_OAUTH_SERVICE_TIER_ALIASES.get(tier.strip().lower())
        if aliased_tier is not None:
            body["service_tier"] = aliased_tier
    return body
