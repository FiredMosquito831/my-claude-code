"""Provider execution shared by inbound API adapters."""

import asyncio
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, cast

from loguru import logger

from my_claude_code.config.constants import (
    FALLBACK_ATTEMPT_SHARE_FLOOR_DEFAULT,
    FALLBACK_COOLDOWN_STEP_OVER_FLOOR_DEFAULT,
    FALLBACK_FIRST_TOKEN_TIMEOUT_DEFAULT,
    FALLBACK_REASONING_ANSWER_TIMEOUT_DEFAULT,
    FALLBACK_STALL_TIMEOUT_DEFAULT,
    FALLBACK_TOTAL_TIMEOUT_DEFAULT,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    Message,
    SystemContent,
    Tool,
    anthropic_request_snapshot,
    get_token_count,
)
from my_claude_code.core.anthropic.stream_contracts import (
    REASONING_HEARTBEAT,
    sse_carries_content,
)
from my_claude_code.core.credential_attribution import record_credential
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    failure_kind_name,
    parse_failure_kinds,
)
from my_claude_code.core.trace import (
    close_stream_input,
    trace_event,
    traced_async_stream,
)

from .ports import ProviderPort, ProviderResolver
from .route_health import RouteHealthRegistry
from .routing import (
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


# The wait is scheduled for exactly the remaining stall budget, so by the time
# it elapses the measured gap lands a hair under the limit. Without a tolerance
# the decision below would attribute every stall to the request budget instead.
_STALL_DECISION_TOLERANCE = 0.05


class _DeadlineExceeded(Exception):
    """Internal marker: our own wait elapsed, not an upstream timeout."""


async def _next_chunk(chunks: AsyncIterator[str], timeout: float | None) -> str:
    """Return the next chunk, or raise ``_DeadlineExceeded`` if ``timeout`` elapses.

    ``asyncio.wait`` rather than ``wait_for`` so a ``TimeoutError`` raised *by
    the provider* stays distinguishable from this deadline elapsing. The two
    mean different things -- one is the upstream giving up, the other is us
    deciding not to keep waiting -- and only the second should be reported as a
    routing deadline.
    """
    if timeout is None:
        return await anext(chunks)
    pending = asyncio.ensure_future(anext(chunks))
    done, _still_running = await asyncio.wait({pending}, timeout=timeout)
    if not done:
        pending.cancel()
        # Let the cancellation land before the caller closes the stream, so a
        # half-cancelled task cannot outlive the attempt that owns it.
        await asyncio.gather(pending, return_exceptions=True)
        raise _DeadlineExceeded
    return pending.result()


def _timeout_failure(
    model_ref: str,
    *,
    seconds: float,
    first_token: bool,
    stalled: bool = False,
    reasoning_only: bool = False,
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
    return ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=504,
        message=f"Provider '{model_ref}' {reason}.",
        retryable=True,
    )


def _provider_retry_after(provider: ProviderPort) -> float | None:
    """The provider's own remaining cooldown, when it reports a usable one.

    Used to size a rate-limit bench from the provider's signal instead of a
    fixed number, per "read signals, don't invent thresholds".

    Deliberately total: this runs while a failure is already being recorded,
    so anything raised here would replace a real upstream error with an
    internal one. A provider that cannot answer just yields None, and the
    registry falls back to its configured eject window.
    """
    try:
        remaining = provider.throttle_remaining()
    except Exception:
        return None
    if isinstance(remaining, bool) or not isinstance(remaining, int | float):
        return None
    return float(remaining) if remaining > 0 else None


def _cooldown_failure(model_ref: str, seconds: float) -> ExecutionFailure:
    """The verdict recorded for a model the chain stepped over while limited."""
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message=(
            f"Provider for '{model_ref}' is in rate-limit cooldown for {seconds:.0f}s."
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

    def mark_benched(self, order: tuple[int, ...]) -> None:
        """Record models the health registry removed before the request began."""
        usable = set(order)
        for index in self._records:
            if index not in usable:
                self._set(
                    index,
                    outcome="skipped",
                    error_kind="ejected",
                    error_message="benched after recent consecutive failures",
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
        )

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
        ledger.mark_benched(order)
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

        async def provider_body() -> AsyncIterator[str]:
            position, provider = prepared
            while True:
                index = order[position]
                routed = attempts[index]
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
                committed = False
                uncommitted_failure: Exception | None = None
                held: list[str] = []
                try:
                    # Baseline attribution for single-credential providers. A
                    # rotating provider overwrites this with the credential it
                    # actually picks for this request.
                    record_credential(0, provider.credential_label)
                    provider_stream = provider.stream_response(
                        routed.request,
                        input_tokens=input_tokens,
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
                            # The provider is holding reasoning back. Nothing
                            # to forward and nothing committed, but the attempt
                            # is demonstrably working rather than silent, so it
                            # earns the answer allowance instead of the
                            # first-token share.
                            if reasoning_since is None:
                                reasoning_since = time.monotonic()
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
                        committed = True
                        yield chunk
                except Exception as exc:
                    if committed:
                        # No fallback is possible past the commit point, but the
                        # attempt still ended in a failure and the log should
                        # say so rather than leaving it as "never reached".
                        ledger.failed(index, exc)
                        ledger.unreachable_after(index, exc)
                        ledger.publish()
                        raise
                    uncommitted_failure = exc
                finally:
                    if provider_stream is not None:
                        await close_stream_input(
                            provider_stream,
                            owner="provider_executor",
                            source="api",
                            preserved_error=sys.exception(),
                        )
                if uncommitted_failure is None:
                    # Empty unless this attempt was held back for a
                    # non-streaming client; a failed attempt's chunks are
                    # dropped with it and never reach the aggregator.
                    for chunk in held:
                        yield chunk
                    self._health.record_success(model_ref)
                    ledger.succeeded(index)
                    ledger.publish()
                    return

                # The failed stream is closed by now, so the next attempt never
                # runs alongside a half-open connection to the previous one.
                failures.append(uncommitted_failure)
                ledger.failed(index, uncommitted_failure)
                # Pass the kind through so the bench duration matches the
                # failure: a 5xx/timeout parks the model for a second, a
                # rate-limit honours the provider's own remaining cooldown,
                # and auth/quota take the full eject window. Without these
                # arguments every ejection used eject_seconds and the whole
                # kind-aware ladder in RouteHealthRegistry was dead code.
                # failure_kind() rather than .kind: an attempt can fail with a
                # raw exception (httpx TimeoutError, RuntimeError from a
                # construction error) that never reached provider
                # classification, and those have no .kind at all.
                kind = failure_kind(uncommitted_failure)
                retry_after_seconds = (
                    _provider_retry_after(provider)
                    if kind is FailureKind.RATE_LIMIT
                    else None
                )
                self._health.record_failure(
                    model_ref,
                    failure_kind=kind.value if kind is not None else None,
                    retry_after_seconds=retry_after_seconds,
                )
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
                following = self._prepare_from(
                    attempts,
                    order,
                    position + 1,
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
        if first_token and not reasoning_only and attempt_budget is not None:
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
        """
        for position in range(start, len(order)):
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
            cooldown = (
                provider.throttle_remaining() if position + 1 < len(order) else 0.0
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
        self._health.record_failure(routed.resolved.provider_model_ref)
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
