"""Measure one proxy before a credential is carried through it.

Three questions, in order, and the second one is not a convenience:

1. **Does the address answer at all?** A TCP connect to the proxy's own
   host and port. A free address that has stopped listening is the commonest
   thing in this catalogue and it costs one socket to find out.
2. **Does the tunnel preserve certificate validation?** An HTTPS request
   *through* the proxy to the provider's own host, built with an ordinary
   client and nothing said about trust -- which is the whole point, and which
   ``tests/contracts/test_tls_verification_is_never_weakened.py`` is what keeps
   true across this package. If the certificate that comes back does not verify, something
   between here and the provider terminated the TLS and is reading the
   plaintext: an API key, an OAuth token, a prompt, a reply. That address is
   marked :data:`~my_claude_code.config.proxy_chains.TLS_INTERCEPTED` and
   **refused** -- it cannot be added to a chain, and one already in a chain is
   held out of selection by the runtime's own ledger. This is the one test that
   catches that class of proxy, and it costs nothing beyond a request the
   checker was already making.
3. **How long did it take?** Wall-clock around step 2, which is the number that
   matters: a handshake through the tunnel to the host the chain will actually
   use, not a ping.

The destination is **the provider's own base-URL host**, never a third-party
echo service. It is the only destination that tests what the chain will
actually do, and it is an address the operator already chose to talk to.

**The exit-IP check is opt-in and against a URL the operator types.** It proves
the address really changed, which is the whole point of the feature, and it is
an outbound request to a stranger. MCC ships no default URL and makes no such
call unless one is configured.

Out of band, always. Nothing here runs inside a request, nothing on the request
path imports this module, and every network call is an ordinary await on the
loop that started it -- the checker's own timer yields between addresses so a
sweep of a long catalogue cannot sit in front of ``/v1/messages``.
"""

import asyncio
import base64
import contextlib
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import socksio.socks5
from loguru import logger

from my_claude_code.config.constants import (
    PROXY_CHECK_MAX_CONCURRENCY_DEFAULT,
    PROXY_CHECK_TIMEOUT_SECONDS_DEFAULT,
)
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
    TLS_UNKNOWN,
    ProxyChains,
    ProxyCheckRecord,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.core.proxy_rotation import PROXY_INTERCEPTION, PROXY_REACHABILITY

#: How long any single leg of the check may take. Deliberately shorter than the
#: request path's own connect timeout: a checker that waits sixty seconds on a
#: dead address turns a sweep of twelve into four minutes of nothing.
#:
#: Since 7.24.0 the number is the operator's, settable as
#: ``PROXY_CHECK_TIMEOUT_SECONDS`` on Limits & Resilience. This name is the
#: shipped default and the fallback every caller that is handed nothing still
#: uses, so a call site that has no Settings in reach behaves exactly as it
#: did. ``config`` is a leaf package that may not import ``application``, so
#: the literal lives in ``config.constants`` and this aliases it.
PROXY_CHECK_TIMEOUT_SECONDS = PROXY_CHECK_TIMEOUT_SECONDS_DEFAULT

#: The most bytes of an exit-IP answer that are kept. The operator's URL is
#: their own choice and may return anything at all; the store holds a short
#: string, not a page.
EXIT_IP_MAX_CHARS = 64

#: How many addresses one *operator-initiated* sweep may have in flight. The
#: background checker keeps the serial default of 1 -- it has all day and it
#: must not sit in front of a request -- but a person who has just ticked
#: twelve candidates and pressed Add is waiting at the screen, and twelve
#: ten-second timeouts in a row is two minutes of a spinner. Four is the bound
#: because the slow half of a check is a TLS handshake through a stranger's
#: machine: four of those overlap comfortably and forty would be a small
#: outbound flood from an admin page.
#:
#: Since 7.24.0 this is the shipped default of the
#: ``PROXY_CHECK_MAX_CONCURRENCY`` setting on Limits & Resilience, and the
#: value every caller that is handed nothing still uses.
PROXY_CHECK_MAX_CONCURRENCY = PROXY_CHECK_MAX_CONCURRENCY_DEFAULT

#: How long the *tidying up* after a check may take. Closing a socket is not
#: part of the measurement and nothing about the verdict depends on it
#: finishing, but on Windows it is where a sweep dies: the proactor loop can
#: raise inside ``_call_connection_lost``, the transport's closed future is
#: then never resolved, and ``StreamWriter.wait_closed()`` waits for it for
#: ever. A fetch of 1,592 addresses on a 7.21.0 install stopped at 1,591 that
#: way and never settled. One second is generous for a close that is going to
#: happen at all; past it the transport is aborted and the socket is the
#: operating system's problem, not this job's.
PROXY_CLOSE_TIMEOUT_SECONDS = 1.0

#: Slack on top of every leg's own ceiling before an address is declared to
#: have leaked a future. Wide enough that a slow-but-working address is never
#: cut short by it -- the legs already have their own timeouts and this is the
#: backstop underneath them, not a second policy.
CHECK_BUDGET_MARGIN_SECONDS = 5.0

