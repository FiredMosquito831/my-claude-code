"""Give the SOCKS5 handshake the deadline the library never gave it.

What was broken
---------------

``httpcore`` 1.0.9 establishes a SOCKS5 tunnel in
``httpcore/_async/socks_proxy.py``. Line 219 reads the request's ``connect``
timeout and hands it to ``connect_tcp`` (line 231) and to ``start_tls``
(line 266) -- but line 244 calls ``_init_socks5_connection(**kwargs)`` with a
``kwargs`` dict of ``stream``/``host``/``port``/``auth`` and **no timeout**.
Inside that function, lines 62, 81 and 97 are ``await stream.read(max_bytes=4096)``
with the ``timeout`` argument left at its ``None`` default, which
``httpcore/_backends/anyio.py:33`` turns into ``anyio.fail_after(None)`` -- no
deadline at all.

So a SOCKS5 address that completes the TCP accept and then says nothing holds
the request **for ever**. Nothing above it can end the wait:
``HTTP_READ_TIMEOUT``, ``HTTP_CONNECT_TIMEOUT`` and the pool timeout are all
set on the ``httpx.AsyncClient`` and none of them reaches that read;
``PROXY_CONNECT_TIMEOUT_SECONDS`` arrives as the leg's *connect* timeout, which
bounds the TCP dial in front of the handshake and the TLS upgrade behind it,
but not the handshake between them. SOCKS support shipped in 7.15.0 and the
hole has been open since.

An HTTP ``CONNECT`` rung was never affected: ``httpcore`` tunnels it through an
ordinary HTTP/1.1 connection that carries the request's own read timeout.

What this does
--------------

One mechanism, installed on a client that has already been built:
:func:`bound_socks_handshake` finds any SOCKS pool the client holds and wraps
that pool's network backend. The wrapper's ``connect_tcp`` returns the real
stream inside :class:`_HandshakeDeadlineStream`, which supplies the connection's
**own connect timeout** to any read or write that would otherwise have been
unbounded.

Three properties follow from doing it this way, and each is the reason a more
obvious alternative was not chosen:

* **No new setting.** The budget is the ``connect`` timeout ``httpcore``
  already passes into ``connect_tcp`` for this very connection -- which on a
  proxied leg *is* ``PROXY_CONNECT_TIMEOUT_SECONDS``, because
  ``providers/runtime/factory.py`` hands the leg that value as its
  ``http_connect_timeout``. The setting's meaning, "how long establishing the
  proxied connection may take", now covers the whole of establishing it.
* **Nothing un-proxied changes.** A client with no proxy, or with an ``http``
  or ``https`` proxy, has no ``AsyncSOCKSProxy`` in it, so this function
  returns the identical object it was given, untouched. There is no subclass,
  no substituted transport and no re-created pool.
* **No TLS opinion of any kind.** The pool, its SSL context and its limits are
  the ones ``httpx`` built from the caller's own arguments. Only the network
  backend -- the thing that opens sockets -- is wrapped, and the wrapper
  forwards ``start_tls`` to the real stream verbatim.

The deadline is per-phase, not one budget for the whole of establishment: the
TCP dial, the handshake and the TLS upgrade each get the connect timeout, in
the same way the dial and the upgrade already did. A single shared budget would
have been a *tightening* -- a healthy-but-slow address that spends four seconds
dialling and four handshaking would start failing against a ten-second
setting -- and this release is not allowed to change what a working chain does.

The deadline is also retired the moment establishment is over, so a request's
own reads can never inherit it. ``httpcore`` calls ``start_tls`` (for an https
origin) or ``get_extra_info`` (for every origin, to ask about ALPN) exactly
once, between the handshake and the first request byte; either one disarms the
stream. After that the wrapper passes every argument straight through.

A timed-out handshake is raised as ``httpcore.ConnectTimeout``, which ``httpx``
maps to ``httpx.ConnectTimeout``: the same class a dead address's TCP dial
produces today, already in ``proxy_rotating._REACHABILITY_TYPES``, so the rung
is benched, the chain switches and the ladder reads exactly as it does for any
other address that did not answer. No new failure kind is introduced.

The one other thing it installs
-------------------------------

Every call site that can be handed an operator's proxy already calls
:func:`bound_socks_handshake`, so it is also where each proxy pool -- SOCKS
*and* HTTP -- gets the stopwatch from ``providers/proxy_dial_clock.py``, which
times the connect to the proxy and the tunnel handshake for the request's
ladder. That is timing only: no deadline, no byte changed, and nothing for an
un-proxied client. On a SOCKS pool the stopwatch sits *inside* the deadline,
so the deadline sees exactly the stream it always saw.
"""

import ssl
import time
import typing

import httpcore
import httpx

from my_claude_code.providers.proxy_dial_clock import clock_proxy_pool

