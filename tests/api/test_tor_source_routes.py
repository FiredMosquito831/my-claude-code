"""Bring-your-own Tor through the Sources routes (7.90.0).

A fake control port stands in for the user's tor (no real tor is ever started
-- by MCC or by a test). Pinned here, end to end through the app:

* saving a Tor source contacts nothing and offers one named address per port;
* *Check Tor* and *New Tor identity* reach the control port, and a second
  identity within 10 s is refused before anything is sent, with the seconds;
* the cookie file is read at the moment of each press and its bytes never
  land in ``proxy_sources.json``, ``proxy_chains.json``, a response or a log;
* a control password is stored owner-only and appears nowhere else;
* the buttons are loopback-only, like every Sources route.
"""

import base64
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from my_claude_code.application.proxy_check import ProxyCheckOutcome
from my_claude_code.application.tor_source import tor_offer_id
from my_claude_code.config import proxy_chains, proxy_sources
from my_claude_code.config.proxy_chains import ProxyCheckRecord
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app
from tests.support.fake_tor_control import FakeTorControl, run_fake_tor

PORTS = [19250, 19251]
PASSWORD = "tor-ctl-password-6620"

pytestmark = pytest.mark.local_serial


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
def tor(tmp_path: Path) -> Iterator[FakeTorControl]:
    fake = FakeTorControl(
        cookie_path=tmp_path / "tor-data" / "control_auth_cookie",
        socks_ports=tuple(PORTS),
    )
    with run_fake_tor(fake) as running:
        yield running


def _client(host: str = "127.0.0.1") -> TestClient:
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"}
    )
    return TestClient(create_test_app(settings), client=(host, 50000))


