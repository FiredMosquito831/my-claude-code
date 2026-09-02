"""Convert Chat Completions requests into Anthropic Messages payloads."""

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from my_claude_code.core.trace import trace_event

from .errors import ChatCompletionsConversionError
from .models import ChatCompletionMessage, OpenAIChatCompletionRequest
from .tools import (
    convert_tool_choice,
    convert_tools,
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

_JSON_OBJECT_INSTRUCTION = (
    "Respond with a single valid JSON object and nothing else. "
    "Do not wrap it in Markdown code fences and do not add commentary."
)

_DATA_URL_PREFIX = "data:"


def convert_request_to_anthropic_payload(
    request: OpenAIChatCompletionRequest,
) -> dict[str, Any]:
    """Convert a Chat Completions request into an Anthropic Messages payload.

    The result is always ``stream: True``: MCC's internal pipeline is SSE end
    to end, and a non-streaming client is served by assembling that stream at
    the API boundary, exactly as ``/v1/messages`` already does.
    """
    if request.n is not None and request.n != 1:
        raise ChatCompletionsConversionError(
            "MCC serves one choice per request; n must be 1 or omitted.",
            param="n",
        )

    _trace_ignored_fields(request)

    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        _append_message(message, messages=messages, system_parts=system_parts)

    if instruction := _response_format_instruction(request.response_format):
        system_parts.append(instruction)

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
    if (max_tokens := request.max_completion_tokens or request.max_tokens) is not None:
        payload["max_tokens"] = max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if stop_sequences := _stop_sequences(request.stop):
        payload["stop_sequences"] = stop_sequences
    if metadata := _metadata(request):
        payload["metadata"] = metadata
    if output_config := _output_config(request.reasoning_effort):
        payload["output_config"] = output_config

    tools = convert_tools(request.tools)
    if tools and not tool_choice_forbids_tools(request.tool_choice):
        payload["tools"] = tools
        tool_choice = convert_tool_choice(
            request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
        )
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    return payload


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
) -> None:
    role = message.role
    if role in {"system", "developer"}:
        if text := _content_as_text(message.content):
            system_parts.append(text)
        return
    if role == "tool":
        _append_tool_result(message, messages)
        return
    if role == "user":
        messages.append(
            {"role": "user", "content": _convert_message_content(message.content)}
        )
        return
    if role == "assistant":
        _append_assistant(message, messages)
        return
    raise ChatCompletionsConversionError(
        f"Unsupported message role: {role!r}", param="messages"
    )


def _append_assistant(
    message: ChatCompletionMessage, messages: list[dict[str, Any]]
) -> None:
    blocks: list[dict[str, Any]] = []
    content = _convert_message_content(message.content, allow_empty=True)
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    else:
        blocks.extend(content)
    blocks.extend(_tool_use_block(call) for call in message.tool_calls or ())

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
    message: ChatCompletionMessage, messages: list[dict[str, Any]]
) -> None:
    tool_call_id = message.tool_call_id
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ChatCompletionsConversionError(
            "A tool message requires tool_call_id", param="messages"
        )
    block = {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": _content_as_text(message.content),
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
        if part_type == "refusal":
            blocks.append({"type": "text", "text": str(part.get("refusal", ""))})
            continue
        raise ChatCompletionsConversionError(
            f"Unsupported content part type: {part_type!r}. "
            "MCC accepts text and image_url parts on /v1/chat/completions.",
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
    if url.startswith(_DATA_URL_PREFIX):
        return {"type": "image", "source": _data_url_source(url)}
    return {"type": "image", "source": {"type": "url", "url": url}}


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


def _output_config(reasoning_effort: str | None) -> dict[str, Any] | None:
    """Preserve the client's named effort for application-level resolution.

    Identical to what the Responses adapter does with ``reasoning.effort``:
    the *name* travels, and the routing layer alone decides what budget it
    means for the model that ends up answering.
    """
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        return None
    return {"effort": reasoning_effort.strip().lower()}


def _response_format_instruction(response_format: Any) -> str | None:
    """Map ``response_format`` onto the nearest Anthropic mechanism.

    Anthropic has no server-enforced JSON mode, so the honest translation is
    an instruction the model can follow, appended to the system prompt. A
    ``json_schema`` request additionally carries its schema verbatim, which is
    what makes the difference between "some JSON" and "this JSON".
    """
    if response_format is None:
        return None
    if not isinstance(response_format, Mapping):
        raise ChatCompletionsConversionError(
            "response_format must be an object", param="response_format"
        )
    format_type = response_format.get("type")
    if format_type in (None, "text"):
        return None
    if format_type == "json_object":
        return _JSON_OBJECT_INSTRUCTION
    if format_type == "json_schema":
        schema_holder = response_format.get("json_schema")
        if not isinstance(schema_holder, Mapping):
            raise ChatCompletionsConversionError(
                "response_format.json_schema must be an object",
                param="response_format",
            )
        schema = schema_holder.get("schema")
        if not isinstance(schema, Mapping):
            raise ChatCompletionsConversionError(
                "response_format.json_schema.schema must be an object",
                param="response_format",
            )
        name = schema_holder.get("name")
        label = f" named {name!r}" if isinstance(name, str) and name else ""
        return (
            f"{_JSON_OBJECT_INSTRUCTION} The object{label} must validate "
            f"against this JSON Schema:\n{json.dumps(schema, sort_keys=True)}"
        )
    raise ChatCompletionsConversionError(
        f"Unsupported response_format type: {format_type!r}. "
        "MCC accepts text, json_object and json_schema.",
        param="response_format",
    )


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
