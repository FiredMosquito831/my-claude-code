"""Validate and forward native Anthropic Messages SSE frames."""

import codecs
import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

from my_claude_code.core.anthropic.thinking import (
    ContentChunk,
    ContentType,
    ThinkTagParser,
)
from my_claude_code.core.anthropic.upstream_errors import anthropic_stream_failure
from my_claude_code.core.reported_cost import (
    record_reported_reasoning,
    reported_reasoning_tokens,
)
from my_claude_code.core.wire_capture import ResponseShape
from my_claude_code.providers.stream_recovery import TruncatedProviderStreamError

_TERMINAL_EVENT = "message_stop"
_ERROR_EVENT = "error"


class AnthropicMessagesThinkTagNormalizer:
    """Split ``<think>...</think>`` tags out of Anthropic ``text`` SSE blocks.

    Activated only for non-Anthropic ``/messages`` gateways that opt in.
    Native Anthropic and Anthropic OAuth never construct this normalizer.
    """

    def __init__(self, *, output_reasoning: bool = True) -> None:
        self._output_reasoning = output_reasoning
        self._next_index = 0
        self._index_map: dict[int, int] = {}
        self._active_text_source_index: int | None = None
        self._active_text_template: dict[str, Any] = {}
        self._last_suppressed_template: dict[str, Any] = {}
        self._think_parser: ThinkTagParser | None = None
        self._open_kind: str | None = None
        self._open_index: int | None = None
        self._emitted_in_active_text = False
        self._suppressed_in_active_text = False
        self._saw_suppressed_only = False

    def _alloc_index(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    def _close_open_subblock(self) -> list[tuple[str, dict[str, Any]]]:
        if self._open_kind is None or self._open_index is None:
            return []
        closed_index = self._open_index
        self._open_kind = None
        self._open_index = None
        return [
            (
                "content_block_stop",
                {"type": "content_block_stop", "index": closed_index},
            )
        ]

    def _emit_chunk(self, chunk: ContentChunk) -> list[tuple[str, dict[str, Any]]]:
        if not chunk.content:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        if chunk.type is ContentType.THINKING:
            if not self._output_reasoning:
                self._suppressed_in_active_text = True
                return out
            if self._open_kind != "thinking":
                out.extend(self._close_open_subblock())
                idx = self._alloc_index()
                out.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": idx,
                            "content_block": {"type": "thinking", "thinking": ""},
                        },
                    )
                )
                self._open_kind = "thinking"
                self._open_index = idx
                self._emitted_in_active_text = True
            out.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._open_index,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": chunk.content,
                        },
                    },
                )
            )
            return out

        if self._open_kind != "text":
            out.extend(self._close_open_subblock())
            idx = self._alloc_index()
            out.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "text",
                            "text": "",
                            **self._active_text_template,
                        },
                    },
                )
            )
            self._open_kind = "text"
            self._open_index = idx
            self._emitted_in_active_text = True
        out.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._open_index,
                    "delta": {"type": "text_delta", "text": chunk.content},
                },
            )
        )
        return out

    def _feed_text(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        if not text or self._think_parser is None:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for chunk in self._think_parser.feed(text):
            out.extend(self._emit_chunk(chunk))
        return out

    def _finish_active_text(self) -> list[tuple[str, dict[str, Any]]]:
        if self._active_text_source_index is None:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        if self._think_parser is not None:
            remaining = self._think_parser.flush()
            if remaining is not None:
                out.extend(self._emit_chunk(remaining))
        out.extend(self._close_open_subblock())
        if not self._emitted_in_active_text:
            if self._suppressed_in_active_text:
                self._saw_suppressed_only = True
                self._last_suppressed_template = dict(self._active_text_template)
            else:
                idx = self._alloc_index()
                out.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": idx,
                            "content_block": {
                                "type": "text",
                                "text": "",
                                **self._active_text_template,
                            },
                        },
                    )
                )
                out.append(
                    (
                        "content_block_stop",
                        {"type": "content_block_stop", "index": idx},
                    )
                )
        self._active_text_source_index = None
        self._active_text_template = {}
        self._think_parser = None
        self._emitted_in_active_text = False
        self._suppressed_in_active_text = False
        return out

    def feed(
        self, event_name: str, payload: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Normalize one validated Anthropic SSE frame into zero or more frames."""

        if event_name == "content_block_start":
            src_idx = _block_index(payload)
            block = payload.get("content_block")
            out = self._finish_active_text()
            if isinstance(block, Mapping) and block.get("type") == "text":
                self._active_text_source_index = src_idx
                self._active_text_template = {
                    key: value
                    for key, value in block.items()
                    if key not in {"type", "text"}
                }
                self._think_parser = ThinkTagParser()
                self._emitted_in_active_text = False
                self._suppressed_in_active_text = False
                eager = block.get("text")
                if isinstance(eager, str) and eager:
                    out.extend(self._feed_text(eager))
                return out
            out_idx = self._alloc_index()
            self._index_map[src_idx] = out_idx
            out.append((event_name, {**payload, "index": out_idx}))
            return out

        if event_name == "content_block_delta":
            src_idx = _block_index(payload)
            if src_idx == self._active_text_source_index:
                delta = payload.get("delta")
                if isinstance(delta, Mapping) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        return self._feed_text(text)
                return []
            out_idx = self._index_map.get(src_idx, src_idx)
            return [(event_name, {**payload, "index": out_idx})]

        if event_name == "content_block_stop":
            src_idx = _block_index(payload)
            if src_idx == self._active_text_source_index:
                return self._finish_active_text()
            out_idx = self._index_map.pop(src_idx, src_idx)
            return [(event_name, {**payload, "index": out_idx})]

        if event_name in {"message_delta", "message_stop"}:
            out = self._finish_active_text()
            if self._next_index == 0 and self._saw_suppressed_only:
                self._saw_suppressed_only = False
                idx = self._alloc_index()
                out.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": idx,
                            "content_block": {
                                "type": "text",
                                "text": "",
                                **self._last_suppressed_template,
                            },
                        },
                    )
                )
                out.append(
                    (
                        "content_block_stop",
                        {"type": "content_block_stop", "index": idx},
                    )
                )
            out.append((event_name, payload))
            return out

        return [(event_name, payload)]

    def flush(self) -> list[tuple[str, dict[str, Any]]]:
        """Flush any unclosed active text block at stream end."""

        return self._finish_active_text()


def _format_sse_frame(event_name: str, payload: Mapping[str, Any]) -> str:
    return (
        f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


async def iter_anthropic_sse_frames(
    chunks: AsyncIterator[bytes],
    shape: ResponseShape | None = None,
    *,
    extract_think_tags: bool = False,
    output_reasoning: bool = True,
) -> AsyncIterator[str]:
    """Yield complete validated SSE frames and require ``message_stop``.

    ``shape`` is tallied from the payload this function has already parsed, so
    describing the reply costs no second parse and no buffered copy.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    terminal_seen = False
    normalizer = (
        AnthropicMessagesThinkTagNormalizer(output_reasoning=output_reasoning)
        if extract_think_tags
        else None
    )

    def _emit_normalized(
        event_name: str, payload: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        if normalizer is None:
            return [(event_name, payload)]
        return normalizer.feed(event_name, payload)

    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            frame, event_name, payload = _validated_frame(raw)
            if frame is None or event_name is None or payload is None:
                continue
            if event_name == _ERROR_EVENT:
                raise anthropic_stream_failure(payload)
            if normalizer is None:
                terminal_seen = terminal_seen or event_name == _TERMINAL_EVENT
                _note_frame_shape(shape, event_name, payload)
                yield frame
            else:
                for norm_event, norm_payload in _emit_normalized(event_name, payload):
                    terminal_seen = terminal_seen or norm_event == _TERMINAL_EVENT
                    _note_frame_shape(shape, norm_event, norm_payload)
                    yield _format_sse_frame(norm_event, norm_payload)

    buffer += decoder.decode(b"", final=True)
    buffer = buffer.replace("\r\n", "\n")
    if buffer.strip():
        frame, event_name, payload = _validated_frame(buffer)
        if frame is not None and event_name is not None and payload is not None:
            if event_name == _ERROR_EVENT:
                raise anthropic_stream_failure(payload)
            if normalizer is None:
                terminal_seen = terminal_seen or event_name == _TERMINAL_EVENT
                _note_frame_shape(shape, event_name, payload)
                yield frame
            else:
                for norm_event, norm_payload in _emit_normalized(event_name, payload):
                    terminal_seen = terminal_seen or norm_event == _TERMINAL_EVENT
                    _note_frame_shape(shape, norm_event, norm_payload)
                    yield _format_sse_frame(norm_event, norm_payload)
    if normalizer is not None:
        for norm_event, norm_payload in normalizer.flush():
            terminal_seen = terminal_seen or norm_event == _TERMINAL_EVENT
            _note_frame_shape(shape, norm_event, norm_payload)
            yield _format_sse_frame(norm_event, norm_payload)
    if not terminal_seen:
        raise TruncatedProviderStreamError(
            "Anthropic Messages stream ended without message_stop."
        )


def _validated_frame(
    raw: str,
) -> tuple[str | None, str | None, dict[str, object] | None]:
    event_name: str | None = None
    data_parts: list[str] = []
    for line in raw.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].lstrip())

    if not data_parts:
        return None, event_name, None
    payload = json.loads("\n".join(data_parts))
    if not isinstance(payload, dict):
        raise ValueError("Anthropic Messages SSE data must be a JSON object.")
    payload_type = payload.get("type")
    if not isinstance(payload_type, str) or not payload_type:
        raise ValueError("Anthropic Messages SSE event is missing a type.")
    if event_name is not None and event_name != payload_type:
        raise ValueError(
            "Anthropic Messages SSE event name does not match its payload type."
        )
    normalized_event = event_name or payload_type
    return (
        f"event: {normalized_event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n",
        normalized_event,
        payload,
    )


