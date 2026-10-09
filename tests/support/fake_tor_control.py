"""A fake tor control port: the far side of ``application/tor_control.py``.

No real tor is ever started by MCC or by its tests (the user's rule: MCC never
downloads, installs or starts tor). This speaks the part of the control
protocol MCC uses, the way tor 0.4.x answers it
(https://spec.torproject.org/control-spec/):

* ``PROTOCOLINFO 1`` -- ``AUTH METHODS=... COOKIEFILE="..."`` and
  ``VERSION Tor="..."``; allowed once before logging in;
* ``AUTHCHALLENGE SAFECOOKIE <nonce>`` -- answers ``SERVERHASH`` and
  ``SERVERNONCE`` computed from the cookie file, and later checks the client
  hash (or answers a wrong ``SERVERHASH`` when ``lie_about_cookie`` is set,
  the way something that is not tor would);
* ``AUTHENTICATE`` -- cookie hex, password hex or quoted, or nothing (NULL);
  ``515`` and a closed connection on a bad login, as tor does;
* ``GETINFO`` -- ``status/circuit-established``, ``status/bootstrap-phase``,
  ``net/listeners/socks``; ``552`` for anything else;
* ``SIGNAL NEWNYM`` (counted), ``QUIT``.

The cookie is a real 32-byte file under the test's directory: what MCC reads
when it logs in. :meth:`FakeTorControl.rotate_cookie` writes a new one, the
way tor does on every start, so a test can tell a cookie read at the moment of
the click from one remembered earlier.

Everything binds ``127.0.0.1``; :func:`run_fake_tor` runs one on its own
event loop thread for synchronous (``TestClient``) tests.
"""

