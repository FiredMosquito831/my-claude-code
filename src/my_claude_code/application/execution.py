"""Provider execution shared by inbound API adapters."""

import asyncio
import contextlib
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast

from loguru import logger

from my_claude_code.config.constants import (
    CREDENTIAL_PROBE_MAX_TOKENS,
    CREDENTIAL_PROBE_TIMEOUT_SECONDS_DEFAULT,
    FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT,
    FALLBACK_COOLDOWN_STEP_OVER_FLOOR_DEFAULT,
    FALLBACK_END_CLEANLY_AFTER_COMMIT_DEFAULT,
    FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT,
    FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT,
    FALLBACK_RESUME_AFTER_COMMIT_DEFAULT,
    FALLBACK_STALL_TIMEOUT_DEFAULT,
    FALLBACK_TOTAL_TIMEOUT_DEFAULT,
    PROVIDER_RETRY_ATTEMPTS_DEFAULT,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    ContentBlockText,
    Message,
    MessagesRequest,
    SystemContent,
    Tool,
    anthropic_request_snapshot,
    get_token_count,
)
from my_claude_code.core.anthropic.stream_contracts import (
    REASONING_HEARTBEAT,
    sse_carries_content,
)
from my_claude_code.core.anthropic.streaming.recovery import CONTINUATION_NUDGE
from my_claude_code.core.anthropic.streaming.splice import (
    ContinuationSplicer,
    SpliceFreeze,
    SpliceState,
    close_frames,
    freeze_stream,
)
from my_claude_code.core.anthropic.streaming.truncation import (
    CommittedStreamTracker,
    StreamTruncation,
)
from my_claude_code.core.credential_attribution import (
    current_credential,
    record_credential,
)
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    failure_kind_name,
    find_execution_failure,
    parse_failure_kinds,
)
from my_claude_code.core.trace import (
    close_stream_input,
    trace_event,
    traced_async_stream,
)
from my_claude_code.core.upstream_ladder import (
    paused_ladder,
    record_credential_decision,
    record_upstream_try,
)
from my_claude_code.core.waiting_clock import waited_seconds

from .deadline_hints import limit_hint
from .errors import ModelRateLimited
from .ports import PooledCredentialPort, ProviderPort, ProviderResolver
from .route_health import BenchReason, RouteHealthRegistry
from .routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
    apply_output_token_budget,
    apply_reasoning_budget,
)

TokenCounter = Callable[
    [list[Message], str | list[SystemContent] | None, list[Tool] | None],
    int,
]
WireApi = Literal["messages", "responses"]
AttemptObserver = Callable[[RoutedMessagesRequest, int], None]


@dataclass(frozen=True, slots=True)
class RouteAttemptRecord:
    """The verdict on one model of a chain, for the request log.

    ``skipped`` covers a model the chain never actually tried -- benched by
    recent failures, or reached with no time left. Those are the attempts that
    were invisible before: a three-model chain that only ever ran one looked
    exactly like a one-model route, so "the fallback did not help" and "the
    fallback was never asked" could not be told apart.
    """

    attempt: int
    provider_id: str
    model_ref: str
    outcome: Literal["succeeded", "failed", "skipped"]
    error_kind: str | None = None
    error_message: str | None = None
    duration_ms: float | None = None
    #: For a ``skipped``/``ejected`` row, the registry's account of why -- mode,
    #: how many failures, of what kind, and how much of the bench is left. Kept
    #: structured rather than only as prose so the request detail can render it
    #: and an export can carry it.
    bench: dict[str, object] | None = None
    #: Set when a stream that had already reached the client failed and was
    #: ended as a valid message instead of an error: how much the reader was
    #: left with, what actually went wrong, and what stop reason the client was
    #: told. ``ended_cleanly`` is false for the one case that still errors, a
    #: half-written tool call.
    truncated: dict[str, object] | None = None
    #: Set on the attempt that finished a message another model had started:
    #: which model stalled, how far it had got, and whether its successor's
    #: output was actually usable. The row that *stalled* keeps its own
    #: failure verdict, so the pair reads as one story.
    continuation: dict[str, object] | None = None


# Reports what became of one attempt, as opposed to announcing that it started.
# The two are separate because the request row wants the model in flight (so a
# failed chain still names the last model it reached) while the attempt log
# wants the verdict, which is only known afterwards. Declared after the record
# it names: a type alias is evaluated eagerly, lazy annotations notwithstanding.
AttemptResultObserver = Callable[[RouteAttemptRecord], None]


@dataclass(frozen=True, slots=True)
class RouteExecutionPolicy:
    """Wall-clock limits deciding when an attempt stops being worth waiting for.

    A provider that accepts a request and then produces nothing is
    indistinguishable, to a caller with no deadline, from one that is thinking
    hard. The only thing that ever ended such an attempt was the transport read
    timeout -- minutes later, after which the stream was retried against the
    same stalled model. Both numbers below exist so a stall is declared while
    the chain can still do something about it.

    ``first_token_timeout`` is the one that matters for fallback: nothing has
    reached the client before the first chunk, so abandoning the attempt there
    is invisible and the next model simply answers instead.

    ``total_timeout`` is a backstop for the case no chain can rescue -- a stream
    that committed and then stalled. It cannot fall back, but it can stop.

    Either value at or below zero disables that limit.
    """

    first_token_timeout: float = FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT
    total_timeout: float = FALLBACK_TOTAL_TIMEOUT_DEFAULT
    # Smallest value the equal-share division of ``total_timeout`` may hand an
    # attempt. Without it the share alone decides the first-token allowance,
    # and on a long chain it silently undercuts ``first_token_timeout``.
    # 0 restores the pure share.
    attempt_share_floor: float = FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT
    # How long a stream that has already produced output may then say nothing.
    # The first-token deadline stops applying the moment content appears, so
    # without this the only thing left bounding a stalled stream is the whole
    # request budget: 106 requests held one for the full 600s after producing
    # real output, on 21 days of measured traffic.
    stall_timeout: float = FALLBACK_STALL_TIMEOUT_DEFAULT
    # How long an attempt may think without starting an answer, once the
    # provider has told us it is holding reasoning back. Deliberately not the
    # attempt's share of the budget: that share is sized for a model which has
    # shown nothing at all, and dividing 600s by an eleven-model chain leaves
    # 54s, which measured against real traffic would divert 1,387 successful
    # reasoning requests to rescue 44 reasoning-only failures. Every one of the
    # 499 budget exhaustions ran the *full* budget, so any allowance under it
    # rescues all of them, and 98% of the slow successes had begun answering
    # inside 300s -- a flat allowance separates the two where the share cannot.
    reasoning_answer_timeout: float = FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT
    # Failure kinds that end the route rather than moving to the next model.
    # A malformed request is the caller's, not the model's: the same body
    # fails identically everywhere, so walking a three-model chain buys three
    # round trips to the same 400. Empty means fall back on everything.
    # A context overflow is deliberately *not* here: it is also a 400, but it
    # is a property of this model's window, which is exactly what a chain of
    # differently-sized models can answer. It classifies as CONTEXT_LENGTH.
    skip_kinds: frozenset[FailureKind] = frozenset({FailureKind.INVALID_REQUEST})
    # Stepping a model over costs the chain a slot, so a wait worth having is
    # one that outlives the hop it saves: sub-second remainders logged as
    # "cooldown for 0s" three requests running were never worth routing around
    # in the first place. 0 steps over any cooldown at all.
    cooldown_step_over_floor: float = FALLBACK_COOLDOWN_STEP_OVER_FLOOR_DEFAULT
    # What a stream that has already reached the client does when it then
    # fails. No chain can rescue it -- the reader has seen the first model's
    # words -- but the protocol can still end the message: close the open
    # block, send a stop reason that means "cut short", and stop. False
    # restores the error the client used to receive after a partial answer.
    end_cleanly_after_commit: bool = FALLBACK_END_CLEANLY_AFTER_COMMIT_DEFAULT
    # Whether a stream that has already reached the client may be *continued*
    # by the next model on the route rather than only ended. The words already
    # sent stay exactly as they are and the next model is asked to carry on
    # from them. Every way this can fail -- no model left, a model that says
    # nothing, a model that starts the answer over -- lands on
    # ``end_cleanly_after_commit``'s truncated message, never on an error, so
    # turning it off can only ever produce a shorter answer, never a worse
    # failure. The one state it does not attempt is a half-written tool call,
    # which cannot be handed to another model honestly.
    resume_after_commit: bool = FALLBACK_RESUME_AFTER_COMMIT_DEFAULT
    # Tries a rate-limited model still gets when there is nowhere to route it.
    # ``PROVIDER_RETRY_ATTEMPTS``, read here rather than in the limiter,
    # because only the executor knows whether the chain holds an alternative.
    # With one key and one configured model a 429 has nothing to route around,
    # so the ladder is still the only thing that can serve the request -- and
    # that is the case this design must not break. It is spent only after the
    # chain, the same-provider preference and the probe have all come up
    # empty, which on a real chain is never.
    rate_limit_attempts: int = PROVIDER_RETRY_ATTEMPTS_DEFAULT


