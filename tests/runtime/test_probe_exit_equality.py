"""A provider with no chain probes exactly as it did before 7.78.7.

The masking fix routes the Providers card's probes through a provider's proxy
chain. For every provider without one -- every provider on a fresh install --
nothing may move: the same requests, byte for byte, dialled to the same place
(no proxy, the static ``<PROVIDER>_PROXY``, or the custom registry entry's
proxy), and the same facts learned from the same answers.

Each scenario runs the card's two buttons through the real
``ApplicationRuntime`` against the masking rig, then calls the unchanged probe
modules directly with the arguments the runtime passed them before the fix
(the static proxy, read the way ``_probe_target`` always read it), and
compares: the ``proxy`` every ``httpx.AsyncClient`` was built with, where each
connection arrived from, and the bytes of every request. Only the
``x-opencode-request`` header differs between two runs of the same probe -- a
fresh id per call, by design -- and it is masked before comparing.

This file deliberately imports nothing the fix added, so the same file runs
unchanged against the release before it.
"""

import dataclasses
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

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
from my_claude_code.core.proxy_rotation import reset_proxy_health
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_REQUEST_HEADER,
    OPENCODE_SESSION_HEADER,
)
from my_claude_code.providers.recovery import (
    FACT_CLIENT_IDENTITY_REQUIRED,
    FACT_OUTPUT_CAP,
    FACT_VISION_UNSUPPORTED,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_PROBE,
    learned_fact_store,
)
from my_claude_code.providers.runtime.capability_probes import (
    ALL_PROBES,
    DEFAULT_PROBES,
    probe_model_capabilities,
)
from my_claude_code.providers.runtime.identity_probe import probe_client_identity
from my_claude_code.providers.runtime.opencode_credentials import probe_credential
from my_claude_code.providers.runtime.reasoning_probe import probe_reasoning_dialect
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.support.masking_harness import (
    FAKE_PROVIDER_HOST,
    MaskingRig,
    SeenRequest,
    start_masking_rig,
)

pytestmark = pytest.mark.local_serial

MODEL = "rig-model"
ZEN = "opencode"
ZEN_KEY = "sk-rigzen-0000aaaa1111bbbb"
CUSTOM_KEY = "sk-rigmask-0000aaaa1111bbbb"


def _answers(request: SeenRequest) -> tuple[int, bytes]:
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


@dataclass(frozen=True)
class Scenario:
    name: str
    provider: str  # "custom" or "zen"
    #: Which rig proxy is the static one: ``None``, ``"http"`` or ``"socks"``.
    static: str | None
    #: ``"none"`` (empty store), ``"off"`` (this provider's chain switched off)
    #: or ``"other"`` (a chain exists, for a different provider).
    chains: str


SCENARIOS = (
    Scenario("custom_no_proxy", "custom", None, "none"),
    Scenario("custom_registry_proxy", "custom", "http", "none"),
    Scenario("custom_chain_switched_off", "custom", "http", "off"),
    Scenario("zen_no_proxy", "zen", None, "none"),
    Scenario("zen_static_proxy", "zen", "socks", "none"),
    Scenario("zen_static_proxy_other_provider_chained", "zen", "socks", "other"),
)


@dataclass
class Bench:
    rig: MaskingRig
    proxies_built: list[Any]
    chains_path: Any
    monkeypatch: Any


