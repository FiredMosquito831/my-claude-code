"""Keep accepting connections when a client resets one before it is accepted.

The bug, on Windows only: CPython's proactor event loop keeps one overlapped
``AcceptEx`` posted on the listening socket. While the loop is held -- a
synchronous image resize, a long chain build -- new connections wait in the
listen backlog. If a client resets one of them before the loop gets back to it
(a Bun ``fetch`` abort, a killed client process), the pending accept completes
with ``WinError 64`` (``ERROR_NETNAME_DELETED``), and
``BaseProactorEventLoop._start_serving`` treats *any* ``OSError`` from an
accept as fatal to the listener: it logs "Accept failed on a socket" and calls
``sock.close()`` on the **listening** socket. The process, its loop, its timers
and its request-log heartbeat all carry on; no new connection can ever reach it
again. That is python/cpython#93821, open since 2022; neither fix PR (#124032,
#124779) is merged, and ``main`` still has the code. It cost this project a
server that sat dead for 7 h 12 min on 2026-10-01, the fifth time.

The fix: ``IocpProactor.accept`` is replaced by a copy that, when an accept
completes with one of :data:`PER_CONNECTION_ACCEPT_ERRORS`, closes the
half-accepted socket and posts a fresh ``AcceptEx`` instead of handing the
error to ``_start_serving``. The future ``_start_serving`` waits on therefore
completes only with a real connection, a cancellation, or an error that is not
about one connection -- and those still reach ``_start_serving`` exactly as
before, so an error this module does not recognise still closes the listener
(and ``runtime/listener_guard.py`` notices, says so and exits).

Detection, not a blind patch. :func:`install_keep_accepting` replaces the
method only when the running interpreter's ``IocpProactor.accept`` *and*
``BaseProactorEventLoop._start_serving`` are, statement for statement, the
implementations this copy was written against (:data:`KNOWN_AFFECTED`, a
SHA-256 of each function's ``ast.unparse`` form, which ignores comments and
formatting and nothing else). A CPython that fixes the bug changes at least one
of the two -- both fix PRs do -- so its fingerprint is not in the set and the
method is left alone, with one WARNING saying so. The same is true of any other
change to either function, which is the conservative direction: never replace
an implementation this module has not read. ``tests/runtime/
test_windows_accept_contract.py`` pins the fingerprints against the
interpreter's own stdlib source on every platform, so the day ``.python-version``
moves to a Python whose accept loop differs, CI says so.

Everywhere but Windows this module does nothing at all: nothing is imported
from ``asyncio.windows_events`` and nothing is patched.
"""

import ast
import asyncio
import hashlib
import inspect
import socket
import struct
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    import _overlapped
    from asyncio import proactor_events, windows_events

#: Windows error codes an accept completes with when the *connection* waiting
#: in the backlog is gone, not the listener. 64 is the one measured on this
#: machine (every logged incident); 1236 and 10054 are the same event under its
#: other two spellings. ``ERROR_OPERATION_ABORTED`` (995) is deliberately absent:
#: it is also what a deliberate close of the listener produces.
PER_CONNECTION_ACCEPT_ERRORS: dict[int, str] = {
    64: "ERROR_NETNAME_DELETED",
    1236: "ERROR_CONNECTION_ABORTED",
    10054: "WSAECONNRESET",
}

#: ``(IocpProactor.accept, BaseProactorEventLoop._start_serving)`` fingerprints
#: of the CPython releases this copy was written against: 3.13.7, 3.14.0 and
#: 3.14.8 are byte-for-byte the same in both functions (checked 2026-10-01
#: against the tags on python/cpython). See :func:`fingerprint`.
KNOWN_AFFECTED: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "e195a4c41caabcb8",
            "b30e234b98d54786",
        ),
    }
)

#: How long one burst of resets is reported as one line.
RESET_REPORT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class InstallResult:
    """What :func:`install_keep_accepting` found and did."""

    #: ``installed``, ``already-installed``, ``not-windows`` or ``unrecognised``.
    state: str
    accept_fingerprint: str | None = None
    start_serving_fingerprint: str | None = None


def fingerprint(source: str) -> str:
    """A short, formatting-blind SHA-256 of one function's source.

    The source is parsed and re-rendered with :func:`ast.unparse`, so comments,
    blank lines and indentation do not count and every statement does. Sixteen
    hex digits: this is a recognition key, not a security boundary.
    """

    tree = ast.parse(textwrap.dedent(source))
    node = tree.body[0]
    return hashlib.sha256(ast.unparse(node).encode("utf-8")).hexdigest()[:16]


