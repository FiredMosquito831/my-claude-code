"""One pass per free-text search, shared, cancellable and never parked (7.91.1).

A saved all-time search re-ran 27 body scans on every start, each minutes long,
on the default executor, and nothing could stop one (2026-10-09). These tests
hold the replacement to its promises with a real log and the real predicate:

* the answers of one page load read every row of the window once, together;
* a narrower window reuses a pass, a wider one reads only the older rows, and
  rows written after a pass are the only ones read again;
* the page of rows is answered while the pass is still reading;
* a newer search stops the running pass within milliseconds, the pass
  continues later from where it stopped, and both answers are right;
* a search nobody waits for stops after the grace period and frees its worker;
* a server stop stops every search at once and tells its waiters;
* rows matched before a clear, or before a backfill rewrote a filtered column,
  are never served after it.
"""

import asyncio
import random
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.api import search_pool
from my_claude_code.api.search_pool import close_search_pool, run_on_search_pool
from my_claude_code.application import search_jobs as search_jobs_module
from my_claude_code.application.search_jobs import (
    SearchAbandoned,
    SearchStopped,
    search_jobs,
)
from my_claude_code.core.request_log import MatchedRows, RequestLogStore
from tests.support.search_log import build_search_log, make_record

pytestmark = pytest.mark.local_serial


