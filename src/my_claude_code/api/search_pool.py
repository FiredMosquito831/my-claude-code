"""The worker threads a free-text request-log search runs on (7.91.1).

A free-text search decompresses and parses every stored body in its window --
minutes over all time on a large log -- and until 7.91.1 every one of its
answers did that on the event loop's default executor, the pool every other
dashboard read and the request path's own small hops share. On 2026-10-09 a
saved all-time search held six of its workers for twenty-five minutes, and
nothing could stop them.

Every request-log read that carries a search now runs here, and nothing else
does: the search's one pass (``application.search_jobs``) and the answers
computed from the rows it found. Two workers, because the work is Python
under the GIL -- seven scanning threads used 1.3 cores between them, so more
threads add no speed, only event-loop lag. One worker runs the pass (one at a
time per log), and the other is always free to answer from rows already read,
so an answer never queues behind a new pass.

The pool is created on first use and replaced after a shutdown, so a RELOAD's
next generation (and the next test) gets a fresh one. Shutting it down first
stops every search where it is -- an interrupt, a millisecond -- so a stop never
waits on a scan, and never queues behind one on another pool.
"""

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger

from my_claude_code.application.search_jobs import search_jobs
from my_claude_code.core.stop_deadline import stop_deadline

#: Worker threads for free-text request-log searches.
SEARCH_POOL_WORKERS = 2

#: Longest a shutdown waits for search work it has just stopped to come back,
#: capped further by the time the shared stop deadline has left. A stopped
#: statement returns within milliseconds; this only bounds the unexpected.
SEARCH_DRAIN_SECONDS = 1.0

_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_jobs: set[asyncio.Future[Any]] = set()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=SEARCH_POOL_WORKERS,
                thread_name_prefix="mcc-search",
            )
        return _pool


def run_on_search_pool[T](func: Callable[..., T], *args: Any) -> asyncio.Future[T]:
    """Run ``func(*args)`` on the search pool, in a copy of this context."""

    loop = asyncio.get_running_loop()
    job = loop.run_in_executor(_executor(), contextvars.copy_context().run, func, *args)
    _jobs.add(job)
    job.add_done_callback(_jobs.discard)
    return job


def _drain_budget() -> float:
    deadline = stop_deadline()
    if not deadline.requested:
        return SEARCH_DRAIN_SECONDS
    return min(SEARCH_DRAIN_SECONDS, deadline.teardown_remaining())


async def close_search_pool() -> None:
    """Stop every search, wait (bounded) for the workers, retire the pool.

    The pool is detached first, so a search asked for after this gets a fresh
    pool rather than an error.
    """

    global _pool
    search_jobs().stop_all()
    with _lock:
        pool, _pool = _pool, None
    if pool is None:
        return
    loop = asyncio.get_running_loop()
    running = [job for job in _jobs if job.get_loop() is loop and not job.done()]
    if running:
        _done, pending = await asyncio.wait(running, timeout=_drain_budget())
        if pending:
            logger.warning(
                "{} request log search job(s) still running at shutdown; "
                "not waiting for them.",
                len(pending),
            )
    pool.shutdown(wait=False, cancel_futures=True)
