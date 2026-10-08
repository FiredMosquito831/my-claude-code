"""What the Proxying page and the dashboard say about how a chain routes (7.78.8).

Three states the page used to leave unsaid:

* **refused** (C-2) -- Direct fallback off and nothing in the chain to route
  through: the card shows the sentence every request to it is refused with;
* **saved, not routing yet** (C-6) -- the chain is on disk but the provider
  could not be rebuilt, so the one built before the save still routes: the
  save answers so, the card keeps saying so until a rebuild succeeds, and the
  log line names the providers;
* **the chain file cannot be read** (C-5) -- a red banner on every page (the
  config-dir status the dashboard loads at start, and the Proxying payload),
  and every save refused with a 503 that names the file, never written.

And one the page keeps: a card with nothing to say carries none of the new
keys, so every healthy install's payload is what it was.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_proxy_routes
from my_claude_code.config import proxy_chains
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, runtime_for_app

NEW_KEYS = ("refusal", "not_routing", "chain_inert")
SECRET_URL = "socks5h://alice:hunter2@203.0.113.7:1080"


@pytest.fixture(autouse=True)
def chains_path(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    proxy_chains.reset_proxy_chains_cache()
    admin_proxy_routes._UNROUTED.clear()
    yield path
    proxy_chains.reset_proxy_chains_cache()
    admin_proxy_routes._UNROUTED.clear()


def _settings(proxy: str = "") -> Settings:
    return Settings.model_validate(
        {
            "model": "nvidia_nim/primary",
            "nvidia_nim_api_key": "nim-key",
            "NVIDIA_NIM_PROXY": proxy,
        }
    )


def _client(settings: Settings | None = None) -> TestClient:
    return TestClient(
        create_test_app(settings or _settings()), client=("127.0.0.1", 50000)
    )


def _card(payload: dict[str, Any], provider_id: str = "nvidia_nim") -> dict[str, Any]:
    return next(
        entry for entry in payload["providers"] if entry["provider_id"] == provider_id
    )


def _store(*, paused: bool, direct_fallback: bool) -> None:
    save_proxy_chains(
        ProxyChains(
            proxies={"px_one": ProxyEndpoint(url="http://198.51.100.9:8080")},
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=True,
                    entries=(ProxyChainEntry(proxy="px_one", paused=paused),),
                    direct_fallback=direct_fallback,
                )
            },
        )
    )


def _put(client: TestClient, *, paused: bool, direct_fallback: bool) -> Any:
    return client.put(
        "/admin/api/proxy-chains",
        json={
            "provider": "nvidia_nim",
            "enabled": True,
            "policy": "failover",
            "scope": "provider",
            "max_switches": 2,
            "direct_fallback": direct_fallback,
            "on": ["quota"],
            "oauth_acknowledged": False,
            "order_by_speed": False,
            "entries": [{"url": "http://198.51.100.9:8080", "paused": paused}],
        },
    )


# ------------------------------------------------------------ refused (C-2)


def test_a_refused_chain_shows_the_sentence_requests_are_refused_with() -> None:
    _store(paused=True, direct_fallback=False)

    card = _card(_client().get("/admin/api/proxy-chains").json())

    assert card["refusal"].startswith("Not sent: NVIDIA NIM's proxy chain")
    assert "its only entry is paused" in card["refusal"]
    assert "Direct fallback is off" in card["refusal"]
    assert "Proxying page -> NVIDIA NIM" in card["refusal"]


@pytest.mark.parametrize(
    ("paused", "direct_fallback", "proxy"),
    [
        (False, False, ""),  # a usable entry
        (True, True, ""),  # Direct fallback on: today's behaviour, PR-4's to change
        (True, False, "http://203.0.113.50:3128"),  # the static proxy carries it
    ],
)
def test_a_card_with_nothing_to_say_carries_none_of_the_new_keys(
    paused: bool, direct_fallback: bool, proxy: str
) -> None:
    _store(paused=paused, direct_fallback=direct_fallback)

    payload = _client(_settings(proxy)).get("/admin/api/proxy-chains").json()

    assert not set(NEW_KEYS) & set(_card(payload))
    assert "store_problem" not in payload


def test_a_save_that_leaves_nothing_usable_answers_with_the_refusal() -> None:
    client = _client()

    payload = _put(client, paused=True, direct_fallback=False).json()

    assert "republish_failed" not in payload
    assert "Direct fallback is off" in _card(payload)["refusal"]


def test_an_unacknowledged_subscription_chain_says_it_is_inert() -> None:
    entry = {
        "provider_id": "chatgpt_oauth",
        "display_name": "ChatGPT",
        "group": "",
        "custom": False,
        "oauth": True,
        "key_count": 1,
        "env_var": None,
        "inherited_proxy": "",
        "base_url": "https://chatgpt.com/backend-api",
    }
    store = ProxyChains(
        proxies={"px_one": ProxyEndpoint(url="http://198.51.100.9:8080")},
        chains={
            "chatgpt_oauth": ProxyChain(
                enabled=True,
                entries=(ProxyChainEntry(proxy="px_one"),),
                direct_fallback=False,
            )
        },
    )

    card = admin_proxy_routes._provider_payload(entry, store, _settings())

    assert card["chain_inert"] is True
    assert "refusal" not in card


# ---------------------------------------------------- not routing yet (C-6)


def test_a_save_whose_rebuild_failed_says_saved_not_routing_yet(
    caplog, monkeypatch
) -> None:
    app = create_test_app(_settings())
    client = TestClient(app, client=("127.0.0.1", 50000))
    runtime = runtime_for_app(app)

    async def broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"proxy {SECRET_URL} was refused by the client builder")

    real = runtime.reload_providers
    monkeypatch.setattr(runtime, "reload_providers", broken)

    payload = _put(client, paused=False, direct_fallback=True).json()

    sentence = payload["republish_failed"]
    assert sentence.startswith("Saved -- not routing yet")
    assert "Restart MCC, or press Save again" in sentence
    assert "hunter2" not in sentence and "alice" not in sentence
    assert _card(payload)["not_routing"] == sentence
    # The chain IS saved: the file has it.
    assert proxy_chains.load_proxy_chains().chain("nvidia_nim") is not None
    # And the card keeps saying so on the next page load.
    again = client.get("/admin/api/proxy-chains").json()
    assert _card(again)["not_routing"] == sentence
    assert any(
        "could not republish nvidia_nim" in record.getMessage()
        and "hunter2" not in record.getMessage()
        for record in caplog.records
    )

    monkeypatch.setattr(runtime, "reload_providers", real)
    fixed = _put(client, paused=False, direct_fallback=True).json()

    assert "republish_failed" not in fixed
    assert "not_routing" not in _card(fixed)


# ---------------------------------------------- unreadable chain file (C-5)


def test_an_unreadable_chain_file_raises_the_banner_and_refuses_saves(
    chains_path: Path,
) -> None:
    _store(paused=False, direct_fallback=False)
    chains_path.write_text("{ this is not json", encoding="utf-8")
    proxy_chains.reset_proxy_chains_cache()
    before = chains_path.read_bytes()
    client = _client()

    page = client.get("/admin/api/proxy-chains").json()
    status = client.get("/admin/api/config-dir").json()
    refused = _put(client, paused=False, direct_fallback=True)

    for problem in (page["store_problem"], status["proxyChainsProblem"]):
        assert str(chains_path) in problem
        assert "cannot be parsed" in problem
        assert "nvidia_nim" in problem  # refused until it can be read
        assert "does not rewrite the file" in problem
    assert refused.status_code == 503
    assert refused.json()["detail"].startswith("Not saved:")
    assert chains_path.read_bytes() == before


def test_a_readable_chain_file_raises_no_banner() -> None:
    _store(paused=False, direct_fallback=False)
    client = _client()

    assert "store_problem" not in client.get("/admin/api/proxy-chains").json()
    assert client.get("/admin/api/config-dir").json()["proxyChainsProblem"] == ""
