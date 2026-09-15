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
        super().__init__()

    def _serve(self, conn: socket.socket) -> None:
        try:
            with self._context.wrap_socket(conn, server_side=True) as tls:
                tls.recv(4096)
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


async def test_a_dead_address_walks_the_reachability_ladder(
    origin: _Origin, clean_proxy: _ConnectProxy
) -> None:
    """A proxy that is not listening is benched without the operator doing anything.

    The port is one the fixture bound and then closed, so it is a refused
    connection rather than a hang -- which is what a dead free proxy actually
    looks like the day after somebody scraped it.
    """

    clean_proxy.close()
    dead_url = f"http://127.0.0.1:{clean_proxy.port}"
    label = mask_proxy_label(dead_url)

    record = await check_proxy(
        dead_url, f"https://{HOSTNAME}:{origin.port}/", timeout=2.0
    )
    apply_outcome(label, record)

    assert record.ok is False
    assert record.tls == TLS_UNKNOWN
    assert record.intercepted is False
    assert PROXY_REACHABILITY.remaining(label) > 0
    assert PROXY_INTERCEPTION.is_refused(label) is False
