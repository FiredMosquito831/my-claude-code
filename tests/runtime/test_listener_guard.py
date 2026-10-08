"""The listener guard: a server that loses its listening socket says so and stops.

7.69.2. On 2026-10-01 a server sat for 7 h 12 min with its listening socket
closed under it -- process, loop, timers and session heartbeat all alive, no
new connection possible, and nothing on the console. These pin what the guard
does instead: one console line with the time, one CRITICAL in ``server.log``,
``listening = 0`` on the session row at once, and the supervisor's stop -- and
that an ordinary stop, which closes the listener too, is none of that.
"""

import asyncio
import re
import socket
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from starlette.types import Message, Scope

from my_claude_code.config.constants import LISTENER_LOST_EXIT_CODE
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import (
    RequestLogStore,
    server_listening,
    set_server_listening,
    touch_server_sessions,
)
from my_claude_code.core.stop_deadline import stop_deadline
from my_claude_code.runtime import bootstrap, listener_guard
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.asgi import RuntimeASGIApp
from my_claude_code.runtime.listener_guard import ListenerGuard


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield sock
    sock.close()


@pytest.fixture
def guard_logger() -> Iterator[MagicMock]:
    """The guard module's logger, stubbed.

    A loguru sink added here would see each record twice whenever an earlier
    test in the same worker left stdlib propagation in place, so the module's
    logger is replaced instead and its calls are the assertion.
    """

    with patch.object(listener_guard, "logger") as stub:
        yield stub


def _lost_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if "lost its listening socket" in line]


@pytest.mark.local_serial
def test_an_open_listener_is_published_as_listening(listener: socket.socket) -> None:
    lost = MagicMock()
    guard = ListenerGuard(lambda: listener, lost)

    assert guard.check() is False
    assert server_listening() is True
    lost.assert_not_called()


def test_nothing_is_watched_before_the_supervisor_binds() -> None:
    guard = ListenerGuard(lambda: None, MagicMock())

    assert guard.check() is False
    assert server_listening() is None


@pytest.mark.local_serial
def test_a_closed_listener_is_reported_once_and_handed_to_the_stop(
    listener: socket.socket,
    capsys: pytest.CaptureFixture[str],
    guard_logger: MagicMock,
) -> None:
    port = listener.getsockname()[1]
    lost = MagicMock()
    guard = ListenerGuard(lambda: listener, lost)
    assert guard.check() is False

    listener.close()
    with patch.object(listener_guard, "touch_server_sessions") as touch:
        assert guard.check() is True
        assert guard.check() is True

    lost.assert_called_once_with()
    touch.assert_called_once_with()
    assert guard.lost
    assert server_listening() is False
    console = _lost_lines(capsys.readouterr().err)
    assert len(console) == 1, console
    assert re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ", console[0])
    assert f"lost its listening socket on 127.0.0.1:{port}" in console[0]
    assert f"exit code {LISTENER_LOST_EXIT_CODE}" in console[0]
    guard_logger.critical.assert_called_once()
    template = guard_logger.critical.call_args.args[0]
    assert template.startswith("Listener lost:")
    assert guard_logger.critical.call_args.kwargs == {
        "where": f"127.0.0.1:{port}",
        "code": LISTENER_LOST_EXIT_CODE,
    }


@pytest.mark.local_serial
def test_a_requested_stop_is_not_a_lost_listener(
    listener: socket.socket,
    capsys: pytest.CaptureFixture[str],
    guard_logger: MagicMock,
) -> None:
    lost = MagicMock()
    guard = ListenerGuard(lambda: listener, lost)
    assert guard.check() is False

    stop_deadline().request(5.0)
    listener.close()

    assert guard.check() is True
    lost.assert_not_called()
    assert not guard.lost
    assert server_listening() is True
    assert _lost_lines(capsys.readouterr().err) == []
    guard_logger.critical.assert_not_called()


