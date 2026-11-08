"""The response-model boundary: the client sees the model it asked for.

Claude Code marks an assistant turn *foreign* when ``message_start.message.model``
differs from the model it requested, and strips every ``thinking`` /
``redacted_thinking`` block from such a turn. MCC routes a request to a provider
model, so on every non-Anthropic door the reply names the routed model and every
turn reads as foreign.

The fix is one seam -- ``api/response_streams.py``, the funnel every Anthropic
SSE frame passes through -- and it rewrites one field of one frame. These tests
prove it on all three upstream door families, prove every other byte of every
stream is unchanged, and prove the two surfaces that must keep the routed name
(the "Say OK" probe and the request log) still do.
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

import pytest
from fastapi.responses import StreamingResponse

from my_claude_code.api.response_streams import (
    anthropic_sse_streaming_response,
    requested_model_stream,
    rewrite_message_start_model,
)
from my_claude_code.core.anthropic.streaming import (
    AnthropicStreamLedger,
    format_sse_event,
)
from my_claude_code.providers.anthropic_messages.streaming import (
    iter_anthropic_sse_frames,
)

#: What the client asked for, on every fixture below.
REQUESTED = "claude-opus-5-5"

#: What routing resolved it to. Deliberately a name the client never sent.
ROUTED = "gpt-6.1-sol"


def _json_error(exc: BaseException) -> Any:
    raise AssertionError(f"unexpected pre-start failure: {exc!r}")


async def _drain(response: StreamingResponse) -> str:
    return "".join(
        [
            chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            async for chunk in response.body_iterator
        ]
    )


async def _boundary(body: AsyncIterator[str], model: str | None) -> str:
    response = await anthropic_sse_streaming_response(
        body,
        pre_start_error_response=_json_error,
        request_id="req_model_boundary",
        response_model=model,
    )
    assert isinstance(response, StreamingResponse)
    return await _drain(response)


# --------------------------------------------------------------------------
# The three door families, each producing the frame its own transport writes.
# --------------------------------------------------------------------------


def _ledger_stream(model: str) -> list[str]:
    """What MCC's own ledger writes -- the Responses and Chat Completions doors.

    Both providers construct ``AnthropicStreamLedger(..., request.model, ...)``
    with the *routed* request (``providers/openai_chat/responses_transport.py``,
    ``providers/openai_chat/provider.py``, ``providers/chatgpt_oauth/provider.py``),
    so this is the exact bytes a client receives on those two doors today.
    """

    ledger = AnthropicStreamLedger("msg_ledger", model, 11)
    return [
        ledger.message_start(),
        ledger.content_block_start(0, "text"),
        ledger.content_block_delta(0, "text_delta", "OK"),
        ledger.content_block_stop(0),
        ledger.message_delta("end_turn", 2),
        ledger.message_stop(),
    ]


async def _anthropic_messages_relay(model: str) -> AsyncIterator[str]:
    """What the Anthropic Messages door relays -- the host's own frames.

    ``iter_anthropic_sse_frames`` is the real relay: it validates each upstream
    frame and re-serialises it compactly. Feeding it bytes is the closest a
    fake upstream can get to CommandCode / Agnes / Nous / OpenCode Go without a
    live call.
    """

    frames = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_relay",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 11, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "OK"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 11, "output_tokens": 2},
        },
        {"type": "message_stop"},
    ]
    raw = "".join(
        f"event: {frame['type']}\ndata: {json.dumps(frame, separators=(',', ':'))}\n\n"
        for frame in frames
    ).encode("utf-8")

    async def chunks() -> AsyncGenerator[bytes]:
        yield raw

    async for frame in iter_anthropic_sse_frames(chunks()):
        yield frame


async def _responses_door(model: str) -> AsyncIterator[str]:
    for frame in _ledger_stream(model):
        yield frame


async def _chat_completions_door(model: str) -> AsyncIterator[str]:
    for frame in _ledger_stream(model):
        yield frame


DOORS: dict[str, Any] = {
    "anthropic_messages_relay": _anthropic_messages_relay,
    "responses": _responses_door,
    "chat_completions": _chat_completions_door,
}


def _model_of(frame: str) -> Any:
    for line in frame.split("\n"):
        if line.startswith("data:"):
            return json.loads(line[5:].lstrip(" ")).get("message", {}).get("model")
    return None


# --------------------------------------------------------------------------
# 1. The rewrite happens, on every door family.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("door", sorted(DOORS))
async def test_every_door_reports_the_requested_model(door: str) -> None:
    stream = await _boundary(DOORS[door](ROUTED), REQUESTED)

    frames = [frame for frame in stream.split("\n\n") if frame]
    start = frames[0]
    assert start.startswith("event: message_start\n")
    assert _model_of(start) == REQUESTED


@pytest.mark.asyncio
@pytest.mark.parametrize("door", sorted(DOORS))
async def test_the_routed_model_is_gone_from_the_client_stream(door: str) -> None:
    """Nothing else in the stream may still name the model routing chose."""

    stream = await _boundary(DOORS[door](ROUTED), REQUESTED)

    assert ROUTED not in stream
    assert REQUESTED in stream


# --------------------------------------------------------------------------
# 2. Nothing else moves: every other byte of every stream is identical.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("door", sorted(DOORS))
async def test_only_the_model_value_differs_in_the_whole_stream(door: str) -> None:
    """Base behaviour (no ``response_model``) vs branch, line by line.

    ``response_model=None`` is byte-for-byte what this function did before the
    feature existed, so this compares the branch against the base path directly.
    """

    before = await _boundary(DOORS[door](ROUTED), None)
    after = await _boundary(DOORS[door](ROUTED), REQUESTED)

    before_lines = before.split("\n")
    after_lines = after.split("\n")
    assert len(before_lines) == len(after_lines)

    differing = [
        (index, was, now)
        for index, (was, now) in enumerate(zip(before_lines, after_lines, strict=True))
        if was != now
    ]
    assert len(differing) == 1, differing
    index, was, now = differing[0]
    assert was == before_lines[index]
    # The one line that moved is the ``data:`` line of ``message_start``, and
    # the only thing that changed inside it is the model's value.
    assert was.startswith("data: ")
    assert now == was.replace(json.dumps(ROUTED), json.dumps(REQUESTED), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("door", sorted(DOORS))
async def test_a_stream_that_already_names_the_requested_model_is_untouched(
    door: str,
) -> None:
    """The Anthropic door's honest case: the names coincide, so nothing moves."""

    before = await _boundary(DOORS[door](REQUESTED), None)
    after = await _boundary(DOORS[door](REQUESTED), REQUESTED)

    assert before == after