class Reads:
    """Counts (and can slow) the body predicate, the expensive part of a pass."""

    def __init__(self, store: RequestLogStore, delay: float = 0.0) -> None:
        self.calls = 0
        self.threads: set[str] = set()
        self.delay = delay
        self._lock = threading.Lock()
        self._real = store._bodies_match

    def __call__(self, *args: Any) -> int:
        with self._lock:
            self.calls += 1
            self.threads.add(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        return self._real(*args)


@pytest.fixture
def log(tmp_path) -> Iterator[tuple[RequestLogStore, list[float], Path]]:
    path = tmp_path / "requests.db"
    store, times = build_search_log(path, rows=200)
    yield store, times, path
    store.close()


def _reads(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch, delay: float = 0.0
) -> Reads:
    reads = Reads(store, delay)
    # ``_connect`` registers the instance's attribute on every new connection.
    monkeypatch.setattr(store, "_bodies_match", reads)
    return reads


async def _never() -> bool:
    return False


def _ask[T](
    store: RequestLogStore,
    filters: dict[str, Any],
    compute: Callable[[MatchedRows], T],
    *,
    settled: Callable[[T, float], bool] | None = None,
    disconnected: Callable[[], Awaitable[bool]] = _never,
) -> Awaitable[T]:
    return search_jobs().answer(
        store,
        filters,
        compute,
        run=run_on_search_pool,
        disconnected=disconnected,
        settled=settled,
    )


def _filters(q: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": None,
        "model": None,
        "status": None,
        "endpoint": None,
        "key": None,
        "since": None,
        "until": None,
        "q": q,
        "local": "hide",
        "harness": None,
        "session": None,
        "folder": None,
        "exit": None,
    }
    base.update(extra)
    return base


def _count(
    store: RequestLogStore, filters: dict[str, Any]
) -> Callable[[MatchedRows], int]:
    return lambda matched: store.count_requests(matched=matched, **filters)


def _fresh(store: RequestLogStore) -> None:
    with store._stats_lock:
        store._stats_cache.clear()


def _page_load(
    store: RequestLogStore, filters: dict[str, Any]
) -> dict[str, Callable[[MatchedRows | None], Any]]:
    """Every answer one Requests view asks for with a search, as callables."""

    return {
        "list": lambda m: store.list_requests_page(
            limit=25,
            offset=0,
            include_total=False,
            include_exits=True,
            matched=m,
            **filters,
        ),
        "count": lambda m: store.count_requests(matched=m, **filters),
        "stats": lambda m: store.stats(matched=m, **filters),
        "cancelled": lambda m: store.cancelled_breakdown(matched=m, **filters),
        "cost": lambda m: store.cost_breakdown(matched=m, **filters),
        "ttft": lambda m: store.ttft_percentiles(matched=m, **filters),
        "no_answer": lambda m: store.no_answer_breakdown(matched=m, **filters),
        "origin": lambda m: store.origin_breakdown(matched=m, **filters),
        "pulse": lambda m: store.pulse(matched=m, **filters),
    }


def _write(path: Path, rows: list[tuple[int, float, str]]) -> None:
    rng = random.Random(99)
    writer = RequestLogStore(path, max_rows=0)
    for index, ts, prompt in rows:
        record = make_record(rng, index, ts)
        record.input_text = prompt
        record.output_text = ""
        record.thinking_text = None
        record.tool_calls = None
        writer.enqueue(record)
    writer.close()


@pytest.mark.asyncio
async def test_one_page_load_reads_every_row_of_the_window_once(
    log, monkeypatch
) -> None:
    store, _times, _path = log
    filters = _filters("x")
    expected: dict[str, Any] = {}
    for name, compute in _page_load(store, filters).items():
        _fresh(store)
        expected[name] = compute(None)
    reads = _reads(store, monkeypatch)
    store.count_requests(**filters)
    one_pass = reads.calls
    assert one_pass > 100

    reads.calls = 0
    reads.threads.clear()
    _fresh(store)
    answers = _page_load(store, filters)
    results = await asyncio.gather(
        *(_ask(store, filters, compute) for compute in answers.values())
    )

    assert dict(zip(answers, results, strict=True)) == expected
    # Nine answers, one pass: every row of the window read once, on the
    # search pool and nowhere else.
    assert reads.calls == one_pass
    assert reads.threads and all(
        name.startswith("mcc-search") for name in reads.threads
    )


@pytest.mark.asyncio
async def test_windows_reuse_a_pass_and_a_wider_one_reads_only_older_rows(
    log, monkeypatch
) -> None:
    store, times, _path = log
    middle = sorted(times)[len(times) // 2]
    recent = _filters("x", since=middle)
    later = _filters("x", since=middle + 600)
    whole = _filters("x")
    expected = [store.count_requests(**f) for f in (recent, later, whole)]
    reads = _reads(store, monkeypatch)
    store.count_requests(**whole)
    full_pass = reads.calls

    reads.calls = 0
    got = [await _ask(store, recent, _count(store, recent))]
    first = reads.calls
    reads.calls = 0
    got.append(await _ask(store, later, _count(store, later)))
    narrower = reads.calls
    got.append(await _ask(store, whole, _count(store, whole)))
    wider = reads.calls

    assert got == expected
    # The narrower window read nothing of its own; the wider one only the
    # rows older than where the first pass stopped. Together: one pass.
    assert narrower == 0
    assert 0 < first < full_pass
    assert first + wider == full_pass


@pytest.mark.asyncio
async def test_rows_written_after_a_pass_are_the_only_rows_read_again(
    log, monkeypatch
) -> None:
    store, times, path = log
    filters = _filters("too long")
    reads = _reads(store, monkeypatch)
    first = await _ask(store, filters, lambda m: store.pulse(matched=m, **filters))
    assert reads.calls > 100

    _write(
        path,
        [
            (9000 + i, max(times) + 10 + i, f"newest prompt too long {i}")
            for i in range(5)
        ],
    )
    reads.calls = 0
    after = await _ask(store, filters, lambda m: store.pulse(matched=m, **filters))
    # The pulse with a search costs the new rows, not another pass.
    assert reads.calls == 5
    monkeypatch.undo()
    assert after == store.pulse(**filters)
    assert after["total"] == first["total"] + 5


@pytest.mark.asyncio
async def test_the_page_is_answered_while_the_pass_is_still_reading(
    log, monkeypatch
) -> None:
    store, _times, _path = log
    filters = _filters("x", local=None)
    expected = store.list_requests_page(
        limit=5, offset=0, include_total=False, **filters
    )
    _reads(store, monkeypatch, delay=0.01)

    def page(m: MatchedRows) -> tuple[list[dict[str, Any]], int | None, bool]:
        return store.list_requests_page(
            limit=5, offset=0, include_total=False, matched=m, **filters
        )

    def final(
        answer: tuple[list[dict[str, Any]], int | None, bool], read_to: float
    ) -> bool:
        rows, _total, has_more = answer
        return len(rows) == 5 and has_more and rows[-1]["ts_epoch"] > read_to

    started = time.perf_counter()
    got = await _ask(store, filters, page, settled=final)
    took = time.perf_counter() - started
    job = search_jobs().job(store, filters, run_on_search_pool)

    assert got == expected
    assert job.progress()["finished"] is False
    # 200 rows at 10 ms each is two seconds of pass; the page came first.
    assert took < 1.0


@pytest.mark.asyncio
async def test_a_newer_search_stops_the_running_pass_and_both_are_answered(
    log, monkeypatch
) -> None:
    store, _times, _path = log
    first = _filters("x", local=None)
    second = _filters("too long", local=None)
    want = [store.count_requests(**first), store.count_requests(**second)]
    _reads(store, monkeypatch, delay=0.005)
    finished: list[str] = []

    async def count(name: str, filters: dict[str, Any]) -> int:
        result = await _ask(store, filters, _count(store, filters))
        finished.append(name)
        return result

    older = asyncio.ensure_future(count("first", first))
    await asyncio.sleep(0.3)
    job = search_jobs().job(store, first, run_on_search_pool)
    running = job.segment
    assert running is not None
    asked = time.perf_counter()
    newer = asyncio.ensure_future(count("second", second))
    await asyncio.wait({running}, timeout=2.0)
    stopped_in = time.perf_counter() - asked
    results = await asyncio.gather(older, newer)

    assert results == want
    assert finished == ["second", "first"]
    # One row's read (5 ms here) plus the interrupt: well inside 50 ms.
    assert stopped_in < 0.05 + 0.005 + 0.05


@pytest.mark.asyncio
async def test_a_search_nobody_waits_for_stops_and_frees_its_worker(
    log, monkeypatch
) -> None:
    store, _times, _path = log
    monkeypatch.setattr(search_jobs_module, "SEARCH_GRACE_SECONDS", 0.3)
    _reads(store, monkeypatch, delay=0.02)
    filters = _filters("x", local=None)
    gone = {"now": False}

    async def disconnected() -> bool:
        return gone["now"]

    waiter = asyncio.ensure_future(
        _ask(store, filters, _count(store, filters), disconnected=disconnected)
    )
    await asyncio.sleep(0.3)
    job = search_jobs().job(store, filters, run_on_search_pool)
    segment = job.segment
    assert segment is not None
    gone["now"] = True
    with pytest.raises(SearchAbandoned):
        await waiter
    left = time.perf_counter()
    await asyncio.wait({segment}, timeout=5.0)
    stopped_after = time.perf_counter() - left

    # The grace, then one row's read (20 ms here) and the interrupt.
    assert segment.done()
    assert stopped_after < 0.3 + 0.05 + 0.02 + 0.05
    assert job.progress()["finished"] is False
    # Both workers are free at once.
    started = time.perf_counter()
    assert await asyncio.gather(
        run_on_search_pool(lambda: 1), run_on_search_pool(lambda: 2)
    ) == [1, 2]
    assert time.perf_counter() - started < 0.2


@pytest.mark.asyncio
async def test_a_stop_stops_every_search_and_tells_its_waiters(
    log, monkeypatch
) -> None:
    store, _times, _path = log
    reads = _reads(store, monkeypatch, delay=0.02)
    filters = _filters("x", local=None)
    waiter = asyncio.ensure_future(_ask(store, filters, _count(store, filters)))
    await asyncio.sleep(0.3)
    assert search_jobs().running()

    started = time.perf_counter()
    await close_search_pool()
    took = time.perf_counter() - started
    with pytest.raises(SearchStopped):
        await waiter

    assert took < search_pool.SEARCH_DRAIN_SECONDS
    assert not search_jobs().running()
    # The next generation gets a fresh pool and answers.
    reads.delay = 0.0
    again = await _ask(store, filters, _count(store, filters))
    monkeypatch.undo()
    assert again == store.count_requests(**filters)
    await close_search_pool()


@pytest.mark.asyncio
async def test_rows_matched_before_a_clear_are_never_served_after_it(log) -> None:
    store, times, path = log
    filters = _filters("too long", local=None)
    before = await _ask(store, filters, _count(store, filters))
    assert before > 0
    store.clear()
    search_jobs().forget(store)
    _write(
        path,
        [
            (i, max(times) + i, "prompt is too long" if i % 3 == 0 else "nothing here")
            for i in range(30)
        ],
    )

    after = await _ask(store, filters, _count(store, filters))
    assert after == store.count_requests(**filters) == 10


@pytest.mark.asyncio
async def test_a_backfill_that_rewrites_a_filtered_column_is_read_again(log) -> None:
    store, _times, path = log
    filters = _filters("x", local="hide")
    first = await _ask(store, filters, _count(store, filters))
    assert first == store.count_requests(**filters)
    # What the is_local backfill does: rewrite the column, move its marker.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE requests SET is_local = 1 WHERE rowid % 4 = 0")
    conn.execute(
        "INSERT OR REPLACE INTO request_log_meta (key, value)"
        " VALUES ('is_local_backfilled_at', '1.0')"
    )
    conn.commit()
    conn.close()

    again = await _ask(store, filters, _count(store, filters))
    assert again == store.count_requests(**filters)
    assert again < first


# ------------------------------------------------------------ 7.91.2: progress


def _plain(filters: dict[str, Any]) -> dict[str, Any]:
    """The filters without the search: what the pass reads its rows by."""

    return {name: value for name, value in filters.items() if name != "q"}


async def _progress(
    store: RequestLogStore, filters: dict[str, Any]
) -> dict[str, Any] | None:
    return await search_jobs().progress(store, filters, run=asyncio.to_thread)


@pytest.mark.asyncio
async def test_progress_counts_every_row_read_before_the_first_match(
    log, monkeypatch
) -> None:
    """A term found nowhere is the pass that needs a progress line most.

    Its count and cards wait for the whole window to be read, and no row ever
    matches: the time read back to must come from the rows read, not from the
    last match, or the line would say nothing until the end.
    """

    store, times, _path = log
    filters = _filters("zqxjvkw", local=None)
    total = store.count_requests(**_plain(filters))
    _reads(store, monkeypatch, delay=0.005)

    assert await _progress(store, filters) is None
    assert search_jobs().find(store, filters) is None
    waiter = asyncio.ensure_future(_ask(store, filters, _count(store, filters)))
    samples: list[dict[str, Any]] = []
    waiters_seen: set[int] = set()
    while not waiter.done():
        await asyncio.sleep(0.1)
        seen = await _progress(store, filters)
        job = search_jobs().find(store, filters)
        if seen is not None and job is not None:
            samples.append(seen)
            if not seen["finished"]:
                waiters_seen.add(job.waiters)
    assert await waiter == 0
    final = await _progress(store, filters)

    running = [s for s in samples if not s["finished"] and s["read"] > 0]
    assert len(running) >= 3, samples
    assert all(s["matched"] == 0 for s in running)
    assert all(0 < s["read"] < total for s in running)
    assert all(s["total"] == total for s in samples)
    reads = [s["read"] for s in samples]
    assert reads == sorted(reads)
    back = [s["searched_back_to"] for s in running]
    assert all(ts is not None and ts <= max(times) for ts in back)
    assert back == sorted(back, reverse=True)
    assert final is not None
    assert final["finished"] is True
    assert final["read"] == final["total"] == total
    assert final["matched"] == 0
    # Asking is never waiting: the count was the only waiter throughout.
    assert waiters_seen == {1}


@pytest.mark.asyncio
async def test_progress_never_goes_back_when_a_pass_is_stopped_and_continued(
    log, monkeypatch
) -> None:
    store, _times, _path = log
    first = _filters("x", local=None)
    second = _filters("too long", local=None)
    want = store.count_requests(**first)
    total = store.count_requests(**_plain(first))
    _reads(store, monkeypatch, delay=0.005)
    older = asyncio.ensure_future(_ask(store, first, _count(store, first)))
    samples: list[dict[str, Any]] = []

    async def sample() -> None:
        while not older.done():
            seen = await _progress(store, first)
            if seen is not None:
                samples.append(seen)
            await asyncio.sleep(0.03)

    sampler = asyncio.ensure_future(sample())
    await asyncio.sleep(0.3)
    stopped_at = await _progress(store, first)
    # The newer question stops this pass; it continues once that is answered.
    await _ask(store, second, _count(store, second))
    assert await older == want
    await sampler
    final = await _progress(store, first)

    assert stopped_at is not None
    assert 0 < stopped_at["read"] < total
    reads = [s["read"] for s in samples]
    assert reads == sorted(reads)
    back = [s["searched_back_to"] for s in samples if s["searched_back_to"] is not None]
    assert back == sorted(back, reverse=True)
    assert final is not None
    assert final["finished"] is True
    assert final["read"] == final["total"] == total


@pytest.mark.asyncio
async def test_asking_for_progress_starts_and_holds_nothing(log, monkeypatch) -> None:
    store, _times, _path = log
    monkeypatch.setattr(search_jobs_module, "SEARCH_GRACE_SECONDS", 0.3)
    reads = _reads(store, monkeypatch, delay=0.01)
    filters = _filters("x", local=None)
    jobs_before = len(search_jobs()._jobs)

    for _ in range(5):
        assert await _progress(store, filters) is None
    assert len(search_jobs()._jobs) == jobs_before
    assert reads.calls == 0

    gone = {"now": False}

    async def disconnected() -> bool:
        return gone["now"]

    waiter = asyncio.ensure_future(
        _ask(store, filters, _count(store, filters), disconnected=disconnected)
    )
    await asyncio.sleep(0.3)
    job = search_jobs().find(store, filters)
    assert job is not None and job.segment is not None
    gone["now"] = True
    with pytest.raises(SearchAbandoned):
        await waiter
    segment = job.segment
    # Polled all through the grace and after it: the pass still stops.
    for _ in range(12):
        await _progress(store, filters)
        await asyncio.sleep(0.05)
    if segment is not None:
        await asyncio.wait({segment}, timeout=5.0)
    assert job.segment is None
    assert job.waiters == 0
    assert job.progress()["finished"] is False


def _gone_after(flags: list[bool], index: int) -> Callable[[], Awaitable[bool]]:
    async def disconnected() -> bool:
        return flags[index]

    return disconnected


@pytest.mark.asyncio
async def test_typing_three_prefixes_leaves_only_the_last_search_reading(
    log, monkeypatch
) -> None:
    """The page's own sequence (7.91.2): a load per prefix, 400 ms apart.

    Each load asks for the count, the stats, TTFT and origin of its prefix.
    Starting the next load aborts the previous one's requests (the page's
    ``AbortController``); the server sees the client leave, the superseded
    searches lose every waiter, their passes stop, and only the last prefix
    is read to the end.
    """

    store, _times, _path = log
    monkeypatch.setattr(search_jobs_module, "SEARCH_GRACE_SECONDS", 0.3)
    prefixes = ("too", "too l", "too long")
    wanted = ("count", "stats", "ttft", "origin")
    expected: dict[str, Any] = {}
    last = _filters(prefixes[-1], local=None)
    for name, compute in _page_load(store, last).items():
        if name in wanted:
            _fresh(store)
            expected[name] = compute(None)
    # 20 ms a row: a pass is seconds long, so 400 ms reads a small part of it.
    reads = _reads(store, monkeypatch, delay=0.02)
    reads.delay = 0.0
    store.count_requests(**last)
    full_pass = reads.calls
    reads.delay = 0.02
    reads.calls = 0
    _fresh(store)

    gone = [False] * len(prefixes)
    loads: list[tuple[dict[str, Any], list[asyncio.Task[Any]]]] = []
    for index, q in enumerate(prefixes):
        if index:
            # The new load aborts the previous one's requests.
            gone[index - 1] = True
        filters = _filters(q, local=None)
        answers = _page_load(store, filters)
        tasks = [
            asyncio.ensure_future(
                _ask(
                    store,
                    filters,
                    answers[name],
                    disconnected=_gone_after(gone, index),
                )
            )
            for name in wanted
        ]
        loads.append((filters, tasks))
        if index < len(prefixes) - 1:
            await asyncio.sleep(0.4)

    results = [
        await asyncio.gather(*tasks, return_exceptions=True) for _f, tasks in loads
    ]
    # Past every grace period, so a superseded pass had its chance to stop.
    await asyncio.sleep(0.3 + 0.5 + 0.2)
    jobs = [search_jobs().find(store, filters) for filters, _tasks in loads]

    assert dict(zip(wanted, results[-1], strict=True)) == expected
    for superseded in results[:-1]:
        assert all(isinstance(result, SearchAbandoned) for result in superseded)
    assert all(job is not None for job in jobs)
    superseded_jobs = [job for job in jobs[:-1] if job is not None]
    assert [job.waiters for job in superseded_jobs] == [0, 0]
    assert all(job.segment is None for job in superseded_jobs)
    assert not any(job.progress()["finished"] for job in superseded_jobs)
    assert jobs[-1] is not None and jobs[-1].progress()["finished"] is True
    assert not search_jobs().running()
    # One full pass for the search typed last, and only the start of the two
    # it replaced.
    assert full_pass > 100
    assert reads.calls < full_pass + full_pass // 2
