"""A fetch tests what it found, and offers only the addresses that worked.

Every test here is about one of the four promises the fetch pass makes:

1. only addresses that PASSED are stored;
2. an address that broke certificate validation is recorded refused, durably,
   and no later fetch offers it -- until a later SUCCESS says otherwise;
3. the sweep's parallelism is bounded by the setting and by nothing else;
4. stopping keeps what already passed.

Nothing here opens a socket. ``check_proxy`` is replaced with a fake that
answers from a table, because what is under test is the *pass* -- the order it
works in, what it keeps, what it writes, what it charges the ladders for. The
checker itself is pinned by ``test_proxy_check.py`` and this pass deliberately
does not own a second copy of it.
"""

import asyncio

import pytest

from my_claude_code.application import proxy_fetch
from my_claude_code.application.proxy_fetch import (
    FetchAlreadyRunning,
    FetchProgress,
    fetch_status,
    reset_fetch_job,
    run_fetch_pass,
    start_fetch,
    stop_fetch,
)
from my_claude_code.application.proxy_ingest import candidate_id
from my_claude_code.config import proxy_chains as chains_config
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(chains_config, "proxy_chains_path", lambda: path)
    chains_config.reset_proxy_chains_cache()
    PROXY_INTERCEPTION.clear()
    PROXY_REACHABILITY.clear()
    reset_fetch_job()
    yield path
    chains_config.reset_proxy_chains_cache()
    PROXY_INTERCEPTION.clear()
    PROXY_REACHABILITY.clear()
    reset_fetch_job()


def _offer(monkeypatch, addresses, *, https_ok=True):
    """Make the feed half answer with exactly these ``ip:port`` strings."""

    endpoints = tuple(
        FeedEndpoint(
            ip=address.split(":")[0],
            port=int(address.split(":")[1]),
            protocol="http",
            https_ok=https_ok,
        )
        for address in addresses
    )

    async def harvest(**kwargs):
        on_feed = kwargs.get("on_feed")
        if on_feed is not None:
            on_feed()
        return [], [("f1", endpoints)], 1

    monkeypatch.setattr(proxy_fetch, "harvest_feeds", harvest)


def _checker(monkeypatch, verdict):
    """Replace the checker with ``verdict(url) -> ProxyCheckRecord``."""

    async def check(url, destination, **kwargs):
        assert destination == DESTINATION
        await asyncio.sleep(0)
        return verdict(url)

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    return check


def _ok(latency=42):
    return ProxyCheckRecord(at="now", ok=True, latency_ms=latency, tls=TLS_STRICT)


def _dead():
    return ProxyCheckRecord(at="now", ok=False, tls=TLS_UNKNOWN, detail="no answer")


def _intercepted():
    return ProxyCheckRecord(
        at="now", ok=False, tls=TLS_INTERCEPTED, detail="breaks certificate validation"
    )


def _labels(store: ProxyChains, ids=None) -> list[str]:
    """The masked labels behind a list of ids, in order. ``None`` = the offer."""

    wanted = store.candidates if ids is None else ids
    rows = [store.endpoint(proxy_id) for proxy_id in wanted]
    return [row.label for row in rows if row is not None]


async def _finish(job) -> None:
    """Await a started job. The task is set before ``start_fetch`` returns."""

    assert job.task is not None
    await job.task


def _chain_holding(address: str) -> str:
    """Put one address in a chain under the id a fetch would file it under.

    ``candidate_id`` is derived from the ``ip:port`` rather than minted, which
    is what lets a promoted address keep its identity across passes -- so a
    chain entry that arrived through the page has this id, and a pass that
    finds the same address again recognises it.
    """

    proxy_id = candidate_id(address)
    store = ProxyChains().with_candidates(
        [(proxy_id, ProxyEndpoint(url=f"http://{address}", label=address))]
    )
    save_proxy_chains(
        store.without_candidate(proxy_id).with_chain(
            "anthropic", ProxyChain(entries=(ProxyChainEntry(proxy=proxy_id),))
        )
    )
    return proxy_id


