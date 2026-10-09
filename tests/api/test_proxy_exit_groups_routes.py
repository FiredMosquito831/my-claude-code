"""PR-S2 (7.89.0) on the Proxying page: exit groups, "Keep one per exit", the
exit-identity mode the checker note reads, and the bulk add's warning."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.proxy_check import (
    DirectExit,
    ExitIdentity,
    ProxyCheckOutcome,
    record_direct_exit,
    reset_direct_exits,
)
from my_claude_code.config import proxy_chains
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

WARP = "104.28.1.2"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    reset_direct_exits()
    yield path
    proxy_chains.reset_proxy_chains_cache()
    reset_direct_exits()


def _client(**values: str) -> TestClient:
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"} | values
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _check(
    exit_ip: str, *, exit_country: str = "", exit_warp: str = ""
) -> ProxyCheckRecord:
    return ProxyCheckRecord(
        at="2026-10-09T10:00:00Z",
        ok=True,
        tls="strict",
        exit_ip=exit_ip,
        exit_country=exit_country,
        exit_warp=exit_warp,
    )


def _seed(path: Path, exits: list[str], *, paused: tuple[int, ...] = ()) -> None:
    proxies = {
        f"px_{index}": ProxyEndpoint(
            url=f"socks5h://198.51.100.{index + 1}:1080",
            last_check=_check(exit_ip, exit_country="NL", exit_warp="on")
            if exit_ip
            else None,
        )
        for index, exit_ip in enumerate(exits)
    }
    chain = ProxyChain(
        enabled=True,
        entries=tuple(
            ProxyChainEntry(proxy=key, paused=index in paused)
            for index, key in enumerate(proxies)
        ),
    )
    save_proxy_chains(ProxyChains(proxies=proxies, chains={"nvidia_nim": chain}), path)
    proxy_chains.reset_proxy_chains_cache()


def _chain(payload: dict) -> dict:
    card = next(p for p in payload["providers"] if p["provider_id"] == "nvidia_nim")
    return card["chain"]


def test_entries_that_share_an_exit_form_a_group(_isolate: Path) -> None:
    _seed(_isolate, [WARP, "203.0.113.9", WARP, WARP])

    chain = _chain(_client().get("/admin/api/proxy-chains").json())

    assert chain["exit_groups"] == [
        {
            "exit_ip": WARP,
            "country": "NL",
            "warp": "on",
            "entries": [
                {
                    "index": 1,
                    "proxy": "px_0",
                    "label": "198.51.100.1:1080",
                    "paused": False,
                },
                {
                    "index": 3,
                    "proxy": "px_2",
                    "label": "198.51.100.3:1080",
                    "paused": False,
                },
                {
                    "index": 4,
                    "proxy": "px_3",
                    "label": "198.51.100.4:1080",
                    "paused": False,
                },
            ],
            "direct": False,
        }
    ]
    assert chain["entries"][0]["last_check"]["exit_warp"] == "on"


def test_no_shared_exit_no_new_key(_isolate: Path) -> None:
    _seed(_isolate, [WARP, "203.0.113.9", ""])

    assert "exit_groups" not in _chain(_client().get("/admin/api/proxy-chains").json())


def test_an_exit_shared_with_direct_is_a_group_of_its_own(_isolate: Path) -> None:
    _seed(_isolate, [WARP, "203.0.113.9"])
    record_direct_exit(
        "nvidia_nim",
        DirectExit(at="2026-10-09T10:00:00Z", identity=ExitIdentity(ip="203.0.113.9")),
    )

    payload = _client().get("/admin/api/proxy-chains").json()
    groups = _chain(payload)["exit_groups"]

    assert [(group["exit_ip"], group["direct"]) for group in groups] == [
        ("203.0.113.9", True)
    ]
    card = next(p for p in payload["providers"] if p["provider_id"] == "nvidia_nim")
    assert card["direct_exit"]["ip"] == "203.0.113.9"


def test_keep_one_per_exit_pauses_the_later_duplicates_and_deletes_nothing(
    _isolate: Path,
) -> None:
    _seed(_isolate, [WARP, "203.0.113.9", WARP, WARP, "203.0.113.9"], paused=(0,))

    response = _client().post(
        "/admin/api/proxy-chains/keep-one-per-exit", json={"provider": "nvidia_nim"}
    )

    assert response.status_code == 200, response.text
    outcome = response.json()["kept_one_per_exit"]
    # Entry 1 was already paused: entry 3 is the first LIVE one of its group.
    assert outcome["kept"] == ["198.51.100.3:1080", "198.51.100.2:1080"]
    assert outcome["paused"] == ["198.51.100.4:1080", "198.51.100.5:1080"]
    stored = proxy_chains.load_proxy_chains().chain("nvidia_nim")
    assert stored is not None
    assert [entry.paused for entry in stored.entries] == [
        True,
        False,
        False,
        True,
        True,
    ]
    assert [entry.proxy for entry in stored.entries] == [
        "px_0",
        "px_1",
        "px_2",
        "px_3",
        "px_4",
    ]


def test_keep_one_per_exit_with_nothing_shared_writes_nothing(_isolate: Path) -> None:
    _seed(_isolate, [WARP, "203.0.113.9"])
    before = _isolate.read_bytes()

    response = _client().post(
        "/admin/api/proxy-chains/keep-one-per-exit", json={"provider": "nvidia_nim"}
    )

    assert response.json()["kept_one_per_exit"]["paused"] == []
    assert _isolate.read_bytes() == before


@pytest.mark.parametrize(
    ("value", "mode"),
    [("", None), ("provider", "provider"), ("https://ip.example.org/", "url")],
)
def test_the_checker_says_how_exit_identity_is_configured(
    value: str, mode: str | None
) -> None:
    checker = (
        _client(PROXY_CHECK_EXIT_IP_URL=value)
        .get("/admin/api/proxy-chains")
        .json()["vocabulary"]["checker"]
    )

    assert checker["exit_ip_configured"] is bool(value)
    if mode is None:
        assert "exit_ip_mode" not in checker
    else:
        assert checker["exit_ip_mode"] == mode


def test_the_direct_readout_needs_exit_identity_on() -> None:
    response = _client().post(
        "/admin/api/proxy-chains/direct-exit", json={"provider": "nvidia_nim"}
    )

    assert response.status_code == 422
    assert "PROXY_CHECK_EXIT_IP_URL" in response.json()["detail"]


def test_a_bulk_add_says_when_an_address_shares_an_exit(
    _isolate: Path, monkeypatch
) -> None:
    _seed(_isolate, [WARP])
    store = proxy_chains.load_proxy_chains()
    offered = ProxyEndpoint(url="socks5h://192.0.2.7:1080", label="192.0.2.7:1080")
    save_proxy_chains(store.with_candidates([("px_offer", offered)]), _isolate)
    proxy_chains.reset_proxy_chains_cache()

    async def fake_check(ids, destinations, **kwargs):
        return {
            proxy_id: ProxyCheckOutcome(label="192.0.2.7:1080", record=_check(WARP))
            for proxy_id in ids
        }

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )

    response = _client().post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": ["px_offer"]},
    )

    assert response.status_code == 200, response.text
    row = response.json()["bulk"]["results"][0]
    assert row["outcome"] == "added"
    assert row["shares_exit_with"] == ["198.51.100.1:1080"]
    assert row["exit_ip"] == WARP
