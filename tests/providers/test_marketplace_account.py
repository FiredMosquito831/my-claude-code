"""7.110.0: the InferHub page's keyed reads -- masked, bounded, never the key."""

import asyncio
import json
from typing import Any

import pytest

from my_claude_code.application.marketplace import current_market_snapshot
from my_claude_code.config.marketplaces import INFERHUB_MARKETPLACE
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime import marketplace_account
from my_claude_code.providers.runtime.config import MaskedExit
from my_claude_code.providers.runtime.marketplace_account import read_marketplace
from my_claude_code.providers.runtime.marketplace_market import MarketRefresh
from my_claude_code.runtime import provider_manager as provider_manager_module
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

KEY = "sk-airo-" + "k" * 24
EVERY_READ = ("usage", "keys", "budgets", "budget_aliases", "budget_default")


def _settings(key: str = KEY) -> Settings:
    settings = Settings()
    settings.inferhub_api_key = key
    settings.inferhub_base_url = "https://api.inferhub.test/v1"
    settings.inferhub_market_base_url = "https://inferhub.test/api"
    return settings


@pytest.fixture
def exit_via(monkeypatch):
    chosen = {"exit": MaskedExit(proxy="socks5h://127.0.0.1:1", label="exit-a")}
    monkeypatch.setattr(
        marketplace_account, "masked_exit_for", lambda *a, **k: chosen["exit"]
    )
    return chosen


def _fake(answers: dict[str, tuple[int, Any]], calls: list[dict[str, Any]]):
    async def fetch(url, *, proxy, timeout, key, params=None):
        calls.append(
            {"url": url, "proxy": proxy, "key": key, "params": dict(params or {})}
        )
        path = url.split("/v1", 1)[-1] if "/v1/" in url else url.split("/api", 1)[-1]
        return answers.get(path, (404, None))

    return fetch


def test_every_read_goes_through_the_masked_exit_with_the_key_and_says_how_it_went(
    monkeypatch, exit_via
):
    calls: list[dict[str, Any]] = []
    answers = {
        "/me/usage": (200, {"balance": {"amount_usdc": "1.000000"}}),
        "/keys": (
            200,
            [
                {"keyPrefix": "sk-airo-zzzz", "kind": "config"},
                {"keyPrefix": KEY[:12], "kind": "inference"},
            ],
        ),
        "/budgets": (
            403,
            {"error": {"code": "insufficient_key_scope", "keyKind": "inference_only"}},
        ),
        "/budgets/aliases": (500, None),
    }
    monkeypatch.setattr(
        marketplace_account, "fetch_marketplace_json", _fake(answers, calls)
    )
    answer = asyncio.run(read_marketplace(_settings(), "inferhub", EVERY_READ))
    assert answer["state"] == "read"
    assert answer["proxy_exit"] == "exit-a"
    reads = answer["reads"]
    assert reads["usage"] == {
        "status": "ok",
        "body": {"balance": {"amount_usdc": "1.000000"}},
    }
    assert reads["budgets"] == {
        "status": "http_403",
        "code": "insufficient_key_scope",
        "key_kind": "inference_only",
    }
    assert reads["budget_aliases"] == {"status": "http_500"}
    assert reads["budget_default"] == {"status": "http_404"}
    # The key list matched by prefix; the list itself never leaves.
    assert answer["key_kind"] == {"kind": "inference", "how": "key_list"}
    assert reads["keys"] == {"status": "ok"}
    assert {call["url"] for call in calls} == {
        "https://api.inferhub.test/v1/me/usage",
        "https://inferhub.test/api/keys",
        "https://inferhub.test/api/budgets",
        "https://inferhub.test/api/budgets/aliases",
        "https://inferhub.test/api/budgets/default",
    }
    assert {call["proxy"] for call in calls} == {"socks5h://127.0.0.1:1"}
    assert {call["key"] for call in calls} == {KEY}
    # The key is in the Authorization header and nowhere in the answer.
    assert KEY not in json.dumps(answer)
    assert KEY[:12] not in json.dumps(answer)


def test_a_key_scope_refusal_names_the_kind_when_the_list_cannot(monkeypatch, exit_via):
    refused = (
        403,
        {"error": {"code": "insufficient_key_scope", "keyKind": "inference_only"}},
    )
    answers = {"/keys": refused, "/budgets": refused}
    monkeypatch.setattr(
        marketplace_account, "fetch_marketplace_json", _fake(answers, [])
    )
    answer = asyncio.run(read_marketplace(_settings(), "inferhub", ("keys", "budgets")))
    assert answer["key_kind"] == {"kind": "inference_only", "how": "refusal"}


