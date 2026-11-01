"""The terminal frames of a Responses stream, and what each one must become.

A Responses stream ends in one of three ways, and until 8.0.1 MCC translated
only the first: ``response.completed``. ``response.incomplete`` and
``response.failed`` fell off the end of the converter, so ``finish()`` closed
the message as ``end_turn`` with the converter's own default usage of ``0 / 0``
and the executor recorded a success with no content.

That is not a hypothetical. A raw capture of the Codex backend on 7.113.4
(``specs/INVESTIGATION-DESKTOP-CHATGPT-EMPTY.md``, CAUSE FOUND) shows a
513-tool Claude Desktop request ending:

    response.incomplete  status: "incomplete"
    incomplete_details: {"reason": "max_output_tokens"}
    usage: all zeros   output items: none

and 34 requests in 30 days -- *exactly* the 34 non-``completed`` terminal
events -- reached the client as empty successes with no fallback.

What each terminal kind must become:

* ``completed`` -- unchanged, byte for byte (the equality contract below pins
  every completed shape against the 8.0.0 converter's own output);
* ``incomplete`` with partial output -- a truncated answer: the message ends
  ``max_tokens``, and the usage the host reported is read;
* ``incomplete`` with no output at all and no output tokens reported -- a
  classified failure, so the chain asks the next model instead of answering
  with silence;
* ``failed`` -- ``TruncatedProviderStreamError``, whatever it streamed first.

``incomplete_details.reason`` is recorded on the attempt's ``response_shape``
(B), because that word is the only evidence that names the host's own reason.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from my_claude_code.application.execution import (
    ProviderExecutor,
    RouteAttemptRecord,
)
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.core.wire_capture import (
    _WIRE_TRACE,
    ResponseShape,
    WireTrace,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.openai_responses.streaming import (
    ResponsesStreamConverter,
    note_responses_event_shape,
)
from my_claude_code.providers.stream_recovery import TruncatedProviderStreamError
from tests.providers.support import passthrough_rate_limiter

BASELINE_PATH = Path(__file__).with_name("responses_completed_equality_baseline.json")

#: The recorded failing shape (2026-10-29, 7.113.4): lifecycle frames only, then
#: a terminal ``incomplete`` with no output items and all-zero usage.
_RECORDED_INCOMPLETE_FRAMES: list[dict[str, Any]] = [
    {"type": "response.created", "response": {"status": "in_progress"}},
    {"type": "response.in_progress", "response": {"status": "in_progress"}},
    {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
    {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        },
    },
]


def _ledger() -> AnthropicStreamLedger:
    return AnthropicStreamLedger("msg_fixed", "gpt-6-luna", input_tokens=0)


def _run(events: list[dict[str, Any]]) -> list[str]:
    """Drive one stream through the converter and return every frame sent."""
    ledger = _ledger()
    converter = ResponsesStreamConverter(ledger)
    frames = [ledger.message_start()]
    for event in events:
        frames.extend(converter.feed(event))
    frames.extend(converter.finish())
    return frames


def _message_delta(frames: list[str]) -> dict[str, Any]:
    for frame in frames:
        event = parse_sse_text(frame)
        if event and event[0].event == "message_delta":
            return event[0].data
    raise AssertionError("the stream carried no message_delta")


# --- A: the terminal kinds -------------------------------------------------


def test_the_recorded_incomplete_shape_is_a_classified_failure() -> None:
    """No output and zero usage is a refusal, not an empty answer.

    This is the exact shape the raw capture recorded, and the one MCC called a
    success 25 times in 30 days.
    """
    ledger = _ledger()
    converter = ResponsesStreamConverter(ledger)
    emitted: list[str] = []
    with pytest.raises(TruncatedProviderStreamError) as raised:
        for event in _RECORDED_INCOMPLETE_FRAMES:
            emitted.extend(converter.feed(event))
    assert emitted == []
    assert "incomplete_details.reason: max_output_tokens" in str(raised.value)
    assert not ledger.message_started


def test_an_incomplete_with_no_usage_block_is_also_a_failure() -> None:
    """Silence about tokens is not evidence that tokens were produced."""
    events = [{"type": "response.incomplete", "response": {"status": "incomplete"}}]
    with pytest.raises(TruncatedProviderStreamError):
        _run(events)


def test_an_incomplete_that_charged_only_the_prompt_is_a_failure() -> None:
    """Prompt tokens say what was asked, not what came back."""
    with pytest.raises(TruncatedProviderStreamError):
        _run(
            [
                {
                    "type": "response.incomplete",
                    "response": {
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {"input_tokens": 512, "output_tokens": 0},
                    },
                }
            ]
        )


def test_incomplete_with_partial_text_finishes_as_a_truncated_answer() -> None:
    """A cut answer is not an error: it ends ``max_tokens`` and keeps its usage."""
    frames = _run(
        [
            {"type": "response.output_text.delta", "delta": "the answer is "},
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            },
        ]
    )

    delta = _message_delta(frames)
    assert delta["delta"] == {"stop_reason": "max_tokens", "stop_sequence": None}
    # Usage is read exactly as ``completed`` reads it, not left at MCC's zeros.
    assert delta["usage"]["input_tokens"] == 5
    assert delta["usage"]["output_tokens"] == 1
    assert "the answer is " in "".join(frames)


def test_incomplete_with_a_partial_tool_call_ends_max_tokens() -> None:
    """A half-written call must not be reported as a call the client may run."""
    frames = _run(
        [
            {
                "type": "response.output_item.added",
                "item": {"id": "call_1", "type": "function_call", "name": "bash"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_1",
                "delta": '{"command":',
            },
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "usage": {"input_tokens": 3, "output_tokens": 9},
                },
            },
        ]
    )

    assert _message_delta(frames)["delta"]["stop_reason"] == "max_tokens"


def test_an_incomplete_turn_that_only_reasoned_is_still_output() -> None:
    """Reasoning the client's policy hides is still output the host produced."""
    frames = _run(
        [
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "rs_1",
                "summary_index": 0,
                "delta": "thinking",
            },
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ]
    )

    assert _message_delta(frames)["delta"]["stop_reason"] == "max_tokens"


