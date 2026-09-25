"""``STREAM_KEEPALIVE_MODE=frames``: empty deltas on the model's own open block.

The 7.46.0 keepalive writes Anthropic's ``ping`` on ``/v1/messages``, and the
official Anthropic SDK -- which is what Claude Code runs -- drops ``ping``
before its stream loop, the only thing that re-arms Claude Code's idle timer.
Frames mode (opt-in) writes, while and only while the model's own ``text`` or
``tool_use`` block is open, an EMPTY ``content_block_delta`` of that block's
own kind. Pinned here, against the real seam and the real ``RequestCapture``,
with no upstream called:

* where a frame may go: only inside an open text or tool-call block; a ping
  before ``message_start``, before the first block, between blocks, inside a
  thinking block, and after the last block -- never a thinking delta, never a
  fabricated block;
* the contract: the concatenated content with frames on is byte-identical to
  frames off, and so is what Claude Code's own parse path assembles from it;
* Claude Code's parse path accepts the empty deltas (transcribed from the
  bundled SDK, see ``_ClaudeCodeParse``) -- and a delta on a closed block, the
  thing this mode must never write, is exactly what it rejects;
* the measurements do not move (``ttft_ms``, ``output_chars``, the chunk count,
  ``_saw_terminal_event``), the cap still applies, other surfaces keep their
  SSE comment, and the request row records how many frames were sent.
"""

import asyncio
import itertools
import json
import sqlite3
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from my_claude_code.api import response_streams
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.api.response_streams import (
    ANTHROPIC_KEEPALIVE_FRAME,
    SSE_COMMENT_KEEPALIVE_FRAME,
    StreamKeepalive,
    anthropic_sse_streaming_response,
    empty_delta_frame,
    openai_sse_streaming_response,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.keepalive_tally import current_keepalive_tally
from my_claude_code.core.request_log import RequestLogStore

FRAMES = StreamKeepalive(
    idle_seconds=0.05, interval_seconds=0.05, max_seconds=0, frames=True
)
PINGS = StreamKeepalive(idle_seconds=0.05, interval_seconds=0.05, max_seconds=0)

TEXT_DELTA_0 = (
    "event: content_block_delta\n"
    'data: {"type": "content_block_delta", "index": 0, '
    '"delta": {"type": "text_delta", "text": ""}}\n\n'
)


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _start(index: int, block: dict[str, Any]) -> str:
    return _event(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def _delta(index: int, delta: dict[str, Any]) -> str:
    return _event(
        "content_block_delta",
        {"type": "content_block_delta", "index": index, "delta": delta},
    )


def _stop(index: int) -> str:
    return _event("content_block_stop", {"type": "content_block_stop", "index": index})


MESSAGE_START = _event(
    "message_start",
    {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "m",
            "content": [],
            "usage": {"input_tokens": 4},
        },
    },
)
MESSAGE_DELTA = _event(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use"},
        "usage": {"output_tokens": 9},
    },
)
MESSAGE_STOP = _event("message_stop", {"type": "message_stop"})

# A turn with every block kind this mode must tell apart: thinking (index 0),
# text (1), a tool call (2). Chunk numbers are what the pause maps key on.
TURN = [
    MESSAGE_START,  # 0
    _start(0, {"type": "thinking", "thinking": "", "signature": ""}),  # 1
    _delta(0, {"type": "thinking_delta", "thinking": "Let me look."}),  # 2
    _delta(0, {"type": "signature_delta", "signature": "sig-from-upstream"}),  # 3
    _stop(0),  # 4
    _start(1, {"type": "text", "text": ""}),  # 5
    _delta(1, {"type": "text_delta", "text": "Reading "}),  # 6
    _delta(1, {"type": "text_delta", "text": "the file."}),  # 7
    _stop(1),  # 8
    _start(2, {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}),  # 9
    _delta(2, {"type": "input_json_delta", "partial_json": ""}),  # 10
    _delta(2, {"type": "input_json_delta", "partial_json": '{"file_path": '}),  # 11
    _delta(2, {"type": "input_json_delta", "partial_json": '"/a.py"}'}),  # 12
    _stop(2),  # 13
    MESSAGE_DELTA,  # 14
    MESSAGE_STOP,  # 15
]

# Silence *before* chunk i. Named for where the client is when it happens.
SILENT_BEFORE_MESSAGE_START = {0: 0.3}
SILENT_BEFORE_FIRST_BLOCK = {1: 0.3}
SILENT_IN_THINKING = {3: 0.3}
SILENT_BETWEEN_BLOCKS = {5: 0.3}
SILENT_IN_TEXT = {7: 0.3}
SILENT_IN_TOOL_ARGS = {12: 0.3}
SILENT_AFTER_LAST_BLOCK = {14: 0.3}


