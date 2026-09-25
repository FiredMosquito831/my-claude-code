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
import json
import time

import pytest

from my_claude_code.application import proxy_fetch
from my_claude_code.application.proxy_check import reset_refusal_lifts
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
    reset_refusal_lifts()
    reset_fetch_job()
    yield path
    chains_config.reset_proxy_chains_cache()
    PROXY_INTERCEPTION.clear()
    PROXY_REACHABILITY.clear()
    reset_refusal_lifts()
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

    # Working on the one after: one pass is not enough since 7.52.4. The
    # address stays refused, is filed as refused, and is not offered.
    _checker(monkeypatch, lambda url: _ok())
    run = await _run()
    assert PROXY_INTERCEPTION.is_refused("10.0.0.9:8080") is True
    assert run.working == 0
    assert run.refused == 1
    assert load_proxy_chains().candidates == ()

    # Working again, in a row: the verdict is retired and it is offered again.
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
    """Stop means "that is enough addresses", never "throw the work away".

    **Reversed in 7.22.1, deliberately.** This used to assert that the check
    which asked for the stop still counted -- that Stop took effect only at the
    *next* address. That is exactly what made Stop unbounded: with the workers
    mid-handshake against strangers' machines, "the next address" is as far
    away as the slowest of them cares to make it, and on one operator's install
    it never arrived at all. Stop now cancels what is in flight, so the address
    whose check was cancelled has no verdict and is not counted -- which is
    what Stop was asking for. What the promise was always about is unchanged
    and is what is asserted here: every verdict that *had* been reached is
    counted and is on disk.
    """

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
    # Some of the twenty, not all of them, and not none of them.
    assert 0 < run.tested < 20
    assert run.working == run.tested
    # On disk, not merely in the reply.
    assert len(load_proxy_chains().candidates) == run.working


@pytest.mark.asyncio
async def test_stop_settles_in_seconds_with_fifty_checks_mid_handshake(monkeypatch):
    """Fifty addresses hanging for ever, and Stop still settles at once.

    The live defect, in miniature: the sweep is in flight against addresses
    that will never answer, and the operator presses Stop. Before 7.22.1 the
    press set a flag the workers read *between* addresses, so a worker stuck
    inside a check never read it and the job never settled -- the page said
    "Stopping..." for seventeen minutes and then for ever.
    """

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 61)])
    halt = asyncio.Event()
    started = asyncio.Event()
    passed = 0
    forever = asyncio.Event()

    async def check(url, destination, **kwargs):
        nonlocal passed
        if passed < 5:
            passed += 1
            return _ok()
        started.set()
        # Never resolves. Only cancellation gets out of here.
        await forever.wait()
        raise AssertionError("a hanging check must never return a verdict")

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    async def press() -> None:
        await started.wait()
        halt.set()

    presser = asyncio.create_task(press())
    began = time.monotonic()
    run = await asyncio.wait_for(_run(concurrency=50, stop=halt), 10.0)
    elapsed = time.monotonic() - began
    await presser

    assert run.stopped is True
    assert elapsed < 2.0, f"stopping took {elapsed:.2f}s"
    # The five that passed before the hang are counted and on disk.
    assert run.working == 5
    assert len(load_proxy_chains().candidates) == 5


@pytest.mark.asyncio
async def test_one_hanging_address_cannot_hold_the_whole_sweep(monkeypatch):
    """The sweep finishes, every address is accounted for, the hang is dead.

    This is the shape of what happened at 1,591 of 1,592: one address that
    never returns. The outer per-address budget is the backstop underneath
    every leg's own timeout -- wherever a future leaks, the job still ends.
    """

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 9)])
    forever = asyncio.Event()

    async def check(url, destination, **kwargs):
        if url.endswith("10.0.0.4:8080"):
            await forever.wait()
            raise AssertionError("unreachable")
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    # A budget small enough to be a test rather than a nap.
    monkeypatch.setattr(proxy_fetch, "check_budget", lambda **kwargs: 0.5)

    run = await asyncio.wait_for(_run(concurrency=4), 10.0)

    assert run.tested == 8, "every address must be accounted for"
    assert run.working == 7
    assert run.dead == 1
    assert run.stopped is False
    store = load_proxy_chains()
    assert len(store.candidates) == 7
    assert "10.0.0.4:8080" not in _labels(store)


