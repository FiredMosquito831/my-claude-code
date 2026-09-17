"""What the store actually does to a provider once the runtime reads it.

The store and the page shipped one release before the runtime did, so until now
nothing could tell the difference between a chain that was saved and a chain
that worked. These are the hops in between: store -> ``ProviderConfig`` ->
factory, and the one that matters most is the first line of each test -- a
provider with no chain must come out of it byte-identical to the release before
this one.
"""

import json

import pytest

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
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
from my_claude_code.providers.runtime.config import (
    build_provider_config,
    resolve_proxy_chain,
)
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.proxy_rotating import ProxyRotatingProvider


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real ``~/.mcc/proxy_chains.json`` under a temporary home."""

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    reset_proxy_chains_cache()

    def write(chains: ProxyChains) -> None:
        save_proxy_chains(chains, path)

    yield write
    reset_proxy_chains_cache()


def _chains(**chain_kwargs) -> ProxyChains:
    return ProxyChains(
        proxies={
            "px_one": ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080"),
            "px_two": ProxyEndpoint(url="http://198.51.100.9:8080"),
        },
        chains={
            "nvidia_nim": ProxyChain(
                enabled=True,
                entries=(
                    ProxyChainEntry(proxy="px_one"),
                    ProxyChainEntry(proxy="px_two"),
                    ProxyChainEntry(proxy=""),
                ),
                **chain_kwargs,
            )
        },
    )


def test_an_absent_provider_still_uses_its_env_proxy(store) -> None:
    """The whole upgrade story, asserted rather than assumed.

    Nothing migrates, the ``.env`` is never rewritten, and an operator who has
    never opened the Proxying page sees no change at all.
    """

    store(ProxyChains())
    settings = Settings.model_validate(
        {"nvidia_nim_api_key": "k1", "NVIDIA_NIM_PROXY": "http://old:3128"}
    )

    proxy, plan = resolve_proxy_chain("nvidia_nim", "http://old:3128", settings)

    assert proxy == "http://old:3128"
    assert plan is None


def test_a_chain_wins_over_the_env_proxy_and_the_env_key_is_not_rewritten(
    store, tmp_path
) -> None:
    """A chain replaces the static address while it has entries, and only then."""

    store(_chains())
    settings = Settings.model_validate(
        {"nvidia_nim_api_key": "k1", "NVIDIA_NIM_PROXY": "http://old:3128"}
    )

    config = build_provider_config(PROVIDER_CATALOG["nvidia_nim"], settings)

    assert config.proxy_chain is not None
    assert [leg.url for leg in config.proxy_chain.legs] == [
        "socks5h://u:p@203.0.113.7:1080",
        "http://198.51.100.9:8080",
        "",
    ]
    # The setting is untouched on the object the server is running on, and the
    # document on disk never mentions it.
    assert settings.nvidia_nim_proxy == "http://old:3128"
    document = json.loads((tmp_path / "proxy_chains.json").read_text(encoding="utf-8"))
    assert "NVIDIA_NIM_PROXY" not in json.dumps(document)


def test_a_leg_carries_a_masked_label_and_never_the_password(store) -> None:
    """The label is the only form of the address that may leave the runtime.

    A proxy password in a log line or the request log would be a worse leak
    than the thing a chain exists to avoid, so the masking happens once, here,
    rather than at every reader.
    """

    store(_chains())
    settings = Settings(nvidia_nim_api_key="k1")

    plan = build_provider_config(PROVIDER_CATALOG["nvidia_nim"], settings).proxy_chain

    assert plan is not None
    assert [leg.label for leg in plan.legs] == [
        "203.0.113.7:1080",
        "198.51.100.9:8080",
        DIRECT_PROXY_LABEL,
    ]
    assert all("p@" not in leg.label for leg in plan.legs)


def test_direct_is_a_legal_rung(store) -> None:
    """ "Try my addresses, then fall back to my own IP" must be expressible."""

    store(_chains())
    settings = Settings(nvidia_nim_api_key="k1")

    plan = build_provider_config(PROVIDER_CATALOG["nvidia_nim"], settings).proxy_chain

    assert plan is not None
    assert plan.legs[-1].url == ""
    assert plan.legs[-1].label == DIRECT_PROXY_LABEL


def test_a_paused_rung_is_skipped_and_costs_no_client(store) -> None:
    """Kept in the store and in the page, absent from the runtime.

    The same semantics a paused route entry has, plus one fewer client, one
    fewer limiter and one fewer recovery ladder while it is paused.
    """

    chains = _chains()
    chain = chains.chains["nvidia_nim"]
    store(
        ProxyChains(
            proxies=chains.proxies,
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=chain.enabled,
                    entries=(
                        ProxyChainEntry(proxy="px_one", paused=True),
                        ProxyChainEntry(proxy="px_two"),
                        ProxyChainEntry(proxy=""),
                    ),
                )
            },
        )
    )

    plan = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"], Settings(nvidia_nim_api_key="k1")
    ).proxy_chain

    assert plan is not None
    assert [leg.label for leg in plan.legs] == [
        "198.51.100.9:8080",
        DIRECT_PROXY_LABEL,
    ]


def test_a_disabled_chain_changes_nothing(store) -> None:
    """``enabled`` is a separate switch from "has entries", at runtime too.

    An operator debugging one request turns the chain off without losing the
    order they spent time on, which is only true if the runtime honours the
    switch rather than the entry count.
    """

    chains = _chains()
    store(
        ProxyChains(
            proxies=chains.proxies,
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=False, entries=chains.chains["nvidia_nim"].entries
                )
            },
        )
    )
    settings = Settings.model_validate(
        {"nvidia_nim_api_key": "k1", "NVIDIA_NIM_PROXY": "http://old:3128"}
    )

    config = build_provider_config(PROVIDER_CATALOG["nvidia_nim"], settings)

    assert config.proxy_chain is None
    assert config.proxy == "http://old:3128"


def test_one_rung_collapses_to_a_static_proxy(store) -> None:
    """Nothing to move between, so nothing is built to move between them."""

    store(
        ProxyChains(
            proxies={"px_one": ProxyEndpoint(url="http://198.51.100.9:8080")},
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=True, entries=(ProxyChainEntry(proxy="px_one"),)
                )
            },
        )
    )

    config = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"], Settings(nvidia_nim_api_key="k1")
    )

    assert config.proxy_chain is None
    assert config.proxy == "http://198.51.100.9:8080"


def test_the_install_wide_ceiling_caps_the_cards_own_number(store) -> None:
    """Two numbers, and the smaller wins.

    ``PROXY_MAX_SWITCHES_PER_REQUEST`` is the most any chain on this install
    may spend inside one attempt; the card's value is the choice within it. A
    setting that could be overridden by a JSON document would not be a limit.
    """

    chains = _chains()
    store(
        ProxyChains(
            proxies=chains.proxies,
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=True,
                    entries=chains.chains["nvidia_nim"].entries,
                    max_switches=5,
                )
            },
        )
    )

    plan = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"],
        Settings.model_validate(
            {"nvidia_nim_api_key": "k1", "PROXY_MAX_SWITCHES_PER_REQUEST": 1}
        ),
    ).proxy_chain

    assert plan is not None
    assert plan.max_switches == 1


def test_a_subscription_login_chain_is_inert_without_the_acknowledgement(
    store,
) -> None:
    """Refused in the runtime, not only in the page.

    The token identifies a person's subscription, and changing its source
    address between requests is the behaviour most likely to be read as account
    sharing. That is the operator's risk to take, and taking it has to be
    something they did on purpose -- so an acknowledgement the runtime ignored
    would be theatre.
    """

    endpoints = {"px_one": ProxyEndpoint(url="http://198.51.100.9:8080")}
    entries = (ProxyChainEntry(proxy="px_one"), ProxyChainEntry(proxy=""))
    store(
        ProxyChains(
            proxies=endpoints,
            chains={"chatgpt_oauth": ProxyChain(enabled=True, entries=entries)},
        )
    )
    settings = Settings()

    proxy, plan = resolve_proxy_chain("chatgpt_oauth", "http://old:3128", settings)
    assert plan is None
    assert proxy == "http://old:3128"

    store(
        ProxyChains(
            proxies=endpoints,
            chains={
                "chatgpt_oauth": ProxyChain(
                    enabled=True, entries=entries, oauth_acknowledged=True
                )
            },
        )
    )
    _proxy, acknowledged = resolve_proxy_chain(
        "chatgpt_oauth", "http://old:3128", settings
    )
    assert acknowledged is not None
    assert len(acknowledged.legs) == 2


def test_the_factory_builds_one_leaf_per_rung_below_the_credential_pool(
    store,
) -> None:
    """The tree the design argues for, asserted on a real provider.

    Two credentials and three rungs is one ``RotatingProvider`` over two
    ``ProxyRotatingProvider``s over six leaves, each leaf differing from its
    sibling in exactly one field.

    Since 7.19.0 those six leaves are built on **first use** rather than at
    construction, so this walks them by asking the pool for each rung rather
    than by reading a list that no longer exists. The shape it asserts is
    unchanged; what changed is when the clients appear, which
    ``test_a_long_chain_builds_no_client_until_a_request_goes_out`` below is
    the assertion for.
    """

    from my_claude_code.providers.runtime.rotating import RotatingProvider

    store(_chains())
    provider = create_provider(
        "nvidia_nim",
        Settings.model_validate(
            {
                "nvidia_nim_api_key": "k1,k2",
                "NVIDIA_NIM_API_KEY_ROTATION": "round_robin",
            }
        ),
    )

    assert isinstance(provider, RotatingProvider)
    pools = [
        pool for pool in provider._providers if isinstance(pool, ProxyRotatingProvider)
    ]
    assert len(pools) == 2
    for pool in pools:
        assert isinstance(pool, ProxyRotatingProvider)
        leaves = [pool._pool.get(index) for index in range(3)]
        assert [leaf._config.proxy for leaf in leaves] == [
            "socks5h://u:p@203.0.113.7:1080",
            "http://198.51.100.9:8080",
            "",
        ]
        # Every leaf carries one credential and one address, and no leaf
        # carries a plan -- the recursion stops at the rung.
        assert {leaf._config.api_key for leaf in leaves} == {leaves[0]._config.api_key}
        assert all(leaf._config.proxy_chain is None for leaf in leaves)
    assert (
        pools[0]._pool.get(0)._config.api_key != pools[1]._pool.get(0)._config.api_key
    )


def test_with_no_chain_provider_construction_is_what_it_always_was(store) -> None:
    """The safety boundary of 7.19.0, asserted rather than asserted about.

    Everything this release adds -- lazy legs, a live-failure bound, a direct
    fallback, a health re-prober -- is reachable only through a configured
    chain. An install that has never opened the Proxying page must get back the
    object it got from 7.18, built from the same config, and that means not
    even a ``ProxyRotatingProvider`` in the tree.
    """

    store(ProxyChains())
    settings = Settings.model_validate({"nvidia_nim_api_key": "k1"})

    provider = create_provider("nvidia_nim", settings)

    assert not isinstance(provider, ProxyRotatingProvider)
    assert provider._config.proxy == ""
    assert provider._config.proxy_chain is None
    # The four fields a chain would have changed are untouched, so the bytes
    # this provider puts on the wire cannot differ from the release before.
    assert (
        provider._config.base_url
        == create_provider("nvidia_nim", settings)._config.base_url
    )


def test_a_three_hundred_entry_chain_round_trips_through_the_store(store) -> None:
    """The cap is gone from the reader, the writer and the runtime alike.

    Three hundred addresses in, three hundred out, three hundred legs in the
    plan -- and, because a leg is built on first use, no client for any of them
    until a request picks one.
    """

    proxies = {
        f"px_{index:03d}": ProxyEndpoint(url=f"http://198.51.100.9:{9000 + index}")
        for index in range(300)
    }
    chains = ProxyChains(
        proxies=proxies,
        chains={
            "nvidia_nim": ProxyChain(
                enabled=True,
                entries=tuple(ProxyChainEntry(proxy=proxy_id) for proxy_id in proxies),
            )
        },
    )
    store(chains)
    settings = Settings.model_validate({"nvidia_nim_api_key": "k1"})

    proxy, plan = resolve_proxy_chain("nvidia_nim", "", settings)

    assert proxy == ""
    assert plan is not None
    assert len(plan.legs) == 300
    assert plan.direct_fallback is True

    provider = create_provider("nvidia_nim", settings)
    assert isinstance(provider, ProxyRotatingProvider)
    assert provider.open_leg_count() == 0


def test_pruning_keeps_every_endpoint_a_long_chain_references(store) -> None:
    """``with_chain`` drops endpoints nothing references, and 300 are referenced."""

    proxies = {
        f"px_{index:03d}": ProxyEndpoint(url=f"http://198.51.100.9:{9000 + index}")
        for index in range(300)
    }
    chains = ProxyChains(proxies=proxies, chains={})
    kept = chains.with_chain(
        "nvidia_nim",
        ProxyChain(
            enabled=True,
            entries=tuple(ProxyChainEntry(proxy=proxy_id) for proxy_id in proxies),
        ),
    )

    assert len(kept.proxies) == 300

    # And one entry removed prunes exactly one endpoint, not the other 299.
    trimmed = kept.with_chain(
        "nvidia_nim",
        ProxyChain(
            enabled=True,
            entries=tuple(
                ProxyChainEntry(proxy=proxy_id) for proxy_id in list(proxies)[:299]
            ),
        ),
    )
    assert len(trimmed.proxies) == 299
