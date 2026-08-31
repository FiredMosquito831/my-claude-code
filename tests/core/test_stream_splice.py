"""What a message spliced from two models is allowed to look like.

Everything here drives the joined byte stream through
``assert_anthropic_stream_contract`` -- the same validator every other stream
test in the tree relies on -- because the whole risk of continuing a message on
a second model is protocol-shaped: reused block indices, two envelopes, a block
left open, two endings. The one test that is *not* protocol-shaped is the
restart test, and that is the one the feature can actually get wrong in a way
the reader would see.
"""

import json

from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
    text_content,
)
from my_claude_code.core.anthropic.streaming.splice import (
    ContinuationSplicer,
    SpliceState,
    close_frames,
    freeze_stream,
    looks_like_a_restart,
    strict_continuation_suffix,
)
from my_claude_code.core.anthropic.streaming.truncation import CommittedStreamTracker
from my_claude_code.core.anthropic.tokens import count_text_tokens


def _frame(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _message_start(input_tokens: int = 41) -> str:
    return _frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "first",
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
    )


def _text_block(index: int) -> str:
    return _frame(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    )


def _text(index: int, text: str) -> str:
    return _frame(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _thinking_block(index: int) -> str:
    return _frame(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": ""},
        },
    )


def _thinking(index: int, text: str) -> str:
    return _frame(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    )


def _tool_block(index: int, name: str = "Bash") -> str:
    return _frame(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": "t1",
                "name": name,
                "input": {},
            },
        },
    )


def _tool_json(index: int, fragment: str) -> str:
    return _frame(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": fragment},
        },
    )


def _block_stop(index: int) -> str:
    return _frame("content_block_stop", {"type": "content_block_stop", "index": index})


def _message_delta(stop_reason: str = "end_turn", output_tokens: int = 7) -> str:
    return _frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": 41, "output_tokens": output_tokens},
        },
    )


def _message_stop() -> str:
    return _frame("message_stop", {"type": "message_stop"})


def _tracker(*frames: str) -> CommittedStreamTracker:
    tracker = CommittedStreamTracker()
    for frame in frames:
        tracker.observe(frame)
    return tracker


def _splice(
    first: tuple[str, ...], second: tuple[str, ...], *, decision_buffer_chars: int = 512
) -> tuple[list[str], ContinuationSplicer]:
    """Run one whole two-model message and return what the client received."""
    tracker = _tracker(*first)
    sent = list(first)
    freeze = freeze_stream(tracker)
    keep_text_open = freeze.state is SpliceState.IN_TEXT
    for frame in close_frames(freeze, keep_text_open=keep_text_open):
        tracker.observe(frame)
        sent.append(frame)
    splicer = ContinuationSplicer(
        freeze,
        keep_text_open=keep_text_open,
        decision_buffer_chars=decision_buffer_chars,
    )
    for chunk in second:
        for frame in splicer.rewrite(chunk):
            tracker.observe(frame)
            sent.append(frame)
    for frame in splicer.finish():
        tracker.observe(frame)
        sent.append(frame)
    return sent, splicer


_FIRST_TEXT_STALL = (
    _message_start(),
    _text_block(0),
    _text(0, "The sea is vast and restless. It carries"),
)


def test_the_spliced_stream_is_a_valid_anthropic_event_sequence() -> None:
    sent, _ = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, " salt and secrets."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )

    assert_anthropic_stream_contract(parse_sse_text("".join(sent)))


def test_a_text_stall_keeps_one_open_text_block_across_the_seam() -> None:
    """No seam: the reader sees one continuous block, not two adjacent ones."""
    sent, splicer = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, " salt and secrets."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    starts = [e for e in events if e.event == "content_block_start"]
    assert len(starts) == 1
    assert text_content(events) == (
        "The sea is vast and restless. It carries salt and secrets."
    )
    assert splicer.usable


