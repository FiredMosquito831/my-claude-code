"""Convert OpenAI Responses requests into Anthropic Messages payloads."""

import base64
import binascii
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from my_claude_code.core.anthropic.structured_output import (
    StructuredOutputValidationError,
    has_tool_history,
    is_http_url,
    parse_pdf_data_url,
    parse_responses_text_format,
    synthesize_missing_history_tools,
)
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
    extract_responses_web_search_spec,
    optional_str,
    parse_arguments,
    required_str,
    responses_tool_name_to_anthropic_name,
    tool_choice_forbids_tools,
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
    *,
    allow_web_search: bool = False,
    allow_image_generation: bool = False,
) -> dict[str, Any]:
    """Convert an OpenAI Responses request into an Anthropic Messages payload."""

    system_parts: list[str] = []
    if instructions := request.instructions:
        system_parts.append(instructions)

    messages: list[dict[str, Any]] = []
    pending = _PendingReasoning()
    quarantined_function_call_ids: set[str] = set()
    emitted_call_ids: set[str] = set()
    dropped_orphan_call_ids: set[str] = set()
    has_previous_response_id = bool(request.previous_response_id)
    for item in _iter_input_items(request.input):
        pending = _append_input_item(
            item,
            messages=messages,
            system_parts=system_parts,
            pending=pending,
            quarantined_function_call_ids=quarantined_function_call_ids,
            emitted_call_ids=emitted_call_ids,
            dropped_orphan_call_ids=dropped_orphan_call_ids,
            has_previous_response_id=has_previous_response_id,
            allow_web_search=allow_web_search,
        )
    _append_pending_reasoning(messages, pending)

    try:
        structured_spec, output_fmt = parse_responses_text_format(request.text)
    except StructuredOutputValidationError as exc:
        raise ResponsesConversionError(exc.message) from exc

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
    if structured_spec is not None:
        payload["structured_output_spec"] = structured_spec
    if output_fmt is not None:
        payload.setdefault("output_config", {})["format"] = output_fmt

    if allow_web_search:
        extract_responses_web_search_spec(
            request.tools, request.tool_choice, allow_web_search=True
        )

    raw_tool_choice = request.tool_choice
    tools = convert_tools(
        request.tools,
        allow_web_search=allow_web_search,
        allow_image_generation=allow_image_generation,
    )
    forbids_tools = tool_choice_forbids_tools(raw_tool_choice)
    history_has_tools = has_tool_history(messages)
    if forbids_tools and history_has_tools:
        payload["tools"] = synthesize_missing_history_tools(messages, tools)
        payload["tool_choice"] = {"type": "none"}
    elif not forbids_tools:
        if tools:
            payload["tools"] = tools
        tool_choice = convert_tool_choice(
            raw_tool_choice,
            parallel_tool_calls=request.parallel_tool_calls if tools else None,
            allow_web_search=allow_web_search,
            allow_image_generation=allow_image_generation,
        )
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    payload["responses_envelope"] = _build_responses_envelope(
        request,
        quarantined_function_call_ids=quarantined_function_call_ids,
        dropped_orphan_call_ids=dropped_orphan_call_ids,
    )

    return payload


