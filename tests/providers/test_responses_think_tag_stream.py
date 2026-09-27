"""Literal ``<think>`` tags on the Responses surface become a thinking block.

Belt and braces for 7.58.1. The builder no longer shows the model its past
reasoning as tags, which removes the cause; this pins the other half -- a
model that still writes ``<think>...</think>`` into its answer text reaches
Claude Code as a thinking block, not as literal tags.

The parser is the Chat door's own :class:`ThinkTagParser`, not a copy, so the
same text parses the same way on either door -- including a genuine answer
that happens to spell the tag, which both doors treat alike (pinned below
against the Chat door's own loop rather than asserted in the abstract).

The regression at the end replays the shape the request log recorded for
Zen's ``muse-spark-1.3``: 109 answers carried a tag, all 107 with a recorded
shape were ``content`` only -- no reasoning item text, no function call -- and
94 of them were nothing but ``<think>...</think>``. The frame envelope is the
real one Zen sent (``tests/contracts/opencode_reference_responses_stream.json``);
the text is neutral wording in the logged structure, because the user's own
session text does not belong in a public repository.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core.anthropic.openai_tool_names import OpenAIToolNameCodec
from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
    text_content,
    thinking_content,
)
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.anthropic.thinking import ContentType, ThinkTagParser
from my_claude_code.providers.openai_responses import ResponsesStreamConverter

REFERENCE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "opencode_reference_responses_stream.json"
    ).read_text(encoding="utf-8")
)


def _text_frame(delta: str, item_id: str = "msg_1") -> dict[str, Any]:
    return {"type": "response.output_text.delta", "item_id": item_id, "delta": delta}


def _message_done(item_id: str = "msg_1") -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {"id": item_id, "type": "message", "status": "completed"},
    }


def _function_call(
    call_id: str = "fc_1", name: str = "bash", arguments: str = '{"command": "ls"}'
) -> list[dict[str, Any]]:
    return [
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "id": call_id, "name": name},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": call_id,
            "delta": arguments,
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": call_id,
                "name": name,
                "arguments": arguments,
            },
        },
    ]


_COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}


def _run(frames: list[dict[str, Any]], **kwargs: Any) -> str:
    ledger = AnthropicStreamLedger("msg_1", "muse-spark-1.3", input_tokens=0)
    converter = ResponsesStreamConverter(ledger, **kwargs)
    out = [ledger.message_start()]
    for frame in frames:
        out.extend(converter.feed(frame))
    out.extend(converter.finish())
    return "".join(out)


def _block_types(sse: str) -> list[str]:
    return [
        str(event.data.get("content_block", {}).get("type", ""))
        for event in parse_sse_text(sse)
        if event.event == "content_block_start"
    ]


def _text_deltas(sse: str) -> list[str]:
    return [
        event.data["delta"]["text"]
        for event in parse_sse_text(sse)
        if event.event == "content_block_delta"
        and event.data.get("delta", {}).get("type") == "text_delta"
    ]


def _chat_door(deltas: list[str], *, output_reasoning: bool = True) -> tuple[str, str]:
    """The Chat Completions door's own loop over ``delta.content``, verbatim in
    effect: feed every delta, keep THINKING only when reasoning is shown, flush
    at the end (``openai_chat/provider.py``)."""

    parser = ThinkTagParser()
    thinking = ""
    text = ""
    parts = [part for delta in deltas for part in parser.feed(delta)]
    remaining = parser.flush()
    if remaining is not None:
        parts.append(remaining)
    for part in parts:
        if part.type is ContentType.THINKING:
            if output_reasoning:
                thinking += part.content
        else:
            text += part.content
    return thinking, text


def test_tags_in_the_answer_become_a_thinking_block() -> None:
    sse = _run(
        [
            _text_frame("<think>\nCheck the constraint first.\n</think>\n\n"),
            _text_frame("The constraint is confirmed."),
            _message_done(),
            _COMPLETED,
        ]
    )
    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["thinking", "text"]
    assert thinking_content(events) == "\nCheck the constraint first.\n"
    assert text_content(events) == "\n\nThe constraint is confirmed."
    assert "<think>" not in sse and "</think>" not in sse


@pytest.mark.parametrize(
    "deltas",
    [
        ["<", "think>abc</", "think>answer"],
        ["<th", "ink>a", "bc</thi", "nk>ans", "wer"],
        ["<think>abc</think>answer"],
        ["<think>", "abc", "</think>", "answer"],
        list("<think>abc</think>answer"),
    ],
    ids=["split-at-bracket", "split-mid-tag", "one-delta", "tag-per-delta", "per-char"],
)
def test_a_tag_split_across_deltas_parses_the_same(deltas: list[str]) -> None:
    frames = [_text_frame(delta) for delta in deltas] + [_message_done(), _COMPLETED]
    sse = _run(frames)
    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert thinking_content(events) == "abc"
    assert text_content(events) == "answer"
    assert _block_types(sse) == ["thinking", "text"]


def test_an_answer_with_no_tags_is_emitted_delta_for_delta() -> None:
    """No tag, no change: one text block, every delta exactly as it came."""

    deltas = ["Here is ", "the answer: 3 ", "is less than 4, and a -> b.", "\n"]
    sse = _run([_text_frame(d) for d in deltas] + [_message_done(), _COMPLETED])
    assert _block_types(sse) == ["text"]
    assert _text_deltas(sse) == deltas


def test_a_held_bracket_that_was_not_a_tag_is_still_delivered() -> None:
    """``<`` could start ``<think>``, so it waits one delta; nothing is lost."""

    sse = _run([_text_frame("a <"), _text_frame("b"), _message_done(), _COMPLETED])
    assert text_content(parse_sse_text(sse)) == "a <b"
    held_to_the_end = _run([_text_frame("x <"), _COMPLETED])
    assert text_content(parse_sse_text(held_to_the_end)) == "x <"
    no_completed_frame = _run([_text_frame("y <")])
    assert text_content(parse_sse_text(no_completed_frame)) == "y <"


@pytest.mark.parametrize(
    "deltas",
    [
        ["<think>plan</think>", "do it"],
        ["intro ", "<think>mid</think>", " outro"],
        ["Use the `<think>` tag to wrap reasoning."],
        ["An orphan </think> close tag."],
        ["<think>never closed"],
        ["a < b and c <thin", "king> d"],
    ],
    ids=[
        "leading",
        "middle",
        "literal-open-tag",
        "orphan-close",
        "unclosed",
        "near-miss",
    ],
)
def test_the_responses_door_parses_exactly_as_the_chat_door(deltas: list[str]) -> None:
    """Same parser, same result -- for tags, for near misses, and for a genuine
    answer that spells the tag (which both doors read as the start of
    reasoning; this release does not make that worse on either)."""

    for output_reasoning in (True, False):
        sse = _run(
            [_text_frame(d) for d in deltas] + [_message_done(), _COMPLETED],
            output_reasoning=output_reasoning,
        )
        events = parse_sse_text(sse)
        assert_anthropic_stream_contract(events)
        thinking, text = _chat_door(deltas, output_reasoning=output_reasoning)
        assert thinking_content(events) == thinking
        # Both doors put one space in a message that would otherwise carry no
        # block at all; that is the only difference from the parser's text.
        assert text_content(events) == (text or " " if not thinking else text)


def test_an_all_tagged_answer_with_reasoning_hidden_still_carries_a_block() -> None:
    """Answer text always produced a content block before the parser; it
    still does, as the single space the Chat door emits in the same case."""

    sse = _run(
        [_text_frame("<think>only a thought</think>"), _message_done(), _COMPLETED],
        output_reasoning=False,
    )
    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["text"]
    assert text_content(events) == " "
    assert "only a thought" not in sse
    # A stream that sent no answer text at all is untouched by that rule.
    assert _block_types(_run([_COMPLETED], output_reasoning=False)) == []


def test_the_literal_tag_case_is_pinned_so_a_change_is_seen() -> None:
    thinking, text = _chat_door(["Use the `<think>` tag to wrap reasoning."])
    assert (thinking, text) == ("` tag to wrap reasoning.", "Use the `")


def test_hidden_reasoning_drops_the_tagged_part_and_keeps_the_answer() -> None:
    sse = _run(
        [_text_frame("<think>secret</think>visible"), _message_done(), _COMPLETED],
        output_reasoning=False,
    )
    events = parse_sse_text(sse)
    assert thinking_content(events) == ""
    assert text_content(events) == "visible"
    assert "secret" not in sse


def test_a_function_call_after_the_closing_tag_still_opens_a_tool_use_block() -> None:
    """The tool call is never swallowed into thinking: it is its own item."""

    codec = OpenAIToolNameCodec.from_names(
        ["Bash"], max_length=64, catalogue={"Bash": "bash"}
    )
    sse = _run(
        [
            _text_frame("<think>\nList the files.\n</think>\n\nListing them now."),
            _message_done(),
            *_function_call(name="bash"),
            _COMPLETED,
        ],
        tool_names=codec,
    )
    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["thinking", "text", "tool_use"]
    starts = [e for e in events if e.event == "content_block_start"]
    assert starts[2].data["content_block"]["name"] == "Bash"
    assert text_content(events) == "\n\nListing them now."


def test_held_text_is_flushed_before_the_tool_block_not_after_it() -> None:
    sse = _run([_text_frame("Running <"), *_function_call(), _COMPLETED])
    assert _block_types(sse) == ["text", "tool_use"]
    assert text_content(parse_sse_text(sse)) == "Running <"


# --------------------------------------------------------------------------
# Regression: muse-spark's recorded shape, in the real Zen frame envelope
# --------------------------------------------------------------------------


def _muse_frames(
    deltas: list[str], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The real Zen capture with its one ``ok`` delta replaced by ``deltas``."""

    frames: list[dict[str, Any]] = []
    for frame in copy.deepcopy(REFERENCE["frames"]):
        if frame["type"] == "response.output_text.delta":
            frames.extend({**frame, "delta": delta} for delta in deltas)
            continue
        if frame["type"] in ("response.content_part.done", "response.output_item.done"):
            text = "".join(deltas)
            if frame["type"] == "response.content_part.done":
                frame["part"]["text"] = text
            elif frame["item"]["type"] == "message":
                frame["item"]["content"][0]["text"] = text
        frames.append(frame)
    return frames + extra + [_COMPLETED]


