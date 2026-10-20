"""What a host reports about its reasoning is recorded even when the text is hidden (7.103.0).

A model can think while the provider keeps the text: Claude 5's default
``display: omitted`` (a signed thinking block with no text), Claude Code's own
redact/updates display, a Responses host that returns only
``encrypted_content``. Before 7.103.0 every one of those was recorded as "no
thinking": ``thinking_chars`` counts visible text, and the reasoning-token
count the host sends was read only from Chat Completions hosts and only with
cost tracking on (spec INVESTIGATION-INVISIBLE-REASONING.md, decisions Q4/Q5/Q7).

Pinned here:

* one reader for the three protocols' reasoning-token fields, which never
  touches a cost field;
* the request's collector exists for every logged request, so the count is
  stored with cost tracking off -- and pricing still needs it on;
* the Anthropic and Responses tallies count thinking blocks, withheld and
  redacted ones, reasoning items, encrypted-only items and summary parts;
* a reply that carries none of it is recorded exactly as before (no
  ``reasoning`` key in its shape), so rows where the host reports nothing are
  byte-identical;
* nothing sent to the client changes: the frames pass through as before.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.cost import RateCard, TokenUsage, compute_from_rates
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.reported_cost import (
    install_reported_cost,
    record_reported_reasoning,
    reported_reasoning_tokens,
)
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.wire_capture import ResponseShape
from my_claude_code.providers.anthropic_messages.streaming import (
    iter_anthropic_sse_frames,
)
from my_claude_code.providers.openai_responses.streaming import (
    ResponsesStreamConverter,
    note_responses_event_shape,
)

OLD_SHAPE_KEYS = [
    "fields",
    "chunks",
    "finish_reason",
    "usage",
    "usage_keys",
    "first_chunk_ms",
]


# --------------------------------------------------------------------------
# One reader, three protocols, no cost field
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "tokens"),
    [
        ({"completion_tokens_details": {"reasoning_tokens": 12}}, 12),
        ({"output_tokens_details": {"reasoning_tokens": 1126}}, 1126),
        ({"output_tokens_details": {"thinking_tokens": 511}}, 511),
        ({"output_tokens_details": {"thinking_tokens": 0}}, 0),
        ({"output_tokens_details": {}}, None),
        ({"output_tokens_details": {"thinking_tokens": True}}, None),
        ({"output_tokens": 30}, None),
        (None, None),
    ],
    ids=["chat", "responses", "anthropic", "zero", "empty", "bool", "absent", "none"],
)
def test_one_reader_names_every_protocols_reasoning_tokens(
    usage: Any, tokens: int | None
) -> None:
    assert reported_reasoning_tokens(usage) == tokens


def test_recording_reasoning_never_touches_a_cost_field() -> None:
    slot = install_reported_cost()

    record_reported_reasoning(
        {
            "cost": 0.5,
            "is_byok": False,
            "output_tokens_details": {"reasoning_tokens": 9},
        }
    )

    assert slot.reasoning_tokens == 9
    assert (slot.cost_usd, slot.is_byok, slot.total_usd) == (None, None, None)


# --------------------------------------------------------------------------
# The collector exists without cost tracking; pricing still needs it
# --------------------------------------------------------------------------


def _capture(store: RequestLogStore, **kwargs: Any) -> RequestCapture:
    return RequestCapture(
        store,
        request_id="req-1",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="anthropic_oauth/claude-haiku-5-5",
        input_text=None,
        params=None,
        **kwargs,
    )


def _finalize(capture: RequestCapture) -> None:
    record = capture._record
    record.provider = "anthropic_oauth"
    record.resolved_model = "claude-haiku-5-5"
    capture._tokens_in = 100
    capture._tokens_out = 600
    capture._finalize("success")


def test_reasoning_tokens_are_stored_with_cost_tracking_off(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store, cost_enabled=False)

    record_reported_reasoning({"output_tokens_details": {"thinking_tokens": 511}})
    _finalize(capture)
    store.close()
    row = store.get_request("req-1")

    assert row is not None
    assert row["reasoning_tokens"] == 511
    assert row["cost_usd"] is None


def test_a_host_that_reports_nothing_still_stores_null(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store, cost_enabled=False)

    _finalize(capture)
    store.close()
    row = store.get_request("req-1")

    assert row is not None
    assert row["reasoning_tokens"] is None


def test_reasoning_tokens_change_no_price_without_a_reasoning_rate() -> None:
    card = RateCard(
        source="models_dev", input_price=1e-6, output_price=4e-6, reasoning_price=None
    )
    without = TokenUsage(tokens_in=100, tokens_out=600)
    with_reasoning = TokenUsage(tokens_in=100, tokens_out=600, reasoning_tokens=511)

    assert compute_from_rates(with_reasoning, card) == compute_from_rates(without, card)


# --------------------------------------------------------------------------
# Anthropic: thinking blocks, withheld and redacted, reported tokens
# --------------------------------------------------------------------------


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )


def _start(index: int, block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, delta: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def _stop(index: int) -> dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


def _message(*blocks: dict[str, Any], usage: dict[str, Any] | None = None) -> bytes:
    return _sse(
        {"type": "message_start", "message": {"id": "msg_1", "usage": {}}},
        *blocks,
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": usage or {"output_tokens": 20},
        },
        {"type": "message_stop"},
    )


async def _chunks(raw: bytes) -> AsyncIterator[bytes]:
    yield raw


async def _tally(raw: bytes) -> tuple[dict[str, Any], list[str]]:
    shape = ResponseShape(started_at=0.0)
    frames = [frame async for frame in iter_anthropic_sse_frames(_chunks(raw), shape)]
    return shape.payload(), frames


WITHHELD = (
    _start(0, {"type": "thinking", "thinking": "", "signature": ""}),
    _delta(0, {"type": "thinking_delta", "thinking": ""}),
    _delta(0, {"type": "signature_delta", "signature": "E" * 2872}),
    _stop(0),
)
TEXT = (
    _start(1, {"type": "text", "text": ""}),
    _delta(1, {"type": "text_delta", "text": "Answer."}),
    _stop(1),
)
THINKING_USAGE = {
    "output_tokens": 600,
    "output_tokens_details": {"thinking_tokens": 511},
}


@pytest.mark.asyncio
async def test_a_signed_empty_thinking_block_is_counted_as_withheld() -> None:
    slot = install_reported_cost()

    payload, _frames = await _tally(_message(*WITHHELD, *TEXT, usage=THINKING_USAGE))

    assert payload["reasoning"] == {
        "reported_tokens": 511,
        "thinking_blocks": 1,
        "withheld_thinking_blocks": 1,
    }
    assert slot.reasoning_tokens == 511


@pytest.mark.asyncio
async def test_visible_thinking_is_a_block_but_not_withheld() -> None:
    shown = (
        _start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "I should read it."}),
        _delta(0, {"type": "signature_delta", "signature": "EqQB"}),
        _stop(0),
    )

    payload, _frames = await _tally(_message(*shown, *TEXT))

    assert payload["reasoning"] == {"thinking_blocks": 1}


@pytest.mark.asyncio
async def test_a_redacted_thinking_block_is_counted() -> None:
    redacted = (
        _start(0, {"type": "redacted_thinking", "data": "EmwKAhgB"}),
        _stop(0),
    )

    payload, _frames = await _tally(_message(*redacted, *TEXT))

    assert payload["reasoning"] == {"redacted_thinking_blocks": 1}


@pytest.mark.asyncio
async def test_the_client_receives_the_same_frames() -> None:
    raw = _message(*WITHHELD, *TEXT, usage=THINKING_USAGE)

    _payload, frames = await _tally(raw)
    plain = [frame async for frame in iter_anthropic_sse_frames(_chunks(raw), None)]

    assert frames == plain
    assert "thinking_tokens" in frames[-2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocks",
    [
        TEXT,
        (
            _start(
                0, {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
            ),
            _delta(0, {"type": "input_json_delta", "partial_json": '{"p": 1}'}),
            _stop(0),
        ),
    ],
    ids=["text", "tool"],
)
async def test_a_reply_without_reasoning_is_shaped_exactly_as_before(
    blocks: tuple[dict[str, Any], ...],
) -> None:
    payload, _frames = await _tally(_message(*blocks))

    assert list(payload) == OLD_SHAPE_KEYS


# --------------------------------------------------------------------------
# Responses: reasoning items, encrypted-only, summary parts, reported tokens
# --------------------------------------------------------------------------


def _responses_tally(*events: dict[str, Any]) -> dict[str, Any]:
    shape = ResponseShape(started_at=0.0)
    for event in events:
        note_responses_event_shape(shape, event)
    return shape.payload()


def _item(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "item": item}


def _completed(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {"status": "completed", "usage": usage},
    }


RESPONSES_USAGE = {
    "input_tokens": 10,
    "output_tokens": 1200,
    "output_tokens_details": {"reasoning_tokens": 1126},
}


def test_an_encrypted_only_reasoning_item_is_counted() -> None:
    encrypted = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "gAAAA",
    }

    payload = _responses_tally(
        _item("response.output_item.added", encrypted),
        _item("response.output_item.done", encrypted),
        _completed(RESPONSES_USAGE),
    )

    assert payload["reasoning"] == {
        "encrypted_only_items": 1,
        "reasoning_items": 1,
        "reported_tokens": 1126,
        "summary_parts": 0,
        "summary_parts_with_text": 0,
    }


def test_a_summarised_reasoning_item_counts_its_parts() -> None:
    summarised = {
        "type": "reasoning",
        "id": "rs_2",
        "summary": [
            {"type": "summary_text", "text": "Reading the file."},
            {"type": "summary_text", "text": ""},
        ],
        "encrypted_content": "gAAAA",
    }

    payload = _responses_tally(
        _item("response.output_item.added", summarised),
        _item("response.output_item.done", summarised),
    )

    assert payload["reasoning"] == {
        "reasoning_items": 1,
        "summary_parts": 2,
        "summary_parts_with_text": 1,
    }


def test_a_responses_reply_without_reasoning_is_shaped_exactly_as_before() -> None:
    payload = _responses_tally(
        {"type": "response.output_text.delta", "delta": "Answer."},
        _completed({"input_tokens": 10, "output_tokens": 5}),
    )

    assert list(payload) == OLD_SHAPE_KEYS


def test_the_responses_converter_hands_the_count_to_the_collector() -> None:
    slot = install_reported_cost()
    converter = ResponsesStreamConverter(AnthropicStreamLedger("msg_1", "m"))

    list(converter.feed(_completed(RESPONSES_USAGE)))

    assert slot.reasoning_tokens == 1126
