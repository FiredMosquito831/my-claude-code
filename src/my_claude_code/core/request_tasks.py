"""Which ``asyncio`` task is serving which request, and whether it is moving.

The smallest possible seam between a request and the task holding its awaits.
It exists for one reason: the stuck-request watchdog cannot dump the stack of
"the task serving ``req_ab12``" unless something wrote down which task that is,
and nothing did.

**Deliberately not the in-flight registry.** The live "In flight" panel
(PR-F1) needs a much wider entry -- session, folder, tier, phase history, the
whole Analytics row before it exists. That is a different release. What is here
is the subset that a watchdog needs and that F1 can grow into: the same
register/unregister choke points, the same weakref reaping rule, the same
``threading.Lock``. F1 adds fields to :class:`RequestTaskEntry` and readers to
:func:`snapshot`; it does not have to move the seam.

**Why more than one task per request.** A streaming answer is held by two
different tasks over its life. The handler's own task awaits the *first* chunk
(``api/response_streams.py::_first_chunk_streaming_response`` calls ``anext``
before it returns a response at all), so the "no upstream byte yet" park lives
there. Everything after that is iterated by the child task Starlette's
``StreamingResponse`` starts inside its ``anyio`` task group, so the
"streaming stopped" park lives *there*, and the handler task is parked on the
task group's ``__aexit__`` where it says nothing. An entry therefore collects
tasks as they announce themselves, newest last, and the watchdog dumps all of
them.

**Why entries cannot leak.** Three independent guarantees, because one is a
promise and three are a design:

1. :func:`unregister` runs as the *first* statement of
   ``RequestCapture._begin_finalize``, above its ``self._store is None`` early
   return -- the single choke point every terminal path goes through (success,
   error, cancellation, ``GeneratorExit``, the optimizer's local answer, all
   four inbound surfaces).
2. Tasks are held by :class:`weakref.ref`. A request whose tasks are all
   collected or ``done()`` is reaped by the next reader. That covers the one
   path ``_begin_finalize`` cannot: a streaming request with the request log
   switched off never enters ``_observe`` at all, so it never finalizes.
3. The registry is bounded by live concurrency, which is bounded by the
   clients. No "stale after N seconds" rule exists here, and none may be added:
   the 09-16 requests were alive for 47 minutes and any such rule would have
   deleted the only evidence of them.

Nothing in this module ever stores a prompt, a response, a header or a key. The
credential and proxy labels it reads are the already-masked ones those slots
carry, exactly as ``core/credential_attribution.py`` states for itself.
"""

import asyncio
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

#: Phase names. Exactly three, because a record that cannot say which of these
#: a stall is in is a record nobody can act on.
#:
#: * ``awaiting_first_upstream_byte`` -- an attempt is in flight and not one
#:   byte has reached the client yet. A slow model looks like this legitimately
#:   for a long time (the operator's own log has TTFT up to 190 s), which is
#:   why the default threshold is what it is.
#: * ``streaming_stopped`` -- bytes did reach the client and then stopped. This
#:   is the one that is nearly always real.
#: * ``between_attempts`` -- MCC is not reading an upstream stream at all:
#:   still routing, asleep on a backoff or a limiter, or switching rungs. The
#:   09-16 park was in this phase.
PHASE_AWAITING_FIRST_BYTE = "awaiting_first_upstream_byte"
PHASE_STREAMING_STOPPED = "streaming_stopped"
PHASE_BETWEEN_ATTEMPTS = "between_attempts"

#: How many distinct tasks one request may collect. Two is the shipped shape
#: (handler, then the streaming child); four leaves room for a surface that
#: hands the body to a third without turning a leak into an unbounded list.
MAX_TASKS_PER_REQUEST = 4