@pytest.mark.asyncio
async def test_a_timed_out_address_is_dead_and_says_how_long_it_was_given(
    monkeypatch,
):
    """Never a pass. Nothing measured that tunnel, so nothing may claim it did."""

    _offer(monkeypatch, ["10.0.0.1:8080"])
    forever = asyncio.Event()

    async def check(url, destination, **kwargs):
        await forever.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    monkeypatch.setattr(proxy_fetch, "check_budget", lambda **kwargs: 0.3)

    run = await asyncio.wait_for(_run(concurrency=1), 10.0)

    assert (run.tested, run.working, run.dead) == (1, 0, 1)
    assert load_proxy_chains().candidates == ()


@pytest.mark.asyncio
async def test_passing_addresses_are_on_disk_while_the_sweep_is_still_running(
    monkeypatch,
):
    """Incremental persistence: the offer grows during the run, not after it.

    The 7.21.0 sweep wrote once, at the end, so a job that never reached the
    end lost all 355 addresses it had found. Here the last address hangs until
    the test has confirmed the earlier ones are already on disk.
    """

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 41)])
    monkeypatch.setattr(proxy_fetch, "FETCH_PERSIST_BATCH", 5)
    held = asyncio.Event()
    seen = 0

    async def check(url, destination, **kwargs):
        nonlocal seen
        seen += 1
        if seen > 30:
            await held.wait()
        await asyncio.sleep(0)
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    sweep = asyncio.create_task(_run(concurrency=1))
    for _ in range(500):
        await asyncio.sleep(0.01)
        chains_config.reset_proxy_chains_cache()
        if len(load_proxy_chains().candidates) >= 25:
            break
    mid_run = len(load_proxy_chains().candidates)
    held.set()
    run = await asyncio.wait_for(sweep, 10.0)

    assert mid_run >= 25, f"only {mid_run} address(es) were on disk mid-run"
    assert mid_run < 40, "that is the whole sweep, not a mid-run snapshot"
    assert run.working == 40
    assert len(load_proxy_chains().candidates) == 40


@pytest.mark.asyncio
async def test_a_sweep_does_not_write_the_store_once_per_address(monkeypatch):
    """In batches. A write per address would be forty writes for forty rows."""

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 41)])
    _checker(monkeypatch, lambda url: _ok())
    writes = 0
    real = proxy_fetch._commit_fetch

    def counted(passing, refused):
        nonlocal writes
        writes += 1
        real(passing, refused)

    monkeypatch.setattr(proxy_fetch, "_commit_fetch", counted)

    run = await _run(concurrency=8)

    assert run.working == 40
    assert writes <= 5, f"{writes} writes for 40 addresses is a write per address"


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
async def test_pressing_stop_twice_is_the_same_as_pressing_it_once(monkeypatch):
    """An operator watching "Stopping..." will press it again. Nothing breaks."""

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 31)])
    started = asyncio.Event()
    forever = asyncio.Event()

    async def check(url, destination, **kwargs):
        started.set()
        await forever.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    job = await start_fetch(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=4,
        connect_timeout=5.0,
    )
    await started.wait()
    assert stop_fetch() == job.job_id
    assert stop_fetch() == job.job_id
    await asyncio.wait_for(_finish(job), 5.0)
    assert fetch_status()["state"] == "stopped"
    # And a third press, after it has settled, is not an error either.
    assert stop_fetch() == ""


