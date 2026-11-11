"""The reply's shape: bounded, content-free, and absent when not measured."""

import json
from contextvars import ContextVar

import pytest

from my_claude_code.core import wire_capture
from my_claude_code.core.wire_capture import (
    RESPONSE_SHAPE_MAX_CHARS,
    install_wire_trace,
    record_response_shape,
    start_response_shape,
)


@pytest.fixture(autouse=True)
def untracked(monkeypatch) -> None:
    """Start every test from "no request is being captured".

    The trace lives in a ``ContextVar``, so a test asserting its *absence*
    otherwise depends on which test the worker ran before it. Replacing the
    variable makes "outside a tracked request" a fact rather than a hope.
    """
    monkeypatch.setattr(
        wire_capture, "_WIRE_TRACE", ContextVar("fcc_wire_trace_test", default=None)
    )


def test_no_tally_is_started_outside_a_tracked_request() -> None:
    """Token counting, model discovery and direct provider use pay nothing."""
    assert start_response_shape() is None
    record_response_shape(None)


def test_a_tally_records_fields_counts_and_characters() -> None:
    install_wire_trace()
    shape = start_response_shape()
    assert shape is not None

    shape.note_chunk(1.0)
    shape.note_field("reasoning_content", 40)
    shape.note_field("reasoning_content", 60)
    shape.note_field("content", 12)
    shape.note_finish("stop")
    shape.note_usage({"prompt_tokens": 10, "completion_tokens": 3})

    payload = shape.payload()
    assert payload["fields"]["reasoning_content"] == {"deltas": 2, "chars": 100}
    assert payload["fields"]["content"] == {"deltas": 1, "chars": 12}
    assert payload["finish_reason"] == "stop"
    assert payload["usage"] is True
    assert payload["usage_keys"] == ["completion_tokens", "prompt_tokens"]
    assert payload["chunks"] == 1


def test_the_tally_stores_no_text_at_all() -> None:
    install_wire_trace()
    shape = start_response_shape()
    assert shape is not None
    secret = "the model's actual answer"

    shape.note_field("content", len(secret))
    shape.note_usage({"prompt_tokens": 1})

    rendered = json.dumps(shape.payload())
    assert secret not in rendered
    for word in secret.split():
        assert word not in rendered


def test_the_first_chunk_is_timed_and_later_ones_are_not() -> None:
    install_wire_trace()
    shape = start_response_shape()
    assert shape is not None
    shape.started_at = 100.0

    shape.note_chunk(100.25)
    shape.note_chunk(101.0)

    payload = shape.payload()
    assert payload["first_chunk_ms"] == 250
    assert payload["chunks"] == 2


def test_an_empty_reply_is_measured_rather_than_missing() -> None:
    """Zero and absent are different facts, and this is the zero one."""
    install_wire_trace()
    shape = start_response_shape()
    assert shape is not None
    shape.note_chunk(1.0)
    shape.note_finish("stop")

    payload = shape.payload()
    assert payload["fields"] == {}
    assert payload["usage"] is False
    assert payload["finish_reason"] == "stop"


def test_the_record_is_capped() -> None:
    install_wire_trace()
    shape = start_response_shape()
    assert shape is not None
    shape.note_usage({f"key_{index}" * 8: 1 for index in range(24)})
    for index in range(40):
        shape.note_field(f"field_{index}", index)

    payload = shape.payload()

    assert len(json.dumps(payload)) <= RESPONSE_SHAPE_MAX_CHARS
    assert len(payload["fields"]) <= 12


def test_a_recorded_tally_lands_against_the_current_attempt() -> None:
    trace = install_wire_trace()
    trace.current_attempt = 2
    shape = start_response_shape()
    assert shape is not None
    shape.note_field("content", 5)

    record_response_shape(shape)

    assert set(trace.responses) == {2}
    assert trace.responses[2]["fields"]["content"]["chars"] == 5


# ------------------------------------------------------------------ 8.20.1
# A tool call whose arguments fail to parse or exceed a bound used to leave no
# trace anywhere: the client threw its own error and the request row said only
# how many calls there were. These are the four facts that make it visible --
# and the tests that keep the arguments themselves out of the record.


def _shape_with_tool_calls(*calls: tuple[str, str], limit: int = 131_072):
    install_wire_trace(tool_argument_limit=limit)
    shape = start_response_shape()
    assert shape is not None
    for name, arguments in calls:
        shape.note_tool_call(name, arguments)
    return shape


