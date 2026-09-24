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
from my_claude_code.core.anthropic.openai_tool_names import (
    EMPTY_TOOL_CATALOGUE,
    MIN_TOOL_NAME_MAX_LENGTH,
    OpenAIToolNameCodec,
)
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningPolicy,
)

from .tool_schema_dialect import (
    RESPONSES_TOOL_SCHEMA_DIALECT,
    DialectSweep,
    ToolSchemaDialect,
    sweep_tool_catalogue,
)

RESPONSES_DEFAULT_REASONING_EFFORT = "medium"
RESPONSES_DEFAULT_REASONING_SUMMARY = "auto"


def responses_tool_name_codec(
    request: MessagesRequest,
    tool_name_max_length: int | None,
    tool_catalogue: Mapping[str, str] = EMPTY_TOOL_CATALOGUE,
) -> OpenAIToolNameCodec | None:
    """Return the tool-name codec a host's declared limit calls for, or None.

    ``None`` -- what ``chatgpt_oauth`` declares, by declaring nothing -- means
    names leave exactly as the client wrote them, which is every byte this
    module sent before 7.18.1. That backend accepts names past 64 characters
    (300 of 300 measured, longest 76), so aliasing there would change a working
    request for no reason and move its prompt-cache prefix.

    A host that refuses long names declares its limit on the transport. OpenCode
    Zen does: 1,040 requests to ``muse-spark-1.3/1.2-contributor-free`` failed
    on 2026-09-16 with "``name`` must be at most 64 characters, got 68". The
    codec is the one the Chat Completions funnel has always used, so a tool gets
    the same alias on either surface, and the alias is a pure function of the
    name -- stable across turns, tool order and tools added mid-session.

    Since 7.23.0 the limit may also be one a host *stated* in a rejection
    rather than one a profile declared -- the two are the same number to
    everything downstream, which is the point of resolving them in one place.
    The codec builds aliases under whatever ceiling it is given; below
    :data:`MIN_TOOL_NAME_MAX_LENGTH` it raises, because an alias that short
    stops naming the tool and inventing one would trade a visible failure for
    a call the model cannot make.

    ``tool_catalogue`` is this host's own tool spellings, empty for every host
    that has none. It turns the same codec from "alias what this host cannot
    accept" into "alias that, *and* rename the tools this host already has a
    name for". One codec either way, so encode and decode stay one decision;
    see :data:`EMPTY_TOOL_CATALOGUE` for the rules it adds.
    """
    if tool_name_max_length is None:
        return None
    if tool_name_max_length < MIN_TOOL_NAME_MAX_LENGTH:
        raise ValueError(
            "Responses tool-name aliasing needs a ceiling of at least "
            f"{MIN_TOOL_NAME_MAX_LENGTH}; got {tool_name_max_length}"
        )
    return OpenAIToolNameCodec.from_request(
        request, max_length=tool_name_max_length, catalogue=tool_catalogue
    )


def alias_responses_body_tool_names(
    body: dict[str, Any], tool_names: OpenAIToolNameCodec
) -> dict[str, Any] | None:
    """Re-encode one already-built body's tool names, or ``None`` if unchanged.

    The tool-name rung's rewrite. It exists because the body handed to the
    sender is the unit a retry can rebuild faithfully: reconstructing it from
    the original request would have to re-derive ``max_output_tokens``, the
    cache key and the caller's ``extra_body``, and any of those drifting would
    make the retry a different request rather than the same one spelled
    legally.

    Three sites, the same three :func:`build_responses_request_body` encodes:
    the ``tools`` catalogue, a forced ``tool_choice``, and every replayed
    ``function_call`` in ``input``. ``encode`` is the identity for a name the
    codec left alone, so a body with nothing to alias comes back ``None`` and
    is never retried.
    """

    if not tool_names.has_aliases:
        return None
    changed = False
    cloned = dict(body)

    tools = cloned.get("tools")
    if isinstance(tools, list):
        rebuilt_tools: list[Any] = []
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else None
            alias = tool_names.encode(name) if isinstance(name, str) else None
            if alias is not None and alias != name:
                rebuilt_tools.append({**tool, "name": alias})
                changed = True
            else:
                rebuilt_tools.append(tool)
        cloned["tools"] = rebuilt_tools

    choice = cloned.get("tool_choice")
    if isinstance(choice, dict):
        rebuilt_choice = _alias_tool_choice(choice, tool_names)
        if rebuilt_choice is not None:
            cloned["tool_choice"] = rebuilt_choice
            changed = True

    items = cloned.get("input")
    if isinstance(items, list):
        rebuilt_items: list[Any] = []
        for item in items:
            name = item.get("name") if isinstance(item, dict) else None
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and isinstance(name, str)
            ):
                alias = tool_names.encode(name)
                if alias != name:
                    rebuilt_items.append({**item, "name": alias})
                    changed = True
                    continue
            rebuilt_items.append(item)
        cloned["input"] = rebuilt_items

    return cloned if changed else None


