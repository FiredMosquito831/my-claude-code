"""One pass per free-text search, shared by every answer that asks it (7.91.1).

A free-text search (``q``) is the one request-log read no index can serve:
every row in the window has both of its stored bodies decompressed, parsed and
tested (``fcc_bodies_match``). Until 7.91.1 every answer for a search ran that
predicate on its own -- the page, the count, the stats cards (fourteen
statements), the cost (seven), TTFT, no-answer, origin and the auto-refresh
pulse: 27 passes for one page load, minutes each over all time, on the event
loop's default executor, where nothing could stop them. A saved search re-ran
all of them on every start, and on 2026-10-09 six of them held the server.

Here a search is read **once**. Its pass (``RequestLogStore.match_rows``) runs
newest first on the search pool, and every answer is computed from the rows it
found (``MatchedRows``), with every other clause of the answer -- the window,
a status sub-label, the Exit filter, a method's own filter -- still the SQL it
was. The rows are the predicate's own answer, so every count, row and order
is the one the predicate gave.

What this module holds to, and the tests hold it:

- **One pass per question.** A search is keyed on its terms and the page's
  filters other than the window, the Exit filter and a status sub-label (those
  three stay SQL beside the rows). A pass read down to a time serves every
  window that starts at or after it; a wider window continues the pass below
  where it stopped. Rows written after a pass are tested when the next answer
  is asked for, and only those -- so the pulse with a search costs the new rows.
- **No thread ever waits.** Waiters are coroutines: they await a future, and
  every half second they ask whether their client is still connected.
- **A search nobody waits for stops.** Three seconds after its last waiter
  left, its statement is interrupted (``sqlite3.Connection.interrupt``,
  measured at 0.5-1.3 ms) and its worker is free. What it had read is kept,
  so asking again continues where it stopped.
- **One pass at a time per log, newest question first.** A new search stops
  the pass that is running (it continues later, from where it stopped, if
  anyone still waits for it), so the search typed last is the one answered
  first, and the pool's second worker is always free to answer from rows
  already read.
- **A stop stops them all**, in milliseconds, so a shutdown never waits on a
  scan.
- **It says how far it has got** (7.91.2): every row a pass reads is counted,
  matched or not, so the page can show "searched back to 12 Aug · 412,000 of
  598,683 rows" while the count and the cards wait for the pass to end.
  Asking (``progress``) starts nothing and keeps nothing running.
"""

import asyncio
import contextlib
import itertools
import json
import math
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from my_claude_code.core.cancelled_reasons import split_status_filter
from my_claude_code.core.request_log import (
    MatchedRows,
    RequestLogStore,
    normalized_search,
    watch_connections,
)
from my_claude_code.core.stop_deadline import stop_deadline
from my_claude_code.core.success_reasons import split_success_status_filter

#: How long a search keeps running after its last waiter left, in seconds.
#: Long enough for a page reload to come back for it; short enough that a
#: search nobody is looking at stops at once.
SEARCH_GRACE_SECONDS = 3.0

#: How often a waiter checks that its client is still there.
WAITER_POLL_SECONDS = 0.5

#: Searches kept, finished or stopped, for the next page, the next refresh and
#: the pulse. Each holds its matched rowids and timestamps: 16 bytes a row.
SEARCH_JOBS_KEPT = 4

#: Matched rows handed over per batch while a pass runs, and the longest a
#: batch waits, so a page's first rows arrive while the pass goes on.
_BATCH_ROWS = 64
_BATCH_SECONDS = 0.05

#: SQLite steps between two checks of a stopped search's flag. The interrupt
#: stops the statement running at that moment; this stops the next one, which
#: an interrupt arriving between two statements would not.
_STOP_CHECK_STEPS = 10_000

#: Filters that are the search's own: a pass reads them with the predicate.
#: ``since``/``until`` are the window, ``exit`` and a status sub-label stay SQL
#: beside the rows (see ``_job_filters``).
_PASS_FILTERS = (
    "provider",
    "model",
    "endpoint",
    "key",
    "local",
    "harness",
    "session",
    "folder",
)


