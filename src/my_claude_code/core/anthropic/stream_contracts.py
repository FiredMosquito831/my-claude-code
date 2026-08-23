"""Neutral SSE parsing and Anthropic stream shape assertions.

Used by default CI contract tests and by opt-in live smoke scenarios.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .server_tool_sse import (
    SERVER_TOOL_USE,
    WEB_FETCH_TOOL_RESULT,
    WEB_SEARCH_TOOL_RESULT,
)

# Content blocks that only use content_block_start/stop (no deltas), including
# Anthropic server tools and eager text emitted in a single start event.
_NO_DELTA_BLOCK_KINDS = frozenset(
    {
        SERVER_TOOL_USE,
        WEB_SEARCH_TOOL_RESULT,
        WEB_FETCH_TOOL_RESULT,
        "text_eager",
        "redacted_thinking",
    }
)

_ALLOWED_BLOCK_START_TYPES = frozenset(
    {
        "text",
        "thinking",
        "tool_use",
        "redacted_thinking",
        SERVER_TOOL_USE,
        WEB_SEARCH_TOOL_RESULT,
        WEB_FETCH_TOOL_RESULT,
    }
)


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: dict[str, Any]
    raw: str


def parse_sse_lines(lines: Iterable[str]) -> list[SSEEvent]:
    events: list[SSEEvent] = []
    current_event = ""
    data_parts: list[str] = []
    raw_parts: list[str] = []

    for line in lines:
        stripped = line.rstrip("\r\n")
        if stripped == "":
            _append_event(events, current_event, data_parts, raw_parts)
            current_event = ""
            data_parts = []
            raw_parts = []
            continue
        raw_parts.append(stripped)
        if stripped.startswith("event:"):
            current_event = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("data:"):
            data_parts.append(stripped.split(":", 1)[1].strip())

    _append_event(events, current_event, data_parts, raw_parts)
    return events


def parse_sse_text(text: str) -> list[SSEEvent]:
    return parse_sse_lines(text.splitlines())


def _append_event(
    events: list[SSEEvent],
    current_event: str,
    data_parts: list[str],
    raw_parts: list[str],
) -> None:
    if not current_event and not data_parts:
        return
    data_text = "\n".join(data_parts)
    data: dict[str, Any]
    try:
        parsed = json.loads(data_text) if data_text else {}
        data = parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        data = {"raw": data_text}
    events.append(SSEEvent(current_event, data, "\n".join(raw_parts)))


def assert_anthropic_stream_contract(
    events: list[SSEEvent], *, allow_error: bool = False
) -> None:
    """Check minimal Anthropic-style SSE invariants and block nesting.

    Does *not* assert strict event ordering (e.g. :class:`message_delta` vs
    content blocks). Successful streams end in ``message_stop``; when explicitly
    allowed, failed streams may instead end in a protocol-native ``error``.
    """
    assert events, "stream produced no SSE events"
    event_names = [event.event for event in events]
    assert "message_start" in event_names, event_names
    allowed_terminal_events = (
        {"message_stop", "error"} if allow_error else {"message_stop"}
    )
    assert event_names[-1] in allowed_terminal_events, event_names

    open_blocks: dict[int, str] = {}
    seen_blocks: set[int] = set()
    for event in events:
        if event.event == "error" and not allow_error:
            raise AssertionError(f"unexpected SSE error event: {event.data}")

        if event.event == "content_block_start":
            index = event_index(event)
            block = event.data.get("content_block", {})
            assert isinstance(block, dict), event.data
            block_type = str(block.get("type", ""))
            assert block_type in _ALLOWED_BLOCK_START_TYPES, event.data
            assert index not in open_blocks, f"block {index} started twice"
            assert index not in seen_blocks, f"block {index} reused after stop"
            if block_type == "text" and str(block.get("text", "")).strip():
                storage = "text_eager"
            else:
                storage = block_type
            open_blocks[index] = storage
            seen_blocks.add(index)
            continue

        if event.event == "content_block_delta":
            index = event_index(event)
            assert index in open_blocks, f"delta for unopened block {index}"
            kind = open_blocks[index]
            assert kind not in _NO_DELTA_BLOCK_KINDS, (
                f"unexpected delta for start/stop-only block {kind} at index {index}"
            )
            delta = event.data.get("delta", {})
            assert isinstance(delta, dict), event.data
            delta_type = str(delta.get("type", ""))
            if kind == "thinking":
                assert delta_type in (
                    "thinking_delta",
                    "signature_delta",
                ), f"block {index} is {kind}, got {delta_type}"
                continue
            expected = {
                "text": "text_delta",
                "tool_use": "input_json_delta",
            }[kind]
            assert delta_type == expected, f"block {index} is {kind}, got {delta_type}"
            continue

        if event.event == "content_block_stop":
            index = event_index(event)
            assert index in open_blocks, f"stop for unopened block {index}"
            open_blocks.pop(index)

    assert not open_blocks, f"unclosed blocks: {open_blocks}"
    assert seen_blocks, "stream did not emit any content blocks"


def event_names(events: list[SSEEvent]) -> list[str]:
    return [event.event for event in events]


def text_content(events: list[SSEEvent]) -> str:
    parts: list[str] = []
    for event in events:
        if event.event == "content_block_start":
            block = event.data.get("content_block", {})
            if isinstance(block, dict) and block.get("type") == "text":
                eager = str(block.get("text", ""))
                if eager:
                    parts.append(eager)
        delta = event.data.get("delta", {})
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            parts.append(str(delta.get("text", "")))
    return "".join(parts)


def thinking_content(events: list[SSEEvent]) -> str:
    parts: list[str] = []
    for event in events:
        delta = event.data.get("delta", {})
        if isinstance(delta, dict) and delta.get("type") == "thinking_delta":
            parts.append(str(delta.get("thinking", "")))
    return "".join(parts)


def has_tool_use(events: list[SSEEvent]) -> bool:
    for event in events:
        block = event.data.get("content_block", {})
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return True
    return False


def event_index(event: SSEEvent) -> int:
    """Return the content block ``index`` field from an SSE payload (strict)."""
    value = event.data.get("index")
    assert isinstance(value, int), event.data
    return value


# Frames that are known to show the reader nothing: the envelope around an
# answer rather than any part of it. Named explicitly rather than inferred, so
# a frame type nobody anticipated falls outside the set and is treated as
# possibly-content instead of being silently assumed harmless.
_SCAFFOLDING_EVENT_TYPES = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_stop",
        "ping",
    }
)


# The one fragment a provider may hand routing that is not output. A stream
# holding reasoning back has nothing to show and has committed nothing, which
# from outside is indistinguishable from a stream that has produced nothing at
# all -- and the two deserve very different deadlines. Empty so that any
# consumer forwarding it writes nothing, and not valid SSE so it can never be
# confused with a frame.
REASONING_HEARTBEAT = ""


# The delta types inside a ``content_block_delta`` that carry reasoning rather
# than any part of the answer. ``signature_delta`` is the cryptographic tail of
# a thinking block and is meaningless without it, so it travels with it.
_REASONING_DELTA_TYPES = frozenset({"thinking_delta", "signature_delta"})


def _event_is_scaffolding(event: SSEEvent, reasoning_commits: bool) -> bool:
    kind = event.data.get("type") or event.event
    if kind in _SCAFFOLDING_EVENT_TYPES:
        return True
    if reasoning_commits or kind != "content_block_delta":
        return False
    delta = event.data.get("delta")
    return isinstance(delta, dict) and delta.get("type") in _REASONING_DELTA_TYPES


def sse_is_scaffolding(text: str, *, reasoning_commits: bool = True) -> bool:
    """Whether an SSE fragment is envelope rather than answer.

    This decides where the model-fallback commit boundary starts. The provider
    holds a brief window of SSE so an early cutoff can be retried invisibly,
    and that window used to be anchored to the *first frame of any kind* --
    which meant a model that sent a `message_start` and then stalled had
    already committed the route, with nothing shown to the reader and no
    fallback possible. Measured on 21 days of real traffic: 393 requests ran
    the full 600s budget having produced only scaffolding, with a configured
    fallback chain sitting unused.

    Anchoring the window to the first non-scaffolding frame fixes that while
    keeping the window itself, which is what makes an immediate cutoff
    invisibly retryable -- held bytes have not reached the client yet.

    Note that ``message_delta`` is scaffolding despite carrying a ``delta``
    key: that delta holds a stop reason and a usage count, not any part of the
    answer. Classifying on the frame type rather than on the presence of a
    ``delta`` field is what keeps that right.

    Deliberately two-way, not three. An unrecognised frame is treated exactly
    like content: it starts the window, so unfamiliar output is delayed by the
    holdback rather than held indefinitely. A third "unknown" class would read
    as more precise while changing nothing any caller does.

    ``reasoning_commits=False`` moves reasoning deltas to the scaffolding side.
    A model that thinks aloud for the whole request budget and never writes an
    answer has shown the reader no answer to lose, so committing the route on
    it spends a configured chain on nothing: measured on this traffic, 44 of
    499 budget exhaustions were a stream that had only reasoned, and 490 of the
    499 never left ``route_attempt = 0``. Holding reasoning keeps the attempt uncommitted, so
    its share of the budget expires and the next model answers instead. The
    cost is that reasoning no longer streams live -- it arrives when the answer
    does, or when the buffer's byte cap forces a commit.
    """
    events = parse_sse_text(text)
    if not events:
        return False
    return all(_event_is_scaffolding(event, reasoning_commits) for event in events)


def sse_carries_content(text: str) -> bool:
    """Whether an SSE fragment moves the answer forward.

    The inverse question to :func:`sse_is_scaffolding`, asked per chunk for the
    whole length of a stream rather than once at its start, so it is answered
    by a substring scan instead of a JSON parse. That is exact rather than
    approximate here: in the Anthropic stream protocol only a
    ``content_block_delta`` can carry text, reasoning or tool arguments, and
    every other frame type is envelope.

    A chunk whose *text* happens to contain the phrase is a content chunk
    anyway, so the one way this can be wrong is a way that cannot matter.
    """
    return "content_block_delta" in text