import asyncio
import contextlib
import hashlib
import hmac
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SERVER_KEY = b"Tor safe cookie authentication server-to-controller hash"
CLIENT_KEY = b"Tor safe cookie authentication controller-to-server hash"


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class FakeTorControl:
    """One fake control port. Configure, :meth:`start`, then read what it saw."""

    cookie_path: Path
    methods: tuple[str, ...] = ("COOKIE", "SAFECOOKIE")
    password: str = ""
    version: str = "0.4.8.13"
    circuit_established: bool = True
    bootstrap: int = 100
    socks_ports: tuple[int, ...] = ()
    #: What ``SIGNAL NEWNYM`` answers (tor answers ``250 OK``).
    newnym_reply: str = "250 OK"
    #: Answer the cookie challenge with a hash no real tor would send.
    lie_about_cookie: bool = False
    #: What ``COOKIEFILE`` names; the real cookie path when empty.
    advertised_cookie: str = ""
    #: ``GETINFO`` keys this tor answers ``552`` for, like an older tor.
    unknown_keys: tuple[str, ...] = ()
    port: int = 0
    #: Every command line received, in order, secrets included -- the fake
    #: is the one place a test may look at what was sent.
    commands: list[str] = field(default_factory=list)
    connections: int = 0
    peers: list[tuple[str, int]] = field(default_factory=list)
    newnyms: int = 0
    logins: list[str] = field(default_factory=list)
    _server: asyncio.Server | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.cookie_path.exists():
            self.rotate_cookie()

    def rotate_cookie(self) -> bytes:
        """Write a fresh 32-byte cookie, as tor does when it starts."""

        cookie = os.urandom(32)
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_path.write_bytes(cookie)
        return cookie

    @property
    def cookie(self) -> bytes:
        return self.cookie_path.read_bytes()

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None

    def seen(self) -> list[str]:
        with self._lock:
            return list(self.commands)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        with self._lock:
            self.connections += 1
            self.peers.append((str(peer[0]), int(peer[1])))
        session: dict[str, Any] = {"authed": False, "protocolinfo": 0}
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self._lock:
                    self.commands.append(line)
                answer, close = self._answer(line, session)
                writer.write(answer.encode("utf-8"))
                await writer.drain()
                if close:
                    return
        except ConnectionError, asyncio.IncompleteReadError:
            return
        finally:
            with contextlib.suppress(BaseException):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 1.0)

    def _answer(self, line: str, session: dict[str, Any]) -> tuple[str, bool]:
        verb, _, rest = line.partition(" ")
        verb = verb.upper()
        if verb == "QUIT":
            return "250 closing connection\r\n", True
        if verb == "PROTOCOLINFO":
            session["protocolinfo"] += 1
            if session["protocolinfo"] > 1 and not session["authed"]:
                return "514 Authentication required.\r\n", True
            cookie = self.advertised_cookie or str(self.cookie_path)
            auth = "250-AUTH METHODS=" + ",".join(self.methods)
            if {"COOKIE", "SAFECOOKIE"} & set(self.methods):
                auth += f" COOKIEFILE={_quoted(cookie)}"
            return (
                "250-PROTOCOLINFO 1\r\n"
                f"{auth}\r\n"
                f"250-VERSION Tor={_quoted(self.version)}\r\n"
                "250 OK\r\n"
            ), False
        if verb == "AUTHCHALLENGE":
            kind, _, nonce_hex = rest.partition(" ")
            if kind.upper() != "SAFECOOKIE" or "SAFECOOKIE" not in self.methods:
                return "513 AUTHCHALLENGE only supports SAFECOOKIE\r\n", True
            client_nonce = bytes.fromhex(nonce_hex.strip())
            server_nonce = os.urandom(32)
            message = self.cookie + client_nonce + server_nonce
            server_hash = hmac.new(SERVER_KEY, message, hashlib.sha256).digest()
            if self.lie_about_cookie:
                server_hash = bytes(32)
            session["safecookie"] = message
            return (
                f"250 AUTHCHALLENGE SERVERHASH={server_hash.hex().upper()} "
                f"SERVERNONCE={server_nonce.hex().upper()}\r\n"
            ), False
        if verb == "AUTHENTICATE":
            method = self._login(rest.strip(), session)
            if method is None:
                return "515 Authentication failed: wrong credentials.\r\n", True
            session["authed"] = True
            with self._lock:
                self.logins.append(method)
            return "250 OK\r\n", False
        if not session["authed"]:
            return "514 Authentication required.\r\n", True
        if verb == "GETINFO":
            return self._getinfo(rest.strip()), False
        if verb == "SIGNAL" and rest.strip().upper() == "NEWNYM":
            with self._lock:
                self.newnyms += 1
            return self.newnym_reply + "\r\n", False
        return f'510 Unrecognized command "{verb}"\r\n', False

    def _login(self, argument: str, session: dict[str, Any]) -> str | None:
        if "safecookie" in session:
            expected = hmac.new(
                CLIENT_KEY, session.pop("safecookie"), hashlib.sha256
            ).hexdigest()
            return "SAFECOOKIE" if argument.lower() == expected else None
        if not argument:
            return "NULL" if "NULL" in self.methods else None
        if argument.startswith('"'):
            value = argument[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            ok = "HASHEDPASSWORD" in self.methods and value == self.password
            return "HASHEDPASSWORD" if ok else None
        try:
            given = bytes.fromhex(argument)
        except ValueError:
            return None
        if "COOKIE" in self.methods and given == self.cookie:
            return "COOKIE"
        if "HASHEDPASSWORD" in self.methods and given == self.password.encode():
            return "HASHEDPASSWORD"
        return None

    def _getinfo(self, key: str) -> str:
        if key in self.unknown_keys:
            return f'552 Unrecognized key "{key}"\r\n'
        if key == "status/circuit-established":
            value = "1" if self.circuit_established else "0"
        elif key == "status/bootstrap-phase":
            value = (
                f'NOTICE BOOTSTRAP PROGRESS={self.bootstrap} TAG=done SUMMARY="Done"'
            )
        elif key == "net/listeners/socks":
            value = " ".join(f'"127.0.0.1:{port}"' for port in self.socks_ports)
        else:
            return f'552 Unrecognized key "{key}"\r\n'
        return f"250-{key}={value}\r\n250 OK\r\n"


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="fake-tor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(10.0)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10.0)
        if not self._thread.is_alive():
            self.loop.close()


@contextlib.contextmanager
def run_fake_tor(fake: FakeTorControl, port: int = 0) -> Iterator[FakeTorControl]:
    """Run ``fake`` on its own loop thread for the length of the block."""

    loop = _LoopThread()
    try:
        loop.call(fake.start(port))
        yield fake
    finally:
        with contextlib.suppress(BaseException):
            loop.call(fake.stop())
        loop.close()


__all__ = ["CLIENT_KEY", "SERVER_KEY", "FakeTorControl", "run_fake_tor"]
