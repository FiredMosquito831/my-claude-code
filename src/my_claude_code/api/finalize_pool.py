"""The worker threads a finished request's bookkeeping runs on.

A streamed answer's stream closes only after its finalize -- thumbnails, the
token estimate, the price (``RequestCapture._compute_finalize_fields``), or a
media row's file writes and thumbnails -- comes back from a worker thread.
Until 7.90.1 that thread came from the event loop's default executor
(``asyncio.to_thread``), the same pool every dashboard read uses. A free-text
search over all time runs one body scan per query on that pool, minutes each,
and a scan cannot be cancelled. Once the scans filled it (20 workers on 16
CPUs), every completion's last step queued behind one, and the client sat on a
finished answer for minutes: 270 s and 282 s in the 2026-10-09 repro.

The finalize now has its own small pool, so no admin read can hold a finished
answer open. One finalize is milliseconds of CPU, so four workers keep up with
far more concurrent requests than a desktop proxy sees.

The pool is created on first use and replaced after a shutdown, so a RELOAD's
next generation (and the next test) gets a fresh one. The shutdown waits for
running jobs with its own bound, never on the default executor: a stop that
queued behind a scan would be the same hang one level up.
"""

import asyncio
import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from loguru import logger

from my_claude_code.core.stop_deadline import stop_deadline

#: Worker threads for request finalize work.
FINALIZE_POOL_WORKERS = 4

#: Longest a shutdown waits for finalize jobs still running, so their rows
#: reach the request log before it is flushed. Capped further by the time the
#: shared stop deadline has left.
FINALIZE_DRAIN_SECONDS = 2.0

_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_jobs: set[asyncio.Future[Any]] = set()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=FINALIZE_POOL_WORKERS,
                thread_name_prefix="mcc-finalize",
            )
        return _pool


def run_on_finalize_pool[T](func: Callable[..., T], *args: Any) -> asyncio.Future[T]:
    """Run ``func(*args)`` on the finalize pool, in a copy of this context.

    The context copy is what ``asyncio.to_thread`` did: per-request state held
    in context variables stays visible inside the worker.
    """
    loop = asyncio.get_running_loop()
    job = loop.run_in_executor(_executor(), contextvars.copy_context().run, func, *args)
    _jobs.add(job)
    job.add_done_callback(_jobs.discard)
    return job


def _drain_budget() -> float:
    deadline = stop_deadline()
    if not deadline.requested:
        return FINALIZE_DRAIN_SECONDS
    return min(FINALIZE_DRAIN_SECONDS, deadline.teardown_remaining())


async def close_finalize_pool() -> None:
    """Retire the pool: wait, bounded, for running jobs, then shut it down.

    The pool is detached first, so a finalize that still arrives gets a fresh
    pool rather than an error. Jobs still running after the bound are left to
    finish on their own; queued ones are cancelled, and a request whose task
    was cancelled still commits its row from the cancelled offload.
    """
    global _pool
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
                "{} request finalize job(s) still running at shutdown; "
                "not waiting for them.",
                len(pending),
            )
    pool.shutdown(wait=False, cancel_futures=True)
