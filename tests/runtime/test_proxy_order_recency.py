"""Prefer recently proven proxies in the speed order (7.94.0, X5 / P-6).

``PROXY_ORDER_RECENCY_HALF_LIFE_MINUTES`` multiplies an address's speed rank by
``2 ** (age / half_life)``, where ``age`` is how much longer ago a REAL request
last succeeded through it than through the chain's most recently proven
address. The properties pinned here:

* at 0 -- what ships -- every fact, order and plan is the one 7.56.0 computed,
  over the existing scenarios and 300 random ledgers;
* with a half-life, an address with no success for longer than the half-life
  ranks below an equally fast one that succeeded just now, by the automatic
  loop's own rules as well as by "Sort by speed now";
* a health check is never a proof, and a chain where nothing is proven keeps
  today's order;
* only the speed order reads it: the page's speed readout and "Pause all but
  the fastest" do not.
"""

import random
import time
from datetime import UTC, datetime

import pytest

from my_claude_code.api.admin_proxy_routes import (
    commit_pause_all_but_fastest,
    commit_speed_order,
)
from my_claude_code.application.proxy_order import (
    entry_facts,
    plan_speed_order,
    ranked,
    speed_order,
)
from my_claude_code.application.proxy_speed_store import speed_payload
from my_claude_code.config import proxy_chains as store_module
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    DIRECT,
    ProxyChain,
    ProxyChainEntry,
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
from my_claude_code.core.proxy_speed import (
    KIND_CHECK,
    KIND_DIAL,
    KIND_TTFT,
    PROXY_SPEED,
    RECENCY_MAX_HALF_LIVES,
    SCORE_WINDOW_SECONDS,
    SpeedSample,
    recency_factor,
)

PROVIDER = "nvidia_nim"
F_MS = 10_000.0
SLOW_MS = 3000.0
#: A fixed clock, so every case is reproducible.
NOW = 1_800_000_000.0
NOW_DT = datetime.fromtimestamp(NOW, tz=UTC)


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
    paused: frozenset[int] = frozenset(),
    policy: str = "failover",
) -> list[str]:
    """Write one chain; ``None`` is Direct. Returns the ledger labels."""

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
        PROVIDER,
        ProxyChain(
            enabled=True,
            policy=policy,
            entries=tuple(entries),
            order_by_speed=True,
        ),
    )
    save_proxy_chains(table)
    return labels


def _checks(label: str, setup_ms: float, *, at: float = NOW, count: int = 5) -> None:
    for step in range(count):
        PROXY_SPEED.note(
            label,
            PROVIDER,
            SpeedSample(kind=KIND_CHECK, at=at - step, ok=True, connect_ms=setup_ms),
        )


def _served(label: str, *, at: float, ok: bool = True) -> None:
    """One live dial through ``label``: answered (``ok``) or failed."""

    PROXY_SPEED.note(
        label,
        PROVIDER,
        SpeedSample(kind=KIND_DIAL, at=at, ok=ok, failure="" if ok else "429"),
    )


def _order() -> list[str]:
    table = load_proxy_chains()
    chain = table.chain(PROVIDER)
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


def _facts(half_life_seconds: float | None):
    chain = load_proxy_chains().chain(PROVIDER)
    assert chain is not None
    kwargs: dict[str, float] = {}
    if half_life_seconds is not None:
        kwargs["recency_half_life_seconds"] = half_life_seconds
    return entry_facts(
        chain,
        load_proxy_chains(),
        PROVIDER,
        failure_cost_ms=F_MS,
        slow_ms=SLOW_MS,
        now=NOW,
        **kwargs,
    )


# ----------------------------------------------------------- the arithmetic


