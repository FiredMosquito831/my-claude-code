"""Which exit a Providers-card probe leaves through (7.78.7, leak C-1).

``masked_exit_for`` picks one exit from the chain real requests are built from.
These pin its rules one at a time against a real store on disk and the real
health ledgers -- the same three tables ``ProxyRotationState`` reads:

* no chain, a switched-off chain, an unacknowledged OAuth chain, or a chain
  whose entries are all paused hands back the static proxy *object* itself;
* a one-entry chain is that entry, as it is for real requests;
* otherwise the first entry in the chain's order that is neither unreachable,
  intercepted nor in a trigger cooldown -- only entry 0 under ``single``;
* a cooldown-only entry before this computer's address;
* this computer's address only when every entry is unhealthy and Direct
  fallback is on; with it off, nothing, and a sentence naming the setting.
"""

import pytest

from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
    reset_proxy_health,
)
from my_claude_code.providers.runtime.config import MaskedExit, masked_exit_for

PROVIDER = "nvidia_nim"
URLS = (
    "socks5h://u:p@203.0.113.7:1080",
    "http://198.51.100.9:8080",
    "socks5://192.0.2.44:1081",
)
LABELS = ("203.0.113.7:1080", "198.51.100.9:8080", "192.0.2.44:1081")
STATIC = "http://static.example:3128"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    reset_proxy_chains_cache()
    reset_proxy_health()

    def write(chains: ProxyChains) -> None:
        save_proxy_chains(chains, path)

    yield write
    reset_proxy_chains_cache()
    reset_proxy_health()


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate({})


def _chains(
    provider_id: str = PROVIDER,
    *,
    entries: tuple[ProxyChainEntry, ...] | None = None,
    **chain_kwargs,
) -> ProxyChains:
    return ProxyChains(
        proxies={
            f"px_{index}": ProxyEndpoint(url=url) for index, url in enumerate(URLS)
        },
        chains={
            provider_id: ProxyChain(
                enabled=chain_kwargs.pop("enabled", True),
                entries=entries
                if entries is not None
                else tuple(ProxyChainEntry(proxy=f"px_{i}") for i in range(3)),
                **chain_kwargs,
            )
        },
    )


@pytest.mark.parametrize("static", [None, "", STATIC])
def test_no_chain_hands_back_the_static_proxy_itself(store, settings, static) -> None:
    store(ProxyChains())

    exit_ = masked_exit_for(PROVIDER, static, settings)

    assert exit_ == MaskedExit(proxy=static)
    assert exit_.proxy is static


@pytest.mark.parametrize(
    "chains",
    [
        _chains(enabled=False),
        _chains(
            entries=tuple(ProxyChainEntry(f"px_{i}", paused=True) for i in range(3))
        ),
        _chains("chatgpt_oauth", oauth_acknowledged=False),
        _chains("some_other_provider"),
    ],
    ids=["switched_off", "all_paused", "oauth_not_acknowledged", "other_provider"],
)
def test_a_chain_the_resolver_drops_changes_nothing(store, settings, chains) -> None:
    store(chains)
    provider = "chatgpt_oauth" if "chatgpt_oauth" in chains.chains else PROVIDER

    assert masked_exit_for(provider, STATIC, settings) == MaskedExit(proxy=STATIC)


def test_a_one_entry_chain_is_its_entry(store, settings) -> None:
    store(_chains(entries=(ProxyChainEntry(proxy="px_1"),)))

    assert masked_exit_for(PROVIDER, STATIC, settings) == MaskedExit(
        proxy=URLS[1], label=LABELS[1]
    )


def test_a_one_entry_direct_chain_is_this_computer(store, settings) -> None:
    """The operator wrote Direct as the only entry: explicit, as for requests."""

    store(_chains(entries=(ProxyChainEntry(proxy=""),)))

    assert masked_exit_for(PROVIDER, STATIC, settings) == MaskedExit(
        proxy="", label=DIRECT_PROXY_LABEL
    )


def test_a_healthy_chain_gives_its_first_entry(store, settings) -> None:
    store(_chains(direct_fallback=False))

    assert masked_exit_for(PROVIDER, STATIC, settings) == MaskedExit(
        proxy=URLS[0], label=LABELS[0]
    )


