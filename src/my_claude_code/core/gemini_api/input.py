"""Convert Gemini ``generateContent`` requests into Anthropic Messages payloads."""

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

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

_JSON_OBJECT_INSTRUCTION = (
    "Respond with a single valid JSON object and nothing else. "
    "Do not wrap it in Markdown code fences and do not add commentary."
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
    """

    __slots__ = ("dropped", "include_thoughts", "payload")

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        dropped: list[str],
        include_thoughts: bool,
    ) -> None:
        self.payload = payload
        self.dropped = dropped
        #: Whether the client asked for thought parts back
        #: (``thinkingConfig.includeThoughts``). Gemini defaults it to false,
        #: and a client that did not ask must not be sent thoughts it will
        #: render as ordinary answer text.
        self.include_thoughts = include_thoughts


def convert_request_to_anthropic_payload(
    request: GeminiGenerateContentRequest,
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

    generation = request.generation_config
    _reject_multiple_candidates(generation)

    messages = _convert_contents(request.content_list)
    if not messages:
        raise GeminiConversionError(
            "contents must contain at least one user or model turn",
            field="contents",
        )

    system_parts: list[str] = []
    if instruction := _system_text(request.system_instruction):
        system_parts.append(instruction)
    if schema_instruction := _response_format_instruction(generation):
        system_parts.append(schema_instruction)

    payload: dict[str, Any] = {
        "model": _required_model(request.model),
        "messages": messages,
        "stream": True,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    _apply_generation_config(payload, generation)
    include_thoughts = _apply_thinking_config(payload, generation)

    tools, hosted = convert_tools(request.tools)
    dropped.extend(hosted)
    tool_names = {tool["name"] for tool in tools or ()}
    tool_choice, forbids = convert_tool_config(
        request.tool_config, tool_names=tool_names
    )
    if tools and not forbids:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    return GeminiConversion(payload, dropped=dropped, include_thoughts=include_thoughts)


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


def _convert_contents(contents: list[GeminiContent]) -> list[dict[str, Any]]:
    """Convert ``contents`` into Anthropic messages.

    The ``functionCall`` id map exists because Gemini's function-call parts
    carry **no id**. Anthropic pairs a
    ``tool_result`` to its ``tool_use`` by id and rejects a mismatch, so MCC
    mints a stable id per call and remembers it by function name: a
    ``functionResponse`` for ``read_file`` is answered against the most recent
    unanswered ``read_file`` call, which is exactly the pairing Gemini's own
    ordering implies.
    """

    messages: list[dict[str, Any]] = []
    call_ids: dict[str, str] = {}
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
            )
            if block is None:
                continue
            if "type" in block and block["type"] == "tool_use":
                counter += 1
            if is_result:
                results.append(block)
            else:
                blocks.append(block)

        if results:
            _append_tool_results(messages, results)
        if blocks:
            messages.append({"role": role, "content": blocks})

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
    call_ids: dict[str, str],
    counter: int,
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(block, is_tool_result)`` for one Gemini part."""

    if "text" in part:
        if part.get("thought") is True:
            # A thought echoed back from a previous turn. Anthropic requires a
            # signature on a ``thinking`` block and rejects one without it, so
            # replaying it would fail the whole request; dropping it loses
            # nothing the model cannot re-derive.
            return None, False
        text = part.get("text")
        if not isinstance(text, str):
            raise GeminiConversionError(
                "contents[].parts[].text must be a string", field="contents"
            )
        if not text:
            return None, False
        return {"type": "text", "text": text}, False

    if inline := _mapping(part, "inlineData", "inline_data"):
        return _inline_data_block(inline), False

    if file_data := _mapping(part, "fileData", "file_data"):
        return _file_data_block(file_data), False

    if call := _mapping(part, "functionCall", "function_call"):
        return _function_call_block(call, call_ids=call_ids, counter=counter), False

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
    if isinstance(mime_type, str) and mime_type in _DOCUMENT_MIME_TYPES:
        return {"type": "document", "source": {"type": "url", "url": uri}}
    return {"type": "image", "source": {"type": "url", "url": uri}}


def _function_call_block(
    call: Mapping[str, Any], *, call_ids: dict[str, str], counter: int
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
    call_ids[name] = call_id
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(args)}


def _function_response_block(
    response: Mapping[str, Any], *, call_ids: dict[str, str]
) -> dict[str, Any]:
    name = response.get("name")
    if not isinstance(name, str) or not name:
        raise GeminiConversionError(
            "functionResponse.name must be a non-empty string", field="contents"
        )
    raw_id = response.get("id")
    tool_use_id = raw_id if isinstance(raw_id, str) and raw_id else call_ids.get(name)
    if not tool_use_id:
        raise GeminiConversionError(
            f"functionResponse for {name!r} answers no functionCall in this "
            "conversation; Anthropic pairs a tool result to its call by id.",
            field="contents",
        )
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": _response_text(response.get("response")),
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

    if effort := _thinking_level_effort(thinking.thinking_level):
        if effort == "none":
            payload["thinking"] = {"type": "disabled", "enabled": False}
        payload["output_config"] = {"effort": effort}
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
            "MCC accepts OFF, MINIMAL, LOW, MEDIUM and HIGH.",
            field="generationConfig.thinkingConfig.thinkingLevel",
        ) from None


def _response_format_instruction(
    generation: GeminiGenerationConfig | None,
) -> str | None:
    """Map ``responseMimeType`` / ``responseSchema`` onto the Anthropic mechanism.

    Anthropic has no server-enforced JSON mode, so the honest translation is
    an instruction the model can follow, appended to the system prompt --
    identical to what the Chat Completions adapter does with
    ``response_format``. A schema additionally travels verbatim, which is what
    makes the difference between "some JSON" and "this JSON".
    """

    if generation is None:
        return None
    schema = generation.response_json_schema or generation.response_schema
    mime_type = generation.response_mime_type
    if schema is None and mime_type in (None, "", "text/plain"):
        return None
    if schema is None:
        if mime_type in {"application/json", "application/x-ndjson"}:
            return _JSON_OBJECT_INSTRUCTION
        if mime_type == "text/x.enum":
            return (
                "Respond with exactly one of the allowed enum values and nothing else."
            )
        raise GeminiConversionError(
            f"Unsupported generationConfig.responseMimeType: {mime_type!r}. "
            "MCC accepts text/plain, application/json and text/x.enum.",
            field="generationConfig.responseMimeType",
        )
    if not isinstance(schema, Mapping):
        raise GeminiConversionError(
            "generationConfig.responseSchema must be an object",
            field="generationConfig.responseSchema",
        )
    normalized = normalize_schema(schema)
    return (
        f"{_JSON_OBJECT_INSTRUCTION} The object must validate against this "
        f"JSON Schema:\n{json.dumps(normalized, sort_keys=True)}"
    )