@pytest.mark.asyncio
async def test_a_job_the_server_restarted_under_comes_back_interrupted(monkeypatch):
    """A job lives in memory. Its record does not.

    Before 7.22.1 there was no record: a server restarted mid-sweep reported
    ``idle`` about a job whose results had never been written, and the page
    that had been watching a ``running`` job found nothing at all. Now the
    sweep persists as it goes, the job's own state is persisted with it, and
    the next start reads that record and says what it is: interrupted.
    """

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 31)])
    started = asyncio.Event()
    forever = asyncio.Event()
    passed = 0

    async def check(url, destination, **kwargs):
        nonlocal passed
        if passed < 3:
            passed += 1
            return _ok()
        started.set()
        await forever.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    monkeypatch.setattr(proxy_fetch, "FETCH_PERSIST_BATCH", 1)

    job = await start_fetch(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=1,
        connect_timeout=5.0,
    )
    await started.wait()
    assert fetch_status()["state"] == "running"

    # Durable while it runs, which is the whole mechanism: the record on disk
    # says a job is running and how far it had got.
    mid_run = json.loads(
        proxy_fetch.proxy_fetch_status_path().read_text(encoding="utf-8")
    )
    assert mid_run["state"] == "running"
    assert mid_run["persisted"] == 3

    # The server is killed outright: nothing gets to run a ``finally``, so the
    # record stays exactly as the sweep last wrote it, and the process's memory
    # of the job goes with the process.
    assert job.task is not None
    job.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job.task
    proxy_fetch.proxy_fetch_status_path().write_text(
        json.dumps(mid_run), encoding="utf-8"
    )
    proxy_fetch.reset_fetch_job()
    assert fetch_status()["state"] == "idle"

    # The next process starts and reads what was left behind.
    assert proxy_fetch.recover_fetch_job() == "interrupted"
    status = fetch_status()
    assert status["state"] == "interrupted"
    assert status["stopping"] is False
    assert "restarted" in str(status["detail"])
    # With its results, which were written while it ran.
    assert status["persisted"] == 3
    assert len(load_proxy_chains().candidates) == 3


@pytest.mark.asyncio
async def test_a_job_cancelled_by_a_shutdown_reads_interrupted_too(monkeypatch):
    """The server going away under a fetch is not the operator stopping it."""

    _offer(monkeypatch, [f"10.0.0.{n}:8080" for n in range(1, 31)])
    started = asyncio.Event()
    forever = asyncio.Event()

    async def check(url, destination, **kwargs):
        started.set()
        await forever.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    job = await start_fetch(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=2,
        connect_timeout=5.0,
    )
    await started.wait()
    assert job.task is not None
    job.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job.task

    status = fetch_status()
    assert status["state"] == "interrupted"
    assert "server stopped" in str(status["detail"])


@pytest.mark.asyncio
async def test_a_finished_job_never_comes_back_as_interrupted(monkeypatch):
    """``interrupted`` is about a job that was cut off, not one that ended."""

    _offer(monkeypatch, ["10.0.0.1:8080"])
    _checker(monkeypatch, lambda url: _ok())

    job = await start_fetch(
        provider_id="anthropic",
        destination=DESTINATION,
        concurrency=1,
        connect_timeout=5.0,
    )
    await _finish(job)
    assert fetch_status()["state"] == "done"

    proxy_fetch.reset_fetch_job()
    assert proxy_fetch.recover_fetch_job() == "done"
    assert fetch_status()["state"] == "done"


@pytest.mark.asyncio
async def test_a_fresh_install_recovers_nothing_and_reads_idle():
    """No record, no job, nothing to report. Every first start."""

    assert proxy_fetch.recover_fetch_job() == ""
    assert fetch_status()["state"] == "idle"


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


# ----------------------------------------------- 7.22.2: the pace of a sweep


@pytest.mark.asyncio
async def test_a_sweep_of_five_hundred_keeps_five_hundred_in_flight_and_no_more(
    monkeypatch,
):
    """The new ceiling, counted rather than asserted from the setting.

    Five hundred is a number somebody typed into a box, and the only thing that
    makes it safe is that the semaphore is also exactly how many sockets are
    open at one moment. So the fake counts what is actually concurrent, and the
    test fails both ways: a bound that leaked would show a peak above 500, and
    a bound that quietly stayed at 32 would show a peak far below it.
    """

    _offer(monkeypatch, [f"10.{n // 250}.{n % 250}.1:8080" for n in range(600)])
    inflight = 0
    peak = 0

    async def check(url, destination, **kwargs):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        inflight -= 1
        return _dead()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    run = await _run(concurrency=500)

    assert run.tested == 600
    assert peak <= 500
    assert peak > 100


