"""Ending a committed Anthropic stream as a valid message."""

import json

from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
    text_content,
)
from my_claude_code.core.anthropic.streaming.emitter import format_sse_event
from my_claude_code.core.anthropic.streaming.truncation import (
    TRUNCATED_STOP_REASON,
    CommittedStreamTracker,
)

_MESSAGE_START = format_sse_event(
    "message_start",
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "big",
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 41, "output_tokens": 1},
        },
    },
)


def _block_start(index: int, block: dict[str, object]) -> str:
    return format_sse_event(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _text(index: int, text: str) -> str:
    return format_sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _thinking(index: int, thinking: str) -> str:
    return format_sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": thinking},
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


def _tracker(*chunks: str) -> CommittedStreamTracker:
    tracker = CommittedStreamTracker()
    for chunk in chunks:
        tracker.observe(chunk)
    return tracker


def _joined(sent: tuple[str, ...], truncation_frames: tuple[str, ...]) -> str:
    return "".join(sent) + "".join(truncation_frames)


def test_a_text_stall_closes_the_open_block_and_ends_the_message() -> None:
    """State (a): the reader keeps the text, and the message is complete."""
    sent = (
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "The sea is vast and restless. It carries"),
    )
    tracker = _tracker(*sent)

    assert tracker.closable
    truncation = tracker.close(reason="timeout")

    events = parse_sse_text(_joined(sent, truncation.frames))
    assert_anthropic_stream_contract(events)
    assert [event.event for event in events][-3:] == [
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert text_content(events) == "The sea is vast and restless. It carries"


def test_a_thinking_stall_closes_the_thinking_block() -> None:
    """State (b): the thinking already shown stays, and the block is closed."""
    sent = (
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "Working on it."),
        _block_stop(0),
        _block_start(1, {"type": "thinking", "thinking": ""}),
        _thinking(1, "first I should"),
    )
    tracker = _tracker(*sent)

    truncation = tracker.close(reason="timeout")
    events = parse_sse_text(_joined(sent, truncation.frames))

    assert_anthropic_stream_contract(events)
    stops = [
        event.data["index"] for event in events if event.event == "content_block_stop"
    ]
    assert stops == [0, 1]


def test_a_stall_between_blocks_needs_no_closing_frame() -> None:
    """State (d): nothing is open, so only the message has to be ended."""
    sent = (
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "Done with the first part."),
        _block_stop(0),
    )
    tracker = _tracker(*sent)

    truncation = tracker.close(reason="upstream")

    assert [parse_sse_text(frame)[0].event for frame in truncation.frames] == [
        "message_delta",
        "message_stop",
    ]
    assert_anthropic_stream_contract(parse_sse_text(_joined(sent, truncation.frames)))


def test_a_half_written_tool_call_cannot_be_ended_honestly() -> None:
    """State (c): closing it would hand Claude Code a tool call to run."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "Let me check that file."),
        _block_stop(0),
        _block_start(1, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
        _tool_json(1, '{"command": "ls -'),
    )

    assert tracker.incomplete_tool_use
    assert not tracker.closable

    abandoned = tracker.abandoned(reason="incomplete_tool_use")
    assert abandoned.frames == ()
    assert abandoned.ended_cleanly is False
    assert abandoned.stop_reason_sent is None
    assert abandoned.chars == len("Let me check that file.")


def test_a_tool_call_that_finished_its_arguments_is_still_closable() -> None:
    """The block is complete; only the message around it is not."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "Checking."),
        _block_stop(0),
        _block_start(1, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
        _tool_json(1, '{"command":'),
        _tool_json(1, ' "ls -la"}'),
    )

    assert not tracker.incomplete_tool_use
    assert tracker.closable


def test_a_tool_block_that_emitted_no_arguments_is_not_closable() -> None:
    """An empty argument stream parses as nothing, not as ``{}``."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "Checking."),
        _block_stop(0),
        _block_start(1, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
    )

    assert tracker.incomplete_tool_use


def test_a_stream_with_no_content_block_is_not_closable() -> None:
    """The 5.41.0 dead-stream shape: an envelope is not a message."""
    tracker = _tracker("event: a\n\n")

    assert tracker.blocks == 0
    assert not tracker.closable


def test_a_message_that_already_chose_its_stop_reason_is_not_closable() -> None:
    """A second ``message_delta`` would overwrite an ending the model chose."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "All done."),
        _block_stop(0),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"input_tokens": 41, "output_tokens": 3},
            },
        ),
    )

    assert tracker.stop_reason_seen
    assert not tracker.closable


def test_the_stop_reason_sent_is_the_protocols_cut_short_value() -> None:
    """``end_turn`` would tell the client a half-written plan was finished."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "Step 1. Step 2."),
    )

    truncation = tracker.close(reason="timeout")
    delta = parse_sse_text(truncation.frames[-2])[0]

    assert TRUNCATED_STOP_REASON == "max_tokens"
    assert delta.data["delta"]["stop_reason"] == "max_tokens"
    assert truncation.stop_reason_sent == "max_tokens"
    assert truncation.reason == "timeout"


def test_usage_reports_what_the_upstream_counted_when_it_said_so() -> None:
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "hello"),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": None, "stop_sequence": None},
                "usage": {"input_tokens": 41, "output_tokens": 128},
            },
        ),
    )

    usage = parse_sse_text(tracker.close(reason="timeout").frames[-2])[0].data["usage"]

    assert usage == {"input_tokens": 41, "output_tokens": 128}


def test_usage_is_estimated_when_the_stream_died_before_its_own_count() -> None:
    """Reporting zero for an answer on the reader's screen is the worse lie."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "The sea is vast and restless. " * 20),
    )

    usage = parse_sse_text(tracker.close(reason="timeout").frames[-2])[0].data["usage"]

    assert usage["input_tokens"] == 41
    assert usage["output_tokens"] > 0


def test_a_frame_split_across_two_chunks_is_still_understood() -> None:
    """A provider may split one SSE frame; half a frame parses as nothing."""
    frame = _text(0, "hello")
    tracker = CommittedStreamTracker()
    tracker.observe(_MESSAGE_START)
    tracker.observe(_block_start(0, {"type": "text", "text": ""}))
    tracker.observe(frame[:20])
    tracker.observe(frame[20:])

    assert tracker.chars == len("hello")
    assert tracker.closable


def test_the_char_count_is_what_the_reader_actually_saw() -> None:
    """Eager text on the start frame counts; thinking and tool JSON do not."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "thinking", "thinking": ""}),
        _thinking(0, "hidden reasoning"),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": "eager"}),
        _text(1, "-and-streamed"),
    )

    assert tracker.chars == len("eager-and-streamed")
    assert tracker.blocks == 2
    assert tracker.close(reason="timeout").as_params() == {
        "chars": len("eager-and-streamed"),
        "blocks": 2,
        "reason": "timeout",
        "stop_reason_sent": "max_tokens",
        "ended_cleanly": True,
    }


def test_the_closing_frames_are_serialised_as_anthropic_sse() -> None:
    """Every frame is one ``event:``/``data:`` pair the client can parse."""
    tracker = _tracker(
        _MESSAGE_START,
        _block_start(0, {"type": "text", "text": ""}),
        _text(0, "partial"),
    )

    for frame in tracker.close(reason="timeout").frames:
        assert frame.startswith("event: ")
        assert frame.endswith("\n\n")
        payload = frame.split("data: ", 1)[1].strip()
        assert isinstance(json.loads(payload), dict)
