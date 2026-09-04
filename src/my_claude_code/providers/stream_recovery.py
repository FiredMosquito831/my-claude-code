"""Provider-owned stream holdback and recovery decisions."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import httpx

from my_claude_code.core.anthropic.stream_contracts import (
    REASONING_HEARTBEAT,
    sse_is_scaffolding,
    sse_visible_chars,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.trace import trace_event

from .failure_policy import retryable_transient_status

EARLY_TRANSPARENT_TOTAL_ATTEMPTS = 5
EARLY_TRANSPARENT_MAX_RETRIES = EARLY_TRANSPARENT_TOTAL_ATTEMPTS - 1
MIDSTREAM_RECOVERY_ATTEMPTS = 5
EARLY_HOLDBACK_SECONDS = 0.75
EARLY_HOLDBACK_CHARS = 0
RECOVERY_BUFFER_MAX_BYTES = 65_536


class TruncatedProviderStreamError(RuntimeError):
    """An upstream stream ended without its required terminal marker."""


class RecoveryFailureAction(StrEnum):
    """How one provider stream should respond to an upstream failure."""

    EARLY_RETRY = "early_retry"
    MIDSTREAM_RECOVERY = "midstream_recovery"
    FINAL_ERROR = "final_error"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Failure decision for one provider stream attempt."""

    action: RecoveryFailureAction
    retryable: bool
    committed: bool
    has_buffered: bool
    early_retry_attempt: int | None = None
    midstream_recovery_attempt: int | None = None