def _append_input_item(
    item: Any,
    *,
    messages: list[dict[str, Any]],
    system_parts: list[str],
    pending: _PendingReasoning,
    quarantined_function_call_ids: set[str],
    emitted_call_ids: set[str],
    dropped_orphan_call_ids: set[str],
    has_previous_response_id: bool,
    allow_web_search: bool = False,
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
        emitted_call_ids.add(call_id)
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
        if not has_previous_response_id and call_id not in emitted_call_ids:
            dropped_orphan_call_ids.add(call_id)
            return pending
        _append_pending_reasoning_before_tool_output(messages, pending)
        _append_tool_result_message(
            messages,
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": _function_call_output_content(item.get("output", "")),
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
    if item_type == "web_search_call":
        action = item.get("action")
        action_summary = ""
        if isinstance(action, dict):
            atype = str(action.get("type") or "search")
            target = (
                action.get("query") or action.get("url") or action.get("pattern") or ""
            )
            action_summary = f"{atype}: {target}".strip()
        note = (
            f"[Prior web_search_call: {action_summary}]"
            if action_summary
            else "[Prior web_search_call]"
        )
        _append_message_item(
            "assistant",
            note,
            messages,
            system_parts,
            reasoning_content=pending.text,
            encrypted_reasoning=pending.items,
        )
        return _PendingReasoning()
    if item_type == "image_generation_call":
        revised_prompt = optional_str(item.get("revised_prompt"))
        _status = optional_str(item.get("status"))
        _output_format = optional_str(item.get("output_format"))
        summary = revised_prompt.strip() if revised_prompt else ""
        note = (
            f"[Prior image_generation_call: {summary}]"
            if summary
            else "[Prior image_generation_call]"
        )
        _append_message_item(
            "assistant",
            note,
            messages,
            system_parts,
            reasoning_content=pending.text,
            encrypted_reasoning=pending.items,
        )
        return _PendingReasoning()

    if item_type == "compaction":
        _append_pending_reasoning(messages, pending)
        summary = optional_str(item.get("summary")) or optional_str(item.get("text"))
        messages.append(
            {
                "role": "assistant",
                "content": summary if summary else "[Prior context compacted]",
            }
        )
        return _PendingReasoning()
    if item_type == "local_shell_call":
        call_id = call_id_from_item(item)
        action = item.get("action")
        tool_input = (
            dict(action)
            if isinstance(action, Mapping)
            else {"action": action}
            if action is not None
            else {}
        )
        tool_use = {
            "type": "tool_use",
            "id": call_id,
            "name": "local_shell",
            "input": tool_input,
        }
        emitted_call_ids.add(call_id)
        _append_tool_use_message(
            messages,
            tool_use,
            reasoning_content=pending.text,
            encrypted_reasoning=pending.items,
        )
        return _PendingReasoning()
    if item_type == "local_shell_call_output":
        call_id = call_id_from_item(item)
        if not has_previous_response_id and call_id not in emitted_call_ids:
            dropped_orphan_call_ids.add(call_id)
            return pending
        _append_pending_reasoning_before_tool_output(messages, pending)
        _append_tool_result_message(
            messages,
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": _function_call_output_content(item.get("output", "")),
            },
        )
        return _PendingReasoning()
    if item_type in {"additional_tools", "configuration_update"}:
        return pending

    raise ResponsesConversionError(
        f"Unsupported Responses input item type: {item_type!r}"
    )


def _normalize_envelope_input_item(
    item: Any,
    *,
    quarantined_function_call_ids: set[str],
    dropped_orphan_call_ids: set[str],
) -> dict[str, Any] | None:
    if isinstance(item, str):
        return {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": item}],
        }
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    if item_type in {"input_text", "output_text", "text"}:
        return {
            "type": "message",
            "role": "user",
            "content": [deepcopy(dict(item))],
        }
    if item_type in {"function_call", "function_call_output"}:
        call_id = optional_str(item.get("call_id")) or optional_str(item.get("id"))
        if call_id and call_id in quarantined_function_call_ids:
            return None
    if item_type in {
        "function_call_output",
        "custom_tool_call_output",
        "local_shell_call_output",
    }:
        call_id = optional_str(item.get("call_id")) or optional_str(item.get("id"))
        if call_id and call_id in dropped_orphan_call_ids:
            return None
    copied = deepcopy(dict(item))
    if (item_type is None or "role" in copied) and "type" not in copied:
        copied["type"] = "message"
    return copied


def _build_responses_envelope(
    request: OpenAIResponsesRequest,
    *,
    quarantined_function_call_ids: set[str],
    dropped_orphan_call_ids: set[str],
) -> dict[str, Any]:
    """Capture the lossless inbound ``/v1/responses`` structures for same-protocol egress."""
    envelope_input: list[dict[str, Any]] = []
    for item in _iter_input_items(request.input):
        normalized = _normalize_envelope_input_item(
            item,
            quarantined_function_call_ids=quarantined_function_call_ids,
            dropped_orphan_call_ids=dropped_orphan_call_ids,
        )
        if normalized is not None:
            envelope_input.append(normalized)

    envelope: dict[str, Any] = {"input": envelope_input}
    if request.instructions is not None:
        envelope["instructions"] = request.instructions
    if request.tools is not None:
        envelope["tools"] = deepcopy(request.tools)
    if request.tool_choice is not None:
        envelope["tool_choice"] = deepcopy(request.tool_choice)
    if request.parallel_tool_calls is not None:
        envelope["parallel_tool_calls"] = request.parallel_tool_calls
    if request.text is not None:
        envelope["text"] = deepcopy(request.text)
    if isinstance(request.reasoning, Mapping):
        envelope["reasoning"] = deepcopy(dict(request.reasoning))
    if request.service_tier is not None:
        envelope["service_tier"] = request.service_tier
    if request.truncation is not None:
        envelope["truncation"] = deepcopy(request.truncation)
    if request.include is not None:
        envelope["include"] = list(request.include)
    if request.prompt_cache_key is not None:
        envelope["prompt_cache_key"] = request.prompt_cache_key
    if request.client_metadata is not None:
        envelope["client_metadata"] = deepcopy(request.client_metadata)
    return envelope


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


def _function_call_output_content(output: Any) -> str | list[dict[str, Any]]:
    """Convert Responses ``function_call_output.output`` into text or Anthropic blocks."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        blocks: list[dict[str, Any]] = []
        has_media = False
        for item in output:
            if isinstance(item, str):
                if item:
                    blocks.append({"type": "text", "text": item})
                continue
            if isinstance(item, Mapping):
                part_type = item.get("type")
                if part_type == "input_image":
                    blocks.append(_image_block(item))
                    has_media = True
                    continue
                if part_type == "input_file":
                    blocks.append(_input_file_block(item))
                    has_media = True
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    if text:
                        blocks.append({"type": "text", "text": text})
                    continue
                if part_type in {"input_text", "output_text", "text"}:
                    extracted = _text_from_part(item)
                    if extracted:
                        blocks.append({"type": "text", "text": extracted})
                    continue
                blocks.append({"type": "text", "text": json.dumps(dict(item))})
                continue
            text_val = str(item)
            if text_val:
                blocks.append({"type": "text", "text": text_val})
        if not has_media:
            return "\n".join(
                str(block.get("text", "")) for block in blocks if block.get("text")
            )
        return blocks
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
            if part_type == "input_file" and allow_images:
                blocks.append(_input_file_block(part))
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
    detail = (
        image_url.get("detail")
        if isinstance(image_url, Mapping)
        else part.get("detail")
    )
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
        block: dict[str, Any] = {"type": "image", "source": _data_url_source(url)}
    elif urlsplit(url).scheme.lower() in {"http", "https"}:
        block = {"type": "image", "source": {"type": "url", "url": url}}
    else:
        raise ResponsesConversionError(
            "input_image.image_url must be a base64 data URL or an http(s) URL."
        )
    if isinstance(detail, str) and detail in {"low", "high"}:
        block["detail"] = detail
    return block


def _input_file_block(part: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an ``input_file`` part (data URL or HTTP(S) URL) to Anthropic ``document``."""
    file_data = part.get("file_data")
    file_url = part.get("file_url")
    if isinstance(file_data, str) and file_data:
        try:
            media_type, data = parse_pdf_data_url(
                file_data, error_context="input_file.file_data"
            )
        except StructuredOutputValidationError as exc:
            raise ResponsesConversionError(exc.message) from exc
        source: dict[str, Any] = {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        }
    elif isinstance(file_url, str) and file_url:
        if not is_http_url(file_url):
            raise ResponsesConversionError(
                "input_file.file_url must be an http(s) URL."
            )
        source = {"type": "url", "url": file_url}
    else:
        file_id = part.get("file_id")
        if isinstance(file_id, str) and file_id:
            raise ResponsesConversionError(
                f"input_file file_id {file_id!r} is not supported: MCC has no "
                "Files API to read it from. Send the file inline as file_data "
                "(a base64 PDF data URL) or an http(s) file_url."
            )
        raise ResponsesConversionError(
            "input_file needs file_data (a base64 PDF data URL) or an http(s) file_url."
        )
    block: dict[str, Any] = {"type": "document", "source": source}
    filename = optional_str(part.get("filename"))
    if filename and filename.strip():
        block["title"] = filename.strip()
    return block


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
