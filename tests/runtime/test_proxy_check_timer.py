"""The background checker's loop: off by default, and never in the way.

``WORKING-NOTES`` has three separate incidents of a background loop turning
out to be sitting in front of ``/v1/messages`` -- the models.dev storm, the
pause rebuild, the discovery sweep. Every assertion here is one of those,
written against the guard that exists because of it.
"""

import asyncio

import pytest

from my_claude_code.application.proxy_check import ProxyCheckOutcome
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
)
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.proxy_check_timer import (
    PROXY_CHECK_MINIMUM_MINUTES,
    ProxyCheckTimer,
    resolve_check_interval,
)


def _timer(**kwargs) -> ProxyCheckTimer:
    defaults: dict = {
        "targets": lambda: {"px_1": "https://example.invalid/"},
        "interval_minutes": lambda: 30.0,
        "enabled": lambda: True,
        "exit_ip_url": lambda: "",
    }
    defaults.update(kwargs)
    return ProxyCheckTimer(
        defaults["targets"],
        defaults["interval_minutes"],
        defaults["enabled"],
        defaults["exit_ip_url"],
    )


def test_the_interval_is_zero_unless_the_operator_asked_for_it() -> None:
    """Two ways to say "not now", and the switch is the one that ships.

    An install that never opens the Proxying page must make no outbound
    request it was not asked to, and this is where that is decided.
    """

    assert resolve_check_interval(False, 30) == 0.0
    assert resolve_check_interval(True, 0) == 0.0
    assert resolve_check_interval(True, 30) == 30 * 60.0


def test_a_mistyped_interval_is_raised_to_the_floor() -> None:
    """One sweep is one request per address to somebody else's host.

    A typed ``1`` against a twelve-entry catalogue would be a knock at their
    door every minute, so the floor is applied rather than the value refused:
    the operator meant "often".
    """

    assert resolve_check_interval(True, 1) == PROXY_CHECK_MINIMUM_MINUTES * 60.0


def test_the_shipped_settings_leave_the_loop_off() -> None:
    settings = Settings.model_validate({"model": "nvidia_nim/primary"})

    assert settings.proxy_check_enabled is False
    assert settings.proxy_check_exit_ip_url == ""
    assert (
        resolve_check_interval(
            settings.proxy_check_enabled, settings.proxy_check_interval_minutes
        )
        == 0.0
    )


@pytest.mark.asyncio
async def test_start_does_nothing_while_the_check_is_off() -> None:
    timer = _timer(enabled=lambda: False)

    assert timer.start() is False
    assert timer.running is False
    await timer.close()


@pytest.mark.asyncio
async def test_a_tick_with_nothing_configured_makes_no_call(monkeypatch) -> None:
    """No chains means no addresses, and no addresses means no sockets."""

    called = False

    async def fake(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        "my_claude_code.runtime.proxy_check_timer.check_endpoints", fake
    )
    timer = _timer(targets=dict)

    assert await timer.tick() == 0
    assert called is False


@pytest.mark.asyncio
async def test_a_tick_that_lands_during_a_sweep_is_skipped_not_queued(
    monkeypatch,
) -> None:
    """The discovery-sweep incident, in one assertion.

    A dozen addresses at a ten-second timeout is two minutes in the worst
    case. A queued tick would make the next one worse, so a tick that arrives
    while a sweep runs is dropped.
    """

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake(proxy_ids, destinations, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr(
        "my_claude_code.runtime.proxy_check_timer.check_endpoints", fake
    )
    timer = _timer()

    first = asyncio.create_task(timer.tick())
    await started.wait()
    assert await timer.tick() == 0
    release.set()
    await first
    assert calls == 1


@pytest.mark.asyncio
async def test_a_failed_sweep_never_stops_the_loop(monkeypatch) -> None:
    """A checker that dies on one bad address would stop watching the others."""

    async def fake(*args, **kwargs):
        raise RuntimeError("the network went away")

    monkeypatch.setattr(
        "my_claude_code.runtime.proxy_check_timer.check_endpoints", fake
    )
    timer = _timer()

    assert await timer.tick() == 0
    # And the guard is released, so the next tick is not locked out by the
    # failure of the last one.
    assert timer._sweeping is False


@pytest.mark.asyncio
async def test_a_sweep_reports_what_it_refused_and_what_it_benched(
    monkeypatch,
) -> None:
    """Both outcomes are named, and they are different outcomes.

    A refusal changes what the proxy will *do*; a bench changes only when it
    will try again. Logging them as one line would hide the first behind the
    second.
    """

    async def fake(proxy_ids, destinations, **kwargs):
        return {
            "px_1": ProxyCheckOutcome(
                "203.0.113.7:1080",
                ProxyCheckRecord(ok=False, tls=TLS_INTERCEPTED),
            ),
            "px_2": ProxyCheckOutcome(
                "198.51.100.9:8080", ProxyCheckRecord(ok=False, detail="no answer")
            ),
            "px_3": ProxyCheckOutcome(
                "192.0.2.7:1080", ProxyCheckRecord(ok=True, tls=TLS_STRICT)
            ),
        }

    monkeypatch.setattr(
        "my_claude_code.runtime.proxy_check_timer.check_endpoints", fake
    )
    timer = _timer(
        targets=lambda: {
            "px_1": "https://a.invalid/",
            "px_2": "https://a.invalid/",
            "px_3": "https://a.invalid/",
        }
    )

    assert await timer.tick() == 3


def test_the_targets_are_the_addresses_in_a_chain_and_nothing_else() -> None:
    """A catalogue entry no chain names is not measured.

    The store prunes unreferenced addresses anyway; this is the second half of
    the same rule, so a sweep never asks about an address the operator has
    already removed.
    """

    from my_claude_code.application.proxy_check import check_targets

    store = ProxyChains(
        proxies={
            "px_1": ProxyEndpoint(url="http://198.51.100.9:8080"),
            "px_2": ProxyEndpoint(url="http://192.0.2.7:3128"),
        },
        chains={
            "nvidia_nim": ProxyChain(
                enabled=True,
                entries=(
                    ProxyChainEntry(proxy="px_1"),
                    # Direct has no address to dial.
                    ProxyChainEntry(proxy=""),
                ),
            )
        },
    )
    settings = Settings.model_validate({"model": "nvidia_nim/primary"})

    targets = check_targets(settings, store)

    assert set(targets) == {"px_1"}
    assert targets["px_1"].startswith("https://")
