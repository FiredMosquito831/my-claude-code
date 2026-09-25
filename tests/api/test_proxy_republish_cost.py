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

7.55.0 made the second half literal. A new generation is still published, but
it *holds the previous generation's objects* for every provider but the saved
one: the same client, the same credential pool with its benches. Until then a
save for one provider rebuilt every provider, and reset every pool with it.
"""

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, runtime_for_app

FIRST_URL = "http://198.51.100.9:8080"
SECOND_URL = "http://203.0.113.11:3128"
SHARED_URL = "http://192.0.2.44:3128"


@pytest.fixture(autouse=True)
def _isolate_chain_store(monkeypatch, tmp_path: Path):
    from my_claude_code.api import admin_proxy_routes
    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    # Process-wide by design (like the undo slot); no test may inherit another
    # test's owed rebuilds.
    admin_proxy_routes._UNROUTED.clear()
    yield path
    admin_proxy_routes._UNROUTED.clear()
    proxy_chains.reset_proxy_chains_cache()


def _settings() -> Settings:
    """Three providers with credentials, so "every provider" means something.

    By their env names where the field has a ``validation_alias``: until
    7.55.0 this passed the field names, which ``Settings`` silently drops, so
    "three providers" was one.
    """

    return Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "OPENROUTER_API_KEY": "or-key",
            "GROQ_API_KEY": "groq-key",
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


def _save_chain(
    client: TestClient, url: str, *urls: str, provider: str = "nvidia_nim"
) -> None:
    response = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": provider,
            "enabled": True,
            "policy": "failover" if urls else "single",
            "scope": "provider",
            "max_switches": 1,
            "on": ["quota"],
            "entries": [{"url": each} for each in (url, *urls)],
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
    """The half that must NOT change: the new chain routes immediately.

    Still a new generation in 7.55.0 -- only what it holds changed.
    """

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


# ------------------------------------------- 7.55.0: only the saved provider


class _Unauthorized(Exception):
    """A key-shaped failure: the pool benches the key that answered it."""

    status_code = 401


def _built(manager, *provider_ids: str) -> dict[str, Any]:
    """Build (or fetch) providers on the current generation, as a request does."""

    runtime = manager._current.runtime
    return {
        provider_id: runtime.resolve_provider(provider_id)
        for provider_id in provider_ids
    }


def _current(manager, provider_id: str) -> Any:
    return manager._current.runtime.resolve_provider(provider_id)


def _count_cleanups(monkeypatch, closed: dict[str, int], **providers: Any) -> None:
    """Count every ``cleanup()`` of these exact objects, by the name given."""

    for name, provider in providers.items():
        closed.setdefault(name, 0)

        async def spy(name: str = name) -> None:
            closed[name] += 1

        monkeypatch.setattr(provider, "cleanup", spy)


def test_chain_save_keeps_other_providers_objects(monkeypatch) -> None:
    """Identity, through the real route and the real manager.

    The saved provider is a new object on the new chain; every other provider
    the old generation had built is the *same object* in the new one -- which
    is what "its pool, its benches, its limiter are untouched" means.
    """

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    before = _built(manager, "nvidia_nim", "open_router", "groq")
    generation = manager.current_generation_id

    _save_chain(client, FIRST_URL)

    assert manager.current_generation_id == generation + 1
    assert manager._current.runtime.is_cached("groq")
    assert manager._current.runtime.is_cached("open_router")
    assert _current(manager, "groq") is before["groq"]
    assert _current(manager, "open_router") is before["open_router"]
    rebuilt = _current(manager, "nvidia_nim")
    assert rebuilt is not before["nvidia_nim"]
    assert rebuilt._config.proxy == FIRST_URL
    assert before["nvidia_nim"]._config.proxy != FIRST_URL

    # What a request would lease sees the same thing.
    lease = asyncio.run(manager.acquire())
    try:
        assert lease.resolve_provider("groq") is before["groq"]
    finally:
        asyncio.run(lease.release())

    # Removing the chain is a write to the same one provider.
    response = client.put(
        "/admin/api/proxy-chains", json={"provider": "nvidia_nim", "remove": True}
    )
    assert response.status_code == 200, response.text
    assert _current(manager, "groq") is before["groq"]
    assert _current(manager, "open_router") is before["open_router"]
    removed = _current(manager, "nvidia_nim")
    assert removed is not rebuilt
    assert removed._config.proxy != FIRST_URL


def test_a_provider_not_yet_built_is_built_fresh_from_the_new_store(
    monkeypatch,
) -> None:
    """Nothing is carried that the old generation never built."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    groq = _built(manager, "groq")["groq"]

    _save_chain(client, FIRST_URL)

    assert not manager._current.runtime.is_cached("nvidia_nim")
    assert not manager._current.runtime.is_cached("open_router")
    assert _current(manager, "nvidia_nim")._config.proxy == FIRST_URL
    assert _current(manager, "groq") is groq


