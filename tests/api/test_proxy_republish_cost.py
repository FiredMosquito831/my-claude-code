"""What a chain edit costs the event loop, and how often a bulk add pays it.

Until 7.27.0 every chain write -- a save, a removal, an undo, and *each ten-address
batch* of a bulk add -- ended in ``reload_providers("proxy_chains")`` with no
arguments, which took ``ProviderRuntimeManager.replace``'s default and fired a
blanket background sweep of **every** configured provider's ``/models``. The
2026-09-11 pause work measured that sweep at 1.6-9.7 s on the reporting machine,
and a 300-address add fired it thirty times.

Two things had to stay true while that went away, and both are asserted here:

* the edited provider's generation is still replaced, synchronously, before the
  save answers -- otherwise the chain is stored and ignored until a restart;
* nothing else about any other provider moves.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, runtime_for_app

FIRST_URL = "http://198.51.100.9:8080"
SECOND_URL = "http://203.0.113.11:3128"


@pytest.fixture(autouse=True)
def _isolate_chain_store(monkeypatch, tmp_path: Path):
    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    yield path
    proxy_chains.reset_proxy_chains_cache()


def _settings() -> Settings:
    """Three providers with credentials, so "every provider" means something."""

    return Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "open_router_api_key": "or-key",
            "groq_api_key": "groq-key",
        }
    )


def _app_and_client() -> tuple[Any, TestClient]:
    app = create_test_app(_settings())
    return app, TestClient(app, client=("127.0.0.1", 50000))


def _count_sweeps(monkeypatch) -> list[int]:
    """Every blanket ``/models`` sweep this process schedules, as it is scheduled."""

    from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

    scheduled: list[int] = []
    original = ProviderRuntimeManager._refresh_generation_in_background

    async def spy(self, generation, *, only_missing: bool):
        scheduled.append(generation.generation_id)
        return await original(self, generation, only_missing=only_missing)

    monkeypatch.setattr(
        ProviderRuntimeManager, "_refresh_generation_in_background", spy
    )
    return scheduled


def _save_chain(client: TestClient, url: str) -> None:
    response = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "single",
            "scope": "provider",
            "max_switches": 1,
            "on": ["quota"],
            "entries": [{"url": url}],
        },
    )
    assert response.status_code == 200, response.text


def test_a_chain_save_does_not_sweep_every_provider(monkeypatch) -> None:
    scheduled = _count_sweeps(monkeypatch)
    _, client = _app_and_client()

    _save_chain(client, FIRST_URL)

    assert scheduled == [], (
        "a chain edit scheduled a blanket /models sweep; that sweep is the "
        "1.6-9.7 s of held event loop this release removes"
    )


def test_a_chain_save_still_republishes_the_generation(monkeypatch) -> None:
    """The half that must NOT change: the new chain routes immediately."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    before = manager.current_generation_id

    _save_chain(client, FIRST_URL)

    assert manager.current_generation_id > before


def test_a_request_after_a_chain_save_goes_out_through_the_new_entries(
    monkeypatch,
) -> None:
    """The equality contract, asked of the object a request would actually use."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager

    _save_chain(client, FIRST_URL)
    first = manager._current.runtime.resolve_provider("nvidia_nim")
    # One rung collapses to a static proxy rather than a pool -- there is
    # nothing to move between -- so the address a request leaves through is on
    # the provider's own config. See ``resolve_proxy_chain``.
    assert first._config.proxy == FIRST_URL

    _save_chain(client, SECOND_URL)
    second = manager._current.runtime.resolve_provider("nvidia_nim")

    assert second._config.proxy == SECOND_URL
    assert second is not first


def test_no_other_providers_catalogue_is_touched_by_a_chain_save(
    monkeypatch,
) -> None:
    """Identity, not equality: nothing re-derived what it already had."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    before = manager.cached_model_ids()

    _save_chain(client, FIRST_URL)

    after = manager.cached_model_ids()
    assert set(after) == set(before)
    for provider_id, models in before.items():
        assert after[provider_id] is models or after[provider_id] == models