def test_a_reply_with_two_tool_calls_records_four_facts_each() -> None:
    """The payload, field for field, on one over-bound call and one ordinary.

    The over-bound call is the shape this release exists for: hundreds of
    thousands of characters of arguments, never parsed, counted as over the
    bound.
    """
    huge = '{"query": "' + "x" * 483_100 + '"}'
    ordinary = '{"file_path": "/tmp/a.txt"}'
    shape = _shape_with_tool_calls(("ToolSearch", huge), ("Read", ordinary))

    tool_calls = shape.payload()["tool_calls"]

    assert tool_calls["calls"] == 2
    assert tool_calls["parsed"] == 1
    assert tool_calls["max_chars"] == len(huge)
    assert tool_calls["over_bound"] == 1
    assert tool_calls["unparseable"] == 0
    assert tool_calls["not_an_object"] == 0
    assert tool_calls["largest"] == [
        {
            "name": "ToolSearch",
            "chars": len(huge),
            "parsed": None,
            "class": "over_bound",
        },
        {"name": "Read", "chars": len(ordinary), "parsed": True, "class": None},
    ]


def test_the_tool_name_is_structure_and_the_arguments_are_not() -> None:
    """Rule 1: the name is kept, and it is the only string kept."""
    shape = _shape_with_tool_calls(("Bash", '{"command": "ls"}'))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["name"] == "Bash"
    assert set(entry) == {"name", "chars", "parsed", "class"}


def test_chars_is_a_length_never_a_slice() -> None:
    """Rule 2: the count is ``len(arguments)`` and nothing of it is kept."""
    arguments = '{"command": "echo hello"}'
    shape = _shape_with_tool_calls(("Bash", arguments))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["chars"] == len(arguments)


def test_parsed_is_a_boolean_never_the_parsed_value() -> None:
    """Rule 3: the parse result is discarded where it is produced."""
    shape = _shape_with_tool_calls(("Read", '{"file_path": "/etc/hosts"}'))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["parsed"] is True
    assert not isinstance(entry["parsed"], dict)


def test_the_class_is_one_of_three_protocol_words_or_null() -> None:
    """Rule 4: never a message, an exception string or a byte offset."""
    shape = _shape_with_tool_calls(
        ("A", '{"ok": 1}'),
        ("B", '{"broken": '),
        ("C", "[1, 2, 3]"),
        ("D", "x" * 200_000),
    )

    rendered = json.dumps(shape.payload())
    classes = [call["class"] for call in shape.payload()["tool_calls"]["largest"]]

    assert set(classes) <= {None, "unparseable", "not_an_object", "over_bound"}
    # ``json.JSONDecodeError``'s own ``msg`` and ``pos`` are deliberately not
    # recorded: a message plus an offset is a fragment of the arguments.
    assert "Expecting" not in rendered
    assert "delimiter" not in rendered
    assert '"pos"' not in rendered


def test_the_largest_list_holds_at_most_four_entries_largest_first() -> None:
    """Rule 5: bounded, and the biggest call is the one you read first."""
    shape = _shape_with_tool_calls(
        *((f"tool_{index}", "x" * (index + 1)) for index in range(9))
    )

    tool_calls = shape.payload()["tool_calls"]

    assert len(tool_calls["largest"]) == 4
    assert [call["chars"] for call in tool_calls["largest"]] == [9, 8, 7, 6]
    assert tool_calls["calls"] == 9


def test_the_tool_call_tally_stores_no_text_at_all() -> None:
    """A tool argument never reaches the payload, not even eight characters.

    The sibling of ``test_the_tally_stores_no_text_at_all`` above, with the
    one input this release is about: the model's own argument string. Every
    substring of eight characters or more is checked, not just whole words,
    because a prefix or a slice would be a fragment of the arguments by
    another name.
    """
    arguments = '{"file_path": "/home/operator/secret-notes.txt", "limit": 40}'
    shape = _shape_with_tool_calls(("Read", arguments))

    rendered = json.dumps(shape.payload())

    assert arguments not in rendered
    for start in range(len(arguments) - 7):
        fragment = arguments[start : start + 8]
        assert fragment not in rendered, fragment
    # And the argument string is not held on the shape either.
    assert arguments not in repr(shape)


def test_the_tool_call_tally_participates_in_the_existing_cap() -> None:
    """Rule 5, second half: no second degradation mechanism was added.

    Forty calls with maximal names, on top of a payload that already overruns
    the cap, still lands under ``RESPONSE_SHAPE_MAX_CHARS`` through the
    existing ``_degraded`` path -- and the tally keeps its own bound of four.
    """
    shape = _shape_with_tool_calls(
        *((f"tool_{index}_" + "n" * 200, "x" * 200_000) for index in range(40))
    )
    shape.note_usage({f"key_{index}" * 8: 1 for index in range(24)})
    for index in range(40):
        shape.note_field(f"field_{index}", index)

    payload = shape.payload()

    assert len(json.dumps(payload)) <= RESPONSE_SHAPE_MAX_CHARS
    assert payload["_degraded"] is True
    assert payload["tool_calls"]["calls"] == 40
    assert len(payload["tool_calls"]["largest"]) == 4
    assert all(len(call["name"]) <= 128 for call in payload["tool_calls"]["largest"])