def test_response_failed_raises_the_truncated_stream_error() -> None:
    with pytest.raises(TruncatedProviderStreamError):
        _run(
            [
                {"type": "response.created", "response": {"status": "in_progress"}},
                {"type": "response.failed", "response": {"status": "failed"}},
            ]
        )


def test_response_failed_after_partial_text_still_raises() -> None:
    """What arrived first is emitted; the turn is then a failure, never a success."""
    ledger = _ledger()
    converter = ResponsesStreamConverter(ledger)
    emitted = list(
        converter.feed({"type": "response.output_text.delta", "delta": "half"})
    )
    with pytest.raises(TruncatedProviderStreamError):
        list(
            converter.feed(
                {"type": "response.failed", "response": {"status": "failed"}}
            )
        )

    assert "half" in "".join(emitted)


def test_response_done_carrying_incomplete_is_treated_as_incomplete() -> None:
    """``response.done`` is an alias: the payload's status decides."""
    with pytest.raises(TruncatedProviderStreamError):
        _run(
            [
                {
                    "type": "response.done",
                    "response": {"status": "incomplete", "usage": {"input_tokens": 0}},
                }
            ]
        )


def test_response_done_carrying_failed_is_treated_as_failed() -> None:
    with pytest.raises(TruncatedProviderStreamError):
        _run([{"type": "response.done", "response": {"status": "failed"}}])


def test_a_zero_usage_completed_stream_keeps_todays_behaviour() -> None:
    """Do not reclassify it: a completed turn is finished, however it counted."""
    frames = _run(
        [
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        ]
    )

    delta = _message_delta(frames)
    assert delta["delta"]["stop_reason"] == "end_turn"
    assert delta["usage"]["input_tokens"] == 0
    assert delta["usage"]["output_tokens"] == 0