def test_a_batch_that_says_republish_false_does_not_rebuild(monkeypatch) -> None:
    """The bulk add's own knob, asserted on the generation counter."""

    scheduled = _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    ids = _offer(client, [FIRST_URL, SECOND_URL])
    before = manager.current_generation_id

    response = client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={
            "action": "add",
            "provider": "nvidia_nim",
            "proxies": ids[:1],
            "republish": False,
        },
    )

    assert response.status_code == 200, response.text
    assert manager.current_generation_id == before
    assert scheduled == []


def test_the_last_batch_republishes_once_for_the_whole_gesture(monkeypatch) -> None:
    scheduled = _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    ids = _offer(client, [FIRST_URL, SECOND_URL])
    before = manager.current_generation_id

    for index, proxy_id in enumerate(ids):
        response = client.post(
            "/admin/api/proxy-chains/candidates/bulk",
            json={
                "action": "add",
                "provider": "nvidia_nim",
                "proxies": [proxy_id],
                "republish": index == len(ids) - 1,
            },
        )
        assert response.status_code == 200, response.text

    assert manager.current_generation_id == before + 1
    assert scheduled == []


def test_a_bulk_add_that_says_nothing_republishes_exactly_as_it_always_did(
    monkeypatch,
) -> None:
    """The default is the old behaviour, for every caller that is not the page."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    ids = _offer(client, [FIRST_URL])
    before = manager.current_generation_id

    response = client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": ids},
    )

    assert response.status_code == 200, response.text
    assert manager.current_generation_id == before + 1


def test_the_republish_route_rebuilds_from_the_store(monkeypatch) -> None:
    """What a stopped run calls, and the only thing it does."""

    scheduled = _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    ids = _offer(client, [FIRST_URL])
    client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={
            "action": "add",
            "provider": "nvidia_nim",
            "proxies": ids,
            "republish": False,
        },
    )
    before = manager.current_generation_id

    response = client.post("/admin/api/proxy-chains/republish", json={})

    assert response.status_code == 200, response.text
    assert manager.current_generation_id == before + 1
    assert scheduled == []
    entries = [
        entry
        for provider in response.json()["providers"]
        if provider["provider_id"] == "nvidia_nim"
        for entry in provider["chain"]["entries"]
    ]
    assert entries, "the stopped run's addresses are in the chain it rebuilt"


# ------------------------------------------------------------------ fixtures


def _stub_bulk_checker(monkeypatch) -> list[dict[str, Any]]:
    """Every address passes. The network is not what these tests are about."""

    from my_claude_code.application.proxy_check import (
        ProxyCheckOutcome,
        apply_outcome,
    )
    from my_claude_code.config.credentials import mask_proxy_label
    from my_claude_code.config.proxy_chains import (
        TLS_STRICT,
        ProxyCheckRecord,
        load_proxy_chains,
        save_proxy_chains,
    )

    calls: list[dict[str, Any]] = []

    async def fake_check(proxy_ids, destinations, **kwargs):
        calls.append({"ids": list(proxy_ids), "kwargs": kwargs})
        store = load_proxy_chains()
        outcomes: dict[str, ProxyCheckOutcome] = {}
        for proxy_id in proxy_ids:
            endpoint = store.endpoint(proxy_id)
            if endpoint is None:
                continue
            label = mask_proxy_label(endpoint.url)
            record = ProxyCheckRecord(ok=True, tls=TLS_STRICT)
            apply_outcome(label, record)
            outcomes[proxy_id] = ProxyCheckOutcome(label=label, record=record)
        fresh = load_proxy_chains()
        for proxy_id, outcome in outcomes.items():
            fresh = fresh.with_check(proxy_id, outcome.record)
        save_proxy_chains(fresh)
        return outcomes

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )
    return calls


def _offer(client: TestClient, urls: list[str]) -> list[str]:
    """Put addresses in the catalogue as candidates, the way a fetch does."""

    from dataclasses import replace

    from my_claude_code.config.proxy_chains import (
        ProxyChains,
        load_proxy_chains,
        save_proxy_chains,
    )

    store: ProxyChains = load_proxy_chains()
    ids: list[str] = []
    for url in urls:
        store, proxy_id = store.add_endpoint(url)
        ids.append(proxy_id)
    save_proxy_chains(replace(store, candidates=tuple(ids)))
    return ids
