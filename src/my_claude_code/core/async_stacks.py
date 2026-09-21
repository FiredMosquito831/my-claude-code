"""Where an ``asyncio`` task is actually suspended, as code locations only.

On 2026-09-16 nine parallel requests parked for 47 minutes and the only
artefact that could have named the ``await`` holding them -- the server log --
had rotated away by the time anyone looked. This module is the instrument that
answers that question the next time it is asked, and it answers it with frames:
``file:line function``, and nothing else. Never ``f_locals``, never an argument
value, never a source line. A stack is a map of the program, not of its data.

Two things here are not obvious and both are load-bearing:

**1. ``Task.get_stack()`` alone is nearly useless for this.** It walks
``frame.f_back``, and a task coroutine has no caller frame -- so for a
*suspended* task it returns exactly one frame: the outermost ``async def`` the
task was created with. Measured at 3.14.0 on a task parked inside
``asyncio.Lock.acquire``::

    get_stack: [('lock_park', 37)]

The awaits below it live on ``cr_await`` (coroutines), ``gi_yieldfrom``
(generators) and ``ag_await`` (async generators), which is a *different* chain.
Walking it gives the frame that matters::

    lock_park -> Lock.__aenter__ -> Lock.acquire -> <FutureIter>

The last named frame is the deepest ``await``, and it is the only one worth
reading first.

**2. An async generator breaks that chain, and ``gc`` repairs it.** ``await
anext(agen)`` suspends on an ``async_generator_asend`` object, which exposes no
attribute pointing at the generator it is driving -- the walk stops dead at a
name that says nothing. It does hold a reference to it, though, so
:func:`gc.get_referents` (one object, no collection, no full traversal) hands it
back and the walk continues into the generator's own ``ag_await``. That matters
here more than anywhere: MCC's whole streaming path is nested async generators,
so without this bridge every streaming request would report its deepest frame as
the outermost one and the instrument would be a decoration.

Paths are made package-relative before they are written anywhere
(:func:`package_relative`): ``my_claude_code/api/response_streams.py``,
``site-packages/httpcore/_async/socks_proxy.py``, ``stdlib/asyncio/locks.py``.
A frame must be readable in a bug report without telling the reader the
operator's username or where they keep their files.
"""

import asyncio
import gc
import inspect
import sysconfig
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import FrameType
from typing import Any

#: How many frames one task's stack may contribute. Deep enough for the whole
#: of MCC's request path plus the SDK under it, shallow enough that a runaway
#: recursion cannot fill a log line.
DEFAULT_FRAME_LIMIT = 40

#: How many tasks the all-task summary will look at. Building a stack is
#: synchronous work on the event loop, so the number that bounds it is a cap,
#: not a hope. See ``tests/core/test_async_stacks.py`` for the measurement.
DEFAULT_TASK_LIMIT = 500

#: How many requests, or tasks, one slice of that synchronous work covers
#: before it hands the event loop back. Ten is ~7 ms of walking at the measured
#: 0.66 ms per request stack -- shorter than one ordinary request's own work,
#: and far under ``HEALTH_BUSY_LAG_MS``. ``0`` never yields, which is what a
#: caller off the loop wants.
YIELD_EVERY = 10

#: Awaitables that hide the thing they are driving. ``gc.get_referents`` is the
#: only way back to it, and it is cheap for a single object.
_BRIDGED_TYPES = frozenset(
    {
        "async_generator_asend",
        "async_generator_athrow",
        "coroutine_wrapper",
    }
)

_UNKNOWN_FRAME = "<unknown>"


def _stdlib_root() -> str:
    try:
        root = sysconfig.get_paths().get("stdlib") or ""
    except Exception:  # pragma: no cover - a broken sysconfig is not our story
        return ""
    return root.replace("\\", "/").rstrip("/").lower()


_STDLIB_ROOT = _stdlib_root()


