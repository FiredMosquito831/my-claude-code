"""Windows: a client reset during a loop hold must not close the listener.

The 2026-10-01 outage, reproduced with real objects: a real proactor event
loop, the listening socket exactly as the supervisor binds it
(``SO_EXCLUSIVEADDRUSE``, backlog 2048), a deliberate hold of the loop thread,
and clients that connect during the hold and reset (``SO_LINGER`` 0) before the
loop gets back to accept them. On CPython 3.13-3.14 that closes the listening
socket for good (python/cpython#93821); with ``runtime/windows_accept.py``
installed by the composition root the listener survives and the next new
connection is answered.

Windows only: the bug lives in ``IocpProactor``. Linux CI skips this file, and
a skip proves nothing -- ``test_windows_accept_contract.py`` is what Linux runs
of this fix (the fingerprints, the re-arm logic against a fake proactor, the
wiring), and this file is run on Windows before every release of it.
"""

import asyncio
import errno
import socket
import struct
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from my_claude_code.cli.commands import _bind_listening_socket
from my_claude_code.config.settings import Settings
from my_claude_code.runtime import windows_accept
from my_claude_code.runtime.bootstrap import build_asgi_app

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="IocpProactor exists only on Windows"
)

HOLD_SECONDS = 1.5
RESETTERS = 8
_RESPONSE = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok"


def _mcc_socket() -> socket.socket:
    """The listening socket the way ``mcc-server`` binds it, on a free port."""

    return _bind_listening_socket(
        cast(Settings, SimpleNamespace(host="127.0.0.1", port=0))
    )


def _connect_then_reset(port: int) -> None:
    """Connect, give the loop no chance to accept, then reset (an RST)."""

    client = socket.socket()
    try:
        client.settimeout(5)
        client.connect(("127.0.0.1", port))
        time.sleep(0.2)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    except OSError:
        pass
    finally:
        client.close()


def _connect_and_close(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass


def _fresh_request(port: int, path: str = "/") -> bytes | str:
    """One request on a NEW connection -- what a client arriving later sees."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(f"GET {path} HTTP/1.1\r\nhost: x\r\n\r\n".encode())
            return client.recv(64)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def _storm_during_hold(
    hold: float = HOLD_SECONDS, resetters: int = RESETTERS
) -> dict[str, Any]:
    """Serve on MCC's socket, hold the loop, reset queued connections, look."""

    sock = _mcc_socket()
    port = sock.getsockname()[1]
    messages: list[str] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(_RESPONSE)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def main() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: messages.append(str(context.get("message")))
        )
        server = await asyncio.start_server(handle, sock=sock)
        clients = [
            threading.Thread(target=_connect_then_reset, args=(port,))
            for _ in range(resetters)
        ]

        def hold_the_loop() -> None:
            for client in clients:
                client.start()
            time.sleep(hold)

        loop.call_soon(hold_the_loop)
        await asyncio.sleep(0.05)
        for client in clients:
            await asyncio.to_thread(client.join)
        await asyncio.sleep(0.5)
        listening = sock.fileno() != -1
        fresh = await asyncio.to_thread(_fresh_request, port)
        server.close()
        await server.wait_closed()
        return {"listening": listening, "fresh": fresh, "messages": messages}

    return asyncio.run(main(), loop_factory=asyncio.ProactorEventLoop)


def _answered(result: dict[str, Any]) -> bool:
    fresh = result["fresh"]
    return isinstance(fresh, bytes) and fresh.startswith(b"HTTP/1.1 200")


@pytest.mark.local_serial
def test_the_composition_root_keeps_the_listener_through_resets_in_a_hold() -> None:
    """The fix as shipped: building the app is what installs it.

    This is the test that fails on 7.69.1, where ``build_asgi_app`` installs
    nothing: there the listener is closed ("Accept failed on a socket") and the
    new connection is refused with WinError 10061.
    """

    with patch("my_claude_code.runtime.bootstrap.configure_logging"):
        build_asgi_app(Settings().model_copy())

    result = _storm_during_hold()

    assert result["listening"], result
    assert _answered(result), result
    assert "Accept failed on a socket" not in result["messages"], result
    assert windows_accept.accept_resets_survived() >= 1, result


@pytest.mark.local_serial
def test_cpython_still_closes_the_listener_without_keep_accepting() -> None:
    """Pins the upstream bug this fix exists for.

    The day this fails, the interpreter in ``.python-version`` no longer closes
    its listener on a reset -- and its fingerprint will have moved too, so
    ``install_keep_accepting`` will already have stopped installing itself.
    """

    windows_accept.uninstall_keep_accepting()

    result = _storm_during_hold()

    assert not result["listening"], result
    assert "Accept failed on a socket" in result["messages"], result
    assert not _answered(result), result


