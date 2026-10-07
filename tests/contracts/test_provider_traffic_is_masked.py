"""A provider reached through a proxy chain never sees this computer's address.

The leak contract of ``specs/PR-EXIT-ROTATION-AND-REPEAT-MODELS-SPEC.md`` C.4,
and the grid every later masking fix fills in. One row per kind of traffic MCC
sends to a provider host; each row drives its traffic against the rig in
``tests/support/masking_harness.py`` -- a fake provider host that records the
peer of every connection, two SOCKS5 proxies and an HTTP proxy that record the
onward socket of every tunnel, and a guard that fails any local lookup of the
provider's hostname -- and asserts where it came from.

The rows that are filled are the three the Providers card's buttons send: the
capability probe, the client-identity probe that rides on it, and a custom
provider's reasoning-dialect probe (leak C-1 -- until 7.78.7 they went out
from this computer whenever the provider was proxied only by a chain). Every
other traffic class is a ``skip`` placeholder naming the change that drives
it, so the grid says what is covered and what is not instead of implying the
whole provider surface is.

Four questions per row:

* chain healthy, Direct fallback off: everything through the first exit;
* first exit unreachable: through the next one, never the dead one;
* every exit unhealthy, Direct fallback off: nothing sent, and the answer
  names the setting;
* every exit unhealthy, Direct fallback on: this computer's address, which is
  the one case the user allowed it (2026-10-06 23:03).
"""

import dataclasses
import json
import socket
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
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
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_SESSION_HEADER,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.support.masking_harness import MaskingRig, SeenRequest, start_masking_rig

#: The custom provider every custom-card row probes.
CUSTOM_NAME = "Mask Co"
#: The built-in provider whose profile declares a client identity -- the only
#: kind the identity probe runs for. Its base URL is a catalogue constant, so
#: the row points the descriptor at the rig rather than at the real host.
IDENTITY_PROVIDER = "opencode"
MODEL = "rig-model"
#: The body of both identity-probe requests (``identity_probe._probe_body``).
_PLAIN_BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16,
    "stream": False,
}


def _rig_answers(request: SeenRequest) -> tuple[int, bytes]:
    """What the fake host says, chosen so every probe learns something."""

    body = json.loads(request.body or b"{}")
    content = (body.get("messages") or [{}])[0].get("content")
    if body.get("max_tokens") == 2_000_000_000:
        message = "max_tokens must be less than or equal to 8192"
    elif isinstance(content, list):
        message = "this model does not support image input"
    elif body.get("reasoning_effort") == "bogus_value":
        message = "reasoning_effort must be one of: low, medium, high"
    elif OPENCODE_SESSION_HEADER not in request.headers:
        message = f"missing {OPENCODE_SESSION_HEADER} header"
    else:
        return 200, b'{"id":"rig","object":"chat.completion","choices":[]}'
    return 400, json.dumps({"error": {"message": message}}).encode()


@dataclass
class ProbeWorld:
    """One runtime, one custom provider and the rig, wired together."""

    rig: MaskingRig
    runtime: ApplicationRuntime
    custom_id: str
    write_chains: Callable[[ProxyChains], None]

    def chain_everything(
        self, *, direct_fallback: bool, policy: str = "failover"
    ) -> None:
        """Give both probed providers the rig's three proxies, in order."""

        proxies = {
            f"px_{index}": ProxyEndpoint(url=url)
            for index, url in enumerate(self.rig.proxy_urls)
        }
        chain = ProxyChain(
            enabled=True,
            policy=policy,
            entries=tuple(ProxyChainEntry(proxy=key) for key in proxies),
            direct_fallback=direct_fallback,
        )
        self.write_chains(
            ProxyChains(
                proxies=proxies,
                chains={self.custom_id: chain, IDENTITY_PROVIDER: chain},
            )
        )

    def mark_unreachable(self, *indexes: int) -> None:
        """Put entries on the reachability ladder, as a failed dial would."""

        for index in indexes:
            PROXY_REACHABILITY.note_failure(
                mask_proxy_label(self.rig.proxy_urls[index]), "rig: refused"
            )


@pytest.fixture
def world(tmp_path, monkeypatch) -> Iterator[ProbeWorld]:
    rig = start_masking_rig(_rig_answers)
    monkeypatch.setattr(socket, "getaddrinfo", rig.dns.getaddrinfo)
    chains_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: chains_path
    )
    reset_proxy_chains_cache()
    reset_proxy_health()

    registry = get_provider_registry()
    entry = registry.add(
        display_name=CUSTOM_NAME,
        base_url=rig.host.base_url(),
        api_keys=("sk-rigmask-0000aaaa1111bbbb",),
    )
    descriptors = dict(registry.all_descriptors())
    descriptors[IDENTITY_PROVIDER] = dataclasses.replace(
        PROVIDER_CATALOG[IDENTITY_PROVIDER], default_base_url=rig.host.base_url()
    )
    monkeypatch.setattr(registry, "all_descriptors", lambda: descriptors)

    settings = Settings.model_validate(
        {"OPENCODE_API_KEY": "sk-rigzen-0000aaaa1111bbbb"}
    )
    runtime = ApplicationRuntime(ProviderRuntimeManager(settings), transcriber=None)
    monkeypatch.setattr(
        runtime,
        "cached_model_ids",
        lambda: {entry.provider_id: frozenset({MODEL})},
    )
    # The dialect probe republishes the generation so the next request spells
    # the new word; there is no generation worth building in this test.
    monkeypatch.setattr(runtime.provider_manager, "replace", AsyncMock(return_value=1))
    try:
        yield ProbeWorld(
            rig=rig,
            runtime=runtime,
            custom_id=entry.provider_id,
            write_chains=lambda chains: save_proxy_chains(chains, chains_path),
        )
    finally:
        rig.close()
        reset_proxy_chains_cache()
        reset_proxy_health()


