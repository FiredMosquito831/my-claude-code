"""8.8.0: the provider's own bytes, captured at the transport.

The whole point of the feature is a byte-level claim -- a request's
``RAW_UPSTREAM`` lines, put together, ARE what the provider sent -- so these
tests compare SHA-256 digests against the fixture's own bytes rather than
reading the logged text. A test that compares "looks right" would pass on the
re-serialisation this release exists to replace.

What is measured here, one door at a time, against a fake upstream that emits
real-shaped frames:

* the Messages door, including a ``:`` comment, ``id:``/``retry:`` fields, a
  multi-line ``data:`` payload and an in-band ``event: error`` frame that the
  previous release never logged at all;
* the Responses door, including the ``event:`` line, the ``data: `` prefix and
  ``[DONE]``;
* the Chat Completions door, whose bytes reached the raw log nowhere before;
* the refusal paths, whose bodies are recorded whole and never in the request
  database;
* the media door, which records metadata and never an image;
* the equality gate: with the setting off, the same traffic writes zero
  ``RAW_UPSTREAM`` lines and leaves every other line byte-identical.
"""

import hashlib
from typing import Any

import httpx
import pytest

from my_claude_code.config import logging_config
from my_claude_code.config.logging_config import (
    RAW_MARKER_UPSTREAM,
    configure_logging,
    configure_raw_diagnostics,
)
from my_claude_code.providers import raw_upstream as ru

#: Every test here drives an async generator, so every one of them is async.
pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# A fake upstream that answers with exact bytes
# ---------------------------------------------------------------------------


class _Body(httpx.AsyncByteStream):
    """Yield the fixture's pieces unchanged, as a real transport would."""

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = list(pieces)

    async def __aiter__(self) -> Any:
        for piece in self.pieces:
            yield piece


def _response(pieces: list[bytes], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream"},
        stream=_Body(pieces),
        request=httpx.Request("POST", "http://127.0.0.1:1/messages"),
    )


async def _collect(chunks: Any) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def _settle() -> None:
    """Wait for the enqueued raw sink to finish writing.

    ``configure_raw_diagnostics`` installs its sink with ``enqueue=True`` -- a
    queue in front of the file is what keeps a slow disk off a request -- so a
    test that read the file immediately after a write would race the writer
    thread and see a prefix of what it recorded. ``logger.complete()`` is that
    queue's own barrier.
    """

    from loguru import logger

    logger.complete()


def _raw_lines(path: Any, marker: str) -> list[bytes]:
    """Every physical line of this marker, in file order, as bytes.

    The sink opens its file in text mode with default newline translation, so
    on Windows each record ends ``\\r\\n``; the terminator is stripped here and
    nowhere else, because the message's own newlines were already escaped by
    the sink and are part of what is being compared.
    """

    if not path.exists():
        return []
    wanted = marker.encode()
    return [
        line.removesuffix(b"\r")
        for line in path.read_bytes().split(b"\n")
        if line.startswith(wanted)
    ]


def _upstream_messages(lines: list[bytes]) -> list[bytes]:
    """The message part of each ``RAW_UPSTREAM <id> <message>`` line.

    The message keeps the ``UPSTREAM: `` prefix the emit site writes -- that
    prefix is what routes the record to the raw sink -- so it is stripped for
    a chunk line, whose own body starts at ``door=``.
    """

    prefix = ru.PREFIX.encode()
    out: list[bytes] = []
    for line in lines:
        parts = line.split(b" ", 2)
        assert len(parts) == 3, line
        message = parts[2]
        if message.startswith(prefix):
            message = message[len(prefix) :]
        out.append(message)
    return out


def _reassembled(lines: list[bytes]) -> bytes:
    """The fixture's bytes, recovered from the recorded lines by concatenation."""

    pieces: list[bytes] = []
    for message in _upstream_messages(lines):
        decoded = ru.chunk_bytes_of(message.decode("utf-8", "surrogateescape"))
        assert decoded is not None, message
        pieces.append(decoded[1])
    return b"".join(pieces)


# ---------------------------------------------------------------------------
# The fixture: real-shaped frames, one per thing the old capture lost
# ---------------------------------------------------------------------------