def _function_fingerprint(function: Callable[..., Any]) -> str | None:
    try:
        return fingerprint(inspect.getsource(function))
    except OSError, TypeError, SyntaxError, IndexError:
        # No source on disk (a frozen or stripped interpreter): nothing can be
        # recognised, so nothing is replaced.
        return None


class _ResetReport:
    """One WARNING per burst of resets, then one line with the burst's count.

    Lives on the loop thread: every caller is a future callback.
    """

    def __init__(self) -> None:
        self.total = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._summary: asyncio.TimerHandle | None = None
        self._unreported = 0

    def record(self, loop: asyncio.AbstractEventLoop, winerror: int) -> None:
        self.total += 1
        if self._loop is not loop:
            # A new event loop (an in-process restart): whatever the old loop
            # had scheduled will never run, so start over.
            self._loop = loop
            self._summary = None
            self._unreported = 0
        if self._summary is not None:
            self._unreported += 1
            return
        logger.warning(
            "A client dropped its connection before the server accepted it "
            "(Windows error {code}, {name}). The server kept listening; nothing "
            "needs to be done. This happens when the server was briefly too busy "
            "to accept connections. Further drops in the next {window:.0f}s are "
            "counted in one line.",
            code=winerror,
            name=PER_CONNECTION_ACCEPT_ERRORS.get(winerror, "unknown"),
            window=RESET_REPORT_WINDOW_SECONDS,
        )
        self._summary = loop.call_later(RESET_REPORT_WINDOW_SECONDS, self._summarise)

    def _summarise(self) -> None:
        self._summary = None
        count, self._unreported = self._unreported, 0
        if count:
            logger.warning(
                "{count} more client connection(s) were dropped before the server "
                "accepted them in the last {window:.0f}s; the server kept "
                "listening throughout.",
                count=count,
                window=RESET_REPORT_WINDOW_SECONDS,
            )

    def reset(self) -> None:
        if self._summary is not None:
            self._summary.cancel()
        self.total = 0
        self._loop = None
        self._summary = None
        self._unreported = 0


_RESETS = _ResetReport()
_ORIGINAL_ACCEPT: Callable[..., Any] | None = None


def accept_resets_survived() -> int:
    """How many accepts this process re-armed instead of losing its listener."""

    return _RESETS.total


def _dropped_connection_error(exc: BaseException) -> int | None:
    """The Windows error code if ``exc`` is about one queued connection."""

    if not isinstance(exc, OSError):
        return None
    code = getattr(exc, "winerror", None)
    return code if code in PER_CONNECTION_ACCEPT_ERRORS else None


def _post_accept(proactor: Any, listener: socket.socket) -> asyncio.Future[Any]:
    """CPython 3.14's ``IocpProactor.accept``, with one difference.

    The task that owns the pre-created accept socket closes it on *any* failure
    (CPython closes it only on cancellation and leaves the rest to the garbage
    collector), and does not re-raise: the failure travels to the caller on the
    returned future, so it is never also logged as "Task exception was never
    retrieved".
    """

    proactor._register_with_iocp(listener)
    conn = proactor._get_accept_socket(listener.family)
    ov = _overlapped.Overlapped(_overlapped.NULL)
    try:
        ov.AcceptEx(listener.fileno(), conn.fileno())
    except OSError:
        conn.close()
        raise

    def finish_accept(trans: Any, key: Any, ov: Any) -> tuple[socket.socket, Any]:
        ov.getresult()
        # Use SO_UPDATE_ACCEPT_CONTEXT so getsockname() etc work.
        buf = struct.pack("@P", listener.fileno())
        conn.setsockopt(socket.SOL_SOCKET, _overlapped.SO_UPDATE_ACCEPT_CONTEXT, buf)
        conn.settimeout(listener.gettimeout())
        return conn, conn.getpeername()

    async def accept_coro(future: asyncio.Future[Any], conn: socket.socket) -> None:
        try:
            await future
        except asyncio.CancelledError:
            conn.close()
            raise
        except OSError:
            # Never handed to anybody, so nobody else will close it.
            conn.close()

    future = proactor._register(ov, listener, finish_accept)
    asyncio.ensure_future(accept_coro(future, conn), loop=proactor._loop)
    return future


