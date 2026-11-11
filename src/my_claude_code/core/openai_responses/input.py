"""Convert OpenAI Responses requests into Anthropic Messages payloads."""

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from my_claude_code.core.trace import trace_event

from .errors import ResponsesConversionError
from .models import OpenAIResponsesRequest
from .reasoning import (
    combine_reasoning,
    reasoning_text_from_item,
    responses_reasoning_to_output_config,
)
from .tools import (
    call_id_from_item,
    convert_tool_choice,
    convert_tools,
    custom_tool_input_to_anthropic,
    optional_str,
    parse_arguments,
    required_str,
    responses_tool_name_to_anthropic_name,
)

#: How an inline image URL starts (the Chat Completions door's same constant).
_DATA_URL_PREFIX = "data:"


@dataclass(slots=True)
class _PendingReasoning:
    """What the reasoning items seen so far have left for the next turn.

    Two halves that travel together and are written in different places: the
    readable ``text`` (``reasoning_content``, since 7.x) and the opaque
    ``items`` (``encrypted_reasoning``, 8.19.0). One object rather than two
    parameters because every branch below either consumes both or neither, and
    two parallel ``str | None`` / ``list | None`` parameters is how one of them
    gets forgotten at a call site.

    Empty is the default and is what every branch returns after it has
    consumed the pending state, so "nothing pending" has one spelling.
    """

    text: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)


def convert_request_to_anthropic_payload(
    request: OpenAIResponsesRequest,
) -> dict[str, Any]:
    """Convert an OpenAI Responses request into an Anthropic Messages payload."""

    system_parts: list[str] = []
    if instructions := request.instructions:
        system_parts.append(instructions)

    messages: list[dict[str, Any]] = []
    pending = _PendingReasoning()
    quarantined_function_call_ids: set[str] = set()
    for item in _iter_input_items(request.input):
        pending = _append_input_item(
            item,
            messages=messages,
            system_parts=system_parts,
            pending=pending,
            quarantined_function_call_ids=quarantined_function_call_ids,
        )
    _append_pending_reasoning(messages, pending)

    if not messages:
        raise ResponsesConversionError("Responses request input must contain a message")

    payload: dict[str, Any] = {
        "model": required_str(request.model, "model"),
        "messages": messages,
        "stream": True,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens
    if request.metadata is not None:
        payload["metadata"] = request.metadata

    if output_config := responses_reasoning_to_output_config(request.reasoning):
        payload["output_config"] = output_config

    raw_tool_choice = request.tool_choice
    tools = convert_tools(request.tools)
    if tools and raw_tool_choice != "none":
        payload["tools"] = tools
    tool_choice = convert_tool_choice(raw_tool_choice)
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice

    return payload


def _append_input_item(
    item: Any,
    *,
    messages: list[dict[str, Any]],
    system_parts: list[str],
    pending: _PendingReasoning,
    quarantined_function_call_ids: set[str],
) -> _PendingReasoning:
    if isinstance(item, str):
        _append_pending_reasoning(messages, pending)
        messages.append({"role": "user", "content": item})
        return _PendingReasoning()
    if not isinstance(item, dict):
        raise ResponsesConversionError(
            f"Unsupported Responses input item: {type(item).__name__}"
        )

    item_type = item.get("type")
    if item_type in (None, "message") or "role" in item:
        role = required_str(item.get("role", "user"), "input.role")
        if role == "assistant":
            _append_message_item(
                role,
                item.get("content", ""),
                messages,
                system_parts,
                reasoning_content=pending.text,
                encrypted_reasoning=pending.items,
            )
            return _PendingReasoning()
        _append_pending_reasoning(messages, pending)
        _append_message_item(role, item.get("content", ""), messages, system_parts)
        return _PendingReasoning()
    if item_type in {"function_call", "custom_tool_call"}:
        namespace = optional_str(item.get("namespace"))
        field_name = f"{item_type}.name"
        name = required_str(item.get("name"), field_name)
        call_id = call_id_from_item(item)
        if item_type == "custom_tool_call":
            tool_input = custom_tool_input_to_anthropic(item.get("input"))
        else:
            try:
                tool_input = parse_arguments(item.get("arguments"))
            except ResponsesConversionError as exc:
                quarantined_function_call_ids.add(call_id)
                _trace_quarantined_function_call(call_id, exc)
                return pending
        tool_use = {
            "type": "tool_use",
            "id": call_id,
            "name": responses_tool_name_to_anthropic_name(name, namespace=namespace),
            "input": tool_input,
        }
        _append_tool_use_message(
            messages,
            tool_use,
            reasoning_content=pending.text,
            encrypted_reasoning=pending.items,
        )
        return _PendingReasoning()
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        call_id = call_id_from_item(item)
        if (
            item_type == "function_call_output"
            and call_id in quarantined_function_call_ids
        ):
            return pending
        _append_pending_reasoning_before_tool_output(messages, pending)
        _append_tool_result_message(
            messages,
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": _function_call_output_text(item.get("output", "")),
            },
        )
        return _PendingReasoning()
    if item_type == "reasoning":
        # The text half is what it has always been. The blob half is new
        # (8.19.0): the whole item, verbatim, when the client echoed an
        # ``encrypted_content`` back. Keeping the item rather than a
        # projection is deliberate -- a replay has to hand the host back
        # exactly what it minted, ``id`` and all, or it is refused.
        combined = combine_reasoning(pending.text, reasoning_text_from_item(item))
        items = pending.items
        if _encrypted_content_of(item) is not None:
            items = [*items, dict(item)]
        return _PendingReasoning(text=combined, items=items)
    if item_type in {"input_text", "output_text", "text"}:
        _append_pending_reasoning(messages, pending)
        messages.append({"role": "user", "content": _text_from_part(item)})
        return _PendingReasoning()

    raise ResponsesConversionError(
        f"Unsupported Responses input item type: {item_type!r}"
    )