@pytest.mark.asyncio
async def test_no_response_model_leaves_the_stream_byte_identical() -> None:
    """A surface that resolved no alias gets exactly what it got before."""

    for door in DOORS:
        assert await _boundary(DOORS[door](ROUTED), None) == await _boundary(
            DOORS[door](ROUTED), ""
        )


# --------------------------------------------------------------------------
# 3. The transform itself: what it refuses to touch.
# --------------------------------------------------------------------------


def _start_frame(model: Any, *, compact: bool = False) -> str:
    payload = {
        "type": "message_start",
        "message": {
            "id": "msg_unit",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 1},
        },
    }
    separators = (",", ":") if compact else None
    body = (
        json.dumps(payload)
        if separators is None
        else json.dumps(payload, separators=separators)
    )
    return f"event: message_start\ndata: {body}\n\n"


@pytest.mark.parametrize("compact", [False, True])
def test_the_transform_rewrites_both_serialisations_mcc_writes(compact: bool) -> None:
    frame = _start_frame(ROUTED, compact=compact)

    rewritten = rewrite_message_start_model(frame, REQUESTED)

    assert rewritten is not None
    assert _model_of(rewritten) == REQUESTED
    assert rewritten == frame.replace(json.dumps(ROUTED), json.dumps(REQUESTED), 1)


def test_a_non_message_start_frame_is_not_a_match() -> None:
    frame = format_sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "message_start"},
        },
    )

    assert rewrite_message_start_model(frame, REQUESTED) is None


