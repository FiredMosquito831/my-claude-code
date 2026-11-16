"""Emit Chat Completions nested ``url_citation`` annotations, JSON, and SSE from the web tool loop."""

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from .completion import extract_completion_blocks, tool_call_from_block
from .events import DONE_FRAME, finish_reason_for, format_chat_sse_data
from .models import OpenAIChatCompletionRequest
from .usage import ChatUsageLedger


def collect_chat_final_text(final_step_blocks: Sequence[Mapping[str, Any]]) -> str:
    """Concatenate final assistant text blocks emitted after the web tool loop."""
    return "".join(
        str(block.get("text") or "")
        for block in final_step_blocks
        if block.get("type") == "text"
    )


def build_nested_chat_annotations(
    annotations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Transform grounded flat citations into Chat Completions' nested ``url_citation`` shape."""
    nested: list[dict[str, Any]] = []
    for ann in annotations:
        url = str(ann.get("url") or "").strip()
        if not url:
            continue
        nested.append(
            {
                "type": "url_citation",
                "url_citation": {
                    "url": url,
                    "title": str(ann.get("title") or url),
                    "start_index": int(ann.get("start_index") or 0),
                    "end_index": int(ann.get("end_index") or 0),
                },
            }
        )
    return nested


def build_chat_completion_from_loop(
    request: OpenAIChatCompletionRequest,
    *,
    completion_id: str,
    final_step_blocks: Sequence[Mapping[str, Any]],
    nested_annotations: Sequence[Mapping[str, Any]],
    loop_usage: Mapping[str, Any],
    stop_reason: str | None,
    created: int | None = None,
) -> dict[str, Any]:
    """Build a complete non-streaming ``chat.completion`` JSON payload from loop output."""
    usage = ChatUsageLedger()
    content, reasoning, tool_calls = extract_completion_blocks(
        final_step_blocks, usage=usage
    )
    usage.absorb(loop_usage)

    choice_message: dict[str, Any] = {
        "role": "assistant",
        "content": content if content or not tool_calls else None,
    }
    if reasoning:
        choice_message["reasoning_content"] = reasoning
        choice_message["reasoning"] = reasoning
    if tool_calls:
        choice_message["tool_calls"] = tool_calls
    if nested_annotations:
        choice_message["annotations"] = [dict(ann) for ann in nested_annotations]

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()) if created is None else created,
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": choice_message,
                "logprobs": None,
                "finish_reason": finish_reason_for(
                    stop_reason,
                    emitted_tool_calls=bool(tool_calls),
                ),
            }
        ],
        "usage": usage.payload(),
    }


async def iter_chat_sse_from_loop(
    request: OpenAIChatCompletionRequest,
    *,
    completion_id: str,
    final_step_blocks: Sequence[Mapping[str, Any]],
    nested_annotations: Sequence[Mapping[str, Any]],
    loop_usage: Mapping[str, Any],
    stop_reason: str | None,
    created: int | None = None,
) -> AsyncIterator[str]:
    """Yield Chat Completions ``chat.completion.chunk`` SSE frames from loop output."""
    ts = int(time.time()) if created is None else created
    usage = ChatUsageLedger()
    usage.absorb(loop_usage)

    def _envelope() -> dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": request.model,
        }

    def _chunk(delta: dict[str, Any], *, finish_reason: str | None = None) -> str:
        payload = _envelope()
        payload["choices"] = [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ]
        return format_chat_sse_data(payload)

    yield _chunk({"role": "assistant", "content": ""})

    tool_call_index = 0
    emitted_tool_calls = False
    for block in final_step_blocks:
        btype = block.get("type")
        if btype == "thinking":
            thought = str(block.get("thinking") or "")
            if thought:
                usage.add_reasoning_text(thought)
                yield _chunk({"reasoning_content": thought, "reasoning": thought})
        elif btype == "text":
            text = str(block.get("text") or "")
            if text:
                yield _chunk({"content": text})
        elif btype == "tool_use":
            call = tool_call_from_block(block)
            args_json = str(call["function"]["arguments"])
            yield _chunk(
                {
                    "tool_calls": [
                        {
                            "index": tool_call_index,
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": "",
                            },
                        }
                    ]
                }
            )
            if args_json:
                yield _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tool_call_index,
                                "function": {"arguments": args_json},
                            }
                        ]
                    }
                )
            tool_call_index += 1
            emitted_tool_calls = True

    if nested_annotations:
        yield _chunk({"annotations": [dict(ann) for ann in nested_annotations]})

    yield _chunk(
        {},
        finish_reason=finish_reason_for(
            stop_reason, emitted_tool_calls=emitted_tool_calls
        ),
    )
    if request.wants_usage and usage.has_counts():
        usage_payload = _envelope()
        usage_payload["choices"] = []
        usage_payload["usage"] = usage.payload()
        yield format_chat_sse_data(usage_payload)
    yield DONE_FRAME