class SearchStopped(Exception):
    """The server is stopping, so this search will not be answered."""


class SearchAbandoned(Exception):
    """The client that asked went away before the answer was ready."""


def is_search(q: str | None) -> bool:
    """Whether ``q`` is a free-text search at all (blank is not)."""

    return bool(normalized_search(q))


def _base_status(status: str | None) -> str | None:
    """The status a pass reads: the page's, without a sub-label.

    Two answers widen the page's status: the cancelled breakdown reads every
    cancelled row whatever sub-label the page picked, and the no-answer
    breakdown every success. A pass read for the plain status serves both, and
    the sub-label stays a clause of each answer that asked for it.
    """

    status, _cancelled = split_status_filter(status)
    status, _success = split_success_status_filter(status)
    return status


class _Stop:
    """Stops one piece of search work: interrupts its connections, refuses new ones."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self.stopped = False

    def watch(self, conn: sqlite3.Connection) -> None:
        """Handed every connection the work opens (``watch_connections``)."""

        with self._lock:
            if self.stopped:
                raise sqlite3.OperationalError("interrupted")
            self._connections.append(conn)
        conn.set_progress_handler(self._check, _STOP_CHECK_STEPS)

    def _check(self) -> int:
        return 1 if self.stopped else 0

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            connections = list(self._connections)
        for conn in connections:
            # A connection already closed has nothing left to stop.
            with contextlib.suppress(sqlite3.ProgrammingError):
                conn.interrupt()


@dataclass(frozen=True)
class _Exact:
    """Which rows a search has read: ``ts_epoch >= at`` (``> at`` if exclusive)."""

    at: float = math.inf
    inclusive: bool = False

    def covers(self, since: float) -> bool:
        if self.inclusive:
            return self.at <= since
        return self.at < since


class _Scan:
    """The rows one segment of a pass has read, row by row (7.91.2).

    Called by the pass's worker for every row it reads, matched or not, so the
    page can say how far a search has got. Plain attributes: each is written
    by that one worker and only read elsewhere, for a progress line.
    """

    __slots__ = ("at", "at_before", "base", "group", "read", "shown_before")

    def __init__(
        self, base: int, shown_before: int = 0, at_before: float | None = None
    ) -> None:
        # Rows earlier segments read that this one does not read again.
        self.base = base
        self.read = 0
        # The timestamp of the last row read, and how many rows had been read
        # before the first row with that timestamp.
        self.at: float | None = None
        self.group = base
        # What the segment before this one had shown: a continued pass reads
        # the rows past its last match again, and neither the count nor the
        # time read back to may go back.
        self.shown_before = shown_before
        self.at_before = at_before

    def __call__(self, ts: float) -> None:
        if ts != self.at:
            self.at = ts
            self.group = self.base + self.read
        self.read += 1

    @property
    def total(self) -> int:
        return self.base + self.read

    @property
    def shown(self) -> int:
        return max(self.shown_before, self.base + self.read)

    @property
    def oldest(self) -> float | None:
        stamps = [ts for ts in (self.at, self.at_before) if ts is not None]
        return min(stamps) if stamps else None


@dataclass(frozen=True)
class _Attempt:
    done: bool
    result: Any = None


_NOT_READY = _Attempt(done=False)


class SearchJob:
    """One search's pass and the rows it matched.

    The rows and how far they reach are shared with worker threads and kept
    under ``_lock``, which is only ever held to copy or append. Everything else
    belongs to the event loop the search was asked on.
    """

    def __init__(
        self,
        store: RequestLogStore,
        q: str,
        filters: dict[str, Any],
        until: float | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.store = store
        self.q = q
        self.filters = filters
        self.until = until
        self.loop = loop
        # Shared with the workers.
        self._lock = threading.Lock()
        self._rowids = array("q")
        self._ts = array("d")
        self._exact = _Exact()
        self._high_water: int | None = None
        self._mark: str | None = None
        self._version = 0
        self._resets = 0
        self._snapshot: tuple[tuple[int, int, float, float], MatchedRows] | None = None
        # 7.91.2: rows the pass has read that a continued pass will not read
        # again (every row newer than ``_exact``), and the running segment's
        # own count -- the "rows read" of the progress line.
        self._read_kept = 0
        self._scan: _Scan | None = None
        # The event loop's.
        # The rows the pass reads in its window, ``(floor, rows)``: counted
        # once per window by the progress route, without the search.
        self.window_rows: tuple[float, int] | None = None
        self.floor = math.inf
        self.waiters = 0
        self.priority = 0
        self.idle_since = time.monotonic()
        self.segment: asyncio.Future[bool] | None = None
        self.segment_stop: _Stop | None = None
        self.error: BaseException | None = None
        # Set when the server stops it: its waiters are told, never answered.
        self.stopped = False
        self._changed: asyncio.Future[None] = loop.create_future()

    # ---------------------------------------------------------------- state

    @property
    def exact(self) -> _Exact:
        with self._lock:
            return self._exact

    @property
    def version(self) -> int:
        with self._lock:
            return self._version + self._resets

    def progress(self) -> dict[str, Any]:
        """How far the pass has got: rows matched, rows read, the time read back to.

        ``read`` counts every row the pass has read, matched or not (7.91.2).
        ``searched_back_to`` is the time of the oldest row read, ``None``
        before the first; ``finished`` says the whole window has been read.
        """

        with self._lock:
            exact = self._exact
            matched = len(self._rowids)
            read = self._read_kept
            scan = self._scan
        back_to = exact.at
        if scan is not None:
            read = max(read, scan.shown)
            oldest = scan.oldest
            if oldest is not None:
                back_to = oldest
        return {
            "matched": matched,
            "read": read,
            "searched_back_to": None if back_to == math.inf else back_to,
            "finished": exact.inclusive,
        }

    def covers(self, since: float | None) -> bool:
        return self.exact.covers(-math.inf if since is None else since)

    def needs_segment(self) -> bool:
        if self.floor == math.inf:
            # Nobody has asked it for a window yet.
            return False
        return not self.covers(None if self.floor == -math.inf else self.floor)

    def wanted(self) -> bool:
        if self.waiters > 0:
            return True
        return time.monotonic() - self.idle_since < SEARCH_GRACE_SECONDS

    # ---------------------------------------------------------- loop events

    def changed(self) -> asyncio.Future[None]:
        return self._changed

    def _on_change(self) -> None:
        done, self._changed = self._changed, self.loop.create_future()
        if not done.done():
            done.set_result(None)

    def notify(self) -> None:
        """Wake this search's waiters; safe from any thread."""

        # A RuntimeError is a loop that is gone (a test client's, or a stopped
        # server's): nobody is left to wake.
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self._on_change)

    # ------------------------------------------------------- worker threads

    def _segment_where(self) -> dict[str, Any]:
        return {**self.filters, "q": self.q, "until": self.until}

    def run_segment(
        self, since: float | None, below: float | None, inclusive: bool, stop: _Stop
    ) -> bool:
        """Read the search from ``below`` down to ``since``; True when finished.

        On a search pool worker. Matched rows are appended as they arrive, so
        a page can be answered while the rest is still being read.
        """

        with self._lock:
            resets = self._resets
        with watch_connections(stop.watch):
            if self._high_water is None:
                high_water = self.store.max_rowid()
                mark = self.store.search_mark(self.filters)
                with self._lock:
                    if self._resets == resets and self._high_water is None:
                        self._high_water = high_water
                        self._mark = mark
            with self._lock:
                before = self._scan
                scan = (
                    _Scan(self._read_kept, before.shown, before.oldest)
                    if before is not None
                    else _Scan(self._read_kept)
                )
                if self._resets == resets:
                    self._scan = scan
            # Rows read before the last match's timestamp: what a continued
            # pass, which starts again at that timestamp, will not read again.
            kept = scan.base
            batch: list[tuple[int, float]] = []
            flushed = time.monotonic()
            rows = self.store.match_rows(
                **self._segment_where(),
                since=since,
                below=below,
                below_inclusive=inclusive,
                on_read=scan,
            )
            try:
                for row in rows:
                    batch.append(row)
                    kept = scan.group
                    if (
                        len(batch) >= _BATCH_ROWS
                        or time.monotonic() - flushed >= _BATCH_SECONDS
                    ):
                        self._append(batch, resets, _Exact(batch[-1][1]), kept)
                        batch = []
                        flushed = time.monotonic()
                        self.notify()
            except sqlite3.OperationalError:
                if not stop.stopped:
                    raise
                if batch:
                    self._append(batch, resets, _Exact(batch[-1][1]), kept)
                self.notify()
                return False
            finally:
                rows.close()
            done = _Exact(-math.inf if since is None else since, inclusive=True)
            self._append(batch, resets, done, scan.total)
            self.notify()
            return True

    def _append(
        self, batch: list[tuple[int, float]], resets: int, exact: _Exact, kept: int
    ) -> None:
        with self._lock:
            if self._resets != resets:
                return
            for rowid, ts in batch:
                self._rowids.append(rowid)
                self._ts.append(ts)
            if exact.at <= self._exact.at:
                self._exact = exact
                self._read_kept = max(self._read_kept, kept)
            self._version += 1

    def catch_up(self) -> bool:
        """Test the rows written since the pass; False if the pass must start over.

        On a search pool worker, before every answer. A backfill that rewrote
        a filtered column, or a log that shrank under the pass (cleared by
        another process), throws the rows away: they are read again rather
        than trusted.
        """

        with self._lock:
            high_water = self._high_water
            mark = self._mark
            resets = self._resets
        if high_water is None:
            return True
        now = self.store.max_rowid()
        if now < high_water or self.store.search_mark(self.filters) != mark:
            self._reset(resets)
            return False
        if now == high_water:
            return True
        found = list(
            self.store.match_rows(
                **self._segment_where(),
                rowid_after=high_water,
                rowid_through=now,
            )
        )
        with self._lock:
            if self._resets != resets:
                return False
            for rowid, ts in found:
                self._rowids.append(rowid)
                self._ts.append(ts)
            self._high_water = max(now, self._high_water or now)
            self._version += 1
        return True

    def reset(self) -> None:
        """Forget every row read; the next answer waits for a new pass."""

        with self._lock:
            resets = self._resets
        self._reset(resets)

    def _reset(self, resets: int) -> None:
        with self._lock:
            if self._resets != resets:
                return
            self._rowids = array("q")
            self._ts = array("d")
            self._exact = _Exact()
            self._high_water = None
            self._mark = None
            self._snapshot = None
            self._read_kept = 0
            self._scan = None
            self._resets += 1
        self.window_rows = None
        self.notify()

    def snapshot(
        self, since: float | None, until: float | None
    ) -> tuple[MatchedRows, _Exact]:
        """The matched rows a window can use, and how far they are exact.

        Rows outside the window are left out to keep the set small; the
        answer's own window clause is what decides, so a row at the edge is
        kept rather than judged here.
        """

        low = -math.inf if since is None else since - 1.0
        high = math.inf if until is None else until + 1.0
        with self._lock:
            exact = self._exact
            key = (self._version, self._resets, low, high)
            cached = self._snapshot
            if cached is not None and cached[0] == key:
                return cached[1], exact
            rowids = array("q", self._rowids)
            stamps = array("d", self._ts)
        kept = [
            rowid for rowid, ts in zip(rowids, stamps, strict=True) if low <= ts <= high
        ]
        matched = MatchedRows(q=self.q, rowids=json.dumps(sorted(set(kept))))
        with self._lock:
            if self._version == key[0] and self._resets == key[1]:
                self._snapshot = (key, matched)
        return matched, exact

    def answer(
        self,
        since: float | None,
        until: float | None,
        compute: Callable[[MatchedRows], Any],
        settled: Callable[[Any, float], bool] | None,
        stop: _Stop,
    ) -> _Attempt:
        """Compute one answer from the rows read so far, if they suffice.

        On a search pool worker. ``settled`` is for an answer that may come
        before the pass ends -- the page of rows -- and says whether what was
        computed from the rows read so far is already the final answer.
        """

        with watch_connections(stop.watch):
            if not self.catch_up():
                return _NOT_READY
            matched, exact = self.snapshot(since, until)
            covered = exact.covers(-math.inf if since is None else since)
            if not covered and settled is None:
                return _NOT_READY
            result = compute(matched)
            # Every row newer than ``exact.at`` has been read, so an answer
            # whose rows all are is the answer the finished pass would give.
            if covered or (settled is not None and settled(result, exact.at)):
                return _Attempt(done=True, result=result)
            return _NOT_READY