class RecoveryHoldbackBuffer:
    """Retain SSE until the reader has actually been shown something.

    The buffer is what makes an early failure recoverable: nothing it holds has
    reached the client, so the attempt can be retried -- by this provider, or by
    the next model on the route -- without the client ever seeing a seam.

    It commits when the first *content* delta arrives, not when a clock
    elapses. The clock is kept only as a backstop for streams that emit
    content this parser does not recognise. Committing on time meant a model
    that sent a ``message_start`` and then stalled had already burned the
    route: measured on 21 days of real traffic, 500 requests hung for the full
    600s budget with a three-model chain sitting unused, every one of them with
    ``tokens_out = 0``.

    ``holdback_chars`` is the second half of the same question. The window
    answers "has this model had time to fail yet"; the character count answers
    "has it said enough to be worth keeping". A model that writes one word and
    dies half a second later has shown the reader nothing worth protecting,
    and holding until *both* conditions are met lets the route start over on
    the next model with no seam and nothing lost. The cost is real and is paid
    by every request, not only the ones that fail: it is exactly that much
    time-to-first-visible-word. 0 -- the shipped default -- asks only the
    clock, which is how this buffer has always behaved.
    """

    def __init__(
        self,
        *,
        holdback_seconds: float = EARLY_HOLDBACK_SECONDS,
        holdback_chars: int = EARLY_HOLDBACK_CHARS,
        max_bytes: int = RECOVERY_BUFFER_MAX_BYTES,
        reasoning_commits: bool = True,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._holdback_seconds = holdback_seconds
        self._holdback_chars = max(0, holdback_chars)
        self._reasoning_commits = reasoning_commits
        self._max_bytes = max_bytes
        self._now = now or time.monotonic
        self._events: list[str] = []
        self._bytes = 0
        self._chars = 0
        self._started_at: float | None = None
        self.committed = False
        # Whether the most recent push held a frame back *only* because
        # reasoning is not allowed to commit. Routing reads it to tell a model
        # that is thinking silently from one that is simply silent -- the two
        # look identical from outside and deserve very different deadlines.
        self.last_push_held_reasoning = False

    def restart_window(self) -> None:
        """Re-anchor the holdback window to the next event, keeping the buffer.

        The opening ``message_start`` frame is built locally and pushed *before*
        the upstream request goes out, so anchoring the window to it spends the
        whole holdback on time-to-first-token -- measured at 9-180s against a
        0.75s window. Every stream then committed on its first upstream byte,
        which silently disabled both invisible early retry and the model
        fallback chain. Re-anchoring when the upstream stream opens makes the
        window mean what its name says.

        Largely redundant now that only content starts the window -- scaffolding
        cannot start it at all -- but kept because it is still correct for a
        retry that re-opens the upstream mid-buffer.
        """
        if not self.committed:
            self._started_at = None

    def _is_held_reasoning(self, event: str) -> bool:
        """True when only the reasoning rule is keeping this frame back."""
        return (
            not self._reasoning_commits
            and sse_is_scaffolding(event, reasoning_commits=False)
            and not sse_is_scaffolding(event)
        )

    def push(self, event: str) -> list[str]:
        self.last_push_held_reasoning = False
        if self.committed:
            return [event]
        if self._holdback_seconds <= 0 and self._holdback_chars <= 0:
            # No window at all: commit immediately rather than hold the first
            # event until some later push happens to check the clock.
            self._events.append(event)
            return self.flush()
        self._events.append(event)
        self._bytes += len(event.encode("utf-8", errors="replace"))
        self._chars += sse_visible_chars(event)
        # The window is anchored to the first frame that shows the reader
        # something, not to the first frame. Scaffolding -- message_start,
        # content_block_start, ping -- never starts it, so a stream that emits
        # a header and then stalls stays uncommitted and can still fall back to
        # the next model. Anchoring to any frame is what made those streams
        # committed with nothing shown: measured on 21 days of real traffic,
        # 393 requests ran the full 600s budget having produced only
        # scaffolding, with a fallback chain sitting unused.
        #
        # Content still gets the window rather than committing on its first
        # byte, because the window is also what makes an immediate cutoff
        # invisibly retryable -- held bytes have not reached the client yet.
        self.last_push_held_reasoning = self._is_held_reasoning(event)
        if self._started_at is None and not sse_is_scaffolding(
            event, reasoning_commits=self._reasoning_commits
        ):
            self._started_at = self._now()
        if self._bytes >= self._max_bytes:
            # The memory ceiling is not a policy and never waits on the
            # character count: a buffer this large has to be released whatever
            # the operator asked for.
            return self.flush()
        if (
            self._started_at is not None
            and self._now() - self._started_at >= self._holdback_seconds
            and self._chars >= self._holdback_chars
        ):
            return self.flush()
        return []

    def flush(self) -> list[str]:
        if self.committed:
            return []
        self.committed = True
        events = self._events
        self._events = []
        self._bytes = 0
        self._chars = 0
        self._started_at = None
        return events

    def discard(self) -> None:
        self._events = []
        self._bytes = 0
        self._chars = 0
        self._started_at = None

    @property
    def has_buffered(self) -> bool:
        return bool(self._events)


class RecoveryController:
    """Own holdback and retry counters for one provider stream lifecycle."""

    def __init__(
        self,
        *,
        provider_name: str,
        request_id: str | None,
        holdback_seconds: float = EARLY_HOLDBACK_SECONDS,
        holdback_chars: int = EARLY_HOLDBACK_CHARS,
        early_retry_attempts: int = EARLY_TRANSPARENT_TOTAL_ATTEMPTS,
        midstream_recovery_attempts: int = MIDSTREAM_RECOVERY_ATTEMPTS,
        reasoning_commits: bool = True,
    ) -> None:
        self._provider_name = provider_name
        self._request_id = request_id
        self._holdback_seconds = holdback_seconds
        self._holdback_chars = holdback_chars
        self._reasoning_commits = reasoning_commits
        # Attempts are counted as retries *after* the first try.
        self._max_early_retries = max(0, early_retry_attempts - 1)
        self._max_midstream_recoveries = max(0, midstream_recovery_attempts)
        self._holdback = self._new_holdback()
        self._early_retry_count = 0
        self._midstream_recovery_count = 0

    def _new_holdback(self) -> RecoveryHoldbackBuffer:
        """One place building the buffer, so a retry cannot lose a setting.

        The early-retry path replaces the buffer mid-stream. Constructing it
        inline there meant every field added afterwards had to be remembered
        twice, and the retry would silently revert to the default.
        """
        return RecoveryHoldbackBuffer(
            holdback_seconds=self._holdback_seconds,
            holdback_chars=self._holdback_chars,
            reasoning_commits=self._reasoning_commits,
        )

    @property
    def committed(self) -> bool:
        return self._holdback.committed

    @property
    def has_buffered(self) -> bool:
        return self._holdback.has_buffered

    @property
    def early_retries(self) -> int:
        return self._early_retry_count

    @property
    def midstream_recoveries(self) -> int:
        return self._midstream_recovery_count

    def push(self, event: str) -> list[str]:
        """Events to forward, plus an empty heartbeat while reasoning is held.

        The empty string is not SSE and never reaches a client: routing
        consumes it, treats it as "this attempt is alive and thinking", and
        keeps the attempt uncommitted. Without it a held reasoning stream is
        indistinguishable from a stream that has produced nothing at all, and
        gets the first-token deadline -- which on an eleven-model chain is the
        budget divided by eleven, far too little for a model that thinks.
        """
        released = self._holdback.push(event)
        if released or not self._holdback.last_push_held_reasoning:
            return released
        return [REASONING_HEARTBEAT]

    def upstream_opened(self) -> None:
        """Start the holdback window now that upstream bytes can actually flow."""
        self._holdback.restart_window()

    def flush(self) -> list[str]:
        return self._holdback.flush()

    def discard(self) -> None:
        self._holdback.discard()

    def flush_uncommitted(self, decision: RecoveryDecision) -> list[str]:
        if not decision.committed and decision.has_buffered:
            return self.flush()
        return []

    def advance_failure(
        self,
        error: BaseException,
        *,
        stream_opened: bool,
        generated_output: bool,
        complete_tool_salvageable: bool,
    ) -> RecoveryDecision:
        retryable = is_retryable_stream_error(error)
        committed = self._holdback.committed
        has_buffered = self._holdback.has_buffered

        if (
            retryable
            and stream_opened
            and not committed
            and not complete_tool_salvageable
            and self._early_retry_count < self._max_early_retries
        ):
            self._early_retry_count += 1
            self._holdback.discard()
            self._holdback = self._new_holdback()
            trace_event(
                stage="provider",
                event="provider.recovery.early_retry",
                source="provider",
                provider=self._provider_name,
                request_id=self._request_id,
                retry_attempt=self._early_retry_count,
                retryable=True,
            )
            return RecoveryDecision(
                action=RecoveryFailureAction.EARLY_RETRY,
                retryable=True,
                committed=False,
                has_buffered=has_buffered,
                early_retry_attempt=self._early_retry_count,
            )

        if (
            retryable
            and generated_output
            and self._midstream_recovery_count < self._max_midstream_recoveries
        ):
            self._midstream_recovery_count += 1
            return RecoveryDecision(
                action=RecoveryFailureAction.MIDSTREAM_RECOVERY,
                retryable=True,
                committed=committed,
                has_buffered=has_buffered,
                midstream_recovery_attempt=self._midstream_recovery_count,
            )

        return RecoveryDecision(
            action=RecoveryFailureAction.FINAL_ERROR,
            retryable=retryable,
            committed=committed,
            has_buffered=has_buffered,
        )


def is_retryable_stream_error(exc: BaseException) -> bool:
    """Return whether one stream failure qualifies for retry or recovery."""
    # Deferred: ~2 s to import, and no startup path asks it anything.
    import openai

    if isinstance(exc, TruncatedProviderStreamError):
        return True
    if isinstance(exc, ExecutionFailure):
        return exc.retryable
    if isinstance(exc, openai.AuthenticationError | openai.BadRequestError):
        return False
    if retryable_transient_status(exc) is not None:
        return True
    return isinstance(
        exc,
        (
            TimeoutError,
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.NetworkError,
            openai.APITimeoutError,
            openai.APIConnectionError,
        ),
    )
