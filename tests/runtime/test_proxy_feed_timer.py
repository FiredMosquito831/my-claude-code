"""The scheduled refresh: off by default, floored, never overlapping.

Written against the same four guards ``test_proxy_check_timer`` pins, because
this is the same loop shape and every one of those guards is an incident this
codebase actually had.
"""

import asyncio

import pytest

from my_claude_code.config.constants import PROXY_FEED_MINIMUM_MINUTES
from my_claude_code.runtime.proxy_feed_timer import (
    ProxyFeedTimer,
    resolve_feed_interval,
)


def test_the_loop_is_off_unless_the_operator_turned_it_on():
    """Both halves of "off": the switch, and a zero interval."""

    assert resolve_feed_interval(False, 60) == 0.0
    assert resolve_feed_interval(True, 0) == 0.0
    assert resolve_feed_interval(False, 0) == 0.0


def test_a_short_interval_is_raised_to_the_floor():
    """These are other people's servers and none refresh faster than this."""

    assert resolve_feed_interval(True, 1) == PROXY_FEED_MINIMUM_MINUTES * 60.0
    assert resolve_feed_interval(True, PROXY_FEED_MINIMUM_MINUTES) == (
        PROXY_FEED_MINIMUM_MINUTES * 60.0
    )
    assert resolve_feed_interval(True, 120) == 7200.0


def test_start_does_nothing_while_the_refresh_is_off():
    timer = ProxyFeedTimer(lambda: 60.0, lambda: False)
    assert timer.start() is False
    assert timer.running is False


@pytest.mark.asyncio
async def test_a_tick_that_lands_mid_pass_is_skipped_not_queued(monkeypatch):
    """Seven feeds at fifteen seconds is a pass worth not doubling."""

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_ingest(**kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        from my_claude_code.application.proxy_ingest import IngestRun

        return IngestRun(at="now")

    monkeypatch.setattr("my_claude_code.runtime.proxy_feed_timer.ingest", slow_ingest)
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True)
    first = asyncio.create_task(timer.tick())
    await started.wait()
    assert await timer.tick() == 0
    release.set()
    await first
    assert calls == 1


@pytest.mark.asyncio
async def test_a_failing_pass_is_logged_and_the_loop_survives(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("the feeds are having a day")

    monkeypatch.setattr("my_claude_code.runtime.proxy_feed_timer.ingest", boom)
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True)
    assert await timer.tick() == 0


@pytest.mark.asyncio
async def test_cancellation_propagates_rather_than_being_swallowed(monkeypatch):
    """``ApplicationRuntime.close()`` has to be able to end this."""

    async def cancelled(**kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("my_claude_code.runtime.proxy_feed_timer.ingest", cancelled)
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True)
    with pytest.raises(asyncio.CancelledError):
        await timer.tick()


@pytest.mark.asyncio
async def test_close_ends_a_running_loop(monkeypatch):
    async def never(seconds):
        await asyncio.sleep(3600)

    timer = ProxyFeedTimer(lambda: 60.0, lambda: True, sleep=never)
    assert timer.start() is True
    await timer.close()
    assert timer.running is False
    assert timer.next_refresh_at is None
