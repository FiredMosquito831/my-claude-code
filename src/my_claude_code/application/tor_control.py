"""A small client for the control port of a tor the user runs (7.90.0).

Bring-your-own Tor: MCC never downloads, installs or starts tor. The user runs
it with the lines the Proxying page shows; MCC dials its SOCKS ports as chain
addresses, and this module speaks to its control port for two buttons --
*New Tor identity* (``SIGNAL NEWNYM``) and *Check Tor* (``GETINFO``) -- and
for nothing else. Nothing here runs on its own: no timer, no trigger after a
refusal (the user's decision 5(a) of 2026-10-06). A refusal on one Tor port
already moves the request to the next port through the chain's own rotation.

Where it connects: ``127.0.0.1:<control port>``, and only there. The host is
not a parameter. The connection is a plain TCP socket opened by this process
-- never through a proxy chain, never around one, and never to a provider.

The protocol (https://spec.torproject.org/control-spec/), as much of it as
the two buttons need:

1. ``PROTOCOLINFO 1`` -- which ways to log in this tor accepts, the path of
   its cookie file, its version. Allowed once before logging in.
2. Log in, in order of preference for the method the user chose:

   * cookie: ``SAFECOOKIE`` -- ``AUTHCHALLENGE SAFECOOKIE <client nonce>``,
     check tor's ``SERVERHASH`` (an HMAC of the cookie that only the real tor
     can make), then ``AUTHENTICATE <client hash>``: the cookie itself never
     crosses the socket. Plain ``COOKIE`` (``AUTHENTICATE <cookie hex>``)
     only when this tor does not offer ``SAFECOOKIE``. The cookie file is the
     one tor names, read at that moment, exactly 32 bytes or refused, and
     never written anywhere or kept after the call.
   * password: ``AUTHENTICATE <hex of the password>``.
   * ``NULL`` (a control port with no login at all): ``AUTHENTICATE``.

3. ``SIGNAL NEWNYM`` -- new circuits for every *new* stream on every SOCKS
   port. Tor accepts one per 10 seconds and defers the rest (its own
   ``MAX_SIGNEWNYM_RATE``); MCC refuses a second press within that window
   locally, without connecting, and says how long is left.
4. ``GETINFO status/circuit-established``, ``status/bootstrap-phase`` and
   ``net/listeners/socks`` -- for the card. A key an older tor does not know
   (``552``) is "not reported", never an error.
5. ``QUIT``.

Replies are parsed line by line (``250-``, ``250+`` data blocks, ``250 ``),
never with fixed sleeps. No reply text, command or secret is logged here; the
caller logs the outcome in words.
"""

import asyncio
import contextlib
import hashlib
import hmac
import math
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from my_claude_code.config.proxy_sources import (
    LOCAL_HOST,
    TOR_AUTH_COOKIE,
    TOR_AUTH_PASSWORD,
)

#: How long the control port gets to accept, and to answer each command. A
#: local tor answers in milliseconds; this bounds a port that accepts and
#: never speaks (what opencode_lite used for the same socket).
TOR_CONTROL_TIMEOUT_SECONDS = 10.0
#: Tor's own limit: one NEWNYM per 10 seconds (``MAX_SIGNEWNYM_RATE`` in tor's
#: source). A second one inside it is deferred by tor, so MCC does not send it.
NEWNYM_INTERVAL_SECONDS = 10.0
#: Tor's control cookie is always 32 random bytes.
COOKIE_LENGTH = 32
_NONCE_LENGTH = 32
_SERVER_KEY = b"Tor safe cookie authentication server-to-controller hash"
_CLIENT_KEY = b"Tor safe cookie authentication controller-to-server hash"
#: The most lines one reply may have: a control port that floods is not tor.
_MAX_REPLY_LINES = 512
#: The most of tor's own words an error sentence quotes.
_QUOTE_LIMIT = 160

METHOD_SAFECOOKIE = "SAFECOOKIE"
METHOD_COOKIE = "COOKIE"
METHOD_PASSWORD = "HASHEDPASSWORD"
METHOD_NULL = "NULL"


class TorControlError(Exception):
    """Why a control-port call stopped, in a sentence the page shows.

    Never carries a cookie, a password or a command line.
    """