async def _run(**rest):
    options: dict = {
        "provider_id": "anthropic",
        "destination": DESTINATION,
        "concurrency": 8,
        "connect_timeout": 5.0,
    }
    options.update(rest)
    return await run_fetch_pass(**options)


@pytest.mark.asyncio
async def test_only_addresses_that_passed_are_stored(monkeypatch):
    """The whole feature in one assertion.

    Three addresses offered, one of them working. What the page is handed
    afterwards is the one, not the three -- and the two that did not answer
    are not in the store at all, so nothing can later read them as untested
    rows somebody might add.
    """

    _offer(monkeypatch, ["10.0.0.1:8080", "10.0.0.2:8080", "10.0.0.3:8080"])
    _checker(monkeypatch, lambda url: _ok() if "10.0.0.2" in url else _dead())

    run = await _run()

    assert (run.tested, run.working, run.dead, run.refused) == (3, 1, 2, 0)
    store = load_proxy_chains()
    labels = _labels(store)
    assert labels == ["10.0.0.2:8080"]
    # And the verdict travelled with it, naming the provider it is about.
    kept = store.endpoint(store.candidates[0])
    assert kept is not None
    assert kept.last_check is not None and kept.last_check.ok is True
    assert kept.checked_for == "anthropic"


@pytest.mark.asyncio
async def test_a_failing_address_is_not_stored_at_all(monkeypatch):
    """Not stored-and-marked: not stored. An offer is a list of what works."""

    _offer(monkeypatch, ["10.0.0.1:8080", "10.0.0.2:8080"])
    _checker(monkeypatch, lambda url: _dead())

    run = await _run()

    assert run.working == 0
    store = load_proxy_chains()
    assert store.candidates == ()
    assert store.proxies == {}


@pytest.mark.asyncio
async def test_an_intercepted_address_is_recorded_refused_and_never_re_offered(
    monkeypatch,
):
    """The security control, end to end and across a second pass.

    A tunnel caught presenting a certificate this machine does not trust is
    reading the plaintext. It must not be offered, it must stay refused after a
    restart -- which is what the store round trip below stands in for -- and a
    later pass that finds it again must not put it back as a fresh row.
    """

    _offer(monkeypatch, ["10.0.0.9:8080"])
    _checker(monkeypatch, lambda url: _intercepted())

    run = await _run()
    assert (run.refused, run.working) == (1, 0)

    store = load_proxy_chains()
    assert store.candidates == ()
    assert _labels(store, store.refused_ids()) == ["10.0.0.9:8080"]
    # The process ledger was armed too, so nothing in this process can select
    # it even if it is already a chain entry somewhere.
    assert PROXY_INTERCEPTION.is_refused("10.0.0.9:8080") is True

    # A second pass finds it again and still does not offer it.
    second = await _run()
    assert second.refused == 1
    assert load_proxy_chains().candidates == ()


@pytest.mark.asyncio
async def test_a_refusal_is_lifted_only_by_a_later_success(monkeypatch):
    """7.17.1's rule, not relaxed by the new pass.

    Failing to connect is not evidence that a machine stopped reading the
    traffic; it is no evidence at all. Only a handshake that verified retires
    the verdict.
    """

    _offer(monkeypatch, ["10.0.0.9:8080"])
    _checker(monkeypatch, lambda url: _intercepted())
    await _run()
    assert PROXY_INTERCEPTION.is_refused("10.0.0.9:8080") is True

    # Offline on the next pass: the verdict stands.
    _checker(monkeypatch, lambda url: _dead())
    await _run()
    assert PROXY_INTERCEPTION.is_refused("10.0.0.9:8080") is True
    assert load_proxy_chains().candidates == ()

    # Working on the one after: the verdict is retired and it is offered again.
    _checker(monkeypatch, lambda url: _ok())
    await _run()
    assert PROXY_INTERCEPTION.is_refused("10.0.0.9:8080") is False
    store = load_proxy_chains()
    assert _labels(store) == ["10.0.0.9:8080"]