def test_shared_provider_not_closed_by_retired_generation(monkeypatch) -> None:
    """A carried object is closed once, by the last generation that holds it.

    Three paths: a retired generation that is still leased (closes on
    release), one retired with nothing leased (closes at once), and a newer
    generation that stops holding an object an older, still-leased one is using
    -- the case a pop-before-retire would get wrong.
    """

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    built = _built(manager, "nvidia_nim", "groq", "open_router")
    closed: dict[str, int] = {}
    _count_cleanups(
        monkeypatch,
        closed,
        nim_1=built["nvidia_nim"],
        groq_1=built["groq"],
        open_router=built["open_router"],
    )
    lease = asyncio.run(manager.acquire())  # a request still on generation 1

    _save_chain(client, FIRST_URL)

    assert closed == {"nim_1": 0, "groq_1": 0, "open_router": 0}, (
        "a leased generation closed something before it was released"
    )
    assert lease.resolve_provider("groq") is built["groq"]
    asyncio.run(lease.release())
    assert closed == {"nim_1": 1, "groq_1": 0, "open_router": 0}, (
        "the retired generation closed a provider the new one holds"
    )

    # Nothing leased: the next save retires and closes at once.
    _count_cleanups(monkeypatch, closed, nim_2=_current(manager, "nvidia_nim"))
    _save_chain(client, SECOND_URL)
    assert closed == {"nim_1": 1, "nim_2": 1, "groq_1": 0, "open_router": 0}

    # An older generation still leased keeps groq alive while a newer one
    # rebuilds it: generation 3 carries groq_1, a lease holds it, and the
    # save of groq's own chain retires generation 3 into generation 4.
    old_lease = asyncio.run(manager.acquire())
    _save_chain(client, FIRST_URL)  # generation 4 still carries groq_1
    _count_cleanups(monkeypatch, closed, nim_3=_current(manager, "nvidia_nim"))
    _save_chain(client, SECOND_URL, provider="groq")  # generation 5 rebuilds groq
    groq_2 = _current(manager, "groq")
    assert groq_2 is not built["groq"]
    assert groq_2._config.proxy == SECOND_URL
    assert closed["groq_1"] == 0, "closed while a leased generation still used it"
    assert old_lease.resolve_provider("groq") is built["groq"]
    asyncio.run(old_lease.release())
    assert closed["groq_1"] == 1

    # Shutdown closes each object still held exactly once.
    _count_cleanups(monkeypatch, closed, groq_2=groq_2)
    asyncio.run(manager.close())
    assert closed == {
        "nim_1": 1,
        "nim_2": 1,
        "nim_3": 1,
        "groq_1": 1,
        "groq_2": 1,
        "open_router": 1,
    }


def test_credential_pool_benches_survive_other_chain_save(monkeypatch) -> None:
    """Bench a key on groq, save nvidia's chain: groq's pool still says so."""

    _count_sweeps(monkeypatch)
    settings = Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "GROQ_API_KEY": "gsk-one,gsk-two",
        }
    )
    app = create_test_app(settings)
    client = TestClient(app, client=("127.0.0.1", 50000))
    manager = runtime_for_app(app).provider_manager
    groq = _current(manager, "groq")
    asyncio.run(groq._state.report_failure(0, _Unauthorized()))
    benched = groq.key_health()
    assert benched[0]["state"] != "HEALTHY", benched
    assert benched[1]["state"] == "HEALTHY", benched

    _save_chain(client, FIRST_URL)

    kept = _current(manager, "groq")
    assert kept is groq
    health = kept.key_health()
    assert health[0]["state"] == benched[0]["state"]
    assert health[0]["failure_count"] == benched[0]["failure_count"] >= 1
    assert health[1]["state"] == "HEALTHY"

    # The contrast, and the cost the page states: groq's *own* chain save
    # rebuilds groq's pool.
    _save_chain(client, SECOND_URL, provider="groq")
    fresh = _current(manager, "groq")
    assert fresh is not groq
    assert fresh.key_health()[0]["state"] == "HEALTHY"
    assert fresh.key_health()[0]["failure_count"] == 0


