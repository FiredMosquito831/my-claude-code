"""The check route, and the refusal it can produce.

``tests/application/test_proxy_check_tls.py`` proves the measurement against a
MITM proxy it stands up itself. This file is about the other half: what the
route does with a verdict once there is one, and specifically that an address
marked ``intercepted`` cannot be written into a chain through this surface. No
socket is opened here -- the checker is stubbed, deliberately, so a failure in
this file means the route is wrong rather than that a port was busy.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.proxy_check import ProxyCheckOutcome
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
    ProxyCheckRecord,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import PROXY_INTERCEPTION, reset_proxy_health
from tests.api.support import create_test_app

SECRET_URL = "socks5h://alice:hunter2@203.0.113.7:1080"
SECOND_URL = "http://198.51.100.9:8080"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    reset_proxy_health()
    yield path
    proxy_chains.reset_proxy_chains_cache()
    reset_proxy_health()


def _settings(**extra) -> Settings:
    return Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            **extra,
        }
    )


def _client(settings: Settings | None = None) -> TestClient:
    return TestClient(
        create_test_app(settings or _settings()), client=("127.0.0.1", 50000)
    )


def _seed(client: TestClient, *urls: str) -> dict:
    """Save a chain of the given addresses and return the refreshed payload."""

    response = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "round_robin",
            "scope": "provider",
            "max_switches": 2,
            "on": ["quota", "rate_limit", "timeout"],
            "entries": [{"url": url} for url in urls],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _entries(payload: dict) -> list[dict]:
    provider = next(
        entry for entry in payload["providers"] if entry["provider_id"] == "nvidia_nim"
    )
    return provider["chain"]["entries"]


def _stub_checker(monkeypatch, verdicts: dict[str, ProxyCheckRecord]) -> list[dict]:
    """Replace the network with a table, and record what was asked for.

    Keyed by the address's masked label, because that is the only name both the
    store and the ledgers agree on.
    """

    calls: list[dict] = []

    async def fake_check_endpoints(
        proxy_ids, destinations, *, timeout=0.0, exit_ip_url="", persist=True
    ):
        from my_claude_code.application.proxy_check import apply_outcome
        from my_claude_code.config.credentials import mask_proxy_label

        store = load_proxy_chains()
        outcomes: dict[str, ProxyCheckOutcome] = {}
        for proxy_id in proxy_ids:
            endpoint = store.endpoint(proxy_id)
            if endpoint is None:
                continue
            label = mask_proxy_label(endpoint.url)
            calls.append(
                {
                    "proxy": proxy_id,
                    "label": label,
                    "destination": destinations.get(proxy_id, ""),
                    "exit_ip_url": exit_ip_url,
                }
            )
            record = verdicts.get(label, ProxyCheckRecord(ok=True, tls=TLS_STRICT))
            apply_outcome(label, record)
            outcomes[proxy_id] = ProxyCheckOutcome(label=label, record=record)
        fresh = load_proxy_chains()
        for proxy_id, outcome in outcomes.items():
            fresh = fresh.with_check(proxy_id, outcome.record)
        save_proxy_chains(fresh)
        return outcomes

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check_endpoints
    )
    return calls


def test_a_check_aims_at_the_providers_own_host(monkeypatch) -> None:
    """Never a third-party echo service.

    The destination has to be the thing the chain will actually talk to, and it
    is a host the operator already chose to talk to. Anything else would make
    the check both less relevant and an outbound call they did not ask for.
    """

    client = _client()
    _seed(client, SECRET_URL)
    calls = _stub_checker(monkeypatch, {})

    response = client.post(
        "/admin/api/proxy-chains/check", json={"provider": "nvidia_nim"}
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0]["destination"].startswith("https://")
    assert "integrate.api.nvidia.com" in calls[0]["destination"]
    # No exit-IP URL configured, so nobody outside the provider is contacted.
    assert calls[0]["exit_ip_url"] == ""


def test_the_operators_exit_ip_url_is_passed_only_when_they_set_one(
    monkeypatch,
) -> None:
    client = _client(_settings(PROXY_CHECK_EXIT_IP_URL="https://example.org/ip"))
    _seed(client, SECRET_URL)
    calls = _stub_checker(monkeypatch, {})

    client.post("/admin/api/proxy-chains/check", json={"provider": "nvidia_nim"})

    assert calls[0]["exit_ip_url"] == "https://example.org/ip"


def test_testing_one_row_measures_only_that_address(monkeypatch) -> None:
    client = _client()
    payload = _seed(client, SECRET_URL, SECOND_URL)
    second = _entries(payload)[1]["proxy"]
    calls = _stub_checker(monkeypatch, {})

    client.post(
        "/admin/api/proxy-chains/check",
        json={"provider": "nvidia_nim", "proxy": second},
    )

    assert [call["proxy"] for call in calls] == [second]


def test_a_verdict_is_stored_and_read_back_on_the_row(monkeypatch) -> None:
    client = _client()
    _seed(client, SECRET_URL)
    _stub_checker(
        monkeypatch,
        {"203.0.113.7:1080": ProxyCheckRecord(ok=True, latency_ms=412, tls=TLS_STRICT)},
    )

    body = client.post(
        "/admin/api/proxy-chains/check", json={"provider": "nvidia_nim"}
    ).json()

    entry = _entries(body)[0]
    assert entry["last_check"]["tls"] == TLS_STRICT
    assert entry["last_check"]["latency_ms"] == 412
    assert entry["refused"] is False
    assert body["checked"][entry["proxy"]]["label"] == "203.0.113.7:1080"


def test_an_intercepted_address_cannot_be_saved_into_a_chain(monkeypatch) -> None:
    """The security control, at the one moment it matters.

    The write that would put this address in front of a credential is the write
    that is refused, and the message says why rather than reporting a validation
    error.
    """

    client = _client()
    payload = _seed(client, SECRET_URL, SECOND_URL)
    entries = _entries(payload)
    _stub_checker(
        monkeypatch,
        {
            "198.51.100.9:8080": ProxyCheckRecord(
                ok=False,
                tls=TLS_INTERCEPTED,
                detail="this proxy breaks certificate validation",
            )
        },
    )
    client.post(
        "/admin/api/proxy-chains/check",
        json={"provider": "nvidia_nim", "proxy": entries[1]["proxy"]},
    )

    refused = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "round_robin",
            "scope": "provider",
            "max_switches": 2,
            "on": ["quota"],
            "entries": [
                {"proxy": entries[0]["proxy"]},
                {"proxy": entries[1]["proxy"]},
            ],
        },
    )

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "198.51.100.9:8080" in detail
    assert "breaks certificate validation" in detail
    assert "reading the traffic" in detail
    # And the refusal never names the password that is in the other address.
    assert "hunter2" not in detail


def test_a_chain_without_the_intercepted_address_still_saves(monkeypatch) -> None:
    """The refusal is about one entry, not about the provider.

    An operator told "no" has to be able to act on it, and the action is
    removing that one row.
    """

    client = _client()
    payload = _seed(client, SECRET_URL, SECOND_URL)
    entries = _entries(payload)
    _stub_checker(
        monkeypatch,
        {"198.51.100.9:8080": ProxyCheckRecord(ok=False, tls=TLS_INTERCEPTED)},
    )
    client.post(
        "/admin/api/proxy-chains/check",
        json={"provider": "nvidia_nim", "proxy": entries[1]["proxy"]},
    )

    saved = client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "round_robin",
            "scope": "provider",
            "max_switches": 2,
            "on": ["quota"],
            "entries": [{"proxy": entries[0]["proxy"]}, {"direct": True}],
        },
    )

    assert saved.status_code == 200, saved.text
    assert len(_entries(saved.json())) == 2


def test_a_refusal_is_armed_in_the_ledger_the_runtime_reads(monkeypatch) -> None:
    """A verdict the page shows and the request path ignores would be theatre."""

    client = _client()
    payload = _seed(client, SECOND_URL)
    _stub_checker(
        monkeypatch,
        {"198.51.100.9:8080": ProxyCheckRecord(ok=False, tls=TLS_INTERCEPTED)},
    )

    client.post(
        "/admin/api/proxy-chains/check",
        json={"provider": "nvidia_nim", "proxy": _entries(payload)[0]["proxy"]},
    )

    assert PROXY_INTERCEPTION.is_refused("198.51.100.9:8080") is True


def test_a_check_response_never_carries_a_proxy_password(monkeypatch) -> None:
    """The whole body is searched, not the field it should be in."""

    client = _client()
    _seed(client, SECRET_URL)
    _stub_checker(monkeypatch, {})

    body = client.post(
        "/admin/api/proxy-chains/check", json={"provider": "nvidia_nim"}
    ).text

    assert "hunter2" not in body
    assert "alice" not in body
    assert "203.0.113.7:1080" in body


def test_checking_an_unconfigured_provider_is_a_404() -> None:
    response = _client().post(
        "/admin/api/proxy-chains/check", json={"provider": "open_router"}
    )

    assert response.status_code == 404


def test_checking_a_provider_with_no_chain_says_what_to_do() -> None:
    """Nothing to measure is a message, not a silent empty result."""

    response = _client().post(
        "/admin/api/proxy-chains/check", json={"provider": "nvidia_nim"}
    )

    assert response.status_code == 422
    assert "saved before it can be measured" in response.json()["detail"]


def test_the_page_is_told_whether_anything_is_checking() -> None:
    """Off is the shipped answer, and the page has to be able to say so."""

    payload = _client().get("/admin/api/proxy-chains").json()
    checker = payload["vocabulary"]["checker"]

    assert checker["enabled"] is False
    assert checker["exit_ip_configured"] is False
    assert checker["interval_minutes"] == 30