# The wait is scheduled for exactly the remaining stall budget, so by the time
# it elapses the measured gap lands a hair under the limit. Without a tolerance
# the decision below would attribute every stall to the request budget instead.
_STALL_DECISION_TOLERANCE = 0.05


class _DeadlineExceeded(Exception):
    """Internal marker: our own wait elapsed, not an upstream timeout."""


@dataclass(frozen=True, slots=True)
class _RateLimitRoute:
    """Where a routed-around 429 sends the request next.

    Exactly one of the two is ever set. ``prefer_provider`` moves the other
    models on the rate-limited provider to the front of what is left;
    ``retry_same_position`` re-enters the pool for the same model, which now
    picks a different key because the probe proved the first one was the
    problem. Neither set is "carry on down the chain in order", which is also
    what an inconclusive probe means.
    """

    prefer_provider: str | None = None
    retry_same_position: bool = False


async def _next_chunk(
    chunks: AsyncIterator[str], timeout: float | None, deadline: float | None = None
) -> str:
    """Return the next chunk, or raise ``_DeadlineExceeded`` if ``timeout`` elapses.

    ``asyncio.wait`` rather than ``wait_for`` so a ``TimeoutError`` raised *by
    the provider* stays distinguishable from this deadline elapsing. The two
    mean different things -- one is the upstream giving up, the other is us
    deciding not to keep waiting -- and only the second should be reported as a
    routing deadline.

    The wait re-arms for any seconds the provider spent asleep in its own
    backoff or behind its limiter. Those are MCC's seconds, not the model's,
    and a first-token deadline that expires during them reports a model that
    never had the time it was given: one measured request spent 51 of its 57
    seconds asleep between retries. The extension is bounded by real elapsed
    sleep -- it can only ever hand back time that provably passed with no
    upstream listening -- so it cannot lengthen a wait on a silent model.

    ``deadline`` is the whole request's budget, and it clamps the re-arm:
    a sleep may move the first-token or stall limit, never
    ``FALLBACK_TOTAL_TIMEOUT``.
    """
    if timeout is None:
        return await anext(chunks)
    pending = asyncio.ensure_future(anext(chunks))
    deadline_at = time.monotonic() + timeout
    if deadline is not None:
        deadline_at = min(deadline_at, deadline)
    credited = waited_seconds()
    while True:
        done, _still_running = await asyncio.wait(
            {pending}, timeout=max(0.0, deadline_at - time.monotonic())
        )
        if done:
            return pending.result()
        spent = waited_seconds()
        if spent > credited:
            deadline_at += spent - credited
            credited = spent
            if deadline is not None:
                deadline_at = min(deadline_at, deadline)
            if deadline_at > time.monotonic():
                continue
        pending.cancel()
        # Let the cancellation land before the caller closes the stream, so a
        # half-cancelled task cannot outlive the attempt that owns it.
        await asyncio.gather(pending, return_exceptions=True)
        raise _DeadlineExceeded


def _timeout_env_var(
    *, first_token: bool, stalled: bool, reasoning_only: bool, share_bound: bool
) -> str:
    """The setting that actually ended this attempt, named for the reader.

    The same order the message wording is chosen in, so the knob named is
    always the knob whose number is printed alongside it. ``share_bound`` is
    the one case where the two come apart: a silent attempt cut short by its
    slice of the request budget rather than by the first-token deadline was
    ended by the floor/budget pair, and sending that reader to the first-token
    box means raising a number that never fires.
    """
    if reasoning_only:
        return "FALLBACK_REASONING_ANSWER_TIMEOUT"
    if first_token:
        return (
            "FALLBACK_ATTEMPT_SHARE_FLOOR"
            if share_bound
            else "FALLBACK_FIRST_TOKEN_TIMEOUT"
        )
    if stalled:
        return "FALLBACK_STALL_TIMEOUT"
    return "FALLBACK_TOTAL_TIMEOUT"


def _timeout_failure(
    model_ref: str,
    *,
    seconds: float,
    first_token: bool,
    stalled: bool = False,
    reasoning_only: bool = False,
    share_bound: bool = False,
) -> ExecutionFailure:
    if reasoning_only:
        reason = f"produced only reasoning for {seconds:g}s without answering"
    elif first_token:
        reason = f"produced no output within {seconds:g}s"
    elif stalled:
        # Distinct from the budget message on purpose: "exceeded the 600s
        # request budget" on a stream abandoned after 120s of silence sends
        # the reader to the wrong setting entirely.
        reason = f"stopped producing output for {seconds:g}s"
    else:
        reason = f"exceeded the {seconds:g}s request budget"
    # The hint travels with the message, so it survives every path the message
    # takes: the API error body, the SSE error frame, and the attempt row in
    # the request log -- including the committed-stream truncation path, which
    # records this same failure rather than raising it.
    hint = limit_hint(
        _timeout_env_var(
            first_token=first_token,
            stalled=stalled,
            reasoning_only=reasoning_only,
            share_bound=share_bound,
        )
    )
    return ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=504,
        message=f"Provider '{model_ref}' {reason}.{hint}",
        retryable=True,
    )


def _continuation_attempt(
    routed: RoutedMessagesRequest, freeze: SpliceFreeze
) -> RoutedMessagesRequest:
    """The next model's request, carrying the answer written so far.

    Assistant turn, then user turn -- deliberately, and this is the one shape
    that works. A request whose *last* message is the assistant's is a prefill,
    which Anthropic rejects with a 400 on Claude 4.6 and later and which only
    three hosts market-wide accept at all. Ending on the user turn makes this
    an ordinary conversation that every dialect's converter already serialises
    correctly, sets no ``prefix`` flag, and needs no provider-specific base
    URL. Measured across thirteen live model/host pairs, it is also the only
    shape that produced a clean continuation rather than a restart.

    Text only. A synthesized ``thinking`` block would reach Anthropic without
    the provider-cryptographic signature it cannot have, and on a think-tags
    host it would be inlined into model-visible prose; neither is a thing the
    reader asked for.

    ``tools`` are deliberately left in place. The same-provider recovery path
    strips them because it only ever wants prose, but a continuation may
    legitimately need to call a tool, and dropping them would leave the model
    narrating the call it should have made.
    """
    prefix = freeze.prefix_text.rstrip()
    messages = [
        *routed.request.messages,
        Message(role="assistant", content=[ContentBlockText(type="text", text=prefix)]),
        Message(role="user", content=CONTINUATION_NUDGE),
    ]
    return replace(
        routed, request=routed.request.model_copy(update={"messages": messages})
    )


def _cooldown_failure(model_ref: str, seconds: float) -> ExecutionFailure:
    """The verdict recorded for a model the chain stepped over while limited."""
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message=(
            f"Provider for '{model_ref}' is in rate-limit cooldown for "
            f"{seconds:.0f}s.{limit_hint('RATE_LIMIT_COOLDOWN_SECONDS')}"
        ),
        retryable=True,
    )


