"""Which legs the factory builds, and what each says in the request log (7.79.2).

C-9 (b) and (c) of the masking spec, and the PR-4 wiring:

* a provider with **no proxy** is built exactly as before -- its leaf, nothing
  around it -- so nothing about it can have moved;
* a provider with **one fixed proxy** -- a custom provider's stored proxy, a
  static ``<PROVIDER>_PROXY``, a chain with one usable entry -- is its leaf
  inside an :class:`AttributedLeg` that names that address before every dial
  (the request log said nothing at all for these before);
* a chain of two or more builds, at the fallback's index, the gated
  :class:`DirectFallbackLeg`, and for a Direct entry the operator wrote, an
  :class:`AttributedLeg` that says when the system proxy carried it.

The bytes a provider sends are its leaf's own: the contract test
``tests/contracts/test_provider_traffic_is_masked.py`` proves where they went.
"""

from collections.abc import Iterator

import pytest

from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    DIRECT_PROXY_LABEL,
    install_proxy_attribution,
    record_proxy,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    amend_proxy_dial,
    current_ladder,
    install_ladder_trace,
    record_proxy_dial,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.openai_chat.provider import OpenAIChatProvider
from my_claude_code.providers.runtime.direct_leg import (
    AttributedLeg,
    DirectFallbackLeg,
)
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.proxy_rotating import ProxyRotatingProvider
from tests.providers.test_credential_rotation import _FakeProvider, _request

KEY = "sk-legs-0000aaaa1111bbbb"
PROXY_A = "socks5h://alice:hunter2@203.0.113.7:1080"
PROXY_B = "http://198.51.100.9:8080"


@pytest.fixture
def chains(tmp_path, monkeypatch) -> Iterator:
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    monkeypatch.setattr("my_claude_code.config.system_proxy.getproxies", dict)
    reset_proxy_chains_cache()
    _CURRENT.set(None)
    _LADDER.set(None)

    def write(provider_id: str, *entries: ProxyChainEntry, **kwargs) -> None:
        save_proxy_chains(
            ProxyChains(
                proxies={
                    "px_a": ProxyEndpoint(url=PROXY_A, label="Office SOCKS"),
                    "px_b": ProxyEndpoint(url=PROXY_B),
                },
                chains={
                    provider_id: ProxyChain(enabled=True, entries=entries, **kwargs)
                },
            ),
            path,
        )
        reset_proxy_chains_cache()

    yield write
    reset_proxy_chains_cache()
    _CURRENT.set(None)
    _LADDER.set(None)


def _custom(name: str, *, proxy: str = "") -> str:
    entry = get_provider_registry().add(
        display_name=name,
        base_url="https://legs.test/v1",
        api_keys=(KEY,),
        proxy=proxy or None,
    )
    return entry.provider_id


def _build(provider_id: str) -> BaseProvider:
    return create_provider(provider_id, Settings())


def test_no_proxy_builds_the_bare_leaf_exactly_as_before(chains) -> None:
    provider = _build(_custom("Bare Co"))

    assert type(provider) is OpenAIChatProvider


def test_a_stored_proxy_is_named_before_every_dial(chains) -> None:
    provider = _build(_custom("Static Co", proxy=PROXY_B))

    assert isinstance(provider, AttributedLeg)
    assert isinstance(provider.leaf, OpenAIChatProvider)
    assert provider.leaf._config.proxy == PROXY_B
    install_proxy_attribution(on_dial=record_proxy_dial)
    install_ladder_trace()
    provider._attribute()
    provider._attribute()
    slot = _CURRENT.get()
    assert slot is not None and slot.label == "198.51.100.9:8080"
    ladder = current_ladder()
    assert ladder is not None
    assert [dial.proxy for dial in ladder.slot().dials] == ["198.51.100.9:8080"] * 2


