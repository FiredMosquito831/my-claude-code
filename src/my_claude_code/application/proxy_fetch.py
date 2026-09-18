"""Fetch a proxy feed, test everything it offered, and keep only what works.

Before this module, pressing Fetch downloaded other people's lists, merged
them, ranked them and offered the result. Every row on that list was a claim
somebody else had published, none of it had been measured, and the operator
found out which addresses were real one Add at a time -- a ten-second check
each, mostly spent discovering that a free proxy from a public list had
stopped listening months ago.

So a fetch now finishes the job it started:

1. **Read the feeds** the operator switched on. Unchanged --
   :func:`~my_claude_code.application.proxy_ingest.fetch_feed`, the same
   parsers, the same timeout, the same ordinary strict TLS.
2. **Merge and rank.** Unchanged.
3. **Test, in rank order, in parallel.** Every address, with
   :func:`~my_claude_code.application.proxy_check.check_proxy` -- *the* checker,
   not a second one written for speed. TCP connect, CONNECT tunnel, a
   strict-TLS request to the chosen provider's own host. What the Add button
   does, done up front for everything on offer.
4. **Keep only what passed.** An address that did not answer is not stored. An
   address whose tunnel broke certificate validation is recorded **refused**,
   durably, so no later fetch offers it again -- and that refusal is lifted
   only by a later success, which is the 7.17.1 rule and is not relaxed here.

What comes back is therefore a list of addresses that were working, against a
named destination, a moment ago. "Add all working" is one press.

**The destination matters and is named.** A check answers one question about
one host: does this tunnel reach *that* host with its certificate intact. So a
fetch is run against one provider -- the one chosen on the page -- and each
stored candidate carries which. With no https destination available there is
nothing to verify against, and the honest answer is to refuse the fetch and say
so rather than store a list nothing has tested.

**This sweep must not sit in front of a request.** It is the largest piece of
outbound work in the product: hundreds of addresses, each one a handshake with
a stranger. The bounds are a fixed pool of workers rather than a task per
address, a yield after every result, no blocking call anywhere on the loop, and
one ``httpx`` client per check closed by its own ``async with`` -- so the
number of sockets open at once is the worker count and nothing else.

**Nothing here runs unless somebody asked.** The operator presses Fetch, or
turns the scheduled refresh on. A fresh install has no feeds, so a pass over
"every enabled feed" is a pass over nothing.
"""

import asyncio
import contextlib
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from loguru import logger

from my_claude_code.application.proxy_check import (
    PROXY_CHECK_TIMEOUT_SECONDS,
    apply_fetch_outcome,
    check_proxy,
)
from my_claude_code.application.proxy_ingest import (
    FEED_TIMEOUT_SECONDS,
    FeedResult,
    IngestRun,
    as_candidate_endpoint,
    candidate_id,
    harvest_feeds,
    rank_merged,
)
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    ProxyChains,
    ProxyEndpoint,
    load_proxy_chains,
    save_proxy_chains,
)

#: The states a fetch job is reported in. ``idle`` is "no job has run in this
#: process"; the other four are a job that started. They are the page's
#: vocabulary as well as this module's, so a run cannot be described one way by
#: the server and another by the browser.
FETCH_STATES: tuple[str, ...] = ("idle", "running", "done", "stopped", "failed")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class FetchProgress:
    """The live counters, mutated by the sweep and read by the status route.

    Deliberately plain and deliberately mutable: this is the one object two
    coroutines share, it is only ever written from the loop that owns the
    sweep, and every field is a monotonically increasing integer -- so a read
    taken between two increments is a slightly old number rather than a
    torn one.
    """

    feeds_total: int = 0
    feeds_read: int = 0
    total: int = 0
    tested: int = 0
    working: int = 0
    dead: int = 0
    refused: int = 0
    offered: int = 0
    corroborated: int = 0
    results: tuple[FeedResult, ...] = ()

    def as_document(self) -> dict[str, object]:
        return {
            "feeds_total": self.feeds_total,
            "feeds_read": self.feeds_read,
            "total": self.total,
            "tested": self.tested,
            "working": self.working,
            "dead": self.dead,
            "refused": self.refused,
            "offered": self.offered,
            "corroborated": self.corroborated,
            "feeds": [
                {
                    "id": result.feed_id,
                    "name": result.name,
                    "ok": result.ok,
                    "count": result.count,
                    "detail": result.detail,
                }
                for result in self.results
            ],
        }


