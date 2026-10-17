"""An abandoned response cleanup still releases its lease (7.97.0).

``ManagedStreamingResponse._cleanup`` bounds its body close, and
``_wait_for_cleanup`` bounds its wait on that cleanup; both read
``teardown_remaining()``, so both expire at the same hard deadline. Whichever
timer the loop runs first wins. When it was the waiter's, ``_abandon_cleanup``
cancelled the cleanup while it was still inside the close, the cancellation
skipped ``await release()``, and the provider drain then reported "One or more
provider runtimes failed to close" -- the ``test_bounded_shutdown`` flake.

These tests make the waiter's bound fire first on purpose, the same ordering a
descheduled worker produces between the two ``teardown_remaining()`` reads,
and hold the rule: the close is given up, the release still runs, and the
cancellation still reaches whoever awaits the cleanup task.
"""

import asyncio
from typing import cast

import pytest
from starlette.responses import ContentStream

from my_claude_code.api import response_streams
from my_claude_code.api.response_streams import ManagedStreamingResponse


class _HungBody:
    """A body whose close never returns: the open, silent upstream."""

    def __init__(self) -> None:
        self.never = asyncio.Event()

    def __aiter__(self) -> _HungBody:
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        await self.never.wait()


def _waiter_fires_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop is in force and the cleanup's own close bound is the later one.

    Decided by which task asks rather than by call order, so the test does not
    depend on whether the loop runs the waiter or the cleanup first.
    """

    def budget() -> float:
        task = asyncio.current_task()
        if task is not None and task.get_name() == "mcc-api-response-cleanup":
            return 1.0
        return 0.05

    monkeypatch.setattr(response_streams, "_cleanup_wait_budget", budget)


@pytest.mark.asyncio
async def test_a_cleanup_abandoned_inside_its_close_still_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _waiter_fires_first(monkeypatch)
    released: list[str] = []

    async def release() -> None:
        released.append("lease")

    body = _HungBody()
    response = ManagedStreamingResponse(cast(ContentStream, body))
    response.bind_release(release)

    # The waiter gives up at its own bound and returns without the cleanup.
    await asyncio.wait_for(response.aclose(), 2.0)
    cleanup = response._cleanup_task
    assert cleanup is not None
    # The abandoned task finishes on its own: the release, then the
    # cancellation it was given.
    await asyncio.wait((cleanup,), timeout=2.0)

    assert released == ["lease"]
    assert cleanup.cancelled()
    body.never.set()


@pytest.mark.asyncio
async def test_a_release_that_raises_still_leaves_the_task_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release's own failure stays trace-only; the cancellation wins."""

    _waiter_fires_first(monkeypatch)
    attempted: list[str] = []

    async def release() -> None:
        attempted.append("lease")
        raise RuntimeError("release failed")

    body = _HungBody()
    response = ManagedStreamingResponse(cast(ContentStream, body))
    response.bind_release(release)

    await asyncio.wait_for(response.aclose(), 2.0)
    cleanup = response._cleanup_task
    assert cleanup is not None
    await asyncio.wait((cleanup,), timeout=2.0)

    assert attempted == ["lease"]
    assert cleanup.cancelled()
    body.never.set()


@pytest.mark.asyncio
async def test_a_cleanup_that_is_not_abandoned_releases_once_and_ends_cleanly() -> None:
    """No stop requested: the close finishes, the release runs once, no cancel."""

    released: list[str] = []

    async def release() -> None:
        released.append("lease")

    body = _HungBody()
    body.never.set()
    response = ManagedStreamingResponse(cast(ContentStream, body))
    response.bind_release(release)

    await asyncio.wait_for(response.aclose(), 2.0)
    await asyncio.wait_for(response.aclose(), 2.0)

    cleanup = response._cleanup_task
    assert cleanup is not None
    assert cleanup.done() and not cleanup.cancelled()
    assert released == ["lease"]
