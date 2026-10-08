"""Direct fallback off means never this computer's own address (7.78.8, C-2).

``resolve_proxy_chain`` used to collapse a chain with no usable entry -- every
entry paused, every address removed, no entry at all -- to "no plan, no proxy",
which builds a provider that dials from this computer whatever the chain's
Direct fallback said. It now resolves that case, and only that case, to a
:class:`MaskedRefusalPlan`. These tests pin the rule from both sides: every
shape that must refuse refuses, with a sentence naming the setting; and every
other shape resolves to exactly what the 7.78.7 resolver gave it, compared
against that resolver's own body over the whole grid.
"""

import itertools
from collections.abc import Iterator

import pytest

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.proxy_chains import (
    OAUTH_PROVIDER_IDS,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    current_proxy_chains,
    masked_refusal_sentence,
    reset_proxy_chains_cache,
    save_proxy_chains,
    unusable_chain_cause,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.providers.base import MaskedRefusalPlan, ProxyChainPlan, ProxyLeg
from my_claude_code.providers.runtime.config import (
    build_provider_config,
    masked_exit_for,
    resolve_proxy_chain,
)

PROVIDER = "nvidia_nim"
NAME = PROVIDER_CATALOG[PROVIDER].display_name
STATIC = "http://203.0.113.50:3128"
PROXIES = {
    "px_one": ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080"),
    "px_two": ProxyEndpoint(url="http://198.51.100.9:8080"),
}


@pytest.fixture
def store(tmp_path, monkeypatch) -> Iterator:
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    reset_proxy_chains_cache()

    def write(chains: ProxyChains) -> None:
        save_proxy_chains(chains, path)
        reset_proxy_chains_cache()

    yield write
    reset_proxy_chains_cache()


def _settings(static: str = "") -> Settings:
    return Settings.model_validate(
        {"NVIDIA_NIM_API_KEY": "nim-key", "NVIDIA_NIM_PROXY": static}
    )


def _chain(*entries: ProxyChainEntry, **kwargs) -> ProxyChains:
    return ProxyChains(
        proxies=PROXIES,
        chains={PROVIDER: ProxyChain(enabled=True, entries=entries, **kwargs)},
    )


def _assert_refusal(plan: ProxyChainPlan | None, *phrases: str) -> str:
    assert isinstance(plan, MaskedRefusalPlan), plan
    assert plan.legs == ()
    assert plan.direct_fallback is False
    for phrase in (
        f"Not sent: {NAME}'s proxy chain",
        "Direct fallback is off",
        f"Proxying page -> {NAME}",
        *phrases,
    ):
        assert phrase in plan.reason, plan.reason
    return plan.reason


# ---------------------------------------------------------- what refuses


def test_every_entry_paused_and_direct_fallback_off_is_refused(store) -> None:
    store(
        _chain(
            ProxyChainEntry(proxy="px_one", paused=True),
            ProxyChainEntry(proxy="px_two", paused=True),
            direct_fallback=False,
        )
    )

    config = build_provider_config(PROVIDER_CATALOG[PROVIDER], _settings())

    assert config.proxy == ""
    _assert_refusal(config.proxy_chain, "all 2 entries are paused")


def test_every_address_removed_and_direct_fallback_off_is_refused(store) -> None:
    """Entries naming addresses the catalogue lost are dropped at read."""

    store(
        ProxyChains(
            proxies={},
            chains={
                PROVIDER: ProxyChain(
                    enabled=True,
                    entries=(
                        ProxyChainEntry(proxy="px_one"),
                        ProxyChainEntry(proxy="px_two"),
                    ),
                    direct_fallback=False,
                )
            },
        )
    )
    read = current_proxy_chains().chain(PROVIDER)
    assert read is not None
    assert read.entries == ()

    config = build_provider_config(PROVIDER_CATALOG[PROVIDER], _settings())

    _assert_refusal(config.proxy_chain, "it has no entries")


def test_an_enabled_chain_with_no_entry_and_direct_fallback_off_is_refused(
    store,
) -> None:
    store(_chain(direct_fallback=False))

    _proxy, plan = resolve_proxy_chain(PROVIDER, "", _settings(), name=NAME)

    _assert_refusal(plan, "it has no entries")


def test_an_entry_naming_a_lost_address_in_memory_is_refused_too(
    monkeypatch,
) -> None:
    """The same test the legs are built with, on a table no read pruned."""

    table = ProxyChains(
        proxies={},
        chains={
            PROVIDER: ProxyChain(
                enabled=True,
                entries=(
                    ProxyChainEntry(proxy="px_gone"),
                    ProxyChainEntry(proxy="px_one", paused=True),
                ),
                direct_fallback=False,
            )
        },
    )
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.config.current_proxy_chains", lambda: table
    )

    _proxy, plan = resolve_proxy_chain(PROVIDER, "", _settings(), name=NAME)

    _assert_refusal(plan, "every entry is paused or names an address that was removed")