_ANTHROPIC_SHAPE_FIELDS = {
    "thinking_delta": ("thinking", "thinking"),
    "signature_delta": ("signature", "signature"),
    "text_delta": ("content", "text"),
    "input_json_delta": ("tool_calls", "partial_json"),
}


def _note_frame_shape(
    shape: ResponseShape | None, event_name: str | None, payload: object
) -> None:
    """Tally one already-parsed Anthropic SSE frame, storing no text.

    The Anthropic protocol names its channels differently from Chat
    Completions, and translating them into OpenAI's words here would be a
    second, worse translation. The delta type is recorded under the name the
    protocol uses, next to the one shared name (``content``) that means the
    same thing in both.

    Since 7.103.0 it also counts what a reply says about its reasoning when
    the text is withheld: thinking blocks, those that closed with no thinking
    text but a signature (``display: omitted`` -- the model thought, the
    provider kept the text), ``redacted_thinking`` blocks, and the
    ``output_tokens_details.thinking_tokens`` the final usage reports, which
    is also handed to the request's reported-usage collector.

    Since 8.20.1 a ``tool_use`` block's argument fragments are accumulated as
    they arrive and measured once at ``content_block_stop``: the tool's name,
    how many characters its arguments carried, whether they parsed and a
    three-word class. The fragments are held only for the life of the block and
    are never stored. This is the tally only -- the frames relayed to the
    client are untouched.
    """
    if (
        event_name == "message_delta"
        and isinstance(payload, Mapping)
        and isinstance(payload.get("usage"), Mapping)
    ):
        record_reported_reasoning(payload.get("usage"))
    if shape is None:
        return
    shape.note_chunk(time.monotonic())
    if not isinstance(payload, Mapping):
        return
    if event_name == "message_delta":
        delta = payload.get("delta")
        if isinstance(delta, Mapping):
            shape.note_finish(delta.get("stop_reason"))
        usage = payload.get("usage")
        shape.note_usage(usage)
        tokens = reported_reasoning_tokens(usage)
        if tokens is not None:
            shape.note_reasoning_tokens(tokens)
        return
    if event_name == "content_block_start":
        _note_block_start(shape, payload)
        return
    if event_name == "content_block_stop":
        _note_block_stop(shape, payload)
        return
    if event_name != "content_block_delta":
        return
    delta = payload.get("delta")
    if not isinstance(delta, Mapping):
        return
    mapped = _ANTHROPIC_SHAPE_FIELDS.get(str(delta.get("type")))
    if mapped is None:
        return
    name, text_key = mapped
    text = delta.get(text_key)
    size = len(text) if isinstance(text, str) else 0
    shape.note_field(name, size)
    if name in {"thinking", "signature"}:
        block = shape.open_thinking.get(_block_index(payload))
        if block is not None:
            block[0 if name == "thinking" else 1] += size
    elif name == "tool_calls" and isinstance(text, str):
        # 8.20.1: the fragments of one tool call's arguments, held only until
        # its ``content_block_stop`` so the tally can say whether they parsed.
        # Never stored; the block's name is element zero.
        carried = shape.open_tool_calls.get(_block_index(payload))
        if carried is not None:
            carried.append(text)


