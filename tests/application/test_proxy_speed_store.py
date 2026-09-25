"""The speed ledger's file and its sources (7.54.0)."""

import json

import pytest

from my_claude_code.application import proxy_check as check_module
from my_claude_code.application.proxy_check import check_endpoints
from my_claude_code.application.proxy_speed_store import (
    flush_speed,
    load_speed,
    record_check,
)
from my_claude_code.config import proxy_chains as store_module
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    ProxyChain,
    ProxyChainEntry,
    ProxyCheckRecord,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.core.proxy_rotation import reset_proxy_health
from my_claude_code.core.proxy_speed import PROXY_SPEED


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(store_module, "proxy_chains_path", lambda: path)
    reset_proxy_health()
    yield path
    reset_proxy_health()


def _score(label: str, provider: str):
    return PROXY_SPEED.score(label, provider, failure_cost_ms=10000, slow_ms=3000)


def _passing(**timings) -> ProxyCheckRecord:
    return ProxyCheckRecord(at="2026-09-25T00:00:00Z", ok=True, tls="strict", **timings)


def test_an_interception_is_not_a_speed_sample() -> None:
    record_check("a:1", "opencode", ProxyCheckRecord(ok=False, tls=TLS_INTERCEPTED))
    record_check("a:1", "opencode", _passing(connect_ms=100, tunnel_ms=50, tls_ms=50))
    assert _score("a:1", "opencode").samples == 1


@pytest.mark.asyncio
async def test_every_try_of_a_round_is_filed_under_the_given_provider(
    store, monkeypatch
) -> None:
    table, proxy_id = load_proxy_chains().add_endpoint("http://198.51.100.9:8080")
    table = table.with_chain(
        "nvidia_nim",
        ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=proxy_id),)),
    )
    save_proxy_chains(table)
    answers = iter([False, False, True])

    async def _check(url, destination, **kwargs):
        if next(answers):
            return _passing(connect_ms=300, tunnel_ms=200, tls_ms=500)
        return ProxyCheckRecord(ok=False, detail="no answer", failure="connect_timeout")

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(check_module, "check_proxy", _check)
    await check_endpoints(
        [proxy_id],
        {proxy_id: "https://integrate.api.nvidia.com/v1"},
        attempts=3,
        spacing=1.0,
        sleep=_no_wait,
        providers={proxy_id: "opencode"},
    )
    given = _score("198.51.100.9:8080", "opencode")
    assert (given.successes, given.samples) == (1, 3)
    assert given.setup_ms == 1000
    # Without ``providers`` the first chain naming the address is used.
    answers = iter([True])
    await check_endpoints([proxy_id], {proxy_id: "https://integrate.api.nvidia.com/v1"})
    assert _score("198.51.100.9:8080", "nvidia_nim").samples == 1


@pytest.mark.asyncio
async def test_an_early_confirm_failure_files_nothing(store, monkeypatch) -> None:
    table, proxy_id = load_proxy_chains().add_endpoint("http://198.51.100.9:8080")
    save_proxy_chains(table)

    async def _fails(url, destination, **kwargs):
        return ProxyCheckRecord(ok=False, detail="no answer")

    monkeypatch.setattr(check_module, "check_proxy", _fails)
    await check_endpoints(
        [proxy_id],
        {proxy_id: "https://integrate.api.nvidia.com/v1"},
        charge_failures=False,
        providers={proxy_id: "nvidia_nim"},
    )
    assert len(PROXY_SPEED) == 0


def test_flush_then_load_round_trips(_isolate_proxy_speed) -> None:
    record_check("a:1", "opencode", _passing(connect_ms=100, tunnel_ms=100))
    assert flush_speed() is True
    assert flush_speed() is False  # nothing new
    document = json.loads(_isolate_proxy_speed.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["keys"][0]["address"] == "a:1"

    PROXY_SPEED.reset()
    assert load_speed() == 1
    assert _score("a:1", "opencode").setup_ms == 200


@pytest.mark.parametrize("content", ["{not json", '"a string"', "[]", ""])
def test_a_bad_file_is_an_empty_ledger(_isolate_proxy_speed, content) -> None:
    record_check("a:1", "opencode", _passing(connect_ms=100))
    _isolate_proxy_speed.parent.mkdir(parents=True, exist_ok=True)
    _isolate_proxy_speed.write_text(content, encoding="utf-8")
    assert load_speed() == 0
    assert len(PROXY_SPEED) == 0


def test_a_missing_file_is_an_empty_ledger(_isolate_proxy_speed) -> None:
    assert not _isolate_proxy_speed.exists()
    assert load_speed() == 0