def test_a_full_settings_apply_still_rebuilds_every_provider(
    monkeypatch, tmp_path: Path
) -> None:
    """``None`` is the old path: a settings apply carries nothing."""

    from my_claude_code.config.admin.persistence import PreparedAdminUpdate

    _count_sweeps(monkeypatch)
    app, _client = _app_and_client()
    runtime = runtime_for_app(app)
    manager = runtime.provider_manager
    before = _built(manager, "nvidia_nim", "open_router", "groq")
    updated = manager.current_settings().model_copy(
        update={"model": "nvidia_nim/secondary"}
    )
    prepared = PreparedAdminUpdate(
        target_values={"MODEL": updated.model},
        settings=updated,
        errors=(),
        pending_fields=(),
        path=tmp_path / ".env",
    )
    applied = {
        "applied": True,
        "valid": True,
        "errors": [],
        "warnings": [],
        "env_preview": "MODEL=nvidia_nim/secondary\n",
        "path": ".env",
        "pending_fields": [],
    }
    with (
        patch(
            "my_claude_code.runtime.application.prepare_admin_update",
            return_value=prepared,
        ),
        patch(
            "my_claude_code.runtime.application.commit_prepared_admin_update",
            side_effect=lambda _prepared: applied,
        ),
    ):
        asyncio.run(runtime.apply_admin_config({"MODEL": updated.model}))

    assert manager.current_settings() is updated
    for provider_id, provider in before.items():
        assert _current(manager, provider_id) is not provider, provider_id

    # And the custom-provider reload, which also names no provider.
    again = _built(manager, "groq")["groq"]
    asyncio.run(runtime.reload_providers("custom_providers", sweep=False))
    assert _current(manager, "groq") is not again


def test_the_last_batch_rebuilds_only_the_provider_it_added_to(monkeypatch) -> None:
    scheduled = _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    # Armed first: a bulk add into a provider with no chain creates it off.
    _save_chain(client, SHARED_URL)
    ids = _offer(client, [FIRST_URL, SECOND_URL])
    before = _built(manager, "nvidia_nim", "groq")

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

    assert scheduled == []
    assert _current(manager, "groq") is before["groq"]
    assert _current(manager, "nvidia_nim") is not before["nvidia_nim"]
    plan = _current(manager, "nvidia_nim")._config.proxy_chain
    assert plan is not None and len(plan.legs) == 3