#: 94 of 109 logged answers: the whole answer is a tagged thought.
THINK_ONLY = ["<think>", "\nConfirming the live ", "constraints first.\n", "</think>"]
#: 15 of 109: a tagged thought, then a sentence announcing the next step.
THINK_THEN_NARRATION = [
    "<think>\nThe log shows ports bound",
    " but no banner yet.\n</think>",
    "\n\nPorts are bound; verifying",
    " the backend next.",
]


def test_a_think_only_muse_answer_reaches_claude_code_as_thinking() -> None:
    sse = _run(_muse_frames(THINK_ONLY, []))
    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert "<think>" not in text_content(events)
    assert _block_types(sse) == ["thinking"]
    assert thinking_content(events) == "\nConfirming the live constraints first.\n"


def test_a_muse_answer_that_narrates_then_calls_gets_its_tool_use_block() -> None:
    """The coordinator's case: text after the closing tag, then a real call."""

    codec = OpenAIToolNameCodec.from_names(
        ["Bash"], max_length=64, catalogue={"Bash": "bash"}
    )
    sse = _run(
        _muse_frames(THINK_THEN_NARRATION, _function_call(name="bash")),
        tool_names=codec,
    )
    events = parse_sse_text(sse)
    assert_anthropic_stream_contract(events)
    assert _block_types(sse) == ["thinking", "text", "tool_use"]
    assert text_content(events) == "\n\nPorts are bound; verifying the backend next."
    tool = [e for e in events if e.event == "content_block_start"][2]
    assert tool.data["content_block"]["name"] == "Bash"
    assert "<think>" not in sse
