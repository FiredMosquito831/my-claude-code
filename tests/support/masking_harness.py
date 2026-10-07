"""A provider host and proxies that record where every connection came from.

The rig behind ``tests/contracts/test_provider_traffic_is_masked.py``: the
contract that a provider reached through a proxy chain never sees this
computer's own address, for any kind of traffic. Built to be extended -- each
masking fix adds its traffic class to that test and drives it against this rig.

Three listeners on ``127.0.0.1``, each on its own port, all running on one
event loop in a background thread so a synchronous ``TestClient`` test and an
``async`` test can both use them:

:class:`FakeProviderHost`
    Plain HTTP/1.1 origin. Answers every request from a responder the test
    supplies, and records the peer ``(ip, port)`` of every connection and the
    exact bytes of every request.
:class:`RecordingSocks5Proxy` and :class:`RecordingHttpProxy`
    A SOCKS5 proxy (no auth; domain, IPv4 and IPv6 targets) and an HTTP proxy
    (``CONNECT`` tunnels and absolute-form forwarding). Each records the target
    every client asked for and the local ``(ip, port)`` of every socket it
    opened onward -- which is exactly what the host records as its peer when
    the traffic really went through the proxy.

The provider's hostname is :data:`FAKE_PROVIDER_HOST`. The proxies resolve it
themselves, the way a real proxy does for ``socks5h`` and ``CONNECT``; this
computer must never look it up. :class:`DnsGuard` patches
``socket.getaddrinfo`` to record every local lookup of it and fail it, so a
request that skipped the proxy shows up as a recorded lookup (and no
connection) rather than silently succeeding. A test that is *meant* to go
direct turns the guard to ``answer`` and the lookup resolves to the host.

So the assertion every row makes is two sets: every peer the host saw is one of
the proxies' onward sockets, and the guard saw no lookup.
"""

import asyncio
import contextlib
import ipaddress
import socket
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: The provider's hostname in every rig URL. ``.test`` is reserved (RFC 2606)
#: and never resolves on a real network.
FAKE_PROVIDER_HOST = "fake-provider.test"

#: How long any one rig operation may take before the test fails instead of
#: hanging. Generous: everything here is loopback.
RIG_TIMEOUT_SECONDS = 10.0

_GREETING_OK = b"\x05\x00"
_CONNECT_OK = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
_HOST_UNREACHABLE = b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00"

Address = tuple[str, int]


@dataclass(frozen=True, slots=True)
class SeenRequest:
    """One request as the provider host received it, byte for byte."""

    peer: Address
    method: str
    target: str
    path: str
    headers: dict[str, str]
    head: bytes
    body: bytes


#: What the host answers: ``(status, json bytes)``.
Responder = Callable[[SeenRequest], tuple[int, bytes]]


def _ok_responder(request: SeenRequest) -> tuple[int, bytes]:
    return 200, b'{"id":"rig","object":"chat.completion","choices":[]}'