def test_a_reply_with_no_tool_call_carries_no_tool_calls_key() -> None:
    """Absent when not measured -- the rule ``shape.reasoning`` already keeps."""
    install_wire_trace()
    shape = start_response_shape()
    assert shape is not None
    shape.note_field("content", 12)

    payload = shape.payload()

    assert "tool_calls" not in payload
    # Byte-identical to what 8.20.0 wrote for the same reply: the whole
    # payload, with no key added and none moved.
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        {
            "fields": {"content": {"deltas": 1, "chars": 12}},
            "chunks": 0,
            "finish_reason": None,
            "usage": False,
            "usage_keys": [],
            "first_chunk_ms": None,
        },
        sort_keys=True,
    )


def test_a_tool_call_adds_exactly_one_key_and_moves_nothing_else() -> None:
    """The equality contract: one new key, every other key unchanged."""
    install_wire_trace()
    without = start_response_shape()
    assert without is not None
    without.note_field("content", 12)
    without.note_finish("tool_use")
    without.note_usage({"input_tokens": 5, "output_tokens": 9})
    base = without.payload()

    with_calls = start_response_shape()
    assert with_calls is not None
    with_calls.note_field("content", 12)
    with_calls.note_finish("tool_use")
    with_calls.note_usage({"input_tokens": 5, "output_tokens": 9})
    with_calls.note_tool_call("Read", '{"file_path": "a"}')
    branch = with_calls.payload()

    # Anti-vacuity: the branch payload really carries the new key.
    assert "tool_calls" in branch
    assert set(branch) - set(base) == {"tool_calls"}
    assert {key: branch[key] for key in base} == base


def test_a_call_of_exactly_the_bound_is_not_over_it() -> None:
    """The comparison is ``>``, so the boundary value is ordinary."""
    shape = _shape_with_tool_calls(("Bash", "x" * 64), limit=64)

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["chars"] == 64
    assert entry["class"] == "unparseable"
    assert entry["parsed"] is False


def test_a_call_one_character_over_the_bound_is_over_it() -> None:
    shape = _shape_with_tool_calls(("Bash", "x" * 65), limit=64)

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["class"] == "over_bound"
    assert entry["parsed"] is None


def test_zero_disables_the_bound() -> None:
    """``0`` means "never flag anything as too large", as the sibling knob does."""
    shape = _shape_with_tool_calls(("Bash", "x" * 200_000), limit=0)

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["chars"] == 200_000
    assert entry["class"] == "unparseable"
    assert entry["parsed"] is False


def test_the_parse_is_skipped_over_the_bound_and_parsed_is_null() -> None:
    """NULL is not 0: "we did not measure" is not "it failed" (III.8).

    The argument string here *would* parse -- trailing whitespace is legal
    JSON -- so a ``parsed`` of ``True`` would prove the parse ran, and a
    ``False`` would prove nobody measured. It must be ``None``.
    """
    shape = _shape_with_tool_calls(("Bash", '{"valid": true}' + " " * 200_000))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["class"] == "over_bound"
    assert entry["parsed"] is None


def test_arguments_that_fail_to_parse_are_classed_unparseable() -> None:
    """Case 1: a ``json.JSONDecodeError`` on the assembled argument string."""
    shape = _shape_with_tool_calls(("Bash", '{"command": "ls -'))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["class"] == "unparseable"
    assert entry["parsed"] is False


def test_arguments_that_parse_to_a_non_object_are_classed_not_an_object() -> None:
    """Case 1b: the client's ``input`` must be an object."""
    shape = _shape_with_tool_calls(("Bash", '["ls", "-la"]'))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["class"] == "not_an_object"
    assert entry["parsed"] is False


def test_arguments_that_parse_to_an_object_are_classed_null() -> None:
    shape = _shape_with_tool_calls(("Bash", '{"command": "ls"}'))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["class"] is None
    assert entry["parsed"] is True


def test_a_deeply_nested_argument_string_does_not_raise() -> None:
    """``json.loads`` recurses per level; a tally may never break a stream."""
    shape = _shape_with_tool_calls(("Bash", "[" * 60_000))

    entry = shape.payload()["tool_calls"]["largest"][0]

    assert entry["class"] == "unparseable"
    assert entry["parsed"] is False


def test_the_bound_is_read_from_the_request_trace() -> None:
    """The setting reaches the tally through the trace, not through ``core``."""
    install_wire_trace(tool_argument_limit=7)
    shape = start_response_shape()
    assert shape is not None

    assert shape.tool_argument_limit == 7