# ------------------------------------------------------------------ drivers


async def _capability_probe(world: ProbeWorld) -> dict[str, Any]:
    return await world.runtime.probe_provider_capabilities(world.custom_id)


async def _identity_probe(world: ProbeWorld) -> dict[str, Any]:
    payload = await world.runtime.probe_provider_capabilities(
        IDENTITY_PROVIDER, (MODEL,)
    )
    if payload.get("status") != "not_sent":
        # The identity probe is the two plain requests, with and without the
        # session header; every capability probe before them adds a field.
        plain = [
            seen
            for seen in world.rig.host.requests
            if json.loads(seen.body) == _PLAIN_BODY
        ]
        assert len(plain) == 2, [seen.headers for seen in plain]
        assert [OPENCODE_SESSION_HEADER in seen.headers for seen in plain] == [
            True,
            False,
        ]
    return payload


async def _dialect_probe(world: ProbeWorld) -> dict[str, Any]:
    return await world.runtime.probe_custom_provider_dialect(world.custom_id)


Driver = Callable[[ProbeWorld], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class TrafficClass:
    name: str
    #: The change that drives this row: ``PR-2`` for the filled rows, else the
    #: one that will (the PR split in the spec).
    filled_by: str
    driver: Driver | None = None


TRAFFIC: tuple[TrafficClass, ...] = (
    TrafficClass("probe_capabilities", "PR-2", _capability_probe),
    TrafficClass("identity_probe", "PR-2", _identity_probe),
    TrafficClass("dialect_probe", "PR-2", _dialect_probe),
    TrafficClass("chat_completions", "PR-3"),
    TrafficClass("responses", "PR-3"),
    TrafficClass("anthropic_messages", "PR-3"),
    TrafficClass("same_exit_retry", "PR-3"),
    TrafficClass("fallback_to_a_model_of_the_same_provider", "PR-3"),
    TrafficClass("discovery_sweep", "PR-3"),
    TrafficClass("test_button", "PR-3"),
    TrafficClass("credential_health_probe", "PR-3"),
    TrafficClass("media_image_with_result_download", "PR-3"),
    TrafficClass("oauth_token_refresh_claude", "PR-5"),
    TrafficClass("oauth_token_refresh_chatgpt", "PR-5"),
    TrafficClass("vertex_token_refresh_socks5", "PR-5"),
)


def _rows() -> list[Any]:
    return [
        pytest.param(
            row,
            id=row.name,
            marks=()
            if row.driver is not None
            else pytest.mark.skip(
                reason=f"placeholder: {row.name} is driven from {row.filled_by}"
            ),
        )
        for row in TRAFFIC
    ]


def _drive(row: TrafficClass) -> Driver:
    assert row.driver is not None
    return row.driver


# -------------------------------------------------------------------- rows


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
async def test_a_chained_provider_is_reached_only_through_its_proxies(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=False)

    payload = await _drive(row)(world)

    world.rig.assert_masked()
    first, *others = world.rig.proxies
    assert first.targets, "the first exit in the chain's order carried nothing"
    assert all(not proxy.targets for proxy in others)
    assert payload["proxy_exit"] == mask_proxy_label(first.url)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
async def test_an_unreachable_exit_is_skipped_for_the_next_one(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=False)
    world.mark_unreachable(0)

    await _drive(row)(world)

    world.rig.assert_masked()
    dead, second, third = world.rig.proxies
    assert dead.accepted == 0
    assert second.targets
    assert not third.targets


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
async def test_no_usable_exit_and_direct_fallback_off_sends_nothing(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=False)
    world.mark_unreachable(0, 1, 2)

    payload = await _drive(row)(world)

    world.rig.assert_nothing_sent()
    assert payload["status"] == "not_sent"
    assert "Direct fallback is off" in payload["detail"]
    assert "Proxying page" in payload["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
async def test_no_usable_exit_and_direct_fallback_on_goes_direct(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=True)
    world.mark_unreachable(0, 1, 2)
    world.rig.dns.mode = "answer"

    payload = await _drive(row)(world)

    assert world.rig.host.peers
    assert world.rig.direct_peers() == world.rig.host.peers
    assert all(proxy.accepted == 0 for proxy in world.rig.proxies)
    assert payload["proxy_exit"] == "direct"


def test_every_row_names_who_fills_it() -> None:
    """The grid is the plan: a placeholder says which change will drive it."""

    names = [row.name for row in TRAFFIC]
    assert len(names) == len(set(names))
    for row in TRAFFIC:
        assert row.filled_by.startswith("PR-")
        assert (row.driver is not None) == (row.filled_by == "PR-2")