class _AttemptLedger:
    """Collects one verdict per model on the route, in chain order.

    Built from the plan rather than from what ran, so a model the chain never
    reached still gets a row saying why. Publishing once at the end keeps the
    request log's writer out of the streaming path -- an attempt verdict is
    never worth a database round trip while a client is waiting for tokens.
    """

    def __init__(
        self,
        model_refs: tuple[str, ...],
        attempts: tuple[RoutedMessagesRequest, ...],
        observer: AttemptResultObserver | None,
    ) -> None:
        self._observer = observer
        self._records: dict[int, RouteAttemptRecord] = {}
        self._started: dict[int, float] = {}
        self._current: int | None = None
        for index, ref in enumerate(model_refs):
            self._records[index] = RouteAttemptRecord(
                attempt=index,
                provider_id=(
                    attempts[index].resolved.provider_id
                    if index < len(attempts)
                    else ""
                ),
                model_ref=ref,
                outcome="skipped",
                error_message="never reached",
            )

    def mark_benched(
        self,
        order: tuple[int, ...],
        why: Callable[[str], BenchReason | None] | None = None,
    ) -> None:
        """Record models the health registry removed before the request began.

        The row used to read "benched after recent consecutive failures" for
        every skip, in a build whose default mode has been rate-based since
        5.61.0 -- so the one line the reader got was, for most installs, about
        a counter that was never consulted. ``why`` is the registry's own
        account, so the sentence names the mode, the evidence and the time
        left, and the structured form rides along for the modal.
        """
        usable = set(order)
        for index, record in self._records.items():
            if index in usable:
                continue
            reason = None if why is None else why(record.model_ref)
            self._set(
                index,
                outcome="skipped",
                error_kind="ejected",
                error_message=(
                    reason.sentence()
                    if reason is not None
                    else "benched after recent failures"
                ),
                bench=None if reason is None else reason.as_dict(),
            )

    def start(self, index: int) -> None:
        self._current = index
        self._started[index] = time.monotonic()

    def _elapsed_ms(self, index: int) -> float | None:
        started = self._started.get(index)
        return None if started is None else (time.monotonic() - started) * 1000.0

    def _set(
        self,
        index: int,
        *,
        outcome: Literal["succeeded", "failed", "skipped"],
        error_kind: str | None = None,
        error_message: str | None = None,
        duration_ms: float | None = None,
        bench: dict[str, object] | None = None,
    ) -> None:
        current = self._records.get(index)
        if current is None:
            return
        self._records[index] = RouteAttemptRecord(
            attempt=current.attempt,
            provider_id=current.provider_id,
            model_ref=current.model_ref,
            outcome=outcome,
            error_kind=error_kind,
            error_message=error_message,
            duration_ms=duration_ms,
            bench=bench,
            truncated=current.truncated,
            continuation=current.continuation,
        )

    def truncated_after_commit(self, index: int, truncation: StreamTruncation) -> None:
        """Attach what became of a stream the client had already started reading.

        Written beside the verdict rather than into it: the attempt still
        failed, with the same kind and the same message it has always had, and
        the reader of the request log should not have to infer a stall from a
        message that now ends in ``message_stop``.
        """
        current = self._records.get(index)
        if current is None:
            return
        self._records[index] = replace(current, truncated=truncation.as_params())

    def continued(self, index: int, continuation: dict[str, object]) -> None:
        """Attach the account of a message this attempt inherited half-written.

        Beside the verdict for the same reason ``truncated_after_commit`` is:
        the attempt succeeded or failed on its own terms, and how it came to be
        finishing someone else's sentence is a separate fact about it.
        """
        current = self._records.get(index)
        if current is None:
            return
        self._records[index] = replace(current, continuation=continuation)

    def succeeded(self, index: int) -> None:
        self._set(
            index,
            outcome="succeeded",
            error_kind=None,
            error_message=None,
            duration_ms=self._elapsed_ms(index),
        )

    def failed(self, index: int, exc: BaseException) -> None:
        self._set(
            index,
            outcome="failed",
            error_kind=failure_kind_name(exc),
            error_message=safe_exception_message(exc),
            duration_ms=self._elapsed_ms(index),
        )

    def interrupted(self) -> None:
        """Record the attempt in flight when the client went away.

        Cancellation used to drop the whole ledger on the floor: publish
        ran only on success, failure and route-end, so a disconnect left
        every verdict the chain had reached invisible in the request log.
        """
        index = self._current
        if index is None:
            return
        self._set(
            index,
            outcome="failed",
            error_kind="interrupted",
            error_message="client cancelled before the stream finished",
            duration_ms=self._elapsed_ms(index),
        )

    def unreachable_after(self, index: int, exc: BaseException) -> None:
        """Mark every model behind ``index`` as never worth trying.

        A route ended by the failure kind rather than by exhaustion: the models
        behind it were not skipped for time or health, they were skipped
        because nothing they could do would change the answer. Saying which is
        the difference between "your chain did not help" and "your chain was
        correctly not used".
        """
        reason = f"not tried: a {failure_kind_name(exc)} failure ends the route"
        for other in self._records:
            if other > index:
                self._set(
                    other,
                    outcome="skipped",
                    error_kind="route_ended",
                    error_message=reason,
                )

    def in_cooldown(self, index: int, seconds: float) -> None:
        """Record a model stepped over because its provider is rate-limited."""
        self._set(
            index,
            outcome="skipped",
            error_kind="cooldown",
            error_message=(
                f"provider in rate-limit cooldown for {seconds:.0f}s;"
                " a later model was tried instead"
            ),
        )

    def out_of_time(self, index: int) -> None:
        self._set(
            index,
            outcome="skipped",
            error_kind="budget_exhausted",
            error_message="request budget spent before this model was tried",
        )

    def publish(self) -> None:
        if self._observer is None:
            return
        for index in sorted(self._records):
            self._observer(self._records[index])
        self._observer = None