def _encrypted_content_of(item: Mapping[str, Any]) -> str | None:
    """The item's ``encrypted_content`` when it is a non-empty string."""

    value = item.get("encrypted_content")
    return value if isinstance(value, str) and value else None


def _trace_quarantined_function_call(
    call_id: str, exc: ResponsesConversionError
) -> None:
    trace_event(
        stage="responses",
        event="responses.input.function_call_quarantined",
        source="openai_responses",
        call_id=call_id,
        error_type=type(exc).__name__,
    )


def _append_message_item(
    role: str,
    content: Any,
    messages: list[dict[str, Any]],
    system_parts: list[str],
    *,
    reasoning_content: str | None = None,
    encrypted_reasoning: list[dict[str, Any]] | None = None,
) -> None:
    normalized_role = "system" if role == "developer" else role
    if normalized_role == "system":
        text = _content_as_text(content)
        if text:
            system_parts.append(text)
        return
    if normalized_role not in {"user", "assistant"}:
        raise ResponsesConversionError(f"Unsupported Responses message role: {role!r}")
    message = {
        "role": normalized_role,
        "content": _convert_message_content(
            content, allow_images=normalized_role == "user"
        ),
    }
    if normalized_role == "assistant" and reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if normalized_role == "assistant" and encrypted_reasoning:
        message["encrypted_reasoning"] = list(encrypted_reasoning)
    messages.append(message)


def _append_pending_reasoning(
    messages: list[dict[str, Any]], pending: _PendingReasoning
) -> None:
    if pending.text is None and not pending.items:
        return
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "",
    }
    if pending.text is not None:
        message["reasoning_content"] = pending.text
    if pending.items:
        message["encrypted_reasoning"] = list(pending.items)
    messages.append(message)


def _append_pending_reasoning_before_tool_output(
    messages: list[dict[str, Any]], pending: _PendingReasoning
) -> None:
    if pending.text is None and not pending.items:
        return
    message = _last_assistant_tool_use_message(messages)
    if message is None:
        _append_pending_reasoning(messages, pending)
        return
    _merge_message_reasoning(message, pending)


def _append_tool_use_message(
    messages: list[dict[str, Any]],
    tool_use: dict[str, Any],
    *,
    reasoning_content: str | None,
    encrypted_reasoning: list[dict[str, Any]] | None = None,
) -> None:
    message = _last_assistant_tool_use_message(messages)
    if message is None:
        message = {"role": "assistant", "content": []}
        messages.append(message)
    if reasoning_content is not None:
        _merge_message_reasoning(message, _PendingReasoning(text=reasoning_content))
    if encrypted_reasoning:
        _merge_message_encrypted_reasoning(message, encrypted_reasoning)
    content = message["content"]
    if isinstance(content, list):
        content.append(tool_use)


