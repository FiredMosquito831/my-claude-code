"""Windows, end to end: a lost listener drains the stream in flight and exits 75.

7.69.2. The real supervisor (``cli/commands._run_supervised_server``), real
uvicorn, the socket bound the way ``mcc-server`` binds it, the real ASGI gate
and listener guard, on a real proactor loop. A stream is in flight when the
accept loop hits an error the keep-accepting fix does not cover (``EMFILE``),
so CPython does what it did on 2026-10-01: "Accept failed on a socket" and the
listening socket is closed. What must follow:

* one console line, one CRITICAL, ``listening = 0`` published;
* the stream already in flight is finished, not cut;
* new connections are refused (nothing is listening any more);
* the process's supervisor raises ``SystemExit(75)`` within the stop bound.

Windows only, because that is where CPython closes a listener on an accept
error. The supervisor half runs everywhere in
``tests/cli/test_listener_lost_supervisor.py``; the guard in
``test_listener_guard.py``.
"""

import asyncio
import errno
import socket
import sys
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from starlette.types import Receive, Scope, Send

from my_claude_code.cli import commands
from my_claude_code.config.constants import LISTENER_LOST_EXIT_CODE
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import server_listening
from my_claude_code.core.startup_state import startup_state
from my_claude_code.core.stop_deadline import stop_deadline
from my_claude_code.runtime import listener_guard
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.asgi import RuntimeASGIApp

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="CPython closes a listener on accept errors only on Windows",
)

BUDGET_SECONDS = 5.0
CHUNKS = 8
CHUNK_INTERVAL = 0.25
# The real bind, held before the supervisor's own name for it is patched.
_REAL_BIND = commands._bind_listening_socket


class _StreamApp:
    """``/stream`` sends ``CHUNKS`` chunks a quarter-second apart."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        for index in range(CHUNKS):
            await send(
                {
                    "type": "http.response.body",
                    "body": f"chunk-{index}\n".encode(),
                    "more_body": True,
                }
            )
            await asyncio.sleep(CHUNK_INTERVAL)
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class _ReadyAtOnce(RuntimeASGIApp):
    """The shipped gate and lifespan, with the application's startup elided."""

    async def _run_startup(self) -> None:
        startup_state().mark_ready()


@pytest.fixture
def startup() -> Iterator[None]:
    startup_state().begin()
    yield
    startup_state().begin()


def _stream(port: int, first_chunk: threading.Event, out: dict[str, Any]) -> None:
    received = b""
    with socket.create_connection(("127.0.0.1", port), timeout=20) as client:
        client.sendall(b"GET /stream HTTP/1.1\r\nhost: x\r\n\r\n")
        while True:
            data = client.recv(4096)
            if not data:
                break
            received += data
            if b"chunk-0" in received:
                first_chunk.set()
            if received.endswith(b"0\r\n\r\n"):
                break
    out["chunks"] = received.count(b"chunk-")
    out["finished_at"] = time.monotonic()


def _new_connection(port: int) -> str:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(b"GET /health HTTP/1.1\r\nhost: x\r\n\r\n")
            return client.recv(32).decode("ascii", "replace")
    except OSError as exc:
        return type(exc).__name__