#: How far one check goes.
#:
#: ``request`` is what every release up to 7.22.1 did and is what
#: :func:`check_proxy` still does when nobody says otherwise: open the tunnel,
#: then send a ``HEAD`` to the destination. An address that answers has been
#: proven end to end, which is why the Test button, Add, and "Add all working"
#: use it and always will -- those put an address in front of a credential.
#:
#: ``tls`` stops one step earlier, at the step that is the actual control: the
#: tunnel is opened and a full TLS handshake to the destination host is
#: completed *through* it, with the library's own default trust, and then the
#: socket is closed. A certificate that does not verify is an intercepting
#: proxy and is refused exactly as before. Nothing is sent to the destination:
#: a sweep of 1,592 addresses used to arrive at the provider as about a
#: thousand ``HEAD`` requests from a thousand different source addresses, and
#: the handshake already answered the question those requests were asked.
CHECK_DEPTH_TLS = "tls"
CHECK_DEPTH_REQUEST = "request"
CHECK_DEPTHS: tuple[str, ...] = (CHECK_DEPTH_TLS, CHECK_DEPTH_REQUEST)

#: What a *fetch sweep* does when the operator has not said. Not the same thing
#: as :func:`check_proxy`'s own parameter default, which stays ``request`` so
#: that every caller written before this existed behaves byte for byte as it
#: did. Mirrored by ``config.constants.PROXY_FETCH_CHECK_DEPTH_DEFAULT``.
DEFAULT_FETCH_CHECK_DEPTH = CHECK_DEPTH_TLS

#: The port a destination with no explicit one is reached on.
HTTPS_DEFAULT_PORT = 443

_DEFAULT_SSL_CONTEXT: ssl.SSLContext | None = None


def default_ssl_context() -> ssl.SSLContext:
    """The context the request path's own clients are built with. Not a copy.

    ``httpx.create_ssl_context()`` called with no arguments is, by definition,
    what ``httpx.AsyncClient()`` builds for itself when nothing is said about
    trust -- the system's own default context loaded with ``certifi``'s roots,
    hostnames matched, certificates required, and ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR`` honoured exactly as the library honours them. Asking the
    library for it is the whole argument: the ``tls`` depth cannot be more
    permissive than a request through the same proxy, because it is verifying
    with the identical object.

    Built once and shared. An ``SSLContext`` is designed to be reused across
    connections -- it is what a connection pool does -- and building one per
    address would re-read the trust store five hundred times in a sweep.
    """

    global _DEFAULT_SSL_CONTEXT
    if _DEFAULT_SSL_CONTEXT is None:
        context = httpx.create_ssl_context()
        # The same protocol list ``httpcore`` offers on a tunnelled HTTPS
        # connection, so the handshake this makes and the handshake a request
        # would have made present the same ClientHello.
        context.set_alpn_protocols(["http/1.1"])
        _DEFAULT_SSL_CONTEXT = context
    return _DEFAULT_SSL_CONTEXT


@dataclass(frozen=True, slots=True)
class ProxyCheckOutcome:
    """One address's verdict, plus the label it is filed under."""

    label: str
    record: ProxyCheckRecord

    @property
    def refused(self) -> bool:
        return self.record.intercepted


def destination_for_provider(provider_id: str, settings: Any) -> str:
    """The https URL a check for one provider should be aimed at.

    The provider's own base URL: the operator's override if they set one, the
    catalogue's default otherwise, and a custom provider's registered URL for
    a custom provider. Never a third-party echo service -- this is the only
    destination that tests what the chain will actually do, and it is a host
    the operator already chose to talk to.
    """

    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is not None:
        attr = descriptor.base_url_attr
        if attr:
            configured = str(getattr(settings, attr, "") or "").strip()
            if configured:
                return configured
        return str(descriptor.default_base_url or "").strip()
    for entry in get_provider_registry().list_custom():
        if entry.provider_id == provider_id:
            return entry.base_url.strip()
    return ""


