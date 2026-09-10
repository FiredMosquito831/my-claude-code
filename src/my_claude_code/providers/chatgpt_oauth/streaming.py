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


def _usage_int(source: Any, key: str) -> int | None:
    """Return one integer usage field, or ``None`` when it was not reported.

    The distinction is load-bearing and is the §44 rule in two lines: a frame
    carrying no ``input_tokens_details`` never measured a cache hit and must
    leave the request-log column NULL ("not measured"), while a frame carrying
    ``cached_tokens: 0`` did measure one and found none, and must record ``0``.
    """
    if not isinstance(source, Mapping):
        return None
    value = source.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class ChatGPTOAuthStreamConverter:
    """Own state for one ChatGPT Responses API stream.

    Usage is read off the terminal ``response.completed`` frame and normalised
    into Anthropic's shape by :meth:`_read_usage`, which carries the field
    names, the invariant and the evidence.
    """

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
        #: Extra Anthropic usage keys the upstream actually reported. Empty
        #: means "not measured" -- never a zero standing in for silence.
        self._usage_fields: dict[str, int] = {}
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
            if isinstance(usage, Mapping):
                self._read_usage(usage)
            return

    def _read_usage(self, usage: Mapping[str, Any]) -> None:
        """Fold one Responses ``usage`` block into Anthropic's shape.

        The endpoint reports prompt-cache hits under
        ``usage.input_tokens_details.cached_tokens``, with
        ``cache_write_tokens`` alongside it. That is not inference: Codex CLI
        0.153.4 deserialises exactly those two names into its own
        ``cached_input_tokens`` / ``cache_write_input_tokens`` and emits them
        as ``codex.turn.token_usage.*`` metrics. Until 6.68.2 this converter
        read ``input_tokens`` and ``output_tokens`` and stopped, so all 11,132
        ``chatgpt_oauth`` rows in the request log carried a NULL cache column
        -- not one zero, which is the signature of a key never read rather
        than of an upstream reporting no hit.

        The two protocols count the prompt differently: a Responses
        ``input_tokens`` *includes* the tokens served from cache, while
        Anthropic's ``input_tokens`` excludes them and expects the caller to
        add ``cache_read_input_tokens`` back for the total. Emitting the
        upstream number under the Anthropic name double-counts every cache
        hit -- the 4.22.1 defect, which
        ``providers/openai_chat/provider.py`` fixed the same way for the Chat
        Completions family. Hence the invariant this holds, asserted in
        ``test_input_plus_cache_read_equals_the_upstream_prompt_count``:

            ``input_tokens + cache_read_input_tokens == usage.input_tokens``

        A usage block with no details reports nothing rather than a zero, so
        "not measured" survives all the way to the column.
        """
        provider_input = _usage_int(usage, "input_tokens") or 0
        self._usage["output_tokens"] = _usage_int(usage, "output_tokens") or 0
        details = usage.get("input_tokens_details")
        cached = _usage_int(details, "cached_tokens")
        written = _usage_int(details, "cache_write_tokens")
        if cached is not None:
            self._usage_fields["cache_read_input_tokens"] = cached
        if written is not None:
            self._usage_fields["cache_creation_input_tokens"] = written
        self._usage["input_tokens"] = (
            max(0, provider_input - cached) if cached is not None else provider_input
        )

    def finish(self, stop_reason: str | None = None) -> Iterator[str]:
        """Emit final message_delta and message_stop events."""
        yield from self._ledger.close_content_blocks()
        reason = stop_reason or "end_turn"
        yield self._ledger.message_delta(
            reason,
            self._usage.get("output_tokens", 0),
            input_tokens=self._usage.get("input_tokens", 0),
            usage_fields=self._usage_fields or None,
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
