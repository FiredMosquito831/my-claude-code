"""ChatGPT OAuth reasoning summaries become Anthropic thinking blocks.

The defect these tests pin down was measured, not guessed: on 2026-09-11 the
request log held 2,671 `chatgpt_oauth/gpt-5.6-luna` requests over seven days
with `thinking_chars = 0` on 100% of them, while MCC's own per-attempt response
shape recorded a `reasoning` delta arriving on 1,522 of 2,651 successful
attempts (57%) and the Models page counter read "requested 2652, returned 0".
`ChatGPTOAuthStreamConverter.feed()` had no branch for
`response.reasoning_summary_text.delta`; the diagnostics-only
`note_responses_event_shape()` was the only function in the module that knew
the event existed, so the text was tallied and dropped.

The fixtures below are the Responses API's documented event shapes for a
reasoning turn, in the order the endpoint sends them, and each asserts the
Anthropic SSE frames that must come out.
"""

import json
import time
from typing import Any

import pytest

from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
    text_content,
    thinking_content,
)
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.providers.chatgpt_oauth.streaming import (
    ChatGPTOAuthStreamConverter,
)

_SUMMARY = "**Reading the log**\n\nI should open the file before answering."


def _run(frames: list[dict[str, Any]], **kwargs: Any) -> str:
    """Drive one recorded Responses frame sequence through the converter."""
    ledger = AnthropicStreamLedger("msg_1", "gpt-5.6-luna", input_tokens=0)
    converter = ChatGPTOAuthStreamConverter(ledger, **kwargs)
    out = [ledger.message_start()]
    for frame in frames:
        out.extend(converter.feed(frame))
    out.extend(converter.finish())
    return "".join(out)


def _block_types(sse: str) -> list[str]:
    """The content block types in the order the client sees them open."""
    kinds: list[str] = []
    for event in parse_sse_text(sse):
        if event.event == "content_block_start":
            block = event.data.get("content_block", {})
            kinds.append(str(block.get("type", "")))
    return kinds


def _message_delta_usage(sse: str) -> dict[str, int]:
    for event in parse_sse_text(sse):
        if event.event == "message_delta":
            usage = event.data.get("usage")
            assert isinstance(usage, dict), event.data
            return usage
    raise AssertionError("the converter emitted no message_delta")


def _reasoning_item_added(item_id: str = "rs_1") -> dict[str, Any]:
    return {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"id": item_id, "type": "reasoning", "summary": []},
    }


def _summary_part_frames(
    text: str, *, summary_index: int = 0, item_id: str = "rs_1"
) -> list[dict[str, Any]]:
    """One summary part as the endpoint streams it: added, deltas, done, done."""
    head, _, tail = text.partition(" ")
    return [
        {
            "type": "response.reasoning_summary_part.added",
            "item_id": item_id,
            "output_index": 0,
            "summary_index": summary_index,
            "part": {"type": "summary_text", "text": ""},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": item_id,
            "output_index": 0,
            "summary_index": summary_index,
            "delta": head + " ",
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": item_id,
            "output_index": 0,
            "summary_index": summary_index,
            "delta": tail,
        },
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": item_id,
            "output_index": 0,
            "summary_index": summary_index,
            "text": text,
        },
        {
            "type": "response.reasoning_summary_part.done",
            "item_id": item_id,
            "output_index": 0,
            "summary_index": summary_index,
            "part": {"type": "summary_text", "text": text},
        },
    ]


def _reasoning_item_done(
    summaries: list[str], *, item_id: str = "rs_1"
) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": item_id,
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": s} for s in summaries],
            "encrypted_content": "gAAAAABn_opaque_blob",
        },
    }


_USAGE_WITH_REASONING: dict[str, Any] = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 900},
    "output_tokens": 120,
    "output_tokens_details": {"reasoning_tokens": 96},
    "total_tokens": 1120,
}


def _completed(usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "usage": dict(_USAGE_WITH_REASONING if usage is None else usage),
        },
    }


def test_chatgpt_oauth_reasoning_summary_delta_becomes_thinking_block():
    """The headline fix, byte for byte.

    Every frame below was tallied by `note_responses_event_shape()` before this
    change and converted by nothing.
    """
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames(_SUMMARY),
            _reasoning_item_done([_SUMMARY]),
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "Done.",
            },
            _completed(),
        ]
    )

    # Byte for byte, the two frames that did not exist before this change.
    assert (
        "event: content_block_start\n"
        'data: {"type": "content_block_start", "index": 0, '
        '"content_block": {"type": "thinking", "thinking": ""}}\n\n'
    ) in sse
    assert (
        "event: content_block_delta\n"
        'data: {"type": "content_block_delta", "index": 0, '
        '"delta": {"type": "thinking_delta", "thinking": "**Reading "}}\n\n'
    ) in sse

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert thinking_content(events) == _SUMMARY
    assert text_content(events) == "Done."
    assert _block_types(sse) == ["thinking", "text"]