def test_the_factor_doubles_per_half_life_and_stops_at_its_cap() -> None:
    assert recency_factor(0.0, 600.0) == 1.0
    assert recency_factor(600.0, 600.0) == 2.0
    assert recency_factor(1200.0, 600.0) == 4.0
    assert recency_factor(-5.0, 600.0) == 1.0
    cap = 2.0**RECENCY_MAX_HALF_LIVES
    assert recency_factor(None, 600.0) == cap
    assert recency_factor(600.0 * 100, 600.0) == cap
    # Off is exactly 1, whatever the age.
    for age in (None, 0.0, 1e9):
        assert recency_factor(age, 0.0) == 1.0


def test_only_an_answered_dial_or_a_first_token_is_a_real_success() -> None:
    label = "192.0.2.9:8080"
    # A passing health check proves the tunnel, not the provider behind it.
    _checks(label, 500.0, at=NOW - 10)
    assert PROXY_SPEED.last_success_at(label, PROVIDER, now=NOW) is None
    # A dial the provider refused is not a success either.
    _served(label, at=NOW - 20, ok=False)
    assert PROXY_SPEED.last_success_at(label, PROVIDER, now=NOW) is None
    # An answered dial is.
    _served(label, at=NOW - 300)
    assert PROXY_SPEED.last_success_at(label, PROVIDER, now=NOW) == NOW - 300
    # A first token through it is, and the latest proof wins.
    PROXY_SPEED.note(
        label, PROVIDER, SpeedSample(kind=KIND_TTFT, at=NOW - 60, ratio=1.2)
    )
    assert PROXY_SPEED.last_success_at(label, PROVIDER, now=NOW) == NOW - 60
    # Outside the ledger's 24-hour window nothing counts.
    later = NOW + SCORE_WINDOW_SECONDS
    assert PROXY_SPEED.last_success_at(label, PROVIDER, now=later) is None


# ------------------------------------------------------------ 0 is today


def _random_ledger(rng: random.Random) -> None:
    """One random chain and ledger: Direct, pauses, benches, refusals, proofs."""

    size = rng.randint(1, 9)
    hosts: list[str | None] = [
        None if rng.random() < 0.12 else f"198.51.100.{index + 1}"
        for index in range(size)
    ]
    paused = frozenset(i for i in range(size) if rng.random() < 0.15)
    labels = _seed(hosts, paused=paused)
    for label in labels:
        if not label:
            continue
        roll = rng.random()
        if roll < 0.1:
            PROXY_REACHABILITY.note_failure(label, "ConnectTimeout")
        elif roll < 0.15:
            PROXY_INTERCEPTION.mark(label, "intercepted")
        elif roll < 0.2:
            PROXY_HEALTH.note_failure(PROVIDER, label, benched_for=300, reason="429")
        for _ in range(rng.randint(0, 8)):
            PROXY_SPEED.note(
                label,
                PROVIDER,
                SpeedSample(
                    kind=KIND_CHECK,
                    at=NOW - rng.uniform(0, 2 * SCORE_WINDOW_SECONDS),
                    ok=rng.random() < 0.7,
                    connect_ms=rng.choice([None, rng.uniform(50, 9000)]),
                    tunnel_ms=rng.choice([None, rng.uniform(10, 3000)]),
                ),
            )
        for _ in range(rng.randint(0, 6)):
            _served(
                label,
                at=NOW - rng.uniform(0, 2 * SCORE_WINDOW_SECONDS),
                ok=rng.random() < 0.6,
            )
        for _ in range(rng.randint(0, 4)):
            PROXY_SPEED.note(
                label,
                PROVIDER,
                SpeedSample(
                    kind=KIND_TTFT,
                    at=NOW - rng.uniform(0, 2 * SCORE_WINDOW_SECONDS),
                    ratio=rng.uniform(0.3, 5.0),
                ),
            )


@pytest.mark.parametrize("seed", range(300))
def test_at_zero_every_fact_order_and_plan_is_todays(seed: int) -> None:
    rng = random.Random(seed)
    _random_ledger(rng)
    today = _facts(None)
    at_zero = _facts(0.0)
    assert at_zero == today
    assert all(fact.recency_factor == 1.0 for fact in at_zero)
    assert all(fact.effective_rank == fact.rank_key for fact in at_zero)
    assert speed_order(at_zero) == speed_order(today)
    for explicit in (False, True):
        assert plan_speed_order(
            at_zero, explicit=explicit, resort_minutes=30, now=NOW_DT
        ) == plan_speed_order(today, explicit=explicit, resort_minutes=30, now=NOW_DT)