class _LoopThread:
    """One event loop in a daemon thread, driven synchronously from a test."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="masking-rig", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(RIG_TIMEOUT_SECONDS)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(RIG_TIMEOUT_SECONDS)
        if not self._thread.is_alive():
            self.loop.close()


class _Listener:
    """Shared start/stop for the three listeners."""

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._lock = threading.Lock()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler = asyncio.current_task()
        if handler is not None:
            self._handlers.add(handler)
        try:
            await self._serve(reader, writer)
        except ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError:
            return
        finally:
            if handler is not None:
                self._handlers.discard(handler)
            await _shut(writer)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        for handler in tuple(self._handlers):
            handler.cancel()
        for handler in tuple(self._handlers):
            with contextlib.suppress(BaseException):
                await handler
        self._handlers.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None


class FakeProviderHost(_Listener):
    """A plain-HTTP origin that records who connected and what they sent."""

    def __init__(self, responder: Responder | None = None) -> None:
        super().__init__()
        self.responder: Responder = responder or _ok_responder
        self._peers: list[Address] = []
        self._requests: list[SeenRequest] = []

    def base_url(self, host: str = FAKE_PROVIDER_HOST, path: str = "/v1") -> str:
        return f"http://{host}:{self.port}{path}"

    @property
    def peers(self) -> list[Address]:
        """The peer of every connection accepted, in order."""

        with self._lock:
            return list(self._peers)

    @property
    def requests(self) -> list[SeenRequest]:
        with self._lock:
            return list(self._requests)

    def clear(self) -> None:
        with self._lock:
            self._peers.clear()
            self._requests.clear()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = _address(writer.get_extra_info("peername"))
        with self._lock:
            self._peers.append(peer)
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                return
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, _version = request_line.split(" ", 2)
            headers: dict[str, str] = {}
            for line in header_lines:
                if line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            body = await reader.readexactly(length) if length else b""
            path = urlsplit(target).path if "://" in target else target
            seen = SeenRequest(
                peer=peer,
                method=method,
                target=target,
                path=path.split("?", 1)[0],
                headers=headers,
                head=head,
                body=body,
            )
            with self._lock:
                self._requests.append(seen)
            status, payload = self.responder(seen)
            writer.write(
                f"HTTP/1.1 {status} RIG\r\n"
                "content-type: application/json\r\n"
                f"content-length: {len(payload)}\r\n\r\n".encode("ascii")
                + payload
            )
            await writer.drain()


class _RecordingProxy(_Listener):
    """What both proxies record, and how they resolve the provider's name."""

    def __init__(self, names: dict[str, str] | None = None) -> None:
        super().__init__()
        self._names = {FAKE_PROVIDER_HOST: "127.0.0.1"} | dict(names or {})
        self._targets: list[Address] = []
        self._outbound: list[Address] = []
        self.accepted = 0

    @property
    def url(self) -> str:
        """The address a chain entry names, scheme included."""

        raise NotImplementedError

    @property
    def targets(self) -> list[Address]:
        """Every ``(host, port)`` a client asked this proxy to reach."""

        with self._lock:
            return list(self._targets)

    @property
    def outbound(self) -> list[Address]:
        """The local address of every socket this proxy opened onward."""

        with self._lock:
            return list(self._outbound)

    def clear(self) -> None:
        with self._lock:
            self._targets.clear()
            self._outbound.clear()
            self.accepted = 0

    def _resolve(self, host: str) -> str | None:
        with contextlib.suppress(ValueError):
            return str(ipaddress.ip_address(host))
        return self._names.get(host.lower())

    async def _open(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        with self._lock:
            self._targets.append((host, port))
        address = self._resolve(host)
        if address is None:
            return None
        up_reader, up_writer = await asyncio.open_connection(address, port)
        with self._lock:
            self._outbound.append(_address(up_writer.get_extra_info("sockname")))
        return up_reader, up_writer


class RecordingSocks5Proxy(_RecordingProxy):
    """A SOCKS5 proxy that resolves the provider's hostname itself."""

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with self._lock:
            self.accepted += 1
        greeting = await reader.readexactly(2)
        await reader.readexactly(greeting[1])
        writer.write(_GREETING_OK)
        await writer.drain()
        request = await reader.readexactly(4)
        kind = request[3]
        if kind == 1:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif kind == 4:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            size = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(size)).decode("idna")
        port = int.from_bytes(await reader.readexactly(2), "big")
        opened = await self._open(host, port)
        if opened is None:
            writer.write(_HOST_UNREACHABLE)
            await writer.drain()
            return
        writer.write(_CONNECT_OK)
        await writer.drain()
        await _relay(reader, writer, *opened)


class RecordingHttpProxy(_RecordingProxy):
    """An HTTP proxy: ``CONNECT`` tunnels and absolute-form forwarding."""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with self._lock:
            self.accepted += 1
        head = await reader.readuntil(b"\r\n\r\n")
        method, target, _version = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            opened = await self._open(host.strip("[]"), int(port))
            if opened is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _relay(reader, writer, *opened)
            return
        parts = urlsplit(target)
        opened = await self._open(parts.hostname or "", parts.port or 80)
        if opened is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n")
            await writer.drain()
            return
        up_reader, up_writer = opened
        up_writer.write(head)
        await up_writer.drain()
        await _relay(reader, writer, up_reader, up_writer)