class ProviderExecutor:
    """Resolve a provider and execute one routed Anthropic Messages stream."""

    def __init__(
        self,
        provider_resolver: ProviderResolver,
        *,
        token_counter: TokenCounter = get_token_count,
        generation_id: int | None = None,
        log_raw_payloads: bool = False,
        policy: RouteExecutionPolicy | None = None,
        health: RouteHealthRegistry | None = None,
        retry_first: str = "skip",
        provider_lookup: Callable[[str], float | None] | None = None,
        # Which failure kinds are eligible for a retry_once on the primary.
        # Auth/invalid-request never retry (won't change on retry).
        retry_once_kinds: frozenset[FailureKind] = frozenset(
            {
                FailureKind.RATE_LIMIT,
                FailureKind.OVERLOADED,
                FailureKind.TIMEOUT,
                FailureKind.UPSTREAM,
                FailureKind.UNAVAILABLE,
            }
        ),
    ) -> None:
        self._provider_resolver = provider_resolver
        self._token_counter = token_counter
        self._generation_id = generation_id
        self._log_raw_payloads = log_raw_payloads
        self._policy = policy or RouteExecutionPolicy()
        self._health = health or RouteHealthRegistry()
        self._retry_first = retry_first
        self._retry_once_kinds = retry_once_kinds
        self._provider_lookup = provider_lookup

    def first_usable_attempt(self, plan: RoutedMessagesPlan) -> RoutedMessagesRequest:
        """The attempt :meth:`stream` would actually reach first.

        Exposed because a locally answered request still has to name the model
        that *would* have served it: a client probing for a silent model
        substitution learns nothing if the answer names a primary that recent
        failures have benched out of the chain. Uses the same ordering and the
        same provider lookup the executor uses, so the two cannot drift.

        ``usable_indexes`` returns the whole chain when every candidate is
        benched -- a degraded route is still a route -- so an empty order is
        not reachable; index 0 is the honest answer if it ever were.
        """
        order = self._health.usable_indexes(
            plan.model_refs(), provider_lookup=self._provider_lookup
        )
        return plan.attempts[order[0] if order else 0]

    def stream(
        self,
        plan: RoutedMessagesPlan,
        *,
        wire_api: WireApi,
        raw_log_label: str,
        raw_log_payload: object,
        request_id: str,
        on_attempt: AttemptObserver | None = None,
        on_attempt_result: AttemptResultObserver | None = None,
    ) -> AsyncIterator[str]:
        """Preflight synchronously, then return the traced provider stream.

        Attempts are tried in order until one commits to the wire; past that
        point a failure propagates instead of moving to the next model, because
        swapping models mid-stream would splice two different completions into
        one answer.

        What "committed" means depends on the client. A streaming client sees
        each chunk as it is produced, so the first chunk commits. A
        non-streaming client is served one aggregated JSON message and sees
        nothing until the stream ends -- so nothing is committed until the
        attempt completes, and a failure at any point can still fall back to
        the next model with the client none the wiser.

        A model that has just failed repeatedly is skipped outright, so a
        request does not re-pay its timeout on the way to a healthy fallback.
        """
        attempts = plan.attempts
        buffer_until_complete = not plan.primary.request.stream
        failures: list[BaseException] = []
        order = self._health.usable_indexes(
            plan.model_refs(), provider_lookup=self._provider_lookup
        )
        if len(order) < len(attempts):
            logger.info(
                "MODEL CHAIN: skipping {} recently-failing model(s) on this route",
                len(attempts) - len(order),
            )
        deadline = (
            time.monotonic() + self._policy.total_timeout
            if self._policy.total_timeout > 0
            else None
        )
        # Every model on the route starts as "never reached". Each one that is
        # tried, benched or timed out overwrites its own entry, so what is left
        # at the end is the whole chain's story rather than only the winner's.
        ledger = _AttemptLedger(plan.model_refs(), attempts, on_attempt_result)
        ledger.mark_benched(order, self._health.why)
        prepared = self._prepare_from(
            attempts,
            order,
            0,
            failures,
            request_id=request_id,
            on_attempt=on_attempt,
            deadline=deadline,
            ledger=ledger,
        )
        if prepared is None:
            # Every attempt failed before opening a stream. Raising here keeps
            # the caller's existing synchronous error surface intact.
            ledger.publish()
            raise failures[-1]

        trace_event(
            stage="ingress",
            event=(
                "my_claude_code.api.responses.request.received"
                if wire_api == "responses"
                else "my_claude_code.api.request.received"
            ),
            source="api",
            message_count=len(plan.primary.request.messages),
            snapshot=anthropic_request_snapshot(plan.primary.request),
            request_id=request_id,
        )

        if self._log_raw_payloads:
            logger.debug(f"{raw_log_label} [{{}}]: {{}}", request_id, raw_log_payload)

        # Counted once and used twice: the input usage the client is told
        # about, and how much of the model's context window its answer still
        # has to fit in. Deliberately after preflight -- a request that cannot
        # be converted at all should not pay for tokenization first.
        input_tokens = self._token_counter(
            plan.primary.request.messages,
            plan.primary.request.system,
            plan.primary.request.tools,
        )
        # Every attempt is bound to its own model's real output capacity, not
        # just the primary: a fallback on a 16,384-token model must not inherit
        # a budget sized for the 262,144-token model above it. Rebound here
        # rather than in routing because the context-headroom half of the
        # decision needs the prompt's token count.
        # Output budget first, then the thinking budget: the reasoning
        # reconciliation has to see the max_tokens that will actually be
        # sent, or the two numbers disagree and the provider 400s. Since
        # 6.8.0 the order is load-bearing in a second way -- the output
        # budget *widens* a thinking attempt's allowance to the model's own
        # published limit before clamping it, so the rung's ratio and the
        # answer reserve are both priced from the real allowance rather than
        # from a client's answer-sized ask.
        attempts = tuple(
            apply_reasoning_budget(apply_output_token_budget(attempt, input_tokens))
            for attempt in attempts
        )
        announced = order[prepared[0]]
        if (
            on_attempt is not None
            and attempts[announced].output_widened_from is not None
        ):
            # ``_prepare_from`` above announced this attempt before either
            # budget existed -- it has to run first, because preflight stays
            # ahead of token counting (WORKING-NOTES 56). Everything the
            # observer records is the same on both objects except the one
            # thing the budget just decided, so the announcement is repeated
            # only when there is something new to say. Re-announcing every
            # attempt would double the chain's announcement sequence, which
            # is itself a contract ("announced before it is tried").
            on_attempt(attempts[announced], announced)

        # Per-request, not per-executor. ProviderExecutor is constructed once
        # per handler and shared by every request in the process, so holding
        # this on self meant the first request to retry position 0 consumed
        # the retry-once budget for the whole process lifetime and no later
        # request ever retried its primary again.
        retried_positions: set[int] = set()
        # One diagnostic probe per chain position per request. A verdict is a
        # fact about one instant and is never cached across requests, but a
        # position that keeps meeting 429s must not keep asking: with three
        # keys that would be three probes for one answer.
        probed_positions: set[int] = set()
        # Tries already spent on a rate-limited model that had nowhere to go.
        rate_limit_attempts: dict[int, int] = {}

        async def provider_body() -> AsyncIterator[str]:
            position, provider = prepared
            # Hoisted out of the loop, because they belong to the *message*
            # rather than to an attempt. Once anything has reached the client
            # every later attempt is bound by it: a continuation must never
            # believe it may still fall back invisibly, and the tracker has to
            # keep following what the reader has actually been shown across
            # the seam so a second failure can be ended -- or continued --
            # against the whole message rather than only its last third.
            committed = False
            tracker = CommittedStreamTracker()
            splicer: ContinuationSplicer | None = None
            resume: SpliceFreeze | None = None
            resumed_from: str | None = None
            while True:
                index = order[position]
                routed = attempts[index]
                attempt_input_tokens = input_tokens
                if resume is not None:
                    routed = _continuation_attempt(routed, resume)
                    # A different body deserves a different count: this one
                    # carries the answer so far and the nudge, and the number
                    # the client was told about describes neither. One extra
                    # tokenization per resume, paid only on the rescue path.
                    attempt_input_tokens = self._token_counter(
                        routed.request.messages,
                        routed.request.system,
                        routed.request.tools,
                    )
                model_ref = routed.resolved.provider_model_ref
                self._trace_route(
                    routed,
                    wire_api=wire_api,
                    request_id=request_id,
                    attempt=index,
                    attempt_count=len(attempts),
                )

                # No attempt may spend the whole budget before producing
                # anything: the share is what guarantees the models below this
                # one actually get a turn. It bounds time-to-first-content
                # only -- once content is flowing the attempt owns the rest of
                # the budget, because cutting a working stream to hand over to
                # another model is worse than letting it finish.
                attempt_deadline = self._attempt_deadline(
                    deadline, len(order) - position
                )
                attempt_budget = (
                    None
                    if attempt_deadline is None
                    else max(0.0, attempt_deadline - time.monotonic())
                )

                provider_stream: AsyncIterator[str] | None = None
                uncommitted_failure: Exception | None = None
                committed_failure: Exception | None = None
                truncation: StreamTruncation | None = None
                held: list[str] = []
                try:
                    # Baseline attribution for single-credential providers. A
                    # rotating provider overwrites this with the credential it
                    # actually picks for this request.
                    record_credential(0, provider.credential_label)
                    provider_stream = provider.stream_response(
                        routed.request,
                        input_tokens=attempt_input_tokens,
                        request_id=request_id,
                        reasoning=routed.reasoning,
                    )
                    chunks = provider_stream.__aiter__()
                    seen_chunk = False
                    reasoning_since: float | None = None
                    last_progress = time.monotonic()
                    while True:
                        try:
                            chunk = await _next_chunk(
                                chunks,
                                self._chunk_timeout(
                                    seen_chunk,
                                    deadline,
                                    attempt_deadline,
                                    last_progress,
                                    reasoning_since,
                                ),
                                deadline,
                            )
                        except StopAsyncIteration:
                            break
                        except _DeadlineExceeded as exc:
                            raise self._deadline_reached(
                                model_ref,
                                seen_chunk=seen_chunk,
                                request_id=request_id,
                                attempt_budget=attempt_budget,
                                last_progress=last_progress,
                                reasoning_since=reasoning_since,
                            ) from exc
                        if chunk == REASONING_HEARTBEAT:
                            # The provider consumed an upstream fragment and
                            # has nothing to forward yet -- reasoning held back
                            # before the answer, or tool arguments buffered
                            # until their JSON parses. Nothing is committed,
                            # but the attempt is demonstrably working rather
                            # than silent, so it earns the answer allowance
                            # instead of the first-token share, and it counts
                            # as progress: measuring the buffer instead of the
                            # model is how a streaming tool call got killed as
                            # a stall. A dead stream sends no heartbeat, so
                            # the guard that ends one is untouched.
                            if reasoning_since is None:
                                reasoning_since = time.monotonic()
                            last_progress = time.monotonic()
                            continue
                        seen_chunk = True
                        # Only a chunk that moves the answer forward counts as
                        # progress. A keepalive resetting this clock is exactly
                        # how a dead stream would hold a request forever, which
                        # is the failure this guard exists to end.
                        if sse_carries_content(chunk):
                            last_progress = time.monotonic()
                        if buffer_until_complete:
                            held.append(chunk)
                            continue
                        if splicer is None:
                            committed = True
                            tracker.observe(chunk)
                            yield chunk
                            continue
                        # A continuation's frames are not the client's frames:
                        # its indices collide with blocks already on screen and
                        # its opening is held back until it has proved it is
                        # continuing the answer rather than starting it again.
                        for frame in splicer.rewrite(chunk):
                            tracker.observe(frame)
                            yield frame
                except Exception as exc:
                    if committed:
                        committed_failure = exc
                    else:
                        uncommitted_failure = exc
                finally:
                    if provider_stream is not None:
                        await close_stream_input(
                            provider_stream,
                            owner="provider_executor",
                            source="api",
                            preserved_error=sys.exception(),
                        )
                if committed_failure is not None:
                    # The reader has this model's words on screen and no other
                    # model can un-send them -- but the next model on the route
                    # can be asked to carry on from them. The route order, the
                    # bench, the cooldown step-over and the attempt's share of
                    # the budget are all decided by the same ``_prepare_from``
                    # a pre-commit fallback calls, so a resume is one more turn
                    # of this loop rather than a second routing policy.
                    freeze = (
                        freeze_stream(tracker)
                        if self._can_resume(committed_failure, tracker, wire_api)
                        else None
                    )
                    following = (
                        None
                        if freeze is None
                        else self._prepare_from(
                            attempts,
                            order,
                            position + 1,
                            failures,
                            request_id=request_id,
                            on_attempt=on_attempt,
                            deadline=deadline,
                            ledger=ledger,
                        )
                    )
                    if freeze is not None and following is not None:
                        keep_text_open = freeze.state is SpliceState.IN_TEXT
                        # After the upstream stream is closed, so the frames
                        # that shape the seam are never interleaved with a
                        # half-open connection to the model that abandoned it.
                        for frame in close_frames(
                            freeze, keep_text_open=keep_text_open
                        ):
                            tracker.observe(frame)
                            yield frame
                        ledger.failed(index, committed_failure)
                        # The same single charge the committed-truncation path
                        # makes, for the same failure. ``_prepare_from`` charges
                        # only the models it could not even start.
                        self._charge_failure(model_ref, committed_failure)
                        self._trace_fallback(
                            routed,
                            committed_failure,
                            request_id=request_id,
                            attempt=index,
                        )
                        logger.warning(
                            "MODEL CONTINUED: '{}' died after {} chars ({});"
                            " asking the next model to finish the answer",
                            model_ref,
                            freeze.chars,
                            failure_kind_name(committed_failure),
                        )
                        resumed_from = model_ref
                        position, provider = following
                        resume = freeze
                        splicer = ContinuationSplicer(
                            freeze, keep_text_open=keep_text_open
                        )
                        continue
                    # Nothing left to continue on, or nothing safe to continue
                    # from: end the message on what was already sent. A valid
                    # short answer beats an API error printed under a
                    # half-written one, and it is where every unusable
                    # continuation lands too.
                    truncation = self._truncate_after_commit(
                        tracker, committed_failure, wire_api
                    )
                    if splicer is not None:
                        ledger.continued(
                            index,
                            splicer.as_params(resumed_from_model=resumed_from or ""),
                        )
                    if truncation is None or not truncation.ended_cleanly:
                        # The attempt still ended in a failure and the log
                        # should say so rather than leaving it as "never
                        # reached".
                        ledger.failed(index, committed_failure)
                        if truncation is not None:
                            ledger.truncated_after_commit(index, truncation)
                        ledger.unreachable_after(index, committed_failure)
                        ledger.publish()
                        raise committed_failure
                    for frame in truncation.frames:
                        tracker.observe(frame)
                        yield frame
                    ledger.failed(index, committed_failure)
                    ledger.truncated_after_commit(index, truncation)
                    ledger.unreachable_after(index, committed_failure)
                    # The uncommitted path has always charged the model for a
                    # failure here; the committed path never reached that line
                    # because it raised first. It fails for exactly the same
                    # reason, so it counts exactly once, the same way.
                    self._charge_failure(model_ref, committed_failure)
                    ledger.publish()
                    return
                if uncommitted_failure is None:
                    # Empty unless this attempt was held back for a
                    # non-streaming client; a failed attempt's chunks are
                    # dropped with it and never reach the aggregator.
                    for chunk in held:
                        yield chunk
                    if splicer is not None:
                        # The continuation's own ``message_delta`` and
                        # ``message_stop`` were dropped by ``rewrite``; exactly
                        # one pair ends the message, and it is this one.
                        continuation_params = splicer.as_params(
                            resumed_from_model=resumed_from or ""
                        )
                        for frame in splicer.finish():
                            tracker.observe(frame)
                            yield frame
                        ledger.continued(index, continuation_params)
                    self._health.record_success(model_ref)
                    ledger.succeeded(index)
                    ledger.publish()
                    return

                # A routed-around 429 arrives wrapped, because the pool has to
                # say two things at once: what happened, and that it chose not
                # to rotate. Only the routing decision reads the wrapper --
                # everything that records, charges or raises sees the
                # provider's own ``rate_limit`` failure, exactly as before, so
                # the request log, ``FALLBACK_SKIP_KINDS`` and the ejection
                # registry are untouched by the new class.
                routed_around: ModelRateLimited | None = None
                if isinstance(uncommitted_failure, ModelRateLimited):
                    routed_around = uncommitted_failure
                    uncommitted_failure = routed_around.failure

                # The failed stream is closed by now, so the next attempt never
                # runs alongside a half-open connection to the previous one.
                failures.append(uncommitted_failure)
                ledger.failed(index, uncommitted_failure)
                # Pass the kind through: it is what decides whether this
                # failure counts against the model at all. A timeout, a 429 or
                # a prompt no model could hold is the request's problem, and
                # counting it is what let one oversized request eject a whole
                # chain. The status rides along so the skipped row can name
                # what the last failure actually was.
                # failure_kind() rather than .kind: an attempt can fail with a
                # raw exception (httpx TimeoutError, RuntimeError from a
                # construction error) that never reached provider
                # classification, and those have no .kind at all -- and now
                # never bench anything either.
                self._charge_failure(model_ref, uncommitted_failure)
                if self._ends_the_route(uncommitted_failure):
                    ledger.unreachable_after(index, uncommitted_failure)
                    ledger.publish()
                    raise uncommitted_failure
                # Retry the primary once for transient errors (timeout,
                # 5xx, 429) before falling back. Only position 0 (the
                # primary) is eligible; already-failed fallbacks are not
                # retried. Auth/invalid-request never retry because they
                # cannot change on a second attempt.
                if (
                    self._retry_first == "retry_once"
                    and routed_around is None
                    and position == 0
                    and index not in retried_positions
                    and self._error_is_retryable(uncommitted_failure)
                ):
                    retried_positions.add(index)
                    logger.info(
                        "MODEL RETRY: '{}' failed once with {}; retrying once before falling back",
                        model_ref,
                        type(uncommitted_failure).__name__,
                    )
                    # Re-prepare the same position (same model). The next
                    # loop iteration streams it again with a fresh
                    # attempt row in the ledger.
                    following = self._prepare_from(
                        attempts,
                        order,
                        position,
                        failures,
                        request_id=request_id,
                        on_attempt=on_attempt,
                        deadline=deadline,
                        ledger=ledger,
                    )
                    if following is not None:
                        position, provider = following
                        continue
                self._trace_fallback(
                    routed, uncommitted_failure, request_id=request_id, attempt=index
                )
                route = _RateLimitRoute()
                if routed_around is not None and position not in probed_positions:
                    probed_positions.add(position)
                    route = await self._route_around_rate_limit(
                        routed_around,
                        provider,
                        plan.probe_candidates,
                        request_id=request_id,
                        has_same_provider_candidate=any(
                            attempts[order[later]].resolved.provider_id
                            == routed_around.provider_id
                            for later in range(position + 1, len(order))
                        ),
                    )
                following = self._prepare_from(
                    attempts,
                    order,
                    position if route.retry_same_position else position + 1,
                    failures,
                    request_id=request_id,
                    on_attempt=on_attempt,
                    deadline=deadline,
                    ledger=ledger,
                    prefer_provider=route.prefer_provider,
                )
                if following is None and (
                    routed_around is not None
                    or failure_kind(uncommitted_failure) is FailureKind.RATE_LIMIT
                ):
                    # Nowhere to route: one key, one configured model, and a
                    # probe that had nothing to ask. The retry ladder is then
                    # the only thing that can still serve this request, so the
                    # operator's PROVIDER_RETRY_ATTEMPTS is spent here rather
                    # than inside the limiter -- which cannot see a chain and
                    # would have spent it on every 429 on every route. A
                    # single-credential provider never wraps in a pool, so it
                    # raises an ordinary rate_limit failure rather than
                    # ``ModelRateLimited``, and both shapes land here.
                    # ``rate_limit_attempts`` is 1 when the toggle is off,
                    # where the limiter has already spent the same ladder.
                    spent = rate_limit_attempts.get(position, 1)
                    if spent < max(1, self._policy.rate_limit_attempts):
                        rate_limit_attempts[position] = spent + 1
                        logger.info(
                            "MODEL RATE LIMITED: '{}' has nowhere to route;"
                            " try {} of {}",
                            model_ref,
                            spent + 1,
                            self._policy.rate_limit_attempts,
                        )
                        following = self._prepare_from(
                            attempts,
                            order,
                            position,
                            failures,
                            request_id=request_id,
                            on_attempt=on_attempt,
                            deadline=deadline,
                            ledger=ledger,
                        )
                if following is None:
                    ledger.publish()
                    raise uncommitted_failure
                position, provider = following

        async def guarded_provider_body() -> AsyncIterator[str]:
            # A client hanging up cancels this generator mid-flight.
            # Without this wrapper the ledger died unpublished, and the
            # request log lost every verdict the chain had reached. Closing
            # the inner body here keeps aclose() reaching the provider
            # synchronously, as it did when this generator was the body.
            inner = provider_body()
            try:
                async for chunk in inner:
                    yield chunk
            except asyncio.CancelledError, GeneratorExit:
                ledger.interrupted()
                ledger.publish()
                raise
            finally:
                await close_stream_input(
                    inner,
                    owner="provider_executor",
                    source="api",
                    preserved_error=sys.exception(),
                )

        stream_trace: dict[str, object] = {
            "request_id": request_id,
            "provider_id": plan.primary.resolved.provider_id,
            "gateway_model": plan.primary.request.model,
        }
        if self._generation_id is not None:
            stream_trace["generation_id"] = self._generation_id

        return traced_async_stream(
            guarded_provider_body(),
            stage="egress",
            source="api",
            complete_event=(
                "my_claude_code.api.responses.stream_completed"
                if wire_api == "responses"
                else "my_claude_code.api.response.stream_completed"
            ),
            interrupted_event=(
                "my_claude_code.api.responses.stream_interrupted"
                if wire_api == "responses"
                else "my_claude_code.api.response.stream_interrupted"
            ),
            chunk_event=None,
            extra=stream_trace,
        )

    def _truncate_after_commit(
        self, tracker: CommittedStreamTracker, exc: Exception, wire_api: WireApi
    ) -> StreamTruncation | None:
        """How a stream the client is already reading should end when it fails.

        ``None`` keeps the behaviour this branch has always had: re-raise, and
        the client receives an API error underneath a partial answer. That is
        right in only two situations -- the operator turned this off, or the
        frames already sent cannot be completed into a valid message at all.

        The one case worth a record but not a clean ending is a ``tool_use``
        block whose arguments stopped mid-JSON. Closing it would hand Claude
        Code a tool call with silently empty arguments, which it would then
        *run*; an honest error is better. The truncation is still returned, so
        the request log can say the turn died inside a tool call rather than
        leaving the reader to guess from a timeout message.
        """
        if not self._policy.end_cleanly_after_commit:
            return None
        # The Responses dialect speaks a different event vocabulary and its
        # assembler can only say ``response.completed`` or ``response.failed``
        # -- there is no ``response.incomplete`` builder
        # (``core/openai_responses/streaming/event_builders.py``). Translating a
        # truncated message through it would tell that client the answer
        # finished, which is the one thing this feature exists to avoid, so the
        # honest ending there is still the error.
        if wire_api != "messages":
            return None
        if tracker.incomplete_tool_use:
            return tracker.abandoned(reason="incomplete_tool_use")
        # Nothing to close, or the model already chose its own ending: in
        # neither case is there a valid message to build out of these frames.
        if not tracker.closable:
            return None
        logger.warning(
            "MODEL TRUNCATED: ending a committed stream cleanly after {} chars"
            " ({}); the answer is incomplete",
            tracker.chars,
            failure_kind_name(exc),
        )
        return tracker.close(reason=failure_kind_name(exc))

    def _charge_failure(self, model_ref: str, exc: Exception) -> None:
        """Count one attempt's failure against the model, exactly once.

        The kind matters: it is what decides whether this failure counts
        against the model at all. A timeout, a 429 or a prompt no model could
        hold is the request's problem, and counting it is what let one
        oversized request eject a whole chain. The status rides along so a
        skipped row can name what the last failure actually was.

        ``failure_kind()`` rather than ``.kind``: an attempt can fail with a
        raw exception (an httpx ``TimeoutError``, a ``RuntimeError`` from a
        construction error) that never reached provider classification, and
        those have no ``.kind`` at all -- and now never bench anything either.

        One function because there are now three ways an attempt can end in a
        failure -- uncommitted, committed-and-truncated, committed-and-
        continued -- and they must charge identically and once.
        """
        kind = failure_kind(exc)
        execution_failure = find_execution_failure(exc)
        self._health.record_failure(
            model_ref,
            failure_kind=kind.value if kind is not None else None,
            status_code=(
                None if execution_failure is None else execution_failure.status_code
            ),
        )

    def _can_resume(
        self, exc: Exception, tracker: CommittedStreamTracker, wire_api: WireApi
    ) -> bool:
        """Whether the next model may be asked to finish this message.

        Every ``False`` below falls through to ``_truncate_after_commit``,
        which is the same short-but-valid message the reader would have got
        without this feature -- so the cost of being wrong here is length, not
        correctness. That asymmetry is why the conditions are permissive about
        *which failure* (any mid-stream death qualifies: a stall, the request
        budget, a 5xx, a dropped transport -- they all leave the same
        half-written answer) and strict about *what state* the stream is in.

        The exclusions, each for its own reason:

        ``responses`` speaks a different event vocabulary with its own ledger,
        which the splicer does not read and could not rewrite.

        A failure kind the operator listed in ``FALLBACK_SKIP_KINDS`` means no
        other model would do better, and that judgement does not stop applying
        because output has started.

        A half-written ``tool_use`` cannot be continued *or* closed honestly --
        ``sse_aggregation`` would substitute ``input={}`` for JSON it cannot
        read and Claude Code would run the call. That one case still raises,
        and it is the only place a reader is still left with a dead turn.

        A ``stop_reason`` already sent means the model chose its own ending;
        ``max_tokens`` exhaustion arrives exactly this way and is not a stall.

        No text sent means there is nothing to continue *from*: the prefill
        would be empty and the request would simply be the original one again.
        """
        if not self._policy.resume_after_commit:
            return False
        if wire_api != "messages":
            return False
        if self._ends_the_route(exc):
            return False
        # ``closable`` is the protocol question -- can these frames be
        # completed into a valid message -- and a stream that cannot be ended
        # cannot be continued either, because a continuation still has to end.
        if not tracker.closable:
            return False
        return bool(tracker.text_prefix)

    def _ends_the_route(self, exc: BaseException) -> bool:
        """Whether this failure means no other model would do better.

        Only kinds the operator has listed. The default is the malformed
        request: the body itself is wrong, so it fails identically on every
        model and a chain turns one fast 400 into three of them. Everything
        else -- timeout, upstream, rate_limit, overloaded, authentication --
        is a property of the model or the moment, which is what a chain is for.

        A context overflow arrives as a 400 too, and used to be swallowed by
        this default because both classified as ``invalid_request``. It is a
        property of *this* model's window, not of the body: the request that
        overflows 256k fits 1M. It is now its own ``CONTEXT_LENGTH`` kind and
        is deliberately absent from the default, so the chain gets its turn.
        An operator who prefers the old abort can put ``context_length`` back
        via ``FALLBACK_SKIP_KINDS``.
        """
        if not self._policy.skip_kinds:
            return False
        kind = failure_kind(exc)
        return kind is not None and kind in self._policy.skip_kinds

    def _attempt_deadline(
        self, deadline: float | None, attempts_remaining: int
    ) -> float | None:
        """When this attempt stops being allowed to produce nothing.

        An equal share of whatever budget is left, counting this attempt and
        every model still behind it. One shared pool meant the first model
        could drain it and the chain was never reached: measured on 21 days of
        real traffic, 393 requests ran the full 600s budget having produced
        only scaffolding, with a configured chain sitting unused.

        Time an attempt does not use flows forward, so a chain of fast failures
        leaves the last model nearly the whole budget rather than a third of it.

        The share alone, though, decided the first-token allowance outright,
        and on a long chain it landed below the deadline the operator had
        configured: 600s over eight models is 75s, so a first-token deadline of
        120s produced "produced no first token after 74.9494s" -- a number
        nothing in the configuration contained. ``attempt_share_floor`` is the
        smallest share this division may return, so the deadline in the box is
        the one that fires. The floor never exceeds what is actually left, and
        ``_chunk_timeout`` still takes the smaller of it and the first-token
        deadline, so raising the floor can only restore the operator's number,
        never invent a longer one. The cost, which is the operator's to make:
        N silent models can spend up to N x the floor before the total budget
        clamps them, and the models after that get less than the floor or
        nothing. ``attempt_share_floor = 0`` restores the pure share.
        """
        if deadline is None or attempts_remaining <= 1:
            return deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return deadline
        share = remaining / attempts_remaining
        if self._policy.attempt_share_floor > 0:
            share = max(share, self._policy.attempt_share_floor)
        # Never hand out budget the request does not have left: the floor
        # raises a share, it does not extend the total.
        share = min(share, remaining)
        return time.monotonic() + share

    def _chunk_timeout(
        self,
        seen_chunk: bool,
        deadline: float | None,
        attempt_deadline: float | None = None,
        last_progress: float | None = None,
        reasoning_since: float | None = None,
    ) -> float | None:
        """Seconds to wait for the next chunk, or ``None`` to wait indefinitely.

        Which limits apply depends on whether the stream has started producing.

        Before the first chunk the first-token limit and this attempt's share
        of the budget both apply, because the attempt is still costing the
        chain its turn.

        After it, the attempt owns the rest of the budget -- truncating a
        working answer to preserve a fallback that can no longer run cleanly
        trades something for nothing -- but it must still be *producing*. The
        stall limit is measured from the last chunk that moved the answer
        forward, so it never shortens a stream that is working and never
        lengthens for one that is only emitting keepalives.
        """
        limits: list[float] = []
        now = time.monotonic()
        if not seen_chunk:
            if (
                reasoning_since is not None
                and self._policy.reasoning_answer_timeout > 0
            ):
                # Measured from the first held thought, not re-armed per
                # thought: a model looping forever produces reasoning
                # indefinitely, so a stall-style clock would never fire on the
                # exact failure this bounds.
                limits.append(
                    max(
                        0.0,
                        reasoning_since + self._policy.reasoning_answer_timeout - now,
                    )
                )
            else:
                if self._policy.first_token_timeout > 0:
                    limits.append(self._policy.first_token_timeout)
                if attempt_deadline is not None:
                    limits.append(max(0.0, attempt_deadline - now))
        elif self._policy.stall_timeout > 0 and last_progress is not None:
            limits.append(max(0.0, last_progress + self._policy.stall_timeout - now))
        if deadline is not None:
            limits.append(max(0.0, deadline - now))
        return min(limits) if limits else None

    def _deadline_reached(
        self,
        model_ref: str,
        *,
        seen_chunk: bool,
        request_id: str,
        attempt_budget: float | None = None,
        last_progress: float | None = None,
        reasoning_since: float | None = None,
    ) -> ExecutionFailure:
        first_token = not seen_chunk
        reasoning_only = first_token and reasoning_since is not None
        # A producing stream can be ended by either the stall limit or the
        # whole-request budget. Which one it was is the difference between
        # "this model went quiet" and "this request ran too long", so decide
        # it from the clocks rather than assuming the budget.
        stalled = (
            not first_token
            and self._policy.stall_timeout > 0
            and last_progress is not None
            and time.monotonic() - last_progress
            >= self._policy.stall_timeout - _STALL_DECISION_TOLERANCE
        )
        if reasoning_only:
            seconds = self._policy.reasoning_answer_timeout
        elif first_token:
            seconds = self._policy.first_token_timeout
        elif stalled:
            seconds = self._policy.stall_timeout
        else:
            seconds = self._policy.total_timeout
        # Report the limit that actually ended it. "produced no output within
        # 120s" on a request abandoned at 40s, because it was one of three
        # models sharing a budget, sends the reader to the wrong setting.
        # ``share_bound`` records which of the two won, so the hint on the
        # message names that same setting rather than the first-token box.
        share_bound = False
        if first_token and not reasoning_only and attempt_budget is not None:
            share_bound = seconds <= 0 or attempt_budget < seconds
            seconds = attempt_budget if seconds <= 0 else min(seconds, attempt_budget)
        logger.warning(
            "MODEL DEADLINE: '{}' {} after {:g}s",
            model_ref,
            "produced only reasoning"
            if reasoning_only
            else (
                "produced no first token"
                if first_token
                else ("stalled" if stalled else "exceeded the request budget")
            ),
            seconds,
        )
        trace_event(
            stage="routing",
            event="my_claude_code.api.route.deadline",
            source="api",
            request_id=request_id,
            provider_model_ref=model_ref,
            first_token=first_token,
            timeout_seconds=seconds,
        )
        return _timeout_failure(
            model_ref,
            seconds=seconds,
            first_token=first_token,
            stalled=stalled,
            reasoning_only=reasoning_only,
            share_bound=share_bound,
        )

    def _error_is_retryable(self, exc: BaseException) -> bool:
        """Whether a one-shot retry of the same model could plausibly help.

        Only transient upstream / server-side kinds are eligible. Auth and
        invalid-request will fail identically on a second attempt, so a
        retry would only add latency to the same answer. Timeout covers
        the holding-pattern case where a model was slow once but may
        respond immediately on retry.
        """
        kind = failure_kind(exc)
        if kind is None:
            # Unclassified errors (raw httpx.TimeoutError, etc.) are
            # treated as transient.
            return True
        return kind in self._retry_once_kinds

    async def _probe_credential_health(
        self,
        provider: ProviderPort,
        candidate: ResolvedModel,
        key_index: int,
        request_id: str,
    ) -> int | None:
        """Ask one cheap question to decide whether a 429 was about the model.

        A 429 on a pooled credential is ambiguous: NVIDIA NIM limits
        ``moonshotai/kimi-k3`` per model and answers ``nemotron`` on the same
        key in the same second, while a free-tier gateway limits the key. The
        pool cannot tell them apart from one response, so when the chain has
        no other model on this provider, one 16-token request to a model the
        operator has already configured on it settles the question.

        Returns the upstream status, or ``None`` if the probe was
        inconclusive -- which is answered by doing exactly what 6.19.0 did.

        Bounded by its own 5s timeout. This clock belongs to the executor,
        which already owns every other deadline in the request; the pool
        still holds none of its own.
        """
        if not isinstance(provider, PooledCredentialPort):
            return None
        request = MessagesRequest(
            model=candidate.provider_model,
            max_tokens=CREDENTIAL_PROBE_MAX_TOKENS,
            messages=[Message(role="user", content="Say OK")],
            stream=True,
        )
        _index, key_label = current_credential()
        started = time.monotonic()
        status: int | None = None
        stream: AsyncIterator[str] | None = None
        try:
            # The probe travels the ordinary provider stack, whose retry
            # frame would record an ``upstream`` try for it. One question
            # deserves one row, and it is the ``probe`` row written below.
            with paused_ladder():
                async with asyncio.timeout(CREDENTIAL_PROBE_TIMEOUT_SECONDS_DEFAULT):
                    stream = provider.stream_on_credential(
                        key_index, request, request_id=request_id
                    )
                    with contextlib.suppress(StopAsyncIteration):
                        # An empty stream still proves the credential accepted
                        # the request, which is the whole question.
                        await anext(stream)
            # A stream that opened at all is the answer: the credential is
            # serving this model, so the 429 was about the other one.
            status = 200
        except Exception as exc:
            failure = find_execution_failure(exc)
            status = failure.status_code if failure is not None else None
            if status is None:
                raw = getattr(exc, "status_code", None)
                status = raw if isinstance(raw, int) else None
        finally:
            if stream is not None:
                await close_stream_input(
                    stream,
                    owner="credential_probe",
                    source="api",
                    preserved_error=sys.exception(),
                )
        record_upstream_try(
            key_index=key_index,
            key_label=key_label,
            status=status,
            kind=None if status is not None else "probe_inconclusive",
            upstream_ms=(time.monotonic() - started) * 1000.0,
            source="probe",
        )
        logger.info(
            "CREDENTIAL PROBE: '{}' on key {} answered {}",
            candidate.provider_model_ref,
            key_index,
            status if status is not None else "nothing conclusive",
        )
        return status

    async def _route_around_rate_limit(
        self,
        exc: ModelRateLimited,
        provider: ProviderPort,
        probe_candidates: Mapping[str, ResolvedModel],
        *,
        request_id: str,
        has_same_provider_candidate: bool,
    ) -> _RateLimitRoute:
        """Decide where a routed-around 429 sends this request next.

        Three answers, and only the first costs nothing. Another model on the
        same provider is the move with evidence behind it, so it is taken
        without asking anything. With no such model the probe decides: a 200
        says the key is fine and only the model is limited, a 429 says the
        limit is the key's after all and the pool should stop offering it,
        and anything else -- an error, the 5s timeout, or no configured model
        on this provider at all -- is inconclusive and answered by doing
        exactly what 6.19.0 did.
        """
        if has_same_provider_candidate:
            return _RateLimitRoute(prefer_provider=exc.provider_id)
        candidate = probe_candidates.get(exc.provider_id)
        if candidate is None or candidate.provider_model == exc.model:
            # Nothing the operator configured on this provider that is not
            # the model that just refused: there is no question to ask.
            return _RateLimitRoute()
        status = await self._probe_credential_health(
            provider, candidate, exc.key_index, request_id
        )
        if status == 429 and isinstance(provider, PooledCredentialPort):
            await provider.escalate_model_bench_to_key(
                exc.key_index, candidate.provider_model_ref, exc.retry_after
            )
            # The key is benched now, so re-entering the pool for the same
            # model picks the next credential instead -- the 6.19.0 outcome,
            # reached only once there was evidence for it.
            return _RateLimitRoute(retry_same_position=True)
        if status is not None and 200 <= status < 400:
            record_credential_decision(
                key_index=exc.key_index,
                key_label=current_credential()[1],
                cls=None,
                status=status,
                reason=(
                    f"probe on {candidate.provider_model_ref} answered {status}"
                    f" -- the key is healthy, only {exc.model} is limited"
                ),
            )
        return _RateLimitRoute()

    def _prepare_from(
        self,
        attempts: tuple[RoutedMessagesRequest, ...],
        order: tuple[int, ...],
        start: int,
        failures: list[BaseException],
        *,
        request_id: str,
        on_attempt: AttemptObserver | None = None,
        deadline: float | None = None,
        ledger: _AttemptLedger | None = None,
        prefer_provider: str | None = None,
    ) -> tuple[int, ProviderPort] | None:
        """Return the first attempt at or after ``start`` that resolves and preflights.

        Preflight runs lazily, one attempt at a time, so a healthy primary never
        pays to validate a fallback it will not use.

        Every candidate is announced to ``on_attempt`` *before* it is tried, so
        the request log names the last model the chain reached even when every
        attempt fails. Announcing only the winner made an exhausted three-model
        chain indistinguishable from a primary that failed on its own.

        ``start`` indexes ``order``, not ``attempts``: a model benched by recent
        failures is not in ``order`` at all and is never reached.

        A candidate whose provider is serving a rate-limit cooldown is stepped
        over while anything remains behind it, because trying it buys a sleep
        rather than an answer.

        ``prefer_provider`` moves the candidates on one provider to the front
        of what is left, keeping their relative order and every other rule
        untouched. It is set only after a 429, and only because a 429 is the
        one failure with evidence of being per-model: NVIDIA NIM refuses
        ``kimi-k3`` on a key while answering ``nemotron`` on that same key in
        the same second. A 5xx never sets it -- that is the gateway failing,
        and preferring another model behind the same failing gateway is the
        wrong bet.
        """
        for position in self._candidate_order(attempts, order, start, prefer_provider):
            index = order[position]
            routed = attempts[index]
            model_ref = routed.resolved.provider_model_ref
            if deadline is not None and time.monotonic() >= deadline:
                # Starting another model with no time left only delays the
                # error the caller is already going to see.
                logger.warning(
                    "MODEL CHAIN EXHAUSTED: request budget spent before trying '{}'",
                    model_ref,
                )
                failures.append(
                    _timeout_failure(
                        model_ref,
                        seconds=self._policy.total_timeout,
                        first_token=False,
                    )
                )
                if ledger is not None:
                    # Everything from here on is unreachable for the same
                    # reason, so say so for each rather than only the first.
                    for remaining in range(position, len(order)):
                        ledger.out_of_time(order[remaining])
                return None
            try:
                provider = self._provider_resolver(routed.resolved.provider_id)
            except Exception as exc:
                if on_attempt is not None:
                    on_attempt(routed, index)
                if ledger is not None:
                    ledger.start(index)
                if self._prepare_failed(
                    routed, index, exc, failures, request_id=request_id, ledger=ledger
                ):
                    return None
                continue

            # A provider serving a 429 cooldown will not refuse the request --
            # it will sleep inside its own limiter until the cooldown expires
            # or this attempt's deadline does, and only then hand the chain a
            # timeout. That wait is the whole reason a chain exists, so spend
            # it on the next model instead. Never on the last candidate: a
            # skipped chain with nothing behind it is an outage, and waiting
            # is still better than refusing outright.
            # Asked only when the answer can change something: with nothing
            # behind this candidate the chain has to try it either way.
            # ``routed.request.model`` and not the provider-prefixed ref: it is
            # the exact string this attempt will send upstream, and the same
            # one the pool's ``report_failure`` scopes a 429 to. Any other
            # spelling makes every (key, model) bench invisible here.
            cooldown = (
                provider.throttle_remaining(routed.request.model)
                if position + 1 < len(order)
                else 0.0
            )
            if cooldown >= self._policy.cooldown_step_over_floor:
                logger.warning(
                    "MODEL COOLDOWN: '{}' is rate-limited for {:.0f}s;"
                    " trying the next model instead of waiting",
                    model_ref,
                    cooldown,
                )
                failures.append(_cooldown_failure(model_ref, cooldown))
                if ledger is not None:
                    ledger.in_cooldown(index, cooldown)
                continue

            if on_attempt is not None:
                on_attempt(routed, index)
            if ledger is not None:
                ledger.start(index)
            try:
                provider.preflight_stream(routed.request, reasoning=routed.reasoning)
            except Exception as exc:
                if self._prepare_failed(
                    routed, index, exc, failures, request_id=request_id, ledger=ledger
                ):
                    return None
                continue
            return position, provider
        return None

    @staticmethod
    def _candidate_order(
        attempts: tuple[RoutedMessagesRequest, ...],
        order: tuple[int, ...],
        start: int,
        prefer_provider: str | None,
    ) -> tuple[int, ...]:
        """Positions to consider, same-provider first when one is preferred.

        A reordering of the positions the loop already walks, never a second
        code path: the provider resolution, the cooldown step-over, the
        announcement and the preflight all still happen exactly once per
        candidate, in the body they always ran in.
        """
        remaining = tuple(range(start, len(order)))
        if prefer_provider is None:
            return remaining
        preferred = tuple(
            position
            for position in remaining
            if attempts[order[position]].resolved.provider_id == prefer_provider
        )
        if not preferred:
            return remaining
        rest = tuple(position for position in remaining if position not in preferred)
        return preferred + rest

    def _prepare_failed(
        self,
        routed: RoutedMessagesRequest,
        index: int,
        exc: Exception,
        failures: list[BaseException],
        *,
        request_id: str,
        ledger: _AttemptLedger | None,
    ) -> bool:
        """Record one pre-stream failure; True when it ends the route."""
        failures.append(exc)
        if ledger is not None:
            ledger.failed(index, exc)
        # The kind matters here too: a pre-stream failure is usually a
        # resolution or credential problem, and only the model-shaped ones
        # should count against the model the route points at.
        pre_stream_failure = find_execution_failure(exc)
        pre_stream_kind = failure_kind(exc)
        self._health.record_failure(
            routed.resolved.provider_model_ref,
            failure_kind=(
                pre_stream_kind.value if pre_stream_kind is not None else None
            ),
            status_code=(
                None if pre_stream_failure is None else pre_stream_failure.status_code
            ),
        )
        self._trace_fallback(routed, exc, request_id=request_id, attempt=index)
        if self._ends_the_route(exc):
            if ledger is not None:
                ledger.unreachable_after(index, exc)
            return True
        return False

    def _trace_route(
        self,
        routed: RoutedMessagesRequest,
        *,
        wire_api: WireApi,
        request_id: str,
        attempt: int,
        attempt_count: int,
    ) -> None:
        route_trace: dict[str, object] = {
            "stage": "routing",
            "event": "my_claude_code.api.route.resolved",
            "source": "api",
            "request_id": request_id,
            "provider_id": routed.resolved.provider_id,
            "provider_model": routed.resolved.provider_model,
            "provider_model_ref": routed.resolved.provider_model_ref,
            "gateway_model": routed.request.model,
            "reasoning_control": routed.reasoning.control.value,
            "reasoning_effort": (
                routed.reasoning.effort.value
                if routed.reasoning.effort is not None
                else None
            ),
            "reasoning_budget_tokens": routed.reasoning.budget_tokens,
            "reasoning_adaptation": routed.reasoning_adaptation.message,
        }
        if attempt_count > 1:
            route_trace["attempt"] = attempt
            route_trace["attempt_count"] = attempt_count
        if wire_api == "responses":
            route_trace["wire_api"] = "responses"
        if self._generation_id is not None:
            route_trace["generation_id"] = self._generation_id
        trace_event(**route_trace)

    def _trace_fallback(
        self,
        routed: RoutedMessagesRequest,
        exc: BaseException,
        *,
        request_id: str,
        attempt: int,
    ) -> None:
        reason = safe_exception_message(exc)
        logger.warning(
            "MODEL FALLBACK: attempt {} '{}' failed before streaming: {}",
            attempt,
            routed.resolved.provider_model_ref,
            reason,
        )
        trace_event(
            stage="routing",
            event="my_claude_code.api.route.fallback",
            source="api",
            request_id=request_id,
            attempt=attempt,
            provider_id=routed.resolved.provider_id,
            provider_model_ref=routed.resolved.provider_model_ref,
            error_kind=type(exc).__name__,
            reason=reason,
        )


