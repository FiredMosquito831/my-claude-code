"""The loop that gives a benched address its way back.

Since 7.19.0 a bench expiring is "due for a re-check", so something has to run
the check. This is that something, and the properties that matter are the ones
about what it does *not* do: it never probes a chain the operator switched off,
it never probes an address that is not due, and its flush half runs whether
probing is on or off.
"""

import asyncio
import contextlib

import pytest

from my_claude_code.application import proxy_check as check_module
from my_claude_code.config import proxy_chains as store_module
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyCheckRecord,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health
from my_claude_code.runtime.proxy_check_timer import ProxyHealthTimer


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(store_module, "proxy_chains_path", lambda: path)
    reset_proxy_health()
    yield path
    reset_proxy_health()


def _settings() -> Settings:
    # Since 7.53.0 a re-probe is a round of PROXY_CHECK_CONFIRM_ATTEMPTS tries
    # spaced PROXY_CHECK_CONFIRM_SPACING_SECONDS apart. The spacing is zeroed
    # here so a failing round does not sleep a real minute in the suite.
    return Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "k",
            "PROXY_CHECK_CONFIRM_SPACING_SECONDS": 0,
        }
    )


def _seed(enabled: bool) -> tuple[str, str]:
    table = load_proxy_chains()
    table, proxy_id = table.add_endpoint("http://198.51.100.9:8080")
    table = table.with_chain(
        "nvidia_nim",
        ProxyChain(enabled=enabled, entries=(ProxyChainEntry(proxy=proxy_id),)),
    )
    save_proxy_chains(table)
    endpoint = load_proxy_chains().proxies[proxy_id]
    return proxy_id, endpoint.label or mask_proxy_label(endpoint.url)


def _timer(enabled: bool = True) -> ProxyHealthTimer:
    return ProxyHealthTimer(_settings, lambda: enabled)


@pytest.mark.asyncio
async def test_a_due_address_is_re_probed_and_a_pass_puts_it_back(
    store, monkeypatch
) -> None:
    """The whole of the way back, in one tick."""

    _, label = _seed(enabled=True)
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    PROXY_REACHABILITY.restore(label, 1, 0.0, "ConnectError")
    assert PROXY_REACHABILITY.due_for_reprobe(label) is True

    async def _passes(url, destination, **kwargs):
        return ProxyCheckRecord(at="2026-09-17T00:00:00Z", ok=True, tls="strict")

    monkeypatch.setattr(check_module, "check_proxy", _passes)

    assert await _timer().tick() == 1
    assert PROXY_REACHABILITY.is_unhealthy(label) is False


@pytest.mark.asyncio
async def test_a_failing_re_probe_moves_the_address_one_tier_down(
    store, monkeypatch
) -> None:
    """A check that did not pass is not a way back; it is the next tier."""

    _, label = _seed(enabled=True)
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    PROXY_REACHABILITY.restore(label, 1, 0.0, "ConnectError")

    async def _fails(url, destination, **kwargs):
        return ProxyCheckRecord(at="2026-09-17T00:00:00Z", ok=False, detail="no answer")

    monkeypatch.setattr(check_module, "check_proxy", _fails)

    await _timer().tick()

    assert PROXY_REACHABILITY.is_unhealthy(label) is True
    assert PROXY_REACHABILITY.failures(label) == 2
    # Second tier: five minutes, not one.
    assert PROXY_REACHABILITY.remaining(label) > 60.0


@pytest.mark.asyncio
async def test_a_disabled_chain_is_never_probed(store, monkeypatch) -> None:
    """A chain that routes nothing must not make MCC contact anybody.

    This is the consent boundary for a loop that ships switched ON: it is not a
    new conversation, it is a re-check of addresses the operator's own traffic
    already used -- and a switched-off chain has no such traffic.
    """

    _, label = _seed(enabled=False)
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    PROXY_REACHABILITY.restore(label, 1, 0.0, "ConnectError")

    called: list[str] = []

    async def _spy(url, destination, **kwargs):
        called.append(url)
        return ProxyCheckRecord(at="2026-09-17T00:00:00Z", ok=True)

    monkeypatch.setattr(check_module, "check_proxy", _spy)

    assert await _timer().tick() == 0
    assert called == []


