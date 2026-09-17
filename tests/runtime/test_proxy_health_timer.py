"""The loop that gives a benched address its way back.

Since 7.19.0 a bench expiring is "due for a re-check", so something has to run
the check. This is that something, and the properties that matter are the ones
about what it does *not* do: it never probes a chain the operator switched off,
it never probes an address that is not due, and its flush half runs whether
probing is on or off.
"""

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
    return Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "k"}
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
