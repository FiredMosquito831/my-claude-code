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
one ``httpx`` client per check whose close is bounded -- so the number of
sockets open at once is the worker count and nothing else.

**And it must always end.** 7.22.1 is that sentence being made true. A fetch of
1,592 addresses on an operator's install reached 1,591 and stopped there: one
address's socket close never completed on the Windows proactor loop, the worker
holding it never returned, the job never settled, Stop said "Stopping..." for
seventeen minutes, and the 355 addresses that had already passed were lost
because the store was only written when the pass finished. Three things follow
from that, and they are the whole of this module's contract now:

* **Every step of a check is bounded** -- see
  :func:`~my_claude_code.application.proxy_check.check_budget` -- and one bound
  sits around the whole of each ``check_proxy`` call underneath all of them. An
  address that outlasts it is recorded dead, never a pass, and the sweep moves
  on.
* **Stop cancels.** The checks in flight are cancellation-safe, so the job
  settles in the time it takes the loop to unwind them rather than in the time
  the slowest stranger takes to answer.
* **What passed is written as it is found**, in batches, so stopping, crashing
  or restarting keeps it.

**Nothing here runs unless somebody asked.** The operator presses Fetch, or
turns the scheduled refresh on. A fresh install has no feeds, so a pass over
"every enabled feed" is a pass over nothing.
"""

import asyncio
import contextlib
import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

from loguru import logger

from my_claude_code.application.proxy_check import (
    CHECK_DEPTH_REQUEST,
    CHECK_DEPTH_TLS,
    DEFAULT_FETCH_CHECK_DEPTH,
    PROXY_CHECK_TIMEOUT_SECONDS,
    apply_fetch_outcome,
    check_budget,
    check_proxy,
    default_ssl_context,
    hold_refusal,
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
from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.constants import (
    PROXY_CHECK_CONFIRM_SPACING_SECONDS_DEFAULT,
    PROXY_CHECK_SLOW_MS_DEFAULT,
    PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT,
    PROXY_FETCH_PERSIST_INTERVAL_SECONDS_DEFAULT,
    PROXY_FETCH_TEST_CONCURRENCY_MAX,
    PROXY_FETCH_TEST_CONCURRENCY_MIN,
)
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.paths import proxy_fetch_status_path
from my_claude_code.config.proxy_chains import (
    CHECK_STATE_FLAKY,
    CHECK_STATE_SLOW,
    CHECK_STATE_WORKING,
    FAILURE_OTHER,
    FAILURE_REFUSED,
    TLS_UNKNOWN,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
    load_proxy_chains,
    save_proxy_chains,
)

#: The states a fetch job is reported in. ``idle`` is "no job has run in this
#: process"; the rest are a job that started. They are the page's vocabulary as
#: well as this module's, so a run cannot be described one way by the server
#: and another by the browser.
#:
#: ``interrupted`` is the one that is not about this process at all: a job
#: lives in memory and dies with the server, so a restart mid-sweep used to
#: leave the page reading ``running`` for ever about something nothing was
#: doing. A sweep persists what it has found as it finds it, and the next start
#: reports the job it inherited as interrupted -- with its results, which are
#: on disk.
FETCH_STATES: tuple[str, ...] = (
    "idle",
    "running",
    "done",
    "stopped",
    "failed",
    "interrupted",
)

#: How the operator's concurrency number is read.
#:
#: ``fixed`` is a count of addresses and is what shipped. ``percent`` reads the
#: same number as a percentage of however many addresses the feeds actually
#: offered, resolved once the merge is done, so one setting paces a list of two
#: hundred and a list of five thousand alike.
#:
#: Mirrored by ``config.constants.PROXY_FETCH_CONCURRENCY_MODE_NAMES``, because
#: ``config`` is a leaf package that may not import this one. Pinned in both
#: directions by ``tests/contracts/test_import_boundaries.py``.
FETCH_CONCURRENCY_MODE_FIXED = "fixed"
FETCH_CONCURRENCY_MODE_PERCENT = "percent"
FETCH_CONCURRENCY_MODES: tuple[str, ...] = (
    FETCH_CONCURRENCY_MODE_FIXED,
    FETCH_CONCURRENCY_MODE_PERCENT,
)
DEFAULT_FETCH_CONCURRENCY_MODE = FETCH_CONCURRENCY_MODE_FIXED

#: How many passing addresses accumulate before the store is written, and how
#: long a smaller batch may wait. Whichever comes first.
#:
#: The pre-7.22.1 sweep wrote once, at the end. A fetch of 1,592 addresses that
#: never reached the end -- because one of them held a socket open for ever --
#: therefore lost all 355 that had passed: the operator watched the counter
#: climb for twenty minutes, pressed Stop, and got nothing. Twenty-five is a
#: batch small enough that almost nothing is ever at risk and large enough that
#: a sweep is not a write per address; five seconds is what makes a slow trickle
#: durable too.
#:
#: Since 7.24.0 the five seconds is the operator's, settable as
#: ``PROXY_FETCH_PERSIST_INTERVAL_SECONDS`` on Limits & Resilience. This name
#: is the shipped default and what a caller that is handed nothing still uses.
FETCH_PERSIST_BATCH = 25
FETCH_PERSIST_INTERVAL_SECONDS = PROXY_FETCH_PERSIST_INTERVAL_SECONDS_DEFAULT

# How hard the *last* write of a pass tries. Writing the store is an atomic
# rename, and on Windows a rename over a file another handle has open fails
# outright -- which the page polling this store several times a second makes a
# real possibility rather than a theoretical one. An ordinary batch that loses
# the race is simply written by the next one; the final batch has no next one.
_PERSIST_ATTEMPTS = 5
_PERSIST_RETRY_SECONDS = 0.2


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clamp_concurrency(value: int, *, floor: int = 1) -> int:
    """The number, inside the range the sweep can actually honour.

    The ceiling is the setting's own maximum and is never exceeded: it is
    exactly how many sockets are open to strangers at one moment.

    The floor is ``1`` by default and deliberately not the setting's minimum of
    four. Settings validation already refuses a configured value below four;
    this function is also called by tests and by a caller that has worked out a
    number for itself, and rounding *up* a caller who asked for one would be
    this code overruling an explicit instruction. A percentage is the exception
    and passes the setting's floor: four is what keeps a small list overlapping
    at all.
    """

    return max(max(1, floor), min(int(value), PROXY_FETCH_TEST_CONCURRENCY_MAX))


@dataclass(frozen=True, slots=True)
class ResolvedConcurrency:
    """How many addresses this pass will test at once, and why that number.

    The *why* is half of it. A percentage resolved against a list nobody has
    seen yet is a number the operator did not type, so it is carried to the
    status payload and said on the page -- "testing 96 at a time (6% of 1,592)"
    -- rather than left to be inferred from how fast the counter moves.
    """

    value: int
    mode: str
    requested: int
    offered: int
    #: Empty when the setting was honoured as written. Otherwise the plain
    #: sentence explaining what was wrong with it and what was done instead.
    #: Never a crash and never a silence: a percentage of 200 is a typo, and
    #: the operator finds out from the page rather than from the clock.
    note: str = ""

    @property
    def summary(self) -> str:
        if self.mode == FETCH_CONCURRENCY_MODE_PERCENT:
            return (
                f"testing {self.value} at a time "
                f"({self.requested}% of {self.offered:,})"
            )
        return f"testing {self.value} at a time"

    def as_document(self) -> dict[str, object]:
        return {
            "concurrency": self.value,
            "concurrency_mode": self.mode,
            "concurrency_requested": self.requested,
            "concurrency_summary": self.summary,
            "concurrency_note": self.note,
        }


def resolve_fetch_concurrency(
    *, requested: int, mode: str, offered: int
) -> ResolvedConcurrency:
    """Turn the setting and the size of the offer into one number.

    ``fixed`` is the number, clamped to the range the setting allows.
    ``percent`` is that percentage of ``offered``, to the nearest address, with
    the same floor and ceiling -- so a small list still gets at least four in
    flight and a huge one still stops at five hundred.

    A ``percent`` value outside 1-100 is a typo rather than an instruction.
    It is **reported** and the number is used as a fixed count, which is the
    only reading of it that could have been meant. Nothing raises: a fetch that
    refused to start over a badly typed percentage would cost the operator the
    whole pass to fix a number they can see.
    """

    wanted = int(requested)
    chosen = str(mode or "").strip().lower() or DEFAULT_FETCH_CONCURRENCY_MODE
    if chosen not in FETCH_CONCURRENCY_MODES:
        return ResolvedConcurrency(
            value=_clamp_concurrency(wanted),
            mode=FETCH_CONCURRENCY_MODE_FIXED,
            requested=wanted,
            offered=offered,
            note=(
                f"PROXY_FETCH_CONCURRENCY_MODE is set to {mode!r}, which is "
                f"not one of {', '.join(FETCH_CONCURRENCY_MODES)}. The number "
                "was read as a fixed count of addresses."
            ),
        )
    if chosen == FETCH_CONCURRENCY_MODE_FIXED:
        return ResolvedConcurrency(
            value=_clamp_concurrency(wanted),
            mode=chosen,
            requested=wanted,
            offered=offered,
        )
    if not 1 <= wanted <= 100:
        return ResolvedConcurrency(
            value=_clamp_concurrency(wanted),
            mode=FETCH_CONCURRENCY_MODE_FIXED,
            requested=wanted,
            offered=offered,
            note=(
                f"PROXY_FETCH_TEST_CONCURRENCY is {wanted}, and in percent "
                "mode it has to be between 1 and 100. It was read as a fixed "
                f"count of addresses for this pass: {_clamp_concurrency(wanted)} "
                "at a time."
            ),
        )
    return ResolvedConcurrency(
        value=_clamp_concurrency(
            int(max(0, offered) * wanted / 100 + 0.5),
            floor=PROXY_FETCH_TEST_CONCURRENCY_MIN,
        ),
        mode=chosen,
        requested=wanted,
        offered=offered,
    )


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
    #: How many passing addresses are already on disk. The difference between
    #: "355 working" and "355 working, and they are still there if this stops"
    #: -- and the only counter here a person reading the page can act on while
    #: the sweep is still going.
    persisted: int = 0
    #: How many addresses are in flight at once, once the setting has been
    #: read against the size of the offer, and the sentence that says why.
    #: Zero and empty until the feeds have answered, because a percentage has
    #: nothing to be a percentage of before then.
    concurrency: int = 0
    concurrency_mode: str = ""
    concurrency_requested: int = 0
    concurrency_summary: str = ""
    concurrency_note: str = ""
    #: How far each address's test goes in this pass. The page says it, because
    #: it is the difference between a sweep that talks to the provider and one
    #: that does not.
    check_depth: str = ""
    #: The confirm stage (7.53.0). ``working`` above still counts every
    #: address that passed, as it always has; ``slow`` and ``flaky`` are the
    #: part of it that carries that label. ``dead`` still counts every address
    #: that was not kept; ``confirmed_dead`` is the part of it that failed
    #: every confirm round, and the rest were refused at the dial (never
    #: re-tested) or left unconfirmed by a Stop. ``confirming`` is how many
    #: addresses the round in progress re-tests, ``confirm_attempt`` which try
    #: that round is, and ``confirm_attempts`` the tries each address gets.
    slow: int = 0
    flaky: int = 0
    confirmed_dead: int = 0
    confirming: int = 0
    confirm_attempt: int = 0
    confirm_attempts: int = 0
    #: True while the link guard has paused the sweep, and the sentence that
    #: says why. Nothing is marked dead while it is true.
    paused: bool = False
    pause_detail: str = ""
    results: tuple[FeedResult, ...] = ()

    def note_concurrency(self, resolved: ResolvedConcurrency) -> None:
        self.concurrency = resolved.value
        self.concurrency_mode = resolved.mode
        self.concurrency_requested = resolved.requested
        self.concurrency_summary = resolved.summary
        self.concurrency_note = resolved.note

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
            "persisted": self.persisted,
            "concurrency": self.concurrency,
            "concurrency_mode": self.concurrency_mode,
            "concurrency_requested": self.concurrency_requested,
            "concurrency_summary": self.concurrency_summary,
            "concurrency_note": self.concurrency_note,
            "check_depth": self.check_depth,
            "slow": self.slow,
            "flaky": self.flaky,
            "confirmed_dead": self.confirmed_dead,
            "confirming": self.confirming,
            "confirm_attempt": self.confirm_attempt,
            "confirm_attempts": self.confirm_attempts,
            "paused": self.paused,
            "pause_detail": self.pause_detail,
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
    slow: int = 0
    flaky: int = 0
    confirmed_dead: int = 0
    confirm_attempts: int = 0

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
                "slow": self.slow,
                "flaky": self.flaky,
                "confirmed_dead": self.confirmed_dead,
                "confirm_attempts": self.confirm_attempts,
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


#: How often the link guard re-checks this machine's own connection while a
#: sweep runs, and how long its one direct handshake may take before the link
#: counts as failing. A TLS handshake to a provider's own host takes 0.03-0.34 s
#: on a healthy link (spec §2); five seconds is a link that is not healthy.
LINK_GUARD_INTERVAL_SECONDS = 15.0
LINK_GUARD_TIMEOUT_SECONDS = 5.0

#: ``probe(host, port, timeout) -> bool``: whether this machine completed a
#: direct TLS handshake to ``host`` within ``timeout``. Injectable for tests.
LinkProbe = Callable[[str, int, float], Awaitable[bool]]


async def probe_link(host: str, port: int, timeout: float) -> bool:
    """One direct TLS handshake to ``host``: no proxy, and no request.

    Verified with :func:`~my_claude_code.application.proxy_check.default_ssl_context`
    -- the library's own default trust, the same object every check uses --
    naming ``host``, and closed as soon as it completes. It asks one question:
    can this machine reach the destination at all right now.
    """

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, port, ssl=default_ssl_context(), server_hostname=host
            ),
            timeout,
        )
    except TimeoutError, OSError:
        return False
    del reader
    with contextlib.suppress(Exception):
        writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1.0)
    except TimeoutError, OSError:
        with contextlib.suppress(Exception):
            writer.transport.abort()
    return True


class LinkGuard:
    """Pause a sweep while this machine's own connection is failing (7.53.0).

    A broken home link makes every proxy look dead, and a fetch that ran
    through one would throw away every address it tested. So before the sweep
    and every :data:`LINK_GUARD_INTERVAL_SECONDS` during it, one direct TLS
    handshake (:func:`probe_link`) goes to the destination host. If it fails or
    takes longer than :data:`LINK_GUARD_TIMEOUT_SECONDS`:

    * the sweep **pauses** -- workers stop taking new addresses;
    * the status says "your own connection to <host> is failing -- paused";
    * **nothing is marked dead** for a check that failed while the link was
      down or that was in flight when the pause began (:meth:`disturbed`):
      the caller puts it back in the queue.

    The next handshake that succeeds resumes it. Not adaptive concurrency: the
    measurement found no self-congestion at 100 at once, so there was nothing
    for a back-off to correct (user decision 12).
    """

    def __init__(
        self,
        destination: str,
        *,
        probe: LinkProbe | None = None,
        counters: FetchProgress | None = None,
        interval: float = LINK_GUARD_INTERVAL_SECONDS,
        timeout: float = LINK_GUARD_TIMEOUT_SECONDS,
    ) -> None:
        parsed = urlsplit(destination.strip())
        self.host = parsed.hostname or ""
        self.port = parsed.port or 443
        self._probe = probe if probe is not None else probe_link
        self._counters = counters
        self._interval = max(0.01, float(interval))
        self._timeout = max(0.01, float(timeout))
        self._ok = asyncio.Event()
        self._ok.set()
        self.paused = False
        #: Bumped at the start of every pause. A check that began under one
        #: epoch and finished under another straddled a pause.
        self.epoch = 0

    async def check(self) -> bool:
        """One handshake; pause or resume on the answer. Returns it."""

        if not self.host:
            return True
        try:
            healthy = bool(
                await asyncio.wait_for(
                    self._probe(self.host, self.port, self._timeout),
                    self._timeout + 1.0,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            healthy = False
        if healthy and self.paused:
            self.paused = False
            self._ok.set()
            logger.info(
                "PROXY FEEDS: the link to {} answers again; resuming", self.host
            )
        elif not healthy and not self.paused:
            self.paused = True
            self.epoch += 1
            self._ok.clear()
            logger.warning(
                "PROXY FEEDS: this machine's own connection to {} is failing; "
                "the fetch is paused and marks nothing dead until it answers",
                self.host,
            )
        if self._counters is not None:
            self._counters.paused = self.paused
            self._counters.pause_detail = (
                f"your own connection to {self.host} is failing -- paused"
                if self.paused
                else ""
            )
        return healthy

    async def watch(self, halt: asyncio.Event) -> None:
        """Re-check every interval until the sweep ends or is stopped."""

        while not halt.is_set():
            if await _halted_within(halt, self._interval):
                return
            await self.check()

    def disturbed(self, epoch: int) -> bool:
        """Whether a check that started under ``epoch`` overlapped a pause."""

        return self.paused or self.epoch != epoch

    async def wait_ok(self) -> None:
        await self._ok.wait()

    def release(self) -> None:
        """Clear the pause for good: the sweep this guarded has ended."""

        self.paused = False
        self._ok.set()
        if self._counters is not None:
            self._counters.paused = False
            self._counters.pause_detail = ""


async def _halted_within(halt: asyncio.Event, seconds: float) -> bool:
    """Wait ``seconds``, or less if Stop is pressed. True when it was."""

    if halt.is_set():
        return True
    if seconds <= 0:
        return False
    try:
        await asyncio.wait_for(halt.wait(), seconds)
    except TimeoutError:
        return False
    return True


def pass_state(record: ProxyCheckRecord, *, tried: int, slow_ms: int) -> str:
    """The label a passing address earns in one fetch (7.53.0).

    ``flaky`` when it passed on fewer than half of at least three tries -- a
    pass always ends an address's tries, so that is one pass after two or more
    failures. Otherwise ``slow`` when its setup (connect + tunnel + TLS) took
    more than ``slow_ms``, and ``working`` when it did not or was not timed.
    """

    if tried >= 3 and 1 / tried < 0.5:
        return CHECK_STATE_FLAKY
    setup = record.setup_ms
    if setup is not None and setup > slow_ms:
        return CHECK_STATE_SLOW
    return CHECK_STATE_WORKING


async def run_fetch_pass(
    *,
    provider_id: str,
    destination: str,
    concurrency: int,
    connect_timeout: float,
    concurrency_mode: str = DEFAULT_FETCH_CONCURRENCY_MODE,
    check_depth: str = DEFAULT_FETCH_CHECK_DEPTH,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    feed_timeout: float = FEED_TIMEOUT_SECONDS,
    persist_interval: float = FETCH_PERSIST_INTERVAL_SECONDS,
    limit: int = 0,
    exit_ip_url: str = "",
    progress: FetchProgress | None = None,
    stop: asyncio.Event | None = None,
    persist: bool = True,
    on_persist: Callable[[], None] | None = None,
    confirm_attempts: int = 0,
    confirm_spacing: float = PROXY_CHECK_CONFIRM_SPACING_SECONDS_DEFAULT,
    confirm_connect_timeout: float = PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT,
    slow_ms: int = PROXY_CHECK_SLOW_MS_DEFAULT,
    link_guard: bool = False,
    link_probe: LinkProbe | None = None,
) -> FetchRun:
    """Read every enabled feed, test everything they offered, keep the passes.

    ``limit`` is the operator's own ceiling on how many addresses are tested
    and offered, applied **in rank order** so a bounded fetch tests the best of
    what was found rather than an arbitrary slice. ``0`` is no ceiling and is
    what ships.

    ``stop`` ends the sweep. Since 7.22.1 it **does** cancel the checks in
    flight, and that is the fix rather than a change of mind: a check is
    cancellation-safe end to end -- every ``finally`` that awaits anything is
    bounded -- so cancelling one costs a verdict about one address and returns
    the loop in milliseconds, where waiting for it costs an operator a Stop
    button that says "Stopping..." for as long as the slowest stranger on the
    list feels like holding a socket. Everything that passed is kept: it was
    already being written while the sweep ran, and the remainder is written
    here. Stopping a fetch still means "that is enough addresses", never "throw
    away the work".

    ``on_persist`` is called after every write of the store, on the loop, so a
    caller that keeps a durable record of the job can keep it current without
    this function knowing what that record is.

    ``concurrency_mode`` decides how ``concurrency`` is read, and the reading
    happens **here**, after the feeds have answered and been merged, because a
    percentage has nothing to be a percentage of until then. The resolved
    number, and the sentence that explains it, go onto the counters so the page
    reports what is happening rather than what was configured.

    ``check_depth`` is how far each address's test goes, and its default --
    :data:`~my_claude_code.application.proxy_check.DEFAULT_FETCH_CHECK_DEPTH`
    -- is the one deliberate behaviour change in 7.22.2: a sweep opens the
    tunnel, finishes a verified TLS handshake to the provider's own host
    through it, and sends nothing. The interception control is identical --
    that verdict is reached during the handshake either way -- and the
    provider's server no longer receives a request per address from an address
    it has never seen. ``request`` restores 7.22.1 exactly.

    **Screen, then confirm (7.53.0).** The sweep above is the *screen*, and it
    is unchanged. With ``confirm_attempts`` above 0, every address the screen
    called dead -- except a real refusal at the dial, which was the verdict
    least often wrong when measured -- is tried again in up to
    ``confirm_attempts`` confirm rounds, the first ``confirm_spacing`` seconds
    after the screen and each later one that long after the last, until it
    passes. So the fetch's number counts re-tests AFTER the screen (user
    decision 1: three), unlike ``check_endpoints``' ``attempts``, which counts
    the first try too. Those tries use
    ``confirm_connect_timeout`` for the dial, which is the live request path's
    own connect limit: an address the confirm stage admits is one live traffic
    would also wait for. A pass in any round is stored like a screen pass; an
    address that failed every round is *confirmed dead* -- not stored, counted,
    and charged to the reachability ladder once if a chain uses it. Measured
    on 1,000 feed addresses, re-tests 30 s apart found 82, then 33, then 47 of
    the 775 a shipped fetch called dead. ``0``, this function's default and
    never a setting value, is exactly the pre-7.53.0 pass.

    Every pass is labelled (:func:`pass_state`): ``working``, ``slow`` when its
    setup took more than ``slow_ms``, or ``flaky`` when it passed on fewer than
    half of three or more tries. Labels only; all three are kept.

    ``link_guard`` watches this machine's own connection while the sweep runs
    -- see :class:`LinkGuard`. ``link_probe`` replaces its handshake, for tests.
    """

    counters = progress if progress is not None else FetchProgress()
    halt = stop if stop is not None else asyncio.Event()
    at = _now()
    depth = (
        CHECK_DEPTH_TLS
        if str(check_depth).strip().lower() == CHECK_DEPTH_TLS
        else CHECK_DEPTH_REQUEST
    )
    counters.check_depth = depth

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

    # The offer is known, so the setting can finally be read against it.
    resolved = resolve_fetch_concurrency(
        requested=int(concurrency),
        mode=concurrency_mode,
        offered=len(ranked),
    )
    counters.note_concurrency(resolved)
    if resolved.note:
        logger.warning("PROXY FEEDS: {}", resolved.note)

    store = load_proxy_chains()
    used = in_use_labels(store)

    passing: list[tuple[str, ProxyEndpoint]] = []
    refused: list[tuple[str, ProxyEndpoint]] = []

    # Rank order, computed once: the workers finish in whatever order the
    # network allows, the page reads the list top to bottom, and now every
    # incremental write has to produce that same order too.
    order = {
        candidate_id(item.endpoint.address): index for index, item in enumerate(ranked)
    }
    write_lock = asyncio.Lock()
    unwritten = 0
    last_write = time.monotonic()

    async def flush(*, force: bool = False) -> None:
        """Write what has passed so far. In batches, never per address.

        A write that cannot land is never a reason to lose a worker or a
        verdict. On Windows the store's atomic rename fails outright while
        another handle has the file open -- and the page polling this store
        several times a second is exactly such a handle -- so the batch is put
        back and the next flush, or the forced one at the end, writes it again.
        """

        nonlocal unwritten, last_write
        if not persist:
            return
        async with write_lock:
            waited = time.monotonic() - last_write
            enough = unwritten >= FETCH_PERSIST_BATCH
            overdue = unwritten > 0 and waited >= persist_interval
            if not force and not enough and not overdue:
                return
            snapshot = sorted(passing, key=lambda pair: order.get(pair[0], len(order)))
            refusals = list(refused)
            pending = unwritten
            unwritten = 0
            last_write = time.monotonic()
            written = False
            failure: OSError | None = None
            # The last write of a pass is the one that must not be skipped, so
            # it is the one that gets to try again.
            for attempt in range(_PERSIST_ATTEMPTS if force else 1):
                try:
                    await asyncio.to_thread(_commit_fetch, snapshot, refusals)
                except OSError as exc:
                    failure = exc
                    await asyncio.sleep(_PERSIST_RETRY_SECONDS * (attempt + 1))
                    continue
                written = True
                break
            if not written:
                unwritten = pending
                logger.warning(
                    "PROXY FEEDS: could not write the offer yet ({}); keeping "
                    "the batch and trying again",
                    failure,
                )
                return
            counters.persisted = len(snapshot)
        if on_persist is not None:
            await asyncio.to_thread(on_persist)

    # One bound around the whole check, underneath every leg's own. If it ever
    # fires, something inside leaked a future -- which is precisely what the
    # unbounded ``wait_closed`` used to do on the Windows proactor loop -- and
    # the honest answer is that this address did not finish, not that the job
    # is stuck on it. A timed-out address is dead. It is never a pass: nothing
    # measured its tunnel.
    budget = check_budget(
        connect_timeout=connect_timeout, timeout=timeout, exit_ip_url=exit_ip_url
    )

    # The confirm stage's own bound: the same legs, with the live connect limit.
    confirm_dial = max(0.1, float(confirm_connect_timeout))
    confirm_bound = check_budget(
        connect_timeout=confirm_dial, timeout=timeout, exit_ip_url=exit_ip_url
    )
    # ``confirm_attempts`` is the number of confirm ROUNDS after the screen:
    # user decision 1 is three re-tests after the screen, which is what the
    # +162 was measured with. 0 -- the default here, never a setting value --
    # is the pre-7.53.0 pass with no confirm stage; ``rounds`` is the most
    # tries any one address gets, the screen included.
    confirm_rounds = max(0, int(confirm_attempts))
    rounds = 1 + confirm_rounds
    slow_limit = max(1, int(slow_ms))
    counters.confirm_attempts = confirm_rounds

    async def measure(url: str, dial: float, bound: float) -> ProxyCheckRecord:
        try:
            return await asyncio.wait_for(
                check_proxy(
                    url,
                    destination,
                    timeout=timeout,
                    exit_ip_url=exit_ip_url,
                    connect_timeout=dial,
                    depth=depth,
                ),
                bound,
            )
        except TimeoutError:
            logger.warning(
                "PROXY CHECK: {} did not finish within {:.0f}s -- abandoning it",
                mask_proxy_label(url),
                bound,
            )
            return ProxyCheckRecord(
                at=_now(),
                ok=False,
                tls=TLS_UNKNOWN,
                detail=f"did not finish within {bound:.0f}s",
                depth=depth,
                failure=FAILURE_OTHER,
            )

    # Every address's tries in this pass, and the screen failures still owed a
    # confirm round, by rank index. The last failing record is kept so the one
    # ledger charge a confirmed-dead address earns is made with its own reason.
    tries: dict[int, int] = {}
    confirming: dict[int, ProxyCheckRecord] = {}
    guard = (
        LinkGuard(
            destination,
            probe=link_probe,
            counters=counters,
            interval=LINK_GUARD_INTERVAL_SECONDS,
            timeout=LINK_GUARD_TIMEOUT_SECONDS,
        )
        if link_guard
        else None
    )

    def keep(index: int, record: ProxyCheckRecord) -> None:
        """File one address's verdict: a pass or a refusal is stored."""

        nonlocal unwritten
        item = ranked[index]
        tried = tries.get(index, 1)
        if record.ok:
            record = replace(
                record,
                state=pass_state(record, tried=tried, slow_ms=slow_limit),
                tries=tried if tried > 1 else record.tries,
            )
        endpoint = replace(
            as_candidate_endpoint(item, at),
            last_check=record,
            checked_for=provider_id,
        )
        if record.intercepted:
            counters.refused += 1
            refused.append((candidate_id(item.endpoint.address), endpoint))
        else:
            counters.working += 1
            if record.state == CHECK_STATE_SLOW:
                counters.slow += 1
            elif record.state == CHECK_STATE_FLAKY:
                counters.flaky += 1
            passing.append((candidate_id(item.endpoint.address), endpoint))
        unwritten += 1

    async def check_one(index: int, attempt: int, pending: deque[int]) -> None:
        """One try of one address, in the screen (attempt 1) or a confirm round."""

        item = ranked[index]
        url = item.endpoint.url
        label = mask_proxy_label(url)
        epoch = guard.epoch if guard is not None else 0
        dial, bound = (
            (connect_timeout, budget) if attempt == 1 else (confirm_dial, confirm_bound)
        )
        raw = await measure(url, dial, bound)
        if (
            not raw.ok
            and not raw.intercepted
            and guard is not None
            and guard.disturbed(epoch)
        ):
            # This machine's own link was failing while this check ran, so its
            # failure is no evidence about the proxy. It goes back in the queue
            # and is tried again once the link answers; nothing is marked dead.
            pending.append(index)
            return
        record = hold_refusal(label, raw)
        tries[index] = tries.get(index, 0) + 1
        if attempt == 1:
            counters.tested += 1
        if record.ok or record.intercepted:
            confirming.pop(index, None)
            apply_fetch_outcome(label, record, in_use=label in used)
            keep(index, record)
            return
        owed = attempt < rounds and (attempt > 1 or record.failure != FAILURE_REFUSED)
        if owed:
            # Not dead yet: re-tested in the next confirm round. The ladder is
            # not charged for a failure that has not been confirmed.
            confirming[index] = record
            return
        confirming.pop(index, None)
        apply_fetch_outcome(label, record, in_use=label in used)
        counters.dead += 1
        if attempt > 1:
            counters.confirmed_dead += 1

    async def run_stage(attempt: int, indexes: list[int]) -> None:
        pending: deque[int] = deque(indexes)

        async def worker() -> None:
            while True:
                if halt.is_set():
                    return
                if guard is not None:
                    await guard.wait_ok()
                if not pending:
                    return
                await check_one(pending.popleft(), attempt, pending)
                # Durable as it goes. A verdict worth keeping is on disk within
                # a batch or five seconds of being measured, so a stop, a crash
                # or a restart keeps it.
                await flush()
                # One yield per address, every address. This is the whole of "a
                # sweep of eight hundred strangers cannot sit in front of
                # /v1/messages": the workers hand the loop back between checks.
                await asyncio.sleep(0)

        workers = max(1, min(resolved.value, len(indexes))) if indexes else 0
        if workers:
            await _sweep(worker, workers, halt)

    watcher: asyncio.Task[None] | None = None
    if guard is not None and ranked:
        await guard.check()
        watcher = asyncio.create_task(guard.watch(halt))
    try:
        await run_stage(1, list(range(len(ranked))))
        # The confirm stage (7.53.0). Every screen failure except a real
        # refusal is tried again, in up to ``confirm_rounds`` rounds one spacing
        # apart, with the live connect limit. The first round starts one
        # spacing after the screen ends.
        for attempt in range(2, rounds + 1):
            if halt.is_set() or not confirming:
                break
            counters.confirm_attempt = attempt - 1
            counters.confirming = len(confirming)
            if await _halted_within(halt, confirm_spacing):
                break
            await run_stage(attempt, sorted(confirming))
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if guard is not None:
            guard.release()
    # Whatever is still owed a round was stopped before it was confirmed:
    # counted dead, never charged to the ladder, never called confirmed.
    counters.dead += len(confirming)
    confirming.clear()
    counters.confirming = 0

    # Back into rank order, and the last of it onto disk. The workers finish in
    # whatever order the network allows, and the page reads this list top to
    # bottom.
    passing.sort(key=lambda pair: order.get(pair[0], len(order)))
    await flush(force=True)

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
        slow=counters.slow,
        flaky=counters.flaky,
        confirmed_dead=counters.confirmed_dead,
        confirm_attempts=confirm_rounds,
    )
    logger.info(
        "PROXY FEEDS: {} of {} feed(s) answered; {} address(es) offered, {} "
        "tested at {} depth, {} at a time, up to {} tries each; {} working "
        "({} slow, {} flaky), {} dead ({} confirmed), {} refused{}",
        run.reached,
        feed_count,
        run.offered,
        run.tested,
        depth,
        resolved.value,
        rounds,
        run.working,
        run.slow,
        run.flaky,
        run.dead,
        run.confirmed_dead,
        run.refused,
        " (stopped early)" if run.stopped else "",
    )
    return run