@pytest.mark.asyncio
async def test_an_address_that_is_not_due_is_not_probed(store, monkeypatch) -> None:
    """The tier is the cadence. A tick that lands inside it does nothing."""

    _, label = _seed(enabled=True)
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    assert PROXY_REACHABILITY.due_for_reprobe(label) is False

    called: list[str] = []

    async def _spy(url, destination, **kwargs):
        called.append(url)
        return ProxyCheckRecord(at="2026-09-17T00:00:00Z", ok=True)

    monkeypatch.setattr(check_module, "check_proxy", _spy)

    assert await _timer().tick() == 0
    assert called == []


@pytest.mark.asyncio
async def test_the_flush_runs_even_with_probing_switched_off(
    store, monkeypatch
) -> None:
    """Turning the re-prober off must not silently turn durability off too."""

    from my_claude_code.application.proxy_health_store import (
        install_listener,
        remove_listener,
    )

    proxy_id, label = _seed(enabled=True)
    install_listener()
    try:
        PROXY_REACHABILITY.note_failure(label, "ConnectError")
        assert await _timer(enabled=False).tick() == 0
    finally:
        remove_listener()

    assert load_proxy_chains().proxies[proxy_id].health is not None


# ------------------ 7.52.2: the re-prober uses the operator's check settings


def _settings_with(**env: object) -> Settings:
    return Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "k", **env}
    )


def _seed_many(count: int) -> list[str]:
    table = load_proxy_chains()
    ids: list[str] = []
    for index in range(count):
        table, proxy_id = table.add_endpoint(f"http://198.51.100.{10 + index}:8080")
        ids.append(proxy_id)
    table = table.with_chain(
        "nvidia_nim",
        ProxyChain(
            enabled=True,
            entries=tuple(ProxyChainEntry(proxy=proxy_id) for proxy_id in ids),
        ),
    )
    save_proxy_chains(table)
    stored = load_proxy_chains().proxies
    labels = [
        stored[proxy_id].label or mask_proxy_label(stored[proxy_id].url)
        for proxy_id in ids
    ]
    for label in labels:
        PROXY_REACHABILITY.note_failure(label, "ConnectError")
        PROXY_REACHABILITY.restore(label, 1, 0.0, "ConnectError")
    return labels


@pytest.mark.asyncio
async def test_reprobe_uses_proxy_check_timeout_setting(store, monkeypatch) -> None:
    """``PROXY_CHECK_TIMEOUT_SECONDS`` reaches the re-probe, not the constant 10."""

    _seed_many(1)
    seen: list[float] = []

    async def _passes(url, destination, **kwargs):
        seen.append(kwargs["timeout"])
        return ProxyCheckRecord(at="2026-09-25T00:00:00Z", ok=True, tls="strict")

    monkeypatch.setattr(check_module, "check_proxy", _passes)
    timer = ProxyHealthTimer(
        lambda: _settings_with(PROXY_CHECK_TIMEOUT_SECONDS=3.5), lambda: True
    )

    assert await timer.tick() == 1
    assert seen == [3.5]