async def _paced(
    chunks: list[str], pauses: dict[int, float] | None = None
) -> AsyncGenerator[str]:
    for index, chunk in enumerate(chunks):
        pause = (pauses or {}).get(index, 0.0)
        if pause:
            await asyncio.sleep(pause)
        yield chunk


def _error(_exc: BaseException) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"type": "error", "error": {"type": "api_error", "message": "x"}},
    )


async def _anthropic(
    body: AsyncIterator[str], keepalive: StreamKeepalive | None
) -> Any:
    return await anthropic_sse_streaming_response(
        body,
        pre_start_error_response=_error,
        request_id="req_frames",
        keepalive=keepalive,
    )


async def _drain(response: Any) -> list[str]:
    return [
        chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        async for chunk in response.body_iterator
    ]


async def _drain_timed(response: Any) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    async for chunk in response.body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        out.append((time.perf_counter(), text))
    return out


def _is_keepalive_shaped(frame: str) -> bool:
    """A ping, or one empty text / tool-argument delta -- nothing else."""
    if frame == ANTHROPIC_KEEPALIVE_FRAME:
        return True
    events = _sse_events(frame)
    if len(events) != 1 or events[0][0] != "content_block_delta":
        return False
    delta = events[0][1]["delta"]
    return (delta["type"], delta.get("text", delta.get("partial_json"))) in {
        ("text_delta", ""),
        ("input_json_delta", ""),
    }


def _split(frames: list[str], body: list[str]) -> tuple[list[str], list[str]]:
    """Separate the body's own chunks from what the seam inserted.

    Aligned against the chunks the body really yielded rather than by shape,
    because an inserted ``input_json_delta ""`` is byte for byte the empty
    delta a real upstream sends first in a tool call (``TURN[10]``) -- and
    when the two are identical bytes it does not matter which is which.
    """
    own: list[str] = []
    inserted: list[str] = []
    for frame in frames:
        if len(own) < len(body) and frame == body[len(own)]:
            own.append(frame)
        else:
            assert _is_keepalive_shaped(frame), frame
            inserted.append(frame)
    return own, inserted


def _content(frames: list[str], body: list[str] = TURN) -> str:
    """Everything the model wrote, with every keepalive (ping or frame) removed."""
    own, _ = _split(frames, body)
    return "".join(own)


def _keepalives(frames: list[str], body: list[str] = TURN) -> list[str]:
    return _split(frames, body)[1]


def _concatenated(frames: list[str]) -> dict[int, bytes]:
    """Per block, every delta payload appended in order, keepalives included.

    This is what "content" means to a client: the text, reasoning and tool
    argument JSON it assembles by concatenating the deltas it was sent.
    """
    out: dict[int, str] = {}
    for frame in frames:
        for name, data in _sse_events(frame):
            if name != "content_block_delta":
                continue
            delta = data["delta"]
            piece = delta.get(
                "text", delta.get("partial_json", delta.get("thinking", ""))
            )
            out[data["index"]] = out.get(data["index"], "") + piece
    return {index: text.encode("utf-8") for index, text in out.items()}


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if name is None or data is None:
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue  # a fragment of a frame split across chunks
        events.append((name, payload))
    return events


# --------------------------------------------------------------------------
# Claude Code's parse path, transcribed from the bundle
# --------------------------------------------------------------------------


class _StreamBroken(Exception):
    """Claude Code's ``Udt``: the stream is treated as broken and abandoned."""