def test_a_frame_whose_model_already_matches_is_returned_by_identity() -> None:
    frame = _start_frame(REQUESTED)

    assert rewrite_message_start_model(frame, REQUESTED) == frame


def test_a_message_start_without_a_string_model_is_returned_unchanged() -> None:
    frame = _start_frame(None)

    assert rewrite_message_start_model(frame, REQUESTED) == frame


def test_an_ambiguous_model_value_is_left_alone() -> None:
    """Two ``"model"`` keys carrying the same value: refuse rather than guess."""

    frame = (
        "event: message_start\ndata: "
        + json.dumps(
            {
                "type": "message_start",
                "model": ROUTED,
                "message": {"model": ROUTED},
            }
        )
        + "\n\n"
    )

    assert rewrite_message_start_model(frame, REQUESTED) == frame


def test_a_frame_with_more_than_one_data_line_is_not_handled() -> None:
    """A frame carrying more than one ``data:`` line is not the shape MCC writes.

    It is reported as "not a ``message_start``" rather than rewritten, so the
    caller leaves it exactly as it arrived and keeps looking.
    """

    payload = json.dumps({"type": "message_start", "message": {"model": ROUTED}})
    frame = f"event: message_start\ndata: {payload}\ndata: {payload}\n\n"

    assert rewrite_message_start_model(frame, REQUESTED) is None


def test_a_model_name_that_prefixes_another_is_not_mistaken_for_it() -> None:
    """The closing quote is what makes the match exact.

    A payload naming both ``gpt-6.1-sol`` and ``gpt-6.1-sol-extra`` must have
    only the exact value replaced; a substring search without the closing quote
    would rewrite both.
    """

    frame = (
        "event: message_start\ndata: "
        + json.dumps(
            {
                "type": "message_start",
                "message": {"model": ROUTED},
                "other": {"model": f"{ROUTED}-extra"},
            }
        )
        + "\n\n"
    )

    rewritten = rewrite_message_start_model(frame, REQUESTED)

    assert rewritten is not None
    payload = json.loads(rewritten.split("data: ", 1)[1])
    assert payload["message"]["model"] == REQUESTED
    assert payload["other"]["model"] == f"{ROUTED}-extra"


def test_an_unparseable_frame_is_left_alone() -> None:
    frame = 'event: message_start\ndata: {"type":"message_start",\n\n'

    assert rewrite_message_start_model(frame, REQUESTED) is None


# --------------------------------------------------------------------------
# 4. The wrapper's lifecycle: it disarms, and it closes what it wrapped.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_first_message_start_is_rewritten() -> None:
    """A second ``message_start`` on the same stream is not the client's turn."""

    first = _start_frame(ROUTED)
    second = _start_frame(ROUTED)

    async def body() -> AsyncGenerator[str]:
        yield first
        yield second

    stream = await _boundary(body(), REQUESTED)

    assert stream.count(json.dumps(REQUESTED)) == 1
    assert stream.count(json.dumps(ROUTED)) == 1


@pytest.mark.asyncio
async def test_the_rewrite_survives_a_keepalive_that_commits_first() -> None:
    """The keepalive path must not lose the rewrite.

    When the body is silent past the idle window the response commits with a
    keepalive as its first frame, and ``message_start`` arrives afterwards
    through the tail. The transform sits upstream of that prefetch, so the
    frame is still rewritten -- this is the edge case that would break if the
    rewrite had been bolted onto the first prefetched chunk instead.

    ``idle_seconds=0`` makes the race deterministic rather than timing-shaped:
    the reader's first wait has a zero timeout, so it always returns before a
    body that has to await anything at all, and the keepalive is the frame the
    response commits with. A wall-clock window here failed once under a loaded
    full-suite run, which is exactly the kind of test that teaches nothing.
    """

    import asyncio

    from my_claude_code.api.response_streams import StreamKeepalive

    async def body() -> AsyncGenerator[str]:
        await asyncio.sleep(0.05)
        for frame in _ledger_stream(ROUTED):
            yield frame

    response = await anthropic_sse_streaming_response(
        body(),
        pre_start_error_response=_json_error,
        request_id="req_keepalive_boundary",
        keepalive=StreamKeepalive(
            idle_seconds=0.0, interval_seconds=0.01, max_seconds=0
        ),
        response_model=REQUESTED,
    )
    assert isinstance(response, StreamingResponse)
    stream = await _drain(response)

    assert stream.startswith("event: ping\n")
    starts = [
        frame
        for frame in stream.split("\n\n")
        if frame.startswith("event: message_start\n")
    ]
    assert len(starts) == 1
    assert _model_of(starts[0]) == REQUESTED
    assert ROUTED not in stream


