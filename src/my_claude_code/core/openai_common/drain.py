"""Read an Anthropic stream to its end after the terminal event, discarding it.

Every non-Messages adapter (``/v1/responses``, ``/v1/chat/completions``,
Gemini ``:streamGenerateContent``) used to ``return`` the moment it translated
the terminal event. That closed the executor's generator while it was still
suspended after its last frame, so the bookkeeping the executor runs once its
provider stream is exhausted -- the attempt's ``succeeded`` outcome and
``route_health.record_success`` -- never ran. The capture then committed the
row with no attempt, and the attempt that followed arrived as
``failed/interrupted`` after the row was sealed and was dropped. Only
``/v1/messages`` drains its stream, so only it recorded either.

Reading to the end costs one more pull after ``message_stop``: the executor
finishes its bookkeeping and the stream ends. Nothing read here is
translated -- the client already has its terminal frame -- and a failure
raised after the terminal event is traced, never reported to the client,
because the answer it would contradict is already complete.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from my_claude_code.core.trace import trace_event


async def drain_after_terminal(
    events: AsyncIterator[Any], *, owner: str, source: str = "core"
) -> None:
    """Exhaust ``events`` without translating anything it still yields."""
    discarded = 0
    try:
        async for _ in events:
            discarded += 1
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        trace_event(
            stage="lifecycle",
            event="stream.after_terminal.failed",
            source=source,
            owner=owner,
            exc_type=type(exc).__name__,
            discarded=discarded,
        )
        return
    if discarded:
        trace_event(
            stage="lifecycle",
            event="stream.after_terminal.discarded",
            source=source,
            owner=owner,
            discarded=discarded,
        )
