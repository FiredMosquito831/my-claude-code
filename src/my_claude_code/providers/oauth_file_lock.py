"""Cross-process refresh locks, spoken the way ``proper-lockfile`` speaks them.

A refresh token is single-use: whoever presents it first gets a new one and
everybody else who presents the same value afterwards is refused. An in-process
``asyncio.Lock`` makes one MCC server single-flight, but the user routinely
runs several MCC servers beside the real Claude Code and Codex clients, and
none of those share a Python lock. The only thing they share is the disk.

``proper-lockfile`` -- the library Claude Code uses for all three of its
credential locks -- implements a lock on ``X`` as a **directory** at
``X.lock``: ``mkdir`` is atomic on every filesystem the clients run on, the
directory's mtime is the liveness heartbeat (touched every ``update``
milliseconds while held), and a lock whose mtime is older than ``stale`` is
presumed abandoned and removed. Joining that protocol means speaking exactly
that, nothing more:

* acquire = ``mkdir``; a ``FileExistsError`` is somebody else's lock;
* hold = touch the directory's mtime every heartbeat;
* break = only when the mtime is more than ``stale`` seconds old;
* release = ``rmdir``, always, including when the body raised.

MCC never writes Claude Code's ``.oauth_refresh.lock.owner`` pid record. That
record is how Claude Code decides it may take over a lock whose holder stopped
heartbeating; MCC is not a Claude Code process and must never be mistaken for
one, so it never claims a lock that way and never breaks a live one.

The waiting budget is Claude Code's own (2.1.283, ``Sa``): a handful of
retries one to two seconds apart, then a short liveness window. A lock still
held after that is **busy** -- the caller fails the attempt into the existing
chain rather than waiting on a network round trip it cannot see.

Both an async and a sync acquire are provided. The async one is what the
Anthropic provider uses on the event loop (``asyncio.sleep`` only, never
``time.sleep``). The sync one exists for the ChatGPT provider, whose refresh
has always been a synchronous ``httpx.post``; it waits the same budget.
"""

import asyncio
import contextlib
import os
import random
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

#: ``proper-lockfile``'s default ``lockfilePath`` is ``${file}.lock``.
LOCK_SUFFIX = ".lock"

#: The pid record Claude Code writes beside its refresh lock. Named here only
#: so the tests can prove MCC never creates it.
CLAUDE_OWNER_RECORD_SUFFIX = ".owner"


class LockBusy(Exception):
    """The lock is held by a live owner and the waiting budget ran out.

    Not a failure of the credential: the only correct response is to fail
    *this* attempt as transient and let the existing chain decide what next.
    """


@dataclass(frozen=True, slots=True)
class LockTiming:
    """How long to wait for, and how to keep, one lock."""

    #: A lock whose mtime is older than this is abandoned and may be removed.
    stale_seconds: float
    #: How often a held lock's mtime is touched. ``0`` disables the heartbeat.
    heartbeat_seconds: float
    #: How many times to retry a busy lock before the liveness window.
    retries: int
    #: The delay between retries is drawn from ``[retry_min, retry_max]``
    #: when ``backoff`` is off (Claude Code's refresh loop), or starts at
    #: ``retry_min`` and doubles up to ``retry_max`` when it is on
    #: (``proper-lockfile``'s own ``retries`` option, the ``.storage-write``
    #: lock).
    retry_min_seconds: float
    retry_max_seconds: float
    backoff: bool = False
    #: After the retries, how long to keep watching for a release.
    liveness_seconds: float = 0.0
    liveness_poll_seconds: float = 0.5

    def retry_delay(self, attempt: int) -> float:
        """The wait before retry number ``attempt`` (1-based)."""

        if self.backoff:
            return min(
                self.retry_min_seconds * (2 ** (attempt - 1)), self.retry_max_seconds
            )
        return random.uniform(self.retry_min_seconds, self.retry_max_seconds)


#: Claude Code 2.1.283's refresh lock (``Iun``): ``stale: 60000``,
#: ``update: 5000``; its ELOCKED loop retries 5 times 1-2 s apart and then
#: watches a silent holder for 7.5 s. MCC's own NATIVE store locks reuse the
#: same numbers so there is one rule to learn.
REFRESH_LOCK_TIMING = LockTiming(
    stale_seconds=60.0,
    heartbeat_seconds=5.0,
    retries=5,
    retry_min_seconds=1.0,
    retry_max_seconds=2.0,
    backoff=False,
    liveness_seconds=7.5,
)

#: Claude Code's ``.storage-write`` lock (2.1.278 offset 197903482):
#: ``retries: {retries: 10, minTimeout: 100, maxTimeout: 1000}``,
#: ``stale: 15000``. Held only for the duration of one file write, so no
#: heartbeat is needed inside a 15 s stale window.
STORAGE_WRITE_LOCK_TIMING = LockTiming(
    stale_seconds=15.0,
    heartbeat_seconds=0.0,
    retries=10,
    retry_min_seconds=0.1,
    retry_max_seconds=1.0,
    backoff=True,
    liveness_seconds=0.0,
)


def lock_dir_for(target: Path) -> Path:
    """The directory ``proper-lockfile`` creates to lock ``target``."""

    return target.with_name(target.name + LOCK_SUFFIX)