@dataclass(frozen=True, slots=True)
class Reply:
    """One control-port reply: its status and the text of each line."""

    status: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(line.replace("\n", " ") for line in self.lines)


@dataclass(frozen=True, slots=True)
class ProtocolInfo:
    """What ``PROTOCOLINFO`` said: how to log in, the cookie file, the version."""

    methods: frozenset[str]
    cookie_file: str = ""
    version: str = ""


CookieReader = Callable[[str], Awaitable[bytes]]


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _QUOTE_LIMIT else text[: _QUOTE_LIMIT - 1] + "…"


def _unquote(text: str) -> tuple[str, str]:
    """Decode the control spec's QuotedString at the start of ``text``.

    Returns the decoded value and what follows the closing quote. Escapes
    are C-style (``\\\\``, ``\\"``, ``\\n``, ``\\r``, ``\\t``, octal ``\\ooo``),
    as tor writes them for a path with backslashes or odd characters.
    """

    if not text.startswith('"'):
        value, _, rest = text.partition(" ")
        return value, rest
    out: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(out), text[index + 1 :].lstrip()
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            octal = re.match(r"[0-7]{1,3}", text[index + 1 : index + 4])
            if octal:
                out.append(chr(int(octal.group(0), 8)))
                index += 1 + len(octal.group(0))
                continue
            out.append({"n": "\n", "r": "\r", "t": "\t"}.get(following, following))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out), ""


def _quoted_values(text: str) -> list[str]:
    """Every QuotedString (or bare word) in a space-separated list."""

    values: list[str] = []
    rest = text.strip()
    while rest:
        value, rest = _unquote(rest)
        if value:
            values.append(value)
        rest = rest.strip()
    return values


def parse_protocol_info(reply: Reply) -> ProtocolInfo:
    methods: set[str] = set()
    cookie_file = ""
    version = ""
    for line in reply.lines:
        if line.startswith("AUTH "):
            match = re.search(r"METHODS=([A-Za-z0-9_,]+)", line)
            if match:
                methods = {item.upper() for item in match.group(1).split(",") if item}
            position = line.find("COOKIEFILE=")
            if position >= 0:
                cookie_file, _ = _unquote(line[position + len("COOKIEFILE=") :])
        elif line.startswith("VERSION "):
            position = line.find("Tor=")
            if position >= 0:
                version, _ = _unquote(line[position + len("Tor=") :])
    return ProtocolInfo(
        methods=frozenset(methods), cookie_file=cookie_file, version=version
    )


def _read_cookie_now(path: str) -> bytes:
    """The cookie file tor named, read this instant. Exactly 32 bytes or nothing.

    Tor's cookie is always 32 random bytes; refusing any other size keeps a
    port that only pretends to be tor from naming some other file of the
    user's and having MCC send its bytes back.
    """

    cookie_path = Path(path)
    try:
        size = cookie_path.stat().st_size
        if size != COOKIE_LENGTH:
            raise TorControlError(
                f"The cookie file tor names ({path}) is {size} bytes, not "
                f"{COOKIE_LENGTH}, so MCC does not use it."
            )
        data = cookie_path.read_bytes()
    except OSError as exc:
        raise TorControlError(
            f"MCC could not read the cookie file tor names ({path}): "
            f"{type(exc).__name__}. If tor runs as another user or inside WSL, "
            "use a control password instead."
        ) from None
    if len(data) != COOKIE_LENGTH:
        raise TorControlError(
            f"The cookie file tor names ({path}) changed while it was read."
        )
    return data


async def read_cookie_file(path: str) -> bytes:
    return await asyncio.to_thread(_read_cookie_now, path)


