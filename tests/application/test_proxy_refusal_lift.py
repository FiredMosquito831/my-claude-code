"""An interception refusal is lifted by two passing checks in a row (7.52.4).

One used to be enough. Measured over five runs on 1,000 feed addresses, 2 of
the 69 ever refused were refused in some runs and passed in another, each time
with "self-signed certificate in certificate chain". User decision 10: require
two passes in a row. A check that did not answer breaks the row -- it is no
evidence that the interception stopped.
"""

import pytest

from my_claude_code.application import proxy_check as check_module
from my_claude_code.application.proxy_check import (
    REFUSAL_LIFT_PASSES,
    arm_refusals_from_store,
    check_endpoints,
    hold_refusal,
    reset_refusal_lifts,
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
from my_claude_code.core.proxy_rotation import (
    PROXY_INTERCEPTION,
    reset_proxy_health,
)

DESTINATION = "https://api.example.invalid/v1"
SELF_SIGNED = "self-signed certificate in certificate chain"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(store_module, "proxy_chains_path", lambda: path)
    store_module.reset_proxy_chains_cache()
    reset_proxy_health()
    reset_refusal_lifts()
    yield path
    store_module.reset_proxy_chains_cache()
    reset_proxy_health()
    reset_refusal_lifts()


def _seed() -> tuple[str, str]:
    table, proxy_id = load_proxy_chains().add_endpoint("http://203.0.113.7:8080")
    table = table.with_chain(
        "nvidia_nim",
        ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=proxy_id),)),
    )
    save_proxy_chains(table)
    endpoint = load_proxy_chains().proxies[proxy_id]
    return proxy_id, endpoint.label or "203.0.113.7:8080"


def _checker(monkeypatch, verdict: str) -> None:
    async def _check(url, destination, **kwargs):
        if verdict == "ok":
            return ProxyCheckRecord(
                at="2026-09-25T00:00:00Z", ok=True, latency_ms=900, tls="strict"
            )
        if verdict == "intercepted":
            return ProxyCheckRecord(
                at="2026-09-25T00:00:00Z",
                ok=False,
                tls=TLS_INTERCEPTED,
                detail=SELF_SIGNED,
            )
        return ProxyCheckRecord(
            at="2026-09-25T00:00:00Z", ok=False, detail="no answer within 10s"
        )

    monkeypatch.setattr(check_module, "check_proxy", _check)


async def _check_once(monkeypatch, proxy_id: str, verdict: str) -> ProxyCheckRecord:
    _checker(monkeypatch, verdict)
    outcomes = await check_endpoints([proxy_id], {proxy_id: DESTINATION})
    return outcomes[proxy_id].record


def test_the_rule_is_two() -> None:
    assert REFUSAL_LIFT_PASSES == 2


@pytest.mark.asyncio
async def test_one_pass_keeps_the_refusal_and_files_it_as_refused(monkeypatch):
    proxy_id, label = _seed()
    await _check_once(monkeypatch, proxy_id, "intercepted")
    assert PROXY_INTERCEPTION.is_refused(label) is True

    held = await _check_once(monkeypatch, proxy_id, "ok")

    assert PROXY_INTERCEPTION.is_refused(label) is True
    assert held.ok is False
    assert held.intercepted is True
    assert held.detail == (
        f"{SELF_SIGNED} -- passed 1 of the 2 checks in a row that lift this refusal"
    )
    assert held.latency_ms == 900
    stored = load_proxy_chains().proxies[proxy_id]
    assert stored.refused is True


@pytest.mark.asyncio
async def test_two_passes_in_a_row_lift_the_refusal(monkeypatch):
    proxy_id, label = _seed()
    await _check_once(monkeypatch, proxy_id, "intercepted")
    await _check_once(monkeypatch, proxy_id, "ok")

    lifted = await _check_once(monkeypatch, proxy_id, "ok")

    assert lifted.ok is True
    assert PROXY_INTERCEPTION.is_refused(label) is False
    assert load_proxy_chains().proxies[proxy_id].refused is False


@pytest.mark.asyncio
async def test_a_check_that_did_not_answer_breaks_the_row(monkeypatch):
    proxy_id, label = _seed()
    await _check_once(monkeypatch, proxy_id, "intercepted")
    await _check_once(monkeypatch, proxy_id, "ok")
    await _check_once(monkeypatch, proxy_id, "dead")
    assert PROXY_INTERCEPTION.is_refused(label) is True

    again = await _check_once(monkeypatch, proxy_id, "ok")

    assert PROXY_INTERCEPTION.is_refused(label) is True
    assert again.detail.endswith(
        "passed 1 of the 2 checks in a row that lift this refusal"
    )
    # The note is never stacked on top of an earlier one.
    assert again.detail.count("passed") == 1


@pytest.mark.asyncio
async def test_a_held_pass_survives_a_restart(monkeypatch):
    proxy_id, label = _seed()
    await _check_once(monkeypatch, proxy_id, "intercepted")
    await _check_once(monkeypatch, proxy_id, "ok")

    # A restart: every in-memory ledger is gone, the store is not.
    reset_proxy_health()
    reset_refusal_lifts()
    assert arm_refusals_from_store() == 1
    assert PROXY_INTERCEPTION.is_refused(label) is True

    # And the count starts again from zero, so one more pass is not enough.
    await _check_once(monkeypatch, proxy_id, "ok")
    assert PROXY_INTERCEPTION.is_refused(label) is True


def test_a_pass_on_an_address_that_is_not_refused_is_untouched() -> None:
    record = ProxyCheckRecord(at="2026-09-25T00:00:00Z", ok=True, tls="strict")
    assert hold_refusal("203.0.113.9:8080", record) is record
    failed = ProxyCheckRecord(at="2026-09-25T00:00:00Z", ok=False, detail="x")
    assert hold_refusal("203.0.113.9:8080", failed) is failed