def test_usage_on_incomplete_matches_usage_on_completed_exactly() -> None:
    """The same block, read by the same code, under every cache field."""
    usage = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 900, "cache_write_tokens": 40},
        "output_tokens": 50,
        "output_tokens_details": {"reasoning_tokens": 20},
        "total_tokens": 1050,
    }
    completed = _message_delta(
        _run(
            [
                {"type": "response.output_text.delta", "delta": "x"},
                {"type": "response.completed", "response": {"usage": usage}},
            ]
        )
    )["usage"]
    incomplete = _message_delta(
        _run(
            [
                {"type": "response.output_text.delta", "delta": "x"},
                {"type": "response.incomplete", "response": {"usage": usage}},
            ]
        )
    )["usage"]

    assert incomplete == completed
    assert incomplete["cache_read_input_tokens"] == 900
    assert incomplete["input_tokens"] + incomplete["cache_read_input_tokens"] == 1000


# --- B: what the attempt row records ---------------------------------------


def _shape_for(events: list[dict[str, Any]]) -> dict[str, Any]:
    shape = ResponseShape(started_at=0.0)
    for event in events:
        note_responses_event_shape(shape, event)
    return shape.payload()


def test_the_incomplete_reason_is_recorded_verbatim() -> None:
    payload = _shape_for(_RECORDED_INCOMPLETE_FRAMES)

    assert payload["finish_reason"] == "incomplete"
    assert payload["incomplete_reason"] == "max_output_tokens"
    assert payload["usage"] is True
    assert payload["usage_keys"] == ["input_tokens", "output_tokens", "total_tokens"]


def test_a_completed_frame_records_no_reason_key() -> None:
    """The completed row keeps exactly the payload it had before 8.0.1."""
    payload = _shape_for(
        [
            {
                "type": "response.completed",
                "response": {"status": "completed", "usage": {"input_tokens": 1}},
            }
        ]
    )

    assert payload["finish_reason"] == "completed"
    assert "incomplete_reason" not in payload


def test_a_failed_frame_records_its_status_and_keeps_no_reason() -> None:
    payload = _shape_for(
        [{"type": "response.failed", "response": {"status": "failed"}}]
    )

    assert payload["finish_reason"] == "failed"
    assert "incomplete_reason" not in payload


def test_a_reason_longer_than_the_bound_is_truncated() -> None:
    shape = ResponseShape(started_at=0.0)
    shape.note_incomplete_reason("r" * 100)
    assert shape.payload()["incomplete_reason"] == "r" * 64


# --- the commit boundary and the chain -------------------------------------


def _provider_config() -> ProviderConfig:
    return ProviderConfig(
        api_key="test_token",
        base_url="https://chatgpt.com/backend-api",
        rate_limit=10,
        rate_window=60,
        max_concurrency=5,
        http_read_timeout=300.0,
        http_write_timeout=10.0,
        http_connect_timeout=60.0,
        proxy="",
    )


def _chatgpt_provider(events: list[dict[str, Any]]) -> ChatGPTOAuthProvider:
    """A real provider answering from a recorded-shape fake upstream."""

    provider = ChatGPTOAuthProvider(
        _provider_config(),
        rate_limiter=passthrough_rate_limiter(),
        account_id="test_account_id",
    )
    payload = b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)

    async def _raw_stream() -> AsyncIterator[bytes]:
        yield payload

    response = MagicMock(status_code=200)
    response.aiter_raw = _raw_stream
    response.aclose = AsyncMock()
    provider._client.build_request = MagicMock(return_value=MagicMock())
    provider._client.send = AsyncMock(return_value=response)
    return provider


def _messages_request(model: str = "gpt-6-luna") -> MessagesRequest:
    return MessagesRequest(
        model=model,
        messages=[Message(role="user", content="hello")],
        max_tokens=64,
    )