def test_the_probe_is_refused_with_the_same_sentence(store) -> None:
    """PR-2's probes read the same resolver: not sent, and why."""

    store(_chain(ProxyChainEntry(proxy="px_one", paused=True), direct_fallback=False))

    exit_ = masked_exit_for(PROVIDER, "", _settings(), name=NAME)
    _proxy, plan = resolve_proxy_chain(PROVIDER, "", _settings(), name=NAME)

    assert exit_.proxy is None
    assert isinstance(plan, MaskedRefusalPlan)
    assert exit_.refused == plan.reason
    assert "its only entry is paused" in exit_.refused


# ------------------------------------------------------- what is unchanged


def test_direct_fallback_on_with_nothing_usable_is_what_it_always_was(store) -> None:
    """The user's rule for this case is a later change (PR-4), not this one."""

    store(
        _chain(
            ProxyChainEntry(proxy="px_one", paused=True),
            ProxyChainEntry(proxy="px_two", paused=True),
            direct_fallback=True,
        )
    )

    assert resolve_proxy_chain(PROVIDER, "", _settings()) == ("", None)
    assert resolve_proxy_chain(PROVIDER, STATIC, _settings(STATIC)) == (STATIC, None)


def test_a_static_proxy_still_carries_a_chain_with_nothing_usable(store) -> None:
    """Masked through ``<PROVIDER>_PROXY``, exactly as before: not refused."""

    store(_chain(ProxyChainEntry(proxy="px_one", paused=True), direct_fallback=False))

    config = build_provider_config(PROVIDER_CATALOG[PROVIDER], _settings(STATIC))

    assert config.proxy == STATIC
    assert config.proxy_chain is None


def test_a_switched_off_chain_is_the_operator_s_switch(store) -> None:
    store(
        ProxyChains(
            proxies=PROXIES,
            chains={
                PROVIDER: ProxyChain(
                    enabled=False,
                    entries=(ProxyChainEntry(proxy="px_one", paused=True),),
                    direct_fallback=False,
                )
            },
        )
    )

    assert resolve_proxy_chain(PROVIDER, "", _settings()) == ("", None)


@pytest.mark.parametrize("provider_id", sorted(OAUTH_PROVIDER_IDS))
def test_an_unacknowledged_subscription_chain_stays_inert(
    store, provider_id: str
) -> None:
    """By design: inert until acknowledged, refused or not (the card says so)."""

    store(
        ProxyChains(
            proxies=PROXIES,
            chains={
                provider_id: ProxyChain(
                    enabled=True,
                    entries=(ProxyChainEntry(proxy="px_one", paused=True),),
                    direct_fallback=False,
                    oauth_acknowledged=False,
                )
            },
        )
    )

    assert resolve_proxy_chain(provider_id, "", _settings()) == ("", None)
    assert unusable_chain_cause(current_proxy_chains(), provider_id) == ""


def test_one_usable_entry_routes_as_before(store) -> None:
    store(
        _chain(
            ProxyChainEntry(proxy="px_one", paused=True),
            ProxyChainEntry(proxy="px_two"),
            direct_fallback=False,
        )
    )

    assert resolve_proxy_chain(PROVIDER, "", _settings()) == (
        PROXIES["px_two"].url,
        None,
    )