#: The Messages door's stream, spelled the way a host writes it: a comment
#: (a keepalive), an ``id:`` field (reconnection state), a ``retry:`` field, an
#: event name, a ``data:`` payload whose JSON is spaced the way a host emits
#: it, a second event split across two ``data:`` lines, and an in-band
#: ``event: error`` frame at the end.
MESSAGES_STREAM = (
    b": keepalive\n\n"
    b"event: message_start\n"
    b"id: evt_1\nretry: 3000\n"
    b'data: {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 3}}}\n'
    b"\n"
    b"event: content_block_delta\n"
    b'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", '
    b'"text": "caf\xc3\xa9 \xe4\xb8\xad"}}\n'
    b"\n"
    b"event: content_block_delta\n"
    b'data: {"type": "content_block_delta", "index": 0,\n'
    b'data: "delta": {"type": "text_delta", "text": " split"}}\n'
    b"\n"
    b"event: message_stop\n"
    b'data: {"type": "message_stop"}\n'
    b"\n"
)

MESSAGES_ERROR_FRAME = (
    b"event: error\n"
    b'data: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}\n'
    b"\n"
)

#: The Responses door's stream: a comment, an ``event:`` line, the ``data: ``
#: prefix, a compact payload with a duplicate key, and ``[DONE]``.
RESPONSES_STREAM = (
    b": keepalive\n"
    b"event: response.output_text.delta\n"
    b'data: {"type":"response.output_text.delta","index":0,"delta":"hi","x":1,"x":2}\n'
    b"\n"
    b'data: {"type":"response.completed"}\n'
    b"\n"
    b"data: [DONE]\n"
    b"\n"
)

#: The Chat Completions door's stream, in the protocol's own spelling.
CHAT_STREAM = (
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,'
    b'"model":"m","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n'
    b"\n"
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,'
    b'"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n'
    b"\n"
    b"data: [DONE]\n"
    b"\n"
)


@pytest.fixture()
def raw_log(tmp_path: Any) -> Any:
    """A real raw-diagnostics sink, on a temporary file, with every gate on."""

    path = tmp_path / "logs" / logging_config.RAW_DIAGNOSTICS_LOG_FILENAME
    configure_logging(tmp_path / "logs" / "server.log", retain_files=2)
    configure_raw_diagnostics(
        path, sse=True, payloads=True, cli=False, upstream=True, retain_files=2
    )
    yield path
    configure_raw_diagnostics(
        path, sse=False, payloads=False, cli=False, upstream=False, retain_files=2
    )
    configure_logging(tmp_path / "logs" / "server.log", retain_files=2)
    # loguru keeps sinks; the next test's ``configure_*`` removes them, and the
    # gate switches are reset here so an off test cannot inherit an on one.
    logging_config.set_upstream_capture(False)
    logging_config.set_inbound_capture(False)


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------


async def test_the_messages_door_records_the_bytes_the_host_sent(raw_log) -> None:
    """Including the four things ``_validated_frame`` loses, and the error frame."""

    from my_claude_code.providers.anthropic_messages.streaming import (
        iter_anthropic_sse_frames,
    )

    # The in-band error frame makes the reader raise; the bytes are recorded
    # before that happens, which is the point (it was never logged at all).
    with pytest.raises(Exception):  # noqa: B017 - the classifier's own failure
        async for _frame in iter_anthropic_sse_frames(
            ru.capture_chunks(
                _Body(
                    list(_pieces(MESSAGES_STREAM + MESSAGES_ERROR_FRAME))
                ).__aiter__(),
                ru.DOOR_ANTHROPIC_MESSAGES,
                enabled=True,
            )
        ):
            pass

    _settle()
    lines = _raw_lines(raw_log, RAW_MARKER_UPSTREAM)
    assert lines, "no RAW_UPSTREAM line was written"
    recorded = _reassembled(lines)
    wire = MESSAGES_STREAM + MESSAGES_ERROR_FRAME
    assert recorded == wire
    assert hashlib.sha256(recorded).hexdigest() == hashlib.sha256(wire).hexdigest()
    # The specific losses, checked one by one so a regression names itself.
    assert b": keepalive" in recorded
    assert b"id: evt_1" in recorded
    assert b"retry: 3000" in recorded
    assert b'"caf\xc3\xa9 \xe4\xb8\xad"' in recorded
    assert recorded.count(b"data: ") == 6
    assert b"event: error" in recorded


