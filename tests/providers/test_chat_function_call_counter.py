"""The deprecated ``delta.function_call`` delta is counted, and nothing else.

The 2026-10-29 investigation could not rule out that a host answered with the
pre-``tool_calls`` single-call shape: ``chunks.py`` decodes the field, no
provider code reads it, and before 7.115.1 the reply shape counted only
``tool_calls`` -- so a turn whose call arrived in that field would look exactly
like a model that stopped on its own. The counter closes that blind spot.

Two things are proven here together, because either alone is not enough: the
field *is* counted when it arrives, and counting it changes not one byte of
what the client receives.
"""

import json
from contextvars import ContextVar
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.core import wire_capture
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.provider import _legacy_function_call_chars
from tests.providers.request_factory import make_messages_request
from tests.providers.sse_replay import _FixedUUID
from tests.providers.support import passthrough_rate_limiter, profiled_provider


@pytest.fixture(autouse=True)
def untracked(monkeypatch) -> None:
    """Start every test from "no request is being captured" (the trace lives
    in a ``ContextVar``, so a test asserting its absence is otherwise
    order-dependent)."""
    monkeypatch.setattr(
        wire_capture, "_WIRE_TRACE", ContextVar("fcc_wire_trace_test", default=None)
    )


def _chunk(delta: dict, finish_reason: str | None = None) -> str:
    frame = {
        "id": "chatcmpl-count",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(frame)}\n\n"


def _body(*chunks: str) -> bytes:
    return ("".join(chunks) + "data: [DONE]\n\n").encode("utf-8")


DEPRECATED_STREAM = _body(
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"function_call": {"name": "probe", "arguments": '{"x":1}'}}),
    _chunk({}, finish_reason="stop"),
)
MODERN_STREAM = _body(
    _chunk({"role": "assistant", "content": "I will read it."}),
    _chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }
            ]
        }
    ),
    _chunk({}, finish_reason="tool_calls"),
)
TEXT_STREAM = _body(
    _chunk({"role": "assistant", "content": "done"}),
    _chunk({}, finish_reason="stop"),
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
    """Drive the real stream over a fake upstream, returning its SSE frames."""

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
        # Deterministic ids so two runs of the same bytes compare equal (the
        # ledger mints a fresh uuid4 when it is built, so the patch has to
        # cover the call itself, not only the iteration).
        with patch("uuid.uuid4", _FixedUUID()):
            stream = provider.stream_response(request, 11, request_id="req-count")
            frames.extend([frame async for frame in stream])
    finally:
        await provider.cleanup()
    return frames


def _events(frames: list[str]) -> list[dict]:
    return [
        json.loads(line[len("data:") :].strip())
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("data:")
    ]


@pytest.mark.asyncio
async def test_the_deprecated_delta_is_counted_with_its_own_name() -> None:
    trace = install_wire_trace()
    await _replay(DEPRECATED_STREAM)

    shape = trace.responses[0]
    assert shape["fields"]["function_call"] == {
        "deltas": 1,
        "chars": len("probe") + len('{"x":1}'),
    }
    # Counted *beside* the modern field, not as it: nothing arrived there.
    assert "tool_calls" not in shape["fields"]


@pytest.mark.asyncio
async def test_counting_the_deprecated_delta_changes_no_emitted_frame() -> None:
    """The counter records; it never acts. Same bytes with and without it."""

    untracked_frames = await _replay(DEPRECATED_STREAM)
    trace = install_wire_trace()
    tracked_frames = await _replay(DEPRECATED_STREAM)

    assert tracked_frames == untracked_frames
    # And the client still receives exactly what it received before: the
    # frame carried no text and no call, so neither reaches the client.
    events = _events(tracked_frames)
    assert not [
        event
        for event in events
        if event.get("type") == "content_block_start"
        and event["content_block"].get("type") == "tool_use"
    ]
    assert [event["type"] for event in events][-1] == "message_stop"
    assert trace.responses, "the tracked run must have recorded a shape"


@pytest.mark.asyncio
async def test_a_modern_tool_call_stream_records_no_deprecated_delta() -> None:
    trace = install_wire_trace()
    frames = await _replay(MODERN_STREAM)

    shape = trace.responses[0]
    assert "function_call" not in shape["fields"]
    assert shape["fields"]["tool_calls"] == {"deltas": 1, "chars": 1}
    assert shape["finish_reason"] == "tool_calls"
    assert [
        event
        for event in _events(frames)
        if event.get("type") == "content_block_start"
        and event["content_block"].get("type") == "tool_use"
    ]


@pytest.mark.asyncio
async def test_a_plain_text_stream_records_no_deprecated_delta() -> None:
    trace = install_wire_trace()
    await _replay(TEXT_STREAM)

    shape = trace.responses[0]
    assert "function_call" not in shape["fields"]
    assert shape["fields"]["content"] == {"deltas": 1, "chars": len("done")}
    assert shape["finish_reason"] == "stop"


def test_the_counter_reads_both_halves_and_tolerates_a_bare_frame() -> None:
    assert _legacy_function_call_chars({"name": "Read", "arguments": '{"p":1}'}) == 11
    assert _legacy_function_call_chars({"name": "", "arguments": ""}) == 0
    # A frame that carries the field at all is one sighting, whatever shape
    # its halves are in -- the field's presence is the finding.
    assert _legacy_function_call_chars({}) == 0
    assert _legacy_function_call_chars(None) == 0
    assert _legacy_function_call_chars({"name": 7, "arguments": None}) == 0


def test_the_name_is_ordered_beside_the_field_it_replaced() -> None:
    """The payload's canonical order names it where a reader looks for it."""

    install_wire_trace()
    shape = wire_capture.start_response_shape()
    assert shape is not None
    shape.note_field("tool_calls", 1)
    shape.note_field("function_call", 3)
    shape.note_field("content", 2)

    assert list(shape.payload()["fields"]) == ["content", "function_call", "tool_calls"]
