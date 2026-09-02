"""Anthropic SSE becomes chat.completion.chunk frames a client can replay."""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.async_iterators import AsyncCloseable
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_chat_completions import (
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
)

_ADAPTER = OpenAIChatCompletionsAdapter()
_COMPLETION_ID = "chatcmpl-test"


class _CloseTrackingAsyncIterator:
    """An upstream that reports whether the adapter closed it."""

    def __init__(
        self, values: list[Any], *, iteration_error: Exception | None = None
    ) -> None:
        self._values = iter(values)
        self._iteration_error = iteration_error
        self.close_calls = 0

    def __aiter__(self) -> _CloseTrackingAsyncIterator:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._values)
        except StopIteration:
            if self._iteration_error is not None:
                error = self._iteration_error
                self._iteration_error = None
                raise error from None
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.close_calls += 1


async def _chunks(*values: str) -> AsyncIterator[str]:
    for value in values:
        yield value


def _stream(source, request: dict[str, Any]) -> AsyncIterator[str]:
    return _ADAPTER.iter_sse_from_anthropic(
        source,
        OpenAIChatCompletionRequest.model_validate(request),
        completion_id=_COMPLETION_ID,
    )


async def _collect(source, request: dict[str, Any]) -> list[str]:
    return [frame async for frame in _stream(source, request)]


def _payloads(frames: list[str]) -> list[dict[str, Any]]:
    """Decode every frame except the terminator, asserting the wire shape."""
    payloads = []
    for frame in frames[:-1]:
        assert frame.startswith("data: ") and frame.endswith("\n\n"), frame
        payloads.append(json.loads(frame[len("data: ") : -2]))
    return payloads


def _deltas(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        choice["delta"] for payload in payloads for choice in payload.get("choices", ())
    ]


def _request(**extra) -> dict[str, Any]:
    return {
        "model": "nvidia_nim/test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
        **extra,
    }


def _text_stream(text: str, *, stop_reason: str = "end_turn") -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 11, "output_tokens": 0}},
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"input_tokens": 0, "output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _thinking_and_text_stream() -> list[str]:
    return [
        format_sse_event("message_start", {"type": "message_start", "message": {}}),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "weighing it"},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "the answer"},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 1}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"input_tokens": 5, "output_tokens": 30},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _two_tool_calls_stream() -> list[str]:
    """Text, then two tool_use blocks whose arguments arrive interleaved."""
    return [
        format_sse_event("message_start", {"type": "message_start", "message": {}}),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "calling"},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "add",
                    "input": {},
                },
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"a":'},
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "sub",
                    "input": {},
                },
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "1}"},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"b":2}'},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 1}
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 2}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"input_tokens": 7, "output_tokens": 9},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


@pytest.mark.asyncio
async def test_a_text_stream_opens_with_a_role_and_closes_with_stop_and_done() -> None:
    frames = await _collect(_chunks(*_text_stream("Hello from provider")), _request())

    assert frames[-1] == "data: [DONE]\n\n"
    payloads = _payloads(frames)
    assert {payload["object"] for payload in payloads} == {"chat.completion.chunk"}
    assert {payload["id"] for payload in payloads} == {_COMPLETION_ID}
    assert {payload["model"] for payload in payloads} == {"nvidia_nim/test-model"}
    deltas = _deltas(payloads)
    assert deltas[0] == {"role": "assistant", "content": ""}
    assert "".join(delta.get("content", "") for delta in deltas) == (
        "Hello from provider"
    )
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["choices"][0]["delta"] == {}
    # No usage was asked for, so none is volunteered.
    assert all("usage" not in payload for payload in payloads)