@pytest.mark.asyncio
async def test_the_sweep_does_not_charge_the_ladder_for_addresses_nobody_uses(
    monkeypatch,
):
    """The reachability ladder holds 512 rows and belongs to the request path.

    A sweep of eight hundred strangers would evict every row about this
    install's own addresses to record benches for machines no chain names --
    so the sweep would break the thing it was meant to inform. An address that
    IS in a chain is charged normally: that is an ordinary measurement about an
    address the operator routes through.
    """

    # One of the offered addresses is already a chain entry.
    _chain_holding("10.0.0.1:8080")
    _offer(monkeypatch, ["10.0.0.1:8080", "10.0.0.2:8080", "10.0.0.3:8080"])
    _checker(monkeypatch, lambda url: _dead())

    await _run()

    # ``state`` answers (failures, until, reason); an address with no row reads
    # as zero failures, which is exactly "the ladder was never charged".
    assert PROXY_REACHABILITY.state("10.0.0.1:8080")[0] == 1
    assert PROXY_REACHABILITY.state("10.0.0.2:8080")[0] == 0
    assert PROXY_REACHABILITY.state("10.0.0.3:8080")[0] == 0


@pytest.mark.asyncio
async def test_the_sweep_never_exceeds_the_concurrency_it_was_given(monkeypatch):
    """Counted with a fake, because a bound nobody counts is a comment.

    The number matters twice: it is how long a fetch takes, and it is exactly
    how many sockets are open to strangers at the same moment.
    """

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 41)])
    inflight = 0
    peak = 0

    async def check(url, destination, **kwargs):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        # Two suspension points, so overlapping work actually overlaps.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        inflight -= 1
        return _dead()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    run = await _run(concurrency=6)

    assert run.tested == 40
    assert peak <= 6
    # And it really did overlap: a bound of six that never ran more than one at
    # a time would pass the assertion above and be a serial sweep.
    assert peak > 1


@pytest.mark.asyncio
async def test_the_cap_is_a_setting_zero_means_unlimited(monkeypatch):
    """0 is unlimited and ships. A ceiling the operator sets is in rank order."""

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 31)])
    _checker(monkeypatch, lambda url: _ok())

    unlimited = await _run(limit=0)
    assert unlimited.tested == 30
    assert len(load_proxy_chains().candidates) == 30

    capped = await _run(limit=4)
    assert capped.tested == 4
    assert len(load_proxy_chains().candidates) == 4


@pytest.mark.asyncio
async def test_a_bounded_fetch_tests_the_best_ranked_addresses(monkeypatch):
    """A cap must not mean "an arbitrary four", it means "the best four"."""

    async def harvest(**kwargs):
        # One address every feed agrees on, and three nobody corroborates.
        first = FeedEndpoint(ip="10.0.0.9", port=8080, protocol="http", https_ok=True)
        rest = tuple(
            FeedEndpoint(ip=f"10.0.0.{n}", port=8080, protocol="http", https_ok=False)
            for n in range(1, 5)
        )
        return [], [("f1", (first, *rest)), ("f2", (first,))], 2

    monkeypatch.setattr(proxy_fetch, "harvest_feeds", harvest)
    _checker(monkeypatch, lambda url: _ok())

    run = await _run(limit=1)

    assert run.tested == 1
    store = load_proxy_chains()
    assert _labels(store) == ["10.0.0.9:8080"]


@pytest.mark.asyncio
async def test_stopping_keeps_everything_that_had_already_passed(monkeypatch):
    """Stop means "that is enough addresses", never "throw the work away"."""

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 21)])
    halt = asyncio.Event()
    seen = 0

    async def check(url, destination, **kwargs):
        nonlocal seen
        seen += 1
        if seen >= 5:
            halt.set()
        await asyncio.sleep(0)
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    run = await _run(concurrency=1, stop=halt)

    assert run.stopped is True
    assert run.tested == 5
    assert run.working == 5
    # On disk, not merely in the reply.
    assert len(load_proxy_chains().candidates) == 5


