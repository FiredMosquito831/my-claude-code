"""Keep the fastest healthy proxy first (7.56.0): the order is data.

Option A as the user decided it: the selection engine is byte-identical and
MCC rewrites a chain's ``entries`` order. The properties that matter are the
ones about what it does NOT do -- write a sort that does not change the entry
failover uses, write one by a thin margin or on thin evidence, write twice
inside the interval, promote an address that is benched, refused, unhealthy or
paused, or move Direct -- and that a chain whose switch is off is never
touched at all.
"""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from my_claude_code.api.admin_proxy_routes import commit_speed_order
from my_claude_code.application.proxy_order import (
    RESORT_MARGIN_MS,
    RESORT_MARGIN_RATIO,
    entry_facts,
    pause_all_but_fastest,
    plan_speed_order,
)
from my_claude_code.config import proxy_chains as store_module
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    DIRECT,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import (
    PROXY_HEALTH,
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
    reset_proxy_health,
)
from my_claude_code.core.proxy_speed import KIND_CHECK, PROXY_SPEED, SpeedSample
from my_claude_code.runtime.proxy_order import ProxyOrderTimer, orderable_chains

PROVIDER = "nvidia_nim"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(store_module, "proxy_chains_path", lambda: path)
    store_module.reset_proxy_chains_cache()
    reset_proxy_health()
    yield path
    reset_proxy_health()
    store_module.reset_proxy_chains_cache()


def _settings(**env: object) -> Settings:
    return Settings.model_validate(
        {"model": "nvidia_nim/primary", "nvidia_nim_api_key": "k", **env}
    )


def _seed(
    hosts: list[str | None],
    *,
    policy: str = "failover",
    order_by_speed: bool = True,
    enabled: bool = True,
    paused: frozenset[int] = frozenset(),
    sorted_at: str = "",
    provider: str = PROVIDER,
) -> list[str]:
    """Write one chain; ``None`` in ``hosts`` is Direct. Returns labels."""

    table = load_proxy_chains()
    entries: list[ProxyChainEntry] = []
    labels: list[str] = []
    for index, host in enumerate(hosts):
        if host is None:
            entries.append(ProxyChainEntry(proxy=DIRECT, paused=index in paused))
            labels.append("")
            continue
        url = f"http://{host}:8080"
        table, proxy_id = table.add_endpoint(url)
        entries.append(ProxyChainEntry(proxy=proxy_id, paused=index in paused))
        labels.append(mask_proxy_label(url))
    table = table.with_chain(
        provider,
        ProxyChain(
            enabled=enabled,
            policy=policy,
            entries=tuple(entries),
            order_by_speed=order_by_speed,
            order_sorted_at=sorted_at,
        ),
    )
    save_proxy_chains(table)
    return labels


def _measure(label: str, setup_ms: float, count: int = 5, ok: bool = True) -> None:
    for _ in range(count):
        PROXY_SPEED.note(
            label,
            PROVIDER,
            SpeedSample(kind=KIND_CHECK, at=time.time(), ok=ok, connect_ms=setup_ms),
        )


def _order(provider: str = PROVIDER) -> list[str]:
    """The chain's entries as labels ("Direct" for the direct rung)."""

    table = load_proxy_chains()
    chain = table.chain(provider)
    assert chain is not None
    out: list[str] = []
    for entry in chain.entries:
        if entry.is_direct:
            out.append("Direct")
            continue
        endpoint = table.endpoint(entry.proxy)
        assert endpoint is not None
        out.append(mask_proxy_label(endpoint.url))
    return out


def _rank(label: str) -> float:
    score = PROXY_SPEED.score(label, PROVIDER, failure_cost_ms=10_000.0, slow_ms=3000)
    assert score.rank_key is not None
    return score.rank_key