#: Every private name this module reads off ``httpx``/``httpcore``. Pinned by
#: ``tests/contracts/test_socks_deadline_contract.py`` so that an upgrade which
#: renames one of them fails a test instead of quietly restoring the hang.
TRANSPORT_POOL_ATTR = "_pool"
POOL_BACKEND_ATTR = "_network_backend"


class _HandshakeDeadlineStream(httpcore.AsyncNetworkStream):
    """A network stream that will not wait for ever before the request starts.

    It is armed with an absolute deadline when the socket is opened and it is
    disarmed as soon as the connection is established. While armed it supplies
    the remaining budget to any ``read``/``write`` whose caller named no
    timeout -- which, in ``httpcore`` 1.0.9, is the SOCKS5 handshake and
    nothing else.
    """

    def __init__(self, stream: httpcore.AsyncNetworkStream, budget: float) -> None:
        self._stream = stream
        self._budget = budget
        self._deadline: float | None = time.monotonic() + budget

    def _remaining(self, timeout: float | None) -> tuple[float | None, bool]:
        """The timeout to use, and whether this module is the one imposing it."""

        if timeout is not None or self._deadline is None:
            return timeout, False
        return max(0.0, self._deadline - time.monotonic()), True

    def _expired(self) -> httpcore.ConnectTimeout:
        return httpcore.ConnectTimeout(
            f"the SOCKS proxy did not finish the handshake within {self._budget:.0f}s"
        )

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        deadline, ours = self._remaining(timeout)
        try:
            return await self._stream.read(max_bytes, deadline)
        except httpcore.ReadTimeout:
            if ours:
                # The wait this module ended. Reported as the connect-class
                # failure a dead address produces, because that is what it is:
                # the connection was never established.
                raise self._expired() from None
            raise

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        deadline, ours = self._remaining(timeout)
        try:
            await self._stream.write(buffer, deadline)
        except httpcore.WriteTimeout:
            if ours:
                raise self._expired() from None
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # The handshake is over by definition, and what comes back is the
        # library's own TLS stream -- not wrapped, so a request's reads run on
        # exactly the object they run on without this module.
        self._deadline = None
        return await self._stream.start_tls(ssl_context, server_hostname, timeout)

    def get_extra_info(self, info: str) -> typing.Any:
        # ``httpcore`` asks the established stream about ALPN before it builds
        # the HTTP connection, and ``http11`` asks it whether the socket is
        # readable. Either way the handshake is behind us.
        self._deadline = None
        return self._stream.get_extra_info(info)


class _HandshakeDeadlineBackend(httpcore.AsyncNetworkBackend):
    """The real backend, handing out streams that carry a connect deadline."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend) -> None:
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_tcp(
            host,
            port,
            timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        if timeout is None:
            # No connect budget was configured at all. Wrapping would invent
            # one, and inventing one is exactly what this release may not do.
            return stream
        return _HandshakeDeadlineStream(stream, float(timeout))

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Never reached through a SOCKS pool; delegated unchanged so the
        # wrapper is a faithful backend whatever asks it.
        return await self._backend.connect_unix_socket(
            path, timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _bind_pool(pool: object) -> bool:
    """Wrap one pool's backend. True when this call is what wrapped it."""

    backend = getattr(pool, POOL_BACKEND_ATTR, None)
    if backend is None or isinstance(backend, _HandshakeDeadlineBackend):
        return False
    setattr(pool, POOL_BACKEND_ATTR, _HandshakeDeadlineBackend(backend))
    return True


def bound_socks_handshake(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Bound the SOCKS5 handshake of every SOCKS pool *client* holds.

    Returns the same client object, so it reads as a wrapper at the call site
    and costs nothing at every call site that has no proxy: a client built
    with no proxy comes back byte-for-byte the object that was passed in. An
    ``http``/``https`` proxy pool gets no deadline -- it never needed one --
    and only the dial stopwatch.

    Call it *after* the client is constructed. The backend is read out of each
    connection at dial time, so wrapping it before the first request reaches
    every connection the client will ever open.
    """

    transports: list[object] = [getattr(client, "_transport", None)]
    transports.extend(getattr(client, "_mounts", {}).values())
    for transport in transports:
        pool = getattr(transport, TRANSPORT_POOL_ATTR, None)
        if isinstance(pool, httpcore.AsyncSOCKSProxy):
            # The stopwatch goes on first, so it ends up inside the deadline;
            # a pool that is already bounded was clocked when it was bounded.
            backend = getattr(pool, POOL_BACKEND_ATTR, None)
            if not isinstance(backend, _HandshakeDeadlineBackend):
                clock_proxy_pool(pool)
            _bind_pool(pool)
        elif isinstance(pool, httpcore.AsyncHTTPProxy):
            clock_proxy_pool(pool)
    return client
