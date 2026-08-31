"""End a committed Anthropic stream as a valid message instead of an error.

A stream that has already shown the reader text cannot fall back to another
model -- the client has seen the first model's words, and no second model can
un-send them. Until now that meant the only thing left to do with a stall, a
mid-stream 5xx or a dropped transport was to raise, so the client received an
HTTP/SSE error *after* a partial answer and the turn died.

The protocol has a better ending available. Every block the stream left open
can be closed, and one ``message_delta``/``message_stop`` pair turns the
partial answer into a short but structurally complete message. This module is
the bookkeeping that makes that ending honest: it follows the frames already
forwarded closely enough to know which blocks are open, how much the reader
actually saw, and whether the stream is in the one state that cannot be closed
truthfully -- a ``tool_use`` block whose arguments never finished arriving.

Deliberately free of any judgement about *when* to use it: the executor owns
that decision, this owns the frames.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from ..stream_contracts import SSEEvent, parse_sse_text
from ..tokens import count_text_tokens
from .emitter import format_sse_event

#: Sent as the ``stop_reason`` of a truncated message. The Anthropic protocol
#: has exactly one value meaning "cut short rather than finished", and this is
#: it: ``end_turn`` would tell Claude Code the model had said everything it
#: meant to say, which for a half-written plan is the dangerous lie. The real
#: cause is recorded on the attempt row instead, where it can be read without
#: being mistaken for a completed answer.
TRUNCATED_STOP_REASON = "max_tokens"


@dataclass(frozen=True, slots=True)
class StreamTruncation:
    """The frames that end a committed stream, and what the reader received."""

    frames: tuple[str, ...]
    chars: int
    blocks: int
    reason: str
    stop_reason_sent: str | None
    ended_cleanly: bool

    def as_params(self) -> dict[str, object]:
        """The request-log payload for ``params.truncated_after_commit``."""
        return {
            "chars": self.chars,
            "blocks": self.blocks,
            "reason": self.reason,
            "stop_reason_sent": self.stop_reason_sent,
            "ended_cleanly": self.ended_cleanly,
        }


@dataclass(slots=True)
class CommittedStreamTracker:
    """Follow a forwarded Anthropic SSE stream closely enough to end it.

    Fed every chunk that reaches the client, in order. Chunks are buffered on
    frame boundaries rather than parsed as they arrive, because a provider is
    free to split one SSE frame across two yields and half a frame parses as
    nothing at all.

    Nothing here decides policy. ``closable`` answers only the protocol
    question -- can the frames already sent be completed into a valid message
    -- and the two counters exist so the request log can say how much of an
    answer the reader was actually left with.
    """

    _buffer: str = ""
    _open_blocks: dict[int, str] = field(default_factory=dict)
    _tool_json: dict[int, list[str]] = field(default_factory=dict)
    _output_parts: list[str] = field(default_factory=list)
    blocks: int = 0
    chars: int = 0
    input_tokens: int = 0
    output_tokens: int | None = None
    stop_reason_seen: bool = False

    def observe(self, chunk: str) -> None:
        """Record one chunk on its way to the client."""
        self._buffer += chunk
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            for event in parse_sse_text(frame):
                self._observe_event(event)

    def _observe_event(self, event: SSEEvent) -> None:
        name = event.event or str(event.data.get("type", ""))
        if name == "message_start":
            self._observe_message_start(event.data)
        elif name == "content_block_start":
            self._observe_block_start(event.data)
        elif name == "content_block_delta":
            self._observe_delta(event.data)
        elif name == "content_block_stop":
            index = event.data.get("index")
            if isinstance(index, int):
                self._open_blocks.pop(index, None)
                self._tool_json.pop(index, None)
        elif name == "message_delta":
            self._observe_message_delta(event.data)

    def _observe_message_start(self, data: dict[str, Any]) -> None:
        message = data.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
            self.input_tokens = usage["input_tokens"]

    def _observe_block_start(self, data: dict[str, Any]) -> None:
        index = data.get("index")
        block = data.get("content_block")
        if not isinstance(index, int) or not isinstance(block, dict):
            return
        self._open_blocks[index] = str(block.get("type", ""))
        self.blocks += 1
        # A ``text`` block may carry its whole body on the start frame; that
        # text is as visible to the reader as any delta and has to be counted.
        eager = block.get("text")
        if isinstance(eager, str) and eager:
            self.chars += len(eager)
            self._output_parts.append(eager)

    def _observe_delta(self, data: dict[str, Any]) -> None:
        index = data.get("index")
        delta = data.get("delta")
        if not isinstance(index, int) or not isinstance(delta, dict):
            return
        kind = delta.get("type")
        if kind == "text_delta":
            text = str(delta.get("text", ""))
            self.chars += len(text)
            self._output_parts.append(text)
        elif kind == "thinking_delta":
            self._output_parts.append(str(delta.get("thinking", "")))
        elif kind == "input_json_delta":
            fragment = str(delta.get("partial_json", ""))
            self._tool_json.setdefault(index, []).append(fragment)
            self._output_parts.append(fragment)

    def _observe_message_delta(self, data: dict[str, Any]) -> None:
        delta = data.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason"):
            self.stop_reason_seen = True
        usage = data.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
            self.output_tokens = usage["output_tokens"]

    @property
    def incomplete_tool_use(self) -> bool:
        """Whether a ``tool_use`` block is open on arguments that never parsed.

        The one state with no honest ending. Closing the block would leave
        ``sse_aggregation`` to substitute ``input={}`` for the JSON it cannot
        read, and Claude Code executes tool calls: a ``Bash`` call with silently
        empty arguments is worse for the reader than an error saying the turn
        died. Arguments that *did* finish arriving are closable -- the block is
        complete, only the message around it is not.
        """
        for index, block_type in self._open_blocks.items():
            if block_type != "tool_use":
                continue
            emitted = "".join(self._tool_json.get(index, []))
            if not emitted.strip():
                return True
            try:
                json.loads(emitted)
            except json.JSONDecodeError:
                return True
        return False

    @property
    def closable(self) -> bool:
        """Whether these frames can be completed into a valid message.

        Three protocol facts, no policy. There must be a content block to
        close, because a stream with none has shown the reader nothing and its
        "message" would be an empty envelope the contract validator rejects.
        The message must not already carry a ``stop_reason``, because a second
        ``message_delta`` would overwrite an ending the model itself chose. And
        no tool call may be left half-written.
        """
        return (
            self.blocks > 0
            and not self.stop_reason_seen
            and not self.incomplete_tool_use
        )

    def close(self, *, reason: str) -> StreamTruncation:
        """Build the frames that end the message, in protocol order.

        Every open block is closed first -- ``stream_contracts`` rejects a
        message with an unclosed block -- then exactly one ``message_delta``
        carrying the truncation stop reason and the usage actually consumed,
        then ``message_stop``.
        """
        frames = [
            format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": index}
            )
            for index in sorted(self._open_blocks)
        ]
        frames.append(
            format_sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": TRUNCATED_STOP_REASON,
                        "stop_sequence": None,
                    },
                    "usage": {
                        "input_tokens": self.input_tokens,
                        "output_tokens": self._consumed_output_tokens(),
                    },
                },
            )
        )
        frames.append(format_sse_event("message_stop", {"type": "message_stop"}))
        return StreamTruncation(
            frames=tuple(frames),
            chars=self.chars,
            blocks=self.blocks,
            reason=reason,
            stop_reason_sent=TRUNCATED_STOP_REASON,
            ended_cleanly=True,
        )

    def abandoned(self, *, reason: str) -> StreamTruncation:
        """Record a committed stream that could *not* be ended cleanly.

        No frames: the caller re-raises and the client gets today's error. The
        counters still go to the request log, because "the turn died 900
        characters in, inside a tool call" is the sentence the reader needs and
        the exception alone cannot say it.
        """
        return StreamTruncation(
            frames=(),
            chars=self.chars,
            blocks=self.blocks,
            reason=reason,
            stop_reason_sent=None,
            ended_cleanly=False,
        )

    def _consumed_output_tokens(self) -> int:
        """What the model actually produced, as far as anything can know.

        The upstream's own count when it sent one; otherwise the same estimate
        the provider ledger would have published had the stream finished. A
        stalled stream usually dies before its ``message_delta``, and reporting
        zero output tokens for an answer the reader can see on screen is a
        worse lie than an estimate.
        """
        if self.output_tokens is not None:
            return self.output_tokens
        return count_text_tokens("".join(self._output_parts))
