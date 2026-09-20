"""The TLS-interception refusal, proved against a MITM proxy this file builds.

A unit test that a refusal *happens* is necessary and nowhere near sufficient:
it proves the branch is reachable, not that the branch is ever taken by the
thing it was written for. So this module stands up the real shape of the
problem on loopback, with no network and no third party:

* a **CA** minted here, and an HTTPS **origin** holding a certificate that
  chains to it -- the honest provider;
* a **clean CONNECT proxy**, which relays bytes and touches nothing;
* a **MITM CONNECT proxy**, which answers ``200 Connection established`` like
  any other proxy and then terminates the TLS itself with a self-signed
  certificate for the same hostname -- exactly the class of proxy the 640k
  study found, reduced to forty lines.

``SSL_CERT_FILE`` points the *test process* at the CA it minted, which is how
the honest origin verifies at all on a machine with no public certificate for
``localhost``. That is the only concession, it is made in the test and never in
``src/``, and it makes the check *stricter* rather than weaker: the rogue
certificate is refused by the same trust store that accepts the honest one,
which is the comparison that matters. Nothing here passes a trust keyword to
``httpx``, builds an SSL context for the checker, or edits the checker's
client -- ``tests/contracts/test_tls_verification_is_never_weakened.py`` would
catch that, and it is there precisely so this proof cannot be faked.

Every socket and thread opened here is closed in a fixture's teardown.
"""

import asyncio
import contextlib
import datetime
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from my_claude_code.application import proxy_check
from my_claude_code.application.proxy_check import (
    apply_outcome,
    check_proxy,
)
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
    TLS_UNKNOWN,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
    reset_proxy_health,
)

pytestmark = pytest.mark.asyncio

HOSTNAME = "localhost"
_HTTP_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


# ------------------------------------------------------------------- the PKI


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write(path: Path, cert: x509.Certificate, key: rsa.RSAPrivateKey) -> Path:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    return path


def _leaf(
    common_name: str,
    issuer_name: x509.Name,
    issuer_key: rsa.RSAPrivateKey,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        # Windows' OpenSSL build refuses a chain whose leaf carries no
        # authority key identifier ("Missing Authority Key Identifier"), so a
        # certificate without one would fail for a reason that has nothing to
        # do with what this file is testing.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
        .sign(issuer_key, hashes.SHA256())
    )
    return cert, key