async def _sweep(
    worker: Callable[[], Coroutine[object, object, None]],
    workers: int,
    halt: asyncio.Event,
) -> None:
    """Run the pool, and cancel it the moment somebody presses Stop.

    The pre-7.22.1 sweep gathered the workers and let ``halt`` be read at the
    top of each worker's loop, which meant Stop took effect *after* the check
    in flight returned. With 32 workers mid-handshake against strangers'
    machines that is thirty-two ten-second waits in the best case -- and in the
    case that actually happened on an operator's install, one of those checks
    never returned at all and Stop never took effect. So Stop cancels.

    Cancelling is safe here because the checker was made safe first: every
    ``await`` inside a check is bounded and every ``finally`` that touches the
    network aborts rather than waits, so a cancelled check unwinds immediately
    and leaves no socket in anybody's hands. What it costs is a verdict about
    each address that was mid-flight, which is exactly what Stop is asking for.

    Exceptions are collected rather than raised: one worker failing must not
    lose the sweep's results, and the sweep's caller writes the store
    afterwards either way. A cancellation of the *sweep itself* -- the server
    shutting down -- still propagates.
    """

    tasks = [asyncio.create_task(worker()) for _ in range(workers)]

    async def supervise() -> None:
        await halt.wait()
        for task in tasks:
            task.cancel()

    guard = asyncio.create_task(supervise())
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        guard.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await guard
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, asyncio.CancelledError
        ):
            logger.warning(
                "PROXY FEEDS: a sweep worker stopped early: {}: {}",
                type(outcome).__name__,
                outcome,
            )


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
    counters.slow = run.slow
    counters.flaky = run.flaky
    counters.confirmed_dead = run.confirmed_dead
    counters.confirm_attempts = run.confirm_attempts
    counters.confirming = 0
    counters.paused = False
    counters.pause_detail = ""