def _routed(provider_id: str, model: str) -> RoutedMessagesRequest:
    return RoutedMessagesRequest(
        request=_messages_request(model),
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id=provider_id,
            provider_model=model,
            provider_model_ref=f"{provider_id}/{model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


class _AnsweringProvider:
    """The next model on the chain, with a short answer of its own."""

    credential_label = None

    def __init__(self, model: str) -> None:
        self._model = model
        self.stream_calls = 0

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        return None

    async def stream_response(
        self,
        request: MessagesRequest,
        *,
        input_tokens: int = 0,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.stream_calls += 1
        ledger = AnthropicStreamLedger("msg_secondary", self._model, input_tokens)
        yield ledger.message_start()
        yield ledger.content_block_start(0, "text")
        yield ledger.content_block_delta(0, "text_delta", "the next model answered")
        yield ledger.content_block_stop(0)
        yield ledger.message_delta("end_turn", 4, input_tokens=17)
        yield ledger.message_stop()


async def _run_chain(
    primary_events: list[dict[str, Any]],
    request_id: str,
) -> tuple[list[str], list[RouteAttemptRecord], _AnsweringProvider]:
    primary = _chatgpt_provider(primary_events)
    secondary = _AnsweringProvider("fallback-model")
    providers = {"chatgpt_oauth": primary, "secondary": secondary}
    attempts: list[RouteAttemptRecord] = []
    executor = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 7,
        health=RouteHealthRegistry(eject_after_failures=0),
    )
    chunks = [
        chunk
        async for chunk in executor.stream(
            RoutedMessagesPlan(
                (
                    _routed("chatgpt_oauth", "gpt-6-luna"),
                    _routed("secondary", "fallback-model"),
                )
            ),
            wire_api="messages",
            raw_log_label="FULL_PAYLOAD",
            raw_log_payload={},
            request_id=request_id,
            on_attempt_result=attempts.append,
        )
    ]
    return chunks, attempts, secondary


@pytest.mark.asyncio
async def test_an_empty_incomplete_turn_falls_through_to_the_next_model() -> None:
    """The whole point: nothing reached the client, so the next model answers."""
    chunks, attempts, secondary = await _run_chain(
        _RECORDED_INCOMPLETE_FRAMES, "req_incomplete_chain"
    )

    assert "the next model answered" in "".join(chunks)
    assert secondary.stream_calls == 1
    failed = [record for record in attempts if record.outcome == "failed"]
    assert [record.model_ref for record in failed] == ["chatgpt_oauth/gpt-6-luna"]
    assert failed[0].error_kind == "upstream"
    assert "incomplete_details.reason: max_output_tokens" in (
        failed[0].error_message or ""
    )
    assert [record.outcome for record in attempts] == ["failed", "succeeded"]


@pytest.mark.asyncio
async def test_a_failed_turn_falls_through_to_the_next_model() -> None:
    chunks, attempts, secondary = await _run_chain(
        [
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.failed", "response": {"status": "failed"}},
        ],
        "req_failed_chain",
    )

    assert "the next model answered" in "".join(chunks)
    assert secondary.stream_calls == 1
    failed = [record for record in attempts if record.outcome == "failed"]
    assert failed[0].error_kind == "upstream"


@pytest.mark.asyncio
async def test_a_completed_turn_is_answered_by_the_primary_alone() -> None:
    """The equality half at the chain level: a working turn never moves."""
    chunks, attempts, secondary = await _run_chain(
        [
            {"type": "response.output_text.delta", "delta": "primary says hi"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                },
            },
        ],
        "req_completed_chain",
    )

    assert "primary says hi" in "".join(chunks)
    assert secondary.stream_calls == 0
    # The fallback is named in the request log as skipped, not as an attempt.
    assert [(record.model_ref, record.outcome) for record in attempts] == [
        ("chatgpt_oauth/gpt-6-luna", "succeeded"),
        ("secondary/fallback-model", "skipped"),
    ]


@pytest.mark.asyncio
async def test_a_partial_incomplete_answer_reaches_the_client_and_no_fallback() -> None:
    chunks, attempts, secondary = await _run_chain(
        [
            {"type": "response.output_text.delta", "delta": "a partial ans"},
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 4, "output_tokens": 2},
                },
            },
        ],
        "req_partial_chain",
    )
    text = "".join(chunks)

    assert "a partial ans" in text
    assert secondary.stream_calls == 0
    assert '"stop_reason": "max_tokens"' in text
    assert '"output_tokens": 2' in text
    # A truncated answer is not an error: the primary answers the request.
    assert [record.outcome for record in attempts] == ["succeeded", "skipped"]


