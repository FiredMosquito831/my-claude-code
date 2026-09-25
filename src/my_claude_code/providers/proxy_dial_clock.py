"""Time the connect and the tunnel handshake of every proxied connection.

A proxy dial has two phases before a single byte of the request leaves: the
TCP connect to the proxy, and the tunnel it then negotiates -- the SOCKS5
greeting and ``CONNECT`` for a ``socks5``/``socks5h`` address, the HTTP
``CONNECT`` exchange for an ``http`` one. Until now neither was measured
anywhere: a try's ``upstream_ms`` starts before the connect and ends at the
response head, so a slow proxy and a slow model read the same.

What this does
--------------

:func:`clock_proxy_pool` wraps one proxy pool's network backend. Its
``connect_tcp`` times the real connect, reports it, and hands back the real
stream inside :class:`_DialClockStream`, which reports the handshake the moment
the tunnel is ready and then gets out of the way. Both figures go to
``core/upstream_ladder``, which files them on the proxy dial in flight -- and
does nothing at all outside a logged request, or when no dial is open.

It is a stopwatch and nothing else:

* **Every byte is forwarded unchanged**, with the caller's own timeout. No
  deadline, no retry, no exception of its own; a failure in the real stream
  propagates as the identical object.
* **It leaves the path at TLS.** ``start_tls`` returns the library's own TLS
  stream, not a wrapped one, so for an ``https`` origin -- every model API --
  nothing here is between the request and the socket once the tunnel is up.
* **Un-proxied clients are untouched**: only a pool that is a proxy pool is
  ever passed in.

When the handshake is over
--------------------------

``start_tls`` ends it for both proxy kinds: ``httpcore`` upgrades to TLS on the
tunnel the moment the proxy has agreed to open it. For a SOCKS pool and a plain
``http`` origin there is no TLS, and the first ``get_extra_info`` after the
negotiation's own reads is the end instead -- ``httpcore`` asks the stream
about ALPN exactly then. An HTTP pool does not get that second rule, because
``httpcore`` asks the same question there *before* it sends ``CONNECT``; for a
forwarding HTTP proxy with a plain ``http`` origin there is no tunnel and no
handshake to report.
"""

import ssl
import time
import typing

import httpcore

from my_claude_code.core.upstream_ladder import (
    record_proxy_connect,
    record_proxy_handshake,
)

#: The private attribute ``httpcore`` keeps a pool's backend under. Pinned,
#: with the SOCKS module's names, by ``tests/contracts``.
POOL_BACKEND_ATTR = "_network_backend"


class _DialClockStream(httpcore.AsyncNetworkStream):
    """A stream that reports when its tunnel became ready, once."""

    def __init__(
        self, stream: httpcore.AsyncNetworkStream, *, info_ends_handshake: bool
    ) -> None:
        self._stream = stream
        self._info_ends_handshake = info_ends_handshake
        self._since: float | None = time.monotonic()
        self._spoke = False

    def _tunnel_ready(self) -> None:
        since = self._since
        if since is None:
            return
        self._since = None
        record_proxy_handshake(time.monotonic() - since)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._spoke = True
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._spoke = True
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Measured before the upgrade: TLS is between MCC and the origin, and
        # the proxy has already done its part.
        self._tunnel_ready()
        return await self._stream.start_tls(ssl_context, server_hostname, timeout)

    def get_extra_info(self, info: str) -> typing.Any:
        if self._info_ends_handshake and self._spoke:
            self._tunnel_ready()
        return self._stream.get_extra_info(info)


class _DialClockBackend(httpcore.AsyncNetworkBackend):
    """The real backend, with a stopwatch on ``connect_tcp``."""

    def __init__(
        self, backend: httpcore.AsyncNetworkBackend, *, info_ends_handshake: bool
    ) -> None:
        self._backend = backend
        self._info_ends_handshake = info_ends_handshake

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        started = time.monotonic()
        stream = await self._backend.connect_tcp(
            host,
            port,
            timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        record_proxy_connect(time.monotonic() - started)
        return _DialClockStream(stream, info_ends_handshake=self._info_ends_handshake)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Never reached through a proxy pool; delegated unchanged so the
        # wrapper is a faithful backend whatever asks it.
        return await self._backend.connect_unix_socket(
            path, timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def clock_proxy_pool(pool: object) -> bool:
    """Put the stopwatch on one proxy pool's backend. True when this call did.

    Idempotent: a pool whose backend is already clocked is left alone, so a
    client bound twice is timed once.
    """

    backend = getattr(pool, POOL_BACKEND_ATTR, None)
    if backend is None or isinstance(backend, _DialClockBackend):
        return False
    if isinstance(pool, httpcore.AsyncSOCKSProxy):
        info_ends_handshake = True
    elif isinstance(pool, httpcore.AsyncHTTPProxy):
        info_ends_handshake = False
    else:
        return False
    setattr(
        pool,
        POOL_BACKEND_ATTR,
        _DialClockBackend(backend, info_ends_handshake=info_ends_handshake),
    )
    return True