_JOB_LOCK = threading.Lock()
_JOB: FetchJob | None = None

#: The job this process inherited from the one before it, if any. Read once, at
#: startup, by :func:`recover_fetch_job`, and reported whenever no job is
#: running in *this* process -- which is how a server restarted mid-sweep
#: answers ``interrupted`` with real numbers instead of ``running`` for ever or
#: ``idle`` about work that is sitting on disk.
_RECOVERED: dict[str, object] | None = None


def _write_job_status(document: dict[str, object]) -> None:
    """Record what the job is doing, durably. Never raises.

    A status file that cannot be written costs the next start its ability to
    say ``interrupted``; a status file that raised would cost this start its
    fetch. The first is much the smaller loss.
    """

    with contextlib.suppress(Exception):
        write_json_document_atomically(proxy_fetch_status_path(), document)


def _read_job_status() -> dict[str, object] | None:
    """The last durable job record, or ``None``. Never raises."""

    try:
        raw = proxy_fetch_status_path().read_text(encoding="utf-8")
    except OSError, ValueError:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def recover_fetch_job() -> str:
    """Read the job the previous process left behind. Called once, at startup.

    A fetch is memory: the job object, its counters, its stop event and its
    task all die with the server. What survives is the store the sweep wrote as
    it went, and this record of what was doing the writing.

    So a job left ``running`` by a process that is no longer here is reported
    ``interrupted``: nothing is testing anything, the counters say how far it
    got, and the addresses that passed are on offer because they were persisted
    while it ran. A job that had already finished is reported as it finished --
    a restart is not a reason to forget that a fetch found 355 working
    addresses ten minutes ago.

    Returns the state it recovered, or ``""`` when there was nothing to
    recover.
    """

    global _RECOVERED
    document = _read_job_status()
    if document is None:
        _RECOVERED = None
        return ""
    recovered = dict(IDLE_STATUS) | document
    state = str(recovered.get("state") or "")
    if state == "running":
        recovered["state"] = "interrupted"
        recovered["stopping"] = False
        recovered["detail"] = (
            "The server restarted while this fetch was running, so it stopped "
            "where it was. Everything that had already passed was written as "
            "it was found and is still on offer below."
        )
        logger.info(
            "PROXY FEEDS: a fetch was interrupted by a restart at {} of {} "
            "tested; {} working address(es) were already saved",
            recovered.get("tested"),
            recovered.get("total"),
            recovered.get("persisted"),
        )
    elif state in ("", "idle"):
        _RECOVERED = None
        return ""
    _RECOVERED = recovered
    return str(recovered["state"])


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
        inherited = _RECOVERED
    if job is not None:
        return job.as_document()
    return dict(IDLE_STATUS) if inherited is None else dict(inherited)


