"""Every step of a check ends, including the ones that are not the measurement.

This is the regression file for the defect that took a 7.21.0 install's fetch
of 1,592 addresses and left it at 1,591 for ever. The last ``PROXY CHECK`` line
was written, the job stayed ``running`` for seventeen minutes, Stop reported
``stopping: true`` and never settled, and the 355 addresses that had already
passed were never written to the store.

The hang was not in the measurement. ``_tcp_connect`` put the *connect* inside
``asyncio.wait_for`` and then, in its ``finally``, awaited
``StreamWriter.wait_closed()`` with no bound at all. ``wait_closed`` waits on a
future the transport is supposed to complete when the connection is lost; on
the Windows proactor loop ``_ProactorBasePipeTransport._call_connection_lost``
can raise -- the server log carried three such lines around the hang -- and a
transport that raised there never completes that future. One address, one
worker, one job, for ever.

So the shape of every test here is: make the close never finish, and assert
that the check finishes anyway. The socket in each is a real one on loopback,
and the transport failure is simulated by monkeypatching ``wait_closed``
itself -- which is both the most direct statement of the defect and the only
way to reproduce it that does not depend on a particular Windows build
misbehaving on the day the suite runs.
"""

import asyncio
import socket
import threading
import time

import pytest

from my_claude_code.application import proxy_check
from my_claude_code.application.proxy_check import (
    PROXY_CLOSE_TIMEOUT_SECONDS,
    check_budget,
    check_proxy,
)
from my_claude_code.config.proxy_chains import TLS_STRICT

pytestmark = pytest.mark.asyncio


@pytest.fixture
def listening_port():
    """A real loopback listener that accepts and then says nothing at all."""

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    accepted: list[socket.socket] = []

    def accept_forever() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            # Held open deliberately: the peer's FIN is never acknowledged by
            # anything closing this end, which is as close as a test can get to
            # the operating-system half of the original symptom.
            accepted.append(connection)

    thread = threading.Thread(target=accept_forever, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        for connection in accepted:
            connection.close()
        thread.join(timeout=2.0)


def _never_closes(monkeypatch) -> None:
    """Make every stream writer's close wait on something that never happens."""

    async def hang(self) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", hang)


async def test_a_close_that_never_completes_cannot_hold_a_check(
    monkeypatch, listening_port
):
    """The exact defect: the connect succeeded, the close never returned."""

    _never_closes(monkeypatch)

    began = time.monotonic()
    reason = await asyncio.wait_for(
        proxy_check._tcp_connect("127.0.0.1", listening_port, 5.0), 10.0
    )
    elapsed = time.monotonic() - began

    # The address answered -- that part of the check was always right.
    assert reason == ""
    # And the tidying up was given a second, not the rest of the afternoon.
    assert elapsed < PROXY_CLOSE_TIMEOUT_SECONDS + 2.0, f"took {elapsed:.2f}s"


async def test_a_check_returns_a_verdict_even_when_its_close_never_finishes(
    monkeypatch, listening_port
):
    """The whole check, not just its first leg, comes back with an answer."""

    _never_closes(monkeypatch)

    began = time.monotonic()
    record = await asyncio.wait_for(
        check_proxy(
            f"http://127.0.0.1:{listening_port}",
            "https://api.example.invalid/v1",
            timeout=2.0,
            connect_timeout=2.0,
        ),
        20.0,
    )
    elapsed = time.monotonic() - began

    # A proxy that accepts and then says nothing is dead, not a pass. What is
    # under test is that an answer arrives at all.
    assert record.ok is False
    assert record.tls != TLS_STRICT
    assert elapsed < 10.0, f"took {elapsed:.2f}s"


async def test_a_check_stuck_in_a_close_is_cancelled_at_once(
    monkeypatch, listening_port
):
    """Stop cancels, and a cancellation must not be swallowed by a ``finally``.

    A ``finally`` that awaits an unbounded thing during cancellation is how a
    task becomes uncancellable. This is the assertion that the fetch sweep's
    Stop can rely on: cancelling a check in its close returns immediately.
    """

    _never_closes(monkeypatch)

    task = asyncio.create_task(
        proxy_check._tcp_connect("127.0.0.1", listening_port, 5.0)
    )
    # Let it get as far as the close.
    await asyncio.sleep(0.05)
    began = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5.0)
    elapsed = time.monotonic() - began

    assert elapsed < 1.0, f"cancelling took {elapsed:.2f}s"


async def test_a_closed_port_is_still_reported_refused_not_hung(monkeypatch):
    """Nothing about the bound close changes what an ordinary failure says."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    reason = await asyncio.wait_for(
        proxy_check._tcp_connect("127.0.0.1", port, 2.0), 10.0
    )

    assert reason, "a closed port must give a reason"
    assert "127.0.0.1" in reason


async def test_the_budget_covers_every_leg_and_then_some():
    """The outer bound is a backstop, never a second, tighter policy."""

    without_exit_ip = check_budget(connect_timeout=5.0, timeout=10.0)
    with_exit_ip = check_budget(
        connect_timeout=5.0, timeout=10.0, exit_ip_url="https://example.invalid/ip"
    )

    # Strictly more than the legs it is wrapping, in both shapes.
    assert without_exit_ip > 5.0 + 10.0
    assert with_exit_ip > 5.0 + 10.0 + 10.0
    assert with_exit_ip > without_exit_ip
