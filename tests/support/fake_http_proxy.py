"""An HTTP ``CONNECT`` proxy that stalls, to show the rung that was never broken.

The SOCKS5 handshake in ``httpcore`` 1.0.9 runs with no deadline; the HTTP
``CONNECT`` tunnel does not, because ``httpcore`` writes it through an ordinary
HTTP/1.1 connection that carries the request's own ``read`` timeout. That
difference is the reason this PR touches one rung type and not the other, so it
is worth a listener rather than a claim: this one accepts the connection, reads
the ``CONNECT`` line and never answers it.
"""

import asyncio
import contextlib

HONEST = "honest"
SILENT = "silent"


class FakeHttpProxy:
    """A ``CONNECT`` proxy that either tunnels honestly or never answers."""

    def __init__(self, behaviour: str = HONEST, *, upstream_port: int = 0) -> None:
        self.behaviour = behaviour
        self.upstream_port = upstream_port
        self.accepted = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._live: set[asyncio.StreamWriter] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.accepted += 1
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)
        self._live.add(writer)
        try:
            with contextlib.suppress(asyncio.IncompleteReadError):
                await reader.readuntil(b"\r\n\r\n")
            if self.behaviour == SILENT:
                await asyncio.Event().wait()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await self._relay(reader, writer)
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

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        up_reader, up_writer = await asyncio.open_connection(
            "127.0.0.1", self.upstream_port
        )
        try:
            await asyncio.gather(_pump(reader, up_writer), _pump(up_reader, writer))
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
        if self._server is not None:
            try:
                self._server.close()
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except BaseException:
                pass
            self._server = None


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
