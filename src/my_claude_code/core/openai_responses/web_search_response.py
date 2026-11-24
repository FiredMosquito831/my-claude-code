"""Emit Responses SSE events and non-streaming JSON from the shared web tool loop."""

import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .items import message_item, reasoning_item, web_search_call_item
from .models import OpenAIResponsesRequest
from .streaming.blocks import ToolBlockState
from .streaming.completion import tool_item
from .streaming.event_builders import (
    content_part_added,
    content_part_done,
    custom_tool_call_input_delta,
    custom_tool_call_input_done,
    function_call_arguments_delta,
    function_call_arguments_done,
    output_item_added,
    output_item_done,
    output_text_annotation_added,
    output_text_delta,
    output_text_done,
    reasoning_text_delta,
    reasoning_text_done,
    response_completed,
    response_created,
    web_search_call_completed,
    web_search_call_in_progress,
    web_search_call_searching,
)
from .tools import convert_tools, responses_tool_identity_from_anthropic_name


def _responses_usage(loop_usage: Mapping[str, Any]) -> dict[str, Any]:
    uncached_input = int(loop_usage.get("input_tokens") or 0)
    cache_read = int(loop_usage.get("cache_read_input_tokens") or 0)
    cache_write = int(loop_usage.get("cache_creation_input_tokens") or 0)
    input_tokens = uncached_input + cache_read + cache_write
    output_tokens = int(loop_usage.get("output_tokens") or 0)
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cache_read > 0:
        usage["input_tokens_details"] = {"cached_tokens": cache_read}
    return usage


def _response_shell(
    request: OpenAIResponsesRequest,
    *,
    response_id: str,
    created_at: int,
    status: str,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": request.model,
        "output": output,
        "parallel_tool_calls": (
            True if request.parallel_tool_calls is None else request.parallel_tool_calls
        ),
        "tool_choice": "auto" if request.tool_choice is None else request.tool_choice,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_output_tokens": request.max_output_tokens,
        "usage": usage,
        "error": None,
    }


def collect_responses_final_text(final_step_blocks: Sequence[Mapping[str, Any]]) -> str:
    """Concatenate final assistant text blocks emitted after the web tool loop."""
    return "".join(
        str(block.get("text") or "")
        for block in final_step_blocks
        if block.get("type") == "text"
    )


def _client_tool_item_and_kind(
    block: Mapping[str, Any],
    *,
    output_index: int,
    converted_tools: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any], str]:
    call_id = str(block.get("id") or f"call_{uuid.uuid4().hex[:16]}")
    raw_name = str(block.get("name") or "")
    identity = responses_tool_identity_from_anthropic_name(converted_tools, raw_name)
    raw_input = block.get("input")
    if identity.kind == "custom":
        raw_text = (
            str(raw_input.get("input", ""))
            if isinstance(raw_input, dict)
            else str(raw_input or "")
        )
        state = ToolBlockState(
            index=0,
            output_index=output_index,
            item_id=f"ctc_{call_id}",
            call_id=call_id,
            kind="custom",
            name=identity.name,
            namespace=identity.namespace,
        )
        item = tool_item(state, status="completed", input_text=raw_text)
        return "custom", item, raw_text
    args_json = json.dumps(raw_input if isinstance(raw_input, dict) else {})
    state = ToolBlockState(
        index=0,
        output_index=output_index,
        item_id=f"fc_{call_id}",
        call_id=call_id,
        kind="function",
        name=identity.name,
        namespace=identity.namespace,
    )
    item = tool_item(state, status="completed", arguments=args_json)
    return "function", item, args_json