def test_a_stopped_run_republishes_only_what_it_wrote(monkeypatch) -> None:
    """``/republish`` rebuilds the providers the unpublished batches wrote."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    _save_chain(client, SHARED_URL)
    ids = _offer(client, [FIRST_URL])
    before = _built(manager, "nvidia_nim", "groq", "open_router")
    client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={
            "action": "add",
            "provider": "nvidia_nim",
            "proxies": ids,
            "republish": False,
        },
    )
    # Not routed yet: the batch said it was not the last.
    assert _current(manager, "nvidia_nim") is before["nvidia_nim"]

    response = client.post("/admin/api/proxy-chains/republish", json={})

    assert response.status_code == 200, response.text
    assert _current(manager, "nvidia_nim") is not before["nvidia_nim"]
    plan = _current(manager, "nvidia_nim")._config.proxy_chain
    assert plan is not None and len(plan.legs) == 2
    assert _current(manager, "groq") is before["groq"]
    assert _current(manager, "open_router") is before["open_router"]

    # With nothing owed, it cannot tell what might be, and rebuilds everything.
    groq = _current(manager, "groq")
    response = client.post("/admin/api/proxy-chains/republish", json={})
    assert response.status_code == 200, response.text
    assert _current(manager, "groq") is not groq


def test_a_lost_republish_is_still_owed_by_the_next_save(monkeypatch) -> None:
    """A batch that never got its ``/republish`` rides on the next chain write."""

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    _save_chain(client, SHARED_URL, provider="groq")
    ids = _offer(client, [FIRST_URL])
    client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={
            "action": "add",
            "provider": "groq",
            "proxies": ids,
            "republish": False,
        },
    )
    groq = _built(manager, "groq")["groq"]
    open_router = _built(manager, "open_router")["open_router"]

    _save_chain(client, SECOND_URL)  # nvidia's chain, not groq's

    assert _current(manager, "groq") is not groq
    plan = _current(manager, "groq")._config.proxy_chain
    assert plan is not None and len(plan.legs) == 2
    assert _current(manager, "open_router") is open_router


def test_undo_rebuilds_only_the_chains_it_changed(monkeypatch) -> None:
    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _stub_bulk_checker(monkeypatch)
    _save_chain(client, SHARED_URL)
    ids = _offer(client, [FIRST_URL])
    response = client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": ids},
    )
    assert response.status_code == 200, response.text
    token = response.json()["bulk"]["undo_token"]
    before = _built(manager, "nvidia_nim", "groq", "open_router")
    plan = before["nvidia_nim"]._config.proxy_chain
    assert plan is not None and len(plan.legs) == 2
    generation = manager.current_generation_id

    response = client.post(
        "/admin/api/proxy-chains/candidates/undo", json={"token": token}
    )

    assert response.status_code == 200, response.text
    assert manager.current_generation_id == generation + 1
    assert _current(manager, "groq") is before["groq"]
    assert _current(manager, "open_router") is before["open_router"]
    undone = _current(manager, "nvidia_nim")
    assert undone is not before["nvidia_nim"]
    assert undone._config.proxy_chain is None
    assert undone._config.proxy == SHARED_URL


def test_a_chain_save_never_changes_another_chains_legs(monkeypatch) -> None:
    """The claim a scoped rebuild rests on, pinned for every PUT shape.

    Two chains share an address. Saving one of them -- keeping the shared
    address, dropping it, naming it by id, removing the chain outright -- must
    leave the other chain's build-time inputs byte-identical, so the provider
    that was not saved has nothing to pick up.
    """

    from my_claude_code.api.admin_proxy_routes import changed_chain_providers
    from my_claude_code.config.proxy_chains import load_proxy_chains
    from my_claude_code.providers.runtime.config import resolve_proxy_chain

    _count_sweeps(monkeypatch)
    app, client = _app_and_client()
    settings = runtime_for_app(app).provider_manager.current_settings()
    _save_chain(client, SHARED_URL, SECOND_URL, provider="groq")
    _save_chain(client, SHARED_URL, FIRST_URL)
    stored = load_proxy_chains()
    shared_id = next(
        proxy_id
        for proxy_id, endpoint in stored.proxies.items()
        if endpoint.url == SHARED_URL
    )
    assert shared_id in stored.chains["groq"].proxy_ids()
    assert shared_id in stored.chains["nvidia_nim"].proxy_ids()
    groq_plan = resolve_proxy_chain("groq", "", settings)

    writes: list[dict[str, Any]] = [
        {"entries": [{"url": FIRST_URL}]},  # drops the shared address
        {"entries": [{"proxy": shared_id}]},  # names it by id
        {"entries": [{"url": SHARED_URL}, {"direct": True}]},  # retypes its URL
        {"remove": True},  # removes the chain that shared it
    ]
    for write in writes:
        before = load_proxy_chains()
        response = client.put(
            "/admin/api/proxy-chains",
            json={
                "provider": "nvidia_nim",
                "enabled": True,
                "policy": "failover",
                "scope": "provider",
                "max_switches": 1,
                "on": ["quota"],
                **write,
            },
        )
        assert response.status_code == 200, response.text
        after = load_proxy_chains()
        assert changed_chain_providers(before, after) <= {"nvidia_nim"}, write
        assert resolve_proxy_chain("groq", "", settings) == groq_plan, write
        assert after.endpoint(shared_id) == before.endpoint(shared_id), write


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
