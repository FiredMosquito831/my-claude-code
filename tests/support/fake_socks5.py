"""A local SOCKS5 listener that can misbehave on purpose, and a target for it.

The rig for the unbounded-handshake defect. Every behaviour here is something a
public SOCKS5 address in an operator's chain has actually been observed to do,
and the point of each is what ``httpcore`` does *next*:

``honest``
    Speaks SOCKS5 correctly and relays to :class:`FakeUpstream`. The control:
    whatever bounds are installed must leave this one unchanged.
``silent``
    Accepts the TCP connection and never answers the greeting. This is the
    defect's own shape -- the read on ``socks_proxy.py`` line 62 that has no
    deadline.
``greet_then_stall``
    Answers the greeting and never answers the ``CONNECT`` (line 97).
``dribble``
    Sends one byte of the greeting reply every ``dribble_interval`` seconds, so
    the socket is never idle and no keepalive or OS-level timer fires.
``close_mid``
    Answers the greeting and closes the socket.
``bad_auth``
    Answers the greeting with ``NO ACCEPTABLE METHODS``.

Nothing here reaches the network: both servers bind ``127.0.0.1`` on port 0.
"""

import asyncio
import contextlib

HONEST = "honest"
SILENT = "silent"
GREET_THEN_STALL = "greet_then_stall"
DRIBBLE = "dribble"
CLOSE_MID = "close_mid"
BAD_AUTH = "bad_auth"

#: The six rig behaviours, in the order the defect report lists them.
BEHAVIOURS = (HONEST, SILENT, GREET_THEN_STALL, DRIBBLE, CLOSE_MID, BAD_AUTH)

#: ``VER=5, METHOD=NO AUTHENTICATION REQUIRED``.
_GREETING_OK = b"\x05\x00"
#: ``VER=5, METHOD=NO ACCEPTABLE METHODS``.
_GREETING_NO = b"\x05\xff"
#: ``VER=5, REP=succeeded, RSV, ATYP=IPv4, 0.0.0.0:0``.
_CONNECT_OK = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"


class FakeUpstream:
    """A minimal HTTP/1.1 origin: one fixed answer, no keep-alive surprises."""

    def __init__(self, body: bytes = b"rig") -> None:
        self._body = body
        self._server: asyncio.Server | None = None
        self.port = 0
        self.requests = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            with contextlib.suppress(asyncio.IncompleteReadError):
                await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"content-type: text/plain\r\n"
                b"content-length: " + str(len(self._body)).encode("ascii") + b"\r\n"
                b"connection: close\r\n\r\n" + self._body
            )
            with contextlib.suppress(Exception):
                await writer.drain()
        finally:
            await _shut(writer)

    async def stop(self) -> None:
        await _close_server(self._server)
        self._server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


class FakeSocks5Server:
    """A SOCKS5 listener whose behaviour is chosen by the test."""

    def __init__(
        self,
        behaviour: str = HONEST,
        *,
        upstream_port: int = 0,
        dribble_interval: float = 2.0,
    ) -> None:
        self.behaviour = behaviour
        self.upstream_port = upstream_port
        self.dribble_interval = dribble_interval
        self.accepted = 0
        self._server: asyncio.Server | None = None
        self._live: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.accepted += 1
        self._live.add(writer)
        handler = asyncio.current_task()
        if handler is not None:
            # Four of the six behaviours park for ever on purpose. Held so
            # ``stop`` can cancel them instead of leaving the loop to complain
            # that a task was destroyed while pending.
            self._handlers.add(handler)
        try:
            await self._converse(reader, writer)
        except (
            ConnectionResetError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
            asyncio.CancelledError,
        ):
            return
        finally:
            self._live.discard(writer)
            if handler is not None:
                self._handlers.discard(handler)
            await _shut(writer)

    async def _converse(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self.behaviour == SILENT:
            # Not even the greeting is read: the client is left holding a
            # socket that is open and will never say anything.
            await asyncio.Event().wait()
            return

        await _read_greeting(reader)

        if self.behaviour == BAD_AUTH:
            writer.write(_GREETING_NO)
            await writer.drain()
            return

        if self.behaviour == DRIBBLE:
            for byte in (_GREETING_OK[:1], _GREETING_OK[1:]):
                await asyncio.sleep(self.dribble_interval)
                writer.write(byte)
                await writer.drain()
            await asyncio.Event().wait()
            return

        writer.write(_GREETING_OK)
        await writer.drain()

        if self.behaviour == CLOSE_MID:
            return

        await _read_connect(reader)

        if self.behaviour == GREET_THEN_STALL:
            await asyncio.Event().wait()
            return

        # honest
        writer.write(_CONNECT_OK)
        await writer.drain()
        await self._relay(reader, writer)

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        up_reader, up_writer = await asyncio.open_connection(
            "127.0.0.1", self.upstream_port
        )
        try:
            await asyncio.gather(
                _pump(reader, up_writer),
                _pump(up_reader, writer),
            )
        finally:
            await _shut(up_writer)

    async def stop(self) -> None:
        for handler in tuple(self._handlers):
            handler.cancel()
        for handler in tuple(self._handlers):
            with contextlib.suppress(BaseException):
                await handler
        self._handlers.clear()
        for writer in tuple(self._live):
            await _shut(writer)
        self._live.clear()
        await _close_server(self._server)
        self._server = None


async def _read_greeting(reader: asyncio.StreamReader) -> None:
    header = await reader.readexactly(2)
    await reader.readexactly(header[1])


async def _read_connect(reader: asyncio.StreamReader) -> None:
    header = await reader.readexactly(4)
    kind = header[3:4]
    if kind == b"\x01":
        await reader.readexactly(4 + 2)
    elif kind == b"\x04":
        await reader.readexactly(16 + 2)
    elif kind == b"\x03":
        length = await reader.readexactly(1)
        await reader.readexactly(length[0] + 2)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()


async def _shut(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 1.0)
    except BaseException:
        return


async def _close_server(server: asyncio.Server | None) -> None:
    if server is None:
        return
    try:
        server.close()
        await asyncio.wait_for(server.wait_closed(), 2.0)
    except BaseException:
        return
