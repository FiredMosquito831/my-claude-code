"""Capture of the three block kinds an assistant turn can stream.

Only ``text_delta`` used to be recorded, so a turn that just called tools --
the common shape under Claude Code -- stored nothing at all, and reasoning was
discarded outright.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.core.request_log import RequestLogStore


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _events(*payloads: dict[str, Any]) -> list[str]:
    return [
        f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
        for payload in payloads
    ]


def _tool_start(index: int, name: str) -> dict[str, Any]:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": f"tu_{index}", "name": name},
    }


def _delta(index: int, delta: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def _make_capture(store: RequestLogStore, **overrides: Any) -> RequestCapture:
    defaults: dict[str, Any] = {
        "request_id": "req_test",
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "stream": True,
        "requested_model": "claude-sonnet-4-5",
        "input_text": "hello",
        "params": None,
    }
    defaults.update(overrides)
    return RequestCapture(store, **defaults)


async def _run(capture: RequestCapture, frames: list[str]) -> None:
    async def body() -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    async for _ in capture.wrap(body()):
        pass


def _detail(store: RequestLogStore) -> dict[str, Any]:
    row = store.get_request("req_test")
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_tool_only_turn_records_the_call(store: RequestLogStore) -> None:
    frames = _events(
        _tool_start(0, "Read"),
        _delta(0, {"type": "input_json_delta", "partial_json": '{"path":'}),
        _delta(0, {"type": "input_json_delta", "partial_json": '"/tmp/a"}'}),
        {"type": "message_delta", "usage": {"output_tokens": 9}},
    )
    capture = _make_capture(store)
    await _run(capture, frames)
    store.close()

    row = _detail(store)
    assert row["tool_call_count"] == 1
    assert row["tool_calls"] == [{"name": "Read", "input": {"path": "/tmp/a"}}]
    # A tool-only turn genuinely has no prose; that is not a capture failure.
    assert row["output_chars"] == 0
    assert row["output_text"] is None


@pytest.mark.asyncio
async def test_thinking_is_kept_out_of_the_response_text(
    store: RequestLogStore,
) -> None:
    frames = _events(
        _delta(0, {"type": "thinking_delta", "thinking": "Let me check. "}),
        _delta(0, {"type": "thinking_delta", "thinking": "Yes."}),
        _delta(1, {"type": "text_delta", "text": "Done."}),
    )
    capture = _make_capture(store)
    await _run(capture, frames)
    store.close()

    row = _detail(store)
    assert row["thinking_text"] == "Let me check. Yes."
    assert row["thinking_chars"] == 18
    assert row["output_text"] == "Done."
    assert row["output_chars"] == 5


@pytest.mark.asyncio
async def test_a_completed_stream_with_no_reasoning_records_zero_not_null(
    store: RequestLogStore,
) -> None:
    """0 is a measurement; NULL is the absence of one.

    Folding them together with ``or None`` made a model that was asked to
    think and returned nothing indistinguishable from a row nobody counted --
    which is precisely the question ``reasoning_by_model`` exists to answer.
    """
    frames = _events(_delta(0, {"type": "text_delta", "text": "Done."}))
    capture = _make_capture(store)
    await _run(capture, frames)
    store.close()

    row = _detail(store)
    assert row["thinking_chars"] == 0
    assert row["thinking_chars"] is not None
    assert row["thinking_text"] is None


@pytest.mark.asyncio
async def test_split_frames_across_chunks_still_capture(
    store: RequestLogStore,
) -> None:
    """SSE frames arrive at arbitrary chunk boundaries, not frame boundaries."""
    blob = "".join(
        _events(
            _delta(0, {"type": "thinking_delta", "thinking": "abc"}),
            _tool_start(1, "Grep"),
            _delta(1, {"type": "input_json_delta", "partial_json": '{"p": "x"}'}),
            _delta(2, {"type": "text_delta", "text": "ok"}),
        )
    )
    capture = _make_capture(store)

    async def body() -> AsyncIterator[str]:
        for index in range(0, len(blob), 7):
            yield blob[index : index + 7]

    async for _ in capture.wrap(body()):
        pass
    store.close()

    row = _detail(store)
    assert row["thinking_text"] == "abc"
    assert row["tool_calls"] == [{"name": "Grep", "input": {"p": "x"}}]
    assert row["output_text"] == "ok"


@pytest.mark.asyncio
async def test_multiple_tool_calls_keep_block_order(store: RequestLogStore) -> None:
    frames = _events(
        _tool_start(1, "Second"),
        _tool_start(0, "First"),
        _delta(1, {"type": "input_json_delta", "partial_json": "{}"}),
        _delta(0, {"type": "input_json_delta", "partial_json": "{}"}),
    )
    capture = _make_capture(store)
    await _run(capture, frames)
    store.close()

    row = _detail(store)
    assert [call["name"] for call in row["tool_calls"]] == ["First", "Second"]


@pytest.mark.asyncio
async def test_truncated_tool_arguments_are_kept_as_a_fragment(
    store: RequestLogStore,
) -> None:
    frames = _events(
        _tool_start(0, "Write"),
        _delta(0, {"type": "input_json_delta", "partial_json": '{"path": "/t'}),
    )
    capture = _make_capture(store)
    await _run(capture, frames)
    store.close()

    row = _detail(store)
    assert row["tool_calls"] == [{"name": "Write", "input_partial": '{"path": "/t'}]


@pytest.mark.asyncio
async def test_tool_use_without_arguments_records_an_empty_input(
    store: RequestLogStore,
) -> None:
    capture = _make_capture(store)
    await _run(capture, _events(_tool_start(0, "ListPages")))
    store.close()

    row = _detail(store)
    assert row["tool_calls"] == [{"name": "ListPages", "input": {}}]


@pytest.mark.asyncio
async def test_bodies_disabled_keeps_counts_but_drops_text(
    store: RequestLogStore,
) -> None:
    frames = _events(
        _tool_start(0, "Read"),
        _delta(0, {"type": "input_json_delta", "partial_json": '{"a": 1}'}),
        _delta(1, {"type": "thinking_delta", "thinking": "secret"}),
    )
    capture = _make_capture(store, capture_bodies=False)
    await _run(capture, frames)
    store.close()

    row = _detail(store)
    assert row["tool_call_count"] == 1
    assert row["thinking_chars"] == 6
    assert row["tool_calls"] is None
    assert row["thinking_text"] is None


@pytest.mark.asyncio
async def test_turn_without_tools_or_thinking_records_nothing_extra(
    store: RequestLogStore,
) -> None:
    capture = _make_capture(store)
    await _run(capture, _events(_delta(0, {"type": "text_delta", "text": "hi"})))
    store.close()

    row = _detail(store)
    assert row["tool_call_count"] is None
    # ...except the thinking count, which since 6.8.0 is a measured 0 rather
    # than a NULL: this stream was watched and it returned no reasoning.
    assert row["thinking_chars"] == 0
    assert row["output_text"] == "hi"


@pytest.mark.asyncio
async def test_complete_message_splits_its_blocks(store: RequestLogStore) -> None:
    capture = _make_capture(store, stream=False)
    capture.finish_success_from_message(
        {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "tool_use", "name": "Search", "input": {"q": "cats"}},
                {"type": "text", "text": "Here you go."},
            ]
        }
    )
    store.close()

    row = _detail(store)
    assert row["thinking_text"] == "hmm"
    assert row["output_text"] == "Here you go."
    assert row["tool_calls"] == [{"name": "Search", "input": {"q": "cats"}}]


@pytest.mark.asyncio
async def test_stats_aggregate_turn_shape(store: RequestLogStore) -> None:
    """The analytics cards count tool use and reasoning across the window."""
    for index, frames in enumerate(
        [
            _events(
                _tool_start(0, "Read"),
                _delta(0, {"type": "input_json_delta", "partial_json": "{}"}),
                _delta(1, {"type": "thinking_delta", "thinking": "hmm"}),
            ),
            _events(
                _tool_start(0, "Grep"),
                _delta(0, {"type": "input_json_delta", "partial_json": "{}"}),
                _tool_start(1, "Bash"),
                _delta(1, {"type": "input_json_delta", "partial_json": "{}"}),
            ),
            _events(_delta(0, {"type": "text_delta", "text": "plain reply"})),
        ]
    ):
        capture = _make_capture(store, request_id=f"req_{index}")
        await _run(capture, frames)
    store.close()

    stats = store.stats()
    assert stats["total"] == 3
    assert stats["tool_calls"] == 3
    assert stats["turns_with_tools"] == 2
    assert stats["turns_with_reasoning"] == 1


@pytest.mark.asyncio
async def test_list_view_exposes_turn_shape_without_bodies(
    store: RequestLogStore,
) -> None:
    """The table needs the shape of a turn, not its transcript."""
    frames = _events(
        _tool_start(0, "Read"),
        _delta(0, {"type": "input_json_delta", "partial_json": "{}"}),
        _delta(1, {"type": "thinking_delta", "thinking": "think"}),
    )
    capture = _make_capture(store)
    await _run(capture, frames)
    store.close()

    rows, total = store.list_requests()
    assert total == 1
    assert rows[0]["tool_call_count"] == 1
    assert rows[0]["thinking_chars"] == 5
    # Bodies are deliberately absent from list projections.
    assert "thinking_text" not in rows[0]
