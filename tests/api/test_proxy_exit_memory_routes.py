"""The Proxying page's side of "Keep trying exits until one answers" (7.81.0).

* the card carries the switch; a chain stored before it reads ticked (7.81.1)
  and one switched off reads unticked;
* a save that does not name it keeps what is stored, and a chain the save
  creates starts ticked;
* an entry shows what MCC remembers about it, and only when it remembers
  something -- every other entry's payload is what it was;
* **Forget exit memory** drops what is remembered for that provider (chat and
  media), lifts the cooldowns and lets unreachable exits be tried again, and
  leaves every other provider's memory alone.
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
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_exit_memory import (
    BLOCKED,
    EXIT_MEMORY,
    MEDIA_EXIT_MEMORY,
    SPENT,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_REACHABILITY,
    reset_proxy_health,
)
from tests.api.support import create_test_app

ONE = "198.51.100.9:8080"
TWO = "203.0.113.7:1080"


@pytest.fixture(autouse=True)
def chains_path(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    monkeypatch.setattr("my_claude_code.config.system_proxy.getproxies", dict)
    proxy_chains.reset_proxy_chains_cache()
    admin_proxy_routes._UNROUTED.clear()
    reset_proxy_health()
    yield path
    proxy_chains.reset_proxy_chains_cache()
    admin_proxy_routes._UNROUTED.clear()
    reset_proxy_health()


def _client() -> TestClient:
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"}
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _card(payload: dict[str, Any], provider_id: str = "nvidia_nim") -> dict[str, Any]:
    return next(
        entry for entry in payload["providers"] if entry["provider_id"] == provider_id
    )


def _store(**chain_kwargs) -> None:
    save_proxy_chains(
        ProxyChains(
            proxies={
                "px_one": ProxyEndpoint(url=f"http://{ONE}"),
                "px_two": ProxyEndpoint(url=f"socks5h://{TWO}"),
            },
            chains={
                "nvidia_nim": ProxyChain(
                    enabled=True,
                    entries=(
                        ProxyChainEntry(proxy="px_one"),
                        ProxyChainEntry(proxy="px_two"),
                    ),
                    **chain_kwargs,
                )
            },
        )
    )


def _put(client: TestClient, **extra: Any) -> Any:
    body = {
        "provider": "nvidia_nim",
        "enabled": True,
        "policy": "failover",
        "scope": "provider",
        "max_switches": 2,
        "direct_fallback": True,
        "on": ["quota", "rate_limit", "timeout"],
        "oauth_acknowledged": False,
        "order_by_speed": False,
        "entries": [{"url": f"http://{ONE}"}, {"url": f"socks5h://{TWO}"}],
    } | extra
    return client.put("/admin/api/proxy-chains", json=body)


def test_a_chain_stored_before_the_switch_reads_ticked() -> None:
    _store()
    assert b"until_served" not in Path(proxy_chains.proxy_chains_path()).read_bytes()

    chain = _card(_client().get("/admin/api/proxy-chains").json())["chain"]

    assert chain["until_served"] is True

    _store(until_served=False)
    chain = _card(_client().get("/admin/api/proxy-chains").json())["chain"]
    assert chain["until_served"] is False


def test_a_chain_a_save_creates_starts_ticked_unless_the_save_says_otherwise() -> None:
    client = _client()

    assert _card(_put(client).json())["chain"]["until_served"] is True
    assert (
        _card(_put(client, until_served=False).json())["chain"]["until_served"] is False
    )
    # Not named again: the stored value stays.
    assert _card(_put(client).json())["chain"]["until_served"] is False
    assert (
        _card(_put(client, until_served=True).json())["chain"]["until_served"] is True
    )
    assert load_proxy_chains().chains["nvidia_nim"].until_served is True


def test_a_save_that_does_not_name_the_switch_keeps_what_is_stored() -> None:
    _store()

    payload = _put(_client()).json()

    assert _card(payload)["chain"]["until_served"] is True
    assert b"until_served" not in Path(proxy_chains.proxy_chains_path()).read_bytes()

    _store(until_served=False)

    payload = _put(_client()).json()

    assert _card(payload)["chain"]["until_served"] is False
    stored = Path(proxy_chains.proxy_chains_path()).read_bytes()
    assert b'"until_served": false' in stored


def test_an_entry_shows_what_is_remembered_and_only_then() -> None:
    _store(until_served=True)
    EXIT_MEMORY.remember(
        "nvidia_nim",
        "sha256:abc",
        ONE,
        state=SPENT,
        seconds=300,
        reason="rate_limit",
        stated_wait=300.0,
        credential_label="nim-…-key",
    )
    MEDIA_EXIT_MEMORY.remember(
        "nvidia_nim", "sha256:abc", ONE, state=BLOCKED, seconds=60, reason="country"
    )

    entries = _card(_client().get("/admin/api/proxy-chains").json())["chain"]["entries"]

    first, second = entries
    assert [row["rail"] for row in first["memory"]] == ["chat", "media"]
    chat = first["memory"][0]
    assert chat["state"] == "spent"
    assert chat["stated_wait"] == 300.0
    assert chat["credential"] == "nim-…-key"
    assert chat["until"].endswith("Z")
    assert 0 < chat["remaining_s"] <= 300
    assert "sha256" not in str(first)
    assert "memory" not in second


def test_forget_drops_this_provider_s_memory_and_frees_its_exits() -> None:
    _store(until_served=True)
    EXIT_MEMORY.remember(
        "nvidia_nim", "c", ONE, state=SPENT, seconds=300, reason="rate_limit"
    )
    MEDIA_EXIT_MEMORY.remember(
        "nvidia_nim", "c", ONE, state=SPENT, seconds=300, reason="rate_limit"
    )
    EXIT_MEMORY.remember(
        "open_router", "c", ONE, state=SPENT, seconds=300, reason="rate_limit"
    )
    PROXY_HEALTH.note_failure("nvidia_nim", ONE, benched_for=300.0, reason="429")
    PROXY_REACHABILITY.note_failure(TWO, "ConnectError")

    response = _client().post(
        "/admin/api/proxy-chains/forget", json={"provider": "nvidia_nim"}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["forgotten"] == {"provider": "nvidia_nim", "remembered": 2}
    entries = _card(payload)["chain"]["entries"]
    assert all("memory" not in entry for entry in entries)
    assert entries[0]["health"]["state"] != "cooldown"
    assert not PROXY_REACHABILITY.is_unhealthy(TWO)
    assert EXIT_MEMORY.records("nvidia_nim") == ()
    assert len(EXIT_MEMORY.records("open_router")) == 1


def test_forget_is_loopback_only() -> None:
    _store(until_served=True)
    settings = Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "nim-key"}
    )
    remote = TestClient(create_test_app(settings), client=("203.0.113.99", 50000))

    response = remote.post(
        "/admin/api/proxy-chains/forget", json={"provider": "nvidia_nim"}
    )

    assert response.status_code in (401, 403, 404)
