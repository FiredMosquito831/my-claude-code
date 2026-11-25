"""Convert Gemini ``generateContent`` requests into Anthropic Messages payloads."""

import base64
import binascii
import contextlib
import json
from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

from my_claude_code.core.anthropic import (
    attach_structured_output_schema,
    has_tool_history,
    synthesize_missing_history_tools,
)
from my_claude_code.core.carrier_surface import (
    GEMINI_THOUGHT_PART,
    GEMINI_THOUGHT_SIGNATURE,
    note,
)

from .errors import GeminiConversionError
from .models import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiThinkingConfig,
)
from .tools import convert_tool_config, convert_tools, normalize_schema

#: Request fields a client may send that this proxy cannot honour and will not
#: fail for. ``safetySettings`` has no Anthropic equivalent at all and
#: ``cachedContent`` names a Google-side cache object that does not exist here;
#: refusing a request over either would break Gemini CLI, which sends
#: ``safetySettings`` by default. So they are dropped *and* recorded.
IGNORED_REQUEST_FIELDS: tuple[str, ...] = ("safety_settings", "cached_content")

#: ``generationConfig`` extra fields accepted by Gemini REST that MCC does not
#: forward across the IR boundary, traced in ``conversion.dropped``.
IGNORED_GENERATION_CONFIG_FIELDS: tuple[tuple[str, str], ...] = (
    ("seed", "generationConfig.seed"),
    ("presencePenalty", "generationConfig.presencePenalty"),
    ("presence_penalty", "generationConfig.presencePenalty"),
    ("frequencyPenalty", "generationConfig.frequencyPenalty"),
    ("frequency_penalty", "generationConfig.frequencyPenalty"),
    ("responseLogprobs", "generationConfig.responseLogprobs"),
    ("response_logprobs", "generationConfig.responseLogprobs"),
    ("logprobs", "generationConfig.logprobs"),
    ("mediaResolution", "generationConfig.mediaResolution"),
    ("media_resolution", "generationConfig.mediaResolution"),
)

#: ``inlineData`` mime types Anthropic takes as a document rather than an
#: image. Everything else with an ``image/`` prefix is an image block; anything
#: outside both sets is a 400 naming the type, because silently dropping a
#: user's attachment is the one failure they cannot see.
_DOCUMENT_MIME_TYPES = frozenset({"application/pdf"})


class GeminiConversion:
    """The Anthropic payload for one Gemini request, plus what was dropped.

    The dropped list is not decoration. A Gemini client that enables
    ``googleSearch`` and gets no search has to be able to find out why, and the
    only place that can say so is the request that dropped it -- so the handler
    traces this list and the request log carries the trace.

    ``carrier_surface`` is the sibling count (8.20.0): every replayed thought
    part and every ``thoughtSignature`` this door cannot carry forward, by
    cell. Counts only, never content, and empty for a request that carried
    neither -- so a row that dropped nothing records exactly the ``params`` it
    always did.
    """

    __slots__ = (
        "carrier_surface",
        "dropped",
        "include_thoughts",
        "payload",
        "preserve_thought_signatures",
    )

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        dropped: list[str],
        include_thoughts: bool,
        carrier_surface: dict[str, int] | None = None,
        preserve_thought_signatures: bool = False,
    ) -> None:
        self.payload = payload
        self.dropped = dropped
        #: Whether the client asked for thought parts back
        #: (``thinkingConfig.includeThoughts``). Gemini defaults it to false,
        #: and a client that did not ask must not be sent thoughts it will
        #: render as ordinary answer text.
        self.include_thoughts = include_thoughts
        #: The carrier-surface cells this request dropped, or ``{}``.
        self.carrier_surface = carrier_surface if carrier_surface is not None else {}
        #: Whether non-``functionCall`` thought parts/signatures were preserved
        #: in ``gemini_envelope`` instead of counted as dropped.
        self.preserve_thought_signatures = preserve_thought_signatures


