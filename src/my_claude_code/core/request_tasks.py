"""Which ``asyncio`` task is serving which request, and whether it is moving.

The smallest possible seam between a request and the task holding its awaits.
It exists for one reason: the stuck-request watchdog cannot dump the stack of
"the task serving ``req_ab12``" unless something wrote down which task that is,
and nothing did.

**Also the in-flight registry.** The live "In flight" view reads the same
entries through :func:`inflight_report`: the same register/unregister choke
points, the same weakref reaping rule, the same ``threading.Lock``. What the
watchdog needed was grown rather than replaced -- the fields a request knows
when it arrives (origin, counts) sit on :class:`RequestTaskEntry`, and the ones
that move (attempt, phase stamps, characters streamed) are read through the
same :class:`RequestProgress` reader the watchdog polls. The registry is on
when either reader is: ``REQUEST_WATCHDOG_ENABLED`` or
``REQUEST_INFLIGHT_ENABLED``.

**Phases, and why each has an exact stamp.** The in-flight view names one of
:data:`INFLIGHT_PHASES` and the monotonic moment it began, and every one of
those moments is a statement MCC itself executed -- the capture being built,
a describe hop finishing, the plan being set, an attempt being started, the
first byte reaching the client. Nothing is stamped from a poll. What a single
read *cannot* say -- whether a request sitting in ``attempt`` is waiting on a
model or asleep on a backoff -- is left to the reader, who gets ``waited_s``
and can compare two reads; MCC asleep moves that number and a silent model
does not.

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
carry, exactly as ``core/credential_attribution.py`` states for itself. The
origin it holds is what the request log itself stores -- the session id and
folder the client stated -- and only when the operator's two capture settings
allow it; lengths and counts stand in for everything else.
"""

import asyncio
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.core.request_origin import project_short, session_short

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

#: In-flight phase names, in the order a request moves through them.
#:
#: * ``received`` -- the capture exists and nothing has been routed yet.
#: * ``describe`` -- not routed yet, and at least one vision describe hop has
#:   already finished. A describe run starts the moment the request arrives, so
#:   its ``phase_since`` is the arrival; the phase can only be *seen* once the
#:   first hop reports, because hops report when they end.
#: * ``routing`` -- the plan is set and no attempt has started.
#: * ``attempt`` -- attempt N has started and not one byte has reached the
#:   client. Waiting on the model and asleep on a backoff both look like this;
#:   ``waited_s`` moving between two reads is what tells them apart.
#: * ``awaiting_content`` -- the first byte has reached the client and the
#:   model has not yet produced text, reasoning or a tool call. Most streams
#:   open with MCC's own ``message_start`` the moment the upstream accepts
#:   (measured: 17 ms after arrival on the scratch rig, against a model that
#:   then said nothing for minutes), so "a byte went out" is not "the model
#:   is talking", and the view must not say it is.
#: * ``streaming`` -- the model's own content has reached the client. Seeing
#:   content needs the request log's observer; with the log off it cannot be
#:   told apart from the opening frame, so ``streaming`` then starts at the
#:   first byte and the row says ``observed: false``.
PHASE_RECEIVED = "received"
PHASE_DESCRIBE = "describe"
PHASE_ROUTING = "routing"
PHASE_ATTEMPT = "attempt"
PHASE_AWAITING_CONTENT = "awaiting_content"
PHASE_STREAMING = "streaming"
INFLIGHT_PHASES = (
    PHASE_RECEIVED,
    PHASE_DESCRIBE,
    PHASE_ROUTING,
    PHASE_ATTEMPT,
    PHASE_AWAITING_CONTENT,
    PHASE_STREAMING,
)