@pytest.mark.local_serial
def test_a_failing_report_step_never_costs_the_stop(listener: socket.socket) -> None:
    lost = MagicMock()
    guard = ListenerGuard(lambda: listener, lost)
    guard.check()
    listener.close()

    with (
        patch.object(listener_guard, "_print_to_console", side_effect=OSError),
        patch.object(listener_guard, "touch_server_sessions", side_effect=RuntimeError),
    ):
        assert guard.check() is True

    lost.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_the_guard_notices_from_the_loop_within_one_interval(
    listener: socket.socket,
) -> None:
    noticed = asyncio.Event()
    guard = ListenerGuard(lambda: listener, noticed.set, interval_seconds=0.05)
    guard.start()
    await asyncio.sleep(0.1)
    assert server_listening() is True

    with patch.object(listener_guard, "touch_server_sessions"):
        started = time.monotonic()
        listener.close()
        await asyncio.wait_for(noticed.wait(), 2.0)
    elapsed = time.monotonic() - started
    await guard.close()

    assert elapsed < 1.0


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_closing_the_guard_is_prompt(listener: socket.socket) -> None:
    guard = ListenerGuard(lambda: listener, MagicMock(), interval_seconds=60.0)
    guard.start()
    await asyncio.sleep(0)

    started = time.monotonic()
    await guard.close()

    assert time.monotonic() - started < 0.5


# ------------------------------------------------------------- session row


def _session_rows(db: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(
            "SELECT id, listening FROM server_sessions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _wait_for(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_the_session_row_says_listening_and_then_not_at_once(tmp_path: Path) -> None:
    db = tmp_path / "requests.db"
    set_server_listening(True)
    store = RequestLogStore(db)
    try:
        assert _wait_for(lambda: db.exists() and _session_rows(db) == [(1, 1)])
        set_server_listening(False)
        with patch("my_claude_code.core.request_log._stores", {db: store}):
            touch_server_sessions()
        # Well inside the 30 s heartbeat: the touch is what wrote it.
        assert _wait_for(lambda: _session_rows(db) == [(1, 0)], timeout=5.0)
    finally:
        store.close()


def test_an_older_session_table_gains_the_column_with_old_rows_unmeasured(
    tmp_path: Path,
) -> None:
    db = tmp_path / "requests.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE server_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_at REAL NOT NULL, last_seen_at REAL NOT NULL, pid INTEGER,"
        " host TEXT, port INTEGER);"
        "INSERT INTO server_sessions (started_at, last_seen_at, pid)"
        " VALUES (1.0, 2.0, 42);"
    )
    conn.commit()
    conn.close()

    set_server_listening(True)
    store = RequestLogStore(db)
    try:
        assert _wait_for(lambda: len(_session_rows(db)) == 2)
    finally:
        store.close()

    assert _session_rows(db) == [(1, None), (2, 1)]


# --------------------------------------------------------------- wiring


async def _drive_lifespan(app: RuntimeASGIApp, between: Any) -> list[str]:
    inbox: asyncio.Queue[Message] = asyncio.Queue()
    sent: list[str] = []
    await inbox.put({"type": "lifespan.startup"})

    async def receive() -> Message:
        message = await inbox.get()
        if message["type"] == "lifespan.shutdown":
            await between()
        return message

    async def send(message: Message) -> None:
        sent.append(str(message["type"]))
        if message["type"] == "lifespan.startup.complete":
            await inbox.put({"type": "lifespan.shutdown"})

    await app(cast(Scope, {"type": "lifespan"}), receive, send)
    return sent


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_the_asgi_lifespan_starts_and_stops_the_guard(
    listener: socket.socket, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = RuntimeASGIApp(
        MagicMock(),
        cast(ApplicationRuntime, MagicMock(spec=ApplicationRuntime)),
        listening_socket=lambda: listener,
        listener_lost_callback=MagicMock(),
    )
    monkeypatch.setattr(app, "_run_startup", _no_startup)
    guard = app.listener_guard
    assert guard is not None
    seen: dict[str, bool] = {}

    async def between() -> None:
        await asyncio.sleep(0)
        seen["watching"] = guard._task is not None and not guard._task.done()

    sent = await _drive_lifespan(app, between)

    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert seen == {"watching": True}
    assert guard._task is None


async def _no_startup() -> None:
    return None


def test_without_a_socket_there_is_no_guard() -> None:
    app = RuntimeASGIApp(
        MagicMock(), cast(ApplicationRuntime, MagicMock(spec=ApplicationRuntime))
    )

    assert app.listener_guard is None


@pytest.mark.local_serial
def test_the_composition_root_hands_the_socket_and_the_stop_to_the_guard(
    listener: socket.socket,
) -> None:
    lost = MagicMock()
    with patch.object(bootstrap, "configure_logging"):
        app = bootstrap.build_asgi_app(
            Settings().model_copy(),
            listener_lost_callback=lost,
            listening_socket=lambda: listener,
        )
    guard = app.listener_guard
    assert guard is not None

    guard.check()
    listener.close()
    with patch.object(listener_guard, "touch_server_sessions"):
        guard.check()

    lost.assert_called_once_with()
