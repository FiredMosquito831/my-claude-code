"""Every door records a tool call's four facts, and none of its arguments.

A tool call whose arguments fail to parse, or run away to hundreds of
kilobytes, used to leave no trace anywhere: MCC forwarded the bytes, the
client threw its own ``InputValidationError``, and the request row said only
how many calls the reply carried. Measured on the ChatGPT OAuth door, 5 of 121
``ToolSearch`` calls carried up to 332,121 characters of arguments and 0 of
1,831 calls on every other door did -- and none of it was visible.

8.20.1 records four facts per call -- name, character count, whether it parsed,
and a three-word class -- on the attempt's existing ``response_shape``. **It
records; it never alters.** Nothing here may change a frame the client
receives, which is why every door below is proved twice: the tally is right,
and the bytes are identical with and without it.
"""

import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.core import wire_capture
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.anthropic.streaming.emitter import format_sse_event
from my_claude_code.core.wire_capture import (
    ResponseShape,
    install_wire_trace,
)
from my_claude_code.providers.anthropic_messages.streaming import (
    iter_anthropic_sse_frames,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_responses.streaming import (
    ResponsesStreamConverter,
    note_responses_event_shape,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.sse_replay import _FixedUUID
from tests.providers.support import passthrough_rate_limiter, profiled_provider


@pytest.fixture(autouse=True)
def untracked(monkeypatch) -> None:
    """Start every test from "no request is being captured"."""
    monkeypatch.setattr(
        wire_capture, "_WIRE_TRACE", ContextVar("fcc_wire_trace_test", default=None)
    )


def _shape(limit: int = 131_072) -> ResponseShape:
    install_wire_trace(tool_argument_limit=limit)
    shape = wire_capture.start_response_shape()
    assert shape is not None
    return shape


# ------------------------------------------------------- C/D: OpenAI Responses


def _responses_events(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "item": {
                "id": item.get("id", "fc_1"),
                "type": "function_call",
                "name": item.get("name", "Read"),
                "arguments": "",
            },
        },
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", **item},
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 9},
            },
        },
    ]


def _tally_responses(events: list[dict[str, Any]], shape: ResponseShape) -> None:
    for event in events:
        note_responses_event_shape(shape, event)


def test_the_responses_door_records_a_parsed_tool_call() -> None:
    """One ordinary call: its name, its length, and that it parsed."""
    arguments = '{"file_path": "/tmp/a.txt"}'
    shape = _shape()

    _tally_responses(
        _responses_events({"id": "fc_1", "name": "Read", "arguments": arguments}),
        shape,
    )

    assert shape.payload()["tool_calls"] == {
        "calls": 1,
        "parsed": 1,
        "max_chars": len(arguments),
        "over_bound": 0,
        "unparseable": 0,
        "not_an_object": 0,
        "largest": [
            {"name": "Read", "chars": len(arguments), "parsed": True, "class": None}
        ],
    }


def test_the_responses_door_records_an_unparseable_tool_call() -> None:
    shape = _shape()

    _tally_responses(
        _responses_events({"id": "fc_1", "name": "Bash", "arguments": '{"command":'}),
        shape,
    )

    entry = shape.payload()["tool_calls"]["largest"][0]
    assert entry["class"] == "unparseable"
    assert entry["parsed"] is False


def test_the_responses_door_records_a_call_over_the_bound() -> None:
    """The measured anomaly: hundreds of kilobytes, never parsed."""
    arguments = '{"query": "' + "x" * 332_000 + '"}'
    shape = _shape()

    _tally_responses(
        _responses_events({"id": "fc_1", "name": "ToolSearch", "arguments": arguments}),
        shape,
    )

    entry = shape.payload()["tool_calls"]["largest"][0]
    assert entry["chars"] == len(arguments)
    assert entry["class"] == "over_bound"
    assert entry["parsed"] is None


def test_the_responses_door_records_nothing_for_a_reply_without_a_call() -> None:
    shape = _shape()

    _tally_responses(
        [
            {"type": "response.created", "response": {"status": "in_progress"}},
            {
                "type": "response.completed",
                "response": {"status": "completed", "usage": {"output_tokens": 1}},
            },
        ],
        shape,
    )

    assert "tool_calls" not in shape.payload()