def test_a_continuation_block_index_never_reuses_one_the_client_has_seen() -> None:
    """Indices are single-use for the whole message, across both models."""
    sent, _ = _splice(
        (
            _message_start(),
            _text_block(0),
            _text(0, "a first answer that got cut off"),
            _block_stop(0),
            _text_block(1),
            _text(1, "and a second paragraph"),
        ),
        (
            _message_start(),
            _text_block(0),
            _text(0, " that finishes it."),
            _block_stop(0),
            _text_block(1),
            _text(1, "a genuinely new block"),
            _block_stop(1),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    indexes = [e.data["index"] for e in events if e.event == "content_block_start"]
    assert indexes == [0, 1, 2]


def test_the_continuation_message_start_is_dropped() -> None:
    sent, _ = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(input_tokens=999),
            _text_block(0),
            _text(0, " salt and secrets."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    starts = [e for e in events if e.event == "message_start"]
    assert len(starts) == 1
    assert starts[0].data["message"]["usage"]["input_tokens"] == 41


def test_exactly_one_message_delta_and_message_stop_survive() -> None:
    sent, _ = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, " salt."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )
    names = [e.event for e in parse_sse_text("".join(sent))]

    assert names.count("message_delta") == 1
    assert names.count("message_stop") == 1
    assert names[-2:] == ["message_delta", "message_stop"]


def test_a_thinking_stall_closes_the_thinking_block_before_the_seam() -> None:
    """A foreign model cannot sign someone else's thinking, so it never tries."""
    sent, _ = _splice(
        (
            _message_start(),
            _text_block(0),
            _text(0, "here is the plan so far"),
            _block_stop(0),
            _thinking_block(1),
            _thinking(1, "weighing the options"),
        ),
        (
            _message_start(),
            _thinking_block(0),
            _thinking(0, "thoughts that are not this message's"),
            _block_stop(0),
            _text_block(1),
            _text(1, "and here is the rest."),
            _block_stop(1),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    # The first model's thinking block is closed, and no second one is opened.
    thinking_starts = [
        e
        for e in events
        if e.event == "content_block_start"
        and e.data["content_block"]["type"] == "thinking"
    ]
    assert len(thinking_starts) == 1
    assert "thoughts that are not this message's" not in "".join(sent)
    assert text_content(events) == "here is the plan so farand here is the rest."


def test_a_stall_between_blocks_opens_the_continuation_at_a_fresh_index() -> None:
    sent, _ = _splice(
        (
            _message_start(),
            _text_block(0),
            _text(0, "a complete first paragraph."),
            _block_stop(0),
        ),
        (
            _message_start(),
            _text_block(0),
            _text(0, "A second one, by another model."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    assert [e.data["index"] for e in events if e.event == "content_block_start"] == [
        0,
        1,
    ]


def test_usage_is_the_sum_across_both_models() -> None:
    """One message, two models, one bill.

    A stalled model almost never sends its own ``message_delta`` -- it dies
    before one -- so its half is the same estimate ``truncation`` would have
    published, and the continuation's reported count is added to it. Reporting
    only the second model's number would price an answer the reader can see on
    screen at a fraction of what it cost.
    """
    sent, _ = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, " salt."),
            _block_stop(0),
            _message_delta(output_tokens=13),
            _message_stop(),
        ),
    )
    deltas = [e for e in parse_sse_text("".join(sent)) if e.event == "message_delta"]
    stalled_half = count_text_tokens("The sea is vast and restless. It carries")

    assert len(deltas) == 1
    assert stalled_half > 0
    assert deltas[0].data["usage"]["output_tokens"] == stalled_half + 13
    assert deltas[0].data["usage"]["input_tokens"] == 41
    assert deltas[0].data["delta"]["stop_reason"] == "end_turn"


def test_a_repeated_prefix_is_dropped_from_the_continuation() -> None:
    """A model that repeats the tail before continuing is not shown twice."""
    sent, splicer = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, "It carries salt and secrets."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    assert text_content(events) == (
        "The sea is vast and restless. It carries salt and secrets."
    )
    assert splicer.dropped_overlap_chars == len("It carries")


def test_a_restarted_answer_is_rejected_rather_than_spliced() -> None:
    """The measured failure: the model writes the opening again, verbatim."""
    sent, splicer = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, "\n\nThe sea is vast and restless. It carries ships and salt."),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    assert splicer.rejected
    assert not splicer.usable
    # Nothing the restarting model said reached the reader, and the message is
    # ended honestly rather than claimed complete.
    assert text_content(events) == "The sea is vast and restless. It carries"
    delta = next(e for e in events if e.event == "message_delta")
    assert delta.data["delta"]["stop_reason"] == "max_tokens"


def test_a_continuation_that_says_nothing_still_ends_the_message() -> None:
    """The dominant measured failure of the nudge is silence, not garbling."""
    sent, splicer = _splice(
        _FIRST_TEXT_STALL,
        (_message_start(), _message_delta(), _message_stop()),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    assert not splicer.usable
    assert text_content(events) == "The sea is vast and restless. It carries"
    assert [e.event for e in events][-3:] == [
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


def test_a_resumed_turn_may_still_call_a_tool() -> None:
    """A continuation's tool call is spliced through, never dropped or emptied."""
    sent, splicer = _splice(
        _FIRST_TEXT_STALL,
        (
            _message_start(),
            _text_block(0),
            _text(0, " salt. Let me check."),
            _block_stop(0),
            _tool_block(1),
            _tool_json(1, '{"command": "ls -la"}'),
            _block_stop(1),
            _message_delta(stop_reason="tool_use"),
            _message_stop(),
        ),
    )
    events = parse_sse_text("".join(sent))

    assert_anthropic_stream_contract(events)
    tool_starts = [
        e
        for e in events
        if e.event == "content_block_start"
        and e.data["content_block"]["type"] == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0].data["index"] == 1
    arguments = "".join(
        e.data["delta"]["partial_json"]
        for e in events
        if e.event == "content_block_delta"
        and e.data["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(arguments) == {"command": "ls -la"}
    delta = next(e for e in events if e.event == "message_delta")
    assert delta.data["delta"]["stop_reason"] == "tool_use"
    assert splicer.usable


def test_a_half_written_tool_call_is_not_a_state_a_freeze_calls_resumable() -> None:
    tracker = _tracker(
        _message_start(),
        _text_block(0),
        _text(0, "running it now"),
        _block_stop(0),
        _tool_block(1),
        _tool_json(1, '{"command": "ls -'),
    )

    assert freeze_stream(tracker).state is SpliceState.IN_TOOL_USE
    assert tracker.incomplete_tool_use
    assert not tracker.closable


def test_the_freeze_reads_only_what_the_reader_was_shown() -> None:
    tracker = _tracker(
        _message_start(),
        _thinking_block(0),
        _thinking(0, "hidden reasoning"),
        _block_stop(0),
        _text_block(1),
        _text(1, "visible words"),
    )
    freeze = freeze_stream(tracker)

    assert freeze.prefix_text == "visible words"
    assert freeze.prefix_thinking == "hidden reasoning"
    assert freeze.state is SpliceState.IN_TEXT
    assert freeze.open_text_index == 1
    assert freeze.next_index == 2
    assert freeze.input_tokens == 41


class TestStrictContinuationSuffix:
    """The one judgement in the module that is not mechanical."""

    def test_a_clean_continuation_is_taken_whole(self) -> None:
        assert strict_continuation_suffix("1\n2\n3\n4\n5", "6\n7\n8\n9\n10") == (
            "6\n7\n8\n9\n10"
        )

    def test_a_mid_sentence_continuation_is_taken_whole(self) -> None:
        assert (
            strict_continuation_suffix(
                "The sea is vast and restless. It carries",
                " salt and secrets. Moonlight pulls the tide.",
            )
            == " salt and secrets. Moonlight pulls the tide."
        )

    def test_a_repeated_tail_is_stripped(self) -> None:
        assert (
            strict_continuation_suffix(
                "the quick brown fox jumps over", " jumps over the lazy dog"
            )
            == " the lazy dog"
        )

    def test_a_verbatim_restart_is_rejected(self) -> None:
        assert (
            strict_continuation_suffix(
                "The sea is vast and restless. It carries",
                "The sea is vast and restless. It carries ships.",
            )
            is None
        )

    def test_a_restart_padded_with_whitespace_is_still_a_restart(self) -> None:
        assert (
            strict_continuation_suffix(
                "The sea is vast and restless. It carries",
                "\n\nThe sea is vast and restless. It carries ships.",
            )
            is None
        )

    def test_a_short_restart_is_rejected_where_the_shipped_helper_accepts_it(
        self,
    ) -> None:
        """The sharp edge this wrapper exists for.

        ``recovery.continuation_suffix`` returns the whole candidate when it is
        shorter than ``max(200, len(existing) // 2)``, which turns a short
        restart into a duplicated opening. Measured, that restart is the most
        common thing a model does with a bare prefill.
        """
        from my_claude_code.core.anthropic.streaming.recovery import (
            continuation_suffix,
        )

        existing = "1\n2\n3\n4\n5"
        restart = "1\n2\n3\n4\n5\n6\n7\n8\n9\n10"

        assert continuation_suffix(existing, restart) == "\n6\n7\n8\n9\n10"
        assert strict_continuation_suffix(existing, restart) is None

    def test_an_empty_continuation_is_not_a_restart(self) -> None:
        assert strict_continuation_suffix("anything at all", "") == ""

    def test_a_jump_back_into_the_middle_is_rejected(self) -> None:
        """A repeat of an earlier passage that then diverges is not a suffix.

        The overlap loop only forgives a repetition that runs to the *end* of
        what was already sent; this one restarts from the middle and then says
        something else, which would put the same sentence on screen twice.
        """
        existing = "Intro sentence here. Alpha beta gamma delta epsilon. Outro."
        assert (
            strict_continuation_suffix(
                existing, "Alpha beta gamma delta epsilon zeta eta."
            )
            is None
        )

    def test_a_repeat_that_runs_to_the_end_is_stripped_not_rejected(self) -> None:
        existing = "Step one is preparation. Step two is execution. Step three"
        assert (
            strict_continuation_suffix(
                existing, "Step two is execution. Step three is review."
            )
            == " is review."
        )

    def test_a_short_opening_is_never_judged_a_restart(self) -> None:
        assert not looks_like_a_restart("abc", "abcdef")