class TorControl:
    """One authenticated-or-not connection to ``127.0.0.1:<control port>``."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        port: int,
        timeout: float,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._port = port
        self._timeout = timeout

    @classmethod
    async def connect(
        cls, port: int, *, timeout: float = TOR_CONTROL_TIMEOUT_SECONDS
    ) -> TorControl:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(LOCAL_HOST, port), timeout
            )
        except TimeoutError:
            raise TorControlError(
                f"{LOCAL_HOST}:{port} did not accept a connection within {timeout:g} s."
            ) from None
        except OSError:
            raise TorControlError(
                f"Nothing answers on {LOCAL_HOST}:{port}. Is tor running with "
                f"ControlPort {LOCAL_HOST}:{port} in its torrc?"
            ) from None
        return cls(reader, writer, port=port, timeout=timeout)

    async def _readline(self) -> str:
        try:
            raw = await asyncio.wait_for(self._reader.readline(), self._timeout)
        except TimeoutError:
            raise TorControlError(
                f"{LOCAL_HOST}:{self._port} stopped answering for {self._timeout:g} s."
            ) from None
        except (ValueError, OSError) as exc:
            raise TorControlError(
                f"{LOCAL_HOST}:{self._port} broke off the conversation "
                f"({type(exc).__name__})."
            ) from None
        if not raw:
            raise TorControlError(f"{LOCAL_HOST}:{self._port} closed the connection.")
        return raw.decode("utf-8", "replace").rstrip("\r\n")

    async def read_reply(self) -> Reply:
        lines: list[str] = []
        while len(lines) < _MAX_REPLY_LINES:
            line = await self._readline()
            if len(line) < 4 or not line[:3].isdigit() or line[3] not in "- +":
                raise TorControlError(
                    f"{LOCAL_HOST}:{self._port} does not answer like a tor "
                    "control port."
                )
            status, separator, text = int(line[:3]), line[3], line[4:]
            if status // 100 == 6:
                # An asynchronous event. MCC subscribes to none; skip it.
                continue
            if separator == "+":
                block = [text]
                while len(block) < _MAX_REPLY_LINES:
                    data = await self._readline()
                    if data == ".":
                        break
                    block.append(data[1:] if data.startswith("..") else data)
                lines.append("\n".join(block))
                continue
            lines.append(text)
            if separator == " ":
                return Reply(status=status, lines=tuple(lines))
        raise TorControlError(
            f"{LOCAL_HOST}:{self._port} sent a reply too long for a tor control port."
        )

    async def send(self, command: str) -> Reply:
        """Send one command line and read its reply. The command is never logged."""

        try:
            self._writer.write(command.encode("utf-8") + b"\r\n")
            await asyncio.wait_for(self._writer.drain(), self._timeout)
        except (OSError, TimeoutError) as exc:
            raise TorControlError(
                f"{LOCAL_HOST}:{self._port} could not be written to "
                f"({type(exc).__name__})."
            ) from None
        return await self.read_reply()

    async def protocol_info(self) -> ProtocolInfo:
        reply = await self.send("PROTOCOLINFO 1")
        if reply.status != 250:
            raise TorControlError(
                f"PROTOCOLINFO was refused ({reply.status} {_quote(reply.text)})."
            )
        return parse_protocol_info(reply)

    async def _authenticate(self, argument: str, what: str) -> None:
        reply = await self.send(f"AUTHENTICATE {argument}".rstrip())
        if reply.status != 250:
            raise TorControlError(
                f"Tor refused {what} ({reply.status} {_quote(reply.text)})."
            )

    async def authenticate(
        self,
        info: ProtocolInfo,
        *,
        auth: str,
        password: str = "",
        read_cookie: CookieReader = read_cookie_file,
    ) -> str:
        """Log in the way ``auth`` says; return the method tor accepted."""

        methods = info.methods
        offered = ", ".join(sorted(methods)) or "nothing"
        if auth == TOR_AUTH_PASSWORD:
            if METHOD_PASSWORD in methods:
                if not password:
                    raise TorControlError("No control password is stored.")
                await self._authenticate(
                    password.encode("utf-8").hex(), "the control password"
                )
                return METHOD_PASSWORD
            if METHOD_NULL in methods:
                await self._authenticate("", "an open login")
                return METHOD_NULL
            raise TorControlError(
                f"This tor does not take a control password (it offers {offered}). "
                "Choose the cookie file instead, or add HashedControlPassword to "
                "its torrc."
            )
        if METHOD_SAFECOOKIE in methods and info.cookie_file:
            await self._safecookie(info.cookie_file, read_cookie)
            return METHOD_SAFECOOKIE
        if METHOD_COOKIE in methods and info.cookie_file:
            cookie = await read_cookie(info.cookie_file)
            await self._authenticate(cookie.hex(), "the cookie")
            return METHOD_COOKIE
        if METHOD_NULL in methods:
            await self._authenticate("", "an open login")
            return METHOD_NULL
        if METHOD_PASSWORD in methods:
            raise TorControlError(
                "This tor asks for a control password: choose the control "
                "password and type it."
            )
        raise TorControlError(
            f"This tor offers no login MCC speaks (it offers {offered})."
        )

    async def _safecookie(self, cookie_file: str, read_cookie: CookieReader) -> None:
        cookie = await read_cookie(cookie_file)
        client_nonce = os.urandom(_NONCE_LENGTH)
        reply = await self.send(f"AUTHCHALLENGE SAFECOOKIE {client_nonce.hex()}")
        if reply.status != 250:
            raise TorControlError(
                f"Tor refused the cookie challenge ({reply.status} "
                f"{_quote(reply.text)})."
            )
        server_hash_hex = re.search(r"SERVERHASH=([0-9A-Fa-f]{64})", reply.text)
        server_nonce_hex = re.search(r"SERVERNONCE=([0-9A-Fa-f]{64})", reply.text)
        if server_hash_hex is None or server_nonce_hex is None:
            raise TorControlError(
                "The cookie challenge's answer is not what tor sends."
            )
        server_nonce = bytes.fromhex(server_nonce_hex.group(1))
        message = cookie + client_nonce + server_nonce
        expected = hmac.new(_SERVER_KEY, message, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, bytes.fromhex(server_hash_hex.group(1))):
            raise TorControlError(
                "What answers on the control port could not prove it knows "
                "tor's cookie, so MCC sent nothing more: the cookie file is "
                "not this tor's, or what answers is not tor."
            )
        client_hash = hmac.new(_CLIENT_KEY, message, hashlib.sha256).hexdigest()
        await self._authenticate(client_hash, "the cookie")

    async def getinfo(self, key: str) -> str | None:
        """One ``GETINFO`` value, or ``None`` when this tor does not know the key."""

        reply = await self.send(f"GETINFO {key}")
        if reply.status == 552:
            return None
        if reply.status != 250:
            raise TorControlError(
                f"GETINFO {key} was refused ({reply.status} {_quote(reply.text)})."
            )
        for line in reply.lines:
            name, sep, value = line.partition("=")
            if sep and name == key:
                return value.removeprefix("\n")
        return None

    async def signal_newnym(self) -> None:
        reply = await self.send("SIGNAL NEWNYM")
        if reply.status != 250:
            raise TorControlError(
                f"Tor refused the new identity ({reply.status} {_quote(reply.text)})."
            )

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._writer.write(b"QUIT\r\n")
            await asyncio.wait_for(self._writer.drain(), 1.0)
            await asyncio.wait_for(self._reader.readline(), 1.0)
        self._writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), 1.0)


# ------------------------------------------------------------------ readings


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class TorReading:
    """What the last *Check Tor* (or *New Tor identity*) learned. In memory only.

    ``socks_listeners`` is tor's own list of the SOCKS ports it listens on
    (``net/listeners/socks``), so the card can say which of the ports the
    user typed tor really has -- learned from the control port alone, with
    nothing sent to the SOCKS ports themselves.
    """

    at: str
    ok: bool
    sentence: str
    version: str = ""
    auth_method: str = ""
    circuit_established: bool | None = None
    bootstrap: int | None = None
    socks_listeners: tuple[int, ...] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "ok": self.ok,
            "sentence": self.sentence,
            "version": self.version,
            "auth_method": self.auth_method,
            "circuit_established": self.circuit_established,
            "bootstrap": self.bootstrap,
            "socks_listeners": None
            if self.socks_listeners is None
            else list(self.socks_listeners),
        }


def _listener_ports(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    ports: list[int] = []
    for item in _quoted_values(value):
        _host, sep, port = item.rpartition(":")
        if sep and port.isdigit():
            ports.append(int(port))
    return tuple(ports)


def _bootstrap(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"PROGRESS=(\d+)", value)
    return int(match.group(1)) if match else None


def _circuit(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip() == "1"


async def _status_on(control: TorControl) -> dict[str, Any]:
    circuit = _circuit(await control.getinfo("status/circuit-established"))
    bootstrap = _bootstrap(await control.getinfo("status/bootstrap-phase"))
    listeners = _listener_ports(await control.getinfo("net/listeners/socks"))
    return {
        "circuit_established": circuit,
        "bootstrap": bootstrap,
        "socks_listeners": listeners,
    }


def _status_sentence(version: str, method: str, status: dict[str, Any]) -> str:
    parts = [f"Tor {version}" if version else "Tor answers"]
    circuit = status["circuit_established"]
    if circuit is True:
        parts.append("circuit established")
    elif circuit is False:
        bootstrap = status["bootstrap"]
        parts.append(
            f"no circuit yet (bootstrapped {bootstrap}%)"
            if bootstrap is not None
            else "no circuit yet"
        )
    parts.append(
        {
            METHOD_SAFECOOKIE: "logged in with the cookie (SAFECOOKIE)",
            METHOD_COOKIE: "logged in with the cookie",
            METHOD_PASSWORD: "logged in with the control password",
            METHOD_NULL: "the control port has no login -- anything on this "
            "computer can steer this tor",
        }.get(method, "logged in")
    )
    return " · ".join(parts) + "."


async def read_tor_status(
    control_port: int,
    *,
    auth: str = TOR_AUTH_COOKIE,
    password: str = "",
    read_cookie: CookieReader = read_cookie_file,
    timeout: float = TOR_CONTROL_TIMEOUT_SECONDS,
) -> TorReading:
    """Log in to ``127.0.0.1:<control_port>`` and read the card's facts.

    Never raises for a tor that is not there or refuses: the reading says
    why, in a sentence.
    """

    try:
        control = await TorControl.connect(control_port, timeout=timeout)
    except TorControlError as exc:
        return TorReading(at=_now_iso(), ok=False, sentence=str(exc))
    try:
        info = await control.protocol_info()
        method = await control.authenticate(
            info, auth=auth, password=password, read_cookie=read_cookie
        )
        status = await _status_on(control)
    except TorControlError as exc:
        return TorReading(at=_now_iso(), ok=False, sentence=str(exc))
    finally:
        await control.close()
    return TorReading(
        at=_now_iso(),
        ok=True,
        sentence=_status_sentence(info.version, method, status),
        version=info.version,
        auth_method=method,
        circuit_established=status["circuit_established"],
        bootstrap=status["bootstrap"],
        socks_listeners=status["socks_listeners"],
    )


# ------------------------------------------------------------------ NEWNYM


@dataclass
class NewnymGuard:
    """One accepted NEWNYM per control port per :data:`NEWNYM_INTERVAL_SECONDS`.

    Kept by this process. Checked before connecting, so a refused press
    sends nothing at all; a press is reserved while it is being sent, so two
    quick clicks cannot both go out.
    """

    interval: float = NEWNYM_INTERVAL_SECONDS
    clock: Callable[[], float] = time.monotonic
    _accepted: dict[int, tuple[float, str]] = field(default_factory=dict)
    _sending: set[int] = field(default_factory=set)

    def wait_seconds(self, port: int) -> int:
        """Whole seconds until a new identity may be asked for on ``port``."""

        if port in self._sending:
            return math.ceil(self.interval)
        last = self._accepted.get(port)
        if last is None:
            return 0
        left = self.interval - (self.clock() - last[0])
        return max(0, math.ceil(left))

    def last_at(self, port: int) -> str:
        last = self._accepted.get(port)
        return last[1] if last is not None else ""

    def reserve(self, port: int) -> int:
        """``0`` and the slot is held; otherwise the seconds still to wait."""

        wait = self.wait_seconds(port)
        if wait == 0:
            self._sending.add(port)
        return wait

    def accepted(self, port: int) -> None:
        self._sending.discard(port)
        self._accepted[port] = (self.clock(), _now_iso())

    def release(self, port: int) -> None:
        self._sending.discard(port)

    def clear(self) -> None:
        self._accepted.clear()
        self._sending.clear()


NEWNYM_GUARD = NewnymGuard()


@dataclass(frozen=True, slots=True)
class NewnymOutcome:
    """What one press of *New Tor identity* did."""

    accepted: bool
    sentence: str
    wait_seconds: int = 0
    #: ``True`` when MCC refused locally and nothing was sent.
    refused_locally: bool = False
    reading: TorReading | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "action": "newnym",
            "accepted": self.accepted,
            "sentence": self.sentence,
            "wait_seconds": self.wait_seconds,
            "refused_locally": self.refused_locally,
        }


async def send_newnym(
    control_port: int,
    *,
    auth: str = TOR_AUTH_COOKIE,
    password: str = "",
    guard: NewnymGuard = NEWNYM_GUARD,
    read_cookie: CookieReader = read_cookie_file,
    timeout: float = TOR_CONTROL_TIMEOUT_SECONDS,
) -> NewnymOutcome:
    """Ask the tor at ``127.0.0.1:<control_port>`` for new circuits.

    Refused here, with nothing sent, within 10 s of the last one this process
    sent through that port.
    """

    wait = guard.reserve(control_port)
    if wait:
        return NewnymOutcome(
            accepted=False,
            refused_locally=True,
            wait_seconds=wait,
            sentence=(
                "Tor allows one new identity per "
                f"{NEWNYM_INTERVAL_SECONDS:g} seconds: try again in {wait} s. "
                "Nothing was sent."
            ),
        )
    try:
        control = await TorControl.connect(control_port, timeout=timeout)
    except TorControlError as exc:
        guard.release(control_port)
        return NewnymOutcome(accepted=False, sentence=str(exc))
    try:
        info = await control.protocol_info()
        method = await control.authenticate(
            info, auth=auth, password=password, read_cookie=read_cookie
        )
        await control.signal_newnym()
        guard.accepted(control_port)
        try:
            status = await _status_on(control)
        except TorControlError:
            # The new identity was accepted; only the card's facts are missing.
            status = dict.fromkeys(
                ("circuit_established", "bootstrap", "socks_listeners")
            )
    except TorControlError as exc:
        guard.release(control_port)
        return NewnymOutcome(accepted=False, sentence=str(exc))
    finally:
        await control.close()
    reading = TorReading(
        at=_now_iso(),
        ok=True,
        sentence=_status_sentence(info.version, method, status),
        version=info.version,
        auth_method=method,
        circuit_established=status["circuit_established"],
        bootstrap=status["bootstrap"],
        socks_listeners=status["socks_listeners"],
    )
    return NewnymOutcome(
        accepted=True,
        wait_seconds=math.ceil(guard.interval),
        sentence=(
            "Tor accepted: new requests on every one of its SOCKS ports get new "
            "circuits, so new exits. A connection still open keeps its exit "
            "until it closes."
        ),
        reading=reading,
    )


# ------------------------------------------------------------- the registry


class TorReadings:
    """The last reading per Tor source, for the card. In memory, never saved."""

    def __init__(self) -> None:
        self._readings: dict[str, TorReading] = {}

    def record(self, source_id: str, reading: TorReading) -> None:
        self._readings[source_id] = reading

    def get(self, source_id: str) -> TorReading | None:
        return self._readings.get(source_id)

    def forget(self, source_id: str) -> None:
        self._readings.pop(source_id, None)

    def clear(self) -> None:
        self._readings.clear()


TOR_READINGS = TorReadings()


def reset_tor_control_state() -> None:
    """Forget every reading and every NEWNYM time (tests; a fresh process)."""

    TOR_READINGS.clear()
    NEWNYM_GUARD.clear()


__all__ = [
    "COOKIE_LENGTH",
    "METHOD_COOKIE",
    "METHOD_NULL",
    "METHOD_PASSWORD",
    "METHOD_SAFECOOKIE",
    "NEWNYM_GUARD",
    "NEWNYM_INTERVAL_SECONDS",
    "TOR_CONTROL_TIMEOUT_SECONDS",
    "TOR_READINGS",
    "NewnymGuard",
    "NewnymOutcome",
    "ProtocolInfo",
    "Reply",
    "TorControl",
    "TorControlError",
    "TorReading",
    "TorReadings",
    "parse_protocol_info",
    "read_cookie_file",
    "read_tor_status",
    "reset_tor_control_state",
    "send_newnym",
]