@lru_cache(maxsize=4096)
def package_relative(filename: str) -> str:
    """Return *filename* with everything above its package root removed.

    Cached because ``co_filename`` is interned and a stack of forty frames on a
    hundred in-flight requests is four thousand calls with perhaps sixty
    distinct paths. Measured on the scratch rig at 100 in-flight requests, the
    cache took the whole endpoint from 440 ms to 275 ms; the rest of that cost
    is the walk itself, and :data:`YIELD_EVERY` is what stops it holding the
    loop.

    The rule is anchor-based and deliberately conservative: a path that matches
    no anchor is reduced to its bare basename rather than guessed at, because
    the failure mode of a guess is writing ``Users/<someone>`` into a log that
    gets pasted into an issue.
    """

    if not filename:
        return _UNKNOWN_FRAME
    # ``<frozen importlib._bootstrap>``, ``<string>``: already location-free.
    if filename.startswith("<"):
        return filename
    normalised = filename.replace("\\", "/")
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts:
        return _UNKNOWN_FRAME
    lowered = [part.lower() for part in parts]
    for anchor in ("site-packages", "dist-packages"):
        if anchor in lowered:
            index = len(lowered) - 1 - lowered[::-1].index(anchor)
            return "/".join(parts[index:])
    if "my_claude_code" in lowered:
        index = len(lowered) - 1 - lowered[::-1].index("my_claude_code")
        return "/".join(parts[index:])
    if _STDLIB_ROOT and normalised.lower().startswith(_STDLIB_ROOT + "/"):
        return "stdlib/" + normalised[len(_STDLIB_ROOT) + 1 :]
    if "tests" in lowered:
        index = len(lowered) - 1 - lowered[::-1].index("tests")
        return "/".join(parts[index:])
    return parts[-1]


def format_frame(frame: FrameType) -> str:
    """One frame as ``package/relative/path.py:LINE qualname``.

    Three attributes of ``f_code`` plus ``f_lineno``. ``f_locals`` is never
    read here and must never be read here; the privacy contract of this whole
    module is that a frame carries no value the program was working on.
    """

    code = frame.f_code
    name = getattr(code, "co_qualname", None) or code.co_name
    return f"{package_relative(code.co_filename)}:{frame.f_lineno} {name}"


def _awaited(obj: object) -> object | None:
    """The next link down the await chain, or ``None`` at the bottom."""

    nxt = getattr(obj, "cr_await", None)
    if nxt is None:
        nxt = getattr(obj, "gi_yieldfrom", None)
    if nxt is None:
        nxt = getattr(obj, "ag_await", None)
    if nxt is not None:
        return nxt
    if type(obj).__name__ not in _BRIDGED_TYPES:
        return None
    try:
        referents = gc.get_referents(obj)
    except Exception:  # pragma: no cover - a C type without tp_traverse
        return None
    for ref in referents:
        if inspect.isasyncgen(ref) or inspect.iscoroutine(ref):
            return ref
    return None


def _own_frame(obj: object) -> FrameType | None:
    for attribute in ("cr_frame", "gi_frame", "ag_frame"):
        frame = getattr(obj, attribute, None)
        if isinstance(frame, FrameType):
            return frame
    return None


def await_chain_frames(
    root: object,
    *,
    limit: int = DEFAULT_FRAME_LIMIT,
    skip_first: bool = False,
) -> list[str]:
    """Frames from *root* down to the deepest suspended ``await``.

    Outermost first, so the last entry is the deepest frame -- the one that
    answers "what is it waiting on". Cycle-guarded by object identity, because
    a malformed awaitable that returns itself would otherwise spin forever on
    the event loop this is supposed to be measuring.
    """

    frames: list[str] = []
    obj: object | None = root
    seen: set[int] = set()
    first = True
    while obj is not None and len(frames) < limit:
        if id(obj) in seen:
            break
        seen.add(id(obj))
        frame = _own_frame(obj)
        if frame is not None and not (first and skip_first):
            frames.append(format_frame(frame))
        first = False
        obj = _awaited(obj)
    return frames