def _function_call_output_text(output: Any) -> str:
    """Normalise a client's ``function_call_output`` to text.

    The Responses format allows this field to be a list of content parts, so a
    client could otherwise land an arbitrary list -- images included -- straight
    inside ``tool_result.content``, which is the one shape the rest of MCC is
    entitled to assume came from a tool it converted itself. The other two
    inbound doors already force a tool result to text; this makes the third
    agree. Accepting images from a client is deliberately a separate question.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                parts.append(text if isinstance(text, str) else json.dumps(dict(item)))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if output is None:
        return ""
    if isinstance(output, Mapping):
        return json.dumps(dict(output))
    return str(output)


def _append_tool_result_message(
    messages: list[dict[str, Any]],
    tool_result: dict[str, Any],
) -> None:
    message = _last_user_tool_result_message(messages)
    if message is None:
        message = {"role": "user", "content": []}
        messages.append(message)
    content = message["content"]
    if isinstance(content, list):
        content.append(tool_result)


def _last_assistant_tool_use_message(
    messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not messages:
        return None
    message = messages[-1]
    if message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return None
    if all(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in content
    ):
        return message
    return None


def _last_user_tool_result_message(
    messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not messages:
        return None
    message = messages[-1]
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return None
    if all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    ):
        return message
    return None


def _merge_message_reasoning(
    message: dict[str, Any], pending: _PendingReasoning
) -> None:
    """Fold the pending halves onto a message that already exists.

    Both halves are merged, not replaced: a tool turn can receive reasoning
    from more than one item, and the text half has combined that way since
    the door existed.
    """

    if pending.text is not None:
        existing = message.get("reasoning_content")
        existing_reasoning = existing if isinstance(existing, str) else None
        message["reasoning_content"] = combine_reasoning(
            existing_reasoning, pending.text
        )
    if pending.items:
        _merge_message_encrypted_reasoning(message, pending.items)


def _merge_message_encrypted_reasoning(
    message: dict[str, Any], items: list[dict[str, Any]]
) -> None:
    """Append ``items`` to the message's own list, keeping the order they came in.

    Appended rather than replaced: two ``reasoning`` items can precede one
    assistant turn (a parallel-tool turn emits one per unit of work), and a
    replacement would silently keep only the last. The list is copied so the
    caller's pending state cannot be mutated through the message.
    """

    existing = message.get("encrypted_reasoning")
    merged = list(existing) if isinstance(existing, list) else []
    merged.extend(items)
    message["encrypted_reasoning"] = merged


def _iter_input_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _convert_message_content(
    content: Any, *, allow_images: bool = False
) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                blocks.append({"type": "text", "text": part})
                continue
            if not isinstance(part, dict):
                raise ResponsesConversionError(
                    f"Unsupported Responses content part: {type(part).__name__}"
                )
            part_type = part.get("type")
            if part_type in {"input_text", "output_text", "text"} or "text" in part:
                blocks.append({"type": "text", "text": _text_from_part(part)})
                continue
            if part_type == "refusal":
                blocks.append({"type": "text", "text": str(part.get("refusal", ""))})
                continue
            if part_type == "input_image" and allow_images:
                # 7.95.0: an image attached to a user turn (Codex ``-i`` or a
                # paste) becomes the same image block the Chat Completions
                # door builds, so the vision chain, describe mode and image
                # capture see it exactly as they see that door's images.
                blocks.append(_image_block(part))
                continue
            raise ResponsesConversionError(
                f"Unsupported Responses content part type: {part_type!r}"
            )
        return blocks
    if isinstance(content, dict):
        return [{"type": "text", "text": _text_from_part(content)}]
    raise ResponsesConversionError(
        f"Unsupported Responses message content: {type(content).__name__}"
    )


def _image_block(part: Mapping[str, Any]) -> dict[str, Any]:
    """An ``input_image`` part as an Anthropic image block (7.95.0).

    Byte for byte the block the Chat Completions door builds from an
    ``image_url`` part: a base64 data URL becomes a ``base64`` source, an
    http(s) URL a ``url`` source. ``detail`` has no Anthropic equivalent and
    is dropped. A ``file_id`` names a file uploaded to OpenAI's Files API,
    which MCC does not have, so it is refused by name rather than guessed at.
    """
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, Mapping) else image_url
    if url is None or url == "":
        file_id = part.get("file_id")
        if isinstance(file_id, str) and file_id:
            raise ResponsesConversionError(
                f"input_image file_id {file_id!r} is not supported: MCC has no "
                "Files API to read it from. Send the image inline as image_url "
                "(a base64 data URL or an http(s) URL)."
            )
        raise ResponsesConversionError(
            "input_image needs image_url: a base64 data URL or an http(s) URL."
        )
    if not isinstance(url, str):
        raise ResponsesConversionError("input_image.image_url must be a string.")
    if url.startswith(_DATA_URL_PREFIX):
        return {"type": "image", "source": _data_url_source(url)}
    if urlsplit(url).scheme.lower() in {"http", "https"}:
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise ResponsesConversionError(
        "input_image.image_url must be a base64 data URL or an http(s) URL."
    )


def _data_url_source(url: str) -> dict[str, Any]:
    """A ``data:<type>;base64,<data>`` URL as an Anthropic ``base64`` source."""
    header, separator, data = url[len(_DATA_URL_PREFIX) :].partition(",")
    if not separator:
        raise ResponsesConversionError("input_image.image_url is a malformed data URL")
    if not header.endswith(";base64"):
        raise ResponsesConversionError(
            "Only base64 data URLs are supported for input_image"
        )
    media_type = header[: -len(";base64")] or "image/png"
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ResponsesConversionError(
            "input_image data URL payload is not valid base64"
        ) from exc
    return {"type": "base64", "media_type": media_type, "data": data}


def _content_as_text(content: Any) -> str:
    converted = _convert_message_content(content)
    if isinstance(converted, str):
        return converted
    return "\n".join(str(block.get("text", "")) for block in converted)


def _text_from_part(part: Mapping[str, Any]) -> str:
    if text := optional_str(part.get("text")):
        return text
    if text := optional_str(part.get("input_text")):
        return text
    if text := optional_str(part.get("output_text")):
        return text
    return ""
