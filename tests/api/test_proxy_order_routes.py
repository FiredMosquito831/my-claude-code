"""The routes behind "Keep the fastest healthy proxy first" (7.56.0).

The switch, "Sort by speed now" and "Pause all but the fastest N", through the
real app. Two properties carry the user's decisions: a chain stored before
7.56.0 stays OFF through every save that does not name the field, and a write
of a new order rebuilds that one provider and nothing else (7.55.0's identity
assertion, reused).
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_proxy_routes import republish_chains
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import load_proxy_chains
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import reset_proxy_health
from my_claude_code.core.proxy_speed import KIND_CHECK, PROXY_SPEED, SpeedSample
from my_claude_code.runtime.proxy_order import ProxyOrderTimer
from tests.api.support import create_test_app, runtime_for_app

SLOW = "http://192.0.2.11:8080"
FAST = "http://192.0.2.12:8080"
MID = "http://192.0.2.13:8080"


@pytest.fixture(autouse=True)
def _isolate_chain_store(monkeypatch, tmp_path: Path):
    from my_claude_code.api import admin_proxy_routes
    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    admin_proxy_routes._UNROUTED.clear()
    reset_proxy_health()
    yield path
    admin_proxy_routes._UNROUTED.clear()
    reset_proxy_health()
    proxy_chains.reset_proxy_chains_cache()


def _settings() -> Settings:
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


def _put(client: TestClient, urls: list[str], **extra: Any) -> dict[str, Any]:
    body = {
        "provider": "nvidia_nim",
        "enabled": True,
        "policy": "failover",
        "scope": "provider",
        "max_switches": 1,
        "on": ["quota"],
        "entries": [{"url": url} for url in urls],
        **extra,
    }
    response = client.put("/admin/api/proxy-chains", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _chain(payload: dict[str, Any], provider: str = "nvidia_nim") -> dict[str, Any]:
    card = next(
        item for item in payload["providers"] if item["provider_id"] == provider
    )
    assert card["chain"] is not None
    return card["chain"]


def _labels(payload: dict[str, Any]) -> list[str]:
    return [entry["label"] or "Direct" for entry in _chain(payload)["entries"]]


def _measure(url: str, setup_ms: float, count: int = 5) -> None:
    for _ in range(count):
        PROXY_SPEED.note(
            mask_proxy_label(url),
            "nvidia_nim",
            SpeedSample(kind=KIND_CHECK, at=time.time(), ok=True, connect_ms=setup_ms),
        )


def _count_rebuilds(monkeypatch) -> list[frozenset[str] | None]:
    from my_claude_code.runtime.application import ApplicationRuntime

    seen: list[frozenset[str] | None] = []
    original = ApplicationRuntime.reload_providers

    async def spy(self, reason, **kwargs):
        seen.append(kwargs.get("rebuild_provider_ids"))
        return await original(self, reason, **kwargs)

    monkeypatch.setattr(ApplicationRuntime, "reload_providers", spy)
    return seen


def test_a_put_that_creates_a_chain_without_the_field_turns_it_on() -> None:
    _, client = _app_and_client()
    payload = _put(client, [SLOW, FAST])
    assert _chain(payload)["order_by_speed"] is True
    assert _chain(payload)["order_offered"] is True
    assert payload["vocabulary"]["order"]["resort_minutes"] == 30


def test_an_existing_chain_stays_off_through_a_save_that_does_not_name_it(
    _isolate_chain_store: Path,
) -> None:
    _, client = _app_and_client()
    _put(client, [SLOW, FAST])
    # Make it a chain stored by 7.55.0: no ordering keys at all.
    document = json.loads(_isolate_chain_store.read_text(encoding="utf-8"))
    for chain in document["chains"].values():
        chain.pop("order_by_speed", None)
        chain.pop("order_sorted_at", None)
    _isolate_chain_store.write_text(json.dumps(document), encoding="utf-8")

    payload = client.get("/admin/api/proxy-chains").json()
    assert _chain(payload)["order_by_speed"] is False
    payload = _put(client, [SLOW, FAST, MID])
    assert _chain(payload)["order_by_speed"] is False
    # Named explicitly, it is honoured either way.
    assert _chain(_put(client, [SLOW, FAST], order_by_speed=True))["order_by_speed"]
    assert not _chain(_put(client, [SLOW], order_by_speed=False))["order_by_speed"]


def test_the_switch_writes_the_flag_only_and_rebuilds_nothing(monkeypatch) -> None:
    _, client = _app_and_client()
    _put(client, [SLOW, FAST], order_by_speed=False)
    _measure(SLOW, 9000)
    _measure(FAST, 300)
    rebuilds = _count_rebuilds(monkeypatch)

    response = client.post(
        "/admin/api/proxy-chains/order",
        json={"provider": "nvidia_nim", "order_by_speed": True},
    )
    assert response.status_code == 200, response.text
    chain = _chain(response.json())
    assert chain["order_by_speed"] is True
    # Turning it on moves nothing by itself.
    assert _labels(response.json()) == [mask_proxy_label(SLOW), mask_proxy_label(FAST)]
    assert rebuilds == []


def test_the_switch_is_refused_for_round_robin() -> None:
    _, client = _app_and_client()
    payload = _put(client, [SLOW, FAST], policy="round_robin", order_by_speed=False)
    assert _chain(payload)["order_offered"] is False
    response = client.post(
        "/admin/api/proxy-chains/order",
        json={"provider": "nvidia_nim", "order_by_speed": True},
    )
    assert response.status_code == 422
    assert "failover and single" in response.json()["detail"]
    response = client.post(
        "/admin/api/proxy-chains/sort", json={"provider": "nvidia_nim"}
    )
    assert response.status_code == 422


def test_sort_now_rebuilds_only_that_provider(monkeypatch) -> None:
    """A written order is a chain save of that provider: 7.55.0's identity."""

    app, client = _app_and_client()
    manager = runtime_for_app(app).provider_manager
    _put(client, [SLOW, MID, FAST], order_by_speed=False)
    runtime = manager._current.runtime
    before = {
        pid: runtime.resolve_provider(pid)
        for pid in ("nvidia_nim", "open_router", "groq")
    }
    generation = manager.current_generation_id
    _measure(SLOW, 9000)
    _measure(MID, 3000)
    _measure(FAST, 300)
    rebuilds = _count_rebuilds(monkeypatch)

    response = client.post(
        "/admin/api/proxy-chains/sort", json={"provider": "nvidia_nim"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sorted"]["written"] is True
    assert body["sorted"]["new_first"] == mask_proxy_label(FAST)
    assert _labels(body) == [mask_proxy_label(u) for u in (FAST, MID, SLOW)]
    assert rebuilds == [frozenset({"nvidia_nim"})]
    assert manager.current_generation_id == generation + 1
    current = manager._current.runtime
    assert current.resolve_provider("groq") is before["groq"]
    assert current.resolve_provider("open_router") is before["open_router"]
    assert current.resolve_provider("nvidia_nim") is not before["nvidia_nim"]
    # It never touched the switch: the chain is still OFF.
    assert _chain(body)["order_by_speed"] is False

    # Pressed again: already in order, nothing written, nothing rebuilt.
    again = client.post("/admin/api/proxy-chains/sort", json={"provider": "nvidia_nim"})
    assert again.json()["sorted"]["written"] is False
    assert rebuilds == [frozenset({"nvidia_nim"})]


def test_the_loop_rebuilds_only_the_provider_it_sorted() -> None:
    app, client = _app_and_client()
    runtime = runtime_for_app(app)
    manager = runtime.provider_manager
    _put(client, [SLOW, FAST])
    built = manager._current.runtime
    before = {
        pid: built.resolve_provider(pid)
        for pid in ("nvidia_nim", "open_router", "groq")
    }
    _measure(SLOW, 9000)
    _measure(FAST, 300)

    timer = ProxyOrderTimer(
        lambda: runtime.settings,
        lambda ids: republish_chains(runtime, ids),
    )
    assert asyncio.run(timer.tick()) == ("nvidia_nim",)
    current = manager._current.runtime
    assert current.resolve_provider("groq") is before["groq"]
    assert current.resolve_provider("open_router") is before["open_router"]
    rebuilt = current.resolve_provider("nvidia_nim")
    assert rebuilt is not before["nvidia_nim"]
    # What the rebuilt provider will dial, in order: failover's index 0 is FAST.
    plan = rebuilt._config.proxy_chain
    assert plan is not None
    assert [leg.url for leg in plan.legs] == [FAST, SLOW]
    assert before["nvidia_nim"]._config.proxy_chain is not None
    assert [leg.url for leg in before["nvidia_nim"]._config.proxy_chain.legs] == [
        SLOW,
        FAST,
    ]
    assert _labels(client.get("/admin/api/proxy-chains").json()) == [
        mask_proxy_label(FAST),
        mask_proxy_label(SLOW),
    ]


def test_pause_all_but_the_fastest_is_explicit_and_reversible(monkeypatch) -> None:
    _, client = _app_and_client()
    payload = _put(client, [SLOW, MID, FAST])
    _measure(SLOW, 9000)
    _measure(MID, 3000)
    _measure(FAST, 300)
    rebuilds = _count_rebuilds(monkeypatch)

    response = client.post(
        "/admin/api/proxy-chains/pause-fastest",
        json={"provider": "nvidia_nim", "keep": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["paused"]["kept"] == [mask_proxy_label(FAST)]
    assert body["paused"]["paused"] == [mask_proxy_label(SLOW), mask_proxy_label(MID)]
    entries = _chain(body)["entries"]
    assert [entry["paused"] for entry in entries] == [True, True, False]
    # Order untouched: pausing is not sorting.
    assert _labels(body) == [mask_proxy_label(u) for u in (SLOW, MID, FAST)]
    assert rebuilds == [frozenset({"nvidia_nim"})]

    # Reversible with the ordinary Resume (a save with paused=false).
    resumed = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "failover",
            "on": ["quota"],
            "order_by_speed": True,
            "entries": [
                {"proxy": entry["proxy"], "paused": False} for entry in entries
            ],
        },
    )
    assert resumed.status_code == 200, resumed.text
    assert not any(entry["paused"] for entry in _chain(resumed.json())["entries"])
    assert payload  # the first save's answer, for the reader


def test_pause_all_but_fastest_refuses_when_nothing_is_measured() -> None:
    _, client = _app_and_client()
    _put(client, [SLOW, FAST])
    response = client.post(
        "/admin/api/proxy-chains/pause-fastest",
        json={"provider": "nvidia_nim", "keep": 1},
    )
    assert response.status_code == 422
    assert "Test all" in response.json()["detail"]
    chain = load_proxy_chains().chain("nvidia_nim")
    assert chain is not None and not any(entry.paused for entry in chain.entries)


def test_the_routes_need_a_saved_chain() -> None:
    _, client = _app_and_client()
    for path, body in (
        (
            "/admin/api/proxy-chains/order",
            {"provider": "nvidia_nim", "order_by_speed": True},
        ),
        ("/admin/api/proxy-chains/sort", {"provider": "nvidia_nim"}),
        (
            "/admin/api/proxy-chains/pause-fastest",
            {"provider": "nvidia_nim", "keep": 2},
        ),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 404, (path, response.text)
    response = client.post("/admin/api/proxy-chains/sort", json={"provider": "nobody"})
    assert response.status_code == 404