def test_chatgpt_oauth_reasoning_text_delta_becomes_thinking_block():
    """Raw reasoning text, for the models that expose it, takes the same path."""
    sse = _run(
        [
            _reasoning_item_added(),
            {
                "type": "response.reasoning_text.delta",
                "item_id": "rs_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "Let me think. ",
            },
            {
                "type": "response.reasoning_text.delta",
                "item_id": "rs_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "The file is small.",
            },
            {
                "type": "response.reasoning_text.done",
                "item_id": "rs_1",
                "output_index": 0,
                "content_index": 0,
                "text": "Let me think. The file is small.",
            },
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert thinking_content(events) == "Let me think. The file is small."
    assert _block_types(sse) == ["thinking"]


def test_a_done_frame_never_repeats_text_its_deltas_already_carried():
    """Idempotence, keyed by (kind, item_id, index).

    The endpoint sends the whole part three more times after streaming it --
    `*_text.done`, `summary_part.done` and the item's own `output_item.done`.
    A converter that emitted each would quadruple every thinking block.
    """
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames(_SUMMARY),
            _reasoning_item_done([_SUMMARY]),
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert thinking_content(events) == _SUMMARY
    assert _block_types(sse) == ["thinking"]


def test_a_summary_part_delivered_only_on_its_done_frame_is_still_shown():
    """The other half of the same rule: emit once, from whichever frame has it."""
    sse = _run(
        [
            _reasoning_item_added(),
            {
                "type": "response.reasoning_summary_part.added",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "text": _SUMMARY,
            },
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert thinking_content(events) == _SUMMARY


def test_a_summary_arriving_only_on_the_finished_reasoning_item_is_shown():
    """A turn whose summary never streamed at all still reaches the client."""
    sse = _run(
        [
            _reasoning_item_added(),
            _reasoning_item_done([_SUMMARY]),
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert thinking_content(events) == _SUMMARY


def test_two_summary_parts_become_one_thinking_block_with_a_separator():
    """One block per contiguous run of reasoning, parts separated by a blank line.

    Anthropic's protocol would allow a block per part, but the ledger keeps one
    `thinking_index` per open block and clients render one thinking block per
    turn; concatenating is what Claude Code expects to read.
    """
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames("First, read the file.", summary_index=0),
            *_summary_part_frames("Then, answer the question.", summary_index=1),
            _reasoning_item_done(
                ["First, read the file.", "Then, answer the question."]
            ),
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["thinking"]
    assert (
        thinking_content(events)
        == "First, read the file.\n\nThen, answer the question."
    )


def test_chatgpt_oauth_thinking_block_closes_before_text_block_starts():
    """No thinking block may still be open when text begins.

    The ledger's `ensure_text_block()` is what guarantees it; this asserts the
    converter goes through the ledger rather than around it.
    """
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames(_SUMMARY),
            {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Hi"},
            _completed(),
        ]
    )

    names = [e.event for e in parse_sse_text(sse)]
    first_text_start = next(
        i
        for i, event in enumerate(parse_sse_text(sse))
        if event.event == "content_block_start"
        and event.data.get("content_block", {}).get("type") == "text"
    )
    assert names.index("content_block_stop") < first_text_start
    assert_anthropic_stream_contract(parse_sse_text(sse))


def test_thinking_then_text_then_tool_use_keeps_the_block_order():
    """The three-block turn, in the order Anthropic requires."""
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames(_SUMMARY),
            _reasoning_item_done([_SUMMARY]),
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "Reading it now.",
            },
            {
                "type": "response.output_item.added",
                "output_index": 2,
                "item": {"type": "function_call", "id": "call_1", "name": "bash"},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_1",
                "delta": '{"command": "ls"}',
            },
            {
                "type": "response.output_item.done",
                "output_index": 2,
                "item": {
                    "type": "function_call",
                    "id": "call_1",
                    "name": "bash",
                    "arguments": '{"command": "ls"}',
                },
            },
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["thinking", "text", "tool_use"]
    assert thinking_content(events) == _SUMMARY
    assert text_content(events) == "Reading it now."


def test_reasoning_that_resumes_after_text_opens_a_second_thinking_block():
    """Interleaving is legal as long as each block is closed before the next."""
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames("First thought.", summary_index=0),
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "Wait.",
            },
            _reasoning_item_added("rs_2"),
            *_summary_part_frames("Second thought.", summary_index=0, item_id="rs_2"),
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["thinking", "text", "thinking"]
    assert thinking_content(events) == "First thought.Second thought."


def test_encrypted_reasoning_without_a_summary_emits_no_thinking_block():
    """The honest "it thought, and returned none of it" case.

    A reasoning item carrying only `encrypted_content` has nothing the client
    may read. An empty thinking block would claim otherwise, so the stream must
    carry none -- `thinking_chars` stays 0 while the upstream's own
    `reasoning_tokens` still count toward the turn's output tokens.
    """
    sse = _run(
        [
            _reasoning_item_added(),
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "gAAAAABn_opaque_blob",
                },
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "Done.",
            },
            _completed(),
        ]
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["text"]
    assert thinking_content(events) == ""
    assert "thinking" not in sse
    # The endpoint billed 96 reasoning tokens inside the 120 output tokens; the
    # count survives even though none of the text did.
    assert _message_delta_usage(sse)["output_tokens"] == 120
    assert _USAGE_WITH_REASONING["output_tokens_details"]["reasoning_tokens"] == 96


def test_a_client_that_turned_reasoning_off_gets_no_thinking_block():
    """`ReasoningPolicy.output_enabled` is honoured, as it is on openai_chat."""
    sse = _run(
        [
            _reasoning_item_added(),
            *_summary_part_frames(_SUMMARY),
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "delta": "Done.",
            },
            _completed(),
        ],
        output_reasoning=False,
    )

    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["text"]
    assert thinking_content(events) == ""


def test_the_stream_response_converter_is_given_the_clients_reasoning_intent():
    """The policy has to reach the converter, or the gate above is decoration."""
    import inspect

    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    source = inspect.getsource(provider_module.ChatGPTOAuthProvider.stream_response)
    assert "output_reasoning=reasoning.output_enabled" in source


@pytest.mark.asyncio
async def test_thinking_reaches_the_request_log_row(tmp_path):
    """End to end: a real-shaped summary turn becomes a row with chars > 0.

    The counterpart of the 6.68.2 cache test, and the assertion the audit's
    headline number is about: this row is what the dashboard reads.
    """
    from collections.abc import AsyncIterator

    from my_claude_code.api.request_capture import RequestCapture
    from my_claude_code.core.request_log import RequestLogStore

    ledger = AnthropicStreamLedger("msg_1", "gpt-5.6-luna", input_tokens=0)
    converter = ChatGPTOAuthStreamConverter(ledger)
    frames = [ledger.message_start()]
    for frame in [
        _reasoning_item_added(),
        *_summary_part_frames(_SUMMARY),
        _reasoning_item_done([_SUMMARY]),
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Done."},
        _completed(),
    ]:
        frames.extend(converter.feed(frame))
    frames.extend(converter.finish())

    store = RequestLogStore(tmp_path / "requests.db")
    capture = RequestCapture(
        store,
        request_id="req_thinking",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="gpt-5.6-luna",
        input_text="hello",
        params=None,
    )

    async def body() -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    async for _ in capture.wrap(body()):
        pass
    store.close()

    row = store.get_request("req_thinking")
    assert row is not None
    assert row["thinking_chars"] == len(_SUMMARY)


@pytest.mark.asyncio
async def test_encrypted_only_reasoning_leaves_the_row_at_zero(tmp_path):
    """And the honest zero stays a zero, not an empty block's worth of noise."""
    from collections.abc import AsyncIterator

    from my_claude_code.api.request_capture import RequestCapture
    from my_claude_code.core.request_log import RequestLogStore

    ledger = AnthropicStreamLedger("msg_1", "gpt-5.6-luna", input_tokens=0)
    converter = ChatGPTOAuthStreamConverter(ledger)
    frames = [ledger.message_start()]
    for frame in [
        _reasoning_item_added(),
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "gAAAAABn_opaque_blob",
            },
        },
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Done."},
        _completed(),
    ]:
        frames.extend(converter.feed(frame))
    frames.extend(converter.finish())

    store = RequestLogStore(tmp_path / "requests.db")
    capture = RequestCapture(
        store,
        request_id="req_encrypted",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="gpt-5.6-luna",
        input_text="hello",
        params=None,
    )

    async def body() -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    async for _ in capture.wrap(body()):
        pass
    store.close()

    row = store.get_request("req_encrypted")
    assert row is not None
    assert row["thinking_chars"] == 0
    assert row["tokens_out"] == 120


def test_the_diagnostics_tally_still_sees_every_reasoning_event():
    """`note_responses_event_shape()` is unchanged and still the shape source."""
    from my_claude_code.core.wire_capture import ResponseShape
    from my_claude_code.providers.chatgpt_oauth.streaming import (
        note_responses_event_shape,
    )

    shape = ResponseShape(started_at=time.monotonic())
    for frame in _summary_part_frames(_SUMMARY):
        note_responses_event_shape(shape, frame)

    payload = json.loads(json.dumps(shape.payload(), default=str))
    assert "reasoning" in payload["fields"]
