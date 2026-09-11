"""Convert Anthropic Messages API requests to OpenAI Responses API format.

Protocol, not provider. ``chatgpt_oauth`` was the first backend here to speak
the Responses API and so this translation grew up inside it; OpenCode Zen
serves two of its free models on the same wire, which is what made the split
necessary. Nothing in this module names a backend, a base URL or a credential
-- the caller supplies every choice a particular host makes about ``store``,
``include``, the output allowance and the cache key.
"""

import json
from collections.abc import Mapping
from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.core.anthropic.conversion import (
    AnthropicToOpenAIConverter,
    OpenAIConversionError,
    ReasoningReplayMode,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningPolicy,
)

RESPONSES_DEFAULT_REASONING_EFFORT = "medium"
RESPONSES_DEFAULT_REASONING_SUMMARY = "auto"

# There is deliberately no per-effort lookup table in this module. Between
# 5.61.1 and 6.68.0 a ``_RESPONSES_EFFORTS`` dict here rewrote ``xhigh`` and
# ``max`` to ``"high"`` on the premise that "the Responses endpoint documents
# four named efforts". That premise is false, and the rewrite was the last step
# before the body was built -- so gating recorded no adaptation and the request
# log honestly reported an effort that never left. 4,635 logged requests claimed
# ``max`` and sent ``high``; 5,743 claimed ``xhigh`` and sent ``high``.
#
# OpenAI's own client publishes the enum. Codex CLI 0.153.4 interns its
# ``ReasoningEffortConfig`` serde variants as
# ``none|minimal|low|medium|high|xhigh|max|ultra|persistent`` and embeds the
# prompt line "GPT-5.6 supports `none`, `low`, `medium`, `high`, `xhigh`, and
# `max`. If omitted, GPT-5.6 defaults to `medium`."; its bundled ``models.json``
# lists ``max`` in ``supported_reasoning_levels`` for gpt-5.6-sol/terra/luna and
# gpt-6-astra. models.dev publishes the same values, which is what the ladder
# already resolves into ``supported_efforts`` and what the Models page shows.
#
# So capability belongs to the ladder and the rung is sent verbatim. A model
# whose published vocabulary lacks a rung is clamped by ``_adapt_effort``
# (``application/reasoning_gating.py``) to the nearest rung it does publish, and
# that clamp is recorded as a CLAMPED adaptation and shown -- exactly as it is
# for every other provider. Do not reintroduce a table here.


def _strip_openai_system_message(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Extract the leading system message as Responses API instructions."""
    if messages and messages[0].get("role") == "system":
        instructions = messages[0].get("content")
        return instructions, messages[1:]
    return None, messages


def _openai_content_to_responses_parts(
    content: Any, *, assistant: bool
) -> list[dict[str, Any]]:
    """Convert OpenAI chat content parts to Responses API content parts.

    User/system parts become ``input_text``/``input_image``; assistant parts
    become ``output_text``. String content becomes a single text part.
    """
    text_type = "output_text" if assistant else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append({"type": text_type, "text": text})
        elif part_type == "image_url" and not assistant:
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                parts.append({"type": "input_image", "image_url": image_url})
    return parts


def _openai_message_to_responses_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one OpenAI-chat message to Responses API input items.

    The Responses API has no ``tool_calls`` field on message items: assistant
    tool calls are standalone ``function_call`` items, and tool results are
    ``function_call_output`` items keyed by ``call_id``.
    """
    role = message.get("role")
    content = message.get("content")

    if role == "tool":
        # The Responses dialect is the one that does not need the hoist:
        # ``FunctionCallOutput.output`` accepts a list of content parts
        # including ``input_image``, so an image a tool returned stays attached
        # to the call that produced it. A plain string stays a plain string,
        # byte for byte, which is every tool result that carried no media.
        output: Any
        if isinstance(content, str):
            output = content
        elif isinstance(content, list):
            parts = _openai_content_to_responses_parts(content, assistant=False)
            output = parts if parts else json.dumps(content)
        else:
            output = json.dumps(content)
        return [
            {
                "type": "function_call_output",
                "call_id": message.get("tool_call_id") or "",
                "output": output,
            }
        ]

    if role == "assistant":
        items: list[dict[str, Any]] = []
        parts = _openai_content_to_responses_parts(content, assistant=True)
        if parts:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": parts,
                }
            )
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            arguments = function.get("arguments")
            items.append(
                {
                    "type": "function_call",
                    "call_id": tool_call.get("id") or "",
                    "name": function.get("name") or "unknown",
                    "arguments": arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments or {}),
                }
            )
        return items

    # user / system converted to user
    if role == "system":
        role = "user"
    return [
        {
            "type": "message",
            "role": role,
            "content": _openai_content_to_responses_parts(content, assistant=False),
        }
    ]