@pytest.mark.parametrize("seed", range(40))
def test_the_writer_at_zero_writes_exactly_what_it_wrote_before(seed: int) -> None:
    """The setting at 0 through the real writer, against the setting absent."""

    rng = random.Random(10_000 + seed)
    _random_ledger(rng)
    before = load_proxy_chains()
    chain = before.chain(PROVIDER)
    assert chain is not None
    # What 7.56.0's arithmetic says, with no half-life anywhere near it.
    today = _facts(None)
    for explicit in (False, True):
        expected = plan_speed_order(
            today,
            explicit=explicit,
            last_sorted_at=chain.order_sorted_at,
            resort_minutes=30,
            now=NOW_DT,
        )
        save_proxy_chains(before)
        unset = commit_speed_order(PROVIDER, _settings(), explicit=explicit, now=NOW_DT)
        order_unset = _order()
        save_proxy_chains(before)
        zero = commit_speed_order(
            PROVIDER,
            _settings(PROXY_ORDER_RECENCY_HALF_LIFE_MINUTES=0),
            explicit=explicit,
            now=NOW_DT,
        )
        assert zero == unset
        assert _order() == order_unset
        assert zero["written"] == expected.write
        assert zero["reason"] == expected.reason


# ------------------------------------------------------- recency switched on


def test_a_stale_exit_ranks_below_an_equally_fast_one_proven_just_now() -> None:
    stale, fresh = _seed(["192.0.2.1", "192.0.2.2"])
    _checks(stale, 1000.0)
    _checks(fresh, 1000.0)
    _served(stale, at=NOW - 20 * 60)  # 20 minutes ago
    _served(fresh, at=NOW)  # just now

    # Today: a tie keeps the operator's order.
    assert speed_order(_facts(0.0)) == (0, 1)
    # Half-life 10 minutes: the stale one is two half-lives behind -> 4x.
    facts = _facts(600.0)
    by_label = {fact.label: fact for fact in facts}
    assert by_label[fresh].recency_factor == 1.0
    assert by_label[stale].recency_factor == pytest.approx(4.0, rel=1e-6)
    assert speed_order(facts) == (1, 0)
    assert [fact.label for fact in ranked(facts)] == [fresh, stale]


def test_the_automatic_loop_moves_a_recently_proven_exit_first() -> None:
    """Through the writer, by its own margin, sample and interval rules."""

    stale, fresh = _seed(["192.0.2.11", "192.0.2.12"])
    _checks(stale, 1000.0)
    _checks(fresh, 1000.0)
    _served(stale, at=NOW - 3600)  # an hour since the first entry served
    _served(fresh, at=NOW - 30)

    off = commit_speed_order(PROVIDER, _settings(), explicit=False, now=NOW_DT)
    assert off["written"] is False
    assert _order() == [stale, fresh]

    on = commit_speed_order(
        PROVIDER,
        _settings(PROXY_ORDER_RECENCY_HALF_LIFE_MINUTES=10),
        explicit=False,
        now=NOW_DT,
    )
    assert on["written"] is True, on
    assert _order() == [fresh, stale]
    assert on["new_first"] == fresh
    # The log and the page are told the rank the order actually compared.
    assert on["old_rank"] > on["new_rank"] * 2


def test_a_faster_exit_still_wins_inside_the_half_life() -> None:
    """Recency weighs against speed; it does not replace it."""

    slow, fast = _seed(["192.0.2.21", "192.0.2.22"])
    _checks(slow, 6000.0)
    _checks(fast, 1000.0)
    _served(slow, at=NOW - 10)
    _served(fast, at=NOW - 120)  # 2 minutes staler, half-life 10 minutes
    assert speed_order(_facts(600.0)) == (1, 0)