class _ClaudeCodeParse:
    """What Claude Code does with each SSE frame on its way to the transcript.

    Transcribed, not guessed, from the Claude Code build installed on the
    machine this was written on (``~/.local/bin/claude.exe``, the bundle
    string ``Claude Code v2.1.268``), read as bytes and never executed:

    * the SDK's SSE layer (``Stream.fromSSEResponse``) yields only the message
      lifecycle events and ``continue``s past ``event: ping``, so a ping never
      reaches the loop below and never re-arms the idle timer;
    * the query loop (bundle offset ~207,946,400): ``content_block_start``
      seeds ``text: ""`` for text and ``input: ""`` for ``tool_use``;
      ``content_block_delta`` looks the block up and throws when it is missing
      or already closed (``content_block_not_found_delta`` /
      ``content_block_closed_delta``), throws when a ``text_delta`` meets a
      non-text block or an ``input_json_delta`` a non-tool block, and
      otherwise does ``text += delta.text`` / ``input += delta.partial_json``
      -- so ``""`` is a no-op; ``content_block_stop`` throws on a missing or
      closed block and closes it;
    * the SDK's accumulator (``mn``, offset ~201,063,900) parses a tool call's
      argument buffer lazily as ``buffer ? parse(buffer) : {}``.

    ``timer_rearms`` records when each yielded event arrived: each one resets
    Claude Code's stream idle timer.
    """

    _YIELDED = frozenset(
        {
            "message_start",
            "message_delta",
            "message_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
        }
    )

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.closed: set[int] = set()
        self.message: dict[str, Any] | None = None
        self.finished: list[dict[str, Any]] = []
        self.stopped = False
        self.timer_rearms: list[float] = []

    def feed(self, text: str, at: float = 0.0) -> None:
        for name, data in _sse_events(text):
            if name == "ping":
                continue  # dropped by the SDK before the loop sees it
            if name == "error":
                raise _StreamBroken("error event")
            if name not in self._YIELDED:
                continue
            self.timer_rearms.append(at)
            self._event(data)

    def _event(self, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "message_start":
            self.message = event["message"]
        elif kind == "content_block_start":
            index = event["index"]
            if index in self.closed:
                raise _StreamBroken("content_block_closed_start")
            block = dict(event["content_block"])
            if block["type"] == "tool_use":
                block["input"] = ""
            elif block["type"] == "text":
                block["text"] = ""
            elif block["type"] == "thinking":
                block["thinking"], block["signature"] = "", ""
            self.blocks[index] = block
        elif kind == "content_block_delta":
            block = self._open(event["index"], "delta")
            delta = event["delta"]
            if delta["type"] == "input_json_delta":
                if block["type"] not in {"tool_use", "server_tool_use"}:
                    raise _StreamBroken("content_block_type_mismatch_input_json")
                if not isinstance(block["input"], str):
                    raise _StreamBroken("content_block_input_not_string")
                block["input"] += delta["partial_json"]
            elif delta["type"] == "text_delta":
                if block["type"] != "text":
                    raise _StreamBroken("content_block_type_mismatch_text")
                block["text"] += delta["text"]
            elif delta["type"] == "thinking_delta":
                if block["type"] != "thinking":
                    raise _StreamBroken("content_block_type_mismatch_thinking_delta")
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "signature_delta":
                if block["type"] != "thinking":
                    raise _StreamBroken("content_block_type_mismatch_signature")
                block["signature"] = delta["signature"]
        elif kind == "content_block_stop":
            index = event["index"]
            block = self._open(index, "stop")
            if self.message is None:
                raise _StreamBroken("partial_message_not_found")
            self.closed.add(index)
            done = dict(block)
            if done["type"] == "tool_use":
                done["input"] = json.loads(done["input"]) if done["input"] else {}
            self.finished.append(done)
        elif kind == "message_stop":
            self.stopped = True

    def _open(self, index: int, what: str) -> dict[str, Any]:
        block = self.blocks.get(index)
        if block is None:
            raise _StreamBroken(f"content_block_not_found_{what}")
        if index in self.closed:
            raise _StreamBroken(f"content_block_closed_{what}")
        return block


def _parse(frames: list[str]) -> _ClaudeCodeParse:
    parse = _ClaudeCodeParse()
    for frame in frames:
        parse.feed(frame)
    return parse


# --------------------------------------------------------------------------
# Settings -> policy
# --------------------------------------------------------------------------


def test_the_shipped_mode_is_ping() -> None:
    assert Settings().stream_keepalive_mode == "ping"
    policy = StreamKeepalive.from_settings(Settings())
    assert policy is not None
    assert policy.frames is False


def _mode(value: str) -> Settings:
    """Settings as the .env / dashboard would supply them, by env-var name."""
    return Settings.model_validate({"STREAM_KEEPALIVE_MODE": value})


def test_frames_mode_is_read_from_the_setting() -> None:
    settings = _mode("frames")
    assert settings.stream_keepalive_mode == "frames"
    policy = StreamKeepalive.from_settings(settings)
    assert policy is not None
    assert policy.frames is True


def test_a_blank_mode_is_the_default_and_an_unknown_one_is_rejected() -> None:
    assert _mode("").stream_keepalive_mode == "ping"
    assert _mode(" FRAMES ").stream_keepalive_mode == "frames"
    with pytest.raises(ValidationError):
        _mode("thinking")


def test_the_frame_is_mccs_own_delta_bytes_with_an_empty_payload() -> None:
    assert empty_delta_frame(0, "text") == TEXT_DELTA_0
    assert empty_delta_frame(3, "tool_use") == (
        "event: content_block_delta\n"
        'data: {"type": "content_block_delta", "index": 3, '
        '"delta": {"type": "input_json_delta", "partial_json": ""}}\n\n'
    )


@pytest.mark.parametrize(
    "block_type",
    ["thinking", "redacted_thinking", "server_tool_use", "web_search_tool_result"],
)
def test_no_frame_exists_for_a_thinking_or_server_block(block_type: str) -> None:
    assert empty_delta_frame(0, block_type) is None


# --------------------------------------------------------------------------
# Where a frame may go, and where only a ping may
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_inside_a_text_block_gets_empty_text_deltas() -> None:
    frames = await _drain(await _anthropic(_paced(TURN, SILENT_IN_TEXT), FRAMES))
    between = frames[frames.index(TURN[6]) + 1 : frames.index(TURN[7])]

    assert len(between) >= 3
    expected = empty_delta_frame(1, "text")
    assert set(between) == {expected}, "the text block's own index and kind"
    assert ANTHROPIC_KEEPALIVE_FRAME not in frames
    assert _content(frames) == "".join(TURN)


@pytest.mark.asyncio
async def test_silence_inside_tool_arguments_gets_empty_input_json_deltas() -> None:
    frames = await _drain(await _anthropic(_paced(TURN, SILENT_IN_TOOL_ARGS), FRAMES))
    between = frames[frames.index(TURN[11]) + 1 : frames.index(TURN[12])]

    assert len(between) >= 3
    assert set(between) == {empty_delta_frame(2, "tool_use")}
    assert _content(frames) == "".join(TURN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pauses", "after"),
    [
        (SILENT_BEFORE_MESSAGE_START, None),
        (SILENT_BEFORE_FIRST_BLOCK, 0),
        (SILENT_IN_THINKING, 2),
        (SILENT_BETWEEN_BLOCKS, 4),
        (SILENT_AFTER_LAST_BLOCK, 13),
    ],
    ids=[
        "before-message_start",
        "before-first-block",
        "inside-thinking",
        "between-blocks",
        "after-last-block",
    ],
)
async def test_anywhere_else_it_falls_back_to_ping(
    pauses: dict[int, float], after: int | None
) -> None:
    """Never a delta outside an open text/tool block; never a thinking delta."""
    frames = await _drain(await _anthropic(_paced(TURN, pauses), FRAMES))
    start = 0 if after is None else frames.index(TURN[after]) + 1
    end = frames.index(TURN[0 if after is None else after + 1])
    silent = frames[start:end]

    assert len(silent) >= 3
    assert set(silent) == {ANTHROPIC_KEEPALIVE_FRAME}
    assert [f for f in frames if f not in TURN] == silent
    assert _content(frames) == "".join(TURN)


@pytest.mark.asyncio
async def test_no_keepalive_ever_mentions_thinking() -> None:
    """Every silence at once: no frame MCC invents names or feeds a thinking block."""
    pauses = dict.fromkeys(range(len(TURN)), 0.15)
    frames = await _drain(await _anthropic(_paced(TURN, pauses), FRAMES))
    invented = _keepalives(frames)

    assert invented, "the rig must actually produce keepalives"
    assert not any("thinking" in frame or "signature" in frame for frame in invented)
    assert _content(frames) == "".join(TURN)


@pytest.mark.asyncio
async def test_the_block_is_tracked_across_fragmented_and_batched_chunks() -> None:
    """A frame split across chunks, and several frames in one chunk."""
    joined = "".join(TURN[:7])
    chunks = [joined[:37], joined[37:250], joined[250:], "".join(TURN[7:])]
    frames = await _drain(await _anthropic(_paced(chunks, {3: 0.3}), FRAMES))

    assert set(frames[3:-1]) == {empty_delta_frame(1, "text")}
    assert _content(frames, chunks) == "".join(TURN)


@pytest.mark.asyncio
async def test_an_unreadable_frame_closes_the_block_rather_than_guess() -> None:
    garbled = [MESSAGE_START, _start(0, {"type": "text", "text": ""})]
    garbled.append("event: content_block_start\ndata: {not json\n\n")
    garbled.append(_delta(0, {"type": "text_delta", "text": "x"}))
    frames = await _drain(await _anthropic(_paced(garbled, {3: 0.3}), FRAMES))

    assert set(frames[3:-1]) == {ANTHROPIC_KEEPALIVE_FRAME}


@pytest.mark.asyncio
async def test_a_block_start_before_message_start_never_earns_a_frame() -> None:
    """Never before ``message_start``: Claude Code has no message to append to."""
    early = [_start(0, {"type": "text", "text": ""}), MESSAGE_START]
    frames = await _drain(await _anthropic(_paced(early, {1: 0.3}), FRAMES))

    assert set(frames[1:-1]) == {ANTHROPIC_KEEPALIVE_FRAME}
    assert frames[-1] == MESSAGE_START


@pytest.mark.asyncio
async def test_other_surfaces_keep_their_sse_comment_in_frames_mode() -> None:
    chunks = ['data: {"id":"c1"}\n\n', "data: [DONE]\n\n"]
    response = await openai_sse_streaming_response(
        _paced(chunks, {1: 0.3}),
        headers={"Cache-Control": "no-cache"},
        pre_start_error_response=_error,
        keepalive=FRAMES,
    )
    frames = await _drain(response)

    assert frames.count(SSE_COMMENT_KEEPALIVE_FRAME) >= 3
    assert "".join(f for f in frames if f != SSE_COMMENT_KEEPALIVE_FRAME) == "".join(
        chunks
    )


@pytest.mark.asyncio
async def test_the_cap_still_applies_in_frames_mode() -> None:
    policy = StreamKeepalive(
        idle_seconds=0.05, interval_seconds=0.05, max_seconds=0.12, frames=True
    )
    frames = await _drain(await _anthropic(_paced(TURN, {7: 0.6}), policy))
    between = frames[frames.index(TURN[6]) + 1 : frames.index(TURN[7])]

    assert 1 <= len(between) <= 2
    assert set(between) == {empty_delta_frame(1, "text")}


# --------------------------------------------------------------------------
# The contract: content, and what Claude Code assembles, are identical
# --------------------------------------------------------------------------

SHAPES = [
    {},
    SILENT_BEFORE_MESSAGE_START,
    SILENT_IN_THINKING,
    SILENT_BETWEEN_BLOCKS,
    SILENT_IN_TEXT,
    SILENT_IN_TOOL_ARGS,
    dict.fromkeys(range(len(TURN)), 0.12),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("pauses", SHAPES)
async def test_concatenated_content_is_byte_identical_with_frames_on_and_off(
    pauses: dict[int, float],
) -> None:
    off = await _drain(await _anthropic(_paced(TURN, pauses), PINGS))
    on = await _drain(await _anthropic(_paced(TURN, pauses), FRAMES))

    assert _content(on).encode("utf-8") == _content(off).encode("utf-8")
    assert _content(on) == "".join(TURN)
    # And with nothing stripped at all: every delta payload the client was
    # sent, keepalives included, concatenates to the same bytes per block.
    assert _concatenated(on) == _concatenated(off) == _concatenated(TURN)
    assert _concatenated(on)[1] == b"Reading the file."
    assert _concatenated(on)[2] == b'{"file_path": "/a.py"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("pauses", SHAPES)
async def test_claude_codes_parse_path_accepts_the_frames_and_assembles_the_same(
    pauses: dict[int, float],
) -> None:
    off = _parse(await _drain(await _anthropic(_paced(TURN, pauses), None)))
    on = _parse(await _drain(await _anthropic(_paced(TURN, pauses), FRAMES)))

    assert on.stopped and off.stopped
    assert on.finished == off.finished
    assert [block["type"] for block in on.finished] == ["thinking", "text", "tool_use"]
    assert on.finished[0]["signature"] == "sig-from-upstream"
    assert on.finished[1]["text"] == "Reading the file."
    assert on.finished[2]["input"] == {"file_path": "/a.py"}


def test_the_parse_model_rejects_what_frames_mode_must_never_write() -> None:
    """The negative control: the transcription is strict where Claude Code is."""
    head = [MESSAGE_START, _start(0, {"type": "text", "text": ""}), _stop(0)]
    with pytest.raises(_StreamBroken, match="closed_delta"):
        _parse([*head, empty_delta_frame(0, "text") or ""])
    with pytest.raises(_StreamBroken, match="not_found_delta"):
        _parse([MESSAGE_START, empty_delta_frame(5, "text") or ""])
    thinking = [MESSAGE_START, _start(0, {"type": "thinking", "thinking": ""})]
    with pytest.raises(_StreamBroken, match="mismatch_text"):
        _parse([*thinking, empty_delta_frame(0, "text") or ""])


@pytest.mark.asyncio
async def test_frames_rearm_claude_codes_timer_where_pings_cannot() -> None:
    """The point of the mode: a silent open block no longer looks dead."""
    pauses = {7: 0.6}

    def longest_gap(timed: list[tuple[float, str]]) -> float:
        parse = _ClaudeCodeParse()
        for at, frame in timed:
            parse.feed(frame, at)
        stamps = parse.timer_rearms
        return max(b - a for a, b in itertools.pairwise(stamps))

    pinged = longest_gap(
        await _drain_timed(await _anthropic(_paced(TURN, pauses), PINGS))
    )
    framed = longest_gap(
        await _drain_timed(await _anthropic(_paced(TURN, pauses), FRAMES))
    )
    assert pinged >= 0.5, "pings are dropped by the SDK: the timer sees the silence"
    assert framed < 0.3, "an empty delta every interval re-arms it"


# --------------------------------------------------------------------------
# Measurements unchanged; the row records the count
# --------------------------------------------------------------------------


def _capture(store: RequestLogStore, request_id: str) -> RequestCapture:
    return RequestCapture(
        store,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="hello",
        params={"max_tokens": 100},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "pauses", "expected"),
    [
        (FRAMES, SILENT_IN_TEXT, "some"),
        (FRAMES, {}, 0),
        (FRAMES, SILENT_BETWEEN_BLOCKS, 0),
        (PINGS, SILENT_IN_TEXT, None),
        (None, SILENT_IN_TEXT, None),
    ],
    ids=["frames-sent", "frames-not-needed", "frames-only-pings", "ping-mode", "off"],
)
async def test_the_row_records_the_frames_and_nothing_else_moves(
    tmp_path,
    policy: StreamKeepalive | None,
    pauses: dict[int, float],
    expected: int | str | None,
) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store, "req_frames_row")
    response = await _anthropic(capture.wrap(_paced(TURN, pauses)), policy)
    frames = await _drain(response)
    store.close()

    row = store.get_request("req_frames_row")
    assert row is not None
    sent = [f for f in _keepalives(frames) if f != ANTHROPIC_KEEPALIVE_FRAME]
    if expected == "some":
        assert len(sent) >= 3
        assert row["keepalive_frames"] == len(sent)
    else:
        assert row["keepalive_frames"] == expected
        assert sent == []
    # The frames never pass through the capture.
    assert row["status"] == "success"
    assert row["output_chars"] == len("Reading the file.")
    assert row["thinking_chars"] == len("Let me look.")
    assert row["ttft_ms"] is not None
    assert capture._saw_terminal_event is True