def test_a_function_call_item_with_no_argument_string_is_not_measured() -> None:
    """Absent is not zero: an item that carried no arguments is not counted."""
    shape = _shape()

    _tally_responses(
        _responses_events({"id": "fc_1", "name": "Read", "arguments": None}),
        shape,
    )

    assert "tool_calls" not in shape.payload()


def test_the_responses_converter_emits_the_same_frames_with_the_tally() -> None:
    """Nothing changes for the client: the converter never sees the shape."""
    events = _responses_events(
        {"id": "fc_1", "name": "Read", "arguments": '{"file_path": "/tmp/a.txt"}'}
    )

    def frames(with_tally: bool) -> list[str]:
        ledger = AnthropicStreamLedger("msg_fixed", "gpt-6-luna", input_tokens=0)
        converter = ResponsesStreamConverter(ledger)
        shape = _shape() if with_tally else None
        emitted = [ledger.message_start()]
        for event in events:
            if shape is not None:
                note_responses_event_shape(shape, event)
            emitted.extend(converter.feed(event))
        emitted.extend(converter.finish())
        return emitted

    tallied = frames(True)
    untallied = frames(False)

    assert tallied == untallied
    assert any("tool_use" in frame for frame in tallied)


# ------------------------------------------------- A/B: Anthropic Messages


def _messages_stream(*frames: str) -> bytes:
    return "".join(frames).encode("utf-8")


def _message_start() -> str:
    return format_sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "test-model",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 4, "output_tokens": 1},
            },
        },
    )


def _tool_block_start(
    index: int, name: str, eager: dict[str, Any] | None = None
) -> str:
    return format_sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": f"toolu_{index}",
                "name": name,
                "input": eager if eager is not None else {},
            },
        },
    )


def _tool_json(index: int, partial: str) -> str:
    return format_sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        },
    )


def _block_stop(index: int) -> str:
    return format_sse_event(
        "content_block_stop", {"type": "content_block_stop", "index": index}
    )


def _messages_end() -> tuple[str, str]:
    return (
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 3},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    )


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _relay(body: bytes, shape: ResponseShape | None) -> list[str]:
    return [frame async for frame in iter_anthropic_sse_frames(_chunks(body), shape)]


@pytest.mark.asyncio
async def test_the_messages_door_records_a_tool_call_at_block_stop() -> None:
    """The arguments arrive as fragments and are measured once, at the stop."""
    body = _messages_stream(
        _message_start(),
        _tool_block_start(0, "Read"),
        _tool_json(0, '{"file_path":'),
        _tool_json(0, ' "/tmp/a.txt"}'),
        _block_stop(0),
        *_messages_end(),
    )
    shape = _shape()

    await _relay(body, shape)

    arguments = '{"file_path": "/tmp/a.txt"}'
    assert shape.payload()["tool_calls"] == {
        "calls": 1,
        "parsed": 1,
        "max_chars": len(arguments),
        "over_bound": 0,
        "unparseable": 0,
        "not_an_object": 0,
        "largest": [
            {"name": "Read", "chars": len(arguments), "parsed": True, "class": None}
        ],
    }


@pytest.mark.asyncio
async def test_the_messages_door_records_a_half_written_call_as_unparseable() -> None:
    body = _messages_stream(
        _message_start(),
        _tool_block_start(0, "Bash"),
        _tool_json(0, '{"command": "ls -'),
        _block_stop(0),
        *_messages_end(),
    )
    shape = _shape()

    await _relay(body, shape)

    entry = shape.payload()["tool_calls"]["largest"][0]
    assert entry["name"] == "Bash"
    assert entry["class"] == "unparseable"