def route_execution_policy(settings: Settings) -> RouteExecutionPolicy:
    """Read the route deadlines and stop-conditions a request should run under."""
    return RouteExecutionPolicy(
        first_token_timeout=settings.fallback_first_token_timeout,
        total_timeout=settings.fallback_total_timeout,
        stall_timeout=settings.fallback_stall_timeout,
        reasoning_answer_timeout=settings.fallback_reasoning_answer_timeout,
        attempt_share_floor=settings.fallback_attempt_share_floor,
        skip_kinds=parse_failure_kinds(settings.fallback_skip_kinds),
        cooldown_step_over_floor=settings.fallback_cooldown_step_over_floor,
        end_cleanly_after_commit=settings.fallback_end_cleanly_after_commit,
        resume_after_commit=settings.fallback_resume_after_commit,
        rate_limit_attempts=(
            settings.provider_retry_attempts
            if settings.rate_limit_routes_around_model
            # With routing off the limiter already spent this ladder on the
            # same key; spending it again here would square it.
            else 1
        ),
    )


# Registries live here rather than on the executor, keyed by the settings that
# define them. The executor is built per request -- `MessagesHandler` and
# everything it owns is constructed inside the request handler -- so a registry
# owned by it was rebuilt every time, its consecutive-failure counter reset
# every time, and its threshold therefore never reached. The docstring below
# named that exact failure mode as the thing to avoid; it just did not account
# for the executor itself being per-request.
#
# Confirmed against a live server before this change: four consecutive failures
# of the same model produced zero "MODEL CHAIN: skipping" lines.
_RouteEjectKey = tuple[
    Literal["consecutive", "rate_based"],
    int,
    int,
    float,
    int,
    float,
]
_REGISTRIES: dict[_RouteEjectKey, RouteHealthRegistry] = {}