@pytest.mark.asyncio
async def test_percent_mode_resolves_against_what_the_feeds_offered(monkeypatch):
    """Six per cent of 1,592 is 96, and the page is told so in those words."""

    resolved = proxy_fetch.resolve_fetch_concurrency(
        requested=6, mode="percent", offered=1592
    )

    assert resolved.value == 96
    assert resolved.mode == "percent"
    assert resolved.note == ""
    assert resolved.summary == "testing 96 at a time (6% of 1,592)"

    # And the sweep really runs at what it resolved, rather than at the raw 6.
    _offer(monkeypatch, [f"10.0.{n // 250}.{n % 250}:8080" for n in range(400)])
    inflight = 0
    peak = 0

    async def check(url, destination, **kwargs):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        inflight -= 1
        return _dead()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    counters = FetchProgress()
    await _run(concurrency=25, concurrency_mode="percent", progress=counters)

    # 25% of 400 is 100, and nothing else in the code could have produced it.
    assert counters.concurrency == 100
    assert counters.concurrency_mode == "percent"
    assert counters.concurrency_summary == "testing 100 at a time (25% of 400)"
    assert peak <= 100
    assert counters.as_document()["concurrency_summary"] == (
        "testing 100 at a time (25% of 400)"
    )


def test_percent_mode_has_a_floor_of_four_and_a_ceiling_of_five_hundred():
    """A tiny list still overlaps, and a huge one still stops where the setting does."""

    assert (
        proxy_fetch.resolve_fetch_concurrency(
            requested=1, mode="percent", offered=10
        ).value
        == 4
    )
    assert (
        proxy_fetch.resolve_fetch_concurrency(
            requested=1, mode="percent", offered=0
        ).value
        == 4
    )
    assert (
        proxy_fetch.resolve_fetch_concurrency(
            requested=100, mode="percent", offered=50_000
        ).value
        == 500
    )


def test_a_percentage_outside_one_to_a_hundred_is_reported_not_obeyed_and_not_fatal():
    """Never a crash, never a silence, and never 200% of the catalogue."""

    resolved = proxy_fetch.resolve_fetch_concurrency(
        requested=200, mode="percent", offered=1592
    )

    assert resolved.mode == "fixed"
    assert resolved.value == 200
    assert "between 1 and 100" in resolved.note
    assert "200" in resolved.note

    # Zero is the other end of the same typo.
    zero = proxy_fetch.resolve_fetch_concurrency(
        requested=0, mode="percent", offered=1592
    )
    assert zero.mode == "fixed"
    assert zero.value == 1
    assert zero.note


def test_fixed_mode_is_the_number_and_says_nothing_extra():
    """The shipped default, unchanged: the setting is a count of addresses."""

    resolved = proxy_fetch.resolve_fetch_concurrency(
        requested=100, mode="fixed", offered=1592
    )

    assert (resolved.value, resolved.mode, resolved.note) == (100, "fixed", "")
    assert resolved.summary == "testing 100 at a time"
    assert proxy_fetch.DEFAULT_FETCH_CONCURRENCY_MODE == "fixed"


def test_an_unknown_mode_is_reported_and_read_as_a_count():
    """Settings validation rejects one, so this is the belt to that braces."""

    resolved = proxy_fetch.resolve_fetch_concurrency(
        requested=64, mode="proportional", offered=900
    )

    assert (resolved.value, resolved.mode) == (64, "fixed")
    assert "proportional" in resolved.note


@pytest.mark.asyncio
async def test_a_sweep_asks_for_the_tls_depth_by_default_and_says_so(monkeypatch):
    """The one deliberate behaviour change, pinned at the call it is made in."""

    _offer(monkeypatch, ["10.0.0.1:8080"])
    seen: list[str] = []

    async def check(url, destination, **kwargs):
        seen.append(kwargs.get("depth", ""))
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    counters = FetchProgress()
    await _run(progress=counters)

    assert seen == ["tls"]
    assert counters.check_depth == "tls"
    assert counters.as_document()["check_depth"] == "tls"


