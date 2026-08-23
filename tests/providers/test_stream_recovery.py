"""Provider-owned stream retry and holdback policy."""

import time

import httpx
import openai

from my_claude_code.core.anthropic.stream_contracts import (
    REASONING_HEARTBEAT,
    sse_is_scaffolding,
)
from my_claude_code.providers.stream_recovery import (
    EARLY_TRANSPARENT_MAX_RETRIES,
    EARLY_TRANSPARENT_TOTAL_ATTEMPTS,
    MIDSTREAM_RECOVERY_ATTEMPTS,
    RecoveryController,
    RecoveryFailureAction,
    RecoveryHoldbackBuffer,
    is_retryable_stream_error,
)


def _statusless_openai_api_error(
    message: str, body: object | None = None
) -> openai.APIError:
    return openai.APIError(
        message,
        request=httpx.Request("POST", "https://provider.test/messages"),
        body=body,
    )


def test_early_transparent_retry_total_attempts_is_five() -> None:
    assert EARLY_TRANSPARENT_TOTAL_ATTEMPTS == 5
    assert EARLY_TRANSPARENT_MAX_RETRIES == 4


def test_midstream_recovery_attempts_total_is_five() -> None:
    assert MIDSTREAM_RECOVERY_ATTEMPTS == 5


def test_retryable_stream_error_classifies_transport_and_http_status() -> None:
    assert is_retryable_stream_error(httpx.ReadError("cut off"))

    request = httpx.Request("GET", "https://example.test")
    assert is_retryable_stream_error(
        httpx.HTTPStatusError(
            "server error", request=request, response=httpx.Response(503)
        )
    )
    assert not is_retryable_stream_error(
        httpx.HTTPStatusError(
            "bad request", request=request, response=httpx.Response(400)
        )
    )


def test_stream_retry_preserves_timeout_scope() -> None:
    request = httpx.Request("POST", "https://provider.test/messages")

    assert is_retryable_stream_error(httpx.ReadTimeout("read", request=request))
    assert not is_retryable_stream_error(
        httpx.ConnectTimeout("connect", request=request)
    )
    assert not is_retryable_stream_error(httpx.WriteTimeout("write", request=request))
    assert not is_retryable_stream_error(httpx.PoolTimeout("pool", request=request))


def test_retryable_stream_error_classifies_statusless_api_error_body_status() -> None:
    assert is_retryable_stream_error(
        _statusless_openai_api_error(
            "stream embedded error",
            {"error": {"message": "internal failure", "code": 500}},
        )
    )


def test_retryable_stream_error_classifies_statusless_internal_error_type() -> None:
    assert is_retryable_stream_error(
        _statusless_openai_api_error(
            "stream embedded error",
            {"error": {"message": "internal failure", "type": "internal_server_error"}},
        )
    )


def test_retryable_stream_error_classifies_resource_exhausted_text() -> None:
    assert is_retryable_stream_error(
        _statusless_openai_api_error(
            "ResourceExhausted: limit reached while generating response",
            {"error": {"message": "ResourceExhausted: limit reached"}},
        )
    )


def test_retryable_stream_error_does_not_retry_bad_request_status() -> None:
    request = httpx.Request("POST", "https://provider.test/messages")
    assert not is_retryable_stream_error(
        openai.BadRequestError(
            "bad request",
            response=httpx.Response(400, request=request),
            body={"error": {"message": "bad request"}},
        )
    )


def test_recovery_controller_advances_early_retry_and_discards_holdback() -> None:
    controller = RecoveryController(provider_name="TEST", request_id="REQ")

    assert controller.push("hidden") == []
    decision = controller.advance_failure(
        httpx.ReadError("early cutoff"),
        stream_opened=True,
        generated_output=True,
        complete_tool_salvageable=False,
    )

    assert decision.action == RecoveryFailureAction.EARLY_RETRY
    assert decision.early_retry_attempt == 1
    assert controller.early_retries == 1
    assert not controller.committed
    assert not controller.has_buffered
    assert controller.flush() == []


