"""The tor control client (7.90.0) against a fake control port.

No real tor is started -- by MCC or by these tests. The fake
(``tests/support/fake_tor_control.py``) answers the way tor 0.4.x does, keeps
every command it was sent, and checks a login the way tor checks it. What is
pinned here:

* every login MCC speaks: PROTOCOLINFO, then SAFECOOKIE (the cookie never
  crosses the socket), plain cookie hex, a control password, an open port;
* a control port that cannot prove it knows the cookie gets nothing more;
* the cookie file is the one tor names, read at the moment of the call --
  never remembered -- and refused unless it is tor's 32 bytes;
* SIGNAL NEWNYM is accepted once, and a second press within 10 s is refused
  here, with the seconds left, before anything is sent;
* GETINFO gives the card its facts, and a key an older tor does not know is
  "not reported", not an error;
* the only address ever dialled is 127.0.0.1:<control port>.
"""

import asyncio
from pathlib import Path
from typing import cast

import pytest
from loguru import logger

from my_claude_code.application import tor_control
from my_claude_code.application.tor_control import (
    NewnymGuard,
    Reply,
    TorControl,
    TorControlError,
    parse_protocol_info,
    read_tor_status,
    send_newnym,
)
from tests.support.fake_tor_control import FakeTorControl
from tests.support.masking_harness import closed_port

# Every test but the parsers serves a fake control port on a loopback socket.
pytestmark = [pytest.mark.asyncio, pytest.mark.local_serial]

PASSWORD = "correct-horse-battery-staple-77"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def logged():
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink)


async def _fake(tmp_path: Path, **kwargs) -> FakeTorControl:
    fake = FakeTorControl(
        cookie_path=tmp_path / "tor" / "control_auth_cookie", **kwargs
    )
    await fake.start()
    return fake


def _secret_forms(secret: bytes) -> list[str]:
    return [secret.hex(), secret.hex().upper()]


async def test_safecookie_logs_in_without_the_cookie_crossing_the_socket(
    tmp_path: Path, logged: list[str]
) -> None:
    fake = await _fake(tmp_path, socks_ports=(19250, 19251))
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert reading.ok, reading.sentence
    assert reading.auth_method == "SAFECOOKIE"
    assert reading.version == "0.4.8.13"
    assert reading.circuit_established is True
    assert reading.bootstrap == 100
    assert reading.socks_listeners == (19250, 19251)
    assert reading.sentence == (
        "Tor 0.4.8.13 · circuit established · logged in with the cookie (SAFECOOKIE)."
    )
    seen = fake.seen()
    assert [line.split(" ")[0] for line in seen] == [
        "PROTOCOLINFO",
        "AUTHCHALLENGE",
        "AUTHENTICATE",
        "GETINFO",
        "GETINFO",
        "GETINFO",
        "QUIT",
    ]
    assert seen[0] == "PROTOCOLINFO 1"
    assert fake.logins == ["SAFECOOKIE"]
    for form in _secret_forms(fake.cookie):
        assert not any(form in line for line in seen), "the cookie was sent"
        assert form not in reading.sentence
        assert not any(form in line for line in logged)


async def test_hex_cookie_when_tor_offers_no_safecookie(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, methods=("COOKIE",))
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert reading.ok, reading.sentence
    assert reading.auth_method == "COOKIE"
    assert f"AUTHENTICATE {fake.cookie.hex()}" in fake.seen()
    assert "AUTHCHALLENGE" not in " ".join(fake.seen())


async def test_password_login_sends_the_password_hex(
    tmp_path: Path, logged: list[str]
) -> None:
    fake = await _fake(tmp_path, methods=("HASHEDPASSWORD",), password=PASSWORD)
    try:
        reading = await read_tor_status(fake.port, auth="password", password=PASSWORD)
    finally:
        await fake.stop()

    assert reading.ok, reading.sentence
    assert reading.auth_method == "HASHEDPASSWORD"
    assert f"AUTHENTICATE {PASSWORD.encode().hex()}" in fake.seen()
    assert "logged in with the control password" in reading.sentence
    assert PASSWORD not in reading.sentence
    assert not any(PASSWORD in line for line in logged)


async def test_a_wrong_password_is_refused_and_never_repeated(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, methods=("HASHEDPASSWORD",), password=PASSWORD)
    try:
        reading = await read_tor_status(fake.port, auth="password", password="nope")
    finally:
        await fake.stop()

    assert not reading.ok
    assert reading.sentence.startswith("Tor refused the control password (515 ")
    assert "nope" not in reading.sentence
    assert "6e6f7065" not in reading.sentence


async def test_password_chosen_but_tor_takes_only_the_cookie(tmp_path: Path) -> None:
    fake = await _fake(tmp_path)
    try:
        reading = await read_tor_status(fake.port, auth="password", password=PASSWORD)
    finally:
        await fake.stop()

    assert not reading.ok
    assert "does not take a control password (it offers COOKIE, SAFECOOKIE)" in (
        reading.sentence
    )
    assert not any(line.startswith("AUTHENTICATE") for line in fake.seen())


