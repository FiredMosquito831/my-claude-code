"""Convert Chat Completions requests into Anthropic Messages payloads."""

import base64
import binascii
from collections.abc import Mapping, Sequence
from typing import Any

from my_claude_code.core.anthropic.structured_output import (
    StructuredOutputValidationError,
    has_tool_history,
    parse_chat_response_format,
    parse_pdf_data_url,
    synthesize_missing_history_tools,
)
from my_claude_code.core.carrier_surface import (
    CHAT_DETAILS_CARRIER,
    CHAT_DETAILS_OTHER,
    note,
)
from my_claude_code.core.openai_responses import is_carrier
from my_claude_code.core.trace import trace_event

from .errors import ChatCompletionsConversionError
from .models import ChatCompletionMessage, OpenAIChatCompletionRequest
from .tools import (
    convert_tool_choice,
    convert_tools,
    extract_chat_web_search_spec,
    parse_arguments,
    tool_choice_forbids_tools,
)

#: Fields a client may send that this proxy cannot honour and will not fail
#: for. Sampling penalties and seeds have no Anthropic equivalent at all, and
#: refusing a request over one would break clients that send them by default;
#: dropping them silently would make an unreproducible answer look like a bug
#: in the model. So they are dropped *and* recorded.
IGNORED_REQUEST_FIELDS: tuple[str, ...] = (
    "frequency_penalty",
    "logprobs",
    "presence_penalty",
    "seed",
    "top_logprobs",
)

_DATA_URL_PREFIX = "data:"


