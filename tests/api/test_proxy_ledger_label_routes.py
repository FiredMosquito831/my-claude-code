"""PR-S1 (7.89.0) on the Proxying page: colliding addresses are told apart on
the page, a chain whose entries would share a name is refused, and a chain
write rebuilds every provider whose labels it renamed."""

import asyncio
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from my_claude_code.api.admin_proxy_routes import republish_chains
from my_claude_code.api.ports import AdminRuntimePort
from my_claude_code.config import proxy_chains
from my_claude_code.config.proxy_chains import (
    note_built_chain_labels,
    reset_built_chain_labels,
)
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

SESSION_ONE = "socks5h://customer-a-sessid-1:pw@gw.example:7777"
SESSION_TWO = "socks5h://customer-a-sessid-2:pw@gw.example:7777"


@pytest.fixture(autouse=True)
def _isolate_chain_store(monkeypatch, tmp_path: Path):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    reset_built_chain_labels()
    yield path
    proxy_chains.reset_proxy_chains_cache()
    reset_built_chain_labels()


def _client() -> TestClient:
    settings = Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "OPENCODE_API_KEY": "sk-opencode-fake-0000",
        }
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _put(client: TestClient, provider: str, entries: list[dict]) -> Response:
    return client.put(
        "/admin/api/proxy-chains",
        json={"provider": provider, "enabled": True, "entries": entries},
    )


def _card(payload: dict, provider: str) -> dict:
    return next(
        entry for entry in payload["providers"] if entry["provider_id"] == provider
    )


def test_two_sessions_on_one_gateway_save_and_show_two_names() -> None:
    client = _client()
    response = _put(client, "nvidia_nim", [{"url": SESSION_ONE}, {"url": SESSION_TWO}])
    assert response.status_code == 200, response.text

    entries = _card(response.json(), "nvidia_nim")["chain"]["entries"]
    labels = [entry["label"] for entry in entries]
    assert labels[0] != labels[1]
    assert all(label.startswith("gw.example:7777#") for label in labels)
    assert all(entry.get("label_disambiguated") is True for entry in entries)
    assert "sessid" not in response.text and "pw@" not in response.text


def test_the_same_address_twice_is_refused_with_a_sentence_naming_both() -> None:
    client = _client()
    response = _put(client, "nvidia_nim", [{"url": SESSION_ONE}, {"url": SESSION_ONE}])

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("Not saved: Entries 1 and 2 would both go by")
    assert "the same address" in detail
    assert "sessid" not in detail and "pw" not in detail.split("go by")[1][:30]
    # Nothing was written.
    assert proxy_chains.load_proxy_chains().chain("nvidia_nim") is None


def test_two_direct_entries_still_save() -> None:
    client = _client()
    response = _put(
        client,
        "nvidia_nim",
        [{"url": SESSION_ONE}, {"direct": True}, {"direct": True}],
    )

    assert response.status_code == 200, response.text


def test_an_address_without_a_collision_has_no_new_key() -> None:
    client = _client()
    response = _put(client, "nvidia_nim", [{"url": SESSION_ONE}])
    entry = _card(response.json(), "nvidia_nim")["chain"]["entries"][0]

    assert entry["label"] == "gw.example:7777"
    assert "label_disambiguated" not in entry


def test_a_write_that_renames_another_chain_s_address_rebuilds_that_provider() -> None:
    client = _client()
    assert _put(client, "opencode", [{"url": SESSION_ONE}]).status_code == 200
    store = proxy_chains.load_proxy_chains()
    note_built_chain_labels("opencode", store.chain_ledger_labels("opencode"))
    assert _put(client, "nvidia_nim", [{"url": SESSION_TWO}]).status_code == 200

    asked: list[object] = []

    class _Admin:
        async def reload_providers(self, reason, *, sweep, rebuild_provider_ids):
            asked.append(rebuild_provider_ids)

    failure = asyncio.run(
        republish_chains(cast(AdminRuntimePort, _Admin()), {"nvidia_nim"})
    )
    assert failure == ""
    assert asked == [frozenset({"nvidia_nim", "opencode"})]