async def test_cookie_chosen_but_tor_asks_for_a_password(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, methods=("HASHEDPASSWORD",), password=PASSWORD)
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert not reading.ok
    assert "asks for a control password" in reading.sentence


async def test_an_open_control_port_logs_in_with_nothing(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, methods=("NULL",))
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert reading.ok
    assert reading.auth_method == "NULL"
    assert "AUTHENTICATE" in fake.seen()
    assert "anything on this computer can steer this tor" in reading.sentence


async def test_a_port_that_cannot_prove_the_cookie_gets_nothing_more(
    tmp_path: Path,
) -> None:
    fake = await _fake(tmp_path, lie_about_cookie=True)
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert not reading.ok
    assert "could not prove it knows tor's cookie" in reading.sentence
    assert not any(line.startswith("AUTHENTICATE") for line in fake.seen())


async def test_a_cookie_file_that_is_not_32_bytes_is_never_used(
    tmp_path: Path,
) -> None:
    other = tmp_path / "not-a-cookie.txt"
    other.write_bytes(b"OPENAI_API_KEY=sk-not-a-real-key-at-all!\n")
    assert len(other.read_bytes()) == 41
    fake = await _fake(tmp_path, methods=("COOKIE",), advertised_cookie=str(other))
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert not reading.ok
    assert "is 41 bytes, not 32" in reading.sentence
    assert [line.split(" ")[0] for line in fake.seen()] == ["PROTOCOLINFO", "QUIT"]


async def test_an_unreadable_cookie_file_says_use_a_password(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, advertised_cookie=str(tmp_path / "missing" / "c"))
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert not reading.ok
    assert "could not read the cookie file tor names" in reading.sentence
    assert "use a control password instead" in reading.sentence


async def test_the_cookie_is_read_at_the_moment_of_each_call(tmp_path: Path) -> None:
    """Tor writes a new cookie every time it starts; a remembered one would fail."""

    fake = await _fake(tmp_path)
    reads: list[str] = []
    real = tor_control.read_cookie_file

    async def counting(path: str) -> bytes:
        reads.append(path)
        return await real(path)

    try:
        first = await read_tor_status(fake.port, read_cookie=counting)
        fake.rotate_cookie()
        second = await read_tor_status(fake.port, read_cookie=counting)
    finally:
        await fake.stop()

    assert first.ok and second.ok, (first.sentence, second.sentence)
    assert reads == [str(fake.cookie_path)] * 2
    assert fake.logins == ["SAFECOOKIE", "SAFECOOKIE"]


async def test_newnym_is_accepted_and_the_card_learns_the_circuit(
    tmp_path: Path,
) -> None:
    fake = await _fake(tmp_path, socks_ports=(19250,))
    guard = NewnymGuard(clock=Clock())
    try:
        outcome = await send_newnym(fake.port, guard=guard)
    finally:
        await fake.stop()

    assert outcome.accepted, outcome.sentence
    assert fake.newnyms == 1
    assert "SIGNAL NEWNYM" in fake.seen()
    assert outcome.sentence.startswith("Tor accepted: new requests on every one")
    assert outcome.wait_seconds == 10
    assert outcome.reading is not None
    assert outcome.reading.circuit_established is True
    assert outcome.reading.socks_listeners == (19250,)


async def test_a_second_newnym_within_10_s_is_refused_without_sending(
    tmp_path: Path,
) -> None:
    fake = await _fake(tmp_path)
    clock = Clock()
    guard = NewnymGuard(clock=clock)
    try:
        first = await send_newnym(fake.port, guard=guard)
        connections = fake.connections
        clock.now += 3.2
        second = await send_newnym(fake.port, guard=guard)
        after_second = fake.connections
        clock.now += 6.9
        third = await send_newnym(fake.port, guard=guard)
    finally:
        await fake.stop()

    assert first.accepted
    assert not second.accepted
    assert second.refused_locally
    assert second.wait_seconds == 7
    assert second.sentence == (
        "Tor allows one new identity per 10 seconds: try again in 7 s. "
        "Nothing was sent."
    )
    assert after_second == connections, "a refused press opened a connection"
    assert third.accepted, third.sentence
    assert fake.newnyms == 2


async def test_two_quick_presses_send_one_newnym(tmp_path: Path) -> None:
    fake = await _fake(tmp_path)
    guard = NewnymGuard(clock=Clock())
    try:
        outcomes = await asyncio.gather(
            send_newnym(fake.port, guard=guard), send_newnym(fake.port, guard=guard)
        )
    finally:
        await fake.stop()

    assert sorted(outcome.accepted for outcome in outcomes) == [False, True]
    assert fake.newnyms == 1