def convert_request_to_anthropic_payload(
    request: GeminiGenerateContentRequest,
    *,
    allow_web_search: bool = False,
    preserve_thought_signatures: bool = False,
) -> GeminiConversion:
    """Convert a ``GenerateContentRequest`` into an Anthropic Messages payload.

    The result is always ``stream: True``: MCC's internal pipeline is SSE end
    to end, and ``:generateContent`` is served by assembling that stream at the
    API boundary, exactly as ``/v1/messages`` already does for a non-streaming
    client.
    """

    dropped = [
        field
        for field in IGNORED_REQUEST_FIELDS
        if getattr(request, field, None) is not None
    ]
    carrier_surface: dict[str, int] = {}

    generation = request.generation_config
    _reject_multiple_candidates(generation)
    if generation is not None and isinstance(generation.model_extra, Mapping):
        ignored_gen = [
            trace_name
            for wire_key, trace_name in IGNORED_GENERATION_CONFIG_FIELDS
            if generation.model_extra.get(wire_key) is not None
        ]
        if ignored_gen:
            dropped.extend(dict.fromkeys(ignored_gen))

    gemini_envelope: dict[str, Any] = {}
    messages = _convert_contents(
        request.content_list,
        carrier_surface,
        preserve_thought_signatures=preserve_thought_signatures,
        gemini_envelope=gemini_envelope,
    )
    if not messages:
        raise GeminiConversionError(
            "contents must contain at least one user or model turn",
            field="contents",
        )

    payload: dict[str, Any] = {
        "model": _required_model(request.model),
        "messages": messages,
        "stream": True,
    }
    if gemini_envelope:
        payload["gemini_envelope"] = gemini_envelope

    system_parts: list[str] = []
    if instruction := _system_text(request.system_instruction):
        system_parts.append(instruction)
    _apply_response_format(payload, generation, system_parts=system_parts)

    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    _apply_generation_config(payload, generation)
    include_thoughts = _apply_thinking_config(payload, generation)

    tools, hosted = convert_tools(request.tools, allow_web_search=allow_web_search)
    dropped.extend(hosted)
    tool_names = {tool["name"] for tool in tools or ()}
    tool_choice, forbids, tool_config_dropped = convert_tool_config(
        request.tool_config, tool_names=tool_names
    )
    dropped.extend(tool_config_dropped)
    history_has_tools = has_tool_history(messages)
    if not forbids:
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
    elif history_has_tools:
        payload["tools"] = synthesize_missing_history_tools(messages, tools or [])
        payload["tool_choice"] = {"type": "none"}

    return GeminiConversion(
        payload,
        dropped=dropped,
        include_thoughts=include_thoughts,
        carrier_surface=carrier_surface,
        preserve_thought_signatures=preserve_thought_signatures,
    )