@pytest.mark.asyncio
async def test_a_second_start_is_refused_naming_the_one_that_is_running(monkeypatch):
    """One sweep at a time, process-wide -- the timer goes through this too."""

    _offer(monkeypatch, ["10.0.0.1:8080"])
    release = asyncio.Event()

    async def check(url, destination, **kwargs):
        await release.wait()
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    job = await start_fetch(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=4,
        connect_timeout=5.0,
    )
    with pytest.raises(FetchAlreadyRunning) as caught:
        await start_fetch(
            provider_id="anthropic",
            destination=DESTINATION,
            concurrency=4,
            connect_timeout=5.0,
        )
    assert caught.value.job_id == job.job_id
    assert fetch_status()["state"] == "running"
    release.set()
    await _finish(job)
    assert fetch_status()["state"] == "done"


@pytest.mark.asyncio
async def test_stop_ends_the_running_job_and_reports_it_stopped(monkeypatch):
    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 31)])
    started = asyncio.Event()

    async def check(url, destination, **kwargs):
        started.set()
        await asyncio.sleep(0)
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    job = await start_fetch(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=1,
        connect_timeout=5.0,
    )
    await started.wait()
    assert stop_fetch() == job.job_id
    await _finish(job)
    status = fetch_status()
    assert status["state"] == "stopped"
    # Whatever had passed is on disk and is reported.
    working = status["working"]
    assert isinstance(working, int) and working >= 1
    assert len(load_proxy_chains().candidates) == working


@pytest.mark.asyncio
async def test_stopping_nothing_is_not_an_error():
    assert stop_fetch() == ""
    assert fetch_status()["state"] == "idle"


@pytest.mark.asyncio
async def test_progress_counts_up_while_the_sweep_runs(monkeypatch):
    """The numbers the page prints have to move, and they have to be honest."""

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 11)])
    counters = FetchProgress()

    def verdict(url):
        if url.endswith("1:8080"):
            return _intercepted()
        return _ok() if url.endswith(("2:8080", "3:8080")) else _dead()

    _checker(monkeypatch, verdict)

    run = await _run(progress=counters, concurrency=2)

    assert counters.feeds_read == 1
    assert counters.total == 10
    assert counters.tested == 10
    assert (counters.working, counters.dead, counters.refused) == (2, 7, 1)
    assert (run.working, run.dead, run.refused) == (2, 7, 1)


@pytest.mark.asyncio
async def test_a_candidate_a_chain_already_holds_is_not_re_offered(monkeypatch):
    """Chains and offers are different tables, and the pass keeps them so."""

    proxy_id = _chain_holding("10.0.0.1:8080")
    _offer(monkeypatch, ["10.0.0.1:8080", "10.0.0.2:8080"])
    _checker(monkeypatch, lambda url: _ok())

    await _run()

    fresh = load_proxy_chains()
    chain = fresh.chain("anthropic")
    assert chain is not None and chain.proxy_ids() == (proxy_id,)
    assert _labels(fresh) == ["10.0.0.2:8080"]


@pytest.mark.asyncio
async def test_a_pass_over_no_feeds_makes_no_request_and_writes_nothing(monkeypatch):
    """Every fresh install: MCC ships no feeds, so there is nothing to read."""

    async def nothing(**kwargs):
        return [], [], 0

    monkeypatch.setattr(proxy_fetch, "harvest_feeds", nothing)

    def explode(*args, **kwargs):  # pragma: no cover - the assertion is that
        raise AssertionError("a pass over no feeds must not check anything")

    monkeypatch.setattr(proxy_fetch, "check_proxy", explode)
    save_proxy_chains(
        ProxyChains().with_candidates(
            [("px_kept00001", ProxyEndpoint(url="http://10.9.9.9:8080"))]
        )
    )

    run = await _run()

    assert (run.tested, run.working) == (0, 0)
    # And it did not clear a list it had no answer for.
    assert load_proxy_chains().candidates == ("px_kept00001",)
