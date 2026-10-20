"""Validate and forward native Anthropic Messages SSE frames."""

import codecs
import json
import time
from collections.abc import AsyncIterator, Mapping

from my_claude_code.core.anthropic.upstream_errors import anthropic_stream_failure
from my_claude_code.core.reported_cost import (
    record_reported_reasoning,
    reported_reasoning_tokens,
)
from my_claude_code.core.wire_capture import ResponseShape
from my_claude_code.providers.stream_recovery import TruncatedProviderStreamError

_TERMINAL_EVENT = "message_stop"
_ERROR_EVENT = "error"


async def iter_anthropic_sse_frames(
    chunks: AsyncIterator[bytes], shape: ResponseShape | None = None
) -> AsyncIterator[str]:
    """Yield complete validated SSE frames and require ``message_stop``.

    ``shape`` is tallied from the payload this function has already parsed, so
    describing the reply costs no second parse and no buffered copy.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    terminal_seen = False
    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            frame, event_name, payload = _validated_frame(raw)
            if frame is None:
                continue
            if event_name == _ERROR_EVENT:
                raise anthropic_stream_failure(payload)
            terminal_seen = terminal_seen or event_name == _TERMINAL_EVENT
            _note_frame_shape(shape, event_name, payload)
            yield frame

    buffer += decoder.decode(b"", final=True)
    buffer = buffer.replace("\r\n", "\n")
    if buffer.strip():
        frame, event_name, payload = _validated_frame(buffer)
        if frame is not None:
            if event_name == _ERROR_EVENT:
                raise anthropic_stream_failure(payload)
            terminal_seen = terminal_seen or event_name == _TERMINAL_EVENT
            _note_frame_shape(shape, event_name, payload)
            yield frame
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


def _block_index(payload: Mapping[str, object]) -> int:
    index = payload.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else -1


def _note_block_start(shape: ResponseShape, payload: Mapping[str, object]) -> None:
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


def _note_block_stop(shape: ResponseShape, payload: Mapping[str, object]) -> None:
    block = shape.open_thinking.pop(_block_index(payload), None)
    if block is None:
        return
    thinking_chars, signature_chars = block
    if thinking_chars == 0 and signature_chars > 0:
        shape.count_reasoning("withheld_thinking_blocks")
