"""7.108.0 (PR-R1...R5): WireGuard runners through the Sources routes.

End to end through the app with no network: importing a file contacts
nothing; the card is masked through and through -- the leak contract of
7.89.0-7.91.0 extended with the WireGuard private key, a preshared key and
the runner's local login, over every response and every log line; Start,
Stop and Restart drive a supervisor (the fake wireproxy under
``sys.executable``); removing a source deletes its config and stops it; every
new route is loopback-only.
"""

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from my_claude_code.application.proxy_runner_state import RUNNERS
from my_claude_code.config import proxy_chains, proxy_sources
from my_claude_code.config.proxy_runner_bins import ResolvedBinary
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.proxy_runners import CONFIG_FILENAME, ProxyRunnerSupervisor
from tests.api.support import create_test_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wireguard"
FAKE = Path(__file__).resolve().parents[1] / "support" / "fake_wireproxy.py"
PSK = "c" * 43 + "="
PROTON = (
    (FIXTURES / "wg-NL-FREE-12.conf")
    .read_text("utf-8")
    .replace("AllowedIPs", f"PresharedKey = {PSK}\nAllowedIPs")
)
MULLVAD = (FIXTURES / "se-sto-wg-001.conf").read_text("utf-8")
KEY = next(
    line.split("=", 1)[1].strip()
    for line in PROTON.splitlines()
    if line.startswith("PrivateKey")
)


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
def supervisor(tmp_path: Path) -> Iterator[ProxyRunnerSupervisor]:
    made = ProxyRunnerSupervisor(
        resolve=lambda: ResolvedBinary(
            Path(sys.executable), "test", "1.1.3", leading=(str(FAKE),)
        ),
        base_dir=lambda: tmp_path / "runners",
        register=lambda pid: None,
        unregister=lambda pid: None,
        ready_poll_seconds=0.05,
    )
    RUNNERS.attach(made)
    yield made
    made.stop_all()
    RUNNERS.detach()


def _client(host: str = "127.0.0.1", ports: str = "41000-41099") -> TestClient:
    settings = Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "PROXY_RUNNER_PORTS": ports,
        }
    )
    return TestClient(create_test_app(settings), client=(host, 50000))