@pytest.fixture
def bench(tmp_path, monkeypatch) -> Iterator[Bench]:
    rig = start_masking_rig(_answers)
    chains_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: chains_path
    )
    reset_proxy_chains_cache()
    reset_proxy_health()

    built: list[Any] = []
    real_client = httpx.AsyncClient

    class RecordingClient(real_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append(kwargs.get("proxy", "<absent>"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
    try:
        yield Bench(rig, built, chains_path, monkeypatch)
    finally:
        rig.close()
        reset_proxy_chains_cache()
        reset_proxy_health()


def _static_url(rig: MaskingRig, which: str | None) -> str | None:
    if which is None:
        return None
    return rig.proxies[0 if which == "socks" else 1].url


def _store(bench: Bench, scenario: Scenario, provider_id: str) -> None:
    proxies = {"px_0": ProxyEndpoint(url=bench.rig.proxies[2].url)}
    chain = ProxyChain(
        enabled=scenario.chains != "off",
        entries=(ProxyChainEntry(proxy="px_0"), ProxyChainEntry(proxy="")),
        direct_fallback=False,
    )
    if scenario.chains == "none":
        document = ProxyChains()
    elif scenario.chains == "off":
        document = ProxyChains(proxies=proxies, chains={provider_id: chain})
    else:
        document = ProxyChains(proxies=proxies, chains={"nvidia_nim": chain})
    save_proxy_chains(document, bench.chains_path)


def _runtime(bench: Bench, scenario: Scenario, static: str | None):
    registry = get_provider_registry()
    if scenario.provider == "custom":
        entry = registry.add(
            display_name="Mask Co",
            base_url=bench.rig.host.base_url(),
            api_keys=(CUSTOM_KEY,),
            proxy=static or "",
        )
        provider_id = entry.provider_id
        settings = Settings.model_validate({})
    else:
        provider_id = ZEN
        descriptors = dict(registry.all_descriptors())
        descriptors[ZEN] = dataclasses.replace(
            PROVIDER_CATALOG[ZEN], default_base_url=bench.rig.host.base_url()
        )
        bench.monkeypatch.setattr(registry, "all_descriptors", lambda: descriptors)
        values = {"OPENCODE_API_KEY": ZEN_KEY}
        if static:
            values["OPENCODE_PROXY"] = static
        settings = Settings.model_validate(values)
    runtime = ApplicationRuntime(ProviderRuntimeManager(settings), transcriber=None)
    bench.monkeypatch.setattr(
        runtime, "cached_model_ids", lambda: {provider_id: frozenset({MODEL})}
    )
    bench.monkeypatch.setattr(
        runtime.provider_manager, "replace", AsyncMock(return_value=1)
    )
    return runtime, provider_id, settings


def _wire(requests: list[SeenRequest]) -> list[tuple[str, str, bytes, bytes]]:
    """Every request as bytes, with the one per-call random id masked."""

    masked: list[tuple[str, str, bytes, bytes]] = []
    for seen in requests:
        lines = []
        for line in seen.head.split(b"\r\n"):
            name = line.partition(b":")[0].strip().lower()
            if name == OPENCODE_REQUEST_HEADER.encode():
                line = name + b": <per-call id>"
            lines.append(line)
        masked.append((seen.method, seen.path, b"\r\n".join(lines), seen.body))
    return masked


def _facts() -> list[tuple[Any, ...]]:
    return sorted(
        (fact.provider_id, fact.model_id, fact.fact_kind, repr(fact.value), fact.source)
        for fact in learned_fact_store().all_facts()
    )


def _arrivals(rig: MaskingRig) -> list[str]:
    """Where each connection came from: ``direct`` or the proxy's url."""

    by_socket = {
        address: proxy.url for proxy in rig.proxies for address in proxy.outbound
    }
    return [by_socket.get(peer, "direct") for peer in rig.host.peers]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_without_a_chain_the_probes_are_unchanged(
    bench: Bench, scenario: Scenario
) -> None:
    rig = bench.rig
    static = _static_url(rig, scenario.static)
    runtime, provider_id, settings = _runtime(bench, scenario, static)
    _store(bench, scenario, provider_id)

    # --- what the card's buttons send now -------------------------------
    payload = await runtime.probe_provider_capabilities(provider_id)
    dialect = (
        await runtime.probe_custom_provider_dialect(provider_id)
        if scenario.provider == "custom"
        else None
    )
    now_wire = _wire(rig.host.requests)
    now_arrivals = _arrivals(rig)
    now_proxies = list(bench.proxies_built)
    now_facts = _facts()

    # --- what the same buttons sent before: the modules, the old arguments
    rig.clear()
    bench.proxies_built.clear()
    base_url = rig.host.base_url()
    probes = ALL_PROBES if settings.model_probe_new_models else DEFAULT_PROBES
    key = CUSTOM_KEY if scenario.provider == "custom" else ZEN_KEY
    await probe_model_capabilities(
        base_url,
        probe_credential(provider_id, MODEL, key),
        MODEL,
        probes=probes,
        proxy=static,
    )
    await probe_client_identity(provider_id, base_url, key, MODEL, proxy=static)
    if scenario.provider == "custom":
        await probe_reasoning_dialect(base_url, key, MODEL, proxy=static)
    then_wire = _wire(rig.host.requests)
    then_arrivals = _arrivals(rig)
    then_proxies = list(bench.proxies_built)

    assert now_wire == then_wire
    assert now_proxies == then_proxies
    assert set(now_proxies) == {static}
    assert now_arrivals == then_arrivals
    assert set(now_arrivals) == {static or "direct"}
    assert all(
        target[0] == FAKE_PROVIDER_HOST for p in rig.proxies for target in p.targets
    )

    # --- and the same facts from the same answers -------------------------
    expected = [
        (provider_id, MODEL, FACT_OUTPUT_CAP, "8192", SOURCE_PROBE),
        (provider_id, MODEL, FACT_VISION_UNSUPPORTED, "True", SOURCE_PROBE),
    ]
    if scenario.provider == "zen":
        expected.append(
            (
                provider_id,
                PROVIDER_WIDE_MODEL_ID,
                FACT_CLIENT_IDENTITY_REQUIRED,
                "True",
                SOURCE_PROBE,
            )
        )
    assert now_facts == sorted(expected)
    assert payload["status"] == "probed"
    if dialect is not None:
        assert dialect["status"] == "learned"
        entry = get_provider_registry().get(provider_id)
        assert entry is not None
        assert tuple(entry.reasoning_effort_enum or ()) == ("low", "medium", "high")
        assert entry.reasoning_probe_status == "learned"
