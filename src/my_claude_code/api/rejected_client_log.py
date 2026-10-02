"""One WARNING per distinct client a ``/v1/*`` 401 turned away, never the token.

INVESTIGATION-SELF-INFLICTED-LOAD.md §3: a Claude Code process running with a
stale or wrong proxy token retries roughly once a second, forever, and until
now nothing was logged before the 401 left -- an operator staring at
``server.log`` during a load spike had no way to tell "a client is hammering
us with a bad token" from "nothing is happening here at all".

The fix has two shapes on purpose:

* The **first** rejection from a client this module has not seen in the
  current window gets its own WARNING line, identifying the client by
  everything decision 11 allows -- its user agent, which auth header NAMES it
  presented (never their values), and its remote address without the port --
  and nothing it is not allowed to carry: the token, any part of it, a hash of
  it, or any header value other than the user agent, never appear here.
* Every rejection after that, from a client already seen this window, is
  counted silently, and at most one summary line per window says how many
  there were. A client retrying once a second for an hour produces one WARNING
  and sixty summary lines, not 3600 WARNINGs.

The "seen this window" set is bounded by *time*, not by an invented count: it
is cleared whenever a window rolls over, which is also when the summary for
the window that just ended is logged. A client silent for a whole window is
forgotten and gets a fresh WARNING the next time it is rejected, which is
exactly what decision 11 asks for ("forget a client after the summary
window").
"""

import time
from dataclasses import dataclass
from threading import Lock

from fastapi import Request
from loguru import logger

#: How often the suppressed-rejection count is summarised, and how long a
#: client is remembered before being forgotten and logged fresh again.
SUMMARY_WINDOW_SECONDS = 60.0

#: The only header names this module will ever name in a log line. Values are
#: never read for logging purposes -- only whether each of these was present.
_AUTH_HEADER_NAMES: tuple[str, ...] = ("authorization", "x-api-key", "x-goog-api-key")


@dataclass(frozen=True)
class RejectedClient:
    """Identifies a client without ever carrying its credentials.

    Hashable, so it is also the dict key the tracker below uses to recognise
    "a client already seen this window".
    """

    remote_address: str
    user_agent: str
    auth_header_names: tuple[str, ...]


def describe_rejected_client(request: Request) -> RejectedClient:
    """Build the identity a 401 is logged under, from one request."""

    client = request.client
    remote_address = client.host if client is not None else "unknown"
    user_agent = request.headers.get("user-agent", "")
    present = tuple(
        name for name in _AUTH_HEADER_NAMES if request.headers.get(name) is not None
    )
    return RejectedClient(
        remote_address=remote_address,
        user_agent=user_agent,
        auth_header_names=present,
    )


class RejectedClientTracker:
    """Process-wide dedup-and-summarise state for rejected ``/v1/*`` clients."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._reset_locked(time.monotonic())

    def _reset_locked(self, now: float) -> None:
        self._window_start = now
        self._seen: set[RejectedClient] = set()
        self._count = 0

    def reset(self) -> None:
        """Forget every client and start a fresh window. Tests only."""

        with self._lock:
            self._reset_locked(time.monotonic())

    def note_rejection(self, client: RejectedClient) -> None:
        """Record one 401 and log what decision 11 asks for."""

        now = time.monotonic()
        is_new = False
        summary: tuple[int, int] | None = None
        with self._lock:
            if now - self._window_start >= SUMMARY_WINDOW_SECONDS:
                if self._count:
                    summary = (self._count, len(self._seen))
                self._reset_locked(now)
            self._count += 1
            if client not in self._seen:
                self._seen.add(client)
                is_new = True
        if summary is not None:
            total, distinct = summary
            logger.warning(
                "REJECTED CLIENT SUMMARY: {} rejected /v1 request(s) from {} "
                "distinct client(s) in the last {:.0f}s.",
                total,
                distinct,
                SUMMARY_WINDOW_SECONDS,
            )
        if is_new:
            logger.warning(
                "REJECTED CLIENT: /v1 request rejected (missing or wrong "
                "proxy token) from {} (user-agent={!r}, auth headers "
                "present={}).",
                client.remote_address,
                client.user_agent,
                list(client.auth_header_names),
            )


_TRACKER = RejectedClientTracker()


def rejected_client_tracker() -> RejectedClientTracker:
    """The one process-wide tracker. A module global for the same reason
    ``core.loop_health.loop_health`` is."""

    return _TRACKER


__all__ = [
    "SUMMARY_WINDOW_SECONDS",
    "RejectedClient",
    "RejectedClientTracker",
    "describe_rejected_client",
    "rejected_client_tracker",
]