@dataclass(frozen=True, slots=True)
class FetchRun:
    """One finished pass, as the page reports it."""

    at: str
    provider_id: str = ""
    destination: str = ""
    results: tuple[FeedResult, ...] = ()
    offered: int = 0
    corroborated: int = 0
    tested: int = 0
    working: int = 0
    dead: int = 0
    refused: int = 0
    stopped: bool = False

    @property
    def reached(self) -> int:
        return sum(1 for result in self.results if result.ok)

    def as_ingest_run(self) -> IngestRun:
        """The shape the pre-7.21 pass answered with, for callers that want it."""

        return IngestRun(
            at=self.at,
            results=self.results,
            offered=self.offered,
            corroborated=self.corroborated,
        )

    def as_document(self) -> dict[str, object]:
        document = self.as_ingest_run().as_document()
        document.update(
            {
                "provider": self.provider_id,
                "tested": self.tested,
                "working": self.working,
                "dead": self.dead,
                "refused": self.refused,
                "stopped": self.stopped,
            }
        )
        return document


def in_use_labels(store: ProxyChains) -> set[str]:
    """The masked labels of every address some chain actually names.

    What separates "this measurement is evidence about an address I route
    through" from "this measurement is about a stranger nobody has chosen". The
    reachability ladder is charged only for the first kind; see
    :func:`~my_claude_code.application.proxy_check.apply_fetch_outcome` for why
    that is not merely tidiness.
    """

    used: set[str] = set()
    for chain in store.chains.values():
        for proxy_id in chain.proxy_ids():
            endpoint = store.endpoint(proxy_id)
            if endpoint is not None:
                used.add(endpoint.label or mask_proxy_label(endpoint.url))
    return used


async def run_fetch_pass(
    *,
    provider_id: str,
    destination: str,
    concurrency: int,
    connect_timeout: float,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    feed_timeout: float = FEED_TIMEOUT_SECONDS,
    limit: int = 0,
    exit_ip_url: str = "",
    progress: FetchProgress | None = None,
    stop: asyncio.Event | None = None,
    persist: bool = True,
) -> FetchRun:
    """Read every enabled feed, test everything they offered, keep the passes.

    ``limit`` is the operator's own ceiling on how many addresses are tested
    and offered, applied **in rank order** so a bounded fetch tests the best of
    what was found rather than an arbitrary slice. ``0`` is no ceiling and is
    what ships.

    ``stop`` ends the sweep at the next address. It is not a cancellation: a
    check already in flight is allowed to finish and its verdict is kept, and
    everything that passed before the press is written to the store. Stopping a
    fetch means "that is enough addresses", not "throw away the work".
    """

    counters = progress if progress is not None else FetchProgress()
    halt = stop if stop is not None else asyncio.Event()
    at = _now()

    results, harvest, feed_count = await harvest_feeds(
        timeout=feed_timeout, on_feed=lambda: _bump_feeds(counters)
    )
    counters.feeds_total = feed_count
    counters.results = tuple(results)
    if not feed_count:
        return FetchRun(at=at, provider_id=provider_id, destination=destination)

    merged = rank_merged(harvest)
    counters.offered = len(merged)
    counters.corroborated = sum(1 for item in merged if len(item.sources) > 1)
    ranked = merged[:limit] if limit > 0 else merged
    counters.total = len(ranked)

    store = load_proxy_chains()
    used = in_use_labels(store)

    passing: list[tuple[str, ProxyEndpoint]] = []
    refused: list[tuple[str, ProxyEndpoint]] = []
    cursor = 0
    cursor_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal cursor
        while True:
            if halt.is_set():
                return
            async with cursor_lock:
                if cursor >= len(ranked):
                    return
                index = cursor
                cursor += 1
            item = ranked[index]
            url = item.endpoint.url
            label = mask_proxy_label(url)
            record = await check_proxy(
                url,
                destination,
                timeout=timeout,
                exit_ip_url=exit_ip_url,
                connect_timeout=connect_timeout,
            )
            apply_fetch_outcome(label, record, in_use=label in used)
            endpoint = replace(
                as_candidate_endpoint(item, at),
                last_check=record,
                checked_for=provider_id,
            )
            counters.tested += 1
            if record.intercepted:
                counters.refused += 1
                refused.append((candidate_id(item.endpoint.address), endpoint))
            elif record.ok:
                counters.working += 1
                passing.append((candidate_id(item.endpoint.address), endpoint))
            else:
                counters.dead += 1
            # One yield per address, every address. This is the whole of "a
            # sweep of eight hundred strangers cannot sit in front of
            # /v1/messages": the workers hand the loop back between checks.
            await asyncio.sleep(0)

    workers = max(1, min(int(concurrency), len(ranked))) if ranked else 0
    if workers:
        await asyncio.gather(*(worker() for _ in range(workers)))

    # Back into rank order. The workers finish in whatever order the network
    # allows, and the page reads this list top to bottom.
    order = {
        candidate_id(item.endpoint.address): index for index, item in enumerate(ranked)
    }
    passing.sort(key=lambda pair: order.get(pair[0], len(order)))

    if persist:
        await asyncio.to_thread(_commit_fetch, passing, refused)

    run = FetchRun(
        at=at,
        provider_id=provider_id,
        destination=destination,
        results=tuple(results),
        offered=counters.offered,
        corroborated=counters.corroborated,
        tested=counters.tested,
        working=counters.working,
        dead=counters.dead,
        refused=counters.refused,
        stopped=halt.is_set(),
    )
    logger.info(
        "PROXY FEEDS: {} of {} feed(s) answered; {} address(es) offered, {} "
        "tested, {} working, {} dead, {} refused{}",
        run.reached,
        feed_count,
        run.offered,
        run.tested,
        run.working,
        run.dead,
        run.refused,
        " (stopped early)" if run.stopped else "",
    )
    return run