@pytest.mark.asyncio
async def test_a_sweep_told_request_sends_the_request_and_says_so(monkeypatch):
    """7.22.1's sweep, available by setting one word."""

    _offer(monkeypatch, ["10.0.0.1:8080"])
    seen: list[str] = []

    async def check(url, destination, **kwargs):
        seen.append(kwargs.get("depth", ""))
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    counters = FetchProgress()
    await _run(check_depth="request", progress=counters)

    assert seen == ["request"]
    assert counters.check_depth == "request"


# ------------------------------------------ 7.53.0: screen, then confirm


def _scripted_checker(monkeypatch, script):
    """``script[address] -> list of records``, one per try, the last repeating.

    Returns the per-address list of the kwargs each try was called with.
    """

    calls: dict[str, list[dict]] = {}

    async def check(url, destination, **kwargs):
        assert destination == DESTINATION
        address = url.split("://", 1)[1]
        seen = calls.setdefault(address, [])
        seen.append(kwargs)
        await asyncio.sleep(0)
        answers = script[address]
        return answers[min(len(seen), len(answers)) - 1]

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)
    return calls


def _refused():
    return ProxyCheckRecord(
        at="now",
        ok=False,
        tls=TLS_UNKNOWN,
        detail="refused the connection",
        failure="refused",
    )


def _timeout():
    return ProxyCheckRecord(
        at="now",
        ok=False,
        tls=TLS_UNKNOWN,
        detail="no answer within 5s",
        failure="connect_timeout",
    )


@pytest.mark.asyncio
async def test_screen_failures_are_confirmed_before_dead(monkeypatch):
    """Only an address that failed every confirm round is dead; the rest are kept.

    The first passes the screen; the second passes it with a setup over the
    slow limit; the third misses the screen and passes the first re-test; the
    fourth passes only on the third re-test (one pass in four tries: flaky);
    the fifth fails the screen and all three re-tests.
    """

    addresses = [
        "198.51.100.1:80",
        "198.51.100.2:80",
        "198.51.100.3:80",
        "198.51.100.4:80",
        "198.51.100.5:80",
    ]
    _offer(monkeypatch, addresses)
    slow_pass = ProxyCheckRecord(
        at="now", ok=True, tls=TLS_STRICT, connect_ms=900, tunnel_ms=1500, tls_ms=900
    )
    calls = _scripted_checker(
        monkeypatch,
        {
            addresses[0]: [_ok()],
            addresses[1]: [slow_pass],
            addresses[2]: [_timeout(), _ok()],
            addresses[3]: [_timeout(), _timeout(), _timeout(), _ok()],
            addresses[4]: [_timeout()],
        },
    )

    run = await _run(confirm_attempts=3, confirm_spacing=0.0, slow_ms=3000)

    store = load_proxy_chains()
    assert _labels(store) == addresses[:4]
    states: dict[str, ProxyCheckRecord] = {}
    for proxy_id in store.candidates:
        endpoint = store.endpoint(proxy_id)
        assert endpoint is not None and endpoint.last_check is not None
        states[endpoint.label] = endpoint.last_check
    assert states[addresses[0]].state == "working"
    assert states[addresses[1]].state == "slow"
    assert states[addresses[2]].state == "working"
    assert states[addresses[2]].tries == 2
    assert states[addresses[3]].state == "flaky"
    assert states[addresses[3]].tries == 4
    # The screen tried each once; the dead one got every confirm round.
    assert len(calls[addresses[0]]) == 1
    assert len(calls[addresses[4]]) == 4
    assert (run.tested, run.working, run.slow, run.flaky) == (5, 4, 1, 1)
    assert (run.dead, run.confirmed_dead, run.confirm_attempts) == (1, 1, 3)
    assert run.as_document()["confirmed_dead"] == 1