def test_resort_only_when_first_healthy_changes_by_margin() -> None:
    settings = _settings()

    # 1. A clearly faster second entry is put first, and the file says when.
    slow, fast = _seed(["192.0.2.1", "192.0.2.2"])
    _measure(slow, 3000)
    _measure(fast, 1000)
    assert _rank(fast) <= RESORT_MARGIN_RATIO * _rank(slow)
    assert _rank(slow) - _rank(fast) >= RESORT_MARGIN_MS
    outcome = commit_speed_order(PROVIDER, settings, explicit=False)
    assert outcome["written"] is True, outcome
    assert outcome["old_first"] == slow and outcome["new_first"] == fast
    assert _order() == [fast, slow]
    chain = load_proxy_chains().chain(PROVIDER)
    assert chain is not None and chain.order_sorted_at.endswith("Z")

    # 2. Faster, but not by 25 %: nothing is written.
    PROXY_SPEED.reset()
    first, second = _seed(["192.0.2.3", "192.0.2.4"])
    _measure(first, 3000)
    _measure(second, 2600)
    assert _rank(second) > RESORT_MARGIN_RATIO * _rank(first)
    before = load_proxy_chains().as_document()
    outcome = commit_speed_order(PROVIDER, settings, explicit=False)
    assert outcome["written"] is False
    assert "not clearly faster" in outcome["reason"]
    assert load_proxy_chains().as_document() == before

    # 3. 25 % but under 500 ms: nothing either (small absolute numbers).
    PROXY_SPEED.reset()
    first, second = _seed(["192.0.2.5", "192.0.2.6"])
    _measure(first, 400, count=20)
    _measure(second, 100, count=20)
    assert _rank(second) <= RESORT_MARGIN_RATIO * _rank(first)
    assert _rank(first) - _rank(second) < RESORT_MARGIN_MS
    assert commit_speed_order(PROVIDER, settings, explicit=False)["written"] is False

    # 4. Only a change BELOW the first healthy entry: not written on its own.
    PROXY_SPEED.reset()
    top, low, mid = _seed(["192.0.2.7", "192.0.2.8", "192.0.2.9"])
    _measure(top, 500)
    _measure(low, 9000)
    _measure(mid, 2000)
    outcome = commit_speed_order(PROVIDER, settings, explicit=False)
    assert outcome["written"] is False
    assert "first healthy entry would not change" in outcome["reason"]
    assert _order() == [top, low, mid]

    # 5. Too few samples for the newcomer: not written.
    PROXY_SPEED.reset()
    first, second = _seed(["192.0.2.10", "192.0.2.11"])
    _measure(first, 6000)
    _measure(second, 300, count=2)
    outcome = commit_speed_order(PROVIDER, settings, explicit=False)
    assert outcome["written"] is False and "sample" in outcome["reason"]

    # 6. When a first-entry change IS written, the rest is sorted with it.
    PROXY_SPEED.reset()
    a, b, c = _seed(["192.0.2.12", "192.0.2.13", "192.0.2.14"])
    _measure(a, 8000)
    _measure(b, 5000)
    _measure(c, 500)
    assert commit_speed_order(PROVIDER, settings, explicit=False)["written"] is True
    assert _order() == [c, b, a]


def test_no_resort_within_interval() -> None:
    settings = _settings(PROXY_ORDER_RESORT_MINUTES=30)
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    slow, fast = _seed(["192.0.2.21", "192.0.2.22"], sorted_at=recent)
    _measure(slow, 4000)
    _measure(fast, 800)

    outcome = commit_speed_order(PROVIDER, settings, explicit=False, now=now)
    assert outcome["written"] is False
    assert "10 minute(s) ago" in outcome["reason"], outcome
    assert _order() == [slow, fast]

    # The interval is the operator's number, and it runs out.
    later = now + timedelta(minutes=21)
    outcome = commit_speed_order(PROVIDER, settings, explicit=False, now=later)
    assert outcome["written"] is True
    assert _order() == [fast, slow]
    chain = load_proxy_chains().chain(PROVIDER)
    assert chain is not None
    assert chain.order_sorted_at == later.isoformat().replace("+00:00", "Z")

    # "Sort by speed now" is not held back by it.
    _measure(slow, 100, count=20)
    outcome = commit_speed_order(
        PROVIDER, settings, explicit=True, now=later + timedelta(minutes=1)
    )
    assert outcome["written"] is True
    assert _order() == [slow, fast]


