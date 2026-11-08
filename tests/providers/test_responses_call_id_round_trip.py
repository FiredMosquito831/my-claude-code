"""A Responses ``function_call`` carries two ids, and the client sees the right one.

Upstream, the item carries:

* ``id`` -- ``fc_…`` -- which names the *output item*, and is what
  ``response.function_call_arguments.delta`` and ``response.output_item.done``
  are keyed by;
* ``call_id`` -- ``call_…`` -- which is what pairs the call with its
  ``function_call_output``.

The protocol pairs by ``call_id``. Until 8.14.3 MCC handed the client the item
``id`` on the Claude Code path (``streaming.py``), so ``tool_result.tool_use_id``
came back as ``fc_…`` and every replayed ``function_call.call_id`` *and*
``function_call_output.call_id`` was an id the backend never minted as a call
id. The upstream then saw an unanswered ``function_call`` beside an orphaned
output. Nothing in the log could show it: the wire recorder keeps only ``type``
and ``name`` on a ``function_call`` item, so both ids are absent from every
stored body.

The four fixtures below are the contract:

* **A** ``id != call_id`` -- the client must see the ``call_id``, and the next
  request must pair by it.
* **B** ``id == call_id`` -- byte-identical to what 8.14.2 sent, which is why
  the common case cannot move.
* **C** a multi-call turn -- each block gets its own ``call_id`` and each
  output pairs with the right call.
* **D** no ``call_id`` -- falls back to the item ``id``, so a host that omits
  the field is unchanged.

And one invariant that must NOT change: ``_active_tool_calls`` stays keyed by
the item ``id``, because the argument deltas are. Fixture A carries a delta
keyed by ``fc_1`` while the client sees ``call_1``; if the keying moved, the
arguments would arrive empty.
"""

import json
from collections.abc import Iterable
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_responses import ResponsesStreamConverter
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
    responses_tool_call_to_anthropic,
)

_COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}


def _convert(frames: Iterable[dict[str, Any]]) -> str:
    ledger = AnthropicStreamLedger("msg_x", "gpt-6-sol", input_tokens=0)
    converter = ResponsesStreamConverter(ledger)
    out = [ledger.message_start()]
    for frame in frames:
        out.extend(converter.feed(frame))
    out.extend(converter.finish())
    return "".join(out)


def _call_frames(
    item_id: str | None, call_id: str | None, name: str, arguments: str
) -> list[dict[str, Any]]:
    """One complete function-call item, exactly as the backend streams it.

    The delta is keyed by the *item* id, which is the only id that frame
    carries. ``call_id`` reaches the client only through the block we open.
    """
    item: dict[str, Any] = {"type": "function_call", "name": name}
    if item_id is not None:
        item["id"] = item_id
    if call_id is not None:
        item["call_id"] = call_id
    delta: dict[str, Any] = {
        "type": "response.function_call_arguments.delta",
        "delta": arguments,
    }
    if item_id is not None:
        delta["item_id"] = item_id
    return [
        {"type": "response.output_item.added", "item": item},
        delta,
        {"type": "response.output_item.done", "item": {**item, "arguments": arguments}},
    ]


