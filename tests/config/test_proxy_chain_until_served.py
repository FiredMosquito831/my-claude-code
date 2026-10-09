"""The "Keep trying exits until one answers" switch in the store and the factory.

Equality proofs 3 and 4 of ``specs/PR-EXIT-ROTATION-AND-REPEAT-MODELS-SPEC.md``
A.6, and the hops a ticked chain takes from the file to its legs:

* a stored chain without the key loads and saves **byte-identical**, and reads
  ``until_served=False`` -- so every chain saved before 7.81.0 rotates exactly
  as it did;
* the key is written only when it is True;
* a leg of a chain without it is built from the same lines as before -- same
  limiter class, no stops armed, the operator's early-retry count;
* a leg of a ticked chain gets the two stops and no early-retry ladder, and its
  rotation state the memory's credential identity -- a fingerprint, never the
  key.
"""

import dataclasses
import json

import pytest

from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    load_proxy_chains,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.config import build_provider_config
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.proxy_leg import ProxiedLegRateLimiter
from my_claude_code.providers.runtime.proxy_rotating import ProxyRotatingProvider

#: A chain document as 7.80.0 writes it: no ``until_served`` key anywhere.
STORED_BEFORE = {
    "version": 1,
    "proxies": {
        "px_one": {
            "url": "socks5h://u:p@203.0.113.7:1080",
            "label": "",
            "added_at": "2026-10-01T10:00:00Z",
            "source": "manual",
            "source_count": 1,
        },
        "px_two": {
            "url": "http://198.51.100.9:8080",
            "label": "office",
            "added_at": "2026-10-01T10:00:00Z",
            "source": "manual",
            "source_count": 1,
        },
    },
    "chains": {
        "nvidia_nim": {
            "enabled": True,
            "policy": "failover",
            "entries": [
                {"proxy": "px_one", "paused": False},
                {"proxy": "px_two", "paused": True},
                {"proxy": "", "paused": False},
            ],
            "on": ["quota", "rate_limit", "timeout"],
            "scope": "provider",
            "max_switches": 2,
            "direct_fallback": True,
            "oauth_acknowledged": False,
            "order_by_speed": True,
            "order_sorted_at": "2026-10-02T11:00:00Z",
        },
        "open_router": {
            "enabled": False,
            "policy": "round_robin",
            "entries": [{"proxy": "px_two", "paused": False}],
            "on": ["rate_limit"],
            "scope": "credential",
            "max_switches": 3,
            "direct_fallback": False,
            "oauth_acknowledged": False,
        },
    },
}


@pytest.fixture
def path(tmp_path, monkeypatch):
    target = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: target
    )
    reset_proxy_chains_cache()
    yield target
    reset_proxy_chains_cache()


def test_a_chain_stored_before_the_key_round_trips_byte_for_byte(path) -> None:
    """Equality proof 3."""

    save_proxy_chains(ProxyChains.from_document(STORED_BEFORE), path)
    first = path.read_bytes()

    loaded = load_proxy_chains(path)
    assert all(not chain.until_served for chain in loaded.chains.values())
    save_proxy_chains(loaded, path)

    assert path.read_bytes() == first
    assert b"until_served" not in first
    assert json.loads(first)["chains"] == STORED_BEFORE["chains"]


def test_the_key_is_written_only_when_ticked_and_read_back(path) -> None:
    chain = ProxyChains.from_document(STORED_BEFORE).chains["nvidia_nim"]
    ticked = dataclasses.replace(chain, until_served=True)

    assert ticked.as_document()["until_served"] is True
    assert "until_served" not in chain.as_document()
    assert ProxyChain.from_document(ticked.as_document(), "x") == ticked
    # Only an explicit true reads as ticked.
    for raw in ("true", 1, None, False):
        document = chain.as_document() | {"until_served": raw}
        parsed = ProxyChain.from_document(document, "x")
        assert parsed is not None and parsed.until_served is False


def _store(path, **chain_kwargs) -> None:
    save_proxy_chains(
        ProxyChains(
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
                    ),
                    **chain_kwargs,
                )
            },
        ),
        path,
    )
    reset_proxy_chains_cache()


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "nvidia_nim_api_key": "nvapi-test-key-0123456789",
            "STREAM_EARLY_RETRY_ATTEMPTS": "4",
        }
    )


def test_the_plan_carries_the_switch_absent_reads_false(path) -> None:
    _store(path)
    plan = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"], _settings()
    ).proxy_chain
    assert plan is not None and plan.until_served is False

    _store(path, until_served=True, max_switches=3)
    plan = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"], _settings()
    ).proxy_chain
    assert plan is not None and plan.until_served is True
    # The existing bound, unchanged: the smaller of the card and the global.
    assert plan.max_switches == min(3, int(_settings().proxy_max_switches_per_request))


def _legs(provider: ProxyRotatingProvider) -> list:
    return [provider._pool.get(index) for index in range(2)]


def test_an_unticked_chain_s_legs_are_built_as_before(path) -> None:
    """Equality proof 4: same limiter class, nothing armed, the operator's
    early-retry count."""

    _store(path)
    provider = create_provider("nvidia_nim", _settings())
    assert isinstance(provider, ProxyRotatingProvider)

    for leg in _legs(provider):
        limiter = leg._rate_limiter
        assert type(limiter) is ProxiedLegRateLimiter
        assert limiter.stop_on_transport is False
        assert limiter.stop_on_rate_limit is False
        assert leg._config.early_retry_attempts == 4
    assert provider._state.until_served is False


def test_a_ticked_chain_s_legs_stop_and_skip_the_early_retry_ladder(path) -> None:
    _store(path, until_served=True)
    provider = create_provider("nvidia_nim", _settings())
    assert isinstance(provider, ProxyRotatingProvider)

    for leg in _legs(provider):
        limiter = leg._rate_limiter
        assert type(limiter) is ProxiedLegRateLimiter
        assert limiter.stop_on_transport is True
        assert limiter.stop_on_rate_limit is True
        assert leg._config.early_retry_attempts == 1
    state = provider._state
    assert state.until_served is True
    assert state._credential.startswith("sha256:")
    assert "nvapi-test-key" not in state._credential
    assert state._credential_label == "nvap…6789"


def test_a_ticked_chain_without_the_rate_limit_chip_keeps_429_retries(path) -> None:
    _store(path, until_served=True, on=("timeout",))
    provider = create_provider("nvidia_nim", _settings())
    assert isinstance(provider, ProxyRotatingProvider)

    limiter = _legs(provider)[0]._rate_limiter
    assert isinstance(limiter, ProxiedLegRateLimiter)
    assert limiter.stop_on_transport is True
    assert limiter.stop_on_rate_limit is False