@dataclass(slots=True)
class RequestProgress:
    """Everything the watchdog reads off one request, in one cheap call.

    Produced by ``RequestCapture.watchdog_progress``. Every field is a counter,
    a label or a code-level identifier; none of them is content.
    """

    phase: str
    attempt_index: int | None = None
    provider: str | None = None
    model_ref: str | None = None
    key_label: str | None = None
    proxy_label: str | None = None
    ttft_ms: float | None = None
    output_chars: int = 0
    thinking_chars: int = 0
    tries: int = 0
    waited_seconds: float = 0.0

    def signature(self) -> tuple[object, ...]:
        """The tuple whose *change* between two polls means "progress".

        Every member is something that only moves when work happened:

        * ``attempt_index`` -- the chain advanced to another model;
        * ``tries`` -- the ladder recorded another upstream try;
        * ``output_chars`` / ``thinking_chars`` -- characters were forwarded to
          the client;
        * ``ttft_ms is not None`` -- the first byte arrived;
        * ``waited_seconds`` -- MCC credited itself another sleep, i.e. a
          limiter or a backoff is running and the request is *not* wedged;
        * ``key_label`` / ``proxy_label`` -- the pools moved to another
          credential or another rung.

        Chunk delivery is counted separately, on the entry itself, because it
        has to be observed whether or not the request log is switched on.
        """

        return (
            self.attempt_index,
            self.tries,
            self.output_chars,
            self.thinking_chars,
            self.ttft_ms is not None,
            round(self.waited_seconds, 3),
            self.key_label,
            self.proxy_label,
        )


ProgressReader = Callable[[], RequestProgress]


@dataclass(slots=True)
class RequestTaskEntry:
    """One in-flight request, its tasks, and the watchdog's bookkeeping."""

    request_id: str
    started_at_mono: float
    started_at_wall: float
    endpoint: str
    protocol: str
    stream: bool
    harness: str | None = None
    requested_model: str | None = None
    #: Chunks handed to the client by ``_PrefetchedStream``. Counted there and
    #: not in ``RequestCapture`` because the capture's observer is skipped
    #: entirely when the request log is off, and "is it still streaming" must
    #: not depend on a logging setting.
    chunks: int = 0
    last_chunk_mono: float | None = None
    progress: ProgressReader | None = None
    _tasks: list[weakref.ref[asyncio.Task[Any]]] = field(default_factory=list)
    # --- watchdog state, owned by runtime/stall_watchdog.py ----------------
    last_signature: tuple[object, ...] | None = None
    last_progress_mono: float = 0.0
    next_threshold: float = 0.0
    reports: int = 0

    def note_task(self, task: asyncio.Task[Any]) -> None:
        for reference in self._tasks:
            if reference() is task:
                return
        if len(self._tasks) >= MAX_TASKS_PER_REQUEST:
            return
        self._tasks.append(weakref.ref(task))

    def tasks(self) -> list[asyncio.Task[Any]]:
        """The live tasks still serving this request, oldest first."""

        live: list[asyncio.Task[Any]] = []
        for reference in self._tasks:
            task = reference()
            if task is not None:
                live.append(task)
        return live

    def abandoned(self) -> bool:
        """True when every task that ever served this request is finished.

        A request that was registered outside any task (a unit test building a
        capture synchronously) has no tasks at all and is never reaped on this
        rule -- absence of evidence is not evidence of completion.
        """

        if not self._tasks:
            return False
        return all(task is None or task.done() for task in (r() for r in self._tasks))

    def read_progress(self) -> RequestProgress | None:
        reader = self.progress
        if reader is None:
            return None
        try:
            return reader()
        except Exception:
            # An observer that raises must never take the watchdog with it,
            # and must never take the request with it either.
            return None


_LOCK = threading.Lock()
_ENTRIES: dict[str, RequestTaskEntry] = {}
_CURRENT: ContextVar[RequestTaskEntry | None] = ContextVar(
    "mcc_request_task_entry", default=None
)
_ENABLED = True
_reaped_total = 0