@pytest.mark.asyncio
async def test_connection_refused_is_not_confirmed(monkeypatch):
    """A real refusal at the dial is dead after the screen: it is never re-tested."""

    _offer(monkeypatch, ["198.51.100.9:80"])
    calls = _scripted_checker(monkeypatch, {"198.51.100.9:80": [_refused()]})

    run = await _run(confirm_attempts=3, confirm_spacing=0.0)

    assert len(calls["198.51.100.9:80"]) == 1
    assert (run.dead, run.confirmed_dead) == (1, 0)
    assert load_proxy_chains().candidates == ()


@pytest.mark.asyncio
async def test_confirm_uses_live_connect_timeout(monkeypatch):
    """The screen dials with the fetch's 5 s; every confirm try with the live limit."""

    _offer(monkeypatch, ["198.51.100.7:80"])
    calls = _scripted_checker(monkeypatch, {"198.51.100.7:80": [_timeout()]})

    await _run(
        confirm_attempts=2,
        confirm_spacing=0.0,
        connect_timeout=5.0,
        confirm_connect_timeout=10.0,
        timeout=7.0,
    )

    dials = [kwargs["connect_timeout"] for kwargs in calls["198.51.100.7:80"]]
    legs = [kwargs["timeout"] for kwargs in calls["198.51.100.7:80"]]
    assert dials == [5.0, 10.0, 10.0]
    assert legs == [7.0, 7.0, 7.0]


@pytest.mark.asyncio
async def test_stop_cancels_confirm_stage(monkeypatch):
    """Stop during the spacing wait ends the fetch at once, confirming nothing."""

    _offer(monkeypatch, ["198.51.100.1:80", "198.51.100.2:80"])
    calls = _scripted_checker(
        monkeypatch,
        {"198.51.100.1:80": [_ok()], "198.51.100.2:80": [_timeout()]},
    )
    progress = FetchProgress()
    halt = asyncio.Event()
    task = asyncio.create_task(
        _run(
            confirm_attempts=3,
            confirm_spacing=600.0,
            progress=progress,
            stop=halt,
        )
    )
    for _ in range(200):
        if progress.confirming:
            break
        await asyncio.sleep(0.01)
    assert progress.confirming == 1
    assert progress.confirm_attempt == 1
    assert progress.confirm_attempts == 3

    began = time.monotonic()
    halt.set()
    run = await asyncio.wait_for(task, 5.0)

    assert time.monotonic() - began < 2.0
    assert run.stopped is True
    assert len(calls["198.51.100.2:80"]) == 1
    assert (run.working, run.dead, run.confirmed_dead) == (1, 1, 0)
    assert _labels(load_proxy_chains()) == ["198.51.100.1:80"]


@pytest.mark.asyncio
async def test_link_guard_pauses_and_marks_nothing_dead(monkeypatch):
    """A check that failed while this machine's own link was down is re-queued."""

    monkeypatch.setattr(proxy_fetch, "LINK_GUARD_INTERVAL_SECONDS", 0.02)
    link = {"up": True}
    probes: list[str] = []
    seen_pause: list[str] = []
    progress = FetchProgress()

    async def probe(host, port, timeout):
        probes.append(host)
        return link["up"]

    _offer(monkeypatch, ["198.51.100.3:80"])
    tries: list[int] = []

    async def check(url, destination, **kwargs):
        tries.append(1)
        if len(tries) == 1:
            # The link drops while this check is in flight, and the check
            # fails because of it.
            link["up"] = False
            for _ in range(100):
                if progress.paused:
                    break
                await asyncio.sleep(0.01)
            seen_pause.append(progress.pause_detail)
            asyncio.get_running_loop().call_later(0.1, link.update, {"up": True})
            return _timeout()
        return _ok()

    monkeypatch.setattr(proxy_fetch, "check_proxy", check)

    run = await _run(link_guard=True, link_probe=probe, progress=progress)

    assert probes and probes[0] == "api.example.invalid"
    assert seen_pause == [
        "your own connection to api.example.invalid is failing -- paused"
    ]
    # Tried again once the link answered, and it passed: nothing marked dead.
    assert len(tries) == 2
    assert (run.tested, run.working, run.dead) == (1, 1, 0)
    assert progress.paused is False
    assert _labels(load_proxy_chains()) == ["198.51.100.3:80"]