@dataclass(slots=True)
class HeldLock:
    """A lock this process holds."""

    path: Path
    #: Whether acquiring it meant waiting behind a live owner first. A caller
    #: that waited must re-read what the lock protects before acting: the
    #: owner it waited for may already have done the work.
    waited: bool = False
    #: Set when the heartbeat found the directory gone or touched by somebody
    #: else -- ``proper-lockfile``'s ``onCompromised``. A caller must not write
    #: under a compromised lock.
    compromised: bool = False
    _last_touch: float = field(default=0.0, repr=False)

    def touch(self) -> None:
        """One heartbeat: prove the directory is still ours, then touch it."""

        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.compromised = True
            return
        # Somebody else touched or recreated it. Windows stores mtimes at
        # 100 ns but some filesystems round to seconds, so allow slack.
        if self._last_touch and abs(mtime - self._last_touch) > 2.0:
            self.compromised = True
            return
        now = time.time()
        try:
            os.utime(self.path, (now, now))
        except OSError:
            self.compromised = True
            return
        self._last_touch = now

    def release(self) -> None:
        """Remove the lock directory. Safe to call twice."""

        with contextlib.suppress(OSError):
            self.path.rmdir()


def try_acquire(lock_path: Path, timing: LockTiming) -> HeldLock | None:
    """One non-blocking attempt: the held lock, or ``None`` when it is busy."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
        return _stamp_new(HeldLock(lock_path))
    return None


def _try_mkdir(lock_path: Path, *, stale_seconds: float) -> bool:
    """One acquisition attempt. Breaks the lock only when it is stale."""

    try:
        lock_path.mkdir()
        return True
    except FileExistsError:
        pass
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        # Released between our mkdir and our stat: try once more.
        try:
            lock_path.mkdir()
            return True
        except FileExistsError:
            return False
    if age <= stale_seconds:
        return False
    try:
        lock_path.rmdir()
    except OSError:
        # Lost the race to break it, or it is not empty (not a lock we know).
        return False
    logger.info(
        "Removed an abandoned lock at {} ({:.0f}s without a heartbeat).",
        lock_path,
        age,
    )
    try:
        lock_path.mkdir()
        return True
    except FileExistsError:
        return False


def _delays(timing: LockTiming) -> Iterator[float]:
    for attempt in range(1, timing.retries + 1):
        yield timing.retry_delay(attempt)


def _release(held: HeldLock) -> None:
    held.release()


def _stamp_new(held: HeldLock) -> HeldLock:
    with contextlib.suppress(OSError):
        held._last_touch = held.path.stat().st_mtime
    return held


async def acquire(
    lock_path: Path,
    timing: LockTiming,
    *,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> HeldLock:
    """Take ``lock_path`` or raise :class:`LockBusy`. Never ``time.sleep``."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
        return _stamp_new(HeldLock(lock_path))
    for delay in _delays(timing):
        await _await(sleep(delay))
        if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
            return _stamp_new(HeldLock(lock_path, waited=True))
    deadline = time.monotonic() + timing.liveness_seconds
    while time.monotonic() < deadline:
        await _await(sleep(timing.liveness_poll_seconds))
        if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
            return _stamp_new(HeldLock(lock_path, waited=True))
    raise LockBusy(f"{lock_path} is held by a live owner")


def acquire_sync(
    lock_path: Path,
    timing: LockTiming,
    *,
    sleep: Callable[[float], object] = time.sleep,
) -> HeldLock:
    """The synchronous twin of :func:`acquire`, for the ChatGPT provider."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
        return _stamp_new(HeldLock(lock_path))
    for delay in _delays(timing):
        sleep(delay)
        if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
            return _stamp_new(HeldLock(lock_path, waited=True))
    deadline = time.monotonic() + timing.liveness_seconds
    while time.monotonic() < deadline:
        sleep(timing.liveness_poll_seconds)
        if _try_mkdir(lock_path, stale_seconds=timing.stale_seconds):
            return _stamp_new(HeldLock(lock_path, waited=True))
    raise LockBusy(f"{lock_path} is held by a live owner")


async def _await(value: object) -> None:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        await value


async def _heartbeat(held: HeldLock, seconds: float) -> None:
    while True:
        await asyncio.sleep(seconds)
        held.touch()
        if held.compromised:
            logger.warning("Lock {} was compromised while held.", held.path)
            return


@contextlib.asynccontextmanager
async def hold(
    lock_path: Path,
    timing: LockTiming,
    *,
    sleep: Callable[[float], object] = asyncio.sleep,
) -> AsyncIterator[HeldLock]:
    """Hold ``lock_path`` for the body, heartbeating, and always release it."""

    held = await acquire(lock_path, timing, sleep=sleep)
    task: asyncio.Task[None] | None = None
    if timing.heartbeat_seconds > 0:
        task = asyncio.get_running_loop().create_task(
            _heartbeat(held, timing.heartbeat_seconds)
        )
    try:
        yield held
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _release(held)


@contextlib.contextmanager
def hold_sync(
    lock_path: Path,
    timing: LockTiming,
    *,
    sleep: Callable[[float], object] = time.sleep,
) -> Iterator[HeldLock]:
    """Hold ``lock_path`` for the body from synchronous code."""

    held = acquire_sync(lock_path, timing, sleep=sleep)
    stop = threading.Event()
    thread: threading.Thread | None = None
    if timing.heartbeat_seconds > 0:

        def _beat() -> None:
            while not stop.wait(timing.heartbeat_seconds):
                held.touch()
                if held.compromised:
                    return

        thread = threading.Thread(target=_beat, name="oauth-lock-heartbeat")
        thread.daemon = True
        thread.start()
    try:
        yield held
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=1.0)
        _release(held)
