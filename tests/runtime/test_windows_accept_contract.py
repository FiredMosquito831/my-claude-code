"""What Linux CI can prove about the Windows keep-accepting fix (7.69.2).

The reproduction itself (``test_windows_keep_accepting.py``) needs a real
``IocpProactor`` and skips everywhere but Windows -- a skip there is not a pass.
Everything here runs on every platform:

* the detection: this interpreter's own stdlib source still has the accept
  loop the fix was written against (read with ``ast``, never imported, so it
  works where ``asyncio.windows_events`` cannot be imported);
* the re-arm logic itself, driven against a fake proactor;
* the burst report;
* the wiring: the composition root installs it, and off Windows it is inert.
"""

import ast
import asyncio
import errno
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.runtime import bootstrap, windows_accept

_ASYNCIO_DIR = Path(asyncio.__file__).parent


def _stdlib_method_fingerprint(module_file: str, class_name: str, method: str) -> str:
    tree = ast.parse((_ASYNCIO_DIR / module_file).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method:
                    return windows_accept.fingerprint(ast.unparse(item))
    raise AssertionError(f"{class_name}.{method} is gone from asyncio/{module_file}")


class _WinError(OSError):
    """An ``OSError`` carrying a Windows error code on any platform."""

    winerror: int

    def __init__(self, code: int) -> None:
        super().__init__(errno.EINVAL, f"WinError {code}")
        self.winerror = code


# ----------------------------------------------------------------- detection


def test_this_interpreters_accept_loop_is_the_one_the_fix_was_written_for() -> None:
    """The pin that says when CPython changes under the fix.

    If this fails, the interpreter ``.python-version`` names has a different
    ``IocpProactor.accept`` or ``_start_serving``. ``install_keep_accepting``
    will already refuse to install on it (it logs a WARNING and changes
    nothing); read the new code, decide whether #93821 is fixed, and either add
    the new pair to ``KNOWN_AFFECTED`` or retire the module.
    """

    pair = (
        _stdlib_method_fingerprint("windows_events.py", "IocpProactor", "accept"),
        _stdlib_method_fingerprint(
            "proactor_events.py", "BaseProactorEventLoop", "_start_serving"
        ),
    )

    assert pair in windows_accept.KNOWN_AFFECTED, pair


def test_the_fingerprint_ignores_formatting_and_sees_every_statement() -> None:
    plain = "def f(x):\n    return x + 1\n"
    commented = "    def f(x):\n\n        # a comment\n        return (x + 1)\n"
    changed = "def f(x):\n    return x + 2\n"

    assert windows_accept.fingerprint(plain) == windows_accept.fingerprint(commented)
    assert windows_accept.fingerprint(plain) != windows_accept.fingerprint(changed)


def test_the_upstream_fix_shape_would_not_be_recognised() -> None:
    """PR #124032's ``_start_serving`` gains a ``ConnectionResetError`` clause."""

    source = (_ASYNCIO_DIR / "proactor_events.py").read_text(encoding="utf-8")
    patched = source.replace(
        "            except OSError as exc:\n                if sock.fileno() != -1:",
        "            except ConnectionResetError:\n                self.call_soon(loop)\n"
        "            except OSError as exc:\n                if sock.fileno() != -1:",
        1,
    )
    assert patched != source
    tree = ast.parse(patched)
    fixed = next(
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BaseProactorEventLoop"
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "_start_serving"
    )
    accept = _stdlib_method_fingerprint("windows_events.py", "IocpProactor", "accept")

    assert (
        accept,
        windows_accept.fingerprint(ast.unparse(fixed)),
    ) not in windows_accept.KNOWN_AFFECTED


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows path")
@pytest.mark.local_serial
def test_off_windows_the_module_is_inert() -> None:
    assert windows_accept.install_keep_accepting().state == "not-windows"
    probe = (
        "import sys, my_claude_code.runtime.windows_accept as m; "
        "print(m.install_keep_accepting().state, "
        "'asyncio.windows_events' in sys.modules, '_overlapped' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["not-windows", "False", "False"]


def test_the_composition_root_installs_it() -> None:
    with (
        patch.object(bootstrap, "configure_logging"),
        patch.object(bootstrap, "install_keep_accepting") as install,
    ):
        bootstrap.build_asgi_app(Settings().model_copy())

    install.assert_called_once_with()


def test_only_per_connection_errors_are_re_armed() -> None:
    classify = windows_accept._dropped_connection_error

    assert classify(_WinError(64)) == 64
    assert classify(_WinError(1236)) == 1236
    assert classify(_WinError(10054)) == 10054
    # A deliberate close of the listener, and anything else, still surfaces.
    assert classify(_WinError(995)) is None
    assert classify(OSError(errno.EMFILE, "Too many open files")) is None
    assert classify(ValueError("not an OSError")) is None


# --------------------------------------------------------- the re-arm logic


class _FakeAccepts:
    """Stands in for ``_post_accept``: one future per posted accept."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.posted: list[asyncio.Future[Any]] = []
        self.raise_on_post: BaseException | None = None

    def __call__(self, proactor: Any, listener: socket.socket) -> asyncio.Future[Any]:
        if self.raise_on_post is not None:
            raise self.raise_on_post
        future: asyncio.Future[Any] = self.loop.create_future()
        self.posted.append(future)
        return future


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_dropped_connection_is_re_armed_and_the_next_one_is_delivered() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    listener = socket.socket()
    conn = socket.socket()
    try:
        with patch.object(windows_accept, "_post_accept", accepts):
            outer = windows_accept._keep_accepting_accept(
                SimpleNamespace(_loop=loop), listener
            )
            accepts.posted[0].set_exception(_WinError(64))
            await _settle()
            assert len(accepts.posted) == 2
            assert not outer.done()
            accepts.posted[1].set_result((conn, ("127.0.0.1", 1)))
            await _settle()

        assert outer.result() == (conn, ("127.0.0.1", 1))
        assert windows_accept.accept_resets_survived() == 1
    finally:
        listener.close()
        conn.close()


@pytest.mark.asyncio
async def test_any_other_accept_error_reaches_the_caller_unchanged() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    listener = socket.socket()
    error = OSError(errno.EMFILE, "Too many open files")
    try:
        with patch.object(windows_accept, "_post_accept", accepts):
            outer = windows_accept._keep_accepting_accept(
                SimpleNamespace(_loop=loop), listener
            )
            accepts.posted[0].set_exception(error)
            await _settle()

        assert outer.exception() is error
        assert len(accepts.posted) == 1
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_a_closed_listener_is_never_re_armed() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    listener = socket.socket()
    error = _WinError(64)
    with patch.object(windows_accept, "_post_accept", accepts):
        outer = windows_accept._keep_accepting_accept(
            SimpleNamespace(_loop=loop), listener
        )
        listener.close()
        accepts.posted[0].set_exception(error)
        await _settle()

    assert outer.exception() is error
    assert len(accepts.posted) == 1


@pytest.mark.asyncio
async def test_cancelling_the_accept_cancels_the_posted_one() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    listener = socket.socket()
    try:
        with patch.object(windows_accept, "_post_accept", accepts):
            outer = windows_accept._keep_accepting_accept(
                SimpleNamespace(_loop=loop), listener
            )
            outer.cancel()
            await _settle()

        assert accepts.posted[0].cancelled()
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_a_connection_that_lands_after_a_cancel_is_closed() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    listener = socket.socket()
    conn = socket.socket()
    try:
        with patch.object(windows_accept, "_post_accept", accepts):
            outer = windows_accept._keep_accepting_accept(
                SimpleNamespace(_loop=loop), listener
            )
            # The accept completed in the same tick the server was closed.
            accepts.posted[0].set_result((conn, ("127.0.0.1", 1)))
            outer.cancel()
            await _settle()

        assert conn.fileno() == -1
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_a_failure_to_post_the_first_accept_raises_like_cpython() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    accepts.raise_on_post = OSError(errno.ENOTSOCK, "not a socket")
    listener = socket.socket()
    try:
        with (
            patch.object(windows_accept, "_post_accept", accepts),
            pytest.raises(OSError, match="not a socket"),
        ):
            windows_accept._keep_accepting_accept(SimpleNamespace(_loop=loop), listener)
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_a_failure_to_re_arm_settles_the_accept_with_that_error() -> None:
    loop = asyncio.get_running_loop()
    accepts = _FakeAccepts(loop)
    listener = socket.socket()
    error = OSError(errno.ENOBUFS, "no buffer space")
    try:
        with patch.object(windows_accept, "_post_accept", accepts):
            outer = windows_accept._keep_accepting_accept(
                SimpleNamespace(_loop=loop), listener
            )
            accepts.raise_on_post = error
            accepts.posted[0].set_exception(_WinError(64))
            await _settle()

        assert outer.exception() is error
    finally:
        listener.close()


# --------------------------------------------------------------- the report


@pytest.mark.asyncio
@pytest.mark.local_serial
async def test_a_burst_of_resets_is_one_warning_and_one_count() -> None:
    loop = asyncio.get_running_loop()
    report = windows_accept._ResetReport()
    # The module's logger, stubbed: a loguru sink here sees each record twice
    # whenever an earlier test in the worker left stdlib propagation in place.
    try:
        with (
            patch.object(windows_accept, "logger") as stub,
            patch.object(windows_accept, "RESET_REPORT_WINDOW_SECONDS", 0.05),
        ):
            for _ in range(4):
                report.record(loop, 64)
            assert stub.warning.call_count == 1
            await asyncio.sleep(0.2)
        total = report.total
    finally:
        report.reset()

    assert total == 4
    first, summary = stub.warning.call_args_list
    assert first.args[0].startswith("A client dropped its connection")
    assert first.kwargs["code"] == 64
    assert first.kwargs["name"] == "ERROR_NETNAME_DELETED"
    assert summary.args[0].startswith("{count} more client connection(s)")
    assert summary.kwargs["count"] == 3
    stub.error.assert_not_called()