def convert_request_to_anthropic_payload(
    request: OpenAIChatCompletionRequest,
    *,
    carrier_surface: dict[str, int] | None = None,
    allow_web_search: bool = False,
) -> dict[str, Any]:
    """Convert a Chat Completions request into an Anthropic Messages payload.

    The result is always ``stream: True``: MCC's internal pipeline is SSE end
    to end, and a non-streaming client is served by assembling that stream at
    the API boundary, exactly as ``/v1/messages`` already does.

    ``carrier_surface`` is a caller's mapping to receive what this door drops
    (8.20.0): every ``reasoning_details`` entry a client sent, split into the
    ones carrying an ``mcc:reasoning:v1:`` envelope and the ones that are
    another dialect's text. Counts only, never content -- the rule
    ``carrier_foreign_note`` keeps. ``None``, which is every caller but the
    Chat handler, records nothing and changes nothing.
    """
    if request.n is not None and request.n != 1:
        raise ChatCompletionsConversionError(
            "MCC serves one choice per request; n must be 1 or omitted.",
            param="n",
        )

    _trace_ignored_fields(request)

    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    pending_tool_call_ids: set[str] = set()
    for message in request.messages:
        if carrier_surface is not None:
            _note_reasoning_details(message, carrier_surface)
        _append_message(
            message,
            messages=messages,
            system_parts=system_parts,
            pending_tool_call_ids=pending_tool_call_ids,
        )

    try:
        structured_spec, output_fmt = parse_chat_response_format(
            request.response_format
        )
    except StructuredOutputValidationError as exc:
        raise ChatCompletionsConversionError(exc.message, param=exc.param) from exc

    if not messages:
        raise ChatCompletionsConversionError(
            "messages must contain at least one user or assistant turn",
            param="messages",
        )

    payload: dict[str, Any] = {
        "model": _required_model(request.model),
        "messages": messages,
        "stream": True,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    # ``max_completion_tokens`` is the field OpenAI moved to; when a client
    # sends both, the newer one is the one it means.
    max_tokens = (
        request.max_completion_tokens
        if request.max_completion_tokens is not None
        else request.max_tokens
    )
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if stop_sequences := _stop_sequences(request.stop):
        payload["stop_sequences"] = stop_sequences
    if metadata := _metadata(request):
        payload["metadata"] = metadata
    if output_config := _output_config(request.reasoning_effort, request.reasoning):
        payload["output_config"] = output_config
    elif thinking := _reasoning_budget_thinking(request.reasoning):
        payload["thinking"] = thinking

    if structured_spec is not None:
        payload["structured_output_spec"] = structured_spec
    if output_fmt is not None:
        payload.setdefault("output_config", {})["format"] = output_fmt

    if allow_web_search:
        extract_chat_web_search_spec(
            request.tools,
            request.tool_choice,
            request.web_search_options,
            allow_web_search=True,
        )

    tools = convert_tools(request.tools, allow_web_search=allow_web_search)
    forbids_tools = tool_choice_forbids_tools(request.tool_choice)
    history_has_tools = has_tool_history(messages)
    if forbids_tools and history_has_tools:
        payload["tools"] = synthesize_missing_history_tools(messages, tools)
        payload["tool_choice"] = {"type": "none"}
    elif tools and not forbids_tools:
        payload["tools"] = tools
        tool_choice = convert_tool_choice(
            request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
            allow_web_search=allow_web_search,
        )
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    return payload


def _note_reasoning_details(
    message: ChatCompletionMessage, counts: dict[str, int]
) -> None:
    """Count the ``reasoning_details`` this door cannot forward (8.20.0).

    The field is declared nowhere on :class:`ChatCompletionMessage`, so
    ``extra="allow"`` swallows it into ``model_extra`` and nothing reads it --
    the drop this count exists to make visible. Read here rather than in a
    second pass so the count and the conversion cannot disagree about what the
    client sent: this is the same message object the conversion walks.

    Two cells, not one, because they are two different facts. A ``data`` that
    passes :func:`~my_claude_code.core.openai_responses.is_carrier` is the
    opaque envelope a carrier-replay build would have replayed; anything else
    is another dialect's text detail, which this door has never forwarded
    either. Neither is counted for a message that carries no such field.
    """

    extra = getattr(message, "model_extra", None)
    if not isinstance(extra, Mapping):
        return
    details = extra.get("reasoning_details")
    if not _is_sequence(details):
        return
    for detail in details:
        data = detail.get("data") if isinstance(detail, Mapping) else None
        note(
            counts,
            CHAT_DETAILS_CARRIER if is_carrier(data) else CHAT_DETAILS_OTHER,
        )


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def _required_model(value: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise ChatCompletionsConversionError(
        "model must be a non-empty string", param="model"
    )


def _append_message(
    message: ChatCompletionMessage,
    *,
    messages: list[dict[str, Any]],
    system_parts: list[str],
    pending_tool_call_ids: set[str],
) -> None:
    role = message.role
    if role in {"system", "developer"}:
        if text := _content_as_text(message.content):
            system_parts.append(text)
        return
    if role == "tool":
        _append_tool_result(
            message,
            messages,
            pending_tool_call_ids=pending_tool_call_ids,
        )
        return
    if role == "user":
        messages.append(
            {"role": "user", "content": _convert_message_content(message.content)}
        )
        return
    if role == "assistant":
        _append_assistant(
            message,
            messages,
            pending_tool_call_ids=pending_tool_call_ids,
        )
        return
    raise ChatCompletionsConversionError(
        f"Unsupported message role: {role!r}", param="messages"
    )


def _append_assistant(
    message: ChatCompletionMessage,
    messages: list[dict[str, Any]],
    *,
    pending_tool_call_ids: set[str],
) -> None:
    blocks: list[dict[str, Any]] = []
    content = _convert_message_content(message.content, allow_empty=True)
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    else:
        blocks.extend(content)
    for call in message.tool_calls or ():
        tool_block = _tool_use_block(call)
        pending_tool_call_ids.add(tool_block["id"])
        blocks.append(tool_block)

    if not blocks and message.reasoning_content is None:
        # An assistant turn with neither text nor a tool call carries nothing
        # a model can condition on; keeping it would send an empty content
        # array upstream, which several providers reject outright.
        return
    anthropic_message: dict[str, Any] = {"role": "assistant", "content": blocks}
    if message.reasoning_content is not None:
        anthropic_message["reasoning_content"] = message.reasoning_content
    messages.append(anthropic_message)


def _tool_use_block(call: Any) -> dict[str, Any]:
    if not isinstance(call, Mapping):
        raise ChatCompletionsConversionError(
            f"Unsupported tool_calls entry: {type(call).__name__}", param="messages"
        )
    function = call.get("function")
    source = function if isinstance(function, Mapping) else call
    name = source.get("name")
    if not isinstance(name, str) or not name:
        raise ChatCompletionsConversionError(
            "tool_calls[].function.name must be a non-empty string", param="messages"
        )
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise ChatCompletionsConversionError(
            f"tool_calls[].id is required for tool call {name!r}", param="messages"
        )
    return {
        "type": "tool_use",
        "id": call_id,
        "name": name,
        "input": parse_arguments(source.get("arguments"), tool_name=name),
    }


def _append_tool_result(
    message: ChatCompletionMessage,
    messages: list[dict[str, Any]],
    *,
    pending_tool_call_ids: set[str],
) -> None:
    tool_call_id = message.tool_call_id
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ChatCompletionsConversionError(
            "A tool message requires tool_call_id", param="messages"
        )
    if tool_call_id not in pending_tool_call_ids:
        return
    pending_tool_call_ids.discard(tool_call_id)
    block = {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": _tool_result_content(message.content),
    }
    # Consecutive tool results belong to one user turn, the way Anthropic
    # expects them: OpenAI sends one message per result, Anthropic one message
    # holding every result for the assistant turn that requested them.
    last = messages[-1] if messages else None
    if (
        last is not None
        and last.get("role") == "user"
        and isinstance(last.get("content"), list)
        and last["content"]
        and all(
            isinstance(existing, dict) and existing.get("type") == "tool_result"
            for existing in last["content"]
        )
    ):
        last["content"].append(block)
        return
    messages.append({"role": "user", "content": [block]})


def _convert_message_content(
    content: Any, *, allow_empty: bool = False
) -> str | list[dict[str, Any]]:
    if content is None:
        if allow_empty:
            return ""
        raise ChatCompletionsConversionError(
            "message content must not be null", param="messages"
        )
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ChatCompletionsConversionError(
            f"Unsupported message content: {type(content).__name__}", param="messages"
        )

    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, Mapping):
            raise ChatCompletionsConversionError(
                f"Unsupported content part: {type(part).__name__}", param="messages"
            )
        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            blocks.append({"type": "text", "text": _part_text(part)})
            continue
        if part_type == "image_url":
            blocks.append(_image_block(part))
            continue
        if part_type == "file":
            blocks.append(_file_block(part))
            continue
        if part_type == "refusal":
            blocks.append({"type": "text", "text": str(part.get("refusal", ""))})
            continue
        raise ChatCompletionsConversionError(
            f"Unsupported content part type: {part_type!r}. "
            "MCC accepts text, image_url, and file parts on /v1/chat/completions.",
            param="messages",
        )
    return blocks


def _image_block(part: Mapping[str, Any]) -> dict[str, Any]:
    image_url = part.get("image_url")
    source = image_url if isinstance(image_url, Mapping) else {}
    url = source.get("url") if source else image_url
    if not isinstance(url, str) or not url:
        raise ChatCompletionsConversionError(
            "image_url.url must be a non-empty string", param="messages"
        )
    detail = source.get("detail") if source else part.get("detail")
    if url.startswith(_DATA_URL_PREFIX):
        block: dict[str, Any] = {"type": "image", "source": _data_url_source(url)}
    else:
        block = {"type": "image", "source": {"type": "url", "url": url}}
    if isinstance(detail, str) and detail in {"low", "high"}:
        block["detail"] = detail
    return block


def _file_block(part: Mapping[str, Any]) -> dict[str, Any]:
    file_obj = part.get("file")
    if not isinstance(file_obj, Mapping):
        raise ChatCompletionsConversionError(
            "file content part requires a file object", param="messages"
        )
    file_data = file_obj.get("file_data")
    if not isinstance(file_data, str) or not file_data:
        file_id = file_obj.get("file_id")
        if isinstance(file_id, str) and file_id:
            raise ChatCompletionsConversionError(
                f"file_id {file_id!r} is not supported: MCC has no Files API to read "
                "it from. Send the file inline as file.file_data.",
                param="messages",
            )
        raise ChatCompletionsConversionError(
            "file.file_data must be a non-empty base64 PDF data URL",
            param="messages",
        )
    try:
        media_type, data = parse_pdf_data_url(file_data, error_context="file.file_data")
    except StructuredOutputValidationError as exc:
        raise ChatCompletionsConversionError(exc.message, param=exc.param) from exc
    block: dict[str, Any] = {
        "type": "document",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }
    filename = file_obj.get("filename")
    if isinstance(filename, str) and filename.strip():
        block["title"] = filename.strip()
    return block


def _tool_result_content(content: Any) -> str | list[dict[str, Any]]:
    converted = _convert_message_content(content, allow_empty=True)
    if isinstance(converted, str):
        return converted
    has_media = any(block.get("type") in {"image", "document"} for block in converted)
    if not has_media:
        return "\n".join(
            str(block.get("text", ""))
            for block in converted
            if block.get("type") == "text"
        )
    return converted


def _data_url_source(url: str) -> dict[str, Any]:
    header, separator, data = url[len(_DATA_URL_PREFIX) :].partition(",")
    if not separator:
        raise ChatCompletionsConversionError(
            "image_url.url is a malformed data URL", param="messages"
        )
    if not header.endswith(";base64"):
        raise ChatCompletionsConversionError(
            "Only base64 data URLs are supported for image_url", param="messages"
        )
    media_type = header[: -len(";base64")] or "image/png"
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChatCompletionsConversionError(
            "image_url data URL payload is not valid base64", param="messages"
        ) from exc
    return {"type": "base64", "media_type": media_type, "data": data}


def _content_as_text(content: Any) -> str:
    converted = _convert_message_content(content, allow_empty=True)
    if isinstance(converted, str):
        return converted
    return "\n".join(
        str(block.get("text", "")) for block in converted if block.get("type") == "text"
    )


def _part_text(part: Mapping[str, Any]) -> str:
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _stop_sequences(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    raise ChatCompletionsConversionError(
        "stop must be a string or a list of strings", param="stop"
    )


def _metadata(request: OpenAIChatCompletionRequest) -> dict[str, Any]:
    metadata: dict[str, Any] = dict(request.metadata or {})
    if request.user:
        # Anthropic's own documented metadata field for the end user.
        metadata.setdefault("user_id", request.user)
    return metadata


def _output_config(
    reasoning_effort: str | None,
    reasoning: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Preserve the client's named effort for application-level resolution.

    Identical to what the Responses adapter does with ``reasoning.effort``:
    the *name* travels, and the routing layer alone decides what budget it
    means for the model that ends up answering. Top-level ``reasoning_effort``
    outranks nested ``reasoning.effort`` when both are provided.
    """
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        return {"effort": reasoning_effort.strip().lower()}
    if isinstance(reasoning, Mapping):
        nested_effort = reasoning.get("effort")
        if isinstance(nested_effort, str) and nested_effort.strip():
            return {"effort": nested_effort.strip().lower()}
    return None


def _reasoning_budget_thinking(
    reasoning: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Map OpenRouter/Responses-style ``reasoning.max_tokens`` to ``thinking``."""
    if not isinstance(reasoning, Mapping):
        return None
    raw_max_tokens = reasoning.get("max_tokens")
    if (
        isinstance(raw_max_tokens, int)
        and not isinstance(raw_max_tokens, bool)
        and raw_max_tokens > 0
    ):
        return {"type": "enabled", "budget_tokens": raw_max_tokens}
    return None


def _trace_ignored_fields(request: OpenAIChatCompletionRequest) -> None:
    ignored = [
        field
        for field in IGNORED_REQUEST_FIELDS
        if getattr(request, field, None) is not None
    ]
    if not ignored:
        return
    trace_event(
        stage="chat_completions",
        event="chat_completions.input.unsupported_fields_ignored",
        source="openai_chat_completions",
        model=request.model,
        fields=ignored,
    )