def test_recovery_controller_retries_statusless_transient_api_error() -> None:
    controller = RecoveryController(provider_name="TEST", request_id="REQ")

    decision = controller.advance_failure(
        _statusless_openai_api_error(
            "ResourceExhausted: limit reached while generating response",
            {"error": {"message": "ResourceExhausted: limit reached"}},
        ),
        stream_opened=True,
        generated_output=False,
        complete_tool_salvageable=False,
    )

    assert decision.action == RecoveryFailureAction.EARLY_RETRY
    assert decision.retryable
    assert decision.early_retry_attempt == 1


def test_recovery_controller_respects_early_retry_limit() -> None:
    controller = RecoveryController(provider_name="TEST", request_id=None)

    for attempt in range(1, EARLY_TRANSPARENT_MAX_RETRIES + 1):
        decision = controller.advance_failure(
            httpx.ReadError("cutoff"),
            stream_opened=True,
            generated_output=False,
            complete_tool_salvageable=False,
        )
        assert decision.action == RecoveryFailureAction.EARLY_RETRY
        assert decision.early_retry_attempt == attempt

    decision = controller.advance_failure(
        httpx.ReadError("cutoff"),
        stream_opened=True,
        generated_output=False,
        complete_tool_salvageable=False,
    )

    assert decision.action == RecoveryFailureAction.FINAL_ERROR
    assert controller.early_retries == EARLY_TRANSPARENT_MAX_RETRIES


def test_recovery_controller_classifies_midstream_recovery_after_commit() -> None:
    controller = RecoveryController(provider_name="TEST", request_id=None)

    assert controller.push("event: content_block_delta\n\n") == []
    assert controller.flush() == ["event: content_block_delta\n\n"]
    decision = controller.advance_failure(
        httpx.ReadError("midstream cutoff"),
        stream_opened=True,
        generated_output=True,
        complete_tool_salvageable=False,
    )

    assert decision.action == RecoveryFailureAction.MIDSTREAM_RECOVERY
    assert decision.retryable
    assert decision.committed
    assert controller.flush_uncommitted(decision) == []


def test_recovery_controller_flushes_uncommitted_midstream_decision() -> None:
    controller = RecoveryController(provider_name="TEST", request_id=None)

    assert controller.push("event: content_block_delta\n\n") == []
    decision = controller.advance_failure(
        httpx.ReadError("midstream cutoff"),
        stream_opened=True,
        generated_output=True,
        complete_tool_salvageable=True,
    )

    assert decision.action == RecoveryFailureAction.MIDSTREAM_RECOVERY
    assert not decision.committed
    assert decision.has_buffered
    assert not controller.committed
    assert controller.has_buffered

    assert controller.flush_uncommitted(decision) == ["event: content_block_delta\n\n"]
    assert not decision.committed
    assert decision.has_buffered
    assert controller.committed
    assert not controller.has_buffered


def test_recovery_controller_non_retryable_error_is_final() -> None:
    request = httpx.Request("POST", "https://example.test/messages")
    error = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=httpx.Response(400, request=request),
    )
    controller = RecoveryController(provider_name="TEST", request_id=None)

    decision = controller.advance_failure(
        error,
        stream_opened=True,
        generated_output=True,
        complete_tool_salvageable=False,
    )

    assert decision.action == RecoveryFailureAction.FINAL_ERROR
    assert not decision.retryable
    assert controller.early_retries == 0