async def test_a_failed_login_does_not_use_up_the_10_s(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, methods=("HASHEDPASSWORD",), password=PASSWORD)
    guard = NewnymGuard(clock=Clock())
    try:
        wrong = await send_newnym(
            fake.port, auth="password", password="wrong", guard=guard
        )
        right = await send_newnym(
            fake.port, auth="password", password=PASSWORD, guard=guard
        )
    finally:
        await fake.stop()

    assert not wrong.accepted
    assert not wrong.refused_locally
    assert right.accepted, right.sentence
    assert fake.newnyms == 1


async def test_a_refused_newnym_is_reported(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, newnym_reply="552 Unrecognized signal")
    guard = NewnymGuard(clock=Clock())
    try:
        outcome = await send_newnym(fake.port, guard=guard)
    finally:
        await fake.stop()

    assert not outcome.accepted
    assert outcome.sentence.startswith("Tor refused the new identity (552 ")
    assert guard.wait_seconds(fake.port) == 0


async def test_keys_an_older_tor_does_not_know_are_not_reported(
    tmp_path: Path,
) -> None:
    fake = await _fake(
        tmp_path, unknown_keys=("net/listeners/socks", "status/bootstrap-phase")
    )
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert reading.ok, reading.sentence
    assert reading.socks_listeners is None
    assert reading.bootstrap is None
    assert reading.circuit_established is True


async def test_no_circuit_yet_says_how_far_tor_bootstrapped(tmp_path: Path) -> None:
    fake = await _fake(tmp_path, circuit_established=False, bootstrap=45)
    try:
        reading = await read_tor_status(fake.port)
    finally:
        await fake.stop()

    assert reading.circuit_established is False
    assert "no circuit yet (bootstrapped 45%)" in reading.sentence


async def test_nothing_listening_is_a_sentence_not_an_error() -> None:
    # Windows takes about two seconds to refuse a loopback connect; the
    # default bound leaves room for that.
    port = closed_port()
    reading = await read_tor_status(port)

    assert not reading.ok
    assert reading.sentence.startswith(f"Nothing answers on 127.0.0.1:{port}.")


async def test_something_that_is_not_tor_is_named_as_such(tmp_path: Path) -> None:
    async def http_server(reader, writer):
        await reader.readline()
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(http_server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reading = await read_tor_status(port)
    finally:
        server.close()
        await server.wait_closed()

    assert not reading.ok
    assert "does not answer like a tor control port" in reading.sentence


async def test_the_only_address_ever_dialled_is_loopback(
    tmp_path: Path, monkeypatch
) -> None:
    fake = await _fake(tmp_path)
    dialled: list[tuple[str, int]] = []
    real = asyncio.open_connection

    async def recording(host, port, **kwargs):
        dialled.append((host, port))
        return await real(host, port, **kwargs)

    monkeypatch.setattr(tor_control.asyncio, "open_connection", recording)
    try:
        await read_tor_status(fake.port)
        await send_newnym(fake.port, guard=NewnymGuard(clock=Clock()))
    finally:
        await fake.stop()

    assert dialled == [("127.0.0.1", fake.port)] * 2


async def test_protocolinfo_decodes_a_windows_cookie_path() -> None:
    reply = Reply(
        status=250,
        lines=(
            "PROTOCOLINFO 1",
            "AUTH METHODS=COOKIE,SAFECOOKIE,HASHEDPASSWORD "
            'COOKIEFILE="C:\\\\Users\\\\me\\\\AppData\\\\Roaming\\\\tor\\\\'
            'control_auth_cookie"',
            'VERSION Tor="0.4.8.13"',
            "OK",
        ),
    )

    info = parse_protocol_info(reply)

    assert info.methods == {"COOKIE", "SAFECOOKIE", "HASHEDPASSWORD"}
    assert (
        info.cookie_file == "C:\\Users\\me\\AppData\\Roaming\\tor\\control_auth_cookie"
    )
    assert info.version == "0.4.8.13"


async def test_a_reply_with_a_data_block_and_an_event_is_read_whole() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"650 STATUS_CLIENT NOTICE CIRCUIT_ESTABLISHED\r\n"
        b"250+net/listeners/socks=\r\n"
        b'"127.0.0.1:19250" "127.0.0.1:19251"\r\n'
        b"..dotted line\r\n"
        b".\r\n"
        b"250 OK\r\n"
    )
    reader.feed_eof()
    control = TorControl(reader, _writer(), port=19260, timeout=2.0)

    reply = await control.read_reply()

    assert reply.status == 250
    assert reply.lines == (
        'net/listeners/socks=\n"127.0.0.1:19250" "127.0.0.1:19251"\n.dotted line',
        "OK",
    )


async def test_a_reply_that_never_ends_is_cut_off() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"250-x\r\n" * 600)
    reader.feed_eof()
    control = TorControl(reader, _writer(), port=19260, timeout=2.0)

    with pytest.raises(TorControlError, match="too long"):
        await control.read_reply()


class _NullWriter:
    """Nothing is written by a test that only reads a reply."""

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def _writer() -> asyncio.StreamWriter:
    return cast(asyncio.StreamWriter, _NullWriter())