@pytest.mark.parametrize("mark", ["unreachable", "intercepted", "cooldown"])
def test_an_entry_held_out_is_skipped_in_order(store, settings, mark) -> None:
    store(_chains(direct_fallback=False))
    _mark(LABELS[0], mark)

    assert masked_exit_for(PROVIDER, None, settings) == MaskedExit(
        proxy=URLS[1], label=LABELS[1]
    )


def test_a_cooldown_entry_comes_before_this_computer(store, settings) -> None:
    """Every proxy rate-limited but answering: still a proxy, never Direct."""

    store(_chains(direct_fallback=True))
    _mark(LABELS[0], "unreachable")
    _mark(LABELS[1], "cooldown")
    _mark(LABELS[2], "intercepted")

    assert masked_exit_for(PROVIDER, None, settings) == MaskedExit(
        proxy=URLS[1], label=LABELS[1]
    )


def test_every_entry_unhealthy_with_direct_fallback_on_is_this_computer(
    store, settings
) -> None:
    store(_chains(direct_fallback=True))
    _mark(LABELS[0], "unreachable")
    _mark(LABELS[1], "intercepted")
    _mark(LABELS[2], "unreachable")

    assert masked_exit_for(PROVIDER, STATIC, settings) == MaskedExit(
        proxy="", label=DIRECT_PROXY_LABEL
    )


def test_every_entry_unhealthy_with_direct_fallback_off_is_refused(
    store, settings
) -> None:
    store(_chains(direct_fallback=False))
    for label in LABELS:
        _mark(label, "unreachable")

    exit_ = masked_exit_for(PROVIDER, STATIC, settings, name="NVIDIA NIM")

    assert exit_.proxy is None
    assert exit_.label is None
    assert "Direct fallback is off" in exit_.refused
    assert "Proxying page, NVIDIA NIM" in exit_.refused
    # Never the password the stored URL carries.
    assert "u:p" not in exit_.refused


def test_a_direct_entry_in_the_chain_is_an_ordinary_entry(store, settings) -> None:
    """Direct written into the chain is the operator's permission, in order."""

    store(
        _chains(
            entries=(
                ProxyChainEntry(proxy="px_0"),
                ProxyChainEntry(proxy=""),
                ProxyChainEntry(proxy="px_1"),
            ),
            direct_fallback=False,
        )
    )
    _mark(LABELS[0], "unreachable")

    assert masked_exit_for(PROVIDER, None, settings) == MaskedExit(
        proxy="", label=DIRECT_PROXY_LABEL
    )


@pytest.mark.parametrize("direct_fallback", [True, False])
def test_single_policy_only_ever_offers_its_first_entry(
    store, settings, direct_fallback
) -> None:
    """``single`` serves from entry 0 alone; a real request never reaches 1."""

    store(_chains(policy="single", direct_fallback=direct_fallback))
    _mark(LABELS[0], "unreachable")

    exit_ = masked_exit_for(PROVIDER, None, settings)

    if direct_fallback:
        assert exit_ == MaskedExit(proxy="", label=DIRECT_PROXY_LABEL)
    else:
        assert exit_.refused


def test_choosing_writes_nothing_to_the_ledgers(store, settings) -> None:
    """A probe reads health; it is not a request and charges nothing."""

    store(_chains(direct_fallback=False))
    before = [PROXY_HEALTH.snapshot(PROVIDER, label) for label in LABELS]

    masked_exit_for(PROVIDER, None, settings)

    assert [PROXY_HEALTH.snapshot(PROVIDER, label) for label in LABELS] == before
    assert all(not PROXY_REACHABILITY.is_unhealthy(label) for label in LABELS)


def _mark(label: str, how: str) -> None:
    if how == "unreachable":
        PROXY_REACHABILITY.note_failure(label, "test")
    elif how == "intercepted":
        PROXY_INTERCEPTION.mark(label, "test")
    else:
        PROXY_HEALTH.note_failure(PROVIDER, label, benched_for=300.0, reason="test")