class DnsGuard:
    """Records every local lookup of the provider's hostname.

    ``mode`` is ``"fail"`` (the default: a lookup is a leak, so it fails the
    connection and is recorded) or ``"answer"`` (a test that is meant to go
    direct: the lookup is recorded and resolves to the host on ``127.0.0.1``).
    """

    def __init__(self, names: tuple[str, ...] = (FAKE_PROVIDER_HOST,)) -> None:
        self._names = frozenset(name.lower() for name in names)
        self.mode = "fail"
        self._lookups: list[str] = []
        self._lock = threading.Lock()
        self._real = socket.getaddrinfo

    @property
    def lookups(self) -> list[str]:
        with self._lock:
            return list(self._lookups)

    def clear(self) -> None:
        with self._lock:
            self._lookups.clear()

    def getaddrinfo(self, host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if name.lower() in self._names:
            with self._lock:
                self._lookups.append(name)
            if self.mode != "answer":
                raise socket.gaierror(
                    socket.EAI_NONAME,
                    f"{name} was looked up on this computer: a DNS leak",
                )
            return self._real("127.0.0.1", *args, **kwargs)
        return self._real(host, *args, **kwargs)


@dataclass
class MaskingRig:
    """The host, two SOCKS5 proxies, one HTTP proxy and the DNS guard.

    ``proxies`` is in chain order: SOCKS5, HTTP, SOCKS5 -- both proxy kinds the
    chain store accepts, so a row exercises both.
    """

    host: FakeProviderHost
    proxies: tuple[_RecordingProxy, ...]
    dns: DnsGuard
    _loop: _LoopThread = field(repr=False)

    @property
    def proxy_urls(self) -> tuple[str, ...]:
        return tuple(proxy.url for proxy in self.proxies)

    def proxy_outbound(self) -> set[Address]:
        return {address for proxy in self.proxies for address in proxy.outbound}

    def direct_peers(self) -> list[Address]:
        """Peers the host saw that are no proxy's onward socket: direct dials."""

        through = self.proxy_outbound()
        return [peer for peer in self.host.peers if peer not in through]

    def clear(self) -> None:
        self.host.clear()
        for proxy in self.proxies:
            proxy.clear()
        self.dns.clear()

    def assert_masked(self) -> None:
        """Something reached the host, all of it through a proxy, no lookup."""

        # The lookup first: a request that skipped the proxy fails right there
        # (the guard refuses it), so it shows up as a lookup and no connection.
        assert self.dns.lookups == [], (
            f"the provider's hostname was looked up locally: {self.dns.lookups}"
        )
        assert self.direct_peers() == [], (
            f"connections from this computer's own address: {self.direct_peers()}"
        )
        assert self.host.peers, "nothing reached the provider host at all"

    def assert_nothing_sent(self) -> None:
        """Not one connection to the host, through a proxy or otherwise."""

        assert self.host.peers == [], f"the host saw {self.host.peers}"
        assert self.dns.lookups == [], f"local lookups: {self.dns.lookups}"
        for proxy in self.proxies:
            assert proxy.targets == [], f"{proxy.url} was asked for {proxy.targets}"

    def close(self) -> None:
        for listener in (self.host, *self.proxies):
            with contextlib.suppress(BaseException):
                self._loop.call(listener.stop())
        self._loop.close()


def start_masking_rig(responder: Responder | None = None) -> MaskingRig:
    """Start the host and the three proxies; the caller installs the guard."""

    loop = _LoopThread()
    host = FakeProviderHost(responder)
    proxies: tuple[_RecordingProxy, ...] = (
        RecordingSocks5Proxy(),
        RecordingHttpProxy(),
        RecordingSocks5Proxy(),
    )
    try:
        for listener in (host, *proxies):
            loop.call(listener.start())
    except BaseException:
        loop.close()
        raise
    return MaskingRig(host=host, proxies=proxies, dns=DnsGuard(), _loop=loop)


def closed_port() -> int:
    """A loopback port nothing listens on: a proxy that refuses every dial."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _address(raw: Any) -> Address:
    return str(raw[0]), int(raw[1])


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    try:
        await asyncio.gather(_pump(reader, up_writer), _pump(up_reader, writer))
    finally:
        await _shut(up_writer)


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(Exception):
        writer.write_eof()


async def _shut(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    with contextlib.suppress(BaseException):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 1.0)


__all__ = [
    "FAKE_PROVIDER_HOST",
    "RIG_TIMEOUT_SECONDS",
    "DnsGuard",
    "FakeProviderHost",
    "MaskingRig",
    "RecordingHttpProxy",
    "RecordingSocks5Proxy",
    "SeenRequest",
    "closed_port",
    "start_masking_rig",
]
