"""The scheduled refresh: off by default, floored, never overlapping.

Written against the same four guards ``test_proxy_check_timer`` pins, because
this is the same loop shape and every one of those guards is an incident this
codebase actually had.
"""

import asyncio

import pytest

from my_claude_code.application.proxy_fetch import FetchRun, reset_fetch_job
from my_claude_code.config.constants import PROXY_FEED_MINIMUM_MINUTES
from my_claude_code.config.settings import Settings
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

    async def slow_pass(self):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return FetchRun(at="now")

    monkeypatch.setattr(ProxyFeedTimer, "_fetch", slow_pass)
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True)
    first = asyncio.create_task(timer.tick())
    await started.wait()
    assert await timer.tick() == 0
    release.set()
    await first
    assert calls == 1


@pytest.mark.asyncio
async def test_a_failing_pass_is_logged_and_the_loop_survives(monkeypatch):
    async def boom(self):
        raise RuntimeError("the feeds are having a day")

    monkeypatch.setattr(ProxyFeedTimer, "_fetch", boom)
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True)
    assert await timer.tick() == 0


@pytest.mark.asyncio
async def test_cancellation_propagates_rather_than_being_swallowed(monkeypatch):
    """``ApplicationRuntime.close()`` has to be able to end this."""

    async def cancelled(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(ProxyFeedTimer, "_fetch", cancelled)
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True)
    with pytest.raises(asyncio.CancelledError):
        await timer.tick()


@pytest.mark.asyncio
async def test_the_scheduled_refresh_goes_through_the_same_tested_path(
    monkeypatch, tmp_path
):
    """7: the timer must not be a second, untested, way to write candidates.

    A loop that stored whatever the lists published while the button stored
    only what it had measured would make the candidate list mean two different
    things depending on which of them wrote it last -- and the one that ran
    unattended would be the one storing the unmeasured rows.
    """

    from my_claude_code.config import proxy_chains as chains_config
    from my_claude_code.config.proxy_chains import ProxyChains, save_proxy_chains
    from my_claude_code.config.proxy_feeds import CustomFeed

    store_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(chains_config, "proxy_chains_path", lambda: store_path)
    chains_config.reset_proxy_chains_cache()
    save_proxy_chains(
        ProxyChains().with_feeds(
            [
                CustomFeed(
                    id="f1",
                    name="A list",
                    url="https://example.invalid/list.txt",
                    parser="lines",
                    enabled=True,
                )
            ]
        )
    )
    reset_fetch_job()

    seen: dict[str, object] = {}

    async def record(**kwargs):
        seen.update(kwargs)
        return FetchRun(at="now", working=3)

    monkeypatch.setattr("my_claude_code.application.proxy_fetch.run_fetch_pass", record)
    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.pick_fetch_destination",
        lambda settings, store, requested="": {
            "provider_id": "anthropic",
            "display_name": "Anthropic",
            "base_url": "https://api.anthropic.com",
        },
    )

    # By env alias: the fields carry a ``validation_alias``, so that is the
    # name an operator sets and the name that populates one here.
    settings = Settings.model_validate(
        {
            "PROXY_FETCH_TEST_CONCURRENCY": 48,
            "PROXY_FETCH_CONNECT_TIMEOUT_SECONDS": 3,
            "PROXY_CANDIDATES_MAX": 17,
            "PROXY_FETCH_CONCURRENCY_MODE": "percent",
            "PROXY_FETCH_CHECK_DEPTH": "request",
        }
    )
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True, settings=lambda: settings)
    assert await timer.tick() == 3
    # The tested path, with the operator's own numbers -- not a second pass
    # with defaults of its own.
    assert seen["provider_id"] == "anthropic"
    assert seen["destination"] == "https://api.anthropic.com"
    assert seen["concurrency"] == 48
    assert seen["connect_timeout"] == 3.0
    assert seen["limit"] == 17
    # The timer is the same sweep the button starts, so it follows the same two
    # 7.22.2 settings rather than a default of its own -- otherwise the offer
    # would depend on which of the two happened to run last.
    assert seen["concurrency_mode"] == "percent"
    assert seen["check_depth"] == "request"
    reset_fetch_job()
    chains_config.reset_proxy_chains_cache()


@pytest.mark.asyncio
async def test_a_pass_with_no_https_destination_reads_nothing(monkeypatch, tmp_path):
    """Never store what could not be measured -- not even unattended."""

    from my_claude_code.config import proxy_chains as chains_config
    from my_claude_code.config.proxy_chains import ProxyChains, save_proxy_chains
    from my_claude_code.config.proxy_feeds import CustomFeed

    store_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(chains_config, "proxy_chains_path", lambda: store_path)
    chains_config.reset_proxy_chains_cache()
    save_proxy_chains(
        ProxyChains().with_feeds(
            [
                CustomFeed(
                    id="f1",
                    name="A list",
                    url="https://example.invalid/list.txt",
                    parser="lines",
                    enabled=True,
                )
            ]
        )
    )
    reset_fetch_job()
    called = False

    async def never(**kwargs):
        nonlocal called
        called = True
        return FetchRun(at="now")

    monkeypatch.setattr("my_claude_code.application.proxy_fetch.run_fetch_pass", never)
    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.pick_fetch_destination",
        lambda settings, store, requested="": None,
    )
    timer = ProxyFeedTimer(lambda: 60.0, lambda: True, settings=Settings)
    assert await timer.tick() == 0
    assert called is False
    reset_fetch_job()
    chains_config.reset_proxy_chains_cache()


@pytest.mark.asyncio
async def test_close_ends_a_running_loop(monkeypatch):
    async def never(seconds):
        await asyncio.sleep(3600)

    timer = ProxyFeedTimer(lambda: 60.0, lambda: True, sleep=never)
    assert timer.start() is True
    await timer.close()
    assert timer.running is False
    assert timer.next_refresh_at is None