# ---------------------------------------------------- the whole grid, both ways


def _resolve_7_78_7(
    provider_id: str, static_proxy: str, settings: Settings
) -> tuple[str, ProxyChainPlan | None]:
    """``resolve_proxy_chain`` exactly as 7.78.7 shipped it (88f73cb2)."""

    store = current_proxy_chains()
    chain = store.chain(provider_id)
    if chain is None or not chain.enabled or not chain.entries:
        return static_proxy, None
    if provider_id in OAUTH_PROVIDER_IDS and not chain.oauth_acknowledged:
        return static_proxy, None

    legs: list[ProxyLeg] = []
    for entry in chain.entries:
        if entry.paused:
            continue
        if entry.is_direct:
            legs.append(ProxyLeg(url="", label=DIRECT_PROXY_LABEL))
            continue
        endpoint = store.endpoint(entry.proxy)
        if endpoint is None:
            continue
        legs.append(
            ProxyLeg(
                url=endpoint.url,
                label=endpoint.label or mask_proxy_label(endpoint.url),
            )
        )

    if not legs:
        return static_proxy, None
    if len(legs) == 1:
        return legs[0].url, None
    return "", ProxyChainPlan(
        legs=tuple(legs),
        policy=chain.policy,
        on=frozenset(chain.on),
        scope=chain.scope,
        max_switches=min(
            chain.max_switches, int(settings.proxy_max_switches_per_request)
        ),
        direct_fallback=chain.direct_fallback,
    )


#: Each entry slot: absent, a live address, a paused one, a lost one, Direct.
_SLOTS = (
    None,
    ProxyChainEntry(proxy="px_one"),
    ProxyChainEntry(proxy="px_two", paused=True),
    ProxyChainEntry(proxy="px_gone"),
    ProxyChainEntry(proxy="", paused=False),
    ProxyChainEntry(proxy="", paused=True),
)


def _grid() -> Iterator[tuple[str, ProxyChains, str]]:
    for (
        provider_id,
        first,
        second,
        enabled,
        fallback,
        acked,
        static,
    ) in itertools.product(
        (PROVIDER, "chatgpt_oauth"),
        _SLOTS,
        _SLOTS,
        (True, False),
        (True, False),
        (True, False),
        ("", STATIC),
    ):
        entries = tuple(slot for slot in (first, second) if slot is not None)
        table = ProxyChains(
            proxies=PROXIES,
            chains={
                provider_id: ProxyChain(
                    enabled=enabled,
                    entries=entries,
                    direct_fallback=fallback,
                    oauth_acknowledged=acked,
                )
            },
        )
        yield provider_id, table, static


def test_refused_exactly_when_the_card_says_so_and_identical_otherwise(
    monkeypatch,
) -> None:
    """Every chain shape: refused iff nothing usable, Direct fallback off, no
    static proxy -- and every other shape resolves byte for byte as 7.78.7.
    """

    settings = _settings()
    current: dict[str, ProxyChains] = {}
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.config.current_proxy_chains",
        lambda: current["table"],
    )
    monkeypatch.setattr(f"{__name__}.current_proxy_chains", lambda: current["table"])
    refused = unchanged = 0
    for provider_id, table, static in _grid():
        current["table"] = table
        chain = table.chain(provider_id)
        assert chain is not None
        new = resolve_proxy_chain(provider_id, static, settings, name=NAME)
        sentence = masked_refusal_sentence(table, provider_id, NAME)
        should_refuse = bool(sentence) and not chain.direct_fallback and not static
        if should_refuse:
            refused += 1
            assert new == (
                "",
                MaskedRefusalPlan(direct_fallback=False, reason=sentence),
            )
        else:
            unchanged += 1
            assert not isinstance(new[1], MaskedRefusalPlan)
            assert new == _resolve_7_78_7(provider_id, static, settings), (
                provider_id,
                table,
                static,
            )
    assert refused and unchanged
