"""Notice that this server's listening socket is gone, say so, and stop.

A server whose listening socket has been closed under it is a zombie: its
process, event loop, timers and request-log heartbeat all carry on, a client
that still holds a keep-alive connection keeps getting answers, and no new
connection can reach it. On 2026-10-01 one sat like that for 7 h 12 min, and
nothing a user looks at said so -- the only record was an ERROR in
``server.log``, and every reader of the session row called it live.

``runtime/windows_accept.py`` removes the known cause on Windows. This module
is the net under everything that fix does not cover -- any other accept error
CPython treats as fatal (``EMFILE`` and friends), a Python the fix does not
recognise, a cause nobody has met yet -- and it does not depend on that module
at all: once a second it asks the one question that matters, "is the socket
the supervisor bound still open?", which costs one attribute read. When the
answer turns to no and nobody asked the server to stop, it

1. prints one line, with the time, to the console (stderr), because that is
   where the user is looking and ``server.log`` is not;
2. writes the same fact at CRITICAL to ``server.log``;
3. marks this process's ``server_sessions`` row ``listening = 0`` at once,
   rather than at the next 30 s heartbeat;
4. hands over to the supervisor's stop, which refuses new requests, lets the
   ones in flight finish inside ``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` (the same
   bound every stop has), stops the timers through the ordinary shutdown and
   exits with :data:`~my_claude_code.config.constants.LISTENER_LOST_EXIT_CODE`.

It never re-binds the port: a fresh process is the honest recovery, and the
desktop app (or whoever started this one) starts it.
"""

import asyncio
import socket
import sys
import time
from collections.abc import Callable
from contextlib import suppress

from loguru import logger

from my_claude_code.config.constants import LISTENER_LOST_EXIT_CODE
from my_claude_code.core.request_log import (
    server_bind_address,
    set_server_listening,
    touch_server_sessions,
)
from my_claude_code.core.stop_deadline import stop_deadline

#: How often the socket is looked at. One ``fileno()`` read per look.
LISTENER_CHECK_SECONDS = 1.0

#: Hands the guard the socket the supervisor bound, or ``None`` before it has.
ListeningSocket = Callable[[], socket.socket | None]


def _address_of(sock: socket.socket) -> tuple[str, int] | None:
    try:
        host, port = sock.getsockname()[:2]
    except OSError, ValueError:
        return None
    return str(host), int(port)


def _print_to_console(line: str) -> None:
    stream = sys.stderr
    if stream is None:
        # A windowless interpreter has no console to print to.
        return
    with suppress(OSError, ValueError):
        stream.write(line + "\n")
        stream.flush()


def listener_lost_console_line(where: str, *, now: float | None = None) -> str:
    """The one line a user sees in the server's console."""

    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    return (
        f"[{stamp}] My Claude Code lost its listening socket on {where} and can "
        "no longer accept new connections. It is finishing the requests already "
        f"in progress, then it exits (exit code {LISTENER_LOST_EXIT_CODE}) so it "
        "can be started again."
    )


class ListenerGuard:
    """Watch one listening socket from the server's own event loop."""

    def __init__(
        self,
        listening_socket: ListeningSocket,
        on_lost: Callable[[], None] | None,
        *,
        interval_seconds: float = LISTENER_CHECK_SECONDS,
    ) -> None:
        self._listening_socket = listening_socket
        self._on_lost = on_lost
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._address: tuple[str, int] | None = None
        self._watching = False
        self._lost = False

    @property
    def lost(self) -> bool:
        """Whether this guard has reported a lost listener."""

        return self._lost

    def start(self) -> None:
        """Begin watching. Call on the server's event loop; idempotent."""

        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="mcc-listener-guard"
            )

    async def close(self) -> None:
        """Stop watching (part of every shutdown, so it is quick and total)."""

        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while not self.check():
            await asyncio.sleep(self._interval)

    def check(self) -> bool:
        """Look once. ``True`` once there is nothing left to watch."""

        if self._lost:
            return True
        sock = self._listening_socket()
        if sock is None:
            # The supervisor has not bound yet.
            return False
        if sock.fileno() != -1:
            if not self._watching:
                self._watching = True
                self._address = _address_of(sock)
                set_server_listening(True)
            return False
        if stop_deadline().requested:
            # A stop that was asked for: uvicorn closes the listener first thing.
            return True
        self._lost = True
        self._report()
        return True

    def _report(self) -> None:
        address = self._address or server_bind_address()
        where = f"{address[0]}:{address[1]}" if address else "its port"
        # Each step on its own, so a failure in one never costs the others --
        # least of all the stop at the end, which is the point of all of it.
        with suppress(Exception):
            _print_to_console(listener_lost_console_line(where))
        with suppress(Exception):
            logger.critical(
                "Listener lost: the listening socket on {where} was closed while "
                "the server was running (nobody asked it to stop), so no new "
                "connection can reach this process. Finishing the requests in "
                "progress within SERVER_GRACEFUL_SHUTDOWN_SECONDS, then exiting "
                "with code {code} so it can be started again. On Windows the "
                "usual cause is python/cpython#93821; look for 'Accept failed on "
                "a socket' just above.",
                where=where,
                code=LISTENER_LOST_EXIT_CODE,
            )
        with suppress(Exception):
            set_server_listening(False)
            touch_server_sessions()
        if self._on_lost is not None:
            self._on_lost()