def _openai_messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert OpenAI-chat message list to Responses API input list."""
    items: list[dict[str, Any]] = []
    for message in messages:
        items.extend(_openai_message_to_responses_items(message))
    return items


def _convert_tools(tools: list[Any] | None) -> list[dict[str, Any]] | None:
    """Convert Anthropic tools to ChatGPT Responses API tool definitions.

    The ChatGPT/Codex backend historically exposes only a small set of built-in
    tools, but we forward tools in the standard function shape so the backend
    can reject or accept them with its own error message.
    """
    if not tools:
        return None
    result: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or {
            "type": "object",
            "properties": {},
        }
        result.append(
            {
                "type": "function",
                "name": getattr(tool, "name", "unknown"),
                "description": getattr(tool, "description", None) or "",
                "parameters": schema,
            }
        )
    return result


def _convert_tool_choice(tool_choice: Any) -> Any:
    """Convert Anthropic tool_choice to ChatGPT Responses API tool_choice."""
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = tool_choice.get("type")
    if choice_type == "tool":
        name = tool_choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    if choice_type in {"auto", "none", "required"}:
        return choice_type
    if choice_type == "any":
        return "required"
    return tool_choice


def _reasoning_block(policy: ReasoningPolicy) -> dict[str, Any] | None:
    """Return the Responses API ``reasoning`` block for one policy, or None.

    Capability is deliberately *not* decided here. ``adapt_reasoning_policy``
    has already constrained this policy to what the resolved model accepts, so
    the provider's only job is to encode the intent it was handed; branching on
    the model id is what this function used to do and is exactly what the
    project forbids.

    An explicit OFF omits the block entirely rather than sending an
    ``effort`` of "none": omission is accepted by every model this backend
    serves, whereas the sentinel value is not documented for all of them.
    A policy that names no effort keeps the endpoint's long-standing
    ``medium`` so nobody's default silently changes.

    The rung itself is written out verbatim -- ``policy.effort.value``, no
    lookup, no narrowing. The two-fact rule has already constrained it: the
    model's published ``supported_efforts`` intersected with this host's
    declared ``effort_values`` (all six rungs, ``provider.py``), with any
    unlisted rung clamped to the nearest published one and *recorded*. The
    private table that used to sit here flattened ``xhigh`` and ``max`` to
    ``"high"`` after that machinery had finished, which is how the collapse
    escaped both the adaptation record and the request log from 5.61.1 until
    6.68.1.
    """
    if policy.control is ReasoningControl.OFF:
        return None
    effort = policy.effort.value if policy.effort is not None else None
    return {
        "effort": effort or RESPONSES_DEFAULT_REASONING_EFFORT,
        "summary": RESPONSES_DEFAULT_REASONING_SUMMARY,
    }


def _extract_system_instructions(request: MessagesRequest) -> str | None:
    """Return the top-level Anthropic system prompt as a single string."""
    system = request.system
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif hasattr(block, "type") and getattr(block, "type", None) == "text":
                parts.append(str(getattr(block, "text", "")))
        text = "\n\n".join(parts)
        return text if text else None
    return None


def build_responses_request_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
    store: bool = False,
    stream: bool = True,
    parallel_tool_calls: bool = False,
    max_output_tokens: int | None = None,
    prompt_cache_key: str | None = None,
    include_encrypted_reasoning: bool = True,
    extra_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Responses API request body from an Anthropic request.

    Every keyword is a choice a *host* makes rather than a property of the
    protocol, which is why none of them is decided here:

    ``store``/``stream``/``parallel_tool_calls``
        the three the ChatGPT/Codex backend has always been sent, and the
        defaults so that caller's body is unchanged by this function existing.
    ``max_output_tokens``
        the Responses spelling of the output allowance. ``None`` omits it,
        which is what the Codex endpoint wants (OpenCode's own codex plugin
        clears it for the same reason); a gateway that sizes its answer from
        the number gets it.
    ``prompt_cache_key``
        the field OpenCode's CLI fills with its session id, and where that
        header earns its keep -- it is what makes a multi-turn conversation
        hit the vendor's prompt cache.
    ``include_encrypted_reasoning``
        adds ``include: ["reasoning.encrypted_content"]`` beside a reasoning
        block. Required for stateless multi-turn reasoning on a ``store:
        false`` conversation, and refused by hosts that do not keep encrypted
        reasoning at all.
    ``extra_body``
        merged last, after the caller's own validator has had it. Absent for
        every backend that forbids one.

    Key order is fixed and deliberate: the body is recorded verbatim in the
    request log and compared byte for byte against a captured reference, so
    "the same request" has to serialise the same way every time.
    """

    try:
        openai_messages = AnthropicToOpenAIConverter.convert_messages(
            request.messages,
            reasoning_replay=ReasoningReplayMode.THINK_TAGS,
        )
    except OpenAIConversionError as exc:
        raise InvalidRequestError(str(exc)) from exc

    instructions = _extract_system_instructions(request)
    _, chat_messages = _strip_openai_system_message(openai_messages)

    body: dict[str, Any] = {
        "model": request.model,
        "input": _openai_messages_to_responses_input(chat_messages),
        "store": store,
        "stream": stream,
        "parallel_tool_calls": parallel_tool_calls,
    }
    if max_output_tokens is not None:
        body["max_output_tokens"] = max_output_tokens
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key

    if instructions:
        body["instructions"] = instructions

    tools = _convert_tools(request.tools)
    if tools:
        body["tools"] = tools
        tool_choice = _convert_tool_choice(request.tool_choice)
        if tool_choice is not None:
            body["tool_choice"] = tool_choice

    reasoning_block = _reasoning_block(reasoning)
    if reasoning_block is not None:
        body["reasoning"] = reasoning_block
        if include_encrypted_reasoning:
            # Required for stateless multi-turn reasoning: without it the
            # backend cannot carry encrypted reasoning across turns of a
            # ``store: false`` conversation.
            body["include"] = ["reasoning.encrypted_content"]

    if extra_body:
        body.update({str(key): value for key, value in extra_body.items()})

    return body


def responses_tool_call_to_anthropic(
    item: dict[str, Any],
    *,
    tool_name_override: str | None = None,
) -> dict[str, Any]:
    """Convert one Responses function_call item to an Anthropic tool_use block."""
    name = item.get("name") or tool_name_override or "unknown"
    arguments = item.get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    try:
        input_data = json.loads(arguments)
    except json.JSONDecodeError:
        input_data = {"raw": arguments}
    return {
        "type": "tool_use",
        "id": item.get("id", ""),
        "name": name,
        "input": input_data,
    }
