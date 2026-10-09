"""PR-S2 (7.89.0): the rule that decides whether "Where does Direct come out?"
may dial a provider from this computer's own address.

The same reads real requests and probes are built from; the leak contract
drives the route against a rig (``tests/contracts``), these pin each branch.
"""

import pytest

from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health
from my_claude_code.providers.runtime.config import direct_exit_refusal

PROXY = "socks5h://203.0.113.7:1080"


@pytest.fixture(autouse=True)
def _fresh():
    reset_proxy_health()
    yield
    reset_proxy_health()


def _use(monkeypatch, chain: ProxyChain | None) -> None:
    store = ProxyChains(
        proxies={"px_a": ProxyEndpoint(url=PROXY)},
        chains={} if chain is None else {"opencode": chain},
    )
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.config.current_proxy_chains", lambda: store
    )


def _refusal(static: str = "") -> str:
    return direct_exit_refusal("opencode", static, Settings(), name="OpenCode Zen")


def _chain(*entries: str, **extra) -> ProxyChain:
    return ProxyChain(
        enabled=True,
        entries=tuple(ProxyChainEntry(proxy=entry) for entry in entries),
        **extra,
    )


def test_no_chain_and_no_static_proxy_may_look(monkeypatch) -> None:
    _use(monkeypatch, None)
    assert _refusal() == ""


def test_a_static_proxy_never_looks_from_here(monkeypatch) -> None:
    _use(monkeypatch, None)
    assert _refusal(PROXY).startswith("Not sent: OpenCode Zen goes out through")


def test_a_chain_with_a_direct_entry_may_look(monkeypatch) -> None:
    _use(monkeypatch, _chain("px_a", "", direct_fallback=False))
    assert _refusal() == ""


def test_under_single_a_direct_entry_below_the_first_is_never_used(
    monkeypatch,
) -> None:
    _use(monkeypatch, _chain("px_a", "", policy="single", direct_fallback=False))
    assert "Direct fallback off" in _refusal()


def test_a_healthy_proxy_and_direct_fallback_on_does_not_look(monkeypatch) -> None:
    _use(monkeypatch, _chain("px_a", "px_a", direct_fallback=True))
    assert "only once every proxy in it is unhealthy" in _refusal()


def test_every_proxy_unhealthy_and_direct_fallback_on_may_look(monkeypatch) -> None:
    _use(monkeypatch, _chain("px_a", direct_fallback=True))
    PROXY_REACHABILITY.note_failure("203.0.113.7:1080", "rig: refused")
    # A one-entry chain is its entry: never from here, healthy or not.
    assert _refusal().startswith("Not sent:")

    two = ProxyChains(
        proxies={
            "px_a": ProxyEndpoint(url=PROXY),
            "px_b": ProxyEndpoint(url="socks5h://198.51.100.9:1080"),
        },
        chains={"opencode": _chain("px_a", "px_b", direct_fallback=True)},
    )
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.config.current_proxy_chains", lambda: two
    )
    PROXY_REACHABILITY.note_failure("198.51.100.9:1080", "rig: refused")
    assert _refusal() == ""


def test_a_chain_with_nothing_usable_is_refused_with_its_own_sentence(
    monkeypatch,
) -> None:
    _use(
        monkeypatch, ProxyChain(enabled=True, entries=(ProxyChainEntry("px_a", True),))
    )
    assert "Proxying page" in _refusal()