def _tool_blocks(sse: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(id, name, input)`` for every tool_use block, in stream order."""
    ids: dict[int, str] = {}
    names: dict[int, str] = {}
    parts: dict[int, str] = {}
    for event in parse_sse_text(sse):
        data = event.data
        if event.event == "content_block_start":
            block = data["content_block"]
            if block.get("type") == "tool_use":
                ids[data["index"]] = block["id"]
                names[data["index"]] = block["name"]
                parts[data["index"]] = ""
        elif event.event == "content_block_delta":
            delta = data["delta"]
            if delta.get("type") == "input_json_delta" and data["index"] in parts:
                parts[data["index"]] += delta.get("partial_json") or ""
    return [
        (ids[index], names[index], json.loads(parts[index]) if parts[index] else {})
        for index in sorted(ids)
    ]


def _next_body(
    tool_use_id: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """The request the client sends next, replaying the id it was given."""
    request = MessagesRequest.model_validate(
        {
            "model": "gpt-6-sol",
            "max_tokens": 64,
            "stream": True,
            "messages": [
                {"role": "user", "content": "do the task"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": name,
                            "input": arguments,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "ok",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": name,
                    "description": "d",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    },
                }
            ],
        }
    )
    return build_responses_request_body(request, reasoning=ReasoningPolicy.off())


def _paired_call_ids(body: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (str(item.get("type")), str(item.get("call_id")))
        for item in body["input"]
        if item.get("type") in {"function_call", "function_call_output"}
    ]


# -- A: the two ids differ ----------------------------------------------------------


def test_a_the_client_sees_the_call_id_and_the_next_request_pairs_by_it() -> None:
    sse = _convert(
        [*_call_frames("fc_1", "call_1", "Bash", '{"command":"ls"}'), _COMPLETED]
    )
    assert _tool_blocks(sse) == [("call_1", "Bash", {"command": "ls"})]

    body = _next_body("call_1", "Bash", {"command": "ls"})
    assert _paired_call_ids(body) == [
        ("function_call", "call_1"),
        ("function_call_output", "call_1"),
    ]


def test_a_the_item_id_still_keys_the_argument_deltas() -> None:
    """The invariant the fix must not break: deltas are matched by item id.

    The block is opened with ``call_1`` and the delta frame is keyed by
    ``fc_1``. If ``_active_tool_calls`` were keyed by the client-visible id
    the delta would find nothing and the arguments would arrive empty.
    """
    sse = _convert(
        [*_call_frames("fc_1", "call_1", "Bash", '{"command":"ls"}'), _COMPLETED]
    )
    assert _tool_blocks(sse)[0][2] == {"command": "ls"}


def test_a_the_non_streaming_converter_agrees() -> None:
    block = responses_tool_call_to_anthropic(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "Bash",
            "arguments": '{"command":"ls"}',
        }
    )
    assert block["id"] == "call_1"


# -- B: the common case cannot move ---------------------------------------------------


def test_b_an_item_whose_id_is_its_call_id_is_byte_identical_to_before() -> None:
    """The exact frames 8.14.2 sent for ``id == call_id``, pinned.

    The equality proof for the common case: a host that mints one id for both
    cannot observe this change at all.
    """
    sse = _convert(
        [*_call_frames("call_9", "call_9", "Bash", '{"command":"ls"}'), _COMPLETED]
    )
    assert sse == (
        'event: message_start\ndata: {"type": "message_start", "message": '
        '{"id": "msg_x", "type": "message", "role": "assistant", "content": [], '
        '"model": "gpt-6-sol", "stop_reason": null, "stop_sequence": null, '
        '"usage": {"input_tokens": 0, "output_tokens": 1}}}\n\n'
        'event: content_block_start\ndata: {"type": "content_block_start", '
        '"index": 0, "content_block": {"type": "tool_use", "id": "call_9", '
        '"name": "Bash", "input": {}}}\n\n'
        'event: content_block_delta\ndata: {"type": "content_block_delta", '
        '"index": 0, "delta": {"type": "input_json_delta", "partial_json": '
        '"{\\"command\\":\\"ls\\"}"}}\n\n'
        'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n'
        'event: message_delta\ndata: {"type": "message_delta", "delta": '
        '{"stop_reason": "tool_use", "stop_sequence": null}, "usage": '
        '{"input_tokens": 0, "output_tokens": 0}}\n\n'
        'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    ), sse


def test_b_the_non_streaming_converter_agrees() -> None:
    block = responses_tool_call_to_anthropic(
        {
            "type": "function_call",
            "id": "call_9",
            "call_id": "call_9",
            "name": "Bash",
            "arguments": '{"command":"ls"}',
        }
    )
    assert block["id"] == "call_9"


# -- C: a multi-call turn -------------------------------------------------------------


def test_c_each_call_gets_its_own_call_id_and_pairs_with_its_own_output() -> None:
    sse = _convert(
        [
            *_call_frames("fc_1", "call_1", "Read", '{"path":"a"}'),
            *_call_frames("fc_2", "call_2", "Grep", '{"pattern":"b"}'),
            _COMPLETED,
        ]
    )
    assert _tool_blocks(sse) == [
        ("call_1", "Read", {"path": "a"}),
        ("call_2", "Grep", {"pattern": "b"}),
    ]

    # Both outputs come back, in the order the calls were made.
    request = MessagesRequest.model_validate(
        {
            "model": "gpt-6-sol",
            "max_tokens": 64,
            "stream": True,
            "messages": [
                {"role": "user", "content": "do the task"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "Read",
                            "input": {"path": "a"},
                        },
                        {
                            "type": "tool_use",
                            "id": "call_2",
                            "name": "Grep",
                            "input": {"pattern": "b"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": "a",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_2",
                            "content": "b",
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "name": "Read",
                    "description": "d",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
                {
                    "name": "Grep",
                    "description": "d",
                    "input_schema": {
                        "type": "object",
                        "properties": {"pattern": {"type": "string"}},
                    },
                },
            ],
        }
    )
    body = build_responses_request_body(request, reasoning=ReasoningPolicy.off())
    assert _paired_call_ids(body) == [
        ("function_call", "call_1"),
        ("function_call", "call_2"),
        ("function_call_output", "call_1"),
        ("function_call_output", "call_2"),
    ]


# -- D: a host that omits call_id -----------------------------------------------------


def test_d_an_item_without_a_call_id_falls_back_to_the_item_id() -> None:
    sse = _convert(
        [*_call_frames("fc_7", None, "Bash", '{"command":"ls"}'), _COMPLETED]
    )
    assert _tool_blocks(sse) == [("fc_7", "Bash", {"command": "ls"})]

    body = _next_body("fc_7", "Bash", {"command": "ls"})
    assert _paired_call_ids(body) == [
        ("function_call", "fc_7"),
        ("function_call_output", "fc_7"),
    ]


def test_d_the_non_streaming_converter_agrees() -> None:
    block = responses_tool_call_to_anthropic(
        {
            "type": "function_call",
            "id": "fc_7",
            "name": "Bash",
            "arguments": '{"command":"ls"}',
        }
    )
    assert block["id"] == "fc_7"


# -- the fallback when the host sends neither -----------------------------------------


def test_an_item_with_neither_id_still_gets_a_globally_unique_call_id() -> None:
    """Neither id present: the converter mints one, as the inbound path does.

    ``new_call_id()`` is ``call_<uuid4 hex[:24]>`` -- globally unique -- which
    is the safe fallback; a per-turn counter could collide across turns of one
    conversation.
    """
    sse = _convert([*_call_frames(None, None, "Bash", '{"command":"ls"}'), _COMPLETED])
    (tool_id, name, _input) = _tool_blocks(sse)[0]
    assert name == "Bash"
    assert tool_id.startswith("call_")
    assert len(tool_id) == len("call_") + 24
    # Two turns never mint the same one.
    again = _convert([*_call_frames(None, None, "Bash", "{}"), _COMPLETED])
    assert _tool_blocks(again)[0][0] != tool_id


@pytest.mark.parametrize(
    "item",
    [
        {"type": "function_call", "id": "call_1", "call_id": "call_1"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1"},
        {"type": "function_call", "id": "fc_1"},
    ],
    ids=["same", "different", "no_call_id"],
)
def test_the_two_converters_never_disagree(item: dict[str, Any]) -> None:
    """Whatever the item, the streaming and non-streaming doors pick one id."""
    item = {**item, "name": "Bash", "arguments": "{}"}
    streamed = _convert(
        [
            {
                "type": "response.output_item.added",
                "item": {k: v for k, v in item.items() if k != "arguments"},
            },
            {"type": "response.output_item.done", "item": item},
            _COMPLETED,
        ]
    )
    assert _tool_blocks(streamed)[0][0] == responses_tool_call_to_anthropic(item)["id"]
