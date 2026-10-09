"""PR-S1 (7.89.0) in the fetch: a stranger on a list never charges a chained
address's books.

A feed lists ``ip:port`` and files its address under that name. Before 7.89.0
a chain could hold an address at the same ``ip:port`` with a login, under the
same name -- so a fetch that caught the stranger intercepting TLS refused the
chain's own address too, and a dead stranger walked its reachability ladder.
"""

import asyncio

import pytest

from my_claude_code.application import proxy_fetch
from my_claude_code.application.proxy_check import reset_refusal_lifts
from my_claude_code.application.proxy_fetch import reset_fetch_job, run_fetch_pass
from my_claude_code.application.proxy_ingest import candidate_id
from my_claude_code.config import proxy_chains as chains_config
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_UNKNOWN,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_feeds import FeedEndpoint
from my_claude_code.core.proxy_rotation import PROXY_INTERCEPTION, PROXY_REACHABILITY

DESTINATION = "https://api.example.invalid/v1"
ADDRESS = "10.0.0.9:8080"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(chains_config, "proxy_chains_path", lambda: path)
    chains_config.reset_proxy_chains_cache()
    PROXY_INTERCEPTION.clear()
    PROXY_REACHABILITY.clear()
    reset_refusal_lifts()
    reset_fetch_job()
    yield path
    chains_config.reset_proxy_chains_cache()
    PROXY_INTERCEPTION.clear()
    PROXY_REACHABILITY.clear()
    reset_refusal_lifts()
    reset_fetch_job()


def _chain_a_login_at_the_same_address() -> None:
    save_proxy_chains(
        ProxyChains(
            proxies={"px_mine": ProxyEndpoint(url=f"http://me:pw@{ADDRESS}")},
            chains={
                "anthropic": ProxyChain(
                    enabled=True, entries=(ProxyChainEntry(proxy="px_mine"),)
                )
            },
        )
    )


def _offer_and_judge(monkeypatch, record: ProxyCheckRecord) -> None:
    ip, port = ADDRESS.split(":")
    endpoint = FeedEndpoint(ip=ip, port=int(port), protocol="http", https_ok=True)

    async def harvest(**kwargs):
        return [], [("f1", (endpoint,))], 1

    async def check(url, destination, **kwargs):
        await asyncio.sleep(0)
        return record

    monkeypatch.setattr(proxy_fetch, "harvest_feeds", harvest)
    monkeypatch.setattr(proxy_fetch, "check_proxy", check)


async def _run() -> None:
    await run_fetch_pass(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=4,
        connect_timeout=5.0,
    )


@pytest.mark.asyncio
async def test_an_intercepting_stranger_does_not_refuse_the_chain_s_address(
    monkeypatch,
) -> None:
    _chain_a_login_at_the_same_address()
    _offer_and_judge(
        monkeypatch,
        ProxyCheckRecord(at="now", ok=False, tls=TLS_INTERCEPTED, detail="intercepts"),
    )

    await _run()

    store = load_proxy_chains()
    mine = store.ledger_label("px_mine")
    stranger = store.ledger_label(candidate_id(ADDRESS))
    assert mine == ADDRESS
    assert stranger.startswith(f"{ADDRESS}#")
    assert PROXY_INTERCEPTION.is_refused(stranger)
    assert not PROXY_INTERCEPTION.is_refused(mine)


@pytest.mark.asyncio
async def test_a_dead_stranger_does_not_walk_the_chain_s_ladder(monkeypatch) -> None:
    _chain_a_login_at_the_same_address()
    _offer_and_judge(
        monkeypatch, ProxyCheckRecord(at="now", ok=False, tls=TLS_UNKNOWN, detail="x")
    )

    await _run()

    assert PROXY_REACHABILITY.state(ADDRESS)[0] == 0