#: How many rows :func:`inflight_report` describes when the caller names no
#: limit. Everything past it is counted in ``total``, never silently dropped.
DEFAULT_INFLIGHT_LIMIT = 200

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
    # --- read by the in-flight view only; never part of ``signature`` -------
    #: Whether the request log is observing this request. When it is not, the
    #: character counters above were never counted and are reported as NULL
    #: rather than as a confident zero.
    observed: bool = True
    tier: str | None = None
    tier_source: str | None = None
    #: Describe hops finished so far. Counted whether or not the log is on.
    describe_hops: int = 0
    #: Monotonic stamps of the transitions the capture itself executed.
    plan_mono: float | None = None
    attempt_mono: float | None = None
    first_byte_mono: float | None = None
    #: When the model's own text, reasoning or tool call first reached the
    #: client. Only an observed (logged) stream can see it.
    first_content_mono: float | None = None
    #: Upstream tries the ladder has recorded for the *current* attempt, and
    #: the last one's census code. ``None`` when the ladder is not installed
    #: (log off): not measured, not zero.
    attempt_tries: int | None = None
    last_try_status: int | None = None
    last_try_error_kind: str | None = None

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
    # --- known on arrival; read by the in-flight view ----------------------
    #: Where the request came from, exactly as the request log would store it
    #: and only when the capture settings allow it. A folder that only the
    #: prompt can supply is resolved at finalize, off the loop, so an in-flight
    #: request says ``project_dir_pending`` instead of guessing.
    session_id: str | None = None
    agent_id: str | None = None
    parent_session_id: str | None = None
    project_dir: str | None = None
    origin_source: str | None = None
    project_dir_pending: bool = False
    tools_count: int | None = None
    input_chars: int | None = None
    image_count: int = 0
    #: Chunks handed to the client by ``_PrefetchedStream``. Counted there and
    #: not in ``RequestCapture`` because the capture's observer is skipped
    #: entirely when the request log is off, and "is it still streaming" must
    #: not depend on a logging setting.
    chunks: int = 0
    first_chunk_mono: float | None = None
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
_INFLIGHT = True
_reaped_total = 0


def configure(*, enabled: bool, inflight: bool = False) -> None:
    """Adopt the operator's two switches. Called once, from the lifespan.

    ``enabled`` is ``REQUEST_WATCHDOG_ENABLED`` and ``inflight`` is
    ``REQUEST_INFLIGHT_ENABLED``; the registry tracks requests when either
    reader wants them. ``core`` may not import ``config``, so the answers are
    pushed in from ``runtime`` rather than read here. Turning both off empties
    the registry as well as stopping it filling: a process that switched both
    readers off should not go on holding weakrefs nobody will ever read.
    """

    global _ENABLED, _INFLIGHT
    with _LOCK:
        _INFLIGHT = bool(inflight)
        _ENABLED = bool(enabled) or _INFLIGHT
        if not _ENABLED:
            _ENTRIES.clear()


def enabled() -> bool:
    return _ENABLED


def inflight_enabled() -> bool:
    """Whether the in-flight view is switched on in this process.

    Read from what the lifespan adopted, not from the settings object: the
    switch is restart-required, and a view that answered "on" from an edited
    setting over a registry that was never filled would be showing an empty
    list as a measurement.
    """

    return _ENABLED and _INFLIGHT