@pytest.fixture
def pki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A CA, an honest certificate that chains to it, and a rogue one that does not."""

    ca_key = _key()
    now = datetime.datetime.now(datetime.UTC)
    ca_name = _name("MCC proxy-check test CA")
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        # Windows' verifier refuses a CA with no key usage extension outright,
        # so the honest chain needs one to be judged on its merits.
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    honest_cert, honest_key = _leaf(HOSTNAME, ca_name, ca_key)
    # Self-signed for the same hostname: the name matches, the chain does not.
    # That is precisely what a transparent interceptor presents to a client
    # that has not been made to trust it.
    rogue_key = _key()
    rogue_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(HOSTNAME))
        .issuer_name(_name(HOSTNAME))
        .public_key(rogue_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(rogue_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(rogue_key.public_key()),
            critical=False,
        )
        .sign(rogue_key, hashes.SHA256())
    )

    # The one concession, and it is the test's own process: without it the
    # honest origin would be rejected too and the comparison would prove
    # nothing. It replaces the trust root; it does not relax verification.
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    # The ``tls`` depth builds its context once and keeps it, which is right in
    # a process whose trust store does not move and wrong in a test file that
    # mints a new CA per test. Cleared on the way in and on the way out, so
    # each test verifies against its own roots and nothing leaks into the rest
    # of the suite.
    monkeypatch.setattr(proxy_check, "_DEFAULT_SSL_CONTEXT", None, raising=False)
    return {
        "ca": ca_pem,
        "honest": _write(tmp_path / "honest.pem", honest_cert, honest_key),
        "rogue": _write(tmp_path / "rogue.pem", rogue_cert, rogue_key),
    }


# --------------------------------------------------------------- the servers


class _Server:
    """A loopback TCP server on an ephemeral port, served by daemon threads.

    Hand-rolled rather than ``http.server``: the MITM proxy has to speak
    ``CONNECT`` and then take the socket over, which is not a thing any
    request handler abstraction lets you do.
    """

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.port: int = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._accepting = threading.Thread(target=self._accept, daemon=True)
        self._accepting.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn: socket.socket) -> None:  # pragma: no cover - overridden
        conn.close()

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._accepting.join(timeout=2)
        for thread in self._threads:
            thread.join(timeout=2)


class _Origin(_Server):
    """The honest provider: HTTPS, one certificate, a 200 to anything."""

    def __init__(self, certificate: Path) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(certificate)
        #: Every request byte this origin was actually sent, which since 7.22.2
        #: is a thing worth asserting about: the ``tls`` depth's whole claim is
        #: that a sweep of a thousand addresses does not arrive here as a
        #: thousand requests, and the only honest way to test that is to ask
        #: the destination whether it heard anything.
        self.requests: list[bytes] = []
        super().__init__()

    def _serve(self, conn: socket.socket) -> None:
        try:
            with self._context.wrap_socket(conn, server_side=True) as tls:
                received = tls.recv(4096)
                if received:
                    self.requests.append(received)
                tls.sendall(_HTTP_OK)
        except OSError:
            pass
        finally:
            conn.close()


class _ConnectProxy(_Server):
    """A forward proxy. Honest when ``mitm`` is ``None``, a wiretap when it is not."""

    def __init__(self, mitm: Path | None = None) -> None:
        self._mitm = None
        if mitm is not None:
            self._mitm = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._mitm.load_cert_chain(mitm)
        super().__init__()

    def _serve(self, conn: socket.socket) -> None:
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                request += chunk
            target = request.split(b" ")[1].decode()
            host, _, port = target.partition(":")
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if self._mitm is None:
                self._tunnel(conn, host, int(port or 443))
            else:
                self._intercept(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def _tunnel(self, conn: socket.socket, host: str, port: int) -> None:
        """Relay bytes both ways and read none of them. What a proxy should do."""

        upstream = socket.create_connection((host, port), timeout=5)
        try:
            done = threading.Event()

            def pump(source: socket.socket, sink: socket.socket) -> None:
                try:
                    while not done.is_set():
                        data = source.recv(8192)
                        if not data:
                            break
                        sink.sendall(data)
                except OSError:
                    pass
                finally:
                    done.set()

            outbound = threading.Thread(target=pump, args=(conn, upstream), daemon=True)
            outbound.start()
            pump(upstream, conn)
            outbound.join(timeout=2)
        finally:
            upstream.close()

    def _intercept(self, conn: socket.socket) -> None:
        """Terminate the TLS here, with a certificate nobody asked this client to trust.

        The whole attack in four lines. If the client completes this handshake,
        every byte of the "encrypted tunnel" is plaintext on this machine: the
        API key, the prompt and the reply.
        """

        assert self._mitm is not None
        try:
            with self._mitm.wrap_socket(conn, server_side=True) as tls:
                tls.recv(4096)
                tls.sendall(_HTTP_OK)
        # ``ssl.SSLError`` is an ``OSError``, so one clause covers the
        # handshake the client refuses -- which is the expected outcome and the
        # point of the exercise.
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _clean_ledgers() -> Iterator[None]:
    reset_proxy_health()
    yield
    reset_proxy_health()


@pytest.fixture
def origin(pki: dict[str, Path]) -> Iterator[_Origin]:
    server = _Origin(pki["honest"])
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def clean_proxy() -> Iterator[_ConnectProxy]:
    proxy = _ConnectProxy()
    try:
        yield proxy
    finally:
        proxy.close()


@pytest.fixture
def mitm_proxy(pki: dict[str, Path]) -> Iterator[_ConnectProxy]:
    proxy = _ConnectProxy(mitm=pki["rogue"])
    try:
        yield proxy
    finally:
        proxy.close()


# ----------------------------------------------------------------- the proof


async def test_a_clean_proxy_passes_with_strict_tls(
    origin: _Origin, clean_proxy: _ConnectProxy
) -> None:
    """A proxy that relays the tunnel verifies the destination and is measured."""

    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
    )

    assert record.ok is True, record.detail
    assert record.tls == TLS_STRICT
    assert record.intercepted is False
    assert record.latency_ms is not None and record.latency_ms >= 0
    assert record.at


async def test_a_proxy_that_terminates_tls_is_marked_intercepted(
    origin: _Origin, mitm_proxy: _ConnectProxy
) -> None:
    """The security control, against the real thing rather than a stub.

    The proxy answers ``200 Connection established`` exactly as the honest one
    does -- it is indistinguishable up to the handshake, which is why a TCP
    check alone would call it healthy and route a key through it.
    """

    record = await check_proxy(
        f"http://127.0.0.1:{mitm_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
    )

    assert record.tls == TLS_INTERCEPTED, record.detail
    assert record.ok is False
    assert record.intercepted is True
    assert "certificate validation" in record.detail


async def test_an_intercepting_proxy_is_refused_across_the_process(
    origin: _Origin, mitm_proxy: _ConnectProxy, clean_proxy: _ConnectProxy
) -> None:
    """A refusal is a process-wide fact, and a later clean check lifts it.

    The two halves matter equally. Without the first, the verdict is a label on
    a page and the chain keeps using the address. Without the second, an
    operator who fixes their network has no way back except editing a file.
    """

    bad_url = f"http://127.0.0.1:{mitm_proxy.port}"
    bad_label = mask_proxy_label(bad_url)
    destination = f"https://{HOSTNAME}:{origin.port}/"

    apply_outcome(bad_label, await check_proxy(bad_url, destination, timeout=10.0))
    assert PROXY_INTERCEPTION.is_refused(bad_label) is True
    # Refused, not benched: the reachability ladder has nothing to say about an
    # address that answered perfectly well and lied.
    assert PROXY_REACHABILITY.remaining(bad_label) == 0.0

    good_url = f"http://127.0.0.1:{clean_proxy.port}"
    good_label = mask_proxy_label(good_url)
    apply_outcome(good_label, await check_proxy(good_url, destination, timeout=10.0))
    assert PROXY_INTERCEPTION.is_refused(good_label) is False

    # The same address, now relaying honestly: the refusal comes off, and only
    # a measurement takes it off.
    PROXY_INTERCEPTION.mark(good_label, "stale verdict")
    apply_outcome(good_label, await check_proxy(good_url, destination, timeout=10.0))
    assert PROXY_INTERCEPTION.is_refused(good_label) is False


async def test_a_dead_address_walks_the_reachability_ladder(origin: _Origin) -> None:
    """A proxy that is not listening is benched without the operator doing anything.

    The port is bound by this test and never listened on, and the socket is
    held for the duration -- which is what a dead free proxy looks like the day
    after somebody scraped it, and, unlike a closed port's number, is a port
    nothing else in a parallel run can be handed.
    """

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_url = f"http://127.0.0.1:{dead.getsockname()[1]}"
    label = mask_proxy_label(dead_url)

    try:
        record = await check_proxy(
            dead_url, f"https://{HOSTNAME}:{origin.port}/", timeout=2.0
        )
    finally:
        dead.close()
    apply_outcome(label, record)

    assert record.ok is False
    assert record.tls == TLS_UNKNOWN
    assert record.intercepted is False
    assert PROXY_REACHABILITY.remaining(label) > 0
    assert PROXY_INTERCEPTION.is_refused(label) is False


async def test_a_refusal_survives_the_proxy_simply_going_offline(
    origin: _Origin, mitm_proxy: _ConnectProxy
) -> None:
    """The regression: a dead check must not retire an interception verdict.

    ``apply_outcome`` used to clear the refusal for any record that was not
    itself an interception, and "did not answer" is such a record. So an
    address caught terminating TLS, later merely offline, lost its verdict and
    could be added to a chain again -- benched, but admitted.

    That is backwards. Failing to connect is not evidence that a machine
    stopped reading the traffic; it is no evidence at all. The refusal is the
    one control standing between a credential and a hostile proxy, and an
    absence must not lift it. Only a check that succeeds can.
    """

    url = f"http://127.0.0.1:{mitm_proxy.port}"
    label = mask_proxy_label(url)
    destination = f"https://{HOSTNAME}:{origin.port}/"

    # Caught in the act.
    apply_outcome(label, await check_proxy(url, destination, timeout=10.0))
    assert PROXY_INTERCEPTION.is_refused(label) is True

    # The same address, now simply not listening. The port is taken over by a
    # socket that never listens, so that "the same address, dead" stays the
    # same address: a closed port's number is free for the next server a
    # parallel run starts, and this test would then be measuring that.
    mitm_proxy.close()
    keeper = socket.socket()
    keeper.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(OSError):
        keeper.bind(("127.0.0.1", mitm_proxy.port))
    try:
        dead = await check_proxy(url, destination, timeout=2.0)
    finally:
        keeper.close()
    apply_outcome(label, dead)

    assert dead.ok is False
    assert dead.intercepted is False, "a closed port is not an interception"
    assert PROXY_INTERCEPTION.is_refused(label) is True, (
        "the interception verdict was lifted by a failure to connect"
    )
    # And it is benched as unreachable as well, which is the other half: the
    # two ledgers answer different questions and both still apply.
    assert PROXY_REACHABILITY.remaining(label) > 0


# ------------------------------------------------------ 7.22.2: the two depths


async def test_the_tls_depth_passes_an_honest_proxy_and_sends_the_origin_nothing(
    origin: _Origin, clean_proxy: _ConnectProxy
) -> None:
    """The default sweep check, against the real shape of it.

    Two assertions and they are the whole feature. The tunnel opened and the
    destination's certificate verified through it -- so the verdict is the same
    verdict the request depth reaches -- and the origin, asked directly, heard
    no request at all. The second one is not inferable from the code: it is the
    provider's side of a thousand-address sweep, measured.
    """

    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
        depth="tls",
    )

    assert record.ok is True, record.detail
    assert record.tls == TLS_STRICT
    assert record.intercepted is False
    assert record.latency_ms is not None and record.latency_ms >= 0
    assert record.depth == "tls"
    # The origin completed a handshake and was then hung up on. Nothing was
    # sent through the tunnel after it.
    assert origin.requests == [], origin.requests


async def test_the_request_depth_still_sends_exactly_one_request(
    origin: _Origin, clean_proxy: _ConnectProxy
) -> None:
    """The golden: 7.22.1's check, byte for byte, still available and still the
    thing Add does.

    Same verdict, same record, and one ``HEAD`` arriving at the origin -- which
    is the difference the depth setting names, stated as a number rather than
    as prose.
    """

    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
        depth="request",
    )

    assert record.ok is True, record.detail
    assert record.tls == TLS_STRICT
    assert record.depth == "request"
    assert len(origin.requests) == 1, origin.requests
    assert origin.requests[0].startswith(b"HEAD ")


async def test_the_default_depth_of_the_checker_itself_is_the_request(
    origin: _Origin, clean_proxy: _ConnectProxy
) -> None:
    """Nobody who does not ask gets the new behaviour.

    The setting's default is ``tls`` and the *sweep* reads it. Every other
    caller -- Test, Add, "Add all working", the background re-prober -- calls
    ``check_proxy`` without a depth, and this pins that those callers are
    unchanged without having to enumerate them.
    """

    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
    )

    assert record.depth == "request"
    assert len(origin.requests) == 1, origin.requests


async def test_the_tls_depth_refuses_an_intercepting_proxy(
    origin: _Origin, mitm_proxy: _ConnectProxy
) -> None:
    """The security control is not what was traded away.

    The interception verdict arrives during the handshake, so stopping at the
    handshake cannot miss it. Same verdict, same durable refusal, and the
    intercepting machine never got a request out of MCC either.
    """

    url = f"http://127.0.0.1:{mitm_proxy.port}"
    record = await check_proxy(
        url, f"https://{HOSTNAME}:{origin.port}/", timeout=10.0, depth="tls"
    )

    assert record.tls == TLS_INTERCEPTED, record.detail
    assert record.ok is False
    assert record.intercepted is True
    assert "certificate validation" in record.detail
    assert record.depth == "tls"

    apply_outcome(mask_proxy_label(url), record)
    assert PROXY_INTERCEPTION.is_refused(mask_proxy_label(url)) is True


async def test_the_tls_depth_calls_a_dead_address_dead(origin: _Origin) -> None:
    """An address with nothing accepting on it is dead, and never an interception.

    The dead address is a socket this test binds and never listens on, held
    open for the duration. Closing a proxy and reusing its port number -- what
    this test did until 7.35.1 -- asks the operating system not to hand that
    port to anything else, and it makes no such promise: on a parallel run it
    was handed to another test's honest proxy, and a port that was supposed to
    be dead answered, verified a certificate and passed.
    """

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    try:
        record = await check_proxy(
            f"http://127.0.0.1:{port}",
            f"https://{HOSTNAME}:{origin.port}/",
            timeout=2.0,
            depth="tls",
        )
    finally:
        dead.close()

    assert record.ok is False
    assert record.tls == TLS_UNKNOWN
    assert record.intercepted is False
    assert record.depth == "tls"


async def test_the_tls_depth_verifies_with_the_library_s_own_default_context(
    pki: dict[str, Path],
) -> None:
    """Where the trust comes from, asserted rather than described.

    The context is whatever ``httpx`` builds for a client that says nothing --
    the same object, not an equivalent one -- which is why the ``tls`` depth
    cannot be more permissive than a request through the same proxy. Two
    properties are checked because they are the two a hand-rolled context gets
    wrong: hostnames are matched, and certificates are required.
    """

    import httpx

    context = proxy_check.default_ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # And it is the same trust store the request path would use: the fixture
    # pointed SSL_CERT_FILE at its own CA, and httpx's own builder honours it,
    # so the two contexts agree on which roots exist.
    theirs = httpx.create_ssl_context()
    assert sorted(cert["serialNumber"] for cert in context.get_ca_certs()) == sorted(
        cert["serialNumber"] for cert in theirs.get_ca_certs()
    )


# ------------------------------------------------- 7.35.1: one socket per address


def _count_dials(monkeypatch: pytest.MonkeyPatch, delay: float = 0.0) -> list[int]:
    """Count every socket the checker opens, optionally slowing each one down."""

    tally = [0]
    real = asyncio.open_connection

    async def counting(host=None, port=None, **kwargs):
        tally[0] += 1
        if delay:
            await asyncio.sleep(delay)
        return await real(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", counting)
    return tally


async def test_the_tls_depth_opens_one_socket_per_address(
    origin: _Origin, clean_proxy: _ConnectProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep reaches a stranger's machine once, not twice.

    Until 7.35.1 this depth dialled the address to ask whether it answered and
    then dialled it again to carry the tunnel. The second dial already answers
    the first question. For a sweep of several hundred addresses that is half
    the connections to strangers' machines, and half the entries in whatever
    they log, for exactly the same verdict -- which is asserted here too, not
    assumed.
    """

    tally = _count_dials(monkeypatch)
    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
        depth="tls",
    )

    assert record.ok is True, record.detail
    assert record.tls == TLS_STRICT
    assert tally[0] == 1, f"the tls depth opened {tally[0]} sockets"