@pytest.mark.asyncio
async def test_ttft_is_the_models_first_frame_in_frames_mode(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store, "req_frames_ttft")
    await _drain(await _anthropic(capture.wrap(_paced(TURN, {0: 0.25})), FRAMES))
    store.close()

    row = store.get_request("req_frames_ttft")
    assert row is not None
    assert row["ttft_ms"] >= 240
    assert row["keepalive_frames"] == 0


@pytest.mark.asyncio
async def test_a_frame_is_not_counted_as_a_chunk(monkeypatch) -> None:
    counted: list[int] = []
    monkeypatch.setattr(
        response_streams, "note_stream_chunk", lambda: counted.append(1)
    )
    frames = await _drain(await _anthropic(_paced(TURN, SILENT_IN_TEXT), FRAMES))

    assert len(counted) == len(TURN)
    assert len(_keepalives(frames)) >= 3


@pytest.mark.asyncio
async def test_the_openai_surface_records_no_frame_count(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    capture = _capture(store, "req_frames_openai")
    tally = current_keepalive_tally()
    assert tally is not None
    await _drain(
        await openai_sse_streaming_response(
            _paced(['data: {"id":"c1"}\n\n', "data: [DONE]\n\n"], {1: 0.2}),
            headers={},
            pre_start_error_response=_error,
            keepalive=FRAMES,
        )
    )
    assert tally.frames_mode is False
    assert tally.recorded_frames() is None
    del capture
    store.close()


def test_an_older_log_gains_the_column_as_null(tmp_path) -> None:
    path = tmp_path / "requests.db"
    RequestLogStore(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE requests DROP COLUMN keepalive_frames")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    assert "keepalive_frames" not in columns

    store = RequestLogStore(path)
    store.close()
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    assert "keepalive_frames" in columns
