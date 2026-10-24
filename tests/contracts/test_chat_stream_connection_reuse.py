"""7.111.1: a finished Chat Completions stream hands its connection back.

What was wrong: the Chat door's stream (``MccChatChunkStream``, a
transcription of the OpenAI SDK's ``AsyncStream``) stopped at
``data: [DONE]`` and closed the response before its body had ended.
``httpcore`` keeps a connection only when both sides of the exchange are done
and closes it otherwise, so no Chat Completions stream ever reused its
connection -- measured in 7.94.0 as 30 new proxy tunnels for 30 requests with
``PROXY_KEEPALIVE_SECONDS=60``, against 1 on the Responses and Messages doors.

What this pins, on the one door that changed:

* the frames every recorded Chat fixture produces are still exactly the ones
  ``sse_chunk_expected.json`` holds (recorded on v6.62.0), and every fixture
  that finished with ``[DONE]`` now read its body to the end;
* two streams through one real HTTP/1.1 pool share one connection;
* a stream abandoned mid-answer is not read any further (no drain on an
  abandoned request -- ``specs/INVESTIGATION-ABANDONED-UPSTREAM.md``);
* a host that holds its body open after ``[DONE]``, or closes it without the
  terminator, still ends the reply with the same frames, within the bound.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.providers.openai_chat import (
    OpenAIChatProvider,
    chunks,
    create_openai_chat_provider,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.sse_replay import (
    EXPECTED_PATH,
    _build_provider,
    _config,
    _FixedUUID,
    load_fixtures,
)
from tests.providers.support import passthrough_rate_limiter

FIXTURES = load_fixtures()
EXPECTED: dict[str, list[str]] = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


def _frames(body: str) -> list[bytes]:
    """The body cut at every SSE frame boundary, separators kept."""

    pieces = [piece + "\n\n" for piece in body.split("\n\n")]
    pieces[-1] = pieces[-1][:-2]
    return [piece.encode("utf-8") for piece in pieces if piece]


class _RecordingBody(httpx.AsyncByteStream):
    """A response body that says how far it was read and whether it was closed.

    ``gate``: after ``hold_after`` pieces it waits for the event before
    serving the rest -- the moment an abandoned request is cancelled.
    ``after_done``: what follows the last piece -- nothing, a body that never
    ends, or a host closing without the chunked terminator.
    """

    def __init__(
        self,
        pieces: list[bytes],
        *,
        hold_after: int | None = None,
        after_done: str = "end",
    ) -> None:
        self.pieces = pieces
        self.served = 0
        self.exhausted = False
        self.closed = False
        self.hold_after = hold_after
        self.after_done = after_done
        self.waiting = asyncio.Event()
        self.gate = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            if self.hold_after is not None and self.served == self.hold_after:
                self.waiting.set()
                await self.gate.wait()
            self.served += 1
            yield piece
        if self.after_done == "never":
            await asyncio.Event().wait()
        if self.after_done == "cut":
            raise httpx.RemoteProtocolError("peer closed connection mid-body")
        self.exhausted = True

    async def aclose(self) -> None:
        self.closed = True


def _provider_on(name: str, body: _RecordingBody) -> OpenAIChatProvider:
    provider = _build_provider(name)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
            request=request,
        )

    provider._client = AsyncOpenAI(
        api_key="test-key",
        base_url=provider._config.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider


async def _replay(
    fixture: dict[str, Any], body: _RecordingBody
) -> tuple[list[str], _RecordingBody]:
    """``tests/providers/sse_replay.replay`` over a body that records its reads."""

    provider = _provider_on(fixture["provider"], body)
    request = make_messages_request(**fixture.get("request", {}))
    frames: list[str] = []
    try:
        with patch("uuid.uuid4", _FixedUUID()):
            stream = provider.stream_response(request, 11, request_id="req-fixed")
            while True:
                try:
                    frames.append(await anext(stream))
                except StopAsyncIteration:
                    break
                except Exception as error:
                    frames.append(f"!error {type(error).__name__}")
                    break
    finally:
        await provider.cleanup()
    return frames, body


def _finishes_with_done(fixture: dict[str, Any]) -> bool:
    return "data: [DONE]" in fixture["sse"] and not any(
        frame.startswith("!error") for frame in EXPECTED[fixture["name"]]
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f["name"])
@pytest.mark.asyncio
async def test_every_recorded_chat_stream_keeps_its_frames_and_reads_to_the_end(
    fixture,
):
    frames, body = await _replay(fixture, _RecordingBody(_frames(fixture["sse"])))

    assert frames == EXPECTED[fixture["name"]]
    assert body.closed
    if _finishes_with_done(fixture):
        assert body.exhausted, "a stream that ended with [DONE] must read its body out"


def test_the_fixtures_cover_streams_that_finish_with_done():
    finished = [f["name"] for f in FIXTURES if _finishes_with_done(f)]
    assert len(finished) >= 10, finished


@pytest.mark.parametrize("after_done", ["never", "cut"])
@pytest.mark.asyncio
async def test_a_body_that_does_not_end_after_done_changes_nothing_downstream(
    after_done, monkeypatch
):
    """Held open past the bound, or cut without its terminator: same frames."""

    monkeypatch.setattr(chunks, "END_OF_BODY_WAIT_SECONDS", 0.05)
    fixture = next(f for f in FIXTURES if f["name"] == "groq_text_stream")
    body = _RecordingBody(_frames(fixture["sse"]), after_done=after_done)

    frames, body = await asyncio.wait_for(_replay(fixture, body), timeout=10)

    assert frames == EXPECTED[fixture["name"]]
    assert body.closed
    assert not body.exhausted


@pytest.mark.asyncio
async def test_a_stream_abandoned_mid_answer_is_not_read_any_further():
    """The cancellation of a request that is still streaming closes at once."""

    fixture = next(f for f in FIXTURES if f["name"] == "groq_text_stream")
    pieces = _frames(fixture["sse"])
    hold_after = 3
    assert len(pieces) > hold_after + 1
    body = _RecordingBody(pieces, hold_after=hold_after)
    provider = _provider_on(fixture["provider"], body)
    request = make_messages_request(**fixture.get("request", {}))

    async def consume() -> None:
        async for _ in provider.stream_response(request, 11, request_id="req-gone"):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(body.waiting.wait(), timeout=10)
        task.cancel()
        # Let a drain, if there were one, read on: the rest is now available.
        body.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
    finally:
        await provider.cleanup()

    assert body.served == hold_after
    assert body.closed
    assert not body.exhausted


@pytest.mark.asyncio
async def test_closing_an_abandoned_chunk_stream_does_not_read_on():
    """The other way a stream is abandoned: closed while parked at a chunk.

    That is how the Chat door's stream ends when a client hangs up between
    two chunks (the request is closed from the top and the stream is
    finalized, ``INVESTIGATION-ABANDONED-UPSTREAM.md`` ordering B). It must
    close the response at once, whatever is still on its way.
    """

    fixture = next(f for f in FIXTURES if f["name"] == "groq_text_stream")
    pieces = _frames(fixture["sse"])
    body = _RecordingBody(pieces)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=body,
            request=request,
        )

    client = AsyncOpenAI(
        api_key="test-key",
        base_url="http://sse.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        stream = chunks.adopt_chat_stream(
            await client.chat.completions.create(
                model="fixture-model",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
        )
        assert isinstance(stream, chunks.MccChatChunkStream)
        iterator = stream._iterator
        await anext(iterator)
        await anext(iterator)
        served = body.served
        await iterator.aclose()
    finally:
        await client.close()

    assert served < len(pieces)
    assert body.served == served
    assert body.closed
    assert not body.exhausted


async def _chunked_sse_server(
    log: list[str],
) -> tuple[asyncio.Server, int]:
    """A loopback HTTP/1.1 host answering every POST with a chunked SSE stream."""

    payload = next(f for f in FIXTURES if f["name"] == "groq_text_stream")["sse"]

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        connection = f"conn-{uuid.uuid4().hex[:8]}"
        log.append(f"open {connection}")
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError, ConnectionError:
                    break
                length = 0
                for line in head.decode("latin-1").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                await reader.readexactly(length)
                log.append(f"request {connection}")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                )
                for piece in _frames(payload):
                    writer.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.local_serial
@pytest.mark.asyncio
async def test_two_chat_streams_share_one_pooled_connection(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    log: list[str] = []
    server, port = await _chunked_sse_server(log)
    config = replace(_config(), base_url=f"http://127.0.0.1:{port}/v1")
    # The provider's own client, as built for production: the SDK's default
    # HTTP client and pool, nothing substituted.
    provider = create_openai_chat_provider("groq", config, passthrough_rate_limiter())
    request = make_messages_request(thinking={"enabled": False})
    try:
        answers = []
        for index in range(2):
            frames = [
                frame
                async for frame in provider.stream_response(
                    request, 11, request_id=f"req-{index}"
                )
            ]
            answers.append(frames)
    finally:
        await provider.cleanup()
        server.close()
        await server.wait_closed()

    assert all(any("message_stop" in frame for frame in a) for a in answers)
    opened = [line for line in log if line.startswith("open")]
    requests = [line for line in log if line.startswith("request")]
    assert len(requests) == 2
    assert len(opened) == 1, log