def test_holdback_buffers_until_delay_then_commits() -> None:
    """The window still commits on the clock -- it just starts later now.

    It is anchored to the first frame that shows the reader something,
    rather than to the first frame of any kind. The payload-less frames
    below classify as "unknown", which keeps the old timing behaviour for
    output this parser cannot read.
    """
    now = [10.0]
    holdback = RecoveryHoldbackBuffer(holdback_seconds=0.75, now=lambda: now[0])

    assert holdback.push("event: content_block_start\n\n") == []
    assert holdback.push("event: content_block_delta\n\n") == []
    now[0] += 0.74
    assert not holdback.committed

    now[0] += 0.01
    flushed = holdback.push("event: content_block_stop\n\n")
    assert flushed == [
        "event: content_block_start\n\n",
        "event: content_block_delta\n\n",
        "event: content_block_stop\n\n",
    ]
    assert holdback.committed
    assert holdback.push("event: message_stop\n\n") == ["event: message_stop\n\n"]


def test_holdback_flushes_at_internal_buffer_cap() -> None:
    holdback = RecoveryHoldbackBuffer(max_bytes=5, now=lambda: 1.0)

    assert holdback.push("ab") == []
    assert holdback.push("cde") == ["ab", "cde"]
    assert holdback.committed


def test_holdback_discard_drops_uncommitted_events() -> None:
    holdback = RecoveryHoldbackBuffer(now=lambda: 1.0)

    assert holdback.push("hidden") == []
    holdback.discard()

    assert holdback.flush() == []


def test_holdback_window_starts_when_the_upstream_stream_opens() -> None:
    """Time-to-first-token must not spend the window.

    The opening ``message_start`` frame is built locally and pushed before the
    upstream request goes out. Anchoring the window there meant a provider with
    a 9-180s TTFT -- i.e. every provider -- had already blown a 0.75s window by
    the time its first real byte arrived, so every stream committed on that
    byte and the model fallback chain could never engage.
    """
    now = [10.0]
    holdback = RecoveryHoldbackBuffer(holdback_seconds=0.75, now=lambda: now[0])

    assert holdback.push("event: message_start\n\n") == []
    now[0] += 30.0  # the upstream takes half a minute to answer
    holdback.restart_window()

    assert holdback.push("event: content_block_delta\n\n") == []
    assert not holdback.committed

    now[0] += 0.76
    assert holdback.push("event: content_block_stop\n\n") == [
        "event: message_start\n\n",
        "event: content_block_delta\n\n",
        "event: content_block_stop\n\n",
    ]
    assert holdback.committed


def test_restarting_the_window_after_commit_does_not_uncommit() -> None:
    holdback = RecoveryHoldbackBuffer(max_bytes=1, now=lambda: 1.0)
    holdback.push("ab")
    assert holdback.committed

    holdback.restart_window()

    assert holdback.committed
    assert holdback.push("cd") == ["cd"]


# ------------------------------------------------- content commit boundary --
#
# Committing on the first *frame* meant a model that sent a header and then
# stalled had already burned the route. Measured on 21 days of real traffic:
# 500 requests hung for the full 600s budget with a three-model chain sitting
# unused, and every failed request in that window had tokens_out = 0 -- so no
# fallback would ever have spliced two answers together.


def _sse(event: str, payload: str) -> str:
    return f"event: {event}\ndata: {payload}\n\n"


MESSAGE_START = _sse(
    "message_start",
    '{"type":"message_start","message":{"id":"msg_1","content":[]}}',
)
PING = _sse("ping", '{"type":"ping"}')
BLOCK_START = _sse(
    "content_block_start",
    '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
)
TEXT = _sse(
    "content_block_delta",
    '{"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hello"}}',
)
THINKING = _sse(
    "content_block_delta",
    '{"type":"content_block_delta","index":0,'
    '"delta":{"type":"thinking_delta","thinking":"hmm"}}',
)
TOOL_ARGS = _sse(
    "content_block_delta",
    '{"type":"content_block_delta","index":0,'
    '"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":1}"}}',
)


def test_scaffolding_never_commits_however_long_it_takes() -> None:
    """The exact shape of the 600s hangs: a header, then nothing.

    The clock used to commit this stream 0.75s in, which made the model
    unfallbackable while the reader had been shown nothing at all.
    """
    clock = iter([0.0, 0.5, 900.0, 1800.0, 3600.0])
    buffer = RecoveryHoldbackBuffer(now=lambda: next(clock))

    assert buffer.push(MESSAGE_START) == []
    assert buffer.push(PING) == []
    assert buffer.push(BLOCK_START) == []
    assert not buffer.committed
    assert buffer.has_buffered