def task_frames(
    task: asyncio.Task[Any], *, limit: int = DEFAULT_FRAME_LIMIT
) -> list[str]:
    """The full frame list for one task: callers first, deepest await last."""

    frames: list[str] = []
    try:
        frames.extend(format_frame(frame) for frame in task.get_stack(limit=limit))
    except Exception:  # pragma: no cover - a task torn down mid-read
        frames = []
    coro = None
    try:
        coro = task.get_coro()
    except Exception:  # pragma: no cover - same
        coro = None
    if coro is not None:
        remaining = max(0, limit - len(frames))
        # ``get_stack`` already reported the coroutine's own frame as the last
        # of the ``f_back`` chain, so the walk starts one link below it.
        frames.extend(
            await_chain_frames(coro, limit=remaining, skip_first=bool(frames))
        )
    return frames[:limit]


def deepest_frame(task: asyncio.Task[Any]) -> str:
    """The single frame a reader should look at first.

    Walks the chain without formatting anything above the bottom of it. The
    all-task summary calls this once per task on the loop, so formatting forty
    frames to throw away thirty-nine is the difference between a cheap answer
    and one that holds the loop.
    """

    try:
        coro = task.get_coro()
    except Exception:  # pragma: no cover - a task torn down mid-read
        coro = None
    frame: FrameType | None = None
    obj: object | None = coro
    seen: set[int] = set()
    steps = 0
    while obj is not None and steps < DEFAULT_FRAME_LIMIT:
        if id(obj) in seen:
            break
        seen.add(id(obj))
        steps += 1
        own = _own_frame(obj)
        if own is not None:
            frame = own
        obj = _awaited(obj)
    if frame is None:
        try:
            stack = task.get_stack(limit=1)
        except Exception:  # pragma: no cover
            stack = []
        if stack:
            frame = stack[-1]
    return format_frame(frame) if frame is not None else _UNKNOWN_FRAME


@dataclass(slots=True)
class TaskStack:
    """One task's identity and where it is suspended."""

    name: str
    done: bool
    frames: list[str] = field(default_factory=list)

    @property
    def deepest(self) -> str:
        return self.frames[-1] if self.frames else _UNKNOWN_FRAME

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "done": self.done,
            "deepest_frame": self.deepest,
            "frames": list(self.frames),
        }


def describe_task(
    task: asyncio.Task[Any], *, limit: int = DEFAULT_FRAME_LIMIT
) -> TaskStack:
    try:
        name = task.get_name()
    except Exception:  # pragma: no cover
        name = "<task>"
    return TaskStack(name=name, done=task.done(), frames=task_frames(task, limit=limit))


async def group_by_deepest_frame(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    task_limit: int = DEFAULT_TASK_LIMIT,
    frame_limit: int = DEFAULT_FRAME_LIMIT,
    yield_every: int = YIELD_EVERY,
) -> dict[str, Any]:
    """Count every live task by the frame it is suspended on.

    This is the summary that would have settled 09-16 in one look: nine tasks
    reporting the same ``proxy_rotating.py:NNN acquire`` is a different finding
    from nine tasks each waiting on their own socket, and no other view
    distinguishes them.
    """

    counts: dict[str, int] = {}
    examined = 0
    total = 0
    for task in tasks:
        total += 1
        if examined >= task_limit:
            continue
        examined += 1
        if yield_every > 0 and examined % yield_every == 0:
            # Measured at 0.42 ms per task. Five hundred of them in one slice
            # would hold this server's single event loop for a fifth of a
            # second, which is exactly what 7.27.0 stopped doing elsewhere.
            await asyncio.sleep(0)
        if task.done():
            continue
        frame = deepest_frame(task)
        counts[frame] = counts.get(frame, 0) + 1
    groups = [
        {"frame": frame, "tasks": count}
        for frame, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "total_tasks": total,
        "examined": examined,
        "truncated": total > examined,
        "groups": groups,
    }


def live_tasks() -> Sequence[asyncio.Task[Any]]:
    """Every task on the running loop, or an empty tuple off the loop."""

    try:
        return tuple(asyncio.all_tasks())
    except RuntimeError:
        return ()


__all__ = [
    "DEFAULT_FRAME_LIMIT",
    "DEFAULT_TASK_LIMIT",
    "YIELD_EVERY",
    "TaskStack",
    "await_chain_frames",
    "deepest_frame",
    "describe_task",
    "format_frame",
    "group_by_deepest_frame",
    "live_tasks",
    "package_relative",
    "task_frames",
]