def test_trigger_benched_entry_not_promoted() -> None:
    """An address holding a live trigger bench keeps its place, fast or not."""

    settings = _settings()
    slow, benched, quick = _seed(["192.0.2.31", "192.0.2.32", "192.0.2.33"])
    _measure(slow, 9000)
    _measure(benched, 100)
    _measure(quick, 1500)
    PROXY_HEALTH.note_failure(PROVIDER, benched, benched_for=300, reason="429")
    assert PROXY_HEALTH.snapshot(PROVIDER, benched)["state"] == "cooldown"

    outcome = commit_speed_order(PROVIDER, settings, explicit=False)
    assert outcome["written"] is True
    assert outcome["new_first"] == quick
    assert _order() == [quick, benched, slow]

    # Nor by the explicit button, from the operator's original order.
    _seed(["192.0.2.31", "192.0.2.32", "192.0.2.33"])
    assert commit_speed_order(PROVIDER, settings, explicit=True)["written"] is True
    assert _order() == [quick, benched, slow]


def test_paused_unhealthy_refused_keep_relative_order() -> None:
    settings = _settings()
    unhealthy, paused, refused, slow, fast = _seed(
        ["192.0.2.41", "192.0.2.42", "192.0.2.43", "192.0.2.44", "192.0.2.45"],
        paused=frozenset({1}),
    )
    # All three "not sortable" ones are the FASTEST by measurement: nothing
    # about their speed may bring them up.
    for label in (unhealthy, paused, refused):
        _measure(label, 50)
    _measure(slow, 7000)
    _measure(fast, 1000)
    PROXY_REACHABILITY.note_failure(unhealthy, "ConnectTimeout")
    PROXY_INTERCEPTION.mark(refused, "intercepted")

    outcome = commit_speed_order(PROVIDER, settings, explicit=True)
    assert outcome["written"] is True
    assert _order() == [fast, slow, unhealthy, paused, refused]
    # The pause itself is carried with its address.
    chain = load_proxy_chains().chain(PROVIDER)
    assert chain is not None
    assert [entry.paused for entry in chain.entries] == [
        False,
        False,
        False,
        True,
        False,
    ]


def test_direct_keeps_distance_from_end() -> None:
    settings = _settings()
    slow, _, fast, mid = _seed(["192.0.2.51", None, "192.0.2.52", "192.0.2.53"])
    _measure(slow, 9000)
    _measure(fast, 500)
    _measure(mid, 3000)
    assert commit_speed_order(PROVIDER, settings, explicit=True)["written"] is True
    order = _order()
    assert order == [fast, "Direct", mid, slow]
    assert len(order) - 1 - order.index("Direct") == 2

    PROXY_SPEED.reset()
    slow, fast, _ = _seed(["192.0.2.54", "192.0.2.55", None])
    _measure(slow, 9000)
    _measure(fast, 500)
    assert commit_speed_order(PROVIDER, settings, explicit=False)["written"] is True
    assert _order() == [fast, slow, "Direct"]


def test_ties_and_unmeasured_keep_the_operators_order() -> None:
    settings = _settings()
    a, b, c = _seed(["192.0.2.61", "192.0.2.62", "192.0.2.63"])
    _measure(a, 2000)
    _measure(b, 2000)
    outcome = commit_speed_order(PROVIDER, settings, explicit=True)
    assert outcome["written"] is False
    assert outcome["reason"] == "the chain is already in speed order"
    assert _order() == [a, b, c]


def test_round_robin_and_switch_off_chains_are_never_sorted_automatically() -> None:
    settings = _settings()
    slow, fast = _seed(["192.0.2.71", "192.0.2.72"], policy="round_robin")
    _measure(slow, 9000)
    _measure(fast, 500)
    assert orderable_chains() == ()
    assert commit_speed_order(PROVIDER, settings, explicit=False)["written"] is False
    assert _order() == [slow, fast]

    _seed(["192.0.2.71", "192.0.2.72"], order_by_speed=False)
    assert orderable_chains() == ()
    assert commit_speed_order(PROVIDER, settings, explicit=False)["written"] is False
    assert _order() == [slow, fast]

    _seed(["192.0.2.71", "192.0.2.72"], enabled=False)
    assert orderable_chains() == ()


