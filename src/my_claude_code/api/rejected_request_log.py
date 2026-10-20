"""One WARNING per client whose request body a model door refused, never the body.

RESUMED-SESSION-422 §1.4: a request a model door cannot parse left no trace
anywhere the user looks -- no request-log row, no ``server.log`` line, only an
access line on a console nobody reads -- while the client showed a 190 KB
error. Decision Q4(a): one WARNING line in ``server.log``, modelled on
``REJECTED CLIENT`` (:mod:`.rejected_client_log`), with the same two shapes:

* the **first** refusal from a client not seen in the current window gets its
  own line: the door, the status, the first failing field and pydantic's
  reason for it, how many fields failed, the client's address and user agent,
  the harness the request says it came from, how many messages the body held
  and how large it was. Never a value from the body.
* every refusal after that from a client already seen this window is counted,
  and at most one summary line per window says how many there were -- a
  client retrying a bad body once a second for an hour leaves one WARNING and
  sixty summaries, not 3600 WARNINGs.

The window is :data:`.rejected_client_log.SUMMARY_WINDOW_SECONDS`, the same
one the 401 tracker uses.
"""

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from fastapi import Request
from loguru import logger

from my_claude_code.core.client_fingerprint import harness_from_headers

from .model_door_errors import ValidationReason
from .rejected_client_log import SUMMARY_WINDOW_SECONDS


@dataclass(frozen=True, slots=True)
class RejectedClientKey:
    """Who sent it, as the dedup window knows them (hashable)."""

    remote_address: str
    user_agent: str
    door: str


@dataclass(frozen=True, slots=True)
class RejectedRequest:
    """Everything one refusal is logged with; metadata only."""

    client: RejectedClientKey
    method: str
    status: int
    first: ValidationReason | None
    field_count: int
    harness: str | None
    message_count: int | None
    body_bytes: int


def describe_rejected_request(
    request: Request,
    *,
    status: int,
    reasons: list[ValidationReason],
    body: Any,
    body_bytes: int,
) -> RejectedRequest:
    """Build the log record for one refused body from the request's metadata."""
    client = request.client
    return RejectedRequest(
        client=RejectedClientKey(
            remote_address=client.host if client is not None else "unknown",
            user_agent=request.headers.get("user-agent", ""),
            door=request.url.path,
        ),
        method=request.method,
        status=status,
        first=reasons[0] if reasons else None,
        field_count=len(reasons),
        harness=harness_from_headers(request.headers).harness,
        message_count=_message_count(body),
        body_bytes=body_bytes,
    )


def _message_count(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    for field in ("messages", "input"):
        value = body.get(field)
        if isinstance(value, list):
            return len(value)
    return None


def _size(body_bytes: int) -> str:
    if body_bytes < 1024:
        return f"{body_bytes} B"
    return f"{body_bytes / 1024:.1f} KB"


class RejectedRequestTracker:
    """Process-wide dedup-and-summarise state for refused model-door bodies."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._reset_locked(time.monotonic())

    def _reset_locked(self, now: float) -> None:
        self._window_start = now
        self._seen: set[RejectedClientKey] = set()
        self._count = 0

    def reset(self) -> None:
        """Forget every client and start a fresh window. Tests only."""
        with self._lock:
            self._reset_locked(time.monotonic())

    def note_rejection(self, rejected: RejectedRequest) -> None:
        """Record one refusal; log it if its client is new this window."""
        now = time.monotonic()
        is_new = False
        summary: tuple[int, int] | None = None
        with self._lock:
            if now - self._window_start >= SUMMARY_WINDOW_SECONDS:
                if self._count:
                    summary = (self._count, len(self._seen))
                self._reset_locked(now)
            self._count += 1
            if rejected.client not in self._seen:
                self._seen.add(rejected.client)
                is_new = True
        if summary is not None:
            total, distinct = summary
            logger.warning(
                "REJECTED REQUEST SUMMARY: {} model-door request(s) refused as "
                "not valid from {} distinct client(s) in the last {:.0f}s.",
                total,
                distinct,
                SUMMARY_WINDOW_SECONDS,
            )
        if is_new:
            first = rejected.first
            logger.warning(
                "REJECTED REQUEST: {} {} answered {} -- {}: {} ({} field(s) failed) "
                "from {} (user-agent={!r}, harness={}, messages={}, body {}).",
                rejected.method,
                rejected.client.door,
                rejected.status,
                first.path if first is not None else "body",
                first.reason if first is not None else "not valid",
                rejected.field_count,
                rejected.client.remote_address,
                rejected.client.user_agent,
                rejected.harness or "unknown",
                rejected.message_count if rejected.message_count is not None else "-",
                _size(rejected.body_bytes),
            )


_TRACKER = RejectedRequestTracker()


def rejected_request_tracker() -> RejectedRequestTracker:
    """The one process-wide tracker, like ``rejected_client_tracker``."""
    return _TRACKER


__all__ = [
    "RejectedClientKey",
    "RejectedRequest",
    "RejectedRequestTracker",
    "describe_rejected_request",
    "rejected_request_tracker",
]