@pytest.mark.asyncio
async def test_closing_the_wrapper_closes_the_body_it_wrapped() -> None:
    closed: list[str] = []

    class Body:
        def __aiter__(self) -> Body:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            closed.append("body")

    stream = requested_model_stream(cast(AsyncIterator[str], Body()), REQUESTED)
    assert isinstance(stream, AsyncIterator)
    await cast(Any, stream).aclose()

    assert closed == ["body"]


def test_a_missing_model_returns_the_body_itself() -> None:
    body = cast(AsyncIterator[str], object())

    assert requested_model_stream(body, None) is body
    assert requested_model_stream(body, "") is body


# --------------------------------------------------------------------------
# 5. The two surfaces that must keep the routed name.
# --------------------------------------------------------------------------


class _LedgerProvider:
    """Stands in for a provider: it answers with the routed model, as they do."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[Any] = []

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(self, request: Any, *, reasoning: Any) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"test-model"})

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: Any,
    ) -> AsyncIterator[str]:
        self.requests.append(request)
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_the_say_ok_probe_still_names_the_resolved_model() -> None:
    """The probe exists to detect substitution, so it must NOT be blinded.

    It builds its own ``MessagesResponse`` and never goes through the boundary,
    which is exactly why the boundary rewrite cannot reach it. If this test ever
    starts failing, the probe has been rewired onto the client-facing path and
    a silently substituted model would read as a clean bill of health.
    """

    from my_claude_code.api.handlers import MessagesHandler
    from my_claude_code.config.settings import Settings
    from my_claude_code.core.anthropic.models import (
        Message,
        MessagesRequest,
        MessagesResponse,
    )

    handler = MessagesHandler(Settings(), provider_resolver=cast(Any, object()))
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=16,
        messages=[Message(role="user", content="Say OK")],
    )

    response = await handler.create(request)

    assert isinstance(response, MessagesResponse)
    assert response.model == "test-model"


@pytest.mark.asyncio
async def test_the_request_log_still_records_the_routed_model() -> None:
    """``requested_model`` and ``resolved_model`` keep their meanings.

    The client now sees the alias it sent; the row must still say which model
    really answered, or the routing decision becomes undiagnosable.
    """

    from my_claude_code.api.handlers import MessagesHandler
    from my_claude_code.api.response_streams import ManagedStreamingResponse
    from my_claude_code.config.settings import Settings
    from my_claude_code.core.anthropic.models import Message, MessagesRequest
    from my_claude_code.core.request_log import get_request_log_store

    ledger = AnthropicStreamLedger("msg_log", "test-model", 11)
    events = [
        ledger.message_start(),
        ledger.content_block_start(0, "text"),
        ledger.content_block_delta(0, "text_delta", "OK"),
        ledger.content_block_stop(0),
        ledger.message_delta("end_turn", 2),
        ledger.message_stop(),
    ]
    handler = MessagesHandler(
        Settings(), provider_resolver=lambda _: _LedgerProvider(events)
    )
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=50,
        stream=True,
        messages=[Message(role="user", content="hello")],
    )

    response = await handler.create(request, request_id="req_boundary_log")
    assert isinstance(response, ManagedStreamingResponse)
    delivered = await _drain(response)
    await response.aclose()

    # The client saw the alias it asked for...
    assert _model_of(delivered.split("\n\n")[0]) == "nvidia_nim/test-model"

    # ...and the row still names the model that really answered.
    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request("req_boundary_log")
    assert row is not None
    assert row["requested_model"] == "nvidia_nim/test-model"
    assert row["resolved_model"] == "test-model"
