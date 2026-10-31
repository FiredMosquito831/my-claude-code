"""The generation window, taken at each door, and what cannot move it.

The rate MCC prints divides a host's token count -- reasoning and tool
arguments included -- by the window those tokens were produced in. Before
7.122.2 the divisor was ``duration_ms - ttft_ms``, and ``ttft_ms`` is the first
byte MCC's client-facing iterator yielded: after the commit holdback, after
tool-argument assembly, after a recovery flush. On a real log that put the
first byte in the last 10% of the request on 75.9% of a week's rows and printed
429,152 tok/s for a 68 s answer.

Every door therefore takes its stamps from its own protocol's deltas, before
any MCC buffer. These tests drive a fake upstream whose deltas really are
spread over a known wall-clock span and assert the recorded window is that
span -- with a 0.75 s holdback in the path, with reasoning the client never
sees, and across a retry that discards a first stream.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.core import wire_capture
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.credentials import (
    load_chatgpt_oauth_credentials,
)
from my_claude_code.providers.openai_chat.responses_transport import (
    ResponsesTransport,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, profiled_provider

pytestmark = pytest.mark.asyncio

#: Four upstream deltas, this far apart. Three gaps make the window, which has
#: to clear the 500 ms minimum with room for a loaded machine.
STEP_SECONDS = 0.3
DELTAS = 4
WINDOW_LOW_MS = int((DELTAS - 1) * STEP_SECONDS * 1000)
#: asyncio never wakes early, so the recorded span is the nominal one plus
#: scheduling slack. Generous: this asserts "the upstream span", not the clock.
WINDOW_HIGH_MS = WINDOW_LOW_MS + 900

#: The shipped default, named here so the test proves the real holdback is in
#: the path rather than a test-only zero.
HOLDBACK_SECONDS = 0.75


@pytest.fixture(autouse=True)
def untracked(monkeypatch) -> None:
    """Start every test from "no request is being captured"."""
    monkeypatch.setattr(
        wire_capture, "_WIRE_TRACE", ContextVar("fcc_wire_trace_test", default=None)
    )


class _TimedStream(httpx.AsyncByteStream):
    """An upstream body delivered in timed pieces, optionally dying mid-stream."""

    def __init__(
        self,
        pieces: list[tuple[float, bytes]],
        *,
        fail_after: Exception | None = None,
    ) -> None:
        self._pieces = pieces
        self._fail_after = fail_after

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, chunk in self._pieces:
            if delay:
                await asyncio.sleep(delay)
            yield chunk
        if self._fail_after is not None:
            raise self._fail_after

    async def aclose(self) -> None:
        return None


def _window_ms(shape: dict[str, Any] | None) -> int:
    assert shape is not None, "the reply recorded no shape at all"
    first = shape.get("gen_first_ms")
    last = shape.get("gen_last_ms")
    assert isinstance(first, int) and isinstance(last, int), shape
    return last - first


def _assert_upstream_span(window_ms: int) -> None:
    assert WINDOW_LOW_MS <= window_ms <= WINDOW_HIGH_MS, window_ms


# ---------------------------------------------------------------- Messages --


def _anthropic(event: str, **payload: Any) -> bytes:
    body = json.dumps({"type": event, **payload})
    return f"event: {event}\ndata: {body}\n\n".encode()


def _anthropic_pieces(*, kind: str = "text") -> list[tuple[float, bytes]]:
    pieces: list[tuple[float, bytes]] = [
        (
            0.0,
            _anthropic(
                "message_start", message={"id": "msg_1", "usage": {"input_tokens": 9}}
            ),
        ),
        (
            0.0,
            _anthropic(
                "content_block_start", index=0, content_block={"type": kind, "text": ""}
            ),
        ),
    ]
    for step in range(DELTAS):
        if kind == "thinking":
            delta = {"type": "thinking_delta", "thinking": f"thought {step}"}
        elif kind == "tool_use":
            delta = {"type": "input_json_delta", "partial_json": f'"{step}"'}
        else:
            delta = {"type": "text_delta", "text": f"word {step}"}
        pieces.append(
            (
                STEP_SECONDS,
                _anthropic("content_block_delta", index=0, delta=delta),
            )
        )
    pieces.append((0.0, _anthropic("content_block_stop", index=0)))
    pieces.append(
        (
            0.0,
            _anthropic(
                "message_delta",
                delta={"stop_reason": "end_turn"},
                usage={"output_tokens": 40},
            ),
        )
    )
    pieces.append((0.0, _anthropic("message_stop")))
    return pieces


def _messages_provider(pieces: list[tuple[float, bytes]], **config: Any):
    provider = AnthropicMessagesProvider(
        ProviderConfig(
            api_key="sk-ant-test",
            base_url="https://upstream.invalid/v1",
            commit_holdback_seconds=HOLDBACK_SECONDS,
            **config,
        ),
        provider_name="UPSTREAM",
        rate_limiter=passthrough_rate_limiter(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TimedStream(pieces),
            request=request,
        )

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


async def _drain_with_times(stream: AsyncIterator[str]) -> list[tuple[str, float]]:
    """Every frame the client was handed, with when it was handed over."""
    import time

    return [(frame, time.monotonic()) async for frame in stream]


def _times(seen: list[tuple[str, float]]) -> list[float]:
    return [at for _frame, at in seen]


def _client_span_ms(seen: list[tuple[str, float]]) -> float:
    times = _times(seen)
    return (times[-1] - times[0]) * 1000


async def test_the_messages_door_measures_the_upstream_span_not_the_flush() -> None:
    """The holdback releases everything at the end; the window does not move.

    With the shipped 0.75 s holdback in the path, a stream this short is
    buffered whole and released at ``message_stop``. The client therefore sees
    a span of a few milliseconds -- which is exactly what the retired
    ``duration - ttft`` arithmetic measured -- while the model generated for
    three steps.
    """

    trace = install_wire_trace()
    provider = _messages_provider(_anthropic_pieces())
    try:
        visible = await _drain_with_times(
            provider.stream_response(make_messages_request())
        )
    finally:
        await provider.cleanup()

    window = _window_ms(trace.responses[0])
    _assert_upstream_span(window)
    seen_span_ms = _client_span_ms(visible)
    assert seen_span_ms < window / 2, (
        "the client-visible frames must be the held-back burst, or this test "
        f"proves nothing: {seen_span_ms} ms against a {window} ms window"
    )


async def test_reasoning_the_client_never_sees_still_counts() -> None:
    """A holding policy hides the thinking; the tokens are still in the count."""

    trace = install_wire_trace()
    provider = _messages_provider(
        _anthropic_pieces(kind="thinking"), fallback_on_reasoning_only=True
    )
    try:
        await _drain_with_times(provider.stream_response(make_messages_request()))
    finally:
        await provider.cleanup()

    _assert_upstream_span(_window_ms(trace.responses[0]))


async def test_tool_arguments_count_as_generation() -> None:
    """A tool-only turn is output, and its deltas are the only clock it has."""

    trace = install_wire_trace()
    provider = _messages_provider(_anthropic_pieces(kind="tool_use"))
    try:
        await _drain_with_times(provider.stream_response(make_messages_request()))
    finally:
        await provider.cleanup()

    _assert_upstream_span(_window_ms(trace.responses[0]))


async def test_a_reply_with_no_deltas_records_no_window() -> None:
    """Nothing streamed is "not measured", never a zero-length window."""

    pieces = [
        (
            0.0,
            _anthropic(
                "message_start", message={"id": "msg_1", "usage": {"input_tokens": 9}}
            ),
        ),
        (
            0.0,
            _anthropic(
                "message_delta",
                delta={"stop_reason": "end_turn"},
                usage={"output_tokens": 0},
            ),
        ),
        (0.0, _anthropic("message_stop")),
    ]
    trace = install_wire_trace()
    provider = _messages_provider(pieces)
    try:
        await _drain_with_times(provider.stream_response(make_messages_request()))
    finally:
        await provider.cleanup()

    shape = trace.responses[0]
    assert "gen_first_ms" not in shape
    assert "gen_last_ms" not in shape


async def test_a_retry_that_discards_a_stream_does_not_stretch_the_window() -> None:
    """The count belongs to the stream that finished, so the window must too.

    The first stream talks, then stalls for a second and a half and dies. If
    the clock were not reset on the retry, the window would span that whole
    stretch -- about 2.5 s here -- instead of the second stream's 0.9 s, and
    the reported rate would be a fraction of what the model that finished
    actually produced.
    """
    stall = 1.5
    trace = install_wire_trace()
    first = (
        [
            (0.0, _anthropic("message_start", message={"id": "msg_1"})),
            (
                0.0,
                _anthropic(
                    "content_block_start",
                    index=0,
                    content_block={"type": "text", "text": ""},
                ),
            ),
        ]
        + [
            (
                0.2,
                _anthropic(
                    "content_block_delta",
                    index=0,
                    delta={"type": "text_delta", "text": "lost"},
                ),
            )
        ]
        * 3
        + [(stall, b"")]
    )
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_TimedStream(first, fail_after=httpx.ReadError("cut")),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TimedStream(_anthropic_pieces()),
            request=request,
        )

    provider = AnthropicMessagesProvider(
        ProviderConfig(
            api_key="sk-ant-test",
            base_url="https://upstream.invalid/v1",
            commit_holdback_seconds=HOLDBACK_SECONDS,
        ),
        provider_name="UPSTREAM",
        rate_limiter=passthrough_limiter(),
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await _drain_with_times(provider.stream_response(make_messages_request()))
    finally:
        await provider.cleanup()

    assert len(calls) == 2, "the early retry did not happen; the test proves nothing"
    # The discarded stream's ~0.4 s must not be in the window the retry reports.
    _assert_upstream_span(_window_ms(trace.responses[0]))


def passthrough_limiter():
    return passthrough_rate_limiter()


# -------------------------------------------------------------------- Chat --


def _chat_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
    frame = {
        "id": "chatcmpl-window",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(frame)}\n\n".encode()


def _chat_usage() -> bytes:
    """The usage hop the door needs to *report* a count rather than estimate one."""
    frame = {
        "id": "chatcmpl-window",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "test-model",
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 40, "total_tokens": 43},
    }
    return f"data: {json.dumps(frame)}\n\n".encode()


def _chat_pieces(*, field: str = "content") -> list[tuple[float, bytes]]:
    pieces: list[tuple[float, bytes]] = [(0.0, _chat_chunk({"role": "assistant"}))]
    pieces.extend(
        (STEP_SECONDS, _chat_chunk({field: f"word {step}"})) for step in range(DELTAS)
    )
    pieces.append((0.0, _chat_chunk({}, finish_reason="stop")))
    pieces.append((0.0, _chat_usage()))
    pieces.append((0.0, b"data: [DONE]\n\n"))
    return pieces


def _chat_provider(**config: Any):
    provider = profiled_provider(
        "groq",
        ProviderConfig(
            api_key="test-key",
            base_url="http://sse.invalid/v1",
            rate_limit=1_000_000,
            rate_window=60,
            commit_holdback_seconds=HOLDBACK_SECONDS,
            **config,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    return provider


def _install_chat_client(provider, pieces: list[tuple[float, bytes]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TimedStream(pieces),
            request=request,
        )

    provider._client = AsyncOpenAI(
        api_key="test-key",
        base_url=provider._config.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_the_chat_door_measures_the_raw_deltas() -> None:
    trace = install_wire_trace()
    provider = _chat_provider()
    _install_chat_client(provider, _chat_pieces())
    try:
        visible = await _drain_with_times(
            provider.stream_response(make_messages_request())
        )
    finally:
        await provider.cleanup()

    window = _window_ms(trace.responses[0])
    _assert_upstream_span(window)
    seen_span_ms = _client_span_ms(visible)
    assert seen_span_ms < window / 2


async def test_withheld_reasoning_deltas_are_in_the_window() -> None:
    """The client's policy hides reasoning; the tokens are in the numerator.

    The stamp is taken on the raw chunk, so a reasoning delta that never
    becomes an SSE frame the client sees still starts and extends the window.
    Without that, a model that thinks before answering would be credited with
    an impossible rate.
    """

    trace = install_wire_trace()
    provider = _chat_provider()
    _install_chat_client(provider, _chat_pieces(field="reasoning_content"))
    try:
        visible = await _drain_with_times(
            provider.stream_response(
                make_messages_request(), reasoning=ReasoningPolicy.off()
            )
        )
    finally:
        await provider.cleanup()

    _assert_upstream_span(_window_ms(trace.responses[0]))
    assert not any("thinking_delta" in frame for frame, _at in visible), (
        "the policy asked for no reasoning, so none may reach the client"
    )


async def test_an_estimated_token_count_records_no_window() -> None:
    """An estimate cannot back a rate, so the window is not even stored."""

    pieces = [(0.0, _chat_chunk({"role": "assistant"}))]
    pieces.extend(
        (STEP_SECONDS, _chat_chunk({"content": f"word {step}"}))
        for step in range(DELTAS)
    )
    # No usage block at all: the door falls back to its own cl100k estimate.
    pieces.append((0.0, _chat_chunk({}, finish_reason="stop")))
    pieces.append((0.0, b"data: [DONE]\n\n"))

    trace = install_wire_trace()
    provider = _chat_provider()
    _install_chat_client(provider, pieces)
    try:
        await _drain_with_times(provider.stream_response(make_messages_request()))
    finally:
        await provider.cleanup()

    shape = trace.responses[0]
    assert shape.get("estimated_tokens") is True
    assert "gen_first_ms" not in shape
    assert "gen_last_ms" not in shape


# ----------------------------------------------------------------- Responses --


def _responses_event(kind: str, **payload: Any) -> bytes:
    body = json.dumps({"type": kind, **payload})
    return f"event: {kind}\ndata: {body}\n\n".encode()


def _responses_pieces(*, field: str = "output_text") -> list[tuple[float, bytes]]:
    pieces: list[tuple[float, bytes]] = [
        (0.0, _responses_event("response.created", response={}))
    ]
    if field == "output_text":
        kind = "response.output_text.delta"
        key = "delta"
    else:
        kind = "response.reasoning_text.delta"
        key = "delta"
    pieces.extend(
        (STEP_SECONDS, _responses_event(kind, **{key: f"word {step}"}))
        for step in range(DELTAS)
    )
    pieces.append(
        (
            0.0,
            _responses_event(
                "response.completed",
                response={"usage": {"input_tokens": 3, "output_tokens": 40}},
            ),
        )
    )
    return pieces


async def _raw_chunks(pieces: list[tuple[float, bytes]]) -> AsyncIterator[bytes]:
    for delay, chunk in pieces:
        if delay:
            await asyncio.sleep(delay)
        yield chunk


@pytest.fixture
def chatgpt_provider() -> ChatGPTOAuthProvider:
    return ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url="https://chatgpt.invalid/backend-api",
            rate_limit=1000,
            rate_window=60,
            commit_holdback_seconds=HOLDBACK_SECONDS,
        ),
        rate_limiter=passthrough_rate_limiter(),
        account_id="test_account_id",
    )


def _install_oauth_response(provider, pieces: list[tuple[float, bytes]]) -> None:
    response = MagicMock()
    response.status_code = 200
    response.aiter_raw = lambda: _raw_chunks(pieces)
    response.aclose = AsyncMock()
    client = provider._client
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(return_value=response)


async def test_the_chatgpt_door_measures_the_raw_response_events(
    chatgpt_provider,
) -> None:
    trace = install_wire_trace()
    _install_oauth_response(chatgpt_provider, _responses_pieces())
    request = MessagesRequest(
        model="gpt-5", messages=[Message(role="user", content="hi")], max_tokens=64
    )
    await _drain_with_times(
        chatgpt_provider.stream_response(request, request_id="req_window")
    )
    assert chatgpt_provider._client.send.await_count == 1

    _assert_upstream_span(_window_ms(trace.responses[0]))


async def test_the_chatgpt_door_counts_reasoning_it_does_not_forward(
    chatgpt_provider,
) -> None:
    trace = install_wire_trace()
    _install_oauth_response(chatgpt_provider, _responses_pieces(field="reasoning"))
    request = MessagesRequest(
        model="gpt-5", messages=[Message(role="user", content="hi")], max_tokens=64
    )
    visible = await _drain_with_times(
        chatgpt_provider.stream_response(
            request, reasoning=ReasoningPolicy.off(), request_id="req_window"
        )
    )

    _assert_upstream_span(_window_ms(trace.responses[0]))
    assert not any("thinking_delta" in frame for frame, _at in visible)


def _chat_transport() -> ResponsesTransport:
    return ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        rate_limiter=passthrough_rate_limiter(),
    )


async def test_the_chat_transport_responses_door_measures_the_raw_events() -> None:
    trace = install_wire_trace()
    transport = _chat_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_TimedStream(_responses_pieces()),
            request=request,
        )

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = make_messages_request()
    body, headers = transport.build_body(
        request, reasoning=ReasoningPolicy.provider_default(), max_output_tokens=64
    )
    frames = [
        event
        async for event in transport.stream(
            request,
            input_tokens=0,
            reasoning=ReasoningPolicy.provider_default(),
            body=body,
            headers=headers,
            surface_label="responses",
        )
    ]
    assert frames

    _assert_upstream_span(_window_ms(trace.responses[0]))
    await transport.aclose()


__all__ = ["load_chatgpt_oauth_credentials"]