def _keep_accepting_accept(self: Any, listener: socket.socket) -> asyncio.Future[Any]:
    """``IocpProactor.accept`` that survives a client resetting a queued connection.

    Returns one future that ``_start_serving`` waits on. Behind it, each
    ``AcceptEx`` that fails with a per-connection error is followed by a fresh
    one; everything else -- a connection, a cancellation, any other error --
    settles the returned future exactly as CPython's own would have. A failure
    posting the *first* accept raises synchronously, as CPython's does.
    """

    loop: asyncio.AbstractEventLoop = self._loop
    outer: asyncio.Future[Any] = loop.create_future()
    current: list[asyncio.Future[Any]] = []

    def post() -> None:
        inner = _post_accept(self, listener)
        current[:] = [inner]
        inner.add_done_callback(settle)

    def settle(inner: asyncio.Future[Any]) -> None:
        try:
            if outer.done():
                # Cancelled while this accept was completing: the connection,
                # if there is one, belongs to nobody now.
                if not inner.cancelled() and inner.exception() is None:
                    conn, _addr = inner.result()
                    conn.close()
                return
            if inner.cancelled():
                outer.cancel()
                return
            exc = inner.exception()
            if exc is None:
                outer.set_result(inner.result())
                return
            code = _dropped_connection_error(exc)
            if code is not None and listener.fileno() != -1 and not loop.is_closed():
                _RESETS.record(loop, code)
                post()
                return
            outer.set_exception(exc)
        except BaseException as exc:
            # A failure here must never leave ``_start_serving`` waiting on a
            # future that will not settle: that would be a listener that is
            # open and never accepts, which nothing could detect.
            if not outer.done():
                outer.set_exception(exc)
            if not isinstance(exc, Exception):
                raise

    def forward_cancel(fut: asyncio.Future[Any]) -> None:
        if fut.cancelled() and current and not current[0].done():
            current[0].cancel()

    post()
    outer.add_done_callback(forward_cancel)
    return outer


def install_keep_accepting() -> InstallResult:
    """Make this process's Windows accept loop survive client resets.

    Idempotent, and a no-op everywhere but Windows. Installed from the
    composition root (``runtime/bootstrap.build_asgi_app``), which runs before
    uvicorn creates its event loop; the method is looked up at accept time, so
    the replacement covers the server's own pre-bound socket with no change to
    how it is created.
    """

    global _ORIGINAL_ACCEPT
    if not _WINDOWS:
        return InstallResult("not-windows")
    # Typed as ``Any``: both members read here are private to CPython and are
    # not in the stdlib stubs, which is the whole reason this is fingerprinted.
    proactor_class: Any = windows_events.IocpProactor
    loop_class: Any = proactor_events.BaseProactorEventLoop
    current = proactor_class.accept
    if current is _keep_accepting_accept:
        return InstallResult("already-installed")
    accept_print = _function_fingerprint(current)
    serving_print = _function_fingerprint(loop_class._start_serving)
    if (accept_print, serving_print) not in KNOWN_AFFECTED:
        logger.warning(
            "This Python's Windows accept loop is not the one My Claude Code's "
            "keep-accepting fix was written for (accept {accept}, _start_serving "
            "{serving}), so the fix was not installed. If this Python fixed "
            "python/cpython#93821 nothing is lost; if it did not, a client reset "
            "can still close the listening socket, and the server will notice, "
            "say so and exit so it can be started again.",
            accept=accept_print,
            serving=serving_print,
        )
        return InstallResult("unrecognised", accept_print, serving_print)
    _ORIGINAL_ACCEPT = current
    proactor_class.accept = _keep_accepting_accept
    logger.info(
        "Keep-accepting is on: a client that resets a connection before it is "
        "accepted no longer closes the listening socket (python/cpython#93821)."
    )
    return InstallResult("installed", accept_print, serving_print)


def uninstall_keep_accepting() -> None:
    """Put CPython's own ``accept`` back and forget the reset count. Tests only."""

    global _ORIGINAL_ACCEPT
    _RESETS.reset()
    if not _WINDOWS or _ORIGINAL_ACCEPT is None:
        return
    proactor_class: Any = windows_events.IocpProactor
    proactor_class.accept = _ORIGINAL_ACCEPT
    _ORIGINAL_ACCEPT = None
