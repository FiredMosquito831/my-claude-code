"""PR-S3 (7.89.0): the Sources routes are masked, and an offer is added to a
chain through the chain page's one bulk add."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from my_claude_code.application.proxy_check import ProxyCheckOutcome
from my_claude_code.application.proxy_sources import ScanAnswer, local_offer_id
from my_claude_code.config import proxy_chains, proxy_sources
from my_claude_code.config.proxy_chains import ProxyCheckRecord
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

USERNAME = "isolated-user-42"
PASSWORD = "s3cret-pass-word"
FOUND = [
    ScanAnswer(1080, True, "socks5", "userpass"),
    ScanAnswer(9050, True, "socks5", "none"),
    ScanAnswer(40000, True, "socks5", "none"),
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()

    async def fake_scan(*args, **kwargs):
        return list(FOUND)

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_source_routes.scan_local", fake_scan
    )
    yield path
    proxy_chains.reset_proxy_chains_cache()


@pytest.fixture
def logged():
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink)


def _client() -> TestClient:
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"}
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def test_no_source_no_new_key_on_the_proxying_page() -> None:
    payload = _client().get("/admin/api/proxy-chains").json()

    assert "sources" not in payload


def test_the_scan_offers_every_listener_and_the_page_shows_them() -> None:
    client = _client()
    payload = client.post("/admin/api/proxy-sources/scan").json()

    assert payload["scan"] == {
        "ports": [1080, 9050, 40000],
        "answering": [1080, 9050, 40000],
        "offered": [1080, 9050, 40000],
    }
    local = payload["sources"]["sources"][0]
    assert local["id"] == "src_local"
    assert [row["port"] for row in local["listeners"]] == [1080, 9050, 40000]
    assert all(row["offered"] for row in local["listeners"])
    assert local["listeners"][1]["usually"] == "Tor"
    assert local["listeners"][1]["tor"] is True
    assert payload["sources"]["terms"].startswith("Spreading requests")
    # The same data from the masked GET.
    assert (
        client.get("/admin/api/proxy-sources").json()["sources"]
        == (payload["sources"]["sources"])
    )


def test_a_login_is_stored_owner_only_and_never_sent_back(
    _isolate: Path, logged: list[str]
) -> None:
    client = _client()
    client.post("/admin/api/proxy-sources/scan")

    response = client.put(
        "/admin/api/proxy-sources",
        json={
            "source": "src_local",
            "credentials": [{"port": 1080, "username": USERNAME, "password": PASSWORD}],
        },
    )

    assert response.status_code == 200, response.text
    for secret in (USERNAME, PASSWORD):
        assert secret not in response.text
        assert secret not in client.get("/admin/api/proxy-sources").text
        assert secret not in client.get("/admin/api/proxy-chains").text
        assert not any(secret in line for line in logged)
    row = response.json()["sources"]["sources"][0]["listeners"][0]
    assert row["secret_set"] is True
    assert row["secret_label"] == "isol…r-42"
    # Where it lives: the sources file, and the offered address's URL.
    stored = json.loads(proxy_sources.proxy_sources_path().read_text("utf-8"))
    assert {"type": "userpass", "username": USERNAME, "password": PASSWORD} in (
        stored["secrets"].values()
    )
    endpoint = proxy_chains.load_proxy_chains().endpoint(local_offer_id(1080, "socks5"))
    assert endpoint is not None
    assert endpoint.url == f"socks5h://{USERNAME}:{PASSWORD}@127.0.0.1:1080"


def test_an_offer_goes_into_a_chain_through_the_bulk_add_and_stays_offered(
    monkeypatch,
) -> None:
    client = _client()
    client.post("/admin/api/proxy-sources/scan")
    tor = local_offer_id(9050, "socks5")

    async def fake_check(ids, destinations, **kwargs):
        return {
            proxy_id: ProxyCheckOutcome(
                label="Local SOCKS5 · 127.0.0.1:9050",
                record=ProxyCheckRecord(
                    at="2026-10-09T10:00:00Z", ok=True, tls="strict"
                ),
            )
            for proxy_id in ids
        }

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )
    response = client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": [tor]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["bulk"]["results"][0]["outcome"] == "added"
    card = next(
        p for p in response.json()["providers"] if p["provider_id"] == "nvidia_nim"
    )
    assert [entry["label"] for entry in card["chain"]["entries"]] == [
        "Local SOCKS5 · 127.0.0.1:9050"
    ]
    row = response.json()["sources"]["sources"][0]["listeners"][1]
    assert row["offered"] is True
    assert row["chained"] == ["nvidia_nim"]


def test_a_login_change_on_a_chained_offer_rebuilds_that_provider(
    monkeypatch,
) -> None:
    from my_claude_code.api import admin_proxy_source_routes

    client = _client()
    client.post("/admin/api/proxy-sources/scan")
    socks = local_offer_id(1080, "socks5")
    client.put(
        "/admin/api/proxy-chains",
        json={"provider": "nvidia_nim", "enabled": True, "entries": [{"proxy": socks}]},
    )
    asked: list[object] = []

    async def republish(services, provider_ids):
        asked.append(frozenset(provider_ids))
        return ""

    monkeypatch.setattr(admin_proxy_source_routes, "_republish", republish)

    client.put(
        "/admin/api/proxy-sources",
        json={
            "source": "src_local",
            "credentials": [{"port": 1080, "username": "u", "password": "p"}],
        },
    )

    assert asked == [frozenset({"nvidia_nim"})]


def test_a_kind_a_later_release_builds_is_refused() -> None:
    response = _client().put(
        "/admin/api/proxy-sources", json={"source": "src_nord", "kind": "account"}
    )

    assert response.status_code == 422
    assert "later release" in response.json()["detail"]


def test_a_login_for_a_port_that_did_not_ask_is_refused() -> None:
    client = _client()
    client.post("/admin/api/proxy-sources/scan")

    response = client.put(
        "/admin/api/proxy-sources",
        json={
            "source": "src_local",
            "credentials": [{"port": 9050, "username": "u", "password": "p"}],
        },
    )

    assert response.status_code == 422
    assert "did not ask" in response.json()["detail"]


def test_removing_the_source_withdraws_its_offers() -> None:
    client = _client()
    client.post("/admin/api/proxy-sources/scan")

    payload = client.put(
        "/admin/api/proxy-sources", json={"source": "src_local", "remove": True}
    ).json()

    assert "sources" not in payload
    assert proxy_chains.load_proxy_chains().source_offers == {}