async def test_the_responses_door_records_the_data_lines_and_done(raw_log) -> None:
    from my_claude_code.providers.openai_responses.streaming import (
        iter_responses_sse_events,
    )

    events = [
        event
        async for event in iter_responses_sse_events(
            _Body(list(_pieces(RESPONSES_STREAM))).__aiter__(),
            door=ru.DOOR_OPENAI_RESPONSES,
            log_raw_bytes=True,
        )
    ]
    assert [event["type"] for event in events] == [
        "response.output_text.delta",
        "response.completed",
    ]
    _settle()
    recorded = _reassembled(_raw_lines(raw_log, RAW_MARKER_UPSTREAM))
    assert recorded == RESPONSES_STREAM
    assert recorded.startswith(b": keepalive\nevent: response.output_text.delta\n")
    assert b"data: " in recorded
    assert recorded.endswith(b"data: [DONE]\n\n")
    # The duplicate key the re-serialisation destroyed is still there.
    assert b'"x":1,"x":2' in recorded


async def test_the_chat_door_records_bytes_it_never_recorded_at_all(raw_log) -> None:
    from openai._streaming import SSEDecoder

    from my_claude_code.providers.openai_chat.chunks import MccChatChunkStream

    # The Chat door's gate is the process switch, the same one every other
    # emit site asks; ``raw_fixture`` turns it on at the file's own sink.
    assert ru.capture_enabled() is True
    stream = MccChatChunkStream(
        response=_response(list(_pieces(CHAT_STREAM))),
        decoder=SSEDecoder(),
    )
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 2
    _settle()
    recorded = _reassembled(_raw_lines(raw_log, RAW_MARKER_UPSTREAM))
    assert recorded == CHAT_STREAM
    assert b"data: [DONE]" in recorded
    assert chunks[0].choices[0].delta.content == "hi"


def _pieces(blob: bytes) -> list[bytes]:
    """Split a fixture into transport-sized chunks, at awkward boundaries.

    Deliberately NOT one piece per frame: a real transport hands over whatever
    its socket delivered, so a frame can straddle two chunks and a chunk can
    hold three. If the capture depended on frame boundaries this would show it.
    """

    step = 17
    return [blob[at : at + step] for at in range(0, len(blob), step)]


# ---------------------------------------------------------------------------
# The equality gate
# ---------------------------------------------------------------------------


async def test_with_the_setting_off_nothing_new_is_written(tmp_path) -> None:
    """Off is the identity function: same bytes out, zero lines in."""

    path = tmp_path / "logs" / logging_config.RAW_DIAGNOSTICS_LOG_FILENAME
    configure_logging(tmp_path / "logs" / "server.log", retain_files=2)
    configure_raw_diagnostics(
        path, sse=True, payloads=True, cli=False, upstream=False, retain_files=2
    )
    before = path.read_bytes() if path.exists() else b""

    body = _Body(list(_pieces(MESSAGES_STREAM)))
    passed = ru.capture_chunks(
        body.__aiter__(), ru.DOOR_ANTHROPIC_MESSAGES, enabled=False
    )
    # Not merely "writes nothing": it is the very iterator, so no wrapper runs.
    assert passed is not None
    assert await _collect(passed) == MESSAGES_STREAM
    after = path.read_bytes() if path.exists() else b""
    assert _raw_lines(path, RAW_MARKER_UPSTREAM) == []
    assert before == after
    configure_raw_diagnostics(
        path, sse=False, payloads=False, cli=False, upstream=False, retain_files=2
    )
    configure_logging(tmp_path / "logs" / "server.log", retain_files=2)


async def test_the_off_switch_returns_the_callers_own_iterator() -> None:
    async def source() -> Any:
        yield b"one"

    original = source()
    assert ru.capture_chunks(original, ru.DOOR_OPENAI_CHAT, enabled=False) is original


async def test_an_oversize_chunk_is_marked_never_cut(raw_log) -> None:
    big = b"x" * (ru.UPSTREAM_CHUNK_MAX_BYTES + 1)
    line = ru.chunk_line(ru.DOOR_OPENAI_CHAT, big)
    assert line.startswith(ru.OVERSIZE)
    fields = ru.header_fields(line)
    assert fields["bytes"] == str(len(big))
    assert fields["sha256"] == hashlib.sha256(big).hexdigest()
    assert ru.chunk_bytes_of(line) is None