def _job_filters(filters: dict[str, Any]) -> dict[str, Any]:
    picked = {name: filters.get(name) for name in _PASS_FILTERS}
    picked["status"] = _base_status(filters.get("status"))
    return picked


class _Lane:
    """One log's search passes on one event loop: at most one runs at a time."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        run: Callable[..., Awaitable[Any]],
    ) -> None:
        self.loop = loop
        self.run = run
        self.jobs: list[SearchJob] = []
        self.running: SearchJob | None = None

    def schedule(self) -> None:
        wanted = [
            job
            for job in self.jobs
            if job.error is None and job.needs_segment() and job.wanted()
        ]
        if not wanted:
            return
        best = max(wanted, key=lambda job: job.priority)
        if self.running is None:
            self._start(best)
        elif self.running is not best and best.priority > self.running.priority:
            # A newer question: stop this pass where it is. It continues from
            # there once nothing newer is waiting, if anyone still waits for it.
            stop = self.running.segment_stop
            if stop is not None:
                stop.stop()

    def _start(self, job: SearchJob) -> None:
        exact = job.exact
        since = None if job.floor == -math.inf else job.floor
        below = None if exact.at == math.inf else exact.at
        stop = _Stop()
        job.segment_stop = stop
        self.running = job
        segment = asyncio.ensure_future(
            self.run(job.run_segment, since, below, not exact.inclusive, stop)
        )
        job.segment = segment
        segment.add_done_callback(lambda done: self._finished(job, stop, done))

    def _finished(self, job: SearchJob, stop: _Stop, done: asyncio.Future[Any]) -> None:
        if self.running is job:
            self.running = None
        job.segment = None
        job.segment_stop = None
        if not done.cancelled():
            error = done.exception()
            if error is not None and not stop.stopped:
                logger.warning("Request log search failed: {}", error)
                job.error = error
        job.notify()
        self.schedule()

    def reap(self) -> None:
        """Stop the pass whose waiters have all been gone for the grace period."""

        job = self.running
        if job is not None and not job.wanted() and job.segment_stop is not None:
            job.segment_stop.stop()
        self.schedule()


class SearchJobs:
    """The process's searches, by question (see the module docstring)."""

    def __init__(self) -> None:
        self._jobs: OrderedDict[tuple[Any, ...], SearchJob] = OrderedDict()
        self._lanes: dict[tuple[int, int], _Lane] = {}
        self._answers: set[_Stop] = set()
        self._priority = itertools.count(1)

    # ---------------------------------------------------------------- jobs

    def _key(
        self,
        loop: asyncio.AbstractEventLoop,
        store: RequestLogStore,
        q: str,
        filters: dict[str, Any],
        until: float | None,
    ) -> tuple[Any, ...]:
        return (
            id(loop),
            id(store),
            store.clear_generation,
            q,
            *(filters[name] for name in sorted(filters)),
            until,
        )

    def _lane(
        self,
        loop: asyncio.AbstractEventLoop,
        store: RequestLogStore,
        run: Callable[..., Awaitable[Any]],
    ) -> _Lane:
        key = (id(loop), id(store))
        lane = self._lanes.get(key)
        if lane is None or lane.loop is not loop:
            lane = _Lane(loop, run)
            self._lanes[key] = lane
        return lane

    def job(
        self,
        store: RequestLogStore,
        filters: dict[str, Any],
        run: Callable[..., Awaitable[Any]],
    ) -> SearchJob:
        """The search these filters ask, created if it is new. On the event loop."""

        loop = asyncio.get_running_loop()
        q = normalized_search(filters.get("q"))
        picked = _job_filters(filters)
        until = filters.get("until")
        key = self._key(loop, store, q, picked, until)
        job = self._jobs.get(key)
        if job is None:
            lane = self._lane(loop, store, run)
            job = SearchJob(store, q, picked, until, loop)
            job.priority = next(self._priority)
            self._jobs[key] = job
            lane.jobs.append(job)
            self._evict()
        else:
            self._jobs.move_to_end(key)
        return job

    def find(self, store: RequestLogStore, filters: dict[str, Any]) -> SearchJob | None:
        """The search these filters ask, if one exists (7.91.2). On the event loop.

        Never creates one, never counts as its use: asking how far a search
        has got must neither start a pass nor keep one from being dropped.
        """

        loop = asyncio.get_running_loop()
        q = normalized_search(filters.get("q"))
        key = self._key(loop, store, q, _job_filters(filters), filters.get("until"))
        return self._jobs.get(key)

    def _evict(self) -> None:
        """Keep the newest searches; never one that is waited for or running."""

        for key, job in list(self._jobs.items()):
            if job.loop.is_closed():
                self._drop(key)
        excess = len(self._jobs) - SEARCH_JOBS_KEPT
        # Oldest first: the dictionary is in order of last use.
        for key, job in list(self._jobs.items()):
            if excess <= 0:
                return
            if job.waiters == 0 and job.segment is None:
                self._drop(key)
                excess -= 1

    def _drop(self, key: tuple[Any, ...]) -> None:
        job = self._jobs.pop(key, None)
        if job is None:
            return
        if job.segment_stop is not None:
            job.segment_stop.stop()
        lane = self._lanes.get((id(job.loop), id(job.store)))
        if lane is not None and job in lane.jobs:
            lane.jobs.remove(job)
            if not lane.jobs:
                self._lanes.pop((id(job.loop), id(job.store)), None)

    def forget(self, store: RequestLogStore) -> None:
        """Forget what ``store``'s searches read: the log was cleared under them.

        A search still waited for reads the cleared log again from the start;
        one nobody waits for is dropped. On the event loop.
        """

        for key, job in list(self._jobs.items()):
            if job.store is not store:
                continue
            if job.waiters == 0 and job.segment is None:
                self._drop(key)
                continue
            if job.segment_stop is not None:
                job.segment_stop.stop()
            job.reset()

    def stop_all(self) -> None:
        """Stop every pass and every answer, and forget every search.

        A server stop: each statement is interrupted where it is, and every
        waiter is told the search will not be answered. On the event loop.
        """

        for key, job in list(self._jobs.items()):
            job.stopped = True
            self._drop(key)
            job.notify()
        for stop in list(self._answers):
            stop.stop()
        self._lanes.clear()

    def running(self) -> list[SearchJob]:
        return [job for job in self._jobs.values() if job.segment is not None]

    async def progress(
        self,
        store: RequestLogStore,
        filters: dict[str, Any],
        *,
        run: Callable[..., Awaitable[Any]],
    ) -> dict[str, Any] | None:
        """How far the search these filters ask has got; None if there is none.

        7.91.2, for the page's progress line ("searched back to 12 Aug ·
        412,000 of 598,683 rows"). Reads the search's state and nothing else
        (``find``): no pass is started, no waiter added, no body read. The one
        query is ``total``, the rows the pass reads in its window -- the
        page's filters without the search, a plain count -- run by ``run``
        once per window and kept on the search.
        """

        job = self.find(store, filters)
        if job is None:
            return None
        progress = job.progress()
        floor = job.floor
        total: int | None = None
        if floor != math.inf:
            cached = job.window_rows
            if cached is not None and cached[0] == floor:
                total = cached[1]
            else:
                since = None if floor == -math.inf else floor
                total = int(
                    await run(
                        lambda: store.count_requests(
                            **job.filters, since=since, until=job.until
                        )
                    )
                )
                job.window_rows = (floor, total)
        back_to = progress["searched_back_to"]
        return {
            "finished": progress["finished"],
            "matched": progress["matched"],
            "read": progress["read"],
            "total": total,
            # A JSON number: the oldest row's time, never minus infinity.
            "searched_back_to": back_to
            if back_to is not None and math.isfinite(back_to)
            else None,
        }

    # --------------------------------------------------------------- answers

    async def answer[T](
        self,
        store: RequestLogStore,
        filters: dict[str, Any],
        compute: Callable[[MatchedRows], T],
        *,
        run: Callable[..., Awaitable[Any]],
        disconnected: Callable[[], Awaitable[bool]],
        settled: Callable[[T, float], bool] | None = None,
    ) -> T:
        """``compute`` over this search's matched rows, once they suffice.

        ``filters`` are the request's own: the window, a status sub-label and
        the Exit filter are applied by ``compute``'s SQL, beside the rows.
        """

        job = self.job(store, filters, run)
        lane = self._lane(job.loop, store, run)
        since = filters.get("since")
        until = filters.get("until")
        floor = -math.inf if since is None else since
        if job.error is not None and job.waiters == 0:
            # A pass that failed told the requests that were waiting for it;
            # the next question gets a pass of its own rather than the error.
            job.error = None
        job.waiters += 1
        try:
            if floor < job.floor:
                job.floor = floor
                if job.needs_segment():
                    # A wider window is a new question for the lane.
                    job.priority = next(self._priority)
            lane.schedule()
            tried = -1
            while True:
                if job.stopped or stop_deadline().requested:
                    raise SearchStopped
                if job.error is not None:
                    raise job.error
                # Taken before anything is read, so a change that lands while
                # this waiter is busy still wakes it.
                change = job.changed()
                version = job.version
                if job.covers(since) or (settled is not None and version != tried):
                    tried = version
                    attempt = await self._attempt(
                        job, since, until, compute, settled, run, disconnected
                    )
                    if attempt.done:
                        return attempt.result
                    lane.schedule()
                await asyncio.wait({change}, timeout=WAITER_POLL_SECONDS)
                if await disconnected():
                    raise SearchAbandoned
        finally:
            job.waiters -= 1
            if job.waiters == 0:
                job.idle_since = time.monotonic()
                if not job.loop.is_closed():
                    job.loop.call_later(SEARCH_GRACE_SECONDS + 0.05, lane.reap)

    async def _attempt[T](
        self,
        job: SearchJob,
        since: float | None,
        until: float | None,
        compute: Callable[[MatchedRows], T],
        settled: Callable[[T, float], bool] | None,
        run: Callable[..., Awaitable[Any]],
        disconnected: Callable[[], Awaitable[bool]],
    ) -> _Attempt:
        stop = _Stop()
        self._answers.add(stop)
        work = asyncio.ensure_future(
            run(job.answer, since, until, compute, settled, stop)
        )
        try:
            while True:
                done, _pending = await asyncio.wait({work}, timeout=WAITER_POLL_SECONDS)
                if done:
                    if stop.stopped:
                        # Stopped by the server's stop, mid-statement.
                        raise SearchStopped
                    return work.result()
                if job.stopped or stop_deadline().requested:
                    raise SearchStopped
                if await disconnected():
                    raise SearchAbandoned
        except BaseException:
            stop.stop()
            # Its result is nobody's now; collected so it is never reported
            # as an exception nobody retrieved.
            work.add_done_callback(_collect)
            raise
        finally:
            self._answers.discard(stop)


def _collect(done: asyncio.Future[Any]) -> None:
    if not done.cancelled():
        done.exception()


_search_jobs = SearchJobs()


def search_jobs() -> SearchJobs:
    """The process's searches (see ``SearchJobs``)."""

    return _search_jobs
