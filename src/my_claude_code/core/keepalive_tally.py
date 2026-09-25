"""How many empty-delta keepalive frames one request's stream carried.

The frames are written at the HTTP boundary (``api/response_streams.py``),
outside ``RequestCapture``, precisely so they can never move ``ttft_ms``,
``output_chars`` or anything else the capture measures. That also means the
capture never sees them, so the count travels the way every other per-request
collector does: the capture installs a mutable slot in a ``ContextVar`` when it
is created, the streaming seam picks the slot up while it builds the response
-- in the same task, before any frame exists -- and increments it directly, and
the capture reads it back when it finalizes the row.

``frames_mode`` says whether the seam ran with ``STREAM_KEEPALIVE_MODE=frames``
on a surface that has frames at all (only ``/v1/messages``). It is what lets
the row tell "frames mode was on and none were needed" (0) from "nobody was
counting" (NULL).
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class KeepaliveTally:
    """Mutable per-request count of empty-delta keepalive frames."""

    frames_mode: bool = False
    frames: int = 0

    def recorded_frames(self) -> int | None:
        """The count for the request row: ``None`` unless frames mode ran."""

        return self.frames if self.frames_mode else None


_KEEPALIVE_TALLY: ContextVar[KeepaliveTally | None] = ContextVar(
    "mcc_keepalive_tally", default=None
)


def install_keepalive_tally() -> KeepaliveTally:
    """Start counting keepalive frames for the current request."""

    slot = KeepaliveTally()
    _KEEPALIVE_TALLY.set(slot)
    return slot


def current_keepalive_tally() -> KeepaliveTally | None:
    """The current request's tally, or ``None`` when nothing is counting."""

    return _KEEPALIVE_TALLY.get()
