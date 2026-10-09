"""7.91.0: a vendor source's schedule -- off unless chosen, never a busy loop.

The loop's whole contract on an install with no scheduled source is "read the
store once, off the event loop, and end". With one, it fetches when due --
through the same function *Fetch now* uses -- and never asks again sooner
than :data:`MIN_SLEEP_SECONDS`, even when a fetch could not run.
"""

import asyncio

import pytest

from my_claude_code.application.proxy_vendor_sources import (
    SOURCE_SCHEDULE,
    AccountEdit,
    FetchOutcome,
    ListEdit,
    save_account_source,
    save_list_source,
)
from my_claude_code.config.proxy_chains import ProxyChains
from my_claude_code.config.proxy_sources import (
    ProxySources,
    reset_proxy_sources_cache,
    save_proxy_sources,
)
from my_claude_code.config.settings import Settings
from my_claude_code.runtime import proxy_source_timer
from my_claude_code.runtime.proxy_source_timer import (
    MIN_SLEEP_SECONDS,
    ProxySourceTimer,
)

NORD_URL = "https://api.nordvpn.com/v1/servers?limit=200"


def _store(*, hours: int) -> None:
    _, sources, _ = save_account_source(
        ProxyChains(),
        ProxySources(),
        "",
        AccountEdit(preset="nordvpn", list_url=NORD_URL, refresh_hours=hours),
    )
    _, sources, _ = save_list_source(
        ProxyChains(),
        sources,
        "",
        ListEdit(paste="198.51.100.10:6540:u:p", refresh_hours=hours),
    )
    save_proxy_sources(sources)
    reset_proxy_sources_cache()


def _timer(sleeps: list[float], *, stop_after: int = 1) -> ProxySourceTimer:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= stop_after:
            raise asyncio.CancelledError

    async def republish(ids) -> str:
        return ""

    return ProxySourceTimer(lambda: Settings(), republish, sleep=sleep)


def _task(timer: ProxySourceTimer) -> asyncio.Task[None]:
    task = timer._task
    assert task is not None
    return task


@pytest.fixture
def fetched(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake(settings, source_id, republish) -> FetchOutcome:
        calls.append(source_id)
        return FetchOutcome(source_id, True, "Fetched: 1 proxy.", 1)

    monkeypatch.setattr(proxy_source_timer, "fetch_source_now", fake)
    return calls


@pytest.mark.asyncio
async def test_with_no_schedule_the_loop_reads_once_and_ends(fetched) -> None:
    _store(hours=0)
    sleeps: list[float] = []
    timer = _timer(sleeps)

    assert timer.start() is True
    await asyncio.wait_for(_task(timer), 5)

    assert fetched == []
    assert sleeps == []
    assert timer.running is False


@pytest.mark.asyncio
async def test_an_empty_store_ends_the_loop_too(fetched) -> None:
    sleeps: list[float] = []
    timer = _timer(sleeps)

    timer.start()
    await asyncio.wait_for(_task(timer), 5)

    assert (fetched, sleeps) == ([], [])


@pytest.mark.asyncio
async def test_a_scheduled_source_never_fetched_is_fetched_then_the_loop_rests(
    fetched,
) -> None:
    _store(hours=6)
    sleeps: list[float] = []
    timer = _timer(sleeps)

    timer.start()
    await asyncio.gather(_task(timer), return_exceptions=True)

    # The account's server list is due; the pasted list has nothing to fetch.
    assert fetched == ["src_account_nordvpn"]
    assert sleeps == [MIN_SLEEP_SECONDS]


@pytest.mark.asyncio
async def test_a_fetch_that_records_nothing_never_spins(monkeypatch) -> None:
    _store(hours=6)
    calls: list[str] = []

    async def busy(settings, source_id, republish) -> FetchOutcome:
        calls.append(source_id)
        return FetchOutcome(
            source_id, False, "A fetch of this source is already running."
        )

    monkeypatch.setattr(proxy_source_timer, "fetch_source_now", busy)
    sleeps: list[float] = []
    timer = _timer(sleeps, stop_after=3)

    timer.start()
    await asyncio.gather(_task(timer), return_exceptions=True)

    assert sleeps == [MIN_SLEEP_SECONDS] * 3
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_save_wakes_the_loop_through_the_hook(fetched) -> None:
    sleeps: list[float] = []
    timer = _timer(sleeps)
    timer.start()
    await asyncio.wait_for(_task(timer), 5)
    assert timer.running is False

    SOURCE_SCHEDULE.attach(timer.rearm)
    _store(hours=6)
    SOURCE_SCHEDULE.changed()
    assert timer.running is True
    await asyncio.gather(_task(timer), return_exceptions=True)

    assert fetched == ["src_account_nordvpn"]