def _prepare_output_entries(
    request: OpenAIResponsesRequest,
    *,
    web_calls: Sequence[Any],
    final_step_blocks: Sequence[Mapping[str, Any]],
    annotations: Sequence[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    converted_tools = convert_tools(request.tools, allow_web_search=True)
    entries: list[tuple[str, dict[str, Any]]] = []

    for call in web_calls:
        srv_id = str(getattr(call, "srv_id", "") or uuid.uuid4().hex[:16])
        ws_id = f"ws_{srv_id.removeprefix('srvtoolu_')}"
        status = str(getattr(call, "status", "completed"))
        action = dict(getattr(call, "action", None) or {})
        entries.append(("web_search_call", web_search_call_item(ws_id, status, action)))

    reasoning_parts: list[str] = []
    client_tools: list[Mapping[str, Any]] = []
    for block in final_step_blocks:
        btype = block.get("type")
        if btype == "thinking":
            thinking_val = str(block.get("thinking") or "")
            if thinking_val:
                reasoning_parts.append(thinking_val)
        elif btype == "tool_use":
            client_tools.append(block)

    if reasoning_parts:
        rs_id = f"rs_{uuid.uuid4().hex[:24]}"
        entries.append(
            ("reasoning", reasoning_item(rs_id, "".join(reasoning_parts), "completed"))
        )

    full_text = collect_responses_final_text(final_step_blocks)
    if full_text or not client_tools:
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        entries.append(
            (
                "message",
                message_item(
                    msg_id, full_text, "completed", annotations=list(annotations)
                ),
            )
        )

    for tool_block in client_tools:
        kind, item, payload = _client_tool_item_and_kind(
            tool_block, output_index=len(entries), converted_tools=converted_tools
        )
        entries.append((f"{kind}:{payload}", item))

    return entries


def build_responses_payload_from_loop(
    request: OpenAIResponsesRequest,
    *,
    web_calls: Sequence[Any],
    final_step_blocks: Sequence[Mapping[str, Any]],
    annotations: Sequence[dict[str, Any]],
    loop_usage: Mapping[str, Any],
    response_id: str | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    """Build a complete non-streaming OpenAI Responses JSON object."""
    rid = response_id or f"resp_{uuid.uuid4().hex}"
    ts = int(time.time()) if created_at is None else created_at
    entries = _prepare_output_entries(
        request,
        web_calls=web_calls,
        final_step_blocks=final_step_blocks,
        annotations=annotations,
    )
    return _response_shell(
        request,
        response_id=rid,
        created_at=ts,
        status="completed",
        output=[item for _kind, item in entries],
        usage=_responses_usage(loop_usage),
    )


async def iter_responses_sse_from_loop(
    request: OpenAIResponsesRequest,
    *,
    web_calls: Sequence[Any],
    final_step_blocks: Sequence[Mapping[str, Any]],
    annotations: Sequence[dict[str, Any]],
    loop_usage: Mapping[str, Any],
    response_id: str | None = None,
    created_at: int | None = None,
) -> AsyncIterator[str]:
    """Emit full Responses API SSE lifecycle events for a completed web search loop."""
    rid = response_id or f"resp_{uuid.uuid4().hex}"
    ts = int(time.time()) if created_at is None else created_at
    entries = _prepare_output_entries(
        request,
        web_calls=web_calls,
        final_step_blocks=final_step_blocks,
        annotations=annotations,
    )

    yield response_created(
        _response_shell(
            request,
            response_id=rid,
            created_at=ts,
            status="in_progress",
            output=[],
            usage=None,
        )
    )

    for idx, (entry_kind, item) in enumerate(entries):
        item_id = str(item["id"])
        if entry_kind == "web_search_call":
            yield output_item_added(idx, web_search_call_item(item_id, "in_progress"))
            yield web_search_call_in_progress(item_id, idx)
            yield web_search_call_searching(item_id, idx)
            yield web_search_call_completed(item_id, idx)
            yield output_item_done(idx, item)
        elif entry_kind == "reasoning":
            reasoning_text = str(item["content"][0]["text"])
            yield output_item_added(idx, reasoning_item(item_id, "", "in_progress"))
            if reasoning_text:
                yield reasoning_text_delta(item_id, idx, reasoning_text)
            yield reasoning_text_done(item_id, idx, reasoning_text)
            yield output_item_done(idx, item)
        elif entry_kind == "message":
            part = item["content"][0]
            text = str(part["text"])
            anns = list(part.get("annotations") or [])
            yield output_item_added(idx, message_item(item_id, "", "in_progress"))
            yield content_part_added(item_id, idx)
            if text:
                yield output_text_delta(item_id, idx, text)
            for ann_idx, ann in enumerate(anns):
                yield output_text_annotation_added(item_id, idx, ann_idx, ann)
            yield output_text_done(item_id, idx, text)
            yield content_part_done(item_id, idx, text, annotations=anns)
            yield output_item_done(idx, item)
        elif entry_kind.startswith("function:"):
            args_json = entry_kind.split(":", 1)[1]
            in_prog = {**item, "status": "in_progress", "arguments": ""}
            yield output_item_added(idx, in_prog)
            if args_json:
                yield function_call_arguments_delta(item_id, idx, args_json)
            yield function_call_arguments_done(item_id, idx, args_json)
            yield output_item_done(idx, item)
        elif entry_kind.startswith("custom:"):
            raw_input = entry_kind.split(":", 1)[1]
            in_prog = {**item, "status": "in_progress", "input": ""}
            yield output_item_added(idx, in_prog)
            if raw_input:
                yield custom_tool_call_input_delta(item_id, idx, raw_input)
            yield custom_tool_call_input_done(item_id, idx, raw_input)
            yield output_item_done(idx, item)

    yield response_completed(
        _response_shell(
            request,
            response_id=rid,
            created_at=ts,
            status="completed",
            output=[item for _kind, item in entries],
            usage=_responses_usage(loop_usage),
        )
    )