def configure(*, enabled: bool) -> None:
    """Adopt ``REQUEST_WATCHDOG_ENABLED``. Called once, from the lifespan.

    ``core`` may not import ``config``, so the operator's answer is pushed in
    from ``runtime`` rather than read here. Turning it off empties the registry
    as well as stopping it filling: a process that switched the watchdog off
    should not go on holding weakrefs nobody will ever read.
    """

    global _ENABLED
    with _LOCK:
        _ENABLED = bool(enabled)
        if not _ENABLED:
            _ENTRIES.clear()


def enabled() -> bool:
    return _ENABLED


def register(
    *,
    request_id: str,
    endpoint: str,
    protocol: str,
    stream: bool,
    harness: str | None,
    requested_model: str | None,
    progress: ProgressReader | None,
) -> RequestTaskEntry | None:
    """Start tracking one request. O(1); the last statement of a capture."""

    if not _ENABLED:
        return None
    now = time.monotonic()
    entry = RequestTaskEntry(
        request_id=request_id,
        started_at_mono=now,
        started_at_wall=time.time(),
        endpoint=endpoint,
        protocol=protocol,
        stream=stream,
        harness=harness,
        requested_model=requested_model,
        progress=progress,
        last_progress_mono=now,
    )
    task = _current_task()
    if task is not None:
        entry.note_task(task)
    with _LOCK:
        _ENTRIES[request_id] = entry
    _CURRENT.set(entry)
    return entry


def unregister(request_id: str) -> None:
    """Stop tracking one request. Idempotent, and never raises."""

    with _LOCK:
        _ENTRIES.pop(request_id, None)


def note_stream_chunk() -> None:
    """One chunk reached the client. The cheapest honest progress signal.

    Called from ``_PrefetchedStream.__anext__``, which is on every streaming
    answer of every surface and, unlike ``RequestCapture._observe``, runs
    whether or not the request log is on. Two increments and a ``ContextVar``
    read; it also adopts the task doing the iterating, which on a streaming
    response is not the task that built the capture.
    """

    if not _ENABLED:
        return
    entry = _CURRENT.get()
    if entry is None:
        return
    entry.chunks += 1
    entry.last_chunk_mono = time.monotonic()
    task = _current_task()
    if task is not None:
        entry.note_task(task)


def note_serving_task() -> None:
    """Adopt the current task for the current request, without a chunk."""

    if not _ENABLED:
        return
    entry = _CURRENT.get()
    if entry is None:
        return
    task = _current_task()
    if task is not None:
        entry.note_task(task)


def current_entry() -> RequestTaskEntry | None:
    return _CURRENT.get()


def snapshot(*, reap: bool = True) -> list[RequestTaskEntry]:
    """Every tracked request, oldest first, with finished ones dropped."""

    global _reaped_total
    with _LOCK:
        entries = list(_ENTRIES.values())
        if reap:
            dead = [entry for entry in entries if entry.abandoned()]
            for entry in dead:
                _ENTRIES.pop(entry.request_id, None)
            if dead:
                _reaped_total += len(dead)
                entries = [entry for entry in entries if not entry.abandoned()]
    entries.sort(key=lambda entry: entry.started_at_mono)
    return entries


def count() -> int:
    with _LOCK:
        return len(_ENTRIES)


def reaped_total() -> int:
    return _reaped_total


def reset() -> None:
    """Empty the registry. For the test fixture, and for a restart."""

    global _ENABLED, _reaped_total
    with _LOCK:
        _ENTRIES.clear()
        _ENABLED = True
        _reaped_total = 0
    _CURRENT.set(None)


def iter_entries() -> Iterator[RequestTaskEntry]:
    yield from snapshot()


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


__all__ = [
    "MAX_TASKS_PER_REQUEST",
    "PHASE_AWAITING_FIRST_BYTE",
    "PHASE_BETWEEN_ATTEMPTS",
    "PHASE_STREAMING_STOPPED",
    "RequestProgress",
    "RequestTaskEntry",
    "configure",
    "count",
    "current_entry",
    "enabled",
    "iter_entries",
    "note_serving_task",
    "note_stream_chunk",
    "reaped_total",
    "register",
    "reset",
    "snapshot",
    "unregister",
]