def _bump_feeds(counters: FetchProgress) -> None:
    counters.feeds_read += 1


def _commit_fetch(
    passing: list[tuple[str, ProxyEndpoint]],
    refused: list[tuple[str, ProxyEndpoint]],
) -> None:
    """Write the offer, and the refusals, in one save on a fresh read.

    The refusals go in **first** and as catalogue entries rather than as
    offers. ``with_candidates`` keeps every address the store already knows to
    be refused, so recording them before it runs is what makes a refusal
    outlive the pass that found it -- and what stops the next fetch offering
    the same machine as an unknown.
    """

    fresh = load_proxy_chains()
    proxies = dict(fresh.proxies)
    for proxy_id, endpoint in passing + refused:
        previous = proxies.get(proxy_id)
        # Keep the row already there if there is one -- it may be a chain
        # entry, which must keep its own provenance and health -- and give it
        # the verdict this pass just measured. For an address that had been
        # refused and has now passed, that write is what retires the refusal:
        # a success is the only evidence that does (7.17.1), and it has to
        # land in the store before ``with_candidates`` asks which addresses
        # are still refused, or the address would stay unofferable for ever.
        proxies[proxy_id] = (
            endpoint
            if previous is None
            else replace(
                previous,
                last_check=endpoint.last_check,
                checked_for=endpoint.checked_for,
            )
        )
    save_proxy_chains(replace(fresh, proxies=proxies).with_candidates(passing))


# ------------------------------------------------------------------ the job