@pytest.mark.asyncio
async def test_max_tokens_becomes_the_length_finish_reason() -> None:
    frames = await _collect(
        _chunks(*_text_stream("cut off", stop_reason="max_tokens")), _request()
    )

    assert _payloads(frames)[-1]["choices"][0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_thinking_is_streamed_as_reasoning_content_before_the_answer() -> None:
    frames = await _collect(_chunks(*_thinking_and_text_stream()), _request())

    deltas = _deltas(_payloads(frames))
    reasoning = [delta for delta in deltas if "reasoning_content" in delta]
    assert reasoning == [
        {"reasoning_content": "weighing it", "reasoning": "weighing it"}
    ]
    # Reasoning arrives before the visible answer, as it did upstream.
    assert deltas.index(reasoning[0]) < next(
        index for index, delta in enumerate(deltas) if delta.get("content")
    )


@pytest.mark.asyncio
async def test_two_tool_calls_stream_with_their_own_indices_and_incremental_args() -> (
    None
):
    frames = await _collect(_chunks(*_two_tool_calls_stream()), _request())

    payloads = _payloads(frames)
    calls = [
        call for delta in _deltas(payloads) for call in delta.get("tool_calls", ())
    ]

    # Openers name the call; the tool-call index is 0 and 1 even though the
    # Anthropic block indices were 1 and 2, because a text block came first.
    openers = [call for call in calls if "id" in call]
    assert openers == [
        {
            "index": 0,
            "id": "toolu_a",
            "type": "function",
            "function": {"name": "add", "arguments": ""},
        },
        {
            "index": 1,
            "id": "toolu_b",
            "type": "function",
            "function": {"name": "sub", "arguments": ""},
        },
    ]
    # Arguments arrive as fragments and reassemble per index.
    assembled: dict[int, str] = {}
    for call in calls:
        assembled[call["index"]] = assembled.get(call["index"], "") + call[
            "function"
        ].get("arguments", "")
    assert json.loads(assembled[0]) == {"a": 1}
    assert json.loads(assembled[1]) == {"b": 2}
    assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_include_usage_appends_one_choiceless_usage_chunk_before_done() -> None:
    frames = await _collect(
        _chunks(*_text_stream("hi")),
        _request(stream_options={"include_usage": True}),
    )

    payloads = _payloads(frames)
    assert payloads[-2]["choices"][0]["finish_reason"] == "stop"
    usage_chunk = payloads[-1]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "total_tokens": 15,
    }
    assert frames[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_cached_prompt_tokens_and_reasoning_tokens_reach_the_usage_details() -> (
    None
):
    stream = _thinking_and_text_stream()
    stream[0] = format_sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 6,
                }
            },
        },
    )
    frames = await _collect(
        _chunks(*stream), _request(stream_options={"include_usage": True})
    )

    usage = _payloads(frames)[-1]["usage"]
    # Anthropic excludes cache reads from input_tokens; OpenAI's prompt_tokens
    # includes them and reports how many were cached. The later message_delta
    # recount of 5 supersedes the 4 seeded by message_start.
    assert usage["prompt_tokens"] == 111
    assert usage["prompt_tokens_details"] == {"cached_tokens": 100}
    assert usage["completion_tokens"] == 30
    assert usage["completion_tokens_details"]["reasoning_tokens"] > 0
    assert usage["completion_tokens_details"]["reasoning_tokens"] <= 30


@pytest.mark.asyncio
async def test_an_upstream_error_frame_becomes_an_error_object_then_done() -> None:
    frames = await _collect(
        _chunks(
            format_sse_event("message_start", {"type": "message_start", "message": {}}),
            format_sse_event(
                "error",
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy"},
                },
            ),
        ),
        _request(),
    )

    assert frames[-1] == "data: [DONE]\n\n"
    assert _payloads(frames)[-1]["error"] == {
        "message": "busy",
        "type": "overloaded_error",
        "param": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_a_failure_after_the_first_frame_ends_the_stream_with_an_error() -> None:
    source = _CloseTrackingAsyncIterator(
        [format_sse_event("message_start", {"type": "message_start", "message": {}})],
        iteration_error=ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="upstream is busy",
            retryable=True,
        ),
    )

    frames = await _collect(source, _request())

    assert frames[-1] == "data: [DONE]\n\n"
    assert _payloads(frames)[-1]["error"]["type"] == "rate_limit_error"
    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_a_failure_before_any_frame_is_re_raised_for_the_http_boundary() -> None:
    source = _CloseTrackingAsyncIterator(
        [],
        iteration_error=ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="upstream is busy",
            retryable=True,
        ),
    )

    with pytest.raises(ExecutionFailure):
        await _collect(source, _request())
    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_the_source_is_closed_when_the_consumer_stops_early() -> None:
    source = _CloseTrackingAsyncIterator(_text_stream("hi"))

    stream = _stream(source, _request())
    await anext(stream)
    assert isinstance(stream, AsyncCloseable)
    await stream.aclose()

    assert source.close_calls == 1


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_message_stop_is_still_finished() -> None:
    frames = await _collect(
        _chunks(*_text_stream("hi")[:-1]),
        _request(stream_options={"include_usage": True}),
    )

    payloads = _payloads(frames)
    assert payloads[-2]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"]["completion_tokens"] == 4
    assert frames[-1] == "data: [DONE]\n\n"


def test_the_non_streaming_form_assembles_the_same_answer() -> None:
    completion = _ADAPTER.completion_from_anthropic_message(
        {
            "content": [
                {"type": "thinking", "thinking": "weighing it"},
                {"type": "text", "text": "the answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 30},
        },
        OpenAIChatCompletionRequest.model_validate(_request(stream=False)),
        completion_id=_COMPLETION_ID,
    )

    assert completion["object"] == "chat.completion"
    assert completion["id"] == _COMPLETION_ID
    assert completion["model"] == "nvidia_nim/test-model"
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "the answer"
    assert choice["message"]["reasoning_content"] == "weighing it"
    assert completion["usage"]["prompt_tokens"] == 5
    assert completion["usage"]["completion_tokens"] == 30


def test_the_non_streaming_form_reports_tool_calls_and_a_null_content() -> None:
    completion = _ADAPTER.completion_from_anthropic_message(
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "add",
                    "input": {"a": 1},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
        OpenAIChatCompletionRequest.model_validate(_request(stream=False)),
        completion_id=_COMPLETION_ID,
    )

    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": "toolu_a",
            "type": "function",
            "function": {"name": "add", "arguments": '{"a":1}'},
        }
    ]