def _block_index(payload: Mapping[Any, Any]) -> int:
    index = payload.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else -1


def _note_block_start(shape: ResponseShape, payload: Mapping[Any, Any]) -> None:
    block = payload.get("content_block")
    if not isinstance(block, Mapping):
        return
    kind = block.get("type")
    if kind == "thinking":
        shape.count_reasoning("thinking_blocks")
        thinking = block.get("thinking")
        signature = block.get("signature")
        shape.open_thinking[_block_index(payload)] = [
            len(thinking) if isinstance(thinking, str) else 0,
            len(signature) if isinstance(signature, str) else 0,
        ]
    elif kind == "redacted_thinking":
        shape.count_reasoning("redacted_thinking_blocks")
    elif kind == "tool_use":
        # The protocol opens a tool call with ``input: {}`` and streams the
        # arguments as ``input_json_delta`` fragments. A start frame that
        # already carries a populated ``input`` therefore handed the arguments
        # over in a form this tally never sees, so the call is left unmeasured
        # rather than recorded as an empty string that "failed to parse".
        eager = block.get("input")
        if not (isinstance(eager, Mapping) and eager):
            name = block.get("name")
            shape.open_tool_calls[_block_index(payload)] = [
                name if isinstance(name, str) else ""
            ]


def _note_block_stop(shape: ResponseShape, payload: Mapping[Any, Any]) -> None:
    index = _block_index(payload)
    carried = shape.open_tool_calls.pop(index, None)
    if carried is not None:
        shape.note_tool_call(carried[0], "".join(carried[1:]))
    block = shape.open_thinking.pop(index, None)
    if block is None:
        return
    thinking_chars, signature_chars = block
    if thinking_chars == 0 and signature_chars > 0:
        shape.count_reasoning("withheld_thinking_blocks")