def route_health_registry(settings: Settings) -> RouteHealthRegistry:
    """Return the shared ejection registry for these ejection settings.

    What a route learns about a model has to outlive a single request: three
    consecutive failures cannot be observed by three independent registries.

    Keyed by all the settings that define ejection rather than held as one
    global, so changing any of them starts a clean registry instead of
    inheriting benches made under a different policy. Nothing else about a
    request can reach it, which keeps this a cache rather than shared
    mutable state.
    """
    key = (
        settings.fallback_behavior,
        settings.fallback_eject_after_failures,
        settings.fallback_eject_window,
        settings.fallback_eject_failure_rate,
        settings.fallback_eject_min_samples,
        settings.fallback_eject_seconds,
        settings.fallback_bench_enabled,
    )
    typed_key = cast(_RouteEjectKey, key)
    registry = _REGISTRIES.get(typed_key)
    if registry is None:
        registry = RouteHealthRegistry(
            mode=cast(Literal["consecutive", "rate_based"], key[0]),
            eject_after_failures=key[1],
            eject_window=key[2],
            eject_failure_rate=key[3],
            eject_min_samples=key[4],
            eject_seconds=key[5],
            bench_enabled=settings.fallback_bench_enabled,
        )
        _REGISTRIES[typed_key] = registry
    return registry


def reset_route_health_registries() -> None:
    """Forget every bench.

    Called by the test suite between tests; no production path invokes it.
    Changing either ejection setting already starts a fresh registry -- the
    cache is keyed by both values -- and within one setting benches are
    meant to outlive a config reload; that persistence is the point.
    """
    _REGISTRIES.clear()