def test_no_key_reads_nothing_and_a_refusing_chain_sends_nothing(monkeypatch, exit_via):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(marketplace_account, "fetch_marketplace_json", _fake({}, calls))
    assert asyncio.run(read_marketplace(_settings(""), "inferhub", EVERY_READ)) == {
        "state": "no_key",
        "reads": {},
        "key_kind": None,
    }
    exit_via["exit"] = MaskedExit(proxy=None, refused="every proxy is unhealthy")
    answer = asyncio.run(read_marketplace(_settings(), "inferhub", EVERY_READ))
    assert answer["state"] == "not_sent"
    assert answer["words"] == "every proxy is unhealthy"
    assert (
        asyncio.run(read_marketplace(_settings(), "nvidia_nim", EVERY_READ))["state"]
        == "off"
    )
    assert calls == []


def test_a_transport_failure_is_named_never_raised(monkeypatch, exit_via):
    async def broken(url, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(marketplace_account, "fetch_marketplace_json", broken)
    answer = asyncio.run(read_marketplace(_settings(), "inferhub", ("usage",)))
    assert answer["reads"]["usage"] == {"status": "failed:OSError"}


def test_the_usage_log_read_pages_and_stops_at_the_declared_bound(
    monkeypatch, exit_via
):
    calls: list[dict[str, Any]] = []

    async def fetch(url, *, proxy, timeout, key, params=None):
        calls.append(dict(params or {}))
        page = int(params["page"])
        return 200, {
            "rows": [{"id": f"{page}-{n}"} for n in range(100)],
            "total": 10_000,
        }

    monkeypatch.setattr(marketplace_account, "fetch_marketplace_json", fetch)
    answer = asyncio.run(
        read_marketplace(
            _settings(),
            "inferhub",
            ("usage_logs",),
            {"usage_logs": {"range": "custom", "from": "A", "to": "B"}},
        )
    )
    read = answer["reads"]["usage_logs"]
    assert read["pages_read"] == 5
    assert read["truncated"] is True
    assert len(read["body"]["rows"]) == 500
    assert [call["page"] for call in calls] == ["1", "2", "3", "4", "5"]
    assert {call["pageSize"] for call in calls} == {"100"}
    assert {(call["range"], call["from"], call["to"]) for call in calls} == {
        ("custom", "A", "B")
    }


def test_the_short_last_page_ends_the_usage_log_read(monkeypatch, exit_via):
    async def fetch(url, *, proxy, timeout, key, params=None):
        return 200, {"rows": [{"id": "only"}], "total": 1}

    monkeypatch.setattr(marketplace_account, "fetch_marketplace_json", fetch)
    answer = asyncio.run(read_marketplace(_settings(), "inferhub", ("usage_logs",)))
    read = answer["reads"]["usage_logs"]
    assert (read["pages_read"], read["truncated"]) == (1, False)


def test_refresh_now_runs_the_same_snapshot_build_and_only_with_a_key(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    built: list[str] = []

    async def refresh(settings, provider_id, dialect, rows, previous):
        built.append(provider_id)
        return MarketRefresh(
            status="built",
            sources={"listing": "empty"},
            payload={
                "dialect": dialect.dialect_id,
                "fetched_at": "2026-10-24T03:00:00Z",
                "sources": {"listing": "empty"},
                "platform": {},
                "notes": {},
                "combos": [],
                "models": {"cc/claude-opus-4-6": {"official": [5.0, 25.0]}},
            },
        )

    monkeypatch.setattr(provider_manager_module, "refresh_market_snapshot", refresh)

    async def run(settings: Settings) -> bool:
        manager = ProviderRuntimeManager(settings)
        try:
            return await manager.refresh_market_snapshot_now(settings)
        finally:
            await manager.close()

    assert asyncio.run(run(_settings(""))) is False
    assert built == []
    assert asyncio.run(run(_settings())) is True
    assert built == ["inferhub"]
    snapshot = current_market_snapshot(INFERHUB_MARKETPLACE)
    assert snapshot is not None
    assert snapshot.fetched_at == "2026-10-24T03:00:00Z"
