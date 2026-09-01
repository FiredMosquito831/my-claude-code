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