@pytest.mark.asyncio
async def test_reprobe_concurrency_can_exceed_four(store, monkeypatch) -> None:
    """``PROXY_CHECK_MAX_CONCURRENCY`` above the old ceiling of four is honoured."""

    _seed_many(8)
    in_flight = 0
    peak = 0
    all_started = asyncio.Event()

    async def _passes(url, destination, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        if in_flight == 8:
            all_started.set()
        # Released when all eight are in flight at once; a ceiling of four
        # never gets there, and the wait gives up after two seconds.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(all_started.wait(), 2.0)
        in_flight -= 1
        return ProxyCheckRecord(at="2026-09-25T00:00:00Z", ok=True, tls="strict")

    monkeypatch.setattr(check_module, "check_proxy", _passes)
    timer = ProxyHealthTimer(
        lambda: _settings_with(PROXY_CHECK_MAX_CONCURRENCY=8), lambda: True
    )

    assert await timer.tick() == 8
    assert peak == 8


# --------------------- 7.53.0: a re-probe is a round; early confirm after a fail


def _scripted(monkeypatch, answers: list[bool]) -> list[str]:
    """Replace the checker with one that answers ``answers`` in order."""

    calls: list[str] = []

    async def _check(url, destination, **kwargs):
        calls.append(url)
        ok = answers[min(len(calls), len(answers)) - 1]
        return ProxyCheckRecord(
            at="2026-09-25T14:02:00Z",
            ok=ok,
            tls="strict" if ok else "unknown",
            detail="" if ok else "no answer",
        )

    monkeypatch.setattr(check_module, "check_proxy", _check)
    return calls


@pytest.mark.asyncio
async def test_reprobe_notes_failure_once_per_round(store, monkeypatch) -> None:
    """Three failed tries are ONE rung down, not three -- and the row says so."""

    proxy_id, label = _seed(enabled=True)
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    PROXY_REACHABILITY.restore(label, 1, 0.0, "ConnectError")
    calls = _scripted(monkeypatch, [False, False, False])
    timer = ProxyHealthTimer(
        lambda: _settings_with(
            PROXY_CHECK_CONFIRM_ATTEMPTS=3, PROXY_CHECK_CONFIRM_SPACING_SECONDS=0
        ),
        lambda: True,
    )

    assert await timer.tick() == 1

    assert len(calls) == 3
    assert PROXY_REACHABILITY.failures(label) == 2
    stored = load_proxy_chains().proxies[proxy_id].last_check
    assert stored is not None and stored.ok is False
    assert stored.tries == 3


@pytest.mark.asyncio
async def test_any_pass_in_round_clears_bench(store, monkeypatch) -> None:
    """Fail, fail, pass: the pass is the record and the address is back."""

    proxy_id, label = _seed(enabled=True)
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    PROXY_REACHABILITY.restore(label, 1, 0.0, "ConnectError")
    calls = _scripted(monkeypatch, [False, False, True])
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _no_real_wait(fake_sleep))
    timer = ProxyHealthTimer(
        lambda: _settings_with(
            PROXY_CHECK_CONFIRM_ATTEMPTS=3, PROXY_CHECK_CONFIRM_SPACING_SECONDS=30
        ),
        lambda: True,
    )

    assert await timer.tick() == 1

    assert len(calls) == 3
    # Two spacings between three tries, at the operator's number.
    assert waits.count(30.0) == 2
    assert PROXY_REACHABILITY.is_unhealthy(label) is False
    stored = load_proxy_chains().proxies[proxy_id].last_check
    assert stored is not None and stored.ok is True and stored.tries == 3


def _no_real_wait(recorder):
    """``asyncio.sleep`` that records positive waits and yields for zero ones."""

    real = asyncio.sleep

    async def sleep(seconds, result=None):
        if seconds and seconds > 0:
            await recorder(float(seconds))
            return result
        return await real(0, result)

    return sleep


@pytest.mark.asyncio
async def test_early_confirm_never_escalates_ladder(store, monkeypatch) -> None:
    """A fresh live failure is re-tested once at the next tick; a fail changes nothing."""

    from my_claude_code.application.proxy_health_store import (
        install_listener,
        remove_listener,
    )

    proxy_id, label = _seed(enabled=True)
    install_listener()
    try:
        # A live request's failure: 0 -> 1, sixty seconds to the next check.
        PROXY_REACHABILITY.note_failure(label, "ConnectTimeout")
        before = PROXY_REACHABILITY.state(label)
        calls = _scripted(monkeypatch, [False])

        assert await _timer().tick() == 1

        assert len(calls) == 1
        # Nothing moved: same rung, same reason, deadline not pushed out.
        after = PROXY_REACHABILITY.state(label)
        assert after[0] == 1 == before[0]
        assert after[2] == before[2]
        assert after[1] <= before[1]
        assert load_proxy_chains().proxies[proxy_id].last_check is None
        # And it is not re-tested again on the next tick: once per failure.
        assert await _timer().tick() == 0
        assert len(calls) == 1

        # A pass on the early confirm puts the address straight back.
        PROXY_REACHABILITY.note_success(label)
        PROXY_REACHABILITY.note_failure(label, "ConnectTimeout")
        calls = _scripted(monkeypatch, [True])
        assert await _timer().tick() == 1
        assert PROXY_REACHABILITY.is_unhealthy(label) is False
    finally:
        remove_listener()