async def test_the_request_depth_still_dials_before_it_builds_a_client(
    origin: _Origin, clean_proxy: _ConnectProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was taken away from the depth every button still uses.

    ``httpx`` owns its own pool and cannot be handed a socket, so the request
    depth reaches the address twice however this is written. The reachability
    dial therefore stays exactly where it was for that depth, and this pins it:
    the sibling test above would otherwise pass just as well if the dial had
    been deleted for everybody.
    """

    calls: list[tuple[str, int]] = []
    real = proxy_check._tcp_connect

    async def recording(host: str, port: int, timeout: float) -> str:
        calls.append((host, port))
        return await real(host, port, timeout)

    monkeypatch.setattr(proxy_check, "_tcp_connect", recording)

    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=10.0,
        depth="request",
    )

    assert record.ok is True, record.detail
    assert calls == [("127.0.0.1", clean_proxy.port)]


async def test_the_tls_depth_keeps_the_reachability_wording_and_carries_no_latency(
    origin: _Origin, clean_proxy: _ConnectProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused connection reads exactly as it did when a separate dial found it.

    The wording is the operator-facing half of the verdict, and folding the
    dial into the handshake is only invisible if it survives. So is the empty
    latency: an address that never answered has no handshake leg to time, and
    the candidate list orders on that number.
    """

    async def refusing(host=None, port=None, **kwargs):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(asyncio, "open_connection", refusing)

    record = await check_proxy(
        f"http://127.0.0.1:{clean_proxy.port}",
        f"https://{HOSTNAME}:{origin.port}/",
        timeout=2.0,
        depth="tls",
    )

    assert record.ok is False
    assert record.tls == TLS_UNKNOWN
    assert record.intercepted is False
    assert record.depth == "tls"
    assert record.detail == (
        f"127.0.0.1:{clean_proxy.port} refused the connection: Connection refused"
    )
    assert record.latency_ms is None


async def test_the_tls_depth_latency_is_the_handshake_and_never_the_dial(
    origin: _Origin, clean_proxy: _ConnectProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half a second of dialling must not show up as half a second of latency.

    With two sockets the measurement started after the first dial and so never
    included one. With one socket it would include the whole dial unless the
    clock is restarted the instant the socket is up -- and since the candidate
    list is ordered by this number, an address on a slow route would have been
    pushed down the list by a change that was supposed to be invisible.

    The comparison calibrates itself against this fixture rather than against a
    fixed number: the same address is measured twice, and the five seconds of
    dialling injected into the second one must not show up in the difference.
    """

    async def measure() -> int:
        record = await check_proxy(
            f"http://127.0.0.1:{clean_proxy.port}",
            f"https://{HOSTNAME}:{origin.port}/",
            timeout=30.0,
            depth="tls",
        )
        assert record.ok is True, record.detail
        assert record.latency_ms is not None
        return record.latency_ms

    fast = await measure()
    _count_dials(monkeypatch, delay=5.0)
    slow = await measure()

    assert slow < fast + 2500, f"the dial leaked into the latency: {fast} -> {slow}"


class _TlsFrontedProxy(_Server):
    """An ``https://`` proxy: the client's very first bytes are a TLS handshake.

    It never gets as far as speaking HTTP, because the certificate it presents
    is the rogue one. That is the whole point of the fixture: for this class of
    proxy the trust decision now happens inside the single dial rather than
    after it, and the verdict must still be interception rather than death.
    """

    def __init__(self, certificate: Path) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(certificate)
        super().__init__()

    def _serve(self, conn: socket.socket) -> None:
        try:
            with self._context.wrap_socket(conn, server_side=True):
                pass
        except OSError:
            pass
        finally:
            conn.close()


async def test_an_https_proxys_own_bad_certificate_is_interception_not_death(
    origin: _Origin, pki: dict[str, Path]
) -> None:
    """The one verdict folding the dial in could have quietly changed.

    An ``https://`` proxy is reached over TLS, so from 7.35.1 its certificate
    is checked during the dial -- and ``ssl.SSLError`` is a subclass of
    ``OSError``, which is the exception the dial turns into "this address is
    dead". Swap those two clauses and a proxy that terminates its own TLS stops
    being refused and starts being retried as an ordinary unreachable address,
    with nothing to show it happened. This is the test that would notice.
    """

    proxy = _TlsFrontedProxy(pki["rogue"])
    try:
        record = await check_proxy(
            f"https://{HOSTNAME}:{proxy.port}",
            f"https://{HOSTNAME}:{origin.port}/",
            timeout=10.0,
            depth="tls",
        )
    finally:
        proxy.close()

    assert record.tls == TLS_INTERCEPTED, record.detail
    assert record.ok is False
    assert record.intercepted is True
    assert "certificate validation" in record.detail