def _create(client: TestClient, control: int, **tor: object) -> dict:
    response = client.put(
        "/admin/api/proxy-sources",
        json={
            "source": "",
            "kind": "tor",
            "tor": {"socks_ports": PORTS, "control_port": control} | tor,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tor_card(payload: dict) -> dict:
    return next(
        source for source in payload["sources"]["sources"] if source["kind"] == "tor"
    )


def test_saving_contacts_nothing_and_offers_one_named_address_per_port(
    tor: FakeTorControl,
) -> None:
    client = _client()

    payload = _create(client, tor.port)

    assert tor.connections == 0, "a save dialled the control port"
    card = _tor_card(payload)
    assert card["id"] == "src_tor"
    assert [row["label"] for row in card["ports"]] == [
        "Tor · 127.0.0.1:19250",
        "Tor · 127.0.0.1:19251",
    ]
    assert [row["proxy"] for row in card["ports"]] == [
        tor_offer_id("src_tor", port) for port in PORTS
    ]
    assert card["torrc"].splitlines()[1:] == [
        "SocksPort 127.0.0.1:19250",
        "SocksPort 127.0.0.1:19251",
        f"ControlPort 127.0.0.1:{tor.port}",
        "CookieAuthentication 1",
    ]
    assert payload["tor_result"]["action"] == "saved"
    assert (
        client.get("/admin/api/proxy-sources").json()["sources"]
        == (payload["sources"]["sources"])
    )


def test_check_tor_reads_the_card_s_facts(tor: FakeTorControl) -> None:
    client = _client()
    _create(client, tor.port)

    response = client.post(
        "/admin/api/proxy-sources/tor/status", json={"source": "src_tor"}
    )

    assert response.status_code == 200, response.text
    result = response.json()["tor_result"]
    assert result == {
        "action": "status",
        "source": "src_tor",
        "ok": True,
        "sentence": "Tor 0.4.8.13 · circuit established · logged in with the "
        "cookie (SAFECOOKIE).",
    }
    status = _tor_card(response.json())["status"]
    assert status["circuit_established"] is True
    assert status["socks_listeners"] == PORTS
    assert tor.connections == 1
    assert tor.newnyms == 0, "Check Tor asked for a new identity"


def test_a_second_new_identity_within_10_s_is_refused_without_sending(
    tor: FakeTorControl,
) -> None:
    client = _client()
    _create(client, tor.port)

    first = client.post(
        "/admin/api/proxy-sources/tor/newnym", json={"source": "src_tor"}
    )
    connections = tor.connections
    second = client.post(
        "/admin/api/proxy-sources/tor/newnym", json={"source": "src_tor"}
    )

    assert first.json()["tor_result"]["accepted"] is True
    assert tor.newnyms == 1
    refused = second.json()["tor_result"]
    assert refused["accepted"] is False
    assert refused["refused_locally"] is True
    assert 1 <= refused["wait_seconds"] <= 10
    assert f"try again in {refused['wait_seconds']} s" in refused["sentence"]
    assert tor.connections == connections, "the refused press reached tor"
    card = _tor_card(second.json())
    assert card["newnym"]["wait_seconds"] >= 1


def test_the_cookie_is_read_at_each_press_and_never_stored_shown_or_logged(
    tor: FakeTorControl, logged: list[str], _chains: Path
) -> None:
    client = _client()
    texts = [json.dumps(_create(client, tor.port))]
    cookies = [tor.cookie]
    for action in ("status", "newnym", "status"):
        response = client.post(
            f"/admin/api/proxy-sources/tor/{action}", json={"source": "src_tor"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["tor_result"].get("ok", True) is True, response.text
        texts.append(response.text)
        # Tor writes a new cookie every time it starts: a remembered one fails.
        cookies.append(tor.rotate_cookie())
    texts.append(client.get("/admin/api/proxy-sources").text)
    texts.append(client.get("/admin/api/proxy-chains").text)

    assert tor.logins == ["SAFECOOKIE"] * 3
    stores = [
        proxy_sources.proxy_sources_path().read_bytes(),
        _chains.read_bytes(),
    ]
    for cookie in cookies:
        forms = [
            cookie.hex(),
            cookie.hex().upper(),
            base64.b64encode(cookie).decode(),
        ]
        for store in stores:
            assert cookie not in store
            assert not any(form.encode() in store for form in forms)
        for text in texts:
            assert not any(form in text for form in forms)
        for line in logged:
            assert not any(form in line for form in forms)


def test_a_control_password_is_stored_owner_only_and_shown_nowhere(
    tmp_path: Path, logged: list[str], _chains: Path
) -> None:
    fake = FakeTorControl(
        cookie_path=tmp_path / "tor-data" / "control_auth_cookie",
        methods=("HASHEDPASSWORD",),
        password=PASSWORD,
    )
    with run_fake_tor(fake):
        client = _client()
        texts = [
            json.dumps(_create(client, fake.port, auth="password", password=PASSWORD))
        ]
        status = client.post(
            "/admin/api/proxy-sources/tor/status", json={"source": "src_tor"}
        )
        texts += [status.text, client.get("/admin/api/proxy-sources").text]

    assert status.json()["tor_result"]["ok"] is True
    assert fake.logins == ["HASHEDPASSWORD"]
    card = _tor_card(status.json())
    assert card["secret_set"] is True
    assert card["auth"] == "password"
    for text in texts:
        assert PASSWORD not in text
        assert PASSWORD.encode().hex() not in text
    assert not any(PASSWORD in line for line in logged)
    assert PASSWORD.encode() not in _chains.read_bytes()
    stored = json.loads(proxy_sources.proxy_sources_path().read_text("utf-8"))
    assert list(stored["secrets"].values()) == [
        {"type": "password", "password": PASSWORD}
    ]


def test_a_saved_password_is_kept_when_the_ports_change(
    tmp_path: Path,
) -> None:
    fake = FakeTorControl(
        cookie_path=tmp_path / "c" / "control_auth_cookie",
        methods=("HASHEDPASSWORD",),
        password=PASSWORD,
    )
    with run_fake_tor(fake):
        client = _client()
        _create(client, fake.port, auth="password", password=PASSWORD)
        changed = client.put(
            "/admin/api/proxy-sources",
            json={
                "source": "src_tor",
                "tor": {
                    "socks_ports": [19250],
                    "control_port": fake.port,
                    "auth": "password",
                },
            },
        )
        status = client.post(
            "/admin/api/proxy-sources/tor/status", json={"source": "src_tor"}
        )

    assert changed.status_code == 200, changed.text
    assert [row["port"] for row in _tor_card(changed.json())["ports"]] == [19250]
    assert status.json()["tor_result"]["ok"] is True


@pytest.mark.parametrize(
    "path",
    [
        "/admin/api/proxy-sources/tor/status",
        "/admin/api/proxy-sources/tor/newnym",
    ],
)
def test_the_buttons_are_loopback_only(tor: FakeTorControl, path: str) -> None:
    _create(_client(), tor.port)

    response = _client("203.0.113.9").post(path, json={"source": "src_tor"})

    assert response.status_code == 403
    assert tor.connections == 0


def test_a_save_from_another_computer_is_refused(tor: FakeTorControl) -> None:
    response = _client("203.0.113.9").put(
        "/admin/api/proxy-sources",
        json={
            "source": "",
            "kind": "tor",
            "tor": {"socks_ports": PORTS, "control_port": tor.port},
        },
    )

    assert response.status_code == 403
    assert not proxy_sources.proxy_sources_path().exists()


def test_a_bad_form_is_a_422_with_a_sentence() -> None:
    response = _client().put(
        "/admin/api/proxy-sources",
        json={
            "source": "",
            "kind": "tor",
            "tor": {"socks_ports": [19250], "control_port": 19250},
        },
    )

    assert response.status_code == 422
    assert (
        "cannot be both a SOCKS port and the control port"
        in (response.json()["detail"])
    )


def test_a_button_for_no_tor_source_is_a_404() -> None:
    response = _client().post(
        "/admin/api/proxy-sources/tor/newnym", json={"source": "src_local"}
    )

    assert response.status_code == 404
    assert "No Tor source" in response.json()["detail"]


def test_a_tor_source_takes_no_listener_login(tor: FakeTorControl) -> None:
    client = _client()
    _create(client, tor.port)

    response = client.put(
        "/admin/api/proxy-sources",
        json={
            "source": "src_tor",
            "credentials": [{"port": 19250, "username": "u", "password": "p"}],
        },
    )

    assert response.status_code == 422
    assert "no listener logins" in response.json()["detail"]


def test_removing_it_withdraws_its_offers_and_forgets_its_reading(
    tor: FakeTorControl,
) -> None:
    client = _client()
    _create(client, tor.port)
    client.post("/admin/api/proxy-sources/tor/status", json={"source": "src_tor"})

    payload = client.put(
        "/admin/api/proxy-sources", json={"source": "src_tor", "remove": True}
    ).json()

    assert "sources" not in payload
    assert proxy_chains.load_proxy_chains().source_offers == {}


def test_tor_ports_go_into_a_chain_through_the_bulk_add_with_their_own_names(
    tor: FakeTorControl, monkeypatch
) -> None:
    client = _client()
    _create(client, tor.port)
    ids = [tor_offer_id("src_tor", port) for port in PORTS]

    async def fake_check(checked, destinations, **kwargs):
        return {
            proxy_id: ProxyCheckOutcome(
                label=proxy_id,
                record=ProxyCheckRecord(
                    at="2026-10-09T12:00:00Z", ok=True, tls="strict"
                ),
            )
            for proxy_id in checked
        }

    monkeypatch.setattr(
        "my_claude_code.api.admin_proxy_routes.check_endpoints", fake_check
    )
    response = client.post(
        "/admin/api/proxy-chains/candidates/bulk",
        json={"action": "add", "provider": "nvidia_nim", "proxies": ids},
    )

    assert response.status_code == 200, response.text
    card = next(
        p for p in response.json()["providers"] if p["provider_id"] == "nvidia_nim"
    )
    assert [entry["label"] for entry in card["chain"]["entries"]] == [
        "Tor · 127.0.0.1:19250",
        "Tor · 127.0.0.1:19251",
    ]
    rows = _tor_card(response.json())["ports"]
    assert [row["chained"] for row in rows] == [["nvidia_nim"], ["nvidia_nim"]]
    assert tor.connections == 0