def test_nothing_proven_keeps_todays_order_with_the_setting_on() -> None:
    a, b, c = _seed(["192.0.2.31", "192.0.2.32", "192.0.2.33"])
    _checks(a, 3000.0)
    _checks(b, 900.0)
    _checks(c, 2000.0)
    assert _facts(600.0) == _facts(0.0)
    assert speed_order(_facts(600.0)) == speed_order(_facts(0.0)) == (1, 2, 0)


def test_a_health_check_alone_never_makes_an_exit_fresh() -> None:
    checked, served = _seed(["192.0.2.41", "192.0.2.42"])
    # The first one is much faster and passed a check a second ago; it has
    # never carried a request. The second carried one ten minutes ago.
    _checks(checked, 200.0, at=NOW - 1)
    _checks(served, 1000.0, at=NOW - 3600)
    _served(served, at=NOW - 600)
    assert speed_order(_facts(0.0)) == (0, 1)
    facts = _facts(300.0)
    by_label = {fact.label: fact for fact in facts}
    assert by_label[served].recency_factor == 1.0
    assert by_label[checked].recency_factor == 2.0**RECENCY_MAX_HALF_LIVES
    assert speed_order(facts) == (1, 0)


def test_benched_paused_refused_and_direct_keep_their_rules() -> None:
    """Recency only reorders the rankable entries; every pin stays a pin."""

    benched, direct, paused, fresh, stale = _seed(
        ["192.0.2.51", None, "192.0.2.52", "192.0.2.53", "192.0.2.54"],
        paused=frozenset({2}),
    )
    for label in (benched, paused, fresh, stale):
        _checks(label, 1000.0)
    for label in (benched, paused):
        _served(label, at=NOW - 1)  # the freshest of all, and not sortable
    _served(fresh, at=NOW - 60)
    _served(stale, at=NOW - 7200)
    PROXY_HEALTH.note_failure(PROVIDER, benched, benched_for=300, reason="429")

    order = speed_order(_facts(600.0))
    # The benched entry and Direct keep their index; the paused one stays
    # below the sortable ones; the fresh one is first of those.
    assert order[0] == 0
    assert order[1] == 1
    assert order[2:] == (3, 4, 2)
    assert direct == ""


def test_only_the_speed_order_reads_the_half_life() -> None:
    """The page's readout and Pause all but the fastest are unchanged."""

    stale, fresh = _seed(["192.0.2.61", "192.0.2.62"])
    _checks(stale, 900.0)
    _checks(fresh, 1000.0)
    _served(stale, at=NOW - 7200)
    _served(fresh, at=NOW - 5)
    settings = _settings(PROXY_ORDER_RECENCY_HALF_LIFE_MINUTES=10)

    payload = speed_payload(
        stale, PROVIDER, connect_timeout_seconds=10.0, slow_ms=SLOW_MS
    )
    assert "recency_factor" not in payload
    assert payload == speed_payload(
        stale, PROVIDER, connect_timeout_seconds=10.0, slow_ms=SLOW_MS
    )

    pause = commit_pause_all_but_fastest(PROVIDER, settings, 1)
    # By speed alone the stale one is the faster, so it is the one kept.
    assert pause["kept"] == [stale]


def test_the_recency_ages_against_wall_clock_when_no_now_is_given() -> None:
    stale, fresh = _seed(["192.0.2.71", "192.0.2.72"])
    wall = time.time()
    _checks(stale, 1000.0, at=wall)
    _checks(fresh, 1000.0, at=wall)
    _served(stale, at=wall - 3600)
    _served(fresh, at=wall - 5)
    chain = load_proxy_chains().chain(PROVIDER)
    assert chain is not None
    facts = entry_facts(
        chain,
        load_proxy_chains(),
        PROVIDER,
        failure_cost_ms=F_MS,
        slow_ms=SLOW_MS,
        recency_half_life_seconds=600.0,
    )
    assert speed_order(facts) == (1, 0)