def test_existing_chain_is_never_reordered_by_the_loop(store: Path) -> None:
    """A chain stored before 7.56.0 (no key) survives many ticks byte-for-byte."""

    labels = _seed(["192.0.2.81", "192.0.2.82", "192.0.2.83"])
    document = json.loads(store.read_text(encoding="utf-8"))
    for chain in document["chains"].values():
        chain.pop("order_by_speed", None)
        chain.pop("order_sorted_at", None)
    store.write_text(json.dumps(document), encoding="utf-8")
    store_module.reset_proxy_chains_cache()
    _measure(labels[0], 9000)
    _measure(labels[1], 5000)
    _measure(labels[2], 200)
    before = store.read_bytes()
    republished: list[frozenset[str]] = []

    async def republish(ids) -> None:
        republished.append(frozenset(ids))

    timer = ProxyOrderTimer(lambda: _settings(), republish)

    async def ticks() -> list[tuple[str, ...]]:
        return [await timer.tick() for _ in range(5)]

    assert asyncio.run(ticks()) == [()] * 5
    assert store.read_bytes() == before
    assert republished == []
    assert _order() == labels


def test_the_loop_republishes_only_the_chain_it_sorted() -> None:
    other = _seed(["198.51.100.1", "198.51.100.2"], provider="open_router")
    slow, fast = _seed(["192.0.2.91", "192.0.2.92"])
    _measure(slow, 9000)
    _measure(fast, 500)
    for label in other:
        PROXY_SPEED.note(
            label,
            "open_router",
            SpeedSample(kind=KIND_CHECK, at=time.time(), ok=True, connect_ms=100),
        )
    republished: list[frozenset[str]] = []

    async def republish(ids) -> None:
        republished.append(frozenset(ids))

    timer = ProxyOrderTimer(lambda: _settings(), republish)
    assert asyncio.run(timer.tick()) == (PROVIDER,)
    assert republished == [frozenset({PROVIDER})]
    assert _order() == [fast, slow]
    assert _order("open_router") == other
    # Sorted, and inside the interval: the next tick writes and rebuilds nothing.
    assert asyncio.run(timer.tick()) == ()
    assert republished == [frozenset({PROVIDER})]


def test_pause_all_but_fastest_keeps_the_best_and_never_direct() -> None:
    a, _, b, c, d = _seed(
        ["192.0.2.101", None, "192.0.2.102", "192.0.2.103", "192.0.2.104"],
        paused=frozenset({4}),
    )
    _measure(a, 6000)
    _measure(b, 500)
    _measure(c, 2000)
    _measure(d, 100)  # paused: never counted among the fastest, stays paused
    table = load_proxy_chains()
    chain = table.chain(PROVIDER)
    assert chain is not None
    facts = entry_facts(
        chain, table, PROVIDER, failure_cost_ms=10_000.0, slow_ms=3000.0
    )
    assert pause_all_but_fastest(facts, 2) == (0,)
    assert pause_all_but_fastest(facts, 1) == (0, 3)
    plan = plan_speed_order(facts, explicit=True, resort_minutes=30)
    assert plan.order[1] == 1  # Direct


def test_selection_engine_untouched() -> None:
    """Option A: the two selection files are byte-identical to 7.55.0's.

    Pinned by the sha256 of their LF-normalised content at 2afe0a61 (v7.55.0),
    so the check needs no git history.
    """

    pinned = {
        "src/my_claude_code/core/proxy_rotation.py": (
            "a4b2a3198e13eb4603661aa40736efd2492cf989522eb69eae7f641b2d43da7a"
        ),
        "src/my_claude_code/providers/runtime/proxy_rotating.py": (
            "472b2b9095d1f30e3273076383064bb53ab95821fee2e6febdcd207d96ce91c2"
        ),
    }
    for relative, digest in pinned.items():
        content = (REPO / relative).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == digest, relative


def test_an_empty_store_ticks_to_nothing() -> None:
    async def republish(ids) -> None:  # pragma: no cover - must not be called
        raise AssertionError(ids)

    assert asyncio.run(ProxyOrderTimer(lambda: _settings(), republish).tick()) == ()
    assert load_proxy_chains().chains == ProxyChains().chains