@pytest.mark.asyncio
async def test_the_failed_attempt_records_its_shape_on_the_wire_trace() -> None:
    """B at the provider boundary: the row carries the host's own reason."""
    provider = _chatgpt_provider(_RECORDED_INCOMPLETE_FRAMES)
    trace = WireTrace()
    token = _WIRE_TRACE.set(trace)
    try:
        with pytest.raises(ExecutionFailure) as raised:
            async for _ in provider.stream_response(
                _messages_request(), request_id="req_shape"
            ):
                pass
    finally:
        _WIRE_TRACE.reset(token)

    assert raised.value.kind is FailureKind.UPSTREAM

    shape = trace.responses[0]
    assert shape["finish_reason"] == "incomplete"
    assert shape["incomplete_reason"] == "max_output_tokens"
    assert shape["fields"] == {}
    assert shape["usage"] is True


@pytest.mark.asyncio
async def test_a_completed_attempt_records_no_reason_key_on_the_wire_trace() -> None:
    provider = _chatgpt_provider(
        [
            {"type": "response.output_text.delta", "delta": "ok"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            },
        ]
    )
    trace = WireTrace()
    token = _WIRE_TRACE.set(trace)
    try:
        async for _ in provider.stream_response(
            _messages_request(), request_id="req_shape_ok"
        ):
            pass
    finally:
        _WIRE_TRACE.reset(token)

    shape = trace.responses[0]
    assert shape["finish_reason"] == "completed"
    assert "incomplete_reason" not in shape


# --- the equality contract for completed streams ---------------------------

#: Completed shapes, each one a stream the base converter must still produce
#: byte for byte. The digests were taken from ``ba831c05`` (8.0.0): each is the
#: sha256 of the converter's own frame list, newline-joined, for the same events
#: with the same fixed message id.
_COMPLETED_CORPUS: dict[str, list[dict[str, Any]]] = {
    "text_then_completed": [
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": "hello "},
        {"type": "response.output_text.delta", "delta": "world"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 11, "output_tokens": 2},
            },
        },
    ],
    "cached_usage": [
        {"type": "response.output_text.delta", "delta": "hi"},
        {
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 900},
                    "output_tokens": 50,
                    "output_tokens_details": {"reasoning_tokens": 20},
                    "total_tokens": 1050,
                }
            },
        },
    ],
    "zero_usage_no_output": [
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    ],
    "no_usage_block": [
        {"type": "response.output_text.delta", "delta": "x"},
        {"type": "response.completed", "response": {}},
    ],
    "response_done_alias": [
        {"type": "response.output_text.delta", "delta": "alias"},
        {"type": "response.done", "response": {"status": "completed"}},
    ],
    "tool_call": [
        {
            "type": "response.output_item.added",
            "item": {"id": "call_1", "type": "function_call", "name": "bash"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "call_1",
            "delta": '{"command":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "call_1",
            "delta": ' "ls"}',
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "call_1",
                "type": "function_call",
                "arguments": '{"command": "ls"}',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 7},
            },
        },
    ],
    "reasoning_then_text": [
        {
            "type": "response.output_item.added",
            "item": {"id": "rs_1", "type": "reasoning"},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "summary_index": 0,
            "delta": "thinking...",
        },
        {"type": "response.output_text.delta", "delta": "answer"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 9, "output_tokens": 4},
            },
        },
    ],
    "held_tag_then_completed": [
        {"type": "response.output_text.delta", "delta": "<think>why"},
        {"type": "response.output_text.delta", "delta": "</think>done"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ],
}


def _digest(frames: list[str]) -> str:
    return hashlib.sha256("\n".join(frames).encode()).hexdigest()


def test_the_baseline_covers_every_completed_shape_here() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert set(baseline) == set(_COMPLETED_CORPUS)


@pytest.mark.parametrize("name", sorted(_COMPLETED_CORPUS))
def test_a_completed_stream_is_byte_identical_to_8_0_0(name: str) -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    frames = _run(_COMPLETED_CORPUS[name])

    assert _digest(frames) == baseline[name], (
        f"the completed shape {name!r} changed; it must be byte-identical"
    )


def test_a_completed_stream_emits_every_frame_in_protocol_order() -> None:
    """A readable guard beside the digests: the union of completed endings."""
    frames = _run(_COMPLETED_CORPUS["tool_call"])

    assert [parse_sse_text(frame)[0].event for frame in frames] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