class FetchAlreadyRunning(RuntimeError):
    """Raised when a second fetch is started while one is still going.

    Carries the running job's id, because "a fetch is already running" with no
    way to find out which one is the kind of error message that makes an
    operator reload the page to learn nothing.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(f"a proxy fetch is already running: {job_id}")
        self.job_id = job_id


@dataclass
class FetchJob:
    """One fetch running in the background, and everything asked about it.

    A fetch of eight hundred addresses takes minutes. Holding an HTTP request
    open for it would time out in every proxy and reverse proxy between the
    browser and this process, would lose the whole pass to one reload, and
    would leave no way to stop it -- so the press starts a job and the page
    asks after it.

    **One at a time**, and the slot is the interlock: the scheduled refresh and
    the button go through the same door, so a tick that lands while somebody is
    watching a fetch is skipped rather than run beside it.
    """

    job_id: str
    provider_id: str
    destination: str
    started_at: float
    started_iso: str
    progress: FetchProgress = field(default_factory=FetchProgress)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "running"
    detail: str = ""
    finished_at: float | None = None
    run: FetchRun | None = None
    task: asyncio.Task[None] | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    def as_document(self) -> dict[str, object]:
        document: dict[str, object] = {
            "job": self.job_id,
            "state": self.state,
            "provider": self.provider_id,
            "detail": self.detail,
            "at": self.started_iso,
            "elapsed_seconds": round(self.elapsed, 1),
            "stopping": self.stop.is_set() and self.state == "running",
        }
        document.update(self.progress.as_document())
        return document


def _settle(counters: FetchProgress, run: FetchRun) -> None:
    """Make the live counters agree with the run that just finished."""

    counters.results = run.results
    counters.offered = run.offered
    counters.corroborated = run.corroborated
    counters.tested = run.tested
    counters.working = run.working
    counters.dead = run.dead
    counters.refused = run.refused


_JOB_LOCK = threading.Lock()
_JOB: FetchJob | None = None

#: What an "idle" status looks like: a fetch has never run in this process. The
#: same keys as a real one, so the page has one shape to read rather than two.
IDLE_STATUS: dict[str, object] = {
    "job": "",
    "state": "idle",
    "provider": "",
    "detail": "",
    "at": "",
    "elapsed_seconds": 0.0,
    "stopping": False,
} | FetchProgress().as_document()


def fetch_status() -> dict[str, object]:
    """What the running -- or last -- fetch is doing. Never raises."""

    with _JOB_LOCK:
        job = _JOB
    return dict(IDLE_STATUS) if job is None else job.as_document()


def running_fetch_id() -> str:
    """The id of the fetch in flight, or ``""`` when none is."""

    with _JOB_LOCK:
        job = _JOB
    return job.job_id if job is not None and job.state == "running" else ""


def stop_fetch(job_id: str = "") -> str:
    """Ask the running fetch to stop at the next address.

    Returns the id it asked to stop, or ``""`` when nothing was running or the
    id named a different job. What has already passed is kept: the sweep writes
    its store after the stop, not instead of it.
    """

    with _JOB_LOCK:
        job = _JOB
    if job is None or job.state != "running":
        return ""
    if job_id and job_id != job.job_id:
        return ""
    job.stop.set()
    return job.job_id


async def start_fetch(
    *,
    provider_id: str,
    destination: str,
    concurrency: int,
    connect_timeout: float,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    limit: int = 0,
    exit_ip_url: str = "",
) -> FetchJob:
    """Start a fetch and return at once. One at a time, process-wide."""

    with _JOB_LOCK:
        global _JOB
        if _JOB is not None and _JOB.state == "running":
            raise FetchAlreadyRunning(_JOB.job_id)
        job = FetchJob(
            job_id=f"fetch_{uuid.uuid4().hex[:12]}",
            provider_id=provider_id,
            destination=destination,
            started_at=time.monotonic(),
            started_iso=_now(),
        )
        _JOB = job

    async def body() -> None:
        try:
            job.run = await run_fetch_pass(
                provider_id=provider_id,
                destination=destination,
                concurrency=concurrency,
                connect_timeout=connect_timeout,
                timeout=timeout,
                limit=limit,
                exit_ip_url=exit_ip_url,
                progress=job.progress,
                stop=job.stop,
            )
            job.state = "stopped" if job.run.stopped else "done"
            # The finished run is the authoritative answer; the live counters
            # were only ever the sweep's running commentary on it. Copying it
            # over means a page that polls once after the end reads the same
            # numbers as one that watched the whole thing.
            _settle(job.progress, job.run)
        except asyncio.CancelledError:
            job.state = "stopped"
            job.detail = "The fetch was cancelled because the server is shutting down."
            raise
        except Exception as exc:  # pragma: no cover - defensive
            job.state = "failed"
            job.detail = f"{type(exc).__name__}: {exc}"
            logger.warning("PROXY FEEDS: the fetch failed: {}", job.detail)
        finally:
            job.finished_at = time.monotonic()

    job.task = asyncio.create_task(body())
    return job


async def wait_for_fetch() -> FetchRun | None:
    """Await the job in flight and hand back what it found.

    For the scheduled refresh and for tests: the route deliberately does not
    wait, but a timer tick has nobody to report to and must not overlap the
    next one.
    """

    with _JOB_LOCK:
        job = _JOB
    if job is None or job.task is None:
        return None
    with contextlib.suppress(Exception):
        await job.task
    return job.run


def reset_fetch_job() -> None:
    """Forget the job slot. For tests, and for a runtime that is shutting down."""

    with _JOB_LOCK:
        global _JOB
        _JOB = None


__all__ = [
    "FETCH_STATES",
    "IDLE_STATUS",
    "FetchAlreadyRunning",
    "FetchJob",
    "FetchProgress",
    "FetchRun",
    "fetch_status",
    "in_use_labels",
    "reset_fetch_job",
    "run_fetch_pass",
    "running_fetch_id",
    "start_fetch",
    "stop_fetch",
    "wait_for_fetch",
]
