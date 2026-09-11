"""Convert OpenAI Responses API SSE events to Anthropic SSE format.

Protocol, not provider. Every host that speaks the Responses API streams the
same frames, so the translation belongs to the protocol and not to whichever
backend happens to be answering: ``chatgpt_oauth`` was simply the first to
need it, and OpenCode Zen serves two of its free models on the same wire.
Nothing in this module names a provider, a base URL or a credential.
"""

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


#: Responses frames that stream a piece of the model's visible reasoning.
#: ``reasoning_summary_text`` is the summary the endpoint writes for a client;
#: ``reasoning_text`` is raw reasoning, which only some models expose. Both are
#: thinking as far as Anthropic's protocol is concerned.
_REASONING_DELTA_EVENTS = frozenset(
    {
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    }
)

#: Frames that repeat a whole reasoning part after its deltas. They are the
#: only source of the text when a part arrives in one piece, and a duplicate of
#: it otherwise, which is why every emission is keyed (see
#: ``_reasoning_part_key``).
_REASONING_DONE_EVENTS = frozenset(
    {
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
        "response.reasoning_summary_part.done",
    }
)


def _reasoning_done_text(event: Mapping[str, Any]) -> str:
    """Read the complete part text off one terminal reasoning frame.

    ``*_text.done`` carries it as ``text``; ``reasoning_summary_part.done``
    nests it under ``part``.
    """
    text = event.get("text")
    if isinstance(text, str) and text:
        return text
    part = event.get("part")
    if isinstance(part, Mapping):
        nested = part.get("text")
        if isinstance(nested, str):
            return nested
    return ""


class ResponsesStreamConverter:
    """Own state for one OpenAI Responses API stream.

    Usage is read off the terminal ``response.completed`` frame and normalised
    into Anthropic's shape by :meth:`_read_usage`, which carries the field
    names, the invariant and the evidence.
    """

    def __init__(
        self,
        ledger: AnthropicStreamLedger,
        *,
        log_raw_events: bool = False,
        output_reasoning: bool = True,
    ) -> None:
        self._ledger = ledger
        self._log_raw_events = log_raw_events
        #: Whether the client's reasoning policy allows the model's thinking to
        #: be shown. ``True`` by default because a converter handed no policy
        #: has been told nothing that would justify dropping content the
        #: endpoint sent; ``stream_response`` passes the real intent.
        self._output_reasoning = output_reasoning
        #: Summary/reasoning parts already streamed as deltas, keyed by
        #: ``(kind, item_id, index)``. A terminal ``.done`` frame repeats the
        #: whole part, so it must only be emitted for a part nobody streamed.
        self._reasoning_parts_streamed: set[tuple[str, str, int]] = set()
        #: Characters emitted into the currently open thinking block, so a
        #: second summary part is separated from the first instead of being
        #: glued onto it.
        self._thinking_chars_in_block = 0
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

            print(json.dumps({"responses_event": event}, default=str))

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

        if event_type in _REASONING_DELTA_EVENTS:
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                key = self._reasoning_part_key(event_type, event)
                if key is not None and key not in self._reasoning_parts_streamed:
                    self._reasoning_parts_streamed.add(key)
                    yield from self._begin_reasoning_part()
                yield from self._emit_thinking(delta)
            return

        if event_type in _REASONING_DONE_EVENTS:
            key = self._reasoning_part_key(event_type, event)
            text = _reasoning_done_text(event)
            if key is not None and key not in self._reasoning_parts_streamed and text:
                self._reasoning_parts_streamed.add(key)
                yield from self._begin_reasoning_part()
                yield from self._emit_thinking(text)
            return

        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "reasoning":
                # Deliberately opens nothing. A reasoning item whose only
                # payload is ``encrypted_content`` carries no text anybody may
                # read, and an empty thinking block would claim otherwise.
                return
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
            if item_type == "reasoning":
                yield from self._flush_reasoning_item(item)
                return
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

    def _reasoning_part_key(
        self, event_type: str, event: Mapping[str, Any]
    ) -> tuple[str, str, int] | None:
        """Name the one summary/reasoning part an event belongs to.

        The Responses API numbers summary parts with ``summary_index`` and raw
        reasoning parts with ``content_index``, both scoped to the reasoning
        item's ``item_id``. The triple is what makes a ``.done`` frame
        recognisable as a repeat of deltas already streamed.
        """
        item_id = event.get("item_id")
        if not isinstance(item_id, str):
            return None
        kind = "text" if "reasoning_text" in event_type else "summary"
        index_key = "content_index" if kind == "text" else "summary_index"
        index = event.get(index_key)
        if not isinstance(index, int) or isinstance(index, bool):
            index = 0
        return (kind, item_id, index)

    def _begin_reasoning_part(self) -> Iterator[str]:
        """Separate a new summary part from whatever is already in the block.

        Every part of a summary lands in one thinking block (see
        :meth:`_emit_thinking`), so without this the last word of one part and
        the first word of the next would run together. A part that opens a
        *new* block -- because text intervened and closed the last one -- needs
        no separator, which is why the ledger's own flag decides and not the
        character count alone.
        """
        if (
            self._output_reasoning
            and self._ledger.blocks.thinking_started
            and self._thinking_chars_in_block
        ):
            yield from self._emit_thinking("\n\n")

    def _emit_thinking(self, text: str) -> Iterator[str]:
        """Route one piece of model reasoning into the Anthropic thinking block.

        The ledger owns the block bookkeeping: ``ensure_thinking_block`` closes
        an open text block first and ``ensure_text_block`` closes an open
        thinking block, so a thinking block can never interleave illegally with
        text or tool_use -- the same contract ``openai_chat/provider.py`` relies
        on for the Chat Completions family.
        """
        if not self._output_reasoning or not text:
            return
        if not self._ledger.blocks.thinking_started:
            self._thinking_chars_in_block = 0
        yield from self._ledger.ensure_thinking_block()
        yield self._ledger.emit_thinking_delta(text)
        self._thinking_chars_in_block += len(text)

    def _flush_reasoning_item(self, item: Mapping[str, Any]) -> Iterator[str]:
        """Emit any summary text of a finished reasoning item nobody streamed.

        The endpoint may deliver a summary only on the item's terminal frame,
        and an item that carries ``encrypted_content`` and an empty ``summary``
        carries nothing to show -- which is the honest "it thought, and
        returned none of it" case that must stay ``thinking_chars = 0``.
        """
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        for kind, field in (("summary", "summary"), ("text", "content")):
            parts = item.get(field)
            if not isinstance(parts, list):
                continue
            for index, part in enumerate(parts):
                if not isinstance(part, Mapping):
                    continue
                text = part.get("text")
                key = (kind, item_id, index)
                if not isinstance(text, str) or not text:
                    continue
                if key in self._reasoning_parts_streamed:
                    continue
                self._reasoning_parts_streamed.add(key)
                yield from self._begin_reasoning_part()
                yield from self._emit_thinking(text)

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


async def iter_responses_sse_events(
    raw_stream: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Parse a raw async SSE byte stream into Responses API event dicts."""
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
    """Tally one Responses API event, storing no text.

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
