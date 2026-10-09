"""7.91.0 (PR-S4 + PR-S5): the vendor kinds through the Sources routes.

End to end through the app, with no vendor contacted -- the one fetch
function is replaced by a recorder that answers the committed fixtures:

* saving an account, a gateway or a list contacts nothing, offers named
  addresses, and no response, page payload or log line carries a password, a
  user name, a session password or a download link's token;
* *Fetch now* reads the confirmed URL, through the exit of the chain named
  for the source -- and sends nothing where that chain refuses;
* the schedule is off unless chosen, and a save that may change it re-arms
  the loop's hook;
* every route is loopback-only.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from my_claude_code.api import admin_proxy_source_routes
from my_claude_code.application.proxy_check import ProxyCheckOutcome
from my_claude_code.application.proxy_vendor_sources import (
    SOURCE_SCHEDULE,
    FetchedText,
)
from my_claude_code.config import proxy_chains, proxy_sources
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "proxy_sources"
NORD_JSON = (FIXTURES / "nordvpn_servers_socks.json").read_text(encoding="utf-8")
WEBSHARE = (FIXTURES / "webshare_list.txt").read_text(encoding="utf-8")
SERVICE_USER = "route-service-user-31"
SERVICE_PASS = "route-service-pass-77"
GATEWAY_USER = "route-gw-user-88"
GATEWAY_PASS = "route-gw-pass-99"
TOKEN = "ROUTE-TOKEN-5e4d3c2b1a"
LIST_URL = f"https://proxy.webshare.io/api/v2/proxy/list/download/{TOKEN}/-/any/username/direct/-/"
NORD_URL = "https://api.nordvpn.com/v1/servers?filters[servers_technologies][identifier]=socks&limit=200"
SECRETS = (SERVICE_USER, SERVICE_PASS, GATEWAY_USER, GATEWAY_PASS, TOKEN, "wspass")


@pytest.fixture(autouse=True)
def _chains(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    yield path
    proxy_chains.reset_proxy_chains_cache()


@pytest.fixture
def logged() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink)


@pytest.fixture
def fetched(monkeypatch) -> list[tuple[str, str | None]]:
    """The fetch, replaced: records (url, proxy) and answers the fixture."""

    calls: list[tuple[str, str | None]] = []

    async def fake_fetch(url: str, *, proxy, timeout, transport=None) -> FetchedText:
        calls.append((url, proxy))
        return FetchedText(ok=True, text=NORD_JSON if "nordvpn" in url else WEBSHARE)

    monkeypatch.setattr(admin_proxy_source_routes, "fetch_vendor_text", fake_fetch)
    return calls


def _client(host: str = "127.0.0.1") -> TestClient:
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"}
    )
    return TestClient(create_test_app(settings), client=(host, 50000))


def _put(client: TestClient, kind: str, block: dict, source: str = "") -> dict:
    key = "proxy_list" if kind == "list" else kind
    response = client.put(
        "/admin/api/proxy-sources",
        json={"source": source, "kind": kind, key: block},
    )
    assert response.status_code == 200, response.text
    return response.json()


ACCOUNT = {
    "preset": "nordvpn",
    "hosts": "nl.socks.nordhold.net",
    "username": SERVICE_USER,
    "password": SERVICE_PASS,
}
GATEWAY = {
    "preset": "brightdata",
    "host": "brd.superproxy.io",
    "port": 22228,
    "user": GATEWAY_USER,
    "password": GATEWAY_PASS,
    "zone": "isp_zone",
    "zone_type": "isp",
    "count": 3,
}
LIST = {"preset": "webshare", "url": LIST_URL}


def _all_three(client: TestClient) -> None:
    _put(client, "account", ACCOUNT)
    _put(client, "gateway", GATEWAY)
    _put(client, "list", LIST)


def test_saving_each_kind_contacts_nothing_and_offers_named_addresses(
    fetched, logged
) -> None:
    client = _client()

    account = _put(client, "account", ACCOUNT)
    gateway = _put(client, "gateway", GATEWAY)
    listed = _put(client, "list", LIST)

    assert fetched == []
    assert account["source_result"]["offered"] == 1
    assert gateway["source_result"]["offered"] == 3
    assert listed["source_result"]["offered"] == 0
    assert "Fetch now" in listed["source_result"]["sentence"]
    kinds = [source["kind"] for source in listed["sources"]["sources"]]
    assert kinds == ["account", "gateway", "list"]
    labels = [
        row["label"]
        for source in listed["sources"]["sources"]
        for row in source["offers"]
    ]
    assert labels[0] == "NordVPN · nl.socks.nordhold.net"
    assert all(label.startswith("Bright Data · session ") for label in labels[1:])


def test_no_route_response_or_log_line_carries_a_secret(fetched, logged) -> None:
    client = _client()
    texts = [
        client.put(
            "/admin/api/proxy-sources",
            json={"source": "", "kind": "account", "account": ACCOUNT},
        ).text,
        client.put(
            "/admin/api/proxy-sources",
            json={"source": "", "kind": "gateway", "gateway": GATEWAY},
        ).text,
        client.put(
            "/admin/api/proxy-sources",
            json={"source": "", "kind": "list", "proxy_list": LIST},
        ).text,
        client.post(
            "/admin/api/proxy-sources/fetch", json={"source": "src_list_webshare"}
        ).text,
        client.get("/admin/api/proxy-sources").text,
        client.get("/admin/api/proxy-chains").text,
    ]

    for secret in SECRETS:
        for text in texts:
            assert secret not in text
        assert not any(secret in line for line in logged), secret
    # Not the gateway's session password either, nor any list URL.
    assert not any("session-" in text and GATEWAY_PASS in text for text in texts)
    assert not any("/download/" in text for text in texts)
    # Where they do live: the two owner-only files.
    stored = proxy_sources.proxy_sources_path().read_text("utf-8")
    assert SERVICE_PASS in stored and GATEWAY_PASS in stored and TOKEN in stored
    chain_file = proxy_chains.proxy_chains_path().read_text("utf-8")
    assert SERVICE_PASS in chain_file and "wspass-first" in chain_file
    assert TOKEN not in chain_file


def test_the_presets_route_is_data_only() -> None:
    document = _client().get("/admin/api/proxy-sources/presets").json()

    ids = [item["id"] for item in document["presets"]]
    assert ids == [
        "nordvpn",
        "pia",
        "ipvanish",
        "torguard",
        "mullvad",
        "ivpn",
        "brightdata",
        "oxylabs",
        "iproyal",
        "decodo",
        "webshare",
    ]
    for item in document["presets"]:
        assert item["doc"].startswith("https://")
        assert item["observed"].startswith("2026-")
        assert item["evidence"] in {"V", "CS", "U"}
    bright = next(item for item in document["presets"] if item["id"] == "brightdata")
    assert bright["zones_refused"] == ["residential", "mobile"]


def test_fetch_now_reads_the_confirmed_url_from_this_computer(fetched) -> None:
    client = _client()
    _put(client, "list", LIST)

    payload = client.post(
        "/admin/api/proxy-sources/fetch", json={"source": "src_list_webshare"}
    ).json()

    assert fetched == [(LIST_URL, None)]
    assert payload["fetch_result"] == {
        "source": "src_list_webshare",
        "ok": True,
        "offered": 4,
        "sentence": "Fetched: 4 proxies.",
    }
    card = payload["sources"]["sources"][0]
    assert card["fetch"]["ok"] is True
    assert len(card["offers"]) == 4


def _proxied_chain(path: Path, *, paused: bool = False) -> None:
    save_proxy_chains(
        ProxyChains(
            proxies={
                "px_one": ProxyEndpoint(
                    url="socks5h://127.0.0.1:41081", label="Exit one"
                ),
                "px_two": ProxyEndpoint(
                    url="socks5h://127.0.0.1:41082", label="Exit two"
                ),
            },
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=True,
                    policy="failover",
                    entries=(
                        ProxyChainEntry(proxy="px_one", paused=paused),
                        ProxyChainEntry(proxy="px_two", paused=paused),
                    ),
                    direct_fallback=False,
                )
            },
        ),
        path,
    )
    proxy_chains.reset_proxy_chains_cache()


def test_fetch_now_goes_through_the_chain_named_for_the_source(
    fetched, _chains: Path
) -> None:
    _proxied_chain(_chains)
    client = _client()
    _put(
        client,
        "account",
        ACCOUNT | {"hosts": "", "list_url": NORD_URL, "fetch_via": "nvidia_nim"},
    )

    payload = client.post(
        "/admin/api/proxy-sources/fetch", json={"source": "src_account_nordvpn"}
    ).json()

    assert fetched == [(NORD_URL, "socks5h://127.0.0.1:41081")]
    assert payload["fetch_result"]["ok"] is True
    assert payload["fetch_result"]["offered"] == 4


def test_fetch_now_sends_nothing_when_that_chain_says_no(
    fetched, _chains: Path
) -> None:
    _proxied_chain(_chains, paused=True)
    client = _client()
    _put(client, "list", LIST | {"fetch_via": "nvidia_nim"})

    payload = client.post(
        "/admin/api/proxy-sources/fetch", json={"source": "src_list_webshare"}
    ).json()

    assert fetched == []
    assert payload["fetch_result"]["ok"] is False
    assert payload["fetch_result"]["sentence"].startswith("Not fetched: Not sent:")
    card = payload["sources"]["sources"][0]
    assert card["fetch"]["ok"] is False and card["fetch"]["fetched_at"]
    assert card["offers"] == []


def test_a_chain_that_is_not_a_configured_provider_is_refused(fetched) -> None:
    response = _client().put(
        "/admin/api/proxy-sources",
        json={"source": "", "kind": "list", "proxy_list": LIST | {"fetch_via": "nope"}},
    )

    assert response.status_code == 422
    assert "not a configured provider" in response.json()["detail"]


def test_the_schedule_is_off_by_default_and_a_save_rearms_the_loop(fetched) -> None:
    woken: list[str] = []
    SOURCE_SCHEDULE.attach(lambda: woken.append("rearm"))
    client = _client()

    _put(client, "list", LIST)
    stored = json.loads(proxy_sources.proxy_sources_path().read_text("utf-8"))
    assert "refresh_hours" not in stored["sources"]["src_list_webshare"]

    _put(client, "list", {"refresh_hours": 6}, source="src_list_webshare")
    stored = json.loads(proxy_sources.proxy_sources_path().read_text("utf-8"))
    assert stored["sources"]["src_list_webshare"]["refresh_hours"] == 6
    assert woken == ["rearm", "rearm"]
    assert fetched == []


def test_bright_data_residential_is_refused_through_the_route() -> None:
    response = _client().put(
        "/admin/api/proxy-sources",
        json={
            "source": "",
            "kind": "gateway",
            "gateway": GATEWAY | {"zone_type": "residential"},
        },
    )

    assert response.status_code == 422
    assert "certificate authority" in response.json()["detail"]


def test_an_offer_goes_into_a_chain_through_the_bulk_add(monkeypatch) -> None:
    client = _client()
    payload = _put(client, "gateway", GATEWAY)
    offers = payload["sources"]["sources"][0]["offers"]

    async def fake_check(ids, destinations, **kwargs):
        return {
            proxy_id: ProxyCheckOutcome(
                label="gateway session",
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
        json={
            "action": "add",
            "provider": "nvidia_nim",
            "proxies": [row["proxy"] for row in offers],
        },
    )

    assert response.status_code == 200, response.text
    assert [row["outcome"] for row in response.json()["bulk"]["results"]] == [
        "added"
    ] * 3
    card = next(
        p for p in response.json()["providers"] if p["provider_id"] == "nvidia_nim"
    )
    assert [entry["label"] for entry in card["chain"]["entries"]] == [
        row["label"] for row in offers
    ]
    assert GATEWAY_PASS not in response.text


def test_removing_a_vendor_source_withdraws_its_offers_and_rearms() -> None:
    woken: list[str] = []
    SOURCE_SCHEDULE.attach(lambda: woken.append("rearm"))
    client = _client()
    _put(client, "gateway", GATEWAY)

    response = client.put(
        "/admin/api/proxy-sources",
        json={"source": "src_gateway_bright_data", "remove": True},
    )

    assert response.status_code == 200, response.text
    assert "sources" not in response.json()
    assert proxy_chains.load_proxy_chains().source_offers == {}
    assert woken == ["rearm", "rearm"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/admin/api/proxy-sources/presets", None),
        ("post", "/admin/api/proxy-sources/fetch", {"source": "src_list_webshare"}),
        (
            "put",
            "/admin/api/proxy-sources",
            {"source": "", "kind": "list", "proxy_list": LIST},
        ),
    ],
)
def test_every_vendor_route_is_loopback_only(method: str, path: str, body) -> None:
    client = _client(host="192.0.2.50")
    response = (
        client.get(path)
        if method == "get"
        else getattr(client, method)(path, json=body)
    )

    assert response.status_code == 403