@pytest.mark.local_serial
def test_resets_are_reported_once_per_burst_never_as_errors() -> None:
    windows_accept.install_keep_accepting()
    # The module's logger, stubbed: a loguru sink here sees each record twice
    # whenever an earlier test in the worker left stdlib propagation in place.
    with patch.object(windows_accept, "logger") as stub:
        result = _storm_during_hold(resetters=12)

    assert result["listening"], result
    stub.warning.assert_called_once()
    assert stub.warning.call_args.args[0].startswith(
        "A client dropped its connection before the server accepted it"
    )
    assert stub.warning.call_args.kwargs["code"] == 64
    assert stub.warning.call_args.kwargs["name"] == "ERROR_NETNAME_DELETED"
    stub.error.assert_not_called()
    stub.critical.assert_not_called()
    assert "Task exception was never retrieved" not in result["messages"], result


@pytest.mark.local_serial
def test_a_non_per_connection_accept_error_still_reaches_start_serving() -> None:
    """Only a dropped *connection* is re-armed; anything else surfaces as before."""

    windows_accept.install_keep_accepting()
    original = windows_accept._post_accept
    fail = {"next": False}

    def post_accept(proactor: Any, listener: socket.socket) -> asyncio.Future[Any]:
        if fail["next"]:
            future: asyncio.Future[Any] = proactor._loop.create_future()
            future.set_exception(OSError(errno.EMFILE, "Too many open files"))
            return future
        return original(proactor, listener)

    sock = _mcc_socket()
    port = sock.getsockname()[1]
    messages: list[str] = []

    async def main() -> bool:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: messages.append(str(context.get("message")))
        )
        server = await loop.create_server(asyncio.Protocol, sock=sock)
        fail["next"] = True
        # Its accept completes normally; the NEXT accept is the one that fails.
        await asyncio.to_thread(_connect_and_close, port)
        await asyncio.sleep(0.3)
        listening = sock.fileno() != -1
        server.close()
        return listening

    with patch.object(windows_accept, "_post_accept", post_accept):
        listening = asyncio.run(main(), loop_factory=asyncio.ProactorEventLoop)

    assert not listening
    assert "Accept failed on a socket" in messages


@pytest.mark.local_serial
def test_closing_the_server_cancels_the_rearmed_accept_cleanly() -> None:
    windows_accept.install_keep_accepting()
    sock = _mcc_socket()
    port = sock.getsockname()[1]
    messages: list[str] = []

    async def main() -> float:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: messages.append(str(context.get("message")))
        )
        server = await loop.create_server(asyncio.Protocol, sock=sock)
        clients = [
            threading.Thread(target=_connect_then_reset, args=(port,)) for _ in range(4)
        ]

        def hold_the_loop() -> None:
            for client in clients:
                client.start()
            time.sleep(1.0)

        loop.call_soon(hold_the_loop)
        await asyncio.sleep(0.05)
        for client in clients:
            await asyncio.to_thread(client.join)
        await asyncio.sleep(0.3)
        started = time.monotonic()
        server.close()
        await server.wait_closed()
        return time.monotonic() - started

    elapsed = asyncio.run(main(), loop_factory=asyncio.ProactorEventLoop)

    assert elapsed < 2.0
    assert sock.fileno() == -1
    assert messages == []


def test_install_is_idempotent() -> None:
    first = windows_accept.install_keep_accepting()
    second = windows_accept.install_keep_accepting()

    assert first.state == "installed"
    assert second.state == "already-installed"


@pytest.mark.local_serial
def test_an_unrecognised_accept_loop_is_left_alone() -> None:
    with patch.object(windows_accept, "KNOWN_AFFECTED", frozenset()):
        result = windows_accept.install_keep_accepting()

    assert result.state == "unrecognised"
    assert result.accept_fingerprint is not None
    # Nothing was replaced, so there is nothing to put back.
    assert windows_accept._ORIGINAL_ACCEPT is None
    assert _storm_during_hold()["listening"] is False


class _HoldApp:
    """A two-route ASGI app: ``/hold`` blocks the loop, anything else is 200."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        if scope["path"] == "/hold":
            time.sleep(HOLD_SECONDS)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"2")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.local_serial
def test_uvicorn_as_mcc_drives_it_survives_a_reset_storm() -> None:
    """``uvicorn.Server.run(sockets=[sock])`` on MCC's socket, as cli/commands does."""

    windows_accept.install_keep_accepting()
    sock = _mcc_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(_HoldApp(), log_level="warning", lifespan="off")
    )
    thread = threading.Thread(
        target=lambda: server.run(sockets=[sock]), name="mcc-accept-test", daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        for _round in range(3):
            holder = threading.Thread(target=_fresh_request, args=(port, "/hold"))
            holder.start()
            time.sleep(0.2)
            resetters = [
                threading.Thread(target=_connect_then_reset, args=(port,))
                for _ in range(RESETTERS)
            ]
            for resetter in resetters:
                resetter.start()
            for resetter in resetters:
                resetter.join()
            holder.join()
        time.sleep(0.3)
        assert sock.fileno() != -1
        assert _answered({"fresh": _fresh_request(port)})
        assert windows_accept.accept_resets_survived() >= 1
    finally:
        server.should_exit = True
        thread.join(10)
    assert not thread.is_alive()