@pytest.mark.asyncio
async def test_a_tool_block_that_carried_its_input_up_front_is_not_measured() -> None:
    """Its arguments never crossed this tally, so it is not called a failure."""
    body = _messages_stream(
        _message_start(),
        _tool_block_start(0, "Read", eager={"file_path": "/tmp/a.txt"}),
        _block_stop(0),
        *_messages_end(),
    )
    shape = _shape()

    await _relay(body, shape)

    assert "tool_calls" not in shape.payload()


@pytest.mark.asyncio
async def test_the_messages_door_relays_the_same_frames_with_the_tally() -> None:
    body = _messages_stream(
        _message_start(),
        _tool_block_start(0, "Read"),
        _tool_json(0, '{"file_path": "/tmp/a.txt"}'),
        _block_stop(0),
        *_messages_end(),
    )

    tallied = await _relay(body, _shape())
    untallied = await _relay(body, None)

    assert tallied == untallied
    assert any("tool_use" in frame for frame in tallied)


# ------------------------------------------------------- E: Chat Completions


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    frame = {
        "id": "chatcmpl-tally",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(frame)}\n\n"


def _body(*chunks: str) -> bytes:
    return ("".join(chunks) + "data: [DONE]\n\n").encode("utf-8")


def _tool_call_stream(name: str, arguments: str) -> bytes:
    return _body(
        _chunk({"role": "assistant", "content": "I will read it."}),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                ]
            }
        ),
        _chunk({}, finish_reason="tool_calls"),
    )


def _config() -> ProviderConfig:
    # Holdback off: it buffers early frames on a wall clock, and a byte
    # comparison must not depend on how fast the machine is.
    return ProviderConfig(
        api_key="test-key",
        base_url="http://sse.invalid/v1",
        rate_limit=1_000_000,
        rate_window=60,
        commit_holdback_seconds=0.0,
        commit_holdback_chars=0,
    )


async def _replay(body: bytes) -> list[str]:
    """Drive the real Chat stream over a fake upstream, returning its frames."""
    provider = profiled_provider(
        "groq", _config(), rate_limiter=passthrough_rate_limiter()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
            request=request,
        )

    provider._client = AsyncOpenAI(
        api_key="test-key",
        base_url=provider._config.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    request = make_messages_request()
    frames: list[str] = []
    try:
        with patch("uuid.uuid4", _FixedUUID()):
            stream = provider.stream_response(request, 11, request_id="req-tally")
            frames.extend([frame async for frame in stream])
    finally:
        await provider.cleanup()
    return frames


@pytest.mark.asyncio
async def test_the_chat_door_records_every_tool_call_the_ledger_assembled() -> None:
    arguments = '{"file_path": "/tmp/a.txt"}'
    trace = install_wire_trace()

    await _replay(_tool_call_stream("Read", arguments))

    tool_calls = trace.responses[0]["tool_calls"]
    assert tool_calls["calls"] == 1
    assert tool_calls["parsed"] == 1
    assert tool_calls["max_chars"] == len(arguments)
    assert tool_calls["largest"] == [
        {"name": "Read", "chars": len(arguments), "parsed": True, "class": None}
    ]


@pytest.mark.asyncio
async def test_the_chat_door_records_a_half_written_call_as_unparseable() -> None:
    trace = install_wire_trace()

    await _replay(_tool_call_stream("Bash", '{"command": "ls -'))

    entry = trace.responses[0]["tool_calls"]["largest"][0]
    assert entry["name"] == "Bash"
    assert entry["class"] == "unparseable"


@pytest.mark.asyncio
async def test_the_chat_door_records_nothing_for_a_text_only_reply() -> None:
    trace = install_wire_trace()

    await _replay(
        _body(
            _chunk({"role": "assistant", "content": "done"}),
            _chunk({}, finish_reason="stop"),
        )
    )

    assert "tool_calls" not in trace.responses[0]


@pytest.mark.asyncio
async def test_the_chat_door_emits_the_same_frames_with_the_tally() -> None:
    body = _tool_call_stream("Read", '{"file_path": "/tmp/a.txt"}')

    untracked_frames = await _replay(body)
    trace = install_wire_trace()
    tracked_frames = await _replay(body)

    assert tracked_frames == untracked_frames
    assert "tool_calls" in trace.responses[0]
