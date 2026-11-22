"""Convert Anthropic Messages API requests to OpenAI Responses API format.

Protocol, not provider. ``chatgpt_oauth`` was the first backend here to speak
the Responses API and so this translation grew up inside it; OpenCode Zen
serves two of its free models on the same wire, which is what made the split
necessary. Nothing in this module names a backend, a base URL or a credential
-- the caller supplies every choice a particular host makes about ``store``,
``include``, the output allowance and the cache key.
"""

import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from typing import Any

from loguru import logger

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.core.anthropic.content import get_block_attr, get_block_type
from my_claude_code.core.anthropic.conversion import (
    AnthropicToOpenAIConverter,
    OpenAIConversionError,
    ReasoningReplayMode,
    empty_system_messages_note,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import (
    EMPTY_TOOL_CATALOGUE,
    MIN_TOOL_NAME_MAX_LENGTH,
    OpenAIToolNameCodec,
)
from my_claude_code.core.openai_responses import (
    ReplayOrigin,
    accepts,
    call_id_from_item,
    decode_carrier,
    is_carrier,
    responses_tool_name_to_anthropic_name,
)
from my_claude_code.core.reasoning import (
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningPolicy,
)
from my_claude_code.core.wire_capture import record_reasoning_adaptation

from .tool_schema_dialect import (
    RESPONSES_TOOL_SCHEMA_DIALECT,
    DialectSweep,
    ToolSchemaDialect,
    sweep_tool_catalogue,
)

RESPONSES_DEFAULT_REASONING_EFFORT = "medium"
RESPONSES_DEFAULT_REASONING_SUMMARY = "auto"

#: How earlier assistant thinking is replayed into a Responses ``input``: not
#: at all. Until 7.58.1 this was ``THINK_TAGS`` -- every earlier thinking block
#: went back upstream as literal ``<think>\n...\n</think>`` *output text* of the
#: assistant, and a model shown its own past turns in that format writes its
#: next turn in it. 1,308 logged answers carried literal tags, ~97 % on
#: ``chatgpt_oauth`` and the rest on OpenCode's Responses-only ``muse-spark``;
#: all 107 muse rows with a recorded shape were plain ``content`` with no
#: reasoning and no function call -- the model narrated "let me ..." in tags and
#: ended the turn.
#:
#: Omission is the only honest option on this wire: OpenAI documents stateless
#: reasoning continuity (``store: false``) as replaying the *reasoning items*
#: with their ``encrypted_content``, which the stream converter never keeps,
#: and nothing shows a Responses host accepting a summary-only reasoning item
#: we would have to invent. So nothing is sent rather than something the host
#: did not write.
RESPONSES_REASONING_REPLAY = ReasoningReplayMode.DISABLED

#: The ``params.wire`` key recording that earlier thinking was left out of
#: ``input``. Counts only, never text -- the same rule every other marker on
#: that record keeps (``tool_schema_pruned``, ``tools_trimmed``). Deliberately
#: not spelled ``reasoning_*``/``thinking_*``: the wire recorder reads any key
#: with those prefixes as a reasoning *instruction* (``is_reasoning_key``), and
#: a note about the history would then count as reasoning sent.
HISTORY_THINKING_OMITTED = "history_thinking_omitted"

#: The four carrier notes (8.16.0). Written **only when the setting is on** and
#: **only when the count is non-zero**, so a request whose history carries no
#: carrier records exactly the ``params.wire`` it recorded before. Counts only,
#: never text: no substring of a carrier ever reaches the request log.
#:
#: The names avoid the ``reasoning_*``/``thinking_*`` prefixes for the same
#: reason ``history_thinking_omitted`` does.
HISTORY_CARRIER_REPLAYED = "history_carrier_replayed"
HISTORY_CARRIER_FOREIGN = "history_carrier_foreign"
HISTORY_CARRIER_MALFORMED = "history_carrier_malformed"
HISTORY_CARRIER_OVERSIZE = "history_carrier_oversize"

#: The plaintext replay's own note (8.17.0), and the one that is not a loss: a
#: ``thinking`` block's readable text -- or an assistant turn's top-level
#: ``reasoning_content`` -- was replayed as a summary-only ``reasoning`` item.
#:
#: A sibling of the carrier notes and never folded into them, because the two
#: say different things about different payloads: the carrier notes count the
#: host's own opaque envelope, this one counts text a model wrote. Folding them
#: would make ``history_carrier_replayed`` claim an envelope that never existed.
#:
#: Written **only when the setting is on** and **only when the count is
#: non-zero**, so a request whose history carries no readable reasoning records
#: exactly the ``params.wire`` it recorded before 8.17.0. Counts only, never
#: text -- the rule ``history_thinking_omitted`` and every ``history_carrier_*``
#: key keeps. The name avoids the ``reasoning_*``/``thinking_*`` prefixes for
#: the same reason they do.
HISTORY_PLAINTEXT_REPLAYED = "history_plaintext_replayed"

#: The block index a string-content assistant turn's ``reasoning_content`` is
#: recorded under. A string turn has no block list, so this can never collide
#: with a real block index -- every one of those is ``>= 0``. It exists so the
#: replay pass and the omission count can speak about that turn in the same
#: ``(message index, block index)`` vocabulary every other block uses.
_STRING_TURN_REASONING_INDEX = -1

#: Sentinel block index for a turn's ``Message.encrypted_reasoning`` items.
#: Negative so it can never collide with a real ``content`` block index
#: (``>= 0``) or :data:`_STRING_TURN_REASONING_INDEX` (``-1``).
_ENCRYPTED_REASONING_INDEX = -2

#: The fifth carrier note (8.16.3), and the only one that is not a loss: a
#: carrier **signature** was removed from a block whose readable text was kept,
#: on a Messages host that declares it signs nothing. It is a sibling of
#: ``history_carrier_foreign`` and never folded into it, because the two facts
#: read as opposites to an operator -- one says a block was lost, the other says
#: the text survived and only the envelope went. Folding a strip into
#: ``history_carrier_foreign`` would make that key's own sentence ("dropped")
#: false, which is the rule ``history_thinking_omitted`` and
#: ``foreign_thinking_dropped`` both keep: counts only, and a sentence that is
#: true.
#:
#: Written only by the **Messages** door, and only when the count is non-zero.
#: The Responses and Chat doors cannot strip: neither sends a ``thinking``
#: block's ``signature`` at all, so a carrier there has no text to survive.
#:
#: Deliberately not in ``_BUILD_CARRIER_KEYS``: that tuple is what the Responses
#: build's own retry marker extracts, and a strip cannot occur on that door.
HISTORY_CARRIER_STRIPPED = "history_carrier_stripped"

#: The three the *build* can count, in the order the note reads. Oversize is
#: the fourth and is counted on the reply instead: it is only knowable once the
#: host has sent the item that would have carried it, which is after this
#: request's body was recorded (see ``ResponsesStreamConverter.carrier_counts``).
_BUILD_CARRIER_KEYS: tuple[str, ...] = (
    HISTORY_CARRIER_REPLAYED,
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_MALFORMED,
)


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
                and item.get("type") in {"function_call", "custom_tool_call"}
                and isinstance(name, str)
                and not item.get("namespace")
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
    reasoning_by_message: Mapping[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Convert OpenAI-chat message list to Responses API input list.

    ``reasoning_by_message`` places replayed ``reasoning`` items **before** the
    message they belong to, keyed by that message's index in ``messages``. That
    is the position rule: a turn's reasoning items sit at the head of its own
    ``input[]`` run, before its ``message`` item and before every
    ``function_call``, which is the shape the host itself writes in ``output[]``
    and the shape that keeps every ``function_call`` adjacent to its own
    ``function_call_output``.

    ``None`` -- every caller until 8.16.0 -- emits exactly the list it emitted
    before.
    """

    items: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if reasoning_by_message is not None:
            items.extend(reasoning_by_message.get(index, ()))
        items.extend(_openai_message_to_responses_items(message, tool_names))
    return items


def _envelope_tool_metadata(
    envelope_tools: list[Any] | None,
    tool_catalogue: Mapping[str, str],
) -> tuple[dict[str, bool], dict[str, dict[str, Any]]]:
    """Index inbound ``strict`` flags and ``type: 'custom'`` tools by Anthropic name."""
    strict_by_name: dict[str, bool] = {}
    custom_by_name: dict[str, dict[str, Any]] = {}
    if not isinstance(envelope_tools, list):
        return strict_by_name, custom_by_name

    def _visit(raw_tool: Any, *, namespace: str | None) -> None:
        if not isinstance(raw_tool, Mapping):
            return
        tool_type = raw_tool.get("type")
        if tool_type == "namespace":
            ns_name = raw_tool.get("name")
            nested = raw_tool.get("tools")
            if isinstance(ns_name, str) and ns_name and isinstance(nested, list):
                for item in nested:
                    _visit(item, namespace=ns_name)
            return
        if tool_type == "function":
            fn = raw_tool.get("function")
            source = fn if isinstance(fn, Mapping) else raw_tool
            name = source.get("name")
            if not isinstance(name, str) or not name:
                return
            key = responses_tool_name_to_anthropic_name(name, namespace=namespace)
            strict_val = source.get("strict")
            if isinstance(strict_val, bool):
                strict_by_name[key] = strict_val
            return
        if tool_type == "custom":
            custom = raw_tool.get("custom")
            source = custom if isinstance(custom, Mapping) else raw_tool
            name = source.get("name")
            if not isinstance(name, str) or not name:
                return
            key = responses_tool_name_to_anthropic_name(name, namespace=namespace)
            if key not in tool_catalogue:
                copied = deepcopy(dict(raw_tool))
                if namespace:
                    copied.pop("namespace", None)
                custom_by_name[key] = copied

    for entry in envelope_tools:
        _visit(entry, namespace=None)
    return strict_by_name, custom_by_name


def _convert_tools(
    tools: list[Any] | None,
    tool_names: OpenAIToolNameCodec | None = None,
    tool_schema_dialect: ToolSchemaDialect = RESPONSES_TOOL_SCHEMA_DIALECT,
    *,
    envelope_tools: list[Any] | None = None,
    tool_catalogue: Mapping[str, str] = EMPTY_TOOL_CATALOGUE,
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
    strict_by_name, custom_by_name = _envelope_tool_metadata(
        envelope_tools, tool_catalogue
    )
    result: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or {
            "type": "object",
            "properties": {},
        }
        raw_name = getattr(tool, "name", "unknown")
        wire_name = tool_names.encode(raw_name) if tool_names is not None else raw_name
        custom_tool = custom_by_name.get(raw_name)
        if custom_tool is not None:
            if isinstance(custom_tool.get("custom"), dict):
                custom_inner = dict(custom_tool["custom"])
                custom_inner["name"] = wire_name
                custom_tool["custom"] = custom_inner
            else:
                custom_tool["name"] = wire_name
            result.append(custom_tool)
            continue
        converted_tool: dict[str, Any] = {
            "type": "function",
            "name": wire_name,
            "description": getattr(tool, "description", None) or "",
            "parameters": schema,
        }
        if raw_name in strict_by_name:
            converted_tool["strict"] = strict_by_name[raw_name]
        result.append(converted_tool)
    return sweep_tool_catalogue(result, tool_schema_dialect)


def _convert_tool_choice(
    tool_choice: Any,
    tool_names: OpenAIToolNameCodec | None = None,
    *,
    custom_tool_names: frozenset[str] = frozenset(),
) -> Any:
    """Convert Anthropic tool_choice to OpenAI Responses API tool_choice.

    A forced choice always uses the Responses API's ``ToolChoiceFunction``
    shape, ``{"type": "function", "name": X}`` (as published in OpenAI's
    OpenAPI spec and the ``openai`` SDK's
    ``types/responses/tool_choice_function.py``), encoding ``X`` through
    ``tool_names`` when a tool-name ceiling codec is active and passing
    ``name`` verbatim otherwise.
    """
    if not isinstance(tool_choice, dict):
        return tool_choice
    choice_type = tool_choice.get("type")
    if choice_type == "tool":
        name = tool_choice.get("name")
        if name:
            wire_name = tool_names.encode(name) if tool_names is not None else name
            if name in custom_tool_names:
                return {"type": "custom", "name": wire_name}
            return {"type": "function", "name": wire_name}
    if choice_type in {"auto", "none", "required"}:
        return choice_type
    if choice_type == "any":
        return "required"
    return tool_choice


def _reasoning_block(
    policy: ReasoningPolicy,
    *,
    unspecified_effort: str | None = None,
    envelope_reasoning: Mapping[str, Any] | None = None,
    allow_context: bool = False,
) -> dict[str, Any] | None:
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
    ``medium`` unless ``unspecified_effort`` overrides ``ADAPTIVE`` /
    level-less ``ON`` (``DEFAULT`` always keeps ``medium``).

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
    if policy.effort is not None:
        effort = policy.effort.value
    elif unspecified_effort and policy.control in {
        ReasoningControl.ADAPTIVE,
        ReasoningControl.ON,
    }:
        effort = unspecified_effort
    else:
        effort = RESPONSES_DEFAULT_REASONING_EFFORT
    block: dict[str, Any] = {"effort": effort}
    if isinstance(envelope_reasoning, Mapping) and "summary" in envelope_reasoning:
        summary = envelope_reasoning.get("summary")
        if summary is not None:
            block["summary"] = summary
    else:
        block["summary"] = RESPONSES_DEFAULT_REASONING_SUMMARY
    if allow_context and isinstance(envelope_reasoning, Mapping):
        context = envelope_reasoning.get("context")
        if context is not None:
            block["context"] = context
    return block


def _note_adaptive_substitution(
    policy: ReasoningPolicy,
    model: str,
    *,
    sent_effort: str | None = None,
) -> None:
    """Record that an ADAPTIVE request left as a named effort rung.

    This door has no adaptive channel: ``reasoning.effort`` takes a word and
    nothing else, so "let the model choose" cannot be spelled. When
    ``sent_effort`` is the endpoint default ``medium`` that wording is kept
    verbatim; when a caller supplies a model's top rung (such as ``"max"`` or
    ``"xhigh"`` on ``chatgpt_oauth``) the recorded message and warning name the
    actual effort sent.

    A no-op outside a tracked request (``record_reasoning_adaptation``), so
    the body builder stays usable from tests and from token counting.
    """

    if policy.control is not ReasoningControl.ADAPTIVE:
        return
    effort = sent_effort or RESPONSES_DEFAULT_REASONING_EFFORT
    rationale = (
        "the endpoint's own default rung"
        if effort == RESPONSES_DEFAULT_REASONING_EFFORT
        else "its highest supported rung"
    )
    record_reasoning_adaptation(
        ReasoningAdaptationKind.SUBSTITUTED,
        f"REASONING ADAPTIVE SUBSTITUTED: '{model}' has no adaptive channel on"
        f" the Responses door; sending effort"
        f" '{effort}' via `reasoning.effort` -- {rationale} -- because a host"
        f" that can only be told a level cannot be told to choose one",
    )
    logger.warning(
        "REASONING ADAPTIVE: '{}' has no adaptive channel on the Responses"
        " door; sent effort '{}'",
        model,
        effort,
    )


def _carrier_blocks(messages: list[Any]) -> Iterator[tuple[int, int, str, str]]:
    """Yield ``(message index, block index, block type, opaque value)``.

    Only the two block types that carry an opaque continuation field are
    walked: a ``thinking`` block's ``signature`` and a ``redacted_thinking``
    block's ``data``. Nothing else on this surface can hold a carrier, and a
    top-level ``reasoning_content`` string is text rather than an envelope.
    """

    for message_index, message in enumerate(messages):
        if getattr(message, "role", None) != "assistant":
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            block_type = get_block_type(block)
            if block_type == "thinking":
                value = get_block_attr(block, "signature", "")
            elif block_type == "redacted_thinking":
                value = get_block_attr(block, "data", "")
            else:
                continue
            if isinstance(value, str):
                yield message_index, block_index, block_type, value


def _decode_accepted_carrier(
    value: str,
    destination: ReplayOrigin | None,
    counts: dict[str, int],
) -> dict[str, Any] | None:
    """Decode one ``mcc:reasoning:v1:`` carrier and check destination provenance."""

    payload = decode_carrier(value)
    if payload is None:
        counts[HISTORY_CARRIER_MALFORMED] = counts.get(HISTORY_CARRIER_MALFORMED, 0) + 1
        return None
    if not accepts(destination, payload.origin):
        counts[HISTORY_CARRIER_FOREIGN] = counts.get(HISTORY_CARRIER_FOREIGN, 0) + 1
        return None
    return dict(payload.native)


def _append_unique_reasoning_item(
    items: dict[int, list[dict[str, Any]]],
    message_index: int,
    item: dict[str, Any],
) -> bool:
    """Append ``item`` to ``items[message_index]`` unless already present."""

    existing = items.get(message_index)
    if existing is not None and item in existing:
        return False
    items.setdefault(message_index, []).append(item)
    return True


def _carrier_items(
    messages: list[Any],
    destination: ReplayOrigin | None,
    counts: dict[str, int],
) -> tuple[dict[int, list[dict[str, Any]]], set[tuple[int, int]]]:
    """Decode this history's carriers and decide which may be replayed.

    Returns ``(reasoning_by_input_index, replayed_blocks)``.

    ``reasoning_by_input_index`` maps an index in ``messages`` to the ordered
    ``reasoning`` items that turn contributes, ready for
    :meth:`~my_claude_code.core.anthropic.conversion.AnthropicToOpenAIConverter.
    convert_messages` to place at the head of that turn's ``input[]`` run.

    Both ``Message.encrypted_reasoning`` (populated by the inbound Responses
    door) and ``content``-block carriers (``thinking.signature`` /
    ``redacted_thinking.data``) are walked in deterministic order. Inside
    ``encrypted_reasoning``, ``mcc:reasoning:v1:`` carriers undergo the same
    decode + provenance check, while raw opaque ``reasoning`` items with
    non-empty ``encrypted_content`` are copied cleanly when active.

    ``replayed_blocks`` is the set of ``(message index, block index)`` whose
    carrier was accepted, so :func:`_earlier_thinking` counts exactly the
    blocks that were **not** replayed. Two passes, one decision: the note and
    the body can never disagree.

    Every outcome is counted, never raised. A block that claims to be a carrier
    and does not decode is dropped and counted; a carrier minted by another
    host is dropped and counted. Neither is ever partially used, and neither is
    ever sent as a raw string in ``encrypted_content`` -- that is precisely the
    fake-blob failure Bifrost documents.

    A ``thinking`` block whose text a client rewrote is still a carrier: the
    envelope is authoritative and the text is decoration, so no comparison is
    made between the block's ``thinking`` and ``native["summary"]``. A
    comparison would be a second source of truth and would drop a valid carrier
    over a cosmetic difference.
    """

    items: dict[int, list[dict[str, Any]]] = {}
    replayed: set[tuple[int, int]] = set()
    for message_index, message in enumerate(messages):
        if getattr(message, "role", None) != "assistant":
            continue
        encrypted_items = getattr(message, "encrypted_reasoning", None)
        if not isinstance(encrypted_items, list):
            continue
        for raw_item in encrypted_items:
            if not isinstance(raw_item, Mapping) or raw_item.get("type") != "reasoning":
                continue
            encrypted = raw_item.get("encrypted_content")
            if not isinstance(encrypted, str) or not encrypted:
                continue
            if is_carrier(encrypted):
                native = _decode_accepted_carrier(encrypted, destination, counts)
                if native is None:
                    continue
                candidate = native
            else:
                candidate = {str(k): v for k, v in raw_item.items()}
            if _append_unique_reasoning_item(items, message_index, candidate):
                counts[HISTORY_CARRIER_REPLAYED] = (
                    counts.get(HISTORY_CARRIER_REPLAYED, 0) + 1
                )
            replayed.add((message_index, _ENCRYPTED_REASONING_INDEX))

    for message_index, block_index, _kind, value in _carrier_blocks(messages):
        if not is_carrier(value):
            continue
        native = _decode_accepted_carrier(value, destination, counts)
        if native is None:
            continue
        if _append_unique_reasoning_item(items, message_index, native):
            counts[HISTORY_CARRIER_REPLAYED] = (
                counts.get(HISTORY_CARRIER_REPLAYED, 0) + 1
            )
        replayed.add((message_index, block_index))
    return items, replayed


def _summary_item(text: str) -> dict[str, Any]:
    """One summary-only ``reasoning`` input item carrying ``text``.

    The exact shape three shipped implementations build and the shape this
    module already *reads* inbound (``core/openai_responses/reasoning.py``
    pulls ``{"type": "summary_text", "text": ...}`` out of a ``summary``
    array). **No ``id``, no ``encrypted_content``, no ``status``, no
    ``content``**: LiteLLM's own docstring records why -- *"the Responses API
    404s on any id it did not mint itself and rejects an empty one, while an
    item without an id is always accepted"* -- and a summary-only item has no
    ciphertext to bind an id to.

    A fresh dict every call, and the text is the block's own string: nothing
    here can mutate the client's history.
    """

    return {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}],
    }


def _plaintext_items(
    messages: list[Any],
    counts: dict[str, int],
    replayed: set[tuple[int, int]],
) -> tuple[dict[int, list[dict[str, Any]]], set[tuple[int, int]]]:
    """Summary-only ``reasoning`` items for this history's readable reasoning.

    The second producer for ``reasoning_by_message`` (8.17.0). PR-2's carrier
    pass replays the *host's own* item; this one replays the *text* a model
    wrote, which is portable across models and providers by decision 9.

    Returns ``(reasoning_by_input_index, replayed_blocks)``, the same pair
    :func:`_carrier_items` returns and for the same reason: the note and the
    body must never disagree about what was replayed.

    ``replayed`` is PR-2's accepted-carrier set: a block in it already went
    upstream inside its envelope, and replaying its text as well would send the
    same reasoning twice. Every other block with readable text is replayed --
    **including one whose carrier was refused**, because the envelope is
    host-bound and the text is not (ROUND 9: nothing is lost).

    The dynamic rule, in full:

    * a ``thinking`` block with readable text and no accepted carrier -> one
      summary item;
    * a ``thinking`` block with blank or whitespace-only text -> nothing, and
      ``_earlier_thinking`` still counts it as omitted;
    * a ``redacted_thinking`` block -> nothing, always: it has no text field,
      and its ``data`` is the opaque envelope, never readable text. Reading
      ``data`` here would be the fake-blob failure Bifrost documents;
    * an assistant turn whose ``content`` is a string with a non-blank top-level
      ``reasoning_content`` -> one summary item. It is plaintext reasoning with
      no envelope, and leaving it out would make the omission note count a
      block that was in fact replayed.

    **Nothing is invented.** A block with no readable text produces no item; a
    ``redacted_thinking`` block with no carrier produces no item; a blank
    ``thinking`` block produces no item.

    **Nothing is raised.** A block that is not a dict, a ``thinking`` field that
    is not a string, a ``content`` that is neither list nor string, a
    ``reasoning_content`` that is not a string -- each is skipped, never raised
    and never partially used. Every outcome that matters is counted.

    **Nothing is mutated.** The item is a new dict and the text is the block's
    own immutable string, so the client's history is untouched -- the rule
    ``drop_foreign_thinking`` documents on the Messages door.
    """

    items: dict[int, list[dict[str, Any]]] = {}
    replayed_blocks: set[tuple[int, int]] = set()
    for message_index, message in enumerate(messages):
        if getattr(message, "role", None) != "assistant":
            continue
        content = getattr(message, "content", None)
        turn_has_carrier = any(msg_idx == message_index for msg_idx, _ in replayed)
        if isinstance(content, list):
            turn_replayed_thinking = False
            for block_index, block in enumerate(content):
                # Only a ``thinking`` block has a text field. A
                # ``redacted_thinking`` block's ``data`` is the envelope.
                if get_block_type(block) != "thinking":
                    continue
                if (message_index, block_index) in replayed:
                    # Its reasoning already went upstream inside the host's own
                    # item; the text would be the same reasoning twice.
                    continue
                text = get_block_attr(block, "thinking", "")
                if not isinstance(text, str) or not text.strip():
                    # Nothing readable to carry.
                    continue
                items.setdefault(message_index, []).append(_summary_item(text))
                replayed_blocks.add((message_index, block_index))
                counts[HISTORY_PLAINTEXT_REPLAYED] = (
                    counts.get(HISTORY_PLAINTEXT_REPLAYED, 0) + 1
                )
                turn_replayed_thinking = True
            if turn_has_carrier or turn_replayed_thinking:
                continue
            reasoning = getattr(message, "reasoning_content", None)
            if not isinstance(reasoning, str) or not reasoning.strip():
                continue
            items.setdefault(message_index, []).append(_summary_item(reasoning))
            replayed_blocks.add((message_index, _STRING_TURN_REASONING_INDEX))
            counts[HISTORY_PLAINTEXT_REPLAYED] = (
                counts.get(HISTORY_PLAINTEXT_REPLAYED, 0) + 1
            )
        elif isinstance(content, str):
            if turn_has_carrier:
                continue
            reasoning = getattr(message, "reasoning_content", None)
            if not isinstance(reasoning, str) or not reasoning.strip():
                continue
            items.setdefault(message_index, []).append(_summary_item(reasoning))
            replayed_blocks.add((message_index, _STRING_TURN_REASONING_INDEX))
            counts[HISTORY_PLAINTEXT_REPLAYED] = (
                counts.get(HISTORY_PLAINTEXT_REPLAYED, 0) + 1
            )
    return items, replayed_blocks


def _earlier_thinking(
    messages: list[Any],
    replayed: set[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """Count the thinking an Anthropic history carries, as ``(blocks, chars)``.

    Exactly what the Responses surface leaves out of ``input``: every
    ``thinking`` block of an assistant turn given as a block list, every
    ``redacted_thinking`` block beside it, the top-level ``reasoning_content``
    of an assistant turn (string or list content), and any un-replayed
    ``encrypted_reasoning`` items when no ``reasoning_content`` covers that turn.

    ``replayed`` is the set of ``(message index, block index)`` whose reasoning
    the replay passes accepted (8.16.0, extended 8.17.0). A block in that set is
    **not** counted: its reasoning went upstream -- inside the envelope, or as a
    summary item -- so calling it omitted would make the note disagree with the
    body. Everything else is counted, which is every block when the setting is
    off -- and that is deliberate: a ``redacted_thinking`` block was silently
    uncounted until 8.16.0, and a note that leaves out what it actually leaves
    out is not a note. The change is visible only in ``params.wire``, never in a
    body.

    A turn's ``reasoning_content`` is recorded under
    :data:`_STRING_TURN_REASONING_INDEX`, and its ``encrypted_reasoning`` under
    :data:`_ENCRYPTED_REASONING_INDEX`, so the omission count skips every turn
    whose reasoning was already replayed.
    """

    skipped = replayed or set()
    blocks = 0
    chars = 0
    for message_index, message in enumerate(messages):
        if getattr(message, "role", None) != "assistant":
            continue
        content = getattr(message, "content", None)
        turn_encrypted_replayed = (message_index, _ENCRYPTED_REASONING_INDEX) in skipped
        turn_text_replayed = (message_index, _STRING_TURN_REASONING_INDEX) in skipped
        counted_turn_reasoning = False
        if isinstance(content, list):
            for block_index, block in enumerate(content):
                block_type = get_block_type(block)
                if block_type == "thinking":
                    field = "thinking"
                elif block_type == "redacted_thinking":
                    field = "data"
                else:
                    continue
                if (message_index, block_index) in skipped:
                    continue
                blocks += 1
                text = get_block_attr(block, field, "")
                chars += len(text) if isinstance(text, str) else 0
            if not turn_encrypted_replayed and not turn_text_replayed:
                reasoning = getattr(message, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning:
                    blocks += 1
                    chars += len(reasoning)
                    counted_turn_reasoning = True
        elif isinstance(content, str):
            if not turn_encrypted_replayed and not turn_text_replayed:
                reasoning = getattr(message, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning:
                    blocks += 1
                    chars += len(reasoning)
                    counted_turn_reasoning = True
        if (
            not turn_encrypted_replayed
            and not turn_text_replayed
            and not counted_turn_reasoning
        ):
            encrypted_items = getattr(message, "encrypted_reasoning", None)
            if isinstance(encrypted_items, list):
                for raw_item in encrypted_items:
                    if (
                        isinstance(raw_item, Mapping)
                        and raw_item.get("type") == "reasoning"
                        and isinstance(
                            encrypted := raw_item.get("encrypted_content"), str
                        )
                        and encrypted
                    ):
                        blocks += 1
                        chars += len(encrypted)
    return blocks, chars


def _history_thinking_note(
    messages: list[Any],
    replayed: set[tuple[int, int]] | None = None,
    plaintext_replay: bool = False,
) -> dict[str, str]:
    """The ``history_thinking_omitted`` record for one history, or ``{}``.

    ``plaintext_replay`` changes **only the sentence's second clause**, and only
    when the setting is on. With it on, "the Responses surface replays no
    reasoning as text" is false for the blocks the plaintext pass replayed --
    the count is already right (``replayed`` skips them), so the *reason* must
    be right too. With it off the sentence is byte-identical to 8.16.3's, which
    is what keeps every stored ``params.wire`` unchanged.
    """

    blocks, chars = _earlier_thinking(messages, replayed)
    if not blocks:
        return {}
    noun = "block" if blocks == 1 else "blocks"
    tail = (
        "the Responses surface replays reasoning only as structured items"
        if plaintext_replay
        else "the Responses surface replays no reasoning as text"
    )
    return {
        HISTORY_THINKING_OMITTED: (
            f"omitted {blocks} earlier thinking {noun} ({chars} chars) from "
            f"input; {tail}"
        )
    }


def carrier_foreign_note(count: int) -> dict[str, str]:
    """The ``history_carrier_foreign`` record for ``count`` dropped carriers.

    **One sentence for every door** (8.16.1). The provenance guard refuses a
    carrier at three seams, and the operator must read the same line whichever
    one refused it:

    * the **Responses** door, where PR-2's ``accepts()`` says the origin does
      not match (:func:`_carrier_note` calls this);
    * the **Messages** door, where the prefix alone decides, because a
      ``responses``-protocol envelope can never match a ``messages``
      destination -- ``accepts()`` is ``False`` for every possible value of the
      other four fields, so the verdict is evaluated once at design time rather
      than at runtime (the spec's Q2);
    * the **Chat** door, for the same protocol reason, in
      ``providers/openai_chat/reasoning_details.py``.

    ``{}`` for a count of zero, which keeps every other request's recorded
    params exactly as they were -- the rule ``empty_system_messages_note``
    states. Counts only: no substring of an envelope, no summary text, no id.
    """

    if count <= 0:
        return {}
    noun = "carrier" if count == 1 else "carriers"
    return {
        HISTORY_CARRIER_FOREIGN: (
            f"dropped {count} reasoning {noun} minted by another host"
        )
    }


def carrier_stripped_note(count: int) -> dict[str, str]:
    """The ``history_carrier_stripped`` record for ``count`` removed envelopes.

    **The other half of the same refusal** (8.16.3), and a different sentence on
    purpose. ``carrier_foreign_note`` says a block was **lost**; this says the
    block's readable text **survived** and only the host-bound envelope went. An
    operator reading the request detail must be able to tell those apart, which
    is why they are two keys and not one count.

    Only the **Messages** door can write it, and only where the destination
    declares ``signs_thinking=False`` -- a host that mints and checks no
    signature accepts an unsigned ``thinking`` block, so the envelope is
    removed from a **new** dict and the block, its text and its position are
    kept. On a host that signs, the same block is dropped whole and counted
    ``history_carrier_foreign`` instead: an unsigned ``thinking`` block is a 400
    there, so stripping would only move the drop one rule later.

    ``{}`` for a count of zero, which keeps every other request's recorded
    params exactly as they were -- the rule ``carrier_foreign_note`` and
    ``empty_system_messages_note`` both state. Counts only: no substring of an
    envelope, no summary text, no id.
    """

    if count <= 0:
        return {}
    noun = "carrier" if count == 1 else "carriers"
    block = "a block" if count == 1 else "blocks"
    return {
        HISTORY_CARRIER_STRIPPED: (
            f"removed {count} reasoning {noun} from {block} whose text was kept "
            "(this host signs nothing)"
        )
    }


def _carrier_note(counts: Mapping[str, int]) -> dict[str, str]:
    """The ``history_carrier_*`` records for one build, or ``{}``.

    Written only for a non-zero count, so a history with no carrier records
    exactly the ``params.wire`` it recorded before 8.16.0. Counts only -- no
    substring of an envelope, no summary text, no id.
    """

    notes: dict[str, str] = {}
    replayed = counts.get(HISTORY_CARRIER_REPLAYED, 0)
    foreign = counts.get(HISTORY_CARRIER_FOREIGN, 0)
    malformed = counts.get(HISTORY_CARRIER_MALFORMED, 0)
    if replayed:
        noun = "carrier" if replayed == 1 else "carriers"
        notes[HISTORY_CARRIER_REPLAYED] = (
            f"replayed {replayed} reasoning {noun} from the client's history"
        )
    notes.update(carrier_foreign_note(foreign))
    if malformed:
        noun = "carrier" if malformed == 1 else "carriers"
        notes[HISTORY_CARRIER_MALFORMED] = (
            f"dropped {malformed} malformed reasoning {noun} from the client's history"
        )
    return notes


def _plaintext_note(counts: Mapping[str, int]) -> dict[str, str]:
    """The ``history_plaintext_replayed`` record for one build, or ``{}``.

    Written only for a non-zero count, so a history with no readable reasoning
    records exactly the ``params.wire`` it recorded before 8.17.0. Counts only
    -- no block's text, no summary, no id.
    """

    replayed = counts.get(HISTORY_PLAINTEXT_REPLAYED, 0)
    if not replayed:
        return {}
    noun = "block" if replayed == 1 else "blocks"
    return {
        HISTORY_PLAINTEXT_REPLAYED: (
            f"replayed {replayed} readable reasoning {noun} from the client's history"
        )
    }


def history_thinking_marker(
    wire_notes: Mapping[str, str] | None,
) -> dict[str, str]:
    """The replay record out of one build's ``wire_notes``, or ``{}``.

    Separate from the schema markers on purpose: those are merged per key by
    :func:`~my_claude_code.providers.recovery.merge_tool_schema_markers`,
    which keeps only its own keys, and a retry never changes what the history
    left out -- so every attempt row of one request carries the same line.
    """

    if not wire_notes:
        return {}
    note = wire_notes.get(HISTORY_THINKING_OMITTED)
    return {HISTORY_THINKING_OMITTED: note} if note else {}


def history_carrier_marker(
    wire_notes: Mapping[str, str] | None,
) -> dict[str, str]:
    """The carrier record out of one build's ``wire_notes``, or ``{}``.

    Beside :func:`history_thinking_marker`, and for the same reason: a retry
    never changes what the history carried, so every attempt row of one
    request carries the same line. Empty when the setting is off -- the keys
    are only ever written by a build that was handed an ``origin`` -- so a
    request whose history carries no carrier records exactly the
    ``params.wire`` it recorded before 8.16.0.
    """

    if not wire_notes:
        return {}
    return {key: wire_notes[key] for key in _BUILD_CARRIER_KEYS if key in wire_notes}


def history_plaintext_marker(
    wire_notes: Mapping[str, str] | None,
) -> dict[str, str]:
    """The plaintext-replay record out of one build's ``wire_notes``, or ``{}``.

    Beside :func:`history_thinking_marker` and :func:`history_carrier_marker`,
    and for the same reason: a retry never changes what the history carried, so
    every attempt row of one request carries the same line. Empty when the
    setting is off -- the key is only ever written by a build that was handed
    ``plaintext_replay=True`` -- so a request whose history carries no readable
    reasoning records exactly the ``params.wire`` it recorded before 8.17.0.
    """

    if not wire_notes:
        return {}
    note = wire_notes.get(HISTORY_PLAINTEXT_REPLAYED)
    return {HISTORY_PLAINTEXT_REPLAYED: note} if note else {}


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
    origin: ReplayOrigin | None = None,
    plaintext_replay: bool = False,
    unspecified_effort: str | None = None,
    apply_responses_sidecars: bool = False,
    allow_openai_responses_sidecars: bool = False,
) -> dict[str, Any]:
    """Build a Responses API request body from an Anthropic request.

    Every keyword is a choice a *host* makes rather than a property of the
    protocol, which is why none of them is decided here:

    ``store``/``stream``/``parallel_tool_calls``
        the three the ChatGPT/Codex backend has always been sent, and the
        defaults so that caller's body is unchanged by this function existing.
        Since 8.15.0 the two Responses senders pass
        ``parallel_tool_calls`` from the operator's setting
        (``RESPONSES_PARALLEL_TOOL_CALLS``, on by default) instead of a
        constant; the keyword default here stays ``False``, so a caller that
        says nothing still gets the bytes every release before 8.15.0 sent.
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
        client's request -- ``tool_schema_pruned``, names and paths only, and
        ``history_thinking_omitted`` and ``empty_system_messages_dropped``,
        counts only -- so the sender records it beside the body it describes.
        Left untouched when nothing was removed.
    ``origin``
        the host this body is about to be sent to, as five strings (8.16.0),
        or ``None`` -- which is every caller until the operator turns
        ``RESPONSES_CARRIER_REPLAY`` on, and is what makes this function's
        output byte-identical to every release before 8.16.0. Present, it
        turns on the carrier pass: every ``thinking``/``redacted_thinking``
        block whose ``signature``/``data`` is one of our envelopes is decoded,
        matched against ``origin`` on all five fields, and -- when it matches
        -- replayed as the ``reasoning`` item it was minted from, at the head
        of its own turn's ``input[]`` run.
    ``plaintext_replay``
        whether an earlier turn's **readable** reasoning is replayed as a
        summary-only ``reasoning`` item (8.17.0), or ``False`` -- the shipped
        default here, and every caller until the operator turns
        ``RESPONSES_PLAINTEXT_REPLAY`` on. ``False`` is what makes this
        function's output byte-identical to every release before 8.17.0.
        Present, a ``thinking`` block with readable text and no accepted
        carrier -- **including one whose carrier the provenance guard refused**
        -- and an assistant turn's non-blank top-level ``reasoning_content``
        each become one ``{"type": "reasoning", "summary": [...]}`` item at the
        head of their own turn's run, after any carrier item for the same turn.
    ``unspecified_effort``
        effort word used when ``reasoning`` is ``ADAPTIVE`` or level-less
        ``ON`` (``effort is None``), or ``None`` to keep the protocol default
        (``"medium"``) so non-ChatGPT callers stay byte-identical.

    Earlier assistant thinking is never replayed **as text** on this surface
    (:data:`RESPONSES_REASONING_REPLAY`); what was left out is counted into
    ``wire_notes``. The carrier is a fourth path, not a mode: it replays an
    opaque item the host itself wrote, never a ``thinking`` string. The
    plaintext path is a fifth, and it is still not "as text": the reasoning
    lands in a structured ``reasoning`` item's ``summary``, which has no field
    a `` thinking`` tag could live in, and never in ``output_text``.

    Order of operations, fixed: the free-tier catalogue (if any) has already
    replaced ``request.tools`` one layer up; the name codec is chosen here and
    encodes the names; the dialect sweeps the encoded tools; the sender then
    applies anything this host has *learned* to refuse, and the reactive rung
    answers whatever is left.

    Key order is fixed and deliberate: the body is recorded verbatim in the
    request log and compared byte for byte against a captured reference, so
    "the same request" has to serialise the same way every time.
    """

    history_counts: dict[str, int] = {}
    reasoning_by_message: dict[int, list[dict[str, Any]]] | None = None
    replayed_blocks: set[tuple[int, int]] = set()
    if origin is not None:
        reasoning_by_message, replayed_blocks = _carrier_items(
            request.messages, origin, history_counts
        )
    if plaintext_replay:
        # The second producer, merged into the same mapping: carrier items
        # first, then the plaintext items, in block order within each group.
        # That order is deterministic, which is what makes the cache-prefix
        # property hold -- it is not a claim about which reads better.
        plaintext, plaintext_blocks = _plaintext_items(
            request.messages, history_counts, replayed_blocks
        )
        replayed_blocks = replayed_blocks | plaintext_blocks
        if plaintext:
            merged = dict(reasoning_by_message or {})
            for index, items in plaintext.items():
                merged.setdefault(index, []).extend(items)
            reasoning_by_message = merged
    try:
        openai_messages = AnthropicToOpenAIConverter.convert_messages(
            request.messages,
            reasoning_replay=RESPONSES_REASONING_REPLAY,
            reasoning_by_message=reasoning_by_message,
        )
    except OpenAIConversionError as exc:
        raise InvalidRequestError(str(exc)) from exc
    if wire_notes is not None:
        wire_notes.update(
            _history_thinking_note(request.messages, replayed_blocks, plaintext_replay)
        )
        wire_notes.update(empty_system_messages_note(request.messages))
        if origin is not None:
            wire_notes.update(_carrier_note(history_counts))
        if plaintext_replay:
            wire_notes.update(_plaintext_note(history_counts))

    tool_names = responses_tool_name_codec(
        request, tool_name_max_length, tool_catalogue
    )
    instructions = _extract_system_instructions(request)
    stripped_system = bool(openai_messages) and (
        openai_messages[0].get("role") == "system"
    )
    _, chat_messages = _strip_openai_system_message(openai_messages)
    if stripped_system and reasoning_by_message:
        # ``convert_messages`` never emits a leading system message (an inline
        # Anthropic system turn becomes a user message), so this is defensive
        # rather than expected: if it ever did, the mapping is one index off
        # and every replayed item would land in front of the wrong turn.
        reasoning_by_message = {
            index - 1: items
            for index, items in reasoning_by_message.items()
            if index > 0
        }

    body: dict[str, Any] = {
        "model": request.model,
        "input": _openai_messages_to_responses_input(
            chat_messages, tool_names, reasoning_by_message
        ),
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

    envelope = request.responses_envelope if apply_responses_sidecars else None
    envelope_tools = (
        envelope.get("tools")
        if isinstance(envelope, Mapping) and isinstance(envelope.get("tools"), list)
        else None
    )
    _, custom_by_name = _envelope_tool_metadata(envelope_tools, tool_catalogue)
    converted = _convert_tools(
        request.tools,
        tool_names,
        tool_schema_dialect,
        envelope_tools=envelope_tools,
        tool_catalogue=tool_catalogue,
    )
    tools = converted.tools if converted is not None else None
    if converted is not None and wire_notes is not None:
        wire_notes.update(converted.wire_marker(tool_schema_dialect))
    if tools:
        body["tools"] = tools
        if include_tool_choice:
            tool_choice = _convert_tool_choice(
                request.tool_choice,
                tool_names,
                custom_tool_names=frozenset(custom_by_name),
            )
            if tool_choice is not None:
                body["tool_choice"] = tool_choice

    envelope_reasoning = (
        envelope.get("reasoning")
        if isinstance(envelope, Mapping)
        and isinstance(envelope.get("reasoning"), Mapping)
        else None
    )
    reasoning_block = _reasoning_block(
        reasoning,
        unspecified_effort=unspecified_effort,
        envelope_reasoning=envelope_reasoning,
        allow_context=allow_openai_responses_sidecars,
    )
    if reasoning_block is not None:
        body["reasoning"] = reasoning_block
        if include_encrypted_reasoning:
            # Required for stateless multi-turn reasoning: without it the
            # backend cannot carry encrypted reasoning across turns of a
            # ``store: false`` conversation.
            body["include"] = ["reasoning.encrypted_content"]

    if isinstance(envelope, Mapping):
        if isinstance(envelope.get("text"), Mapping):
            raw_text = envelope["text"]
            text_out: dict[str, Any] = {}
            if raw_text.get("format") is not None:
                text_out["format"] = deepcopy(raw_text["format"])
            if (
                allow_openai_responses_sidecars
                and raw_text.get("verbosity") is not None
            ):
                text_out["verbosity"] = raw_text["verbosity"]
            if text_out:
                body["text"] = text_out
        if allow_openai_responses_sidecars:
            if envelope.get("truncation") is not None:
                body["truncation"] = deepcopy(envelope["truncation"])
            service_tier = envelope.get("service_tier")
            if isinstance(service_tier, str) and service_tier:
                body["service_tier"] = service_tier
        raw_include = envelope.get("include")
        if isinstance(raw_include, list):
            merged_include: list[str] = list(body.get("include", ()))
            for item in raw_include:
                if not isinstance(item, str) or not item:
                    continue
                if (
                    not allow_openai_responses_sidecars
                    and item == "output.encrypted_content"
                ):
                    continue
                if (
                    not include_encrypted_reasoning
                    and item == "reasoning.encrypted_content"
                ):
                    continue
                if item not in merged_include:
                    merged_include.append(item)
            if merged_include:
                body["include"] = merged_include

    # Recorded beside the body it describes, at the one site both Responses
    # callers (``chatgpt_oauth`` and the OpenCode Responses transport) go
    # through, so the two cannot disagree about what an adaptive request was
    # sent. A no-op outside a tracked request.
    _note_adaptive_substitution(
        reasoning,
        request.model,
        sent_effort=(
            str(reasoning_block["effort"])
            if reasoning_block is not None and "effort" in reasoning_block
            else None
        ),
    )

    if extra_body:
        body.update({str(key): value for key, value in extra_body.items()})

    return body


def _is_const_oneof_matching_enum(one_of: Any, enum_values: Any) -> bool:
    if not isinstance(one_of, list) or not isinstance(enum_values, list) or not one_of:
        return False
    const_values: list[Any] = []
    for entry in one_of:
        if not isinstance(entry, Mapping) or "const" not in entry:
            return False
        const_values.append(entry["const"])
    return len(const_values) == len(enum_values) and all(
        value in enum_values for value in const_values
    )


def _clean_redundant_oneof_with_enum(node: Any) -> Any:
    """Drop redundant ``oneOf: [{const}]`` alongside matching ``enum`` copy-on-write."""
    if isinstance(node, list):
        rebuilt = [_clean_redundant_oneof_with_enum(item) for item in node]
        if all(new is old for new, old in zip(rebuilt, node, strict=True)):
            return node
        return rebuilt
    if not isinstance(node, Mapping):
        return node
    drop_oneof = _is_const_oneof_matching_enum(node.get("oneOf"), node.get("enum"))
    changed = drop_oneof
    out: dict[str, Any] = {}
    for key, value in node.items():
        if drop_oneof and key == "oneOf":
            continue
        cleaned = _clean_redundant_oneof_with_enum(value)
        if cleaned is not value:
            changed = True
        out[str(key)] = cleaned
    return out if changed else node


def _sanitize_passthrough_tool(
    tool: Any,
    *,
    tool_names: OpenAIToolNameCodec | None,
) -> Any:
    if not isinstance(tool, Mapping):
        return deepcopy(tool)
    copied = deepcopy(dict(tool))
    tool_type = copied.get("type")
    if tool_type == "namespace":
        nested = copied.get("tools")
        if isinstance(nested, list):
            copied["tools"] = [
                _sanitize_passthrough_tool(item, tool_names=None) for item in nested
            ]
        return copied
    if tool_type == "function":
        fn = copied.get("function")
        if isinstance(fn, dict):
            if tool_names is not None and isinstance(fn.get("name"), str):
                fn["name"] = tool_names.encode(fn["name"])
            if "parameters" in fn:
                fn["parameters"] = _clean_redundant_oneof_with_enum(fn["parameters"])
        else:
            if tool_names is not None and isinstance(copied.get("name"), str):
                copied["name"] = tool_names.encode(copied["name"])
            if "parameters" in copied:
                copied["parameters"] = _clean_redundant_oneof_with_enum(
                    copied["parameters"]
                )
        return copied
    if tool_type == "custom":
        custom = copied.get("custom")
        if isinstance(custom, dict):
            if tool_names is not None and isinstance(custom.get("name"), str):
                custom["name"] = tool_names.encode(custom["name"])
        elif tool_names is not None and isinstance(copied.get("name"), str):
            copied["name"] = tool_names.encode(copied["name"])
        return copied
    return copied


def _sweep_passthrough_tools(
    raw_tools: list[Any],
    *,
    tool_names: OpenAIToolNameCodec | None,
    tool_schema_dialect: ToolSchemaDialect,
    wire_notes: dict[str, str] | None,
) -> list[Any]:
    prepared = [
        _sanitize_passthrough_tool(tool, tool_names=tool_names) for tool in raw_tools
    ]
    top_sweep = sweep_tool_catalogue(prepared, tool_schema_dialect)
    result = list(top_sweep.tools)
    all_removals = list(top_sweep.removals)
    all_applied = list(top_sweep.applied)
    all_translations = list(top_sweep.translations)

    for index, tool in enumerate(result):
        if not isinstance(tool, Mapping) or tool.get("type") != "namespace":
            continue
        nested = tool.get("tools")
        if not isinstance(nested, list) or not nested:
            continue
        ns_sweep = sweep_tool_catalogue(nested, tool_schema_dialect)
        if ns_sweep.tools is not nested:
            updated_ns = dict(tool)
            updated_ns["tools"] = list(ns_sweep.tools)
            result[index] = updated_ns
        all_removals.extend(ns_sweep.removals)
        all_applied.extend(ns_sweep.applied)
        all_translations.extend(ns_sweep.translations)

    if wire_notes is not None and (all_removals or all_translations):
        combined_sweep = DialectSweep(
            tools=result,
            removals=tuple(all_removals),
            applied=tuple(dict.fromkeys(all_applied)),
            translations=tuple(all_translations),
        )
        wire_notes.update(combined_sweep.wire_marker(tool_schema_dialect))
    return result


def _sanitize_passthrough_reasoning_item(
    item: Mapping[str, Any],
    *,
    origin: ReplayOrigin | None,
    plaintext_replay: bool,
    drop_reasoning_items: bool,
    history_counts: dict[str, int],
) -> dict[str, Any] | None:
    if drop_reasoning_items:
        return None
    encrypted = item.get("encrypted_content")
    if isinstance(encrypted, str) and encrypted:
        if is_carrier(encrypted):
            native = _decode_accepted_carrier(encrypted, origin, history_counts)
            if native is None:
                if plaintext_replay:
                    summary = item.get("summary")
                    if isinstance(summary, list) and summary:
                        return {"type": "reasoning", "summary": deepcopy(summary)}
                return None
            sanitized = deepcopy(native)
            if sanitized.get("content") != []:
                sanitized.pop("content", None)
            history_counts[HISTORY_CARRIER_REPLAYED] = (
                history_counts.get(HISTORY_CARRIER_REPLAYED, 0) + 1
            )
            return sanitized
        sanitized = deepcopy(dict(item))
        # Strip non-empty reasoning_text content blocks (maxItems: 0 on Codex input[]).
        if sanitized.get("content") != []:
            sanitized.pop("content", None)
        return sanitized

    summary = item.get("summary")
    if isinstance(summary, list) and summary:
        return {"type": "reasoning", "summary": deepcopy(summary)}
    return None


def _sanitize_passthrough_input(
    raw_input: Any,
    *,
    tool_names: OpenAIToolNameCodec | None,
    origin: ReplayOrigin | None,
    plaintext_replay: bool,
    drop_reasoning_items: bool,
    history_counts: dict[str, int],
) -> list[dict[str, Any]]:
    if not isinstance(raw_input, list):
        return []
    sanitized_items: list[dict[str, Any]] = []
    for raw_item in raw_input:
        if not isinstance(raw_item, Mapping):
            continue
        item_type = raw_item.get("type")
        if item_type == "reasoning":
            cleaned = _sanitize_passthrough_reasoning_item(
                raw_item,
                origin=origin,
                plaintext_replay=plaintext_replay,
                drop_reasoning_items=drop_reasoning_items,
                history_counts=history_counts,
            )
            if cleaned is not None:
                sanitized_items.append(cleaned)
            continue
        copied = deepcopy(dict(raw_item))
        if item_type == "message" or (
            "role" in copied
            and item_type
            not in {
                "compaction",
                "function_call",
                "function_call_output",
                "custom_tool_call",
                "custom_tool_call_output",
                "local_shell_call",
                "local_shell_call_output",
                "web_search_call",
                "additional_tools",
                "configuration_update",
            }
        ):
            copied.pop("encrypted_content", None)
            if copied.get("role") == "system":
                copied["role"] = "developer"
            sanitized_items.append(copied)
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            name = copied.get("name")
            if (
                tool_names is not None
                and isinstance(name, str)
                and not copied.get("namespace")
            ):
                copied["name"] = tool_names.encode(name)
            sanitized_items.append(copied)
            continue
        sanitized_items.append(copied)
    return sanitized_items


def build_responses_passthrough_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
    store: bool = False,
    stream: bool = True,
    parallel_tool_calls: bool = False,
    prompt_cache_key: str | None = None,
    tool_name_max_length: int | None = None,
    tool_schema_dialect: ToolSchemaDialect = RESPONSES_TOOL_SCHEMA_DIALECT,
    wire_notes: dict[str, str] | None = None,
    origin: ReplayOrigin | None = None,
    plaintext_replay: bool = False,
    unspecified_effort: str | None = None,
    drop_reasoning_items: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    """Reconstruct a Codex Responses body from ``request.responses_envelope``.

    Preserves native Codex CLI structures (separate ``instructions``, in-order
    ``role: "developer"`` items, ``type: "custom"`` and ``namespace`` tools,
    ``compaction``/``local_shell_call*``/``additional_tools``, ``text``,
    ``service_tier``, ``client_metadata``, ``reasoning.{summary,context}``)
    while respecting MCC model routing, ``ReasoningPolicy``, schema dialect,
    tool-name aliasing, carrier provenance, and Codex parameter sanitization.
    """
    envelope = request.responses_envelope
    if not isinstance(envelope, Mapping) or "input" not in envelope:
        fallback_body = build_responses_request_body(
            request,
            reasoning=reasoning,
            store=store,
            stream=stream,
            parallel_tool_calls=parallel_tool_calls,
            prompt_cache_key=prompt_cache_key,
            tool_name_max_length=tool_name_max_length,
            tool_schema_dialect=tool_schema_dialect,
            wire_notes=wire_notes,
            origin=origin,
            plaintext_replay=plaintext_replay,
            unspecified_effort=unspecified_effort,
            apply_responses_sidecars=isinstance(envelope, Mapping),
            allow_openai_responses_sidecars=True,
        )
        fallback_body.pop("truncation", None)
        return fallback_body

    tool_names = responses_tool_name_codec(
        request, tool_name_max_length, EMPTY_TOOL_CATALOGUE
    )
    history_counts: dict[str, int] = {}
    sanitized_input = _sanitize_passthrough_input(
        envelope.get("input"),
        tool_names=tool_names,
        origin=origin,
        plaintext_replay=plaintext_replay,
        drop_reasoning_items=drop_reasoning_items,
        history_counts=history_counts,
    )
    if wire_notes is not None and history_counts:
        wire_notes.update(_carrier_note(history_counts))

    caller_parallel = envelope.get("parallel_tool_calls")
    effective_parallel = (
        caller_parallel if isinstance(caller_parallel, bool) else parallel_tool_calls
    )

    body: dict[str, Any] = {
        "model": request.model,
        "input": sanitized_input,
    }
    if not compact:
        body["store"] = store
    body["stream"] = stream
    body["parallel_tool_calls"] = effective_parallel

    effective_cache_key = prompt_cache_key or envelope.get("prompt_cache_key")
    if isinstance(effective_cache_key, str) and effective_cache_key:
        body["prompt_cache_key"] = effective_cache_key

    instructions = envelope.get("instructions")
    if isinstance(instructions, str) and instructions:
        body["instructions"] = instructions

    raw_tools = envelope.get("tools")
    if isinstance(raw_tools, list) and raw_tools:
        body["tools"] = _sweep_passthrough_tools(
            raw_tools,
            tool_names=tool_names,
            tool_schema_dialect=tool_schema_dialect,
            wire_notes=wire_notes,
        )
    if "tool_choice" in envelope and envelope["tool_choice"] is not None:
        choice = deepcopy(envelope["tool_choice"])
        if (
            tool_names is not None
            and isinstance(choice, dict)
            and isinstance(choice.get("name"), str)
            and not choice.get("namespace")
        ):
            choice["name"] = tool_names.encode(choice["name"])
        body["tool_choice"] = choice

    if isinstance(envelope.get("text"), Mapping):
        body["text"] = deepcopy(dict(envelope["text"]))
    service_tier = envelope.get("service_tier")
    if isinstance(service_tier, str) and service_tier:
        body["service_tier"] = service_tier
    if isinstance(envelope.get("client_metadata"), Mapping):
        body["client_metadata"] = deepcopy(dict(envelope["client_metadata"]))

    envelope_reasoning = (
        envelope.get("reasoning")
        if isinstance(envelope.get("reasoning"), Mapping)
        else None
    )
    reasoning_block = _reasoning_block(
        reasoning,
        unspecified_effort=unspecified_effort,
        envelope_reasoning=envelope_reasoning,
        allow_context=True,
    )
    if reasoning_block is not None:
        body["reasoning"] = reasoning_block

    if not compact:
        include_list: list[str] = []
        raw_include = envelope.get("include")
        if isinstance(raw_include, list):
            for entry in raw_include:
                if isinstance(entry, str) and entry and entry not in include_list:
                    include_list.append(entry)
        if "reasoning.encrypted_content" not in include_list:
            include_list.append("reasoning.encrypted_content")
        body["include"] = include_list

    _note_adaptive_substitution(
        reasoning,
        request.model,
        sent_effort=(
            str(reasoning_block["effort"])
            if reasoning_block is not None and "effort" in reasoning_block
            else None
        ),
    )

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
        # The protocol pairs a call with its ``function_call_output`` by
        # ``call_id``; the item ``id`` identifies the output item and pairs
        # nothing. The client echoes this value back as ``tool_use_id``, and
        # that value becomes both the replayed ``function_call.call_id`` and
        # the ``function_call_output``'s ``call_id``, so it has to be the
        # ``call_id``. Same helper, same answer as the streaming converter.
        "id": call_id_from_item(item),
        "name": name,
        "input": input_data,
    }