def test_a_lost_listener_finishes_the_stream_in_flight_then_exits_75(
    startup: None, capsys: pytest.CaptureFixture[str]
) -> None:
    proactor_class: Any = asyncio.windows_events.IocpProactor
    original_accept = proactor_class.accept
    fail_next_accept = threading.Event()

    def accept(self: Any, listener: socket.socket) -> Any:
        if fail_next_accept.is_set():
            raise OSError(errno.EMFILE, "Too many open files")
        return original_accept(self, listener)

    settings = Settings.model_construct(
        host="127.0.0.1",
        port=18549,
        anthropic_auth_token="freecc",
        model="nvidia_nim/test-model",
        open_admin_browser=False,
        server_graceful_shutdown_seconds=BUDGET_SECONDS,
        server_port_takeover="never",
    )
    bound: dict[str, socket.socket] = {}

    def bind(_settings: Settings) -> socket.socket:
        sock = _REAL_BIND(cast(Settings, SimpleNamespace(host="127.0.0.1", port=0)))
        bound["socket"] = sock
        return sock

    stop: dict[str, Any] = {}

    def build(_settings: Settings, **kwargs: Any) -> RuntimeASGIApp:
        stop["supervisor"] = kwargs["listener_lost_callback"]
        return _ReadyAtOnce(
            _StreamApp(),
            cast(ApplicationRuntime, MagicMock(spec=ApplicationRuntime)),
            listener_lost_callback=kwargs["listener_lost_callback"],
            listening_socket=kwargs["listening_socket"],
        )

    outcome: dict[str, Any] = {}

    def supervise() -> None:
        try:
            outcome["result"] = commands._run_supervised_server(
                settings, open_admin_browser=False
            )
        except SystemExit as exc:
            outcome["exit"] = exc.code
        outcome["exited_at"] = time.monotonic()

    # A daemon, and stopped through the supervisor's own callback if the test
    # fails first: a server thread that outlives a failed assertion would keep
    # the pytest process from ever exiting.
    supervisor = threading.Thread(target=supervise, name="mcc-drain-test", daemon=True)
    with (
        patch.object(proactor_class, "accept", accept),
        # The pytest process must never be hard-exited by a test; the bound is
        # asserted from the timings instead.
        patch.object(type(stop_deadline()), "arm_hard_exit", lambda *_a, **_k: None),
        patch.object(commands, "_bind_listening_socket", side_effect=bind),
        patch.object(commands, "build_asgi_app", side_effect=build),
        patch.object(commands, "_schedule_open_admin_browser"),
        patch.object(commands, "_survey_other_servers"),
        patch.object(commands, "kill_all_best_effort"),
        patch.object(commands, "probe_port_available", return_value=True),
        # The two loggers whose lines are asserted, stubbed: a loguru sink sees
        # each record twice whenever an earlier test left stdlib propagation on.
        patch.object(listener_guard, "logger") as guard_log,
        patch.object(commands, "logger") as supervisor_log,
    ):
        supervisor.start()
        try:
            deadline = time.monotonic() + 30
            while "socket" not in bound and time.monotonic() < deadline:
                time.sleep(0.01)
            port = bound["socket"].getsockname()[1]
            while time.monotonic() < deadline:
                if _new_connection(port).startswith("HTTP/1.1 200"):
                    break
                time.sleep(0.05)
            assert server_listening() is True

            first_chunk = threading.Event()
            stream: dict[str, Any] = {}
            client = threading.Thread(
                target=_stream, args=(port, first_chunk, stream), daemon=True
            )
            client.start()
            assert first_chunk.wait(10)

            # The accept for this connection completes normally; the one
            # CPython posts after it fails, and CPython closes the listener.
            fail_next_accept.set()
            lost_at = time.monotonic()
            _new_connection(port)
            client.join(20)
            supervisor.join(30)
            refused_after = _new_connection(port)
        finally:
            # Still inside the patches, so the hard exit stays disarmed.
            if supervisor.is_alive() and "supervisor" in stop:
                stop["supervisor"]()
                supervisor.join(15)

    assert not supervisor.is_alive()
    assert outcome.get("exit") == LISTENER_LOST_EXIT_CODE, outcome
    assert stream["chunks"] == CHUNKS, stream
    assert stream["finished_at"] <= outcome["exited_at"]
    assert outcome["exited_at"] - lost_at < BUDGET_SECONDS + 3.0
    assert refused_after == "ConnectionRefusedError"
    assert server_listening() is False
    console = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "lost its listening socket" in line
    ]
    assert len(console) == 1, console
    guard_log.critical.assert_called_once()
    assert guard_log.critical.call_args.args[0].startswith("Listener lost:")
    assert any(
        call.args[0].startswith("Stop requested (action={action}")
        and call.kwargs["action"] == "stop"
        for call in supervisor_log.info.call_args_list
    ), supervisor_log.info.call_args_list