def test_a_one_entry_chain_is_named_by_its_entry(chains) -> None:
    """The entry's own name, as the Proxying page and the speed ledger know it."""

    provider_id = _custom("One Co")
    chains(
        provider_id,
        ProxyChainEntry(proxy="px_a"),
        ProxyChainEntry(proxy="px_b", paused=True),
    )

    provider = _build(provider_id)

    assert isinstance(provider, AttributedLeg)
    assert provider.leaf._config.proxy == PROXY_A
    install_proxy_attribution()
    provider._attribute()
    slot = _CURRENT.get()
    assert slot is not None and slot.label == "Office SOCKS"
    assert "hunter2" not in str(slot.label)


def test_a_one_entry_direct_chain_says_direct(chains) -> None:
    provider_id = _custom("Direct Co")
    chains(provider_id, ProxyChainEntry(proxy=""))

    provider = _build(provider_id)

    assert isinstance(provider, AttributedLeg)
    assert provider.leaf._config.proxy == ""
    install_proxy_attribution()
    provider._attribute()
    slot = _CURRENT.get()
    assert slot is not None and slot.label == DIRECT_PROXY_LABEL


def test_a_chain_builds_the_gated_fallback_and_an_attributed_direct_rung(
    chains,
) -> None:
    provider_id = _custom("Chain Co")
    chains(
        provider_id,
        ProxyChainEntry(proxy="px_a"),
        ProxyChainEntry(proxy=""),
        ProxyChainEntry(proxy="px_b"),
    )

    provider = _build(provider_id)

    assert isinstance(provider, ProxyRotatingProvider)
    pool = provider._pool
    assert isinstance(pool.get(0), OpenAIChatProvider)  # a proxied leg: bare
    direct_rung = pool.get(1)
    assert isinstance(direct_rung, AttributedLeg)
    assert direct_rung.leaf._config.proxy == ""
    fallback = pool.get(3)
    assert isinstance(fallback, DirectFallbackLeg)
    # Built lazily: no client exists for the fallback until it is allowed.
    assert fallback._leaf is None


def test_the_attributed_leg_is_its_leaf_for_everything_else(chains) -> None:
    leaf = _FakeProvider(chunks=("a", "b"))
    # Something only this provider class has, reached past BaseProvider.
    leaf.__dict__["extra"] = "provider-specific"
    leg = AttributedLeg(leaf, label="203.0.113.7:1080")

    assert leg.credential_label == leaf.credential_label
    assert leg.throttle_remaining() == 0.0
    assert leg.extra == "provider-specific"
    with pytest.raises(AttributeError):
        _ = leg.no_such_thing
    with pytest.raises(AttributeError):
        _ = leg._private_of_the_leaf


@pytest.mark.asyncio
async def test_the_attributed_leg_streams_the_leaf_s_own_chunks(chains) -> None:
    install_proxy_attribution()
    leaf = _FakeProvider(chunks=("a", "b"))
    leg = AttributedLeg(leaf, label="203.0.113.7:1080")

    chunks = [chunk async for chunk in leg.stream_response(_request())]

    assert chunks == ["a", "b"]
    assert leaf.calls == 1
    slot = _CURRENT.get()
    assert slot is not None and slot.label == "203.0.113.7:1080"


def test_a_direct_rung_through_the_system_proxy_is_relabelled(
    chains, monkeypatch
) -> None:
    monkeypatch.setattr(
        "my_claude_code.config.system_proxy.getproxies",
        lambda: {"https": "http://corp-proxy.test:3128"},
    )
    install_proxy_attribution(on_dial=record_proxy_dial, on_amend=amend_proxy_dial)
    install_ladder_trace()
    leaf = _FakeProvider()
    leaf.__dict__["_config"] = ProviderConfig(
        api_key="k", base_url="https://legs.test/v1"
    )
    leg = AttributedLeg(leaf, direct=True)

    # What the frozen pool does before it dials its Direct rung, then the leg.
    record_proxy(DIRECT_PROXY_LABEL)
    leg._attribute()

    slot = _CURRENT.get()
    assert slot is not None
    assert slot.label == "direct via system proxy corp-proxy.test:3128"
    ladder = current_ladder()
    assert ladder is not None
    assert [dial.proxy for dial in ladder.slot().dials] == [slot.label]