def _required_model(value: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise GeminiConversionError(
        "The model path segment must not be empty", field="model"
    )


def _reject_multiple_candidates(generation: GeminiGenerationConfig | None) -> None:
    if generation is None or generation.candidate_count is None:
        return
    if generation.candidate_count != 1:
        raise GeminiConversionError(
            "MCC serves one candidate per request; candidateCount must be 1 "
            "or omitted.",
            field="generationConfig.candidateCount",
        )


def _convert_contents(
    contents: list[GeminiContent],
    carrier_surface: dict[str, int],
    *,
    preserve_thought_signatures: bool = False,
    gemini_envelope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert ``contents`` into Anthropic messages.

    The ``functionCall`` id map exists because Gemini's function-call parts
    often carry **no id**. Anthropic pairs a ``tool_result`` to its
    ``tool_use`` by id and rejects a mismatch, so MCC mints a stable id per
    call and queues ids per function name (FIFO) so repeated calls to the same
    function pair cleanly in order.

    ``carrier_surface`` receives the ingress cells (8.20.0), counted at the
    very place that drops them so the count and the conversion cannot disagree.
    """

    messages: list[dict[str, Any]] = []
    call_ids: dict[str, deque[str]] = defaultdict(deque)
    tool_call_signatures: dict[str, str] = {}
    thought_parts: list[dict[str, Any]] = []
    counter = 0

    for index, content in enumerate(contents):
        role = _role(content.role)
        blocks: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for position, part in enumerate(content.parts or ()):
            if not isinstance(part, Mapping):
                raise GeminiConversionError(
                    f"Unsupported contents[{index}].parts entry: {type(part).__name__}",
                    field="contents",
                )
            block, is_result = _convert_part(
                part,
                index=index,
                position=position,
                call_ids=call_ids,
                counter=counter,
                carrier_surface=carrier_surface,
                preserve_thought_signatures=preserve_thought_signatures,
                tool_call_signatures=tool_call_signatures,
                thought_parts=thought_parts,
            )
            if block is None:
                continue
            if "type" in block and block["type"] == "tool_use":
                counter += 1
            if is_result:
                results.append(block)
            else:
                blocks.append(block)

        if results and blocks and role == "user":
            _append_tool_results(messages, [*results, *blocks])
        else:
            if results:
                _append_tool_results(messages, results)
            if blocks:
                messages.append({"role": role, "content": blocks})

    if gemini_envelope is not None:
        if tool_call_signatures:
            gemini_envelope["tool_call_signatures"] = tool_call_signatures
        if preserve_thought_signatures and thought_parts:
            gemini_envelope["thought_parts"] = thought_parts

    return messages


def _role(value: str | None) -> str:
    """Return the Anthropic role for a Gemini content role.

    Gemini says ``model`` where Anthropic says ``assistant``, and omits the
    role entirely on a single-turn request, which its own SDKs treat as
    ``user``.
    """

    if value is None or not value.strip():
        return "user"
    normalized = value.strip().lower()
    if normalized == "model":
        return "assistant"
    if normalized in {"user", "function", "tool"}:
        return "user"
    raise GeminiConversionError(
        f"Unsupported contents[].role: {value!r}. MCC accepts user and model.",
        field="contents",
    )


def _convert_part(
    part: Mapping[str, Any],
    *,
    index: int,
    position: int,
    call_ids: dict[str, deque[str]],
    counter: int,
    carrier_surface: dict[str, int],
    preserve_thought_signatures: bool = False,
    tool_call_signatures: dict[str, str] | None = None,
    thought_parts: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(block, is_tool_result)`` for one Gemini part."""

    sig = _thought_signature_value(part)

    if call := _mapping(part, "functionCall", "function_call"):
        block = _function_call_block(call, call_ids=call_ids, counter=counter)
        if sig and tool_call_signatures is not None:
            tool_call_signatures[block["id"]] = sig
        return block, False

    if sig and not preserve_thought_signatures:
        note(carrier_surface, GEMINI_THOUGHT_SIGNATURE)

    if "text" in part:
        if part.get("thought") is True:
            if preserve_thought_signatures:
                if thought_parts is not None:
                    entry: dict[str, Any] = {
                        "index": index,
                        "position": position,
                        "text": part.get("text"),
                    }
                    if sig:
                        entry["thought_signature"] = sig
                    thought_parts.append(entry)
            else:
                note(carrier_surface, GEMINI_THOUGHT_PART)
            return None, False
        text = part.get("text")
        if not isinstance(text, str):
            raise GeminiConversionError(
                "contents[].parts[].text must be a string", field="contents"
            )
        if preserve_thought_signatures and sig and thought_parts is not None:
            thought_parts.append(
                {
                    "index": index,
                    "position": position,
                    "text": text,
                    "thought": False,
                    "thought_signature": sig,
                }
            )
        if not text:
            return None, False
        return {"type": "text", "text": text}, False

    if inline := _mapping(part, "inlineData", "inline_data"):
        return _inline_data_block(inline), False

    if file_data := _mapping(part, "fileData", "file_data"):
        return _file_data_block(file_data), False

    if response := _mapping(part, "functionResponse", "function_response"):
        return _function_response_block(response, call_ids=call_ids), True

    if "executableCode" in part or "codeExecutionResult" in part:
        raise GeminiConversionError(
            "MCC cannot replay Google code-execution parts; they are produced "
            "by a Google-hosted tool this proxy does not run.",
            field="contents",
        )

    raise GeminiConversionError(
        f"Unsupported contents[{index}].parts[{position}]: no text, inlineData, "
        "fileData, functionCall or functionResponse key.",
        field="contents",
    )


def _thought_signature_value(part: Mapping[str, Any]) -> str | None:
    """Return Gemini's non-empty ``thoughtSignature`` / ``thought_signature`` if any."""

    for key in ("thoughtSignature", "thought_signature"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _mapping(part: Mapping[str, Any], *keys: str) -> Mapping[str, Any] | None:
    for key in keys:
        value = part.get(key)
        if isinstance(value, Mapping):
            return value
        if value is not None:
            raise GeminiConversionError(
                f"contents[].parts[].{key} must be an object", field="contents"
            )
    return None


def _inline_data_block(inline: Mapping[str, Any]) -> dict[str, Any]:
    mime_type = inline.get("mimeType") or inline.get("mime_type")
    data = inline.get("data")
    if not isinstance(mime_type, str) or not mime_type:
        raise GeminiConversionError(
            "inlineData.mimeType must be a non-empty string", field="contents"
        )
    if not isinstance(data, str) or not data:
        raise GeminiConversionError(
            "inlineData.data must be a base64 string", field="contents"
        )
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GeminiConversionError(
            "inlineData.data is not valid base64", field="contents"
        ) from exc
    source = {"type": "base64", "media_type": mime_type, "data": data}
    if mime_type in _DOCUMENT_MIME_TYPES:
        return {"type": "document", "source": source}
    if mime_type.startswith("image/"):
        return {"type": "image", "source": source}
    raise GeminiConversionError(
        f"Unsupported inlineData.mimeType: {mime_type!r}. "
        "MCC accepts image/* and application/pdf.",
        field="contents",
    )


def _file_data_block(file_data: Mapping[str, Any]) -> dict[str, Any]:
    uri = file_data.get("fileUri") or file_data.get("file_uri")
    mime_type = file_data.get("mimeType") or file_data.get("mime_type")
    if not isinstance(uri, str) or not uri:
        raise GeminiConversionError(
            "fileData.fileUri must be a non-empty string", field="contents"
        )
    if not uri.startswith(("http://", "https://")):
        raise GeminiConversionError(
            "fileData.fileUri must be an http(s) URL. MCC has no Google Files "
            "API, so a files/ resource name cannot be resolved here.",
            field="contents",
        )
    if not isinstance(mime_type, str) or not mime_type.strip():
        raise GeminiConversionError(
            "fileData.mimeType must be a non-empty string", field="contents"
        )
    normalized_mime = mime_type.strip().lower()
    if normalized_mime in _DOCUMENT_MIME_TYPES:
        return {"type": "document", "source": {"type": "url", "url": uri}}
    if normalized_mime.startswith("image/"):
        return {"type": "image", "source": {"type": "url", "url": uri}}
    raise GeminiConversionError(
        f"Unsupported fileData.mimeType: {mime_type!r}. "
        "MCC accepts image/* and application/pdf.",
        field="contents",
    )


def _function_call_block(
    call: Mapping[str, Any], *, call_ids: dict[str, deque[str]], counter: int
) -> dict[str, Any]:
    name = call.get("name")
    if not isinstance(name, str) or not name:
        raise GeminiConversionError(
            "functionCall.name must be a non-empty string", field="contents"
        )
    args = call.get("args")
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise GeminiConversionError(
            f"functionCall.args for {name!r} must be an object", field="contents"
        )
    raw_id = call.get("id")
    call_id = raw_id if isinstance(raw_id, str) and raw_id else f"gemini_call_{counter}"
    call_ids[name].append(call_id)
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(args)}


def _function_response_block(
    response: Mapping[str, Any], *, call_ids: dict[str, deque[str]]
) -> dict[str, Any]:
    name = response.get("name")
    if not isinstance(name, str) or not name:
        raise GeminiConversionError(
            "functionResponse.name must be a non-empty string", field="contents"
        )
    raw_id = response.get("id")
    queue = call_ids.get(name)
    if isinstance(raw_id, str) and raw_id:
        tool_use_id = raw_id
        if queue:
            with contextlib.suppress(ValueError):
                queue.remove(raw_id)
    else:
        tool_use_id = queue.popleft() if queue else None
    if not tool_use_id:
        raise GeminiConversionError(
            f"functionResponse for {name!r} answers no functionCall in this "
            "conversation; Anthropic pairs a tool result to its call by id.",
            field="contents",
        )
    text = _response_text(response.get("response"))
    raw_parts = response.get("parts")
    if raw_parts is None:
        content: str | list[dict[str, Any]] = text
    else:
        if not isinstance(raw_parts, list):
            raise GeminiConversionError(
                "functionResponse.parts must be a list", field="contents"
            )
        extra_blocks: list[dict[str, Any]] = []
        extra_texts: list[str] = []
        has_media = False
        for part in raw_parts:
            if not isinstance(part, Mapping):
                raise GeminiConversionError(
                    f"Unsupported functionResponse.parts entry: {type(part).__name__}",
                    field="contents",
                )
            if inline := _mapping(part, "inlineData", "inline_data"):
                extra_blocks.append(_inline_data_block(inline))
                has_media = True
            elif file_data := _mapping(part, "fileData", "file_data"):
                extra_blocks.append(_file_data_block(file_data))
                has_media = True
            elif "text" in part:
                part_text = part.get("text")
                if not isinstance(part_text, str):
                    raise GeminiConversionError(
                        "functionResponse.parts[].text must be a string",
                        field="contents",
                    )
                if part_text:
                    extra_blocks.append({"type": "text", "text": part_text})
                    extra_texts.append(part_text)
        if has_media:
            combined: list[dict[str, Any]] = []
            if text:
                combined.append({"type": "text", "text": text})
            combined.extend(extra_blocks)
            content = combined
        elif extra_texts:
            content = (
                "\n".join([text, *extra_texts]) if text else "\n".join(extra_texts)
            )
        else:
            content = text
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }


def _response_text(value: Any) -> str:
    """Render a ``functionResponse.response`` object as tool-result text.

    Gemini's field is always an object; Anthropic's ``tool_result.content``
    is text or a block list. Compact JSON is the lossless rendering, and the
    one-key ``{"output": ...}`` and ``{"result": ...}`` wrappers Google's own
    SDK adds are unwrapped so the model sees the tool's answer rather than the
    envelope.
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and len(value) == 1:
        for key in ("output", "result", "content"):
            if key in value:
                inner = value[key]
                return (
                    inner
                    if isinstance(inner, str)
                    else json.dumps(inner, separators=(",", ":"))
                )
    return json.dumps(value, separators=(",", ":"), default=str)


def _append_tool_results(
    messages: list[dict[str, Any]], results: list[dict[str, Any]]
) -> None:
    """Fold tool results into one user turn, the way Anthropic expects them."""

    last = messages[-1] if messages else None
    if (
        last is not None
        and last.get("role") == "user"
        and isinstance(last.get("content"), list)
        and last["content"]
        and all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in last["content"]
        )
    ):
        last["content"].extend(results)
        return
    messages.append({"role": "user", "content": results})


def _system_text(value: GeminiContent | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    parts: list[str] = []
    for part in value.parts or ():
        if isinstance(part, Mapping):
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n\n".join(parts)


def _apply_generation_config(
    payload: dict[str, Any], generation: GeminiGenerationConfig | None
) -> None:
    if generation is None:
        return
    if generation.max_output_tokens is not None:
        payload["max_tokens"] = generation.max_output_tokens
    if generation.temperature is not None:
        payload["temperature"] = generation.temperature
    if generation.top_p is not None:
        payload["top_p"] = generation.top_p
    if generation.top_k is not None:
        payload["top_k"] = generation.top_k
    if stops := [
        value
        for value in (generation.stop_sequences or ())
        if isinstance(value, str) and value
    ]:
        payload["stop_sequences"] = stops


def _apply_thinking_config(
    payload: dict[str, Any], generation: GeminiGenerationConfig | None
) -> bool:
    """Translate ``thinkingConfig`` into MCC's reasoning intent.

    Google gives ``thinkingBudget`` two reserved values and both are intent
    rather than arithmetic: ``0`` means "do not think" and ``-1`` means "decide
    for yourself". They become Anthropic's ``disabled`` and ``adaptive``
    respectively, which is exactly what ``application/reasoning.py`` reads;
    any positive number travels as a real ``budget_tokens`` and the routing
    layer alone decides what the answering model can afford.

    Returns whether the client asked for thought parts back.
    """

    thinking: GeminiThinkingConfig | None = (
        generation.thinking_config if generation is not None else None
    )
    if thinking is None:
        return False
    effort = _thinking_level_effort(thinking.thinking_level)
    if effort is not None:
        if effort == "none":
            payload["thinking"] = {"type": "disabled", "enabled": False}
        else:
            payload["thinking"] = {"type": "adaptive"}
            payload.setdefault("output_config", {})["effort"] = effort
        return thinking.include_thoughts is True

    budget = thinking.thinking_budget
    if isinstance(budget, int) and not isinstance(budget, bool):
        if budget == 0:
            payload["thinking"] = {"type": "disabled", "enabled": False}
        elif budget < 0:
            payload["thinking"] = {"type": "adaptive"}
        else:
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif thinking.include_thoughts is True:
        # Asking for thoughts and naming no budget is still asking to think.
        payload["thinking"] = {"type": "enabled"}
    return thinking.include_thoughts is True


#: Gemini 3's ``thinkingLevel`` rungs -> MCC's own effort vocabulary. The
#: *name* travels and the routing layer alone decides what budget it means for
#: the model that ends up answering, exactly as ``reasoning_effort`` does on
#: the Chat Completions surface. ``OFF`` is Google's way of spelling "do not
#: think", which is the same intent as ``thinkingBudget: 0``.
THINKING_LEVEL_EFFORTS: dict[str, str] = {
    "OFF": "none",
    "NONE": "none",
    "MINIMAL": "minimal",
    "LOW": "low",
    "MEDIUM": "medium",
    "HIGH": "high",
    "XHIGH": "xhigh",
}


def _thinking_level_effort(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().upper()
    if normalized in {"THINKING_LEVEL_UNSPECIFIED", "UNSPECIFIED"}:
        return None
    try:
        return THINKING_LEVEL_EFFORTS[normalized]
    except KeyError:
        raise GeminiConversionError(
            f"Unsupported thinkingConfig.thinkingLevel: {value!r}. "
            "MCC accepts OFF, MINIMAL, LOW, MEDIUM, HIGH and XHIGH.",
            field="generationConfig.thinkingConfig.thinkingLevel",
        ) from None


def _apply_response_format(
    payload: dict[str, Any],
    generation: GeminiGenerationConfig | None,
    *,
    system_parts: list[str],
) -> None:
    """Map ``responseMimeType`` / ``responseSchema`` onto canonical structured output IR."""

    if generation is None:
        return
    schema = (
        generation.response_json_schema
        if generation.response_json_schema is not None
        else generation.response_schema
    )
    schema_field = (
        "generationConfig.responseJsonSchema"
        if generation.response_json_schema is not None
        else "generationConfig.responseSchema"
    )
    mime_type = generation.response_mime_type
    if schema is None and mime_type in (None, "", "text/plain"):
        return
    if schema is None:
        if mime_type in {"application/json", "application/x-ndjson"}:
            payload["structured_output_spec"] = {"type": "json_object"}
            return
        if mime_type == "text/x.enum":
            system_parts.append(
                "Respond with exactly one of the allowed enum values and nothing else."
            )
            return
        raise GeminiConversionError(
            f"Unsupported generationConfig.responseMimeType: {mime_type!r}. "
            "MCC accepts text/plain, application/json and text/x.enum.",
            field="generationConfig.responseMimeType",
        )
    if not isinstance(schema, Mapping):
        raise GeminiConversionError(
            f"{schema_field} must be an object",
            field=schema_field,
        )
    normalized = normalize_schema(schema)
    if mime_type == "text/x.enum":
        system_parts.append(
            "Respond with exactly one of the allowed enum values and nothing else. "
            f"Allowed schema:\n{json.dumps(normalized, sort_keys=True)}"
        )
        return
    if mime_type not in (None, "", "application/json", "application/x-ndjson"):
        raise GeminiConversionError(
            f"Unsupported generationConfig.responseMimeType: {mime_type!r}. "
            "MCC accepts text/plain, application/json and text/x.enum.",
            field="generationConfig.responseMimeType",
        )
    attach_structured_output_schema(
        payload, schema=normalized, name="structured_output"
    )