def _alias_tool_choice(
    choice: dict[str, Any], tool_names: OpenAIToolNameCodec
) -> dict[str, Any] | None:
    """Re-encode a forced choice in either spelling, or ``None`` if unchanged."""

    name = choice.get("name")
    if isinstance(name, str):
        alias = tool_names.encode(name)
        return {"type": "function", "name": alias} if alias != name else None
    nested = choice.get("function")
    if isinstance(nested, dict) and isinstance(nested.get("name"), str):
        alias = tool_names.encode(nested["name"])
        # Promoted to the Responses spelling on the way, exactly as
        # ``_convert_tool_choice`` does the moment a codec exists: a host
        # strict enough to state a name ceiling is the kind that also reads
        # the published shape.
        return {"type": "function", "name": alias} if alias != nested["name"] else None
    return None


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


def _openai_message_to_responses_items(
    message: dict[str, Any],
    tool_names: OpenAIToolNameCodec | None = None,
) -> list[dict[str, Any]]:
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
            name = function.get("name") or "unknown"
            if tool_names is not None:
                name = tool_names.encode(name)
            items.append(
                {
                    "type": "function_call",
                    "call_id": tool_call.get("id") or "",
                    "name": name,
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
    tool_names: OpenAIToolNameCodec | None = None,
) -> list[dict[str, Any]]:
    """Convert OpenAI-chat message list to Responses API input list."""
    items: list[dict[str, Any]] = []
    for message in messages:
        items.extend(_openai_message_to_responses_items(message, tool_names))
    return items


def _convert_tools(
    tools: list[Any] | None,
    tool_names: OpenAIToolNameCodec | None = None,
    tool_schema_dialect: ToolSchemaDialect = RESPONSES_TOOL_SCHEMA_DIALECT,
) -> DialectSweep | None:
    """Convert Anthropic tools to ChatGPT Responses API tool definitions.

    The ChatGPT/Codex backend historically exposes only a small set of built-in
    tools, but we forward tools in the standard function shape so the backend
    can reject or accept them with its own error message.

    Then, **unconditionally**, the host's declared schema dialect: this is the
    one place a Responses tool definition is built, so it is the one place no
    Responses host can be built around. The sweep runs after the name codec,
    so a removal is recorded under the name that went on the wire -- the same
    name the reactive rung records -- and it returns the list it was handed,
    by identity, when nothing offends. See
    :mod:`~my_claude_code.providers.openai_responses.tool_schema_dialect`.
    """
    if not tools:
        return None
    result: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or {
            "type": "object",
            "properties": {},
        }
        name = getattr(tool, "name", "unknown")
        if tool_names is not None:
            name = tool_names.encode(name)
        result.append(
            {
                "type": "function",
                "name": name,
                "description": getattr(tool, "description", None) or "",
                "parameters": schema,
            }
        )
    return sweep_tool_catalogue(result, tool_schema_dialect)


def _convert_tool_choice(
    tool_choice: Any,
    tool_names: OpenAIToolNameCodec | None = None,
) -> Any:
    """Convert Anthropic tool_choice to ChatGPT Responses API tool_choice.

    A forced choice is spelled two ways. Without a codec it keeps the Chat
    Completions nesting ``{"type": "function", "function": {"name": X}}`` that
    ``chatgpt_oauth`` has always been sent -- left alone deliberately, because
    that backend's bytes are not this fix's to change. With one (a host that
    declared a tool-name limit) it uses the Responses API's own
    ``ToolChoiceFunction`` shape, ``{"type": "function", "name": X}``, as
    published in OpenAI's OpenAPI spec and the ``openai`` SDK's
    ``types/responses/tool_choice_function.py``, carrying the wire alias.
    """
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = tool_choice.get("type")
    if choice_type == "tool":
        name = tool_choice.get("name")
        if name:
            if tool_names is not None:
                return {"type": "function", "name": tool_names.encode(name)}
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
    tool_name_max_length: int | None = None,
    tool_catalogue: Mapping[str, str] = EMPTY_TOOL_CATALOGUE,
    include_tool_choice: bool = True,
    tool_schema_dialect: ToolSchemaDialect = RESPONSES_TOOL_SCHEMA_DIALECT,
    wire_notes: dict[str, str] | None = None,
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
    ``tool_name_max_length``
        the longest tool name this host accepts. ``None`` sends every name as
        the client wrote it. A declared limit aliases the names that exceed it
        (or are not ``[A-Za-z0-9_-]``) in ``tools``, a forced ``tool_choice``
        and replayed ``function_call`` items; see
        :func:`responses_tool_name_codec`. The stream converter must be handed
        the same codec to decode the model's calls back.
    ``tool_catalogue``
        this host's own tool spellings, for a host that classifies the
        catalogue it is sent rather than merely accepting one. Empty -- every
        host but OpenCode's free tier -- renames nothing.
    ``include_tool_choice``
        ``False`` omits the field entirely, for a host proven to accept no
        value but ``auto`` -- which is the Responses default, so omitting it
        sends the same instruction rather than a different one. ``True``, the
        default, is what every host that has never refused one gets, and it
        leaves their body byte-identical.
    ``tool_schema_dialect``
        what this host's validator refuses in a tool schema. Never optional:
        a host that declares nothing gets the Responses default, and one whose
        validator refuses nothing says so with an empty dialect. Swept after
        the name codec; identity when nothing offends.
    ``wire_notes``
        a caller's mapping to receive what this build took out of the
        client's request -- ``tool_schema_pruned``, names and paths only --
        so the sender records it beside the body it describes. Left untouched
        when nothing was removed.

    Order of operations, fixed: the free-tier catalogue (if any) has already
    replaced ``request.tools`` one layer up; the name codec is chosen here and
    encodes the names; the dialect sweeps the encoded tools; the sender then
    applies anything this host has *learned* to refuse, and the reactive rung
    answers whatever is left.

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

    tool_names = responses_tool_name_codec(
        request, tool_name_max_length, tool_catalogue
    )
    instructions = _extract_system_instructions(request)
    _, chat_messages = _strip_openai_system_message(openai_messages)

    body: dict[str, Any] = {
        "model": request.model,
        "input": _openai_messages_to_responses_input(chat_messages, tool_names),
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

    converted = _convert_tools(request.tools, tool_names, tool_schema_dialect)
    tools = converted.tools if converted is not None else None
    if converted is not None and wire_notes is not None:
        wire_notes.update(converted.wire_marker(tool_schema_dialect))
    if tools:
        body["tools"] = tools
        if include_tool_choice:
            tool_choice = _convert_tool_choice(request.tool_choice, tool_names)
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
    tool_names: OpenAIToolNameCodec | None = None,
) -> dict[str, Any]:
    """Convert one Responses function_call item to an Anthropic tool_use block.

    ``tool_names`` is the codec the request was built with, when its host
    declared a tool-name limit; a wire alias is decoded back to the client's
    original name. A name the codec did not generate passes through unchanged.
    """
    name = item.get("name") or tool_name_override or "unknown"
    if tool_names is not None:
        name = tool_names.decode(name)
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
