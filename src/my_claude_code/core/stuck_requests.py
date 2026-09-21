"""One in-flight request, rendered as the record a stall investigation needs.

The join between :mod:`my_claude_code.core.request_tasks` (which request, which
tasks) and :mod:`my_claude_code.core.async_stacks` (where those tasks are
suspended). It lives in ``core`` rather than in the watchdog because two
callers need exactly the same document and must not drift: the background
watchdog that writes it to ``logs/stuck-requests.jsonl`` when nobody is looking,
and ``GET /admin/api/tasks/stacks`` when somebody is.

Everything here is a counter, a label, a code location or a phase name. The
privacy contract is the one :mod:`async_stacks` states: frames carry
``file:line function`` and never a value the program was working on, and the
credential and proxy labels are the already-masked ones those slots hold.
"""

import asyncio
import time
from typing import Any

from my_claude_code.core.async_stacks import (
    DEFAULT_FRAME_LIMIT,
    DEFAULT_TASK_LIMIT,
    YIELD_EVERY,
    describe_task,
    group_by_deepest_frame,
    live_tasks,
)
from my_claude_code.core.request_tasks import (
    PHASE_BETWEEN_ATTEMPTS,
    RequestTaskEntry,
    count,
    reaped_total,
    snapshot,
)

#: How many in-flight requests one answer describes in full. Everything past it
#: is counted, never silently dropped: the answer says ``truncated``.
DEFAULT_REQUEST_LIMIT = 50


def describe_entry(
    entry: RequestTaskEntry,
    *,
    now: float | None = None,
    frame_limit: int = DEFAULT_FRAME_LIMIT,
    phase_override: str | None = None,
    no_progress_for: float | None = None,
) -> dict[str, Any]:
    """The whole record for one request, stacks included.

    ``phase_override`` and ``no_progress_for`` exist for the watchdog, which
    holds two consecutive samples and can therefore say things a single read
    cannot -- that the waiting clock is still moving, and how long ago the last
    observed change was. The endpoint passes neither and reports the structural
    phase plus the age of the last chunk, which is all one look can honestly
    support.
    """

    moment = time.monotonic() if now is None else now
    progress = entry.read_progress()
    stacks = [describe_task(task, limit=frame_limit) for task in entry.tasks()]
    last_chunk_age = (
        None if entry.last_chunk_mono is None else moment - entry.last_chunk_mono
    )
    still_for = no_progress_for
    if still_for is None:
        still_for = moment - max(entry.last_progress_mono, entry.last_chunk_mono or 0.0)
    phase = phase_override or (
        progress.phase if progress is not None else PHASE_BETWEEN_ATTEMPTS
    )
    record: dict[str, Any] = {
        "request_id": entry.request_id,
        "started_at": entry.started_at_wall,
        "age_s": round(moment - entry.started_at_mono, 3),
        "no_progress_for_s": round(max(0.0, still_for), 3),
        "phase": phase,
        "endpoint": entry.endpoint,
        "protocol": entry.protocol,
        "stream": entry.stream,
        "harness": entry.harness,
        "requested_model": entry.requested_model,
        "chunks_to_client": entry.chunks,
        "last_chunk_age_s": (
            None if last_chunk_age is None else round(last_chunk_age, 3)
        ),
        "tasks": [stack.as_dict() for stack in stacks],
        "deepest_frame": stacks[-1].deepest if stacks else None,
    }
    if progress is not None:
        record.update(
            {
                "attempt_index": progress.attempt_index,
                "provider": progress.provider,
                "model_ref": progress.model_ref,
                "key_label": progress.key_label,
                "proxy_label": progress.proxy_label,
                "ttft_ms": progress.ttft_ms,
                "output_chars": progress.output_chars,
                "thinking_chars": progress.thinking_chars,
                "upstream_tries": progress.tries,
                "waited_s": round(progress.waited_seconds, 3),
            }
        )
    return record


async def stuck_report(
    *,
    stall_seconds: float,
    frame_limit: int = DEFAULT_FRAME_LIMIT,
    task_limit: int = DEFAULT_TASK_LIMIT,
    request_limit: int = DEFAULT_REQUEST_LIMIT,
    yield_every: int = YIELD_EVERY,
) -> dict[str, Any]:
    """Everything ``GET /admin/api/tasks/stacks`` answers with.

    Two halves, and the second is the one that would have settled 09-16 in a
    single look: the in-flight requests with their own stacks, and *every*
    task on the loop counted by the frame it is suspended on. Nine tasks under
    one ``file:line function`` is a finding; nine tasks under nine is a
    different one, and no other view tells them apart.

    **Why this is a coroutine.** Building a stack is synchronous work, measured
    on the scratch rig at **0.66 ms per request** (33 frames through sixteen
    nested async generators) and **0.42 ms per task** for the summary's deepest
    frame. At a hundred in-flight requests that is ~130 ms, and a route that
    held this server's one event loop for 130 ms would be the very thing
    7.27.0's loop-health work exists to stop. So the walk yields to the loop
    every :data:`YIELD_EVERY` requests and every :data:`YIELD_EVERY` tasks: the
    wall time is unchanged, but no single slice is longer than a few
    milliseconds and ``/health`` keeps answering at its usual latency.
    """

    now = time.monotonic()
    entries = snapshot()
    oldest_first = sorted(entries, key=lambda entry: entry.started_at_mono)
    shown = oldest_first[:request_limit]
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(shown):
        rows.append(describe_entry(entry, now=now, frame_limit=frame_limit))
        if yield_every > 0 and (index + 1) % yield_every == 0:
            await asyncio.sleep(0)
    stuck = 0
    if stall_seconds > 0:
        stuck = sum(1 for row in rows if row["no_progress_for_s"] >= stall_seconds)
    return {
        "now": time.time(),
        "stall_seconds": stall_seconds,
        "in_flight": count(),
        "reaped": reaped_total(),
        "stuck": stuck,
        "shown": len(rows),
        "truncated": len(oldest_first) > len(rows),
        "requests": rows,
        "tasks_by_deepest_frame": await group_by_deepest_frame(
            live_tasks(),
            task_limit=task_limit,
            frame_limit=frame_limit,
            yield_every=yield_every,
        ),
    }


__all__ = [
    "DEFAULT_REQUEST_LIMIT",
    "YIELD_EVERY",
    "describe_entry",
    "stuck_report",
]