def test_the_first_real_token_starts_the_window_then_commits() -> None:
    """Content starts the clock; the window still buys an invisible retry.

    Committing on content's first byte instead would remove the grace the
    window exists for: bytes still held have not reached the client, which is
    what lets the provider retry an immediate cutoff with no visible seam.
    Scaffolding is what must never start it.
    """
    now = [0.0]
    buffer = RecoveryHoldbackBuffer(now=lambda: now[0])

    assert buffer.push(MESSAGE_START) == []
    assert buffer.push(BLOCK_START) == []
    # Content arrives and is still held: this is the retry grace.
    assert buffer.push(TEXT) == []
    assert not buffer.committed

    now[0] += 0.75
    released = buffer.push(TEXT)

    assert released == [MESSAGE_START, BLOCK_START, TEXT, TEXT]
    assert buffer.committed
    # Past the boundary every event goes straight out.
    assert buffer.push(PING) == [PING]


def test_reasoning_and_tool_arguments_are_not_scaffolding() -> None:
    """A turn that only thinks, or only calls a tool, has still shown its work.

    Under Claude Code most turns call tools and produce no prose at all, so
    treating a reasoning or tool-argument delta as envelope would hold a whole
    real answer back from the reader until the byte cap.
    """
    for content in (THINKING, TOOL_ARGS):
        now = [0.0]
        buffer = RecoveryHoldbackBuffer(now=lambda clock=now: clock[0])
        assert buffer.push(MESSAGE_START) == []
        # Scaffolding leaves the window unstarted, so this frame anchors it.
        assert buffer.push(content) == []
        now[0] += 0.75
        assert buffer.push(content)[-1] == content
        assert buffer.committed


def test_scaffolding_alone_never_starts_the_window_at_any_length() -> None:
    """The property the whole change rests on, stated once, directly."""
    message_delta = _sse(
        "message_delta",
        '{"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":3}}',
    )
    # message_delta carries a "delta" key holding a stop reason, not answer
    # text. Classifying on frame type rather than on the presence of a delta
    # field is what keeps it on the right side of the line.
    for frame in (MESSAGE_START, PING, BLOCK_START, message_delta):
        assert sse_is_scaffolding(frame), frame
    for frame in (TEXT, THINKING, TOOL_ARGS):
        assert not sse_is_scaffolding(frame), frame
    # Anything unrecognised is treated as content, so it starts the window
    # and is merely delayed rather than held indefinitely.
    assert not sse_is_scaffolding(_sse("surprise", '{"type":"surprise"}'))


def test_output_the_parser_does_not_recognise_still_commits_on_the_clock() -> None:
    """An unclassifiable frame must be delayed, never held forever.

    Recognised scaffolding is exempt from the clock -- holding it is the point.
    Anything else keeps the old time-based behaviour, so a provider emitting
    some shape this parser has never seen degrades to a 0.75s delay rather than
    to a stream that never arrives.
    """
    now = 0.0
    buffer = RecoveryHoldbackBuffer(now=lambda: now)
    surprise = _sse("surprise", '{"type":"surprise"}')

    assert buffer.push(surprise) == []
    assert not buffer.committed

    now = 10.0
    assert buffer.push(surprise) == [surprise, surprise]
    assert buffer.committed


def test_scaffolding_is_exempt_from_the_clock_but_unknown_output_is_not() -> None:
    """The two paths differ only in what the frame is, so pin them together."""
    now = 0.0
    scaffolding_only = RecoveryHoldbackBuffer(now=lambda: now)
    assert scaffolding_only.push(MESSAGE_START) == []
    now = 3600.0
    assert scaffolding_only.push(PING) == []
    assert not scaffolding_only.committed