def _import(client: TestClient, text: str = PROTON, **block: object) -> dict:
    response = client.put(
        "/admin/api/proxy-sources",
        json={
            "source": "",
            "kind": "runner",
            "runner": {"profile": text, "filename": "wg-NL-FREE-12.conf"} | block,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _card(document: dict) -> dict:
    return next(
        source
        for source in document["sources"]["sources"]
        if source["kind"] == "runner"
    )


def _secrets() -> tuple[str, ...]:
    table = proxy_sources.load_proxy_sources()
    found = [KEY, KEY[:8], PSK]
    for secret in table.secrets.values():
        if secret.type == "userpass":
            found += [secret.username, secret.password]
    return tuple(found)


def test_importing_contacts_nothing_and_offers_one_named_address(logged) -> None:
    document = _import(_client())

    card = _card(document)
    assert card["name"] == "Proton NL-FREE#12"
    assert card["vendor"] == "proton"
    assert card["status"]["state"] == "stopped"
    assert card["auto_restart"] is False
    assert [offer["label"] for offer in card["offers"]] == ["Proton · NL-FREE#12"]
    assert card["offers"][0]["offered"] is True
    assert document["runner_result"]["action"] == "saved"
    assert "Nothing was contacted" in document["runner_result"]["sentence"]
    assert any(
        "WireGuard runner src_runner_proton_nl_free_12 saved" in line for line in logged
    )


def test_no_response_log_line_or_chain_file_carries_a_runner_secret(
    supervisor: ProxyRunnerSupervisor, logged
) -> None:
    client = _client()
    texts = [
        client.put(
            "/admin/api/proxy-sources",
            json={"source": "", "kind": "runner", "runner": {"profile": PROTON}},
        ).text
    ]
    source = "src_runner_proton_nl_free_12"
    texts.append(
        client.post(
            "/admin/api/proxy-sources/runner/start", json={"source": source}
        ).text
    )
    deadline = time.monotonic() + 20
    while (RUNNERS.reading(source) or supervisor.start(source)).state != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    texts += [
        client.get("/admin/api/proxy-sources").text,
        client.get("/admin/api/proxy-chains").text,
        client.get("/admin/api/proxy-sources/runner-binary").text,
        client.put(
            "/admin/api/proxy-sources",
            json={"source": source, "kind": "runner", "runner": {"auto_restart": True}},
        ).text,
        client.post(
            "/admin/api/proxy-sources/runner/stop", json={"source": source}
        ).text,
    ]

    secrets = _secrets()
    assert len(secrets) == 5
    for secret in secrets:
        for text in texts:
            assert secret not in text, secret
        assert not any(secret in line for line in logged), secret
    # Where they live: the owner-only store holds the key; the chain file holds
    # the local login in the address's URL (option A) and never the key.
    stored = proxy_sources.proxy_sources_path().read_text("utf-8")
    assert KEY in stored and PSK in stored
    chain_file = proxy_chains.proxy_chains_path().read_text("utf-8")
    assert KEY not in chain_file and PSK not in chain_file
    assert secrets[3] in chain_file


def test_start_stop_restart_drive_the_supervisor_and_record_enabled(
    supervisor: ProxyRunnerSupervisor, tmp_path: Path
) -> None:
    client = _client()
    _import(client)
    source = "src_runner_proton_nl_free_12"

    started = client.post(
        "/admin/api/proxy-sources/runner/start", json={"source": source}
    )
    assert started.status_code == 200, started.text
    assert started.json()["runner_result"]["state"] == "starting"
    assert proxy_sources.load_proxy_sources().sources[source].enabled is True
    config = tmp_path / "runners" / source / CONFIG_FILENAME
    assert KEY in config.read_text("utf-8")
    deadline = time.monotonic() + 20
    while (RUNNERS.reading(source) or supervisor.start(source)).state != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    card = _card(client.get("/admin/api/proxy-chains").json())
    assert card["status"]["state"] == "ready"
    assert card["status"]["pid"] == supervisor.running_pid(source)

    restarted = client.post(
        "/admin/api/proxy-sources/runner/restart", json={"source": source}
    )
    assert restarted.json()["runner_result"]["state"] == "starting"

    stopped = client.post(
        "/admin/api/proxy-sources/runner/stop", json={"source": source}
    )
    assert stopped.json()["runner_result"]["state"] == "stopped"
    assert proxy_sources.load_proxy_sources().sources[source].enabled is False
    assert supervisor.running_pid(source) == 0


def test_removing_a_runner_stops_it_deletes_its_config_and_withdraws_offers(
    supervisor: ProxyRunnerSupervisor, tmp_path: Path
) -> None:
    client = _client()
    _import(client)
    source = "src_runner_proton_nl_free_12"
    client.post("/admin/api/proxy-sources/runner/start", json={"source": source})
    assert (tmp_path / "runners" / source / CONFIG_FILENAME).exists()

    response = client.put(
        "/admin/api/proxy-sources", json={"source": source, "remove": True}
    )

    assert response.status_code == 200
    assert response.json()["runner_result"]["action"] == "removed"
    assert not (tmp_path / "runners" / source).exists()
    assert supervisor.running_pid(source) == 0
    stored = proxy_sources.load_proxy_sources()
    assert stored.sources == {} and stored.secrets == {}
    assert "sources" not in response.json()


def test_a_button_with_no_supervisor_is_refused_and_starts_nothing() -> None:
    client = _client()
    _import(client)

    response = client.post(
        "/admin/api/proxy-sources/runner/start",
        json={"source": "src_runner_proton_nl_free_12"},
    )

    assert response.status_code == 503
    assert "nothing was started" in response.json()["detail"]
    missing = client.post(
        "/admin/api/proxy-sources/runner/stop", json={"source": "src_nope"}
    )
    assert missing.status_code == 404


def test_mullvad_exits_are_offered_one_address_each() -> None:
    document = _import(
        _client(),
        MULLVAD,
        filename="se-sto-wg-001.conf",
        vendor="mullvad",
        exits="se-sto-wg-socks5-001.relays.mullvad.net\nnl-ams-wg-socks5-002.relays.mullvad.net",
    )

    card = _card(document)
    assert [offer["role"] for offer in card["offers"]] == ["socks", "exit", "exit"]
    assert [offer["label"] for offer in card["offers"][1:]] == [
        "Mullvad · se-sto-wg-socks5-001",
        "Mullvad · nl-ams-wg-socks5-002",
    ]
    assert card["offers"][1]["login"] is False
    assert any("takes no login" in line for line in card["lines"])
    assert any("UNVERIFIED" in line for line in card["lines"])
    assert card["in_tunnel_shape"].endswith("relays.mullvad.net:1080")


def test_a_bad_file_or_a_full_port_range_is_a_422_with_a_sentence() -> None:
    client = _client()
    bad = client.put(
        "/admin/api/proxy-sources",
        json={"source": "", "kind": "runner", "runner": {"profile": "[Interface]\n"}},
    )
    assert bad.status_code == 422
    assert "PrivateKey" in bad.json()["detail"] or "[Peer]" in bad.json()["detail"]

    full = _client(ports="1024-1024").put(
        "/admin/api/proxy-sources",
        json={"source": "", "kind": "runner", "runner": {"profile": PROTON}},
    )
    assert full.status_code == 422
    assert "PROXY_RUNNER_PORTS" in full.json()["detail"]


def test_the_binary_routes_never_download_unasked_and_refuse_a_bad_path(
    tmp_path: Path,
) -> None:
    client = _client()
    status = client.get("/admin/api/proxy-sources/runner-binary").json()
    assert status["binary"]["version"] == "1.1.3"
    assert status["binary"]["ready"] is False
    assert [vendor["id"] for vendor in status["vendors"]][:2] == ["proton", "mullvad"]

    # The suite refuses every download, so a press reports why, as a sentence.
    pressed = client.post("/admin/api/proxy-sources/runner-binary/install").json()
    assert pressed["install_result"]["ok"] is False
    assert pressed["install_result"]["sentence"]

    own = client.post(
        "/admin/api/proxy-sources/runner-binary/own",
        json={"path": str(tmp_path / "none")},
    )
    assert own.status_code == 422
    assert "There is no file" in own.json()["detail"]


def test_an_old_runner_document_is_kept_and_not_edited() -> None:
    proxy_sources.proxy_sources_path().parent.mkdir(parents=True, exist_ok=True)
    proxy_sources.proxy_sources_path().write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "src_proton": {
                        "kind": "runner",
                        "runner": "wireproxy",
                        "name": "Proton NL-FREE#12",
                        "enabled": False,
                        "ports": [41001],
                        "secret": "sec_2222",
                    }
                },
                "secrets": {
                    "sec_2222": {"type": "userpass", "username": "u", "password": "p"}
                },
            }
        ),
        encoding="utf-8",
    )
    proxy_sources.reset_proxy_sources_cache()
    client = _client()

    card = _card(client.get("/admin/api/proxy-chains").json())
    assert card["built"] is False
    edit = client.put(
        "/admin/api/proxy-sources",
        json={"source": "src_proton", "kind": "runner", "runner": {"profile": PROTON}},
    )
    assert edit.status_code == 422
    assert "not a WireGuard runner" in edit.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/admin/api/proxy-sources/runner/start", {"source": "x"}),
        ("post", "/admin/api/proxy-sources/runner/stop", {"source": "x"}),
        ("post", "/admin/api/proxy-sources/runner/restart", {"source": "x"}),
        ("get", "/admin/api/proxy-sources/runner-binary", None),
        ("post", "/admin/api/proxy-sources/runner-binary/install", None),
        ("post", "/admin/api/proxy-sources/runner-binary/own", {"path": ""}),
        (
            "put",
            "/admin/api/proxy-sources",
            {"source": "", "kind": "runner", "runner": {"profile": PROTON}},
        ),
    ],
)
def test_every_runner_route_is_loopback_only(method: str, path: str, body) -> None:
    client = _client(host="192.0.2.10")
    call = getattr(client, method)
    response = call(path, json=body) if body is not None else call(path)
    assert response.status_code == 403