def check_targets(
    settings: Any, store: ProxyChains, *, enabled_only: bool = False
) -> dict[str, str]:
    """Map every address in a chain to the host a check should aim at.

    An address shared between two providers is checked against the first
    provider that names it, in store order. One check is enough: the question
    is whether this tunnel keeps *any* certificate honest, and a machine that
    terminates one terminates them all.

    ``enabled_only`` narrows it to chains the operator actually armed, which is
    what the health re-prober asks for: a chain that is switched off routes no
    traffic, so re-testing its addresses would be an outbound request nobody
    asked for. A *paused* entry inside an enabled chain is still included --
    it is held out of selection, not out of the catalogue, and an operator who
    un-pauses it wants a current answer rather than a stale one.
    """

    targets: dict[str, str] = {}
    for provider_id, chain in store.chains.items():
        if enabled_only and not chain.enabled:
            continue
        destination = destination_for_provider(provider_id, settings)
        if not destination.lower().startswith("https://"):
            continue
        for entry in chain.entries:
            if entry.proxy and entry.proxy not in targets:
                targets[entry.proxy] = destination
    return targets


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_certificate_failure(error: BaseException) -> bool:
    """Whether a failure was the destination's certificate failing to verify.

    Read off the whole cause chain: ``httpx`` raises ``ConnectError`` with the
    ``ssl`` error as ``__cause__``, and a proxy adds another link. The message
    check is the fallback for a transport that flattened the chain -- some
    SOCKS implementations raise their own error type with the OpenSSL text
    carried only in ``str()``.
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(seen) < 8:
        seen.append(current)
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    text = " ".join(str(link) for link in seen).lower()
    return (
        "certificate verify failed" in text
        or "self-signed certificate" in text
        or "self signed certificate" in text
        or "certificate_verify_failed" in text
    )


def _host_and_port(url: str) -> tuple[str, int] | None:
    parsed = urlsplit(url.strip())
    host = parsed.hostname
    if not host:
        return None
    if parsed.port:
        return host, parsed.port
    # A proxy URL with no port is legal and the default depends on what it is:
    # 1080 is the SOCKS port, 443 and 80 the HTTP ones.
    if parsed.scheme in {"socks5", "socks5h"}:
        return host, 1080
    return host, 443 if parsed.scheme == "https" else 80


def _abort_writer(writer: asyncio.StreamWriter) -> None:
    """Throw the socket away without waiting for anybody to agree.

    ``transport.abort()`` is synchronous and cannot block: it drops the
    connection and schedules the callbacks. It is what is left when a polite
    close has already been given its second and has not come back.
    """

    with contextlib.suppress(Exception):
        writer.transport.abort()


async def _release_writer(writer: asyncio.StreamWriter) -> None:
    """Give up a socket in bounded time, whatever the transport does.

    Three ways out and every one of them ends:

    * the close completes, which is the ordinary case;
    * it does not complete within :data:`PROXY_CLOSE_TIMEOUT_SECONDS`, or it
      raises because the peer had already dropped the connection -- the
      transport is aborted and the check carries on with its verdict;
    * the surrounding task is being cancelled, in which case there is nothing
      left to wait for at all: abort, and let the cancellation through.

    The last two are the point. ``wait_closed()`` resolves a future the
    transport is supposed to complete, and a transport whose
    ``_call_connection_lost`` raised never completes it -- so the unbounded
    await that used to be here could hold one address, and with it the whole
    sweep, for ever.
    """

    with contextlib.suppress(Exception):
        writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), PROXY_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError, OSError:
        _abort_writer(writer)
    except asyncio.CancelledError:
        _abort_writer(writer)
        raise


async def _release_client(client: httpx.AsyncClient) -> None:
    """Close an ``httpx`` client in bounded time, whatever its pool is doing.

    The same argument as :func:`_release_writer`, one layer up. A client whose
    connection to a stranger's proxy is mid-handshake can take an unbounded
    time to shut its pool down, and the measurement is already over by the time
    this runs: a close that has not happened in a second is abandoned, and the
    sockets are collected with the client.
    """

    try:
        await asyncio.wait_for(client.aclose(), PROXY_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError, OSError, RuntimeError:
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - transport-specific
        logger.debug("PROXY CHECK: a client did not close cleanly: {}", exc)


def check_budget(
    *,
    connect_timeout: float,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    exit_ip_url: str = "",
) -> float:
    """The longest one call to :func:`check_proxy` may honestly take.

    Every leg's own ceiling, plus the closes, plus a margin -- so a caller can
    put one bound around the whole call and know it is not cutting short a
    check that was still working. It is a backstop, not a policy: if it ever
    fires, a future leaked somewhere inside and the address is recorded dead
    rather than allowed to hold the job.
    """

    legs = max(0.1, float(connect_timeout)) + max(0.1, float(timeout))
    if exit_ip_url:
        legs += max(0.1, float(timeout))
    return legs + 3.0 * PROXY_CLOSE_TIMEOUT_SECONDS + CHECK_BUDGET_MARGIN_SECONDS


async def _tcp_connect(host: str, port: int, timeout: float) -> str:
    """Empty string when the address answered; a reason when it did not."""

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        del reader
        return ""
    except TimeoutError:
        return f"no answer from {host}:{port} within {timeout:.0f}s"
    except OSError as exc:
        return f"{host}:{port} refused the connection: {exc.strerror or exc}"
    finally:
        if writer is not None:
            # Closing a socket the peer already dropped raises, and a socket
            # whose transport has stopped answering never closes at all.
            # Nothing about this check depends on either, so both are bounded.
            await _release_writer(writer)


def _destination_endpoint(destination: str) -> tuple[str, int] | None:
    """The host and port an https destination URL names."""

    parsed = urlsplit(destination.strip())
    host = parsed.hostname
    if not host:
        return None
    return host, parsed.port or HTTPS_DEFAULT_PORT


def _proxy_credentials(url: str) -> tuple[bytes, bytes] | None:
    parsed = urlsplit(url.strip())
    if not parsed.username:
        return None
    return (
        parsed.username.encode("utf-8"),
        (parsed.password or "").encode("utf-8"),
    )


async def _open_connect_tunnel(
    writer: asyncio.StreamWriter,
    reader: asyncio.StreamReader,
    host: str,
    port: int,
    credentials: tuple[bytes, bytes] | None,
) -> None:
    """``CONNECT host:port`` on an already-open socket. Raises on a refusal.

    The same request ``httpcore`` writes for a tunnelled connection -- the
    absolute target, a ``Host`` header, ``Accept`` -- and the same reading of
    the answer: anything outside 2xx is the proxy declining to tunnel.
    """

    target = f"{host}:{port}"
    lines = [
        f"CONNECT {target} HTTP/1.1",
        f"Host: {target}",
        "Accept: */*",
    ]
    if credentials is not None:
        token = base64.b64encode(b"%s:%s" % credentials).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    try:
        status = int(parts[1])
    except IndexError, ValueError:
        raise httpx.ProxyError(
            f"the proxy answered {status_line!r} to CONNECT"
        ) from None
    if not 200 <= status <= 299:
        raise httpx.ProxyError(f"the proxy refused the tunnel: {status_line.strip()}")


async def _open_socks5_tunnel(
    writer: asyncio.StreamWriter,
    reader: asyncio.StreamReader,
    host: str,
    port: int,
    credentials: tuple[bytes, bytes] | None,
) -> None:
    """The SOCKS5 handshake, step for step as ``httpcore`` performs it.

    Same library (``socksio``), same order, same three exchanges. The only
    difference is that each reply is read as an exact frame rather than as "up
    to four kilobytes", because what follows on this socket is a TLS
    ClientHello and a byte of the handshake left in a buffer would break it.
    """

    conn = socksio.socks5.SOCKS5Connection()
    method = (
        socksio.socks5.SOCKS5AuthMethod.NO_AUTH_REQUIRED
        if credentials is None
        else socksio.socks5.SOCKS5AuthMethod.USERNAME_PASSWORD
    )
    conn.send(socksio.socks5.SOCKS5AuthMethodsRequest([method]))
    writer.write(conn.data_to_send())
    await writer.drain()
    reply = conn.receive_data(await reader.readexactly(2))
    if getattr(reply, "method", None) != method:
        raise httpx.ProxyError("the proxy would not agree an authentication method")

    if credentials is not None:
        conn.send(socksio.socks5.SOCKS5UsernamePasswordRequest(*credentials))
        writer.write(conn.data_to_send())
        await writer.drain()
        auth_reply = conn.receive_data(await reader.readexactly(2))
        if not getattr(auth_reply, "success", False):
            raise httpx.ProxyError("the proxy rejected the username and password")

    conn.send(
        socksio.socks5.SOCKS5CommandRequest.from_address(
            socksio.socks5.SOCKS5Command.CONNECT, (host, port)
        )
    )
    writer.write(conn.data_to_send())
    await writer.drain()
    header = await reader.readexactly(4)
    kind = header[3:4]
    if kind == b"\x01":
        rest = 4 + 2
    elif kind == b"\x04":
        rest = 16 + 2
    elif kind == b"\x03":
        length = await reader.readexactly(1)
        header += length
        rest = length[0] + 2
    else:
        raise httpx.ProxyError(
            "the proxy answered SOCKS5 with an address MCC cannot read"
        )
    command_reply = conn.receive_data(header + await reader.readexactly(rest))
    code = getattr(command_reply, "reply_code", None)
    if code != socksio.socks5.SOCKS5ReplyCode.SUCCEEDED:
        raise httpx.ProxyError(f"the proxy would not connect: {code}")


class _Unreachable(Exception):
    """The proxy's own port never answered. Carries the reachability wording.

    The ``tls`` depth reaches the proxy once, so the verdict that used to come
    from a separate reachability dial has to travel back out of the one
    connection attempt. This is that wire: it is raised only before the socket
    is established, and the caller turns it into exactly the record the
    separate dial produced -- no latency, no TLS opinion, the same sentence.
    """


async def _verified_handshake(
    url: str,
    destination: str,
    *,
    timeout: float,
    connect_timeout: float,
    on_connect: Callable[[], None] | None = None,
) -> None:
    """Tunnel to the destination and finish a verified TLS handshake. No request.

    Three bounded steps and then the socket is thrown away:

    1. a socket to the proxy's own port -- **the only one this check opens.**
       Until 7.35.1 the ``tls`` depth dialled the address twice: once to ask
       whether it answered at all and again to carry the tunnel. The answer to
       the first question is already in the second dial, so the reachability
       verdict is taken from this connection and reported through
       :class:`_Unreachable` in the wording the separate dial used;
    2. the tunnel -- ``CONNECT`` for an HTTP proxy, the SOCKS5 handshake for a
       SOCKS one, using the same ``socksio`` the request path uses;
    3. ``start_tls`` with :func:`default_ssl_context`, naming the destination
       host, which is what makes this a *verification* rather than a
       connection: the certificate chain is checked against the same trust
       store the request path checks against and the hostname must match.

    ``on_connect`` is called the instant step 1 completes. That is what keeps
    the reported latency meaning what it meant when there were two sockets --
    the handshake leg, never the dial -- which matters because the candidate
    list is ordered by it.

    Everything that is not about *reaching* the address is decided after the
    socket is up, in the order the two-socket version decided it: an address
    that is not listening is dead even when the destination URL or the proxy's
    scheme is also unusable.

    Returns nothing. Success is the absence of an exception; a certificate that
    does not verify raises ``ssl.SSLCertVerificationError`` from inside
    ``start_tls``, which is precisely what the caller turns into a refusal.
    """

    endpoint = _host_and_port(url)
    if endpoint is None:
        raise httpx.ConnectError("not a usable address")
    scheme = (urlsplit(url.strip()).scheme or "http").lower()
    context = default_ssl_context()

    if scheme == "https":
        # A proxy that is itself reached over TLS. Its own certificate is
        # verified with the same context, which is what a request through it
        # would have done too.
        stream = asyncio.open_connection(
            endpoint[0], endpoint[1], ssl=context, server_hostname=endpoint[0]
        )
    else:
        stream = asyncio.open_connection(endpoint[0], endpoint[1])
    try:
        reader, writer = await asyncio.wait_for(stream, timeout=connect_timeout)
    except TimeoutError:
        raise _Unreachable(
            f"no answer from {endpoint[0]}:{endpoint[1]} within {connect_timeout:.0f}s"
        ) from None
    except ssl.SSLError:
        # An https proxy whose own certificate does not check out. That is a
        # statement about trust, not about reachability, so it goes to the
        # caller's certificate classifier -- and it has to be caught ahead of
        # ``OSError``, which it inherits from. Delete this clause and an
        # intercepting https proxy becomes an ordinary unreachable address;
        # ``test_an_https_proxys_own_bad_certificate_is_interception_not_death``
        # is the test that says so.
        raise
    except OSError as exc:
        raise _Unreachable(
            f"{endpoint[0]}:{endpoint[1]} refused the connection: {exc.strerror or exc}"
        ) from None

    if on_connect is not None:
        on_connect()
    try:
        target = _destination_endpoint(destination)
        if target is None:
            raise httpx.ConnectError("not a usable address")
        if scheme in {"socks5", "socks5h"}:
            opener = _open_socks5_tunnel
        elif scheme in {"http", "https"}:
            opener = _open_connect_tunnel
        else:
            raise httpx.ConnectError(f"MCC does not speak {scheme} to a proxy")
        credentials = _proxy_credentials(url)
        await asyncio.wait_for(
            opener(writer, reader, target[0], target[1], credentials), timeout=timeout
        )
        await asyncio.wait_for(
            writer.start_tls(context, server_hostname=target[0]), timeout=timeout
        )
    finally:
        await _release_writer(writer)


async def check_proxy(
    url: str,
    destination: str,
    *,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    exit_ip_url: str = "",
    connect_timeout: float | None = None,
    depth: str = CHECK_DEPTH_REQUEST,
) -> ProxyCheckRecord:
    """Run the three-step check against one address and return its verdict.

    ``destination`` is an https URL belonging to the provider whose chain this
    address serves. ``exit_ip_url`` is fetched only when the operator supplied
    one, and a failure there never changes the verdict: it is extra evidence,
    not a gate.

    ``connect_timeout`` bounds **step 1 only** -- the plain TCP connection to
    the proxy's own port. ``None`` means "the same as ``timeout``", which is
    what every caller did before the fetch sweep existed and is therefore what
    the Test and Add buttons still get, byte for byte. The sweep passes a
    shorter one because the commonest thing in a public list is an address that
    has stopped listening, and the difference between five seconds and ten,
    multiplied by six hundred dead addresses, is the difference between a fetch
    an operator waits for and one they abandon. It never shortens the HTTPS leg
    that follows: an address that answered has earned the full handshake.

    ``depth`` chooses between the two ways step 2 can end, and it defaults to
    :data:`CHECK_DEPTH_REQUEST` -- what every caller did before 7.22.2, byte
    for byte. :data:`CHECK_DEPTH_TLS` stops at the verified handshake and sends
    the destination nothing at all. Both answer the same question about
    interception, because the answer arrives during the handshake; the
    difference is whether the provider's server is also asked to answer a
    request it never needed to see.
    """

    label = mask_proxy_label(url)
    endpoint = _host_and_port(url)
    tls_only = str(depth).strip().lower() == CHECK_DEPTH_TLS
    proven = CHECK_DEPTH_TLS if tls_only else CHECK_DEPTH_REQUEST
    if endpoint is None:
        return ProxyCheckRecord(
            at=_now(),
            ok=False,
            tls=TLS_UNKNOWN,
            detail="not a usable proxy address",
            depth=proven,
        )

    dial = timeout if connect_timeout is None else max(0.1, float(connect_timeout))
    if not tls_only:
        # The request depth reaches the address twice however this is written:
        # once here, and again inside ``httpx``, which owns its own pool and
        # cannot be handed a socket. So the reachability dial stays exactly
        # where it was for that depth -- byte for byte the check every caller
        # made before the fetch sweep existed.
        reason = await _tcp_connect(endpoint[0], endpoint[1], dial)
        if reason:
            return ProxyCheckRecord(
                at=_now(), ok=False, tls=TLS_UNKNOWN, detail=reason, depth=proven
            )

    started = time.monotonic()

    def _connected() -> None:
        # The measurement starts when the socket is up, not when the dial
        # began: the number this produces is the handshake leg, which is what
        # it has always been and what the candidate list is ordered by.
        nonlocal started
        started = time.monotonic()

    try:
        if tls_only:
            # One socket: the dial, the tunnel, the handshake, and then it is
            # dropped. The trust used is not "the same kind of" trust as the
            # request path's -- it is the identical context object the library
            # builds for a client that says nothing about it.
            await _verified_handshake(
                url,
                destination,
                timeout=timeout,
                connect_timeout=dial,
                on_connect=_connected,
            )
        else:
            # Nothing is said about trust here, and that is the point: the
            # client gets the library's own default context -- the system trust
            # store, hostnames matched, certificates required -- which is byte
            # for byte the client the request path builds for this same proxy.
            # A check made with anything more permissive would measure nothing
            # at all.
            client = httpx.AsyncClient(
                proxy=url, timeout=timeout, follow_redirects=False
            )
            try:
                response = await client.head(destination)
                del response
            finally:
                # A bounded ``async with``. Shutting the pool down waits on
                # each connection the transport still holds, and a connection
                # through a proxy that has stopped answering is exactly the one
                # that does not come back -- the same shape of stall as
                # ``wait_closed``. The client is built with nothing said about
                # trust either way; only the giving-up is bounded.
                await _release_client(client)
    except _Unreachable as exc:
        # The address never answered. The same record the separate reachability
        # dial returned, down to the sentence and to carrying no latency: there
        # was no handshake leg to time.
        return ProxyCheckRecord(
            at=_now(), ok=False, tls=TLS_UNKNOWN, detail=str(exc), depth=proven
        )
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        if _is_certificate_failure(exc):
            # The security control. The tunnel answered, and what came back was
            # not the provider's certificate.
            logger.warning(
                "PROXY CHECK: {} terminates TLS to {} -- refusing it",
                label,
                destination,
            )
            return ProxyCheckRecord(
                at=_now(),
                ok=False,
                latency_ms=elapsed,
                tls=TLS_INTERCEPTED,
                detail=(
                    "this proxy breaks certificate validation -- MCC will not "
                    "route through it"
                ),
                depth=proven,
            )
        return ProxyCheckRecord(
            at=_now(),
            ok=False,
            latency_ms=elapsed,
            tls=TLS_UNKNOWN,
            detail=_transport_reason(exc),
            depth=proven,
        )

    # Any status at all is a pass. The question was whether the tunnel carries
    # a verified HTTPS conversation to this host, and a 404 or a 405 from the
    # provider's own base URL answers it as well as a 200 does. At ``tls``
    # depth there is no status to read and none is needed: the handshake
    # completed against the destination's own certificate, which is the whole
    # of what a pass ever meant.
    latency_ms = int((time.monotonic() - started) * 1000)
    exit_ip = ""
    if exit_ip_url:
        exit_ip = await _exit_ip(url, exit_ip_url, timeout)
    return ProxyCheckRecord(
        at=_now(),
        ok=True,
        latency_ms=latency_ms,
        tls=TLS_STRICT,
        detail="",
        exit_ip=exit_ip,
        depth=proven,
    )


def _transport_reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.ProxyError):
        return f"the proxy refused the tunnel: {exc}"
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | TimeoutError):
        return "the proxy did not answer in time"
    if isinstance(exc, httpx.ConnectError):
        return f"could not reach the destination through this proxy: {exc}"
    return f"{type(exc).__name__}: {exc}"


async def _exit_ip(proxy_url: str, exit_ip_url: str, timeout: float) -> str:
    """The operator's own URL, fetched through the proxy. Never a default.

    Best effort in every direction: a URL that is down, slow or answers with a
    page says nothing about whether the tunnel verifies, so a failure here is
    recorded as an absence rather than as a verdict.
    """

    try:
        client = httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, follow_redirects=True
        )
        try:
            response = await client.get(exit_ip_url)
            response.raise_for_status()
            return response.text.strip()[:EXIT_IP_MAX_CHARS]
        finally:
            await _release_client(client)
    except Exception as exc:
        logger.debug("PROXY CHECK: exit-IP URL did not answer: {}", exc)
        return ""


def apply_outcome(label: str, record: ProxyCheckRecord) -> None:
    """Tell the running pools what one check found.

    Three ledgers, three different meanings, and this is the only place the
    three are written together:

    * An intercepted address is **refused** -- held out of every chain in the
      process until a later check *succeeds* and shows the certificate
      verifying again.
    * A dead address walks the ordinary reachability ladder, which is the whole
      of "a dead free proxy should stop being tried": the operator does
      nothing and the chain routes around it.
    * A working address clears both, because a measurement that just succeeded
      is better evidence than a bench taken before it.

    **A refusal is lifted only by success.** This used to clear the refusal on
    any record that was not itself an interception -- including "did not
    answer". A proxy caught terminating TLS, later merely offline, therefore
    lost its verdict and could be added to a chain again. That is backwards:
    failing to connect is not evidence that a machine stopped reading the
    traffic, it is no evidence at all, and the one control standing between a
    credential and a hostile proxy must not be cleared by an absence. An
    address that cannot be reached keeps whatever verdict it had earned.
    """

    if not label:
        return
    if record.intercepted:
        PROXY_INTERCEPTION.mark(label, record.detail)
        return
    if record.ok:
        # Success is the only evidence that retires a refusal: the tunnel was
        # opened and the destination's certificate verified through it.
        PROXY_INTERCEPTION.clear_endpoint(label)
        PROXY_REACHABILITY.note_success(label)
    else:
        PROXY_REACHABILITY.note_failure(label, record.detail or "check failed")


def apply_fetch_outcome(label: str, record: ProxyCheckRecord, *, in_use: bool) -> None:
    """:func:`apply_outcome`, for an address that may belong to nobody yet.

    A fetch tests every address a public list offered -- hundreds of them, and
    most of those are strangers this install has never routed a byte through.
    Two of the three ledgers must therefore be written differently here, and
    the difference is not a nicety:

    * **Interception is written exactly as always.** It is the security
      control, it is rare, and an address caught terminating TLS must be
      refused whether or not anybody is using it. ``in_use`` does not enter
      into it.
    * **The reachability ladder is only charged for an address that is
      actually in a chain.** That ladder holds
      :data:`~my_claude_code.core.proxy_rotation.MAX_TRACKED_ENDPOINTS` rows
      and it exists to tell the request path which of *this install's own*
      addresses are worth dialling. A sweep of eight hundred candidates would
      evict every one of those rows to record benches for addresses no chain
      references -- so the sweep would break the thing it was meant to
      inform. An address nobody uses needs no ladder row.
    * **An address that IS in a chain is charged normally.** A fetch-test is
      the same three questions the Test button asks, against the same
      destination, so its answer about an address the operator is routing
      through is ordinary evidence and is recorded as such. Deciding otherwise
      would mean throwing away a measurement because of where it came from.
    """

    if not label:
        return
    if record.intercepted:
        PROXY_INTERCEPTION.mark(label, record.detail)
        return
    if record.ok:
        # Success retires a refusal wherever it is measured: the tunnel was
        # opened and the destination's certificate verified through it. That is
        # the 7.17.1 rule and it is the same rule here.
        PROXY_INTERCEPTION.clear_endpoint(label)
        if in_use:
            PROXY_REACHABILITY.note_success(label)
        return
    if in_use:
        PROXY_REACHABILITY.note_failure(label, record.detail or "check failed")


def arm_refusals_from_store(store: ProxyChains | None = None) -> int:
    """Re-arm the interception ledger from what the checker already found.

    The ledger is process-lifetime state and the store is durable, so without
    this a restart would quietly re-admit every address a previous run refused.
    Called once at startup, before the first request.
    """

    table = load_proxy_chains() if store is None else store
    armed = 0
    for endpoint in table.proxies.values():
        if endpoint.refused:
            PROXY_INTERCEPTION.mark(
                endpoint.label or mask_proxy_label(endpoint.url),
                endpoint.last_check.detail if endpoint.last_check else "",
            )
            armed += 1
    if armed:
        logger.info(
            "PROXY CHECK: {} address(es) stay refused from a previous check", armed
        )
    return armed


class _CheckLoop:
    """One thread with one event loop, for the duration of one sweep.

    **What this is for, and what it measured.** A TLS handshake is not Python
    work: ``SSLObject.do_handshake`` and the certificate-chain verification
    under it are OpenSSL C calls that release the GIL while they run. A hundred
    of them *scheduled on the server's own event loop*, however, are a hundred
    callbacks between that loop and anything else it was going to do -- and the
    2026-09-18 report is what that costs. Measured here, on the reporting
    machine, against 300 addresses (100 honest tunnels to a local https origin,
    100 listeners that accept and answer nothing, 100 black-holed 192.0.2.x) at
    concurrency 100, with a task asking for 100 ms of sleep as the instrument:

    ==================================  ==============  ========  ==========
    mechanism                           max loop gap    p99       wall
    ==================================  ==============  ========  ==========
    today (on the server's loop)        650 / 614 /     265 /     24.9 s
                                        478 ms          275 ms
    ``asyncio.to_thread`` per address   681 ms          53 ms     23.5 s
    bounded ThreadPoolExecutor (8)      35 ms           16 ms     277.6 s
    **this: one worker loop**           **40 / 65 ms**  13-15 ms  24.7 s
    ==================================  ==============  ========  ==========

    Three runs of the first row and two of the last, so the spread is the
    measurement's own. The 9 ms median in every run is this machine's timer
    granularity, which is the floor the worker loop's p99 sits on. ``to_thread``
    per address was rejected on the measurement -- a hundred threads each
    running ``asyncio.run`` cost the loop as much as the loop doing the work
    itself, the same finding the 2026-09-11 dashboard work recorded for a
    GIL-bound hold. The bounded pool reached the target and took **eleven times
    as long**, because eight threads cannot hold a hundred checks in flight. A
    subprocess worker was specified as the fallback and was not built: there
    was nothing left for it to win.

    **What does NOT move.** Only :func:`check_proxy` runs here -- the same
    coroutine, with the same :func:`default_ssl_context`, reached through the
    same call. The semaphore, the per-address yield, the verdict handling, the
    ledgers and the single save all stay on the caller's loop and in the
    caller's order, so every ledger mutation in this process still happens on
    one thread and a verdict is applied exactly where and when it always was.
    Measured verdict equality on the population above: 100 of 300 pass, either
    way.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="mcc-proxy-check-loop", daemon=True
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()

    async def run(self, coroutine: Any) -> Any:
        """Await ``coroutine`` on the worker loop from the caller's loop.

        Cancellation crosses both ways: cancelling the caller cancels the task
        on the worker, which is what lets a stop settle rather than wait out a
        ten-second connect timeout.
        """

        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def close(self) -> None:
        """Stop the loop and join the thread, never on the caller's loop."""

        self._loop.call_soon_threadsafe(self._loop.stop)
        await asyncio.to_thread(self._thread.join, 10.0)


async def check_endpoints(
    proxy_ids: Iterable[str],
    destinations: dict[str, str],
    *,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    exit_ip_url: str = "",
    persist: bool = True,
    concurrency: int = 1,
    max_concurrency: int = PROXY_CHECK_MAX_CONCURRENCY,
    budget: float | None = None,
    off_loop: bool = False,
) -> dict[str, ProxyCheckOutcome]:
    """Check several stored addresses, persist the verdicts, arm the ledgers.

    ``destinations`` maps a proxy id to the https URL to test it against. The
    store is re-read before the write rather than held across the checks: a
    sweep takes seconds and an operator editing a chain in the meantime must
    not lose the edit to a result about a different address.

    ``concurrency`` is 1 by default, which is the background checker's contract
    with the request path: one address at a time, yielding between them. A
    caller with a person waiting on the answer -- the Proxying page's bulk add
    -- raises it. The result is keyed by proxy id either way, and the verdicts
    are written in one save at the end either way, so nothing downstream can
    tell which was used.

    ``max_concurrency`` is the ceiling that request is clamped to, and it
    defaults to :data:`PROXY_CHECK_MAX_CONCURRENCY` -- four, the number every
    caller written before 7.22.2 got. "Add all working" raises it, because it
    is the same gesture as the fetch that produced the list and an operator who
    set a fetch to a hundred meant a hundred; the *checks themselves* are
    unchanged, so the address that was proven end to end before is proven end
    to end now, just sooner.

    ``budget`` is the outer per-address bound the fetch sweep has carried since
    7.22.1, and it is ``None`` -- absent -- by default, so the background
    checker and the Test button behave exactly as they did. A caller that
    raises the concurrency passes one: with a hundred checks in flight, one
    address that leaks a future would hold a slot for the rest of the job.

    ``off_loop`` moves the handshakes -- and only the handshakes -- onto one
    worker thread with an event loop of its own, for the duration of this call.
    It is ``False`` by default, so the background checker, the Test button and
    every caller written before 7.27.0 behave exactly as they did, down to the
    thread they run on. The bulk add passes it, because a hundred concurrent
    tunnels through strangers' machines is the one gesture that was measured
    holding the server's loop for two thirds of a second at a time. See
    :class:`_CheckLoop` for the measurement and for what deliberately does not
    move.
    """

    table = load_proxy_chains()
    wanted = [
        proxy_id
        for proxy_id in proxy_ids
        if table.endpoint(proxy_id) is not None and destinations.get(proxy_id, "")
    ]
    outcomes: dict[str, ProxyCheckOutcome] = {}
    ceiling = max(1, int(max_concurrency))
    limit = asyncio.Semaphore(max(1, min(int(concurrency), ceiling)))

    worker = _CheckLoop() if off_loop and wanted else None
    if worker is not None:
        worker.start()

    async def run_one(coroutine: Any) -> ProxyCheckRecord:
        """One check, here or on the worker loop. Nothing else differs."""

        if worker is None:
            return await coroutine
        return await worker.run(coroutine)

    async def measure(proxy_id: str) -> None:
        endpoint = table.endpoint(proxy_id)
        if endpoint is None:  # pragma: no cover - filtered above
            return
        label = endpoint.label or mask_proxy_label(endpoint.url)
        async with limit:
            checking = check_proxy(
                endpoint.url,
                destinations[proxy_id],
                timeout=timeout,
                exit_ip_url=exit_ip_url,
            )
            if budget is None:
                record = await run_one(checking)
            else:
                try:
                    record = await run_one(asyncio.wait_for(checking, budget))
                except TimeoutError:
                    logger.warning(
                        "PROXY CHECK: {} did not finish within {:.0f}s -- "
                        "abandoning it",
                        label,
                        budget,
                    )
                    record = ProxyCheckRecord(
                        at=_now(),
                        ok=False,
                        tls=TLS_UNKNOWN,
                        detail=f"did not finish within {budget:.0f}s",
                        depth=CHECK_DEPTH_REQUEST,
                    )
        apply_outcome(label, record)
        outcomes[proxy_id] = ProxyCheckOutcome(label=label, record=record)
        # One yield per address. A sweep of a full catalogue is a dozen network
        # calls and this is what keeps them from sitting in front of a request.
        await asyncio.sleep(0)

    try:
        await asyncio.gather(*(measure(proxy_id) for proxy_id in wanted))
    finally:
        # Always, including on a cancellation: a worker loop that outlived its
        # sweep is a thread this process never gets back.
        if worker is not None:
            await worker.close()
    # Back into the order asked for: a caller reports these to a person reading
    # a list, and gather finishes them in whatever order the network allows.
    outcomes = {
        proxy_id: outcomes[proxy_id] for proxy_id in wanted if proxy_id in outcomes
    }

    if persist and outcomes:
        fresh = load_proxy_chains()
        for proxy_id, outcome in outcomes.items():
            fresh = fresh.with_check(proxy_id, outcome.record)
        save_proxy_chains(fresh)
    return outcomes


__all__ = [
    "CHECK_BUDGET_MARGIN_SECONDS",
    "CHECK_DEPTHS",
    "CHECK_DEPTH_REQUEST",
    "CHECK_DEPTH_TLS",
    "DEFAULT_FETCH_CHECK_DEPTH",
    "EXIT_IP_MAX_CHARS",
    "HTTPS_DEFAULT_PORT",
    "PROXY_CHECK_MAX_CONCURRENCY",
    "PROXY_CHECK_TIMEOUT_SECONDS",
    "PROXY_CLOSE_TIMEOUT_SECONDS",
    "ProxyCheckOutcome",
    "apply_fetch_outcome",
    "apply_outcome",
    "arm_refusals_from_store",
    "check_budget",
    "check_endpoints",
    "check_proxy",
    "check_targets",
    "default_ssl_context",
    "destination_for_provider",
]