def register(
    *,
    request_id: str,
    endpoint: str,
    protocol: str,
    stream: bool,
    harness: str | None,
    requested_model: str | None,
    progress: ProgressReader | None,
    started_at_mono: float | None = None,
    session_id: str | None = None,
    agent_id: str | None = None,
    parent_session_id: str | None = None,
    project_dir: str | None = None,
    origin_source: str | None = None,
    project_dir_pending: bool = False,
    tools_count: int | None = None,
    input_chars: int | None = None,
    image_count: int = 0,
) -> RequestTaskEntry | None:
    """Start tracking one request. O(1); the last statement of a capture.

    ``started_at_mono`` is the capture's own arrival stamp, so the age the
    in-flight view reports and the phase stamps the capture takes afterwards
    are measured on one clock from one origin.
    """

    if not _ENABLED:
        return None
    now = time.monotonic()
    started = now if started_at_mono is None else started_at_mono
    entry = RequestTaskEntry(
        request_id=request_id,
        started_at_mono=started,
        started_at_wall=time.time() - (now - started),
        endpoint=endpoint,
        protocol=protocol,
        stream=stream,
        harness=harness,
        requested_model=requested_model,
        session_id=session_id,
        agent_id=agent_id,
        parent_session_id=parent_session_id,
        project_dir=project_dir,
        origin_source=origin_source,
        project_dir_pending=project_dir_pending,
        tools_count=tools_count,
        input_chars=input_chars,
        image_count=image_count,
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
    moment = time.monotonic()
    if entry.chunks == 0:
        entry.first_chunk_mono = moment
    entry.chunks += 1
    entry.last_chunk_mono = moment
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

    global _ENABLED, _INFLIGHT, _reaped_total
    with _LOCK:
        _ENTRIES.clear()
        _ENABLED = True
        _INFLIGHT = True
        _reaped_total = 0
    _CURRENT.set(None)


def iter_entries() -> Iterator[RequestTaskEntry]:
    yield from snapshot()


def inflight_count() -> int | None:
    """How many requests are in flight, or ``None`` when nobody is counting.

    Reaps first, like every other reader. A streamed answer with the request
    log off never finalizes -- its capture has no observer -- and leaves only
    when its tasks are done, so a bare ``len`` would go on counting it until
    some other reader happened by. Measured on the scratch rig: all eleven
    log-off requests of a run left through the reaper.
    """

    if not inflight_enabled():
        return None
    return len(snapshot())


def inflight_phase(
    entry: RequestTaskEntry, progress: RequestProgress | None
) -> tuple[str, float]:
    """The phase this request is in, and the monotonic moment it began.

    Latest transition wins, and each one is a stamp the capture or the stream
    took when the transition happened. The first byte is whichever of the two
    witnesses saw it first: the capture's observer (log on) or the chunk
    counter where bytes leave (always).
    """

    candidates = [entry.first_chunk_mono]
    if progress is not None:
        candidates.append(progress.first_byte_mono)
    stamps = [stamp for stamp in candidates if stamp is not None]
    first_byte = min(stamps) if stamps else None
    if first_byte is not None:
        if progress is not None and progress.first_content_mono is not None:
            return PHASE_STREAMING, progress.first_content_mono
        if progress is not None and progress.observed:
            return PHASE_AWAITING_CONTENT, first_byte
        return PHASE_STREAMING, first_byte
    if progress is None:
        return PHASE_RECEIVED, entry.started_at_mono
    if progress.attempt_mono is not None:
        return PHASE_ATTEMPT, progress.attempt_mono
    if progress.plan_mono is not None:
        return PHASE_ROUTING, progress.plan_mono
    if progress.describe_hops > 0:
        return PHASE_DESCRIBE, entry.started_at_mono
    return PHASE_RECEIVED, entry.started_at_mono


def inflight_row(entry: RequestTaskEntry, *, now: float) -> dict[str, Any]:
    """One in-flight request as the view shows it: counts and labels only.

    Every value is a string, a number, a bool or ``None``, so the endpoint can
    hand the list to ``JSONResponse`` without an encoder pass. ``None`` means
    "not measured" (the house rule), never zero: characters streamed are only
    counted while the request log observes the stream, and the ladder's tries
    only while it is installed.
    """

    progress = entry.read_progress()
    phase, phase_since = inflight_phase(entry, progress)
    ttft_ms: float | None = None
    if progress is not None and progress.ttft_ms is not None:
        ttft_ms = progress.ttft_ms
    elif entry.first_chunk_mono is not None:
        ttft_ms = (entry.first_chunk_mono - entry.started_at_mono) * 1000
    last_chunk_age = (
        None if entry.last_chunk_mono is None else now - entry.last_chunk_mono
    )
    observed = progress is not None and progress.observed
    content_ms: float | None = None
    if progress is not None and progress.first_content_mono is not None:
        content_ms = (progress.first_content_mono - entry.started_at_mono) * 1000
    row: dict[str, Any] = {
        "id": entry.request_id,
        "started_at": entry.started_at_wall,
        "started_at_mono": round(entry.started_at_mono, 3),
        "elapsed_ms": round((now - entry.started_at_mono) * 1000, 1),
        "endpoint": entry.endpoint,
        "protocol": entry.protocol,
        "stream": entry.stream,
        "harness": entry.harness,
        "requested_model": entry.requested_model,
        "tier": None if progress is None else progress.tier,
        "tier_source": None if progress is None else progress.tier_source,
        "attempt_index": None if progress is None else progress.attempt_index,
        "provider": None if progress is None else progress.provider,
        "model_ref": None if progress is None else progress.model_ref,
        "phase": phase,
        "phase_since": round(phase_since, 3),
        "phase_elapsed_ms": round(max(0.0, now - phase_since) * 1000, 1),
        "describe_hops": 0 if progress is None else progress.describe_hops,
        "observed": observed,
        "ttft_ms": None if ttft_ms is None else round(ttft_ms, 1),
        "first_content_ms": None if content_ms is None else round(content_ms, 1),
        "output_chars": progress.output_chars if observed and progress else None,
        "thinking_chars": (progress.thinking_chars if observed and progress else None),
        "chunks_to_client": entry.chunks,
        "last_chunk_age_s": (
            None if last_chunk_age is None else round(last_chunk_age, 3)
        ),
        "waited_s": (0.0 if progress is None else round(progress.waited_seconds, 3)),
        "attempt_tries": None if progress is None else progress.attempt_tries,
        "last_try_status": None if progress is None else progress.last_try_status,
        "last_try_error_kind": (
            None if progress is None else progress.last_try_error_kind
        ),
        "key_label": None if progress is None else progress.key_label,
        "proxy_label": None if progress is None else progress.proxy_label,
        "tools_count": entry.tools_count,
        "input_chars": entry.input_chars,
        "image_count": entry.image_count,
        "session_id": entry.session_id,
        "session_short": session_short(entry.session_id),
        "agent_id": entry.agent_id,
        "parent_session_id": entry.parent_session_id,
        "project_dir": entry.project_dir,
        "project_short": project_short(entry.project_dir),
        "project_dir_pending": entry.project_dir_pending,
        "origin_source": entry.origin_source,
    }
    return row


def inflight_report(*, limit: int = DEFAULT_INFLIGHT_LIMIT) -> dict[str, Any]:
    """Everything ``GET /admin/api/requests/in-flight`` answers with.

    One lock, one reap, and one short loop over at most ``limit`` entries --
    no database, no provider, no stack walk. Oldest first, and the cut is made
    *after* sorting, so the rows shown are the ones worth looking at and
    ``total`` says how many there are in all.
    """

    if not inflight_enabled():
        return {"enabled": False}
    now = time.monotonic()
    entries = snapshot()
    shown = entries[: max(0, limit)]
    return {
        "enabled": True,
        "total": len(entries),
        "shown": len(shown),
        "truncated": len(shown) < len(entries),
        "reaped": reaped_total(),
        "now_mono": round(now, 3),
        "rows": [inflight_row(entry, now=now) for entry in shown],
    }


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


__all__ = [
    "DEFAULT_INFLIGHT_LIMIT",
    "INFLIGHT_PHASES",
    "MAX_TASKS_PER_REQUEST",
    "PHASE_ATTEMPT",
    "PHASE_AWAITING_CONTENT",
    "PHASE_AWAITING_FIRST_BYTE",
    "PHASE_BETWEEN_ATTEMPTS",
    "PHASE_DESCRIBE",
    "PHASE_RECEIVED",
    "PHASE_ROUTING",
    "PHASE_STREAMING",
    "PHASE_STREAMING_STOPPED",
    "RequestProgress",
    "RequestTaskEntry",
    "configure",
    "count",
    "current_entry",
    "enabled",
    "inflight_count",
    "inflight_enabled",
    "inflight_phase",
    "inflight_report",
    "inflight_row",
    "iter_entries",
    "note_serving_task",
    "note_stream_chunk",
    "reaped_total",
    "register",
    "reset",
    "snapshot",
    "unregister",
]
