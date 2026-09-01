"""Convert ChatGPT Responses API SSE events to Anthropic SSE format."""

import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.wire_capture import ResponseShape


def _finish_reason_from_status(status: Any) -> str:
    if not isinstance(status, str):
        return "end_turn"
    status_lower = status.lower()
    if status_lower in {"completed", "stop"}:
        return "end_turn"
    if status_lower in {"max_tokens", "length"}:
        return "max_tokens"
    if status_lower in {"content_filter"}:
        return "content_filter"
    return "end_turn"


class ChatGPTOAuthStreamConverter:
    """Own state for one ChatGPT Responses API stream."""

    def __init__(
        self,
        ledger: AnthropicStreamLedger,
        *,
        log_raw_events: bool = False,
    ) -> None:
        self._ledger = ledger
        self._log_raw_events = log_raw_events
        self._active_tool_calls: dict[str, dict[str, Any]] = {}
        self._usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
        }
        self._finished = False

    def _log_event(self, event: dict[str, Any]) -> None:
        if self._log_raw_events:
            import json

            print(json.dumps({"chatgpt_oauth_event": event}, default=str))

    def feed(self, event: dict[str, Any]) -> Iterator[str]:
        """Yield Anthropic SSE events for one Responses API event."""
        self._log_event(event)
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield from self._ledger.ensure_text_block()
                yield self._ledger.emit_text_delta(delta)
            return

        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "function_call":
                tool_id = item.get("id") or f"call_{len(self._active_tool_calls)}"
                name = item.get("name") or "unknown"
                self._active_tool_calls[tool_id] = {
                    "id": tool_id,
                    "name": name,
                    "arguments": "",
                    "index": len(self._active_tool_calls),
                }
                yield from self._ledger.close_content_blocks()
                yield self._ledger.start_tool_block(
                    tool_index=self._active_tool_calls[tool_id]["index"],
                    tool_id=tool_id,
                    name=name,
                )
            return

        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            delta = event.get("delta")
            tool = self._active_tool_calls.get(item_id)
            if tool is not None and isinstance(delta, str):
                tool["arguments"] += delta
                yield self._ledger.emit_tool_delta(tool["index"], delta)
            return

        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "function_call":
                tool_id = item.get("id")
                tool = self._active_tool_calls.get(tool_id)
                if tool is not None:
                    # Ensure the complete argument object has been emitted.
                    full_args = item.get("arguments") or tool.get("arguments") or "{}"
                    if tool["arguments"] != full_args:
                        remaining = full_args[len(tool["arguments"]) :]
                        if remaining:
                            yield self._ledger.emit_tool_delta(tool["index"], remaining)
                            tool["arguments"] = full_args
                    yield self._ledger.stop_tool_block(tool["index"])
            return

        if event_type in {"response.completed", "response.done"}:
            response = event.get("response") or event
            if self._finished:
                return
            self._finished = True
            yield from self._ledger.close_content_blocks()
            usage = response.get("usage") if isinstance(response, dict) else None
            if isinstance(usage, dict):
                self._usage["input_tokens"] = usage.get("input_tokens", 0) or 0
                self._usage["output_tokens"] = usage.get("output_tokens", 0) or 0
            return

    def finish(self, stop_reason: str | None = None) -> Iterator[str]:
        """Emit final message_delta and message_stop events."""
        yield from self._ledger.close_content_blocks()
        reason = stop_reason or "end_turn"
        yield self._ledger.message_delta(
            reason,
            self._usage.get("output_tokens", 0),
            input_tokens=self._usage.get("input_tokens", 0),
        )
        yield self._ledger.message_stop()


async def iter_chatgpt_oauth_sse_events(
    raw_stream: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Parse a raw async SSE byte stream into ChatGPT Responses API event dicts."""
    buffer = ""
    async for chunk in raw_stream:
        if isinstance(chunk, bytes):
            text = chunk.decode("utf-8", errors="replace")
        else:
            text = str(chunk)
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: ") :].strip()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


_RESPONSES_SHAPE_FIELDS = {
    "response.output_text.delta": "content",
    "response.reasoning_summary_text.delta": "reasoning",
    "response.reasoning_text.delta": "reasoning",
    "response.function_call_arguments.delta": "tool_calls",
}


def note_responses_event_shape(shape: ResponseShape | None, event: Any) -> None:
    """Tally one ChatGPT Responses API event, storing no text.

    The Responses API spells its channels as event types rather than delta
    fields, so the mapping is named here rather than guessed downstream. Only
    the four that carry model output are counted; the rest are lifecycle.
    """
    if shape is None or not isinstance(event, Mapping):
        return
    shape.note_chunk(time.monotonic())
    kind = str(event.get("type", ""))
    name = _RESPONSES_SHAPE_FIELDS.get(kind)
    if name is not None:
        delta = event.get("delta")
        shape.note_field(name, len(delta) if isinstance(delta, str) else 0)
        return
    if kind in ("response.completed", "response.incomplete", "response.failed"):
        response = event.get("response")
        if isinstance(response, Mapping):
            shape.note_usage(response.get("usage"))
            status = response.get("status")
            shape.note_finish(status if isinstance(status, str) else kind)
        else:
            shape.note_finish(kind)