def running_fetch_id() -> str:
    """The id of the fetch in flight, or ``""`` when none is."""

    with _JOB_LOCK:
        job = _JOB
    return job.job_id if job is not None and job.state == "running" else ""


def stop_fetch(job_id: str = "") -> str:
    """Ask the running fetch to stop, and cancel what it has in flight.

    Returns the id it asked to stop, or ``""`` when nothing was running or the
    id named a different job. Idempotent: pressing Stop twice sets an event
    that is already set and returns the same id, because an operator watching a
    button that still says "Stopping..." will press it again and must not be
    punished for it.

    What has already passed is kept. It was written while the sweep ran, and
    the remainder is written as the sweep settles -- after the stop, never
    instead of it.
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
    concurrency_mode: str = DEFAULT_FETCH_CONCURRENCY_MODE,
    check_depth: str = DEFAULT_FETCH_CHECK_DEPTH,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    feed_timeout: float = FEED_TIMEOUT_SECONDS,
    persist_interval: float = FETCH_PERSIST_INTERVAL_SECONDS,
    limit: int = 0,
    exit_ip_url: str = "",
    confirm_attempts: int = 0,
    confirm_spacing: float = PROXY_CHECK_CONFIRM_SPACING_SECONDS_DEFAULT,
    confirm_connect_timeout: float = PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT,
    slow_ms: int = PROXY_CHECK_SLOW_MS_DEFAULT,
    link_guard: bool = False,
    link_probe: LinkProbe | None = None,
) -> FetchJob:
    """Start a fetch and return at once. One at a time, process-wide."""

    with _JOB_LOCK:
        global _JOB, _RECOVERED
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
        # A new job supersedes whatever the last process left behind.
        _RECOVERED = None

    # Durable from the first moment, so a server killed one second into a sweep
    # still reports an interrupted job rather than an idle one.
    await asyncio.to_thread(_write_job_status, job.as_document())

    async def body() -> None:
        try:
            job.run = await run_fetch_pass(
                provider_id=provider_id,
                destination=destination,
                concurrency=concurrency,
                connect_timeout=connect_timeout,
                concurrency_mode=concurrency_mode,
                check_depth=check_depth,
                timeout=timeout,
                feed_timeout=feed_timeout,
                persist_interval=persist_interval,
                limit=limit,
                exit_ip_url=exit_ip_url,
                progress=job.progress,
                stop=job.stop,
                on_persist=lambda: _write_job_status(job.as_document()),
                confirm_attempts=confirm_attempts,
                confirm_spacing=confirm_spacing,
                confirm_connect_timeout=confirm_connect_timeout,
                slow_ms=slow_ms,
                link_guard=link_guard,
                link_probe=link_probe,
            )
            job.state = "stopped" if job.run.stopped else "done"
            # The finished run is the authoritative answer; the live counters
            # were only ever the sweep's running commentary on it. Copying it
            # over means a page that polls once after the end reads the same
            # numbers as one that watched the whole thing.
            _settle(job.progress, job.run)
        except asyncio.CancelledError:
            # Not ``stopped``: nobody pressed Stop. The server went away under
            # it, which is the same thing that happens to a job when the
            # process is killed outright -- and it must read the same way, so
            # that "interrupted" means one thing on the page rather than two.
            job.state = "interrupted"
            job.detail = (
                "The server stopped while this fetch was running, so the fetch "
                "stopped with it. Everything that had already passed was "
                "written as it was found and is still on offer below."
            )
            raise
        except Exception as exc:  # pragma: no cover - defensive
            job.state = "failed"
            job.detail = f"{type(exc).__name__}: {exc}"
            logger.warning("PROXY FEEDS: the fetch failed: {}", job.detail)
        finally:
            job.finished_at = time.monotonic()
            # The last word, and the one a restart would read: a job that
            # reached here is finished, whatever it finished as, and must never
            # come back as ``interrupted``. Written on the loop rather than in
            # a thread precisely because this runs in a ``finally``: a job
            # being cancelled has no time left to hand to an executor, and one
            # small atomic write once per job is not what makes a loop late.
            _write_job_status(job.as_document())

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
    """Forget the job slot. For tests, and for a runtime that is shutting down.

    The recovered record goes with it: "no job" has to mean the same thing
    whether this process never ran one or was told to forget the one it did.
    """

    with _JOB_LOCK:
        global _JOB, _RECOVERED
        _JOB = None
        _RECOVERED = None


__all__ = [
    "DEFAULT_FETCH_CONCURRENCY_MODE",
    "FETCH_CONCURRENCY_MODES",
    "FETCH_CONCURRENCY_MODE_FIXED",
    "FETCH_CONCURRENCY_MODE_PERCENT",
    "FETCH_PERSIST_BATCH",
    "FETCH_PERSIST_INTERVAL_SECONDS",
    "FETCH_STATES",
    "IDLE_STATUS",
    "LINK_GUARD_INTERVAL_SECONDS",
    "LINK_GUARD_TIMEOUT_SECONDS",
    "FetchAlreadyRunning",
    "FetchJob",
    "FetchProgress",
    "FetchRun",
    "LinkGuard",
    "LinkProbe",
    "ResolvedConcurrency",
    "fetch_status",
    "in_use_labels",
    "pass_state",
    "probe_link",
    "recover_fetch_job",
    "reset_fetch_job",
    "resolve_fetch_concurrency",
    "run_fetch_pass",
    "running_fetch_id",
    "start_fetch",
    "stop_fetch",
    "wait_for_fetch",
]