def test_the_byte_cap_still_bounds_what_is_held() -> None:
    """Holding longer must not mean holding without limit."""
    buffer = RecoveryHoldbackBuffer(max_bytes=len(MESSAGE_START) + 1, now=lambda: 0.0)

    assert buffer.push(MESSAGE_START) == []
    assert buffer.push(PING) == [MESSAGE_START, PING]
    assert buffer.committed


def test_reasoning_commits_the_route_by_default() -> None:
    """The shipped classifier treats a thought as answer content."""
    assert not sse_is_scaffolding(THINKING)
    clock = iter([0.0, 0.1, 5.0])
    buffer = RecoveryHoldbackBuffer(now=lambda: next(clock))
    assert buffer.push(THINKING) == []
    assert buffer.push(THINKING) == [THINKING, THINKING]
    assert buffer.committed


def test_reasoning_alone_never_commits_when_fallback_is_preferred() -> None:
    """The measured shape of 44 of 499 budget exhaustions.

    A primary that thinks for the whole request budget and never writes an
    answer used to commit on its first thought, which spent an eight-model
    chain on nothing. Held back, the attempt stays uncommitted and the route
    can still move.
    """
    assert sse_is_scaffolding(THINKING, reasoning_commits=False)
    clock = iter([0.0, 300.0, 600.0, 1200.0])
    buffer = RecoveryHoldbackBuffer(reasoning_commits=False, now=lambda: next(clock))

    assert buffer.push(MESSAGE_START) == []
    assert buffer.push(THINKING) == []
    assert buffer.push(THINKING) == []
    assert not buffer.committed
    assert buffer.has_buffered


def test_a_signature_delta_travels_with_the_thinking_block_it_signs() -> None:
    """It is the cryptographic tail of a thought, not a word of the answer."""
    signature = _sse(
        "content_block_delta",
        '{"type":"content_block_delta","index":0,'
        '"delta":{"type":"signature_delta","signature":"abc"}}',
    )
    assert sse_is_scaffolding(signature, reasoning_commits=False)
    assert not sse_is_scaffolding(signature)


def test_an_answer_still_commits_when_reasoning_does_not() -> None:
    """Holding thoughts back must not hold the answer back too."""
    clock = iter([0.0, 0.1, 5.0])
    buffer = RecoveryHoldbackBuffer(reasoning_commits=False, now=lambda: next(clock))

    assert buffer.push(THINKING) == []
    buffer.push(TEXT)
    assert buffer.push(TEXT) == [THINKING, TEXT, TEXT]
    assert buffer.committed


def test_tool_arguments_are_an_answer_even_when_reasoning_is_not() -> None:
    """A tool call is the model acting, not deliberating."""
    assert not sse_is_scaffolding(TOOL_ARGS, reasoning_commits=False)


def test_an_early_retry_keeps_holding_reasoning_back() -> None:
    """The retry path rebuilds the buffer; it must not revert to the default.

    Constructing the replacement inline is what made this losable: the setting
    would apply to the first attempt and silently not to the second.
    """
    controller = RecoveryController(
        provider_name="p",
        request_id="r",
        reasoning_commits=False,
    )
    controller.push(THINKING)
    decision = controller.advance_failure(
        httpx.ReadError("reset"),
        stream_opened=True,
        generated_output=False,
        complete_tool_salvageable=False,
    )
    assert decision.action is RecoveryFailureAction.EARLY_RETRY

    # The wait is the assertion. Pushing twice back to back proves nothing:
    # a buffer that had reverted to committing on reasoning would still be
    # inside its 0.75s window and would return [] for the same reason the
    # correct one does. Only a push from outside the window separates them.
    controller.push(THINKING)
    time.sleep(0.8)
    # A heartbeat, not an event: nothing to forward, and the route is still
    # abandonable. A buffer that had reverted would return the thoughts here.
    assert controller.push(THINKING) == [REASONING_HEARTBEAT]
    assert not controller.committed
