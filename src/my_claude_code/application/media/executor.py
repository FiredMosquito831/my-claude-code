"""The media attempt loop: a separate copy of the chat executor's rules.

User decision (2026-09-26 03:38, #4): image, speech, transcription and video
requests go through a SEPARATE copy of chat's retry / key / 429 / pause /
bench / proxy rules, and ``application/execution.py`` stays untouched. This
module is that copy of ``ProviderExecutor.stream`` and its helpers. Every
decision below mirrors a line there; ``tests/contracts/test_media_chat_parity.py``
drives the same failure scenarios through both and asserts the same decisions,
so a later change to either side that is not made to the other fails the suite.

What is deliberately *not* copied, because it has no media meaning: token
counting, output and reasoning budgets, the reasoning heartbeat, and resuming a
committed stream on the next model (that path exists only on the Messages
wire). What is copied verbatim in effect: the pause and bench ordering, the
all-paused and all-out-of-credits verdicts, the rate-limit cooldown step-over,
the three deadlines, ``FALLBACK_RETRY_FIRST=retry_once`` on the primary,
``FALLBACK_SKIP_KINDS`` ending the route, the 429 route-around (prefer the same
provider, else one chat-sized probe on the same key, else escalate), the
rate-limit ladder when nothing else is left, and the failure-charging rules.

Two things are media's own, and neither exists on the chat side:

* **The capability gate.** A candidate whose provider declares no surface for
  the operation is skipped *without* being charged -- it did not fail, it was
  never offered the job.
* **Its health books.** ``media_route_health_registry`` is a registry of its
  own, never the chat one, so a model benched for images stays usable for chat
  and the other way round.
"""

import asyncio
import contextlib
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from loguru import logger

from my_claude_code.application.errors import ModelRateLimited
from my_claude_code.application.execution import (
    AttemptResultObserver,
    RouteExecutionPolicy,
    _all_paused_failure,
    _all_quota_failure,
    _AttemptLedger,
    _cooldown_failure,
    _timeout_failure,
)
from my_claude_code.application.ports import PooledCredentialPort, ProviderPort
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.constants import (
    CREDENTIAL_PROBE_MAX_TOKENS,
    CREDENTIAL_PROBE_TIMEOUT_SECONDS_DEFAULT,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import Message, MessagesRequest
from my_claude_code.core.credential_attribution import (
    current_credential,
    record_credential,
)
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.core.trace import close_stream_input, trace_event
from my_claude_code.core.upstream_ladder import (
    paused_ladder,
    record_credential_decision,
    record_upstream_try,
)
from my_claude_code.core.waiting_clock import waited_seconds

from .ports import MediaProviderPort, MediaProviderResolver, PooledMediaPort
from .request import MediaAttempt, MediaChunk, MediaPlan

#: Announced before an attempt is tried, like the chat ``AttemptObserver``.
MediaAttemptObserver = Callable[[MediaAttempt, int], None]
#: Frames that end a committed stream the surface's own way (an SSE ``error``).
CommitErrorFrames = Callable[[BaseException], Sequence[bytes]]

_STALL_DECISION_TOLERANCE = 0.05


class _MediaDeadlineExceeded(Exception):
    """Internal marker: our own wait elapsed, not an upstream timeout."""


@dataclass(frozen=True, slots=True)
class _RateLimitRoute:
    """Where a routed-around 429 sends the request next (chat's own shape)."""

    prefer_provider: str | None = None
    retry_same_position: bool = False


class _MediaLedger(_AttemptLedger):
    """The chat ledger plus the one verdict only media has."""

    def unsupported(self, index: int, operation: str) -> None:
        self._set(
            index,
            outcome="skipped",
            error_kind="unsupported",
            error_message=(
                f"provider declares no {operation} surface; not tried and not charged"
            ),
        )


def _unsupported_failure(operation: str) -> ExecutionFailure:
    """The verdict for a rail none of whose providers offers the operation."""

    return ExecutionFailure(
        kind=FailureKind.INVALID_REQUEST,
        status_code=400,
        message=(
            f"No model on this rail is served by a provider that declares a "
            f"{operation} endpoint (or one that streams, if a stream was asked "
            "for). Choose a model on a provider that serves it on Model Config."
        ),
        retryable=False,
    )


async def _next_chunk[T](
    chunks: AsyncIterator[T], timeout: float | None, deadline: float | None = None
) -> T:
    """Chat's ``_next_chunk``, typed for any chunk: same re-arm, same clamp."""

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
        await asyncio.gather(pending, return_exceptions=True)
        raise _MediaDeadlineExceeded


_RETRY_ONCE_KINDS = frozenset(
    {
        FailureKind.RATE_LIMIT,
        FailureKind.OVERLOADED,
        FailureKind.TIMEOUT,
        FailureKind.UPSTREAM,
        FailureKind.UNAVAILABLE,
    }
)


class MediaExecutor:
    """Resolve a media provider and execute one routed media request."""

    def __init__(
        self,
        provider_resolver: MediaProviderResolver,
        *,
        policy: RouteExecutionPolicy | None = None,
        health: RouteHealthRegistry | None = None,
        retry_first: str = "skip",
        provider_lookup: Callable[[str], float | None] | None = None,
        chat_provider_resolver: Callable[[str], ProviderPort] | None = None,
        retry_once_kinds: frozenset[FailureKind] = _RETRY_ONCE_KINDS,
    ) -> None:
        self._provider_resolver = provider_resolver
        self._policy = policy or RouteExecutionPolicy()
        self._health = health or RouteHealthRegistry()
        self._retry_first = retry_first
        self._provider_lookup = provider_lookup
        self._chat_provider_resolver = chat_provider_resolver
        self._retry_once_kinds = retry_once_kinds

    def execute(
        self,
        plan: MediaPlan,
        *,
        request_id: str,
        on_attempt: MediaAttemptObserver | None = None,
        on_attempt_result: AttemptResultObserver | None = None,
        commit_error_frames: CommitErrorFrames | None = None,
    ) -> AsyncIterator[MediaChunk]:
        """Preflight synchronously, then return the attempt stream.

        Mirrors ``ProviderExecutor.stream``: attempts are tried in order until
        one commits. A non-streaming request commits only when its whole answer
        has arrived, so any failure before that falls back invisibly.
        """
        attempts = plan.attempts
        buffer_until_complete = not plan.primary.request.stream
        failures: list[BaseException] = []
        order = self._health.usable_indexes(
            plan.model_refs(),
            provider_lookup=self._provider_lookup,
            paused=plan.paused_refs,
        )
        if not order:
            failure = _all_paused_failure(plan.paused_env_var)
            ledger = _MediaLedger(
                plan.model_refs(), plan.resolved_models(), on_attempt_result
            )
            ledger.mark_paused((), plan.paused_refs)
            ledger.publish()
            raise failure
        if len(order) < len(attempts):
            logger.info(
                "MEDIA CHAIN: skipping {} recently-failing model(s) on this rail",
                len(attempts) - len(order),
            )
        deadline = (
            time.monotonic() + self._policy.total_timeout
            if self._policy.total_timeout > 0
            else None
        )
        ledger = _MediaLedger(
            plan.model_refs(), plan.resolved_models(), on_attempt_result
        )
        ledger.mark_benched(order, self._health.why)
        ledger.mark_paused(order, plan.paused_refs)
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
            ledger.publish()
            if not failures:
                raise _unsupported_failure(plan.primary.request.operation)
            raise _all_quota_failure(failures) or failures[-1]

        retried_positions: set[int] = set()
        probed_positions: set[int] = set()
        rate_limit_attempts: dict[int, int] = {}

        async def provider_body() -> AsyncIterator[MediaChunk]:
            position, provider = prepared
            committed = False
            while True:
                index = order[position]
                attempt = attempts[index]
                model_ref = attempt.resolved.provider_model_ref
                attempt_deadline = self._attempt_deadline(
                    deadline, len(order) - position
                )
                attempt_budget = (
                    None
                    if attempt_deadline is None
                    else max(0.0, attempt_deadline - time.monotonic())
                )
                provider_stream: AsyncIterator[MediaChunk] | None = None
                uncommitted_failure: Exception | None = None
                committed_failure: Exception | None = None
                held: list[MediaChunk] = []
                try:
                    record_credential(0, provider.credential_label)
                    provider_stream = provider.execute(attempt, request_id=request_id)
                    chunks = provider_stream.__aiter__()
                    seen_chunk = False
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
                                ),
                                deadline,
                            )
                        except StopAsyncIteration:
                            break
                        except _MediaDeadlineExceeded as exc:
                            raise self._deadline_reached(
                                model_ref,
                                seen_chunk=seen_chunk,
                                request_id=request_id,
                                attempt_budget=attempt_budget,
                                last_progress=last_progress,
                            ) from exc
                        seen_chunk = True
                        ledger.note_content(index)
                        last_progress = time.monotonic()
                        if buffer_until_complete:
                            held.append(chunk)
                            continue
                        committed = True
                        yield chunk
                except Exception as exc:
                    if committed:
                        committed_failure = exc
                    else:
                        uncommitted_failure = exc
                finally:
                    if provider_stream is not None:
                        await close_stream_input(
                            provider_stream,
                            owner="media_executor",
                            source="api",
                            preserved_error=sys.exception(),
                        )
                if committed_failure is not None:
                    # Nothing can move a stream the client has already seen to
                    # another model. With FALLBACK_END_CLEANLY_AFTER_COMMIT the
                    # surface ends it its own way and the model is charged, as
                    # chat charges a cleanly-ended truncation; otherwise the
                    # error propagates uncharged, as chat's does.
                    ledger.failed(index, committed_failure)
                    ledger.unreachable_after(index, committed_failure)
                    if (
                        not self._policy.end_cleanly_after_commit
                        or commit_error_frames is None
                    ):
                        ledger.publish()
                        raise committed_failure
                    for frame in commit_error_frames(committed_failure):
                        yield frame
                    self._charge_failure(model_ref, committed_failure)
                    ledger.publish()
                    return
                if uncommitted_failure is None:
                    for chunk in held:
                        yield chunk
                    self._health.record_success(model_ref)
                    ledger.succeeded(index)
                    ledger.publish()
                    return

                routed_around: ModelRateLimited | None = None
                if isinstance(uncommitted_failure, ModelRateLimited):
                    routed_around = uncommitted_failure
                    uncommitted_failure = routed_around.failure

                failures.append(uncommitted_failure)
                ledger.failed(index, uncommitted_failure)
                self._charge_failure(model_ref, uncommitted_failure)
                if self._ends_the_route(uncommitted_failure):
                    ledger.unreachable_after(index, uncommitted_failure)
                    ledger.publish()
                    raise uncommitted_failure
                if (
                    self._retry_first == "retry_once"
                    and routed_around is None
                    and position == 0
                    and index not in retried_positions
                    and self._error_is_retryable(uncommitted_failure)
                ):
                    retried_positions.add(index)
                    logger.info(
                        "MEDIA RETRY: '{}' failed once with {}; retrying once"
                        " before falling back",
                        model_ref,
                        type(uncommitted_failure).__name__,
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
                    if following is not None:
                        position, provider = following
                        continue
                self._trace_fallback(
                    attempt, uncommitted_failure, request_id=request_id, index=index
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
                    spent = rate_limit_attempts.get(position, 1)
                    if spent < max(1, self._policy.rate_limit_attempts):
                        rate_limit_attempts[position] = spent + 1
                        logger.info(
                            "MEDIA RATE LIMITED: '{}' has nowhere to route;"
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
                    raise _all_quota_failure(failures) or uncommitted_failure
                position, provider = following

        async def guarded_provider_body() -> AsyncIterator[MediaChunk]:
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
                    owner="media_executor",
                    source="api",
                    preserved_error=sys.exception(),
                )

        return guarded_provider_body()

    # ------------------------------------------------------------------ copies
    def _charge_failure(self, model_ref: str, exc: Exception) -> None:
        """Count one attempt's failure against the model, exactly once (chat's rule)."""
        kind = failure_kind(exc)
        execution_failure = find_execution_failure(exc)
        self._health.record_failure(
            model_ref,
            failure_kind=kind.value if kind is not None else None,
            status_code=(
                None if execution_failure is None else execution_failure.status_code
            ),
        )

    def _ends_the_route(self, exc: BaseException) -> bool:
        """Only the kinds in ``FALLBACK_SKIP_KINDS`` end the route (chat's rule)."""
        if not self._policy.skip_kinds:
            return False
        kind = failure_kind(exc)
        return kind is not None and kind in self._policy.skip_kinds

    def _attempt_deadline(
        self, deadline: float | None, attempts_remaining: int
    ) -> float | None:
        """An equal share of what is left, floored (chat's rule)."""
        if deadline is None or attempts_remaining <= 1:
            return deadline
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return deadline
        share = remaining / attempts_remaining
        if self._policy.attempt_share_floor > 0:
            share = max(share, self._policy.attempt_share_floor)
        share = min(share, remaining)
        return time.monotonic() + share

    def _chunk_timeout(
        self,
        seen_chunk: bool,
        deadline: float | None,
        attempt_deadline: float | None = None,
        last_progress: float | None = None,
    ) -> float | None:
        """Chat's first-token / share / stall / budget limits (no reasoning)."""
        limits: list[float] = []
        now = time.monotonic()
        if not seen_chunk:
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
    ) -> ExecutionFailure:
        first_token = not seen_chunk
        stalled = (
            not first_token
            and self._policy.stall_timeout > 0
            and last_progress is not None
            and time.monotonic() - last_progress
            >= self._policy.stall_timeout - _STALL_DECISION_TOLERANCE
        )
        if first_token:
            seconds = self._policy.first_token_timeout
        elif stalled:
            seconds = self._policy.stall_timeout
        else:
            seconds = self._policy.total_timeout
        share_bound = False
        if first_token and attempt_budget is not None:
            share_bound = seconds <= 0 or attempt_budget < seconds
            seconds = attempt_budget if seconds <= 0 else min(seconds, attempt_budget)
        logger.warning(
            "MEDIA DEADLINE: '{}' {} after {:g}s",
            model_ref,
            "produced no answer"
            if first_token
            else ("stalled" if stalled else "exceeded the request budget"),
            seconds,
        )
        trace_event(
            stage="routing",
            event="my_claude_code.api.media.deadline",
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
            share_bound=share_bound,
        )

    def _error_is_retryable(self, exc: BaseException) -> bool:
        kind = failure_kind(exc)
        if kind is None:
            return True
        return kind in self._retry_once_kinds

    async def _probe_credential_health(
        self,
        provider: ProviderPort | None,
        candidate: ResolvedModel,
        key_index: int,
        request_id: str,
        blocked_for: float = 0.0,
    ) -> int | None:
        """Chat's probe, asked of the CHAT provider on the same key index.

        A 16-token chat question is the cheapest thing a key can be asked; a
        probe that generated an image would be billed as one. The key order is
        the configured order on both sides, so index ``key_index`` is the same
        secret. Selection and health accounting are bypassed exactly as the
        chat probe bypasses them: this is a measurement, not a request.

        ``blocked_for`` is the 429's reactive block on the MEDIA key. On the
        chat side the probe runs on the very leaf that just met the 429, so
        it first waits out that leaf's own block inside the same 5 s bound --
        and a block longer than the bound makes the probe inconclusive. The
        chat leaf here never saw the media 429, so the wait is served
        explicitly, inside the same bound, to reach the same verdict.
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
            with paused_ladder():
                async with asyncio.timeout(CREDENTIAL_PROBE_TIMEOUT_SECONDS_DEFAULT):
                    if blocked_for > 0:
                        await asyncio.sleep(blocked_for)
                    stream = provider.stream_on_credential(
                        key_index, request, request_id=request_id
                    )
                    with contextlib.suppress(StopAsyncIteration):
                        await anext(stream)
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
                    owner="media_credential_probe",
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
            "MEDIA CREDENTIAL PROBE: '{}' on key {} answered {}",
            candidate.provider_model_ref,
            key_index,
            status if status is not None else "nothing conclusive",
        )
        return status

    async def _route_around_rate_limit(
        self,
        exc: ModelRateLimited,
        provider: MediaProviderPort,
        probe_candidates: Mapping[str, ResolvedModel],
        *,
        request_id: str,
        has_same_provider_candidate: bool,
    ) -> _RateLimitRoute:
        """Chat's three answers; the escalation lands on the MEDIA key book."""
        if has_same_provider_candidate:
            return _RateLimitRoute(prefer_provider=exc.provider_id)
        candidate = probe_candidates.get(exc.provider_id)
        if candidate is None or candidate.provider_model == exc.model:
            return _RateLimitRoute()
        chat_provider: ProviderPort | None = None
        if self._chat_provider_resolver is not None:
            try:
                chat_provider = self._chat_provider_resolver(exc.provider_id)
            except Exception:
                chat_provider = None
        blocked_for = (
            provider.key_throttle_remaining(exc.key_index)
            if isinstance(provider, PooledMediaPort)
            else 0.0
        )
        status = await self._probe_credential_health(
            chat_provider, candidate, exc.key_index, request_id, blocked_for
        )
        if status == 429 and isinstance(provider, PooledMediaPort):
            await provider.escalate_model_bench_to_key(
                exc.key_index, candidate.provider_model_ref, exc.retry_after
            )
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
        attempts: Sequence[MediaAttempt],
        order: tuple[int, ...],
        start: int,
        failures: list[BaseException],
        *,
        request_id: str,
        on_attempt: MediaAttemptObserver | None = None,
        deadline: float | None = None,
        ledger: _MediaLedger | None = None,
        prefer_provider: str | None = None,
    ) -> tuple[int, MediaProviderPort] | None:
        """Chat's ``_prepare_from`` plus the uncharged capability gate."""
        for position in self._candidate_order(attempts, order, start, prefer_provider):
            index = order[position]
            attempt = attempts[index]
            model_ref = attempt.resolved.provider_model_ref
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "MEDIA CHAIN EXHAUSTED: request budget spent before trying '{}'",
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
                    for remaining in range(position, len(order)):
                        ledger.out_of_time(order[remaining])
                return None
            try:
                provider = self._provider_resolver(attempt.resolved.provider_id)
            except Exception as exc:
                if on_attempt is not None:
                    on_attempt(attempt, index)
                if ledger is not None:
                    ledger.start(index)
                if self._prepare_failed(
                    attempt, index, exc, failures, request_id=request_id, ledger=ledger
                ):
                    return None
                continue

            if not provider.supports(attempt.request):
                if ledger is not None:
                    ledger.unsupported(index, attempt.request.operation)
                logger.info(
                    "MEDIA ROUTE: '{}' declares no {} surface; skipped, not charged",
                    model_ref,
                    attempt.request.operation,
                )
                continue

            cooldown = (
                provider.throttle_remaining(attempt.resolved.provider_model)
                if position + 1 < len(order)
                else 0.0
            )
            if cooldown >= self._policy.cooldown_step_over_floor:
                logger.warning(
                    "MEDIA COOLDOWN: '{}' is rate-limited for {:.0f}s;"
                    " trying the next model instead of waiting",
                    model_ref,
                    cooldown,
                )
                failures.append(_cooldown_failure(model_ref, cooldown))
                if ledger is not None:
                    ledger.in_cooldown(index, cooldown)
                continue

            if on_attempt is not None:
                on_attempt(attempt, index)
            if ledger is not None:
                ledger.start(index)
            try:
                provider.preflight(attempt)
            except Exception as exc:
                if self._prepare_failed(
                    attempt, index, exc, failures, request_id=request_id, ledger=ledger
                ):
                    return None
                continue
            return position, provider
        return None

    @staticmethod
    def _candidate_order(
        attempts: Sequence[MediaAttempt],
        order: tuple[int, ...],
        start: int,
        prefer_provider: str | None,
    ) -> tuple[int, ...]:
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
        attempt: MediaAttempt,
        index: int,
        exc: Exception,
        failures: list[BaseException],
        *,
        request_id: str,
        ledger: _MediaLedger | None,
    ) -> bool:
        """Record one pre-send failure; True when it ends the route (chat's rule)."""
        failures.append(exc)
        if ledger is not None:
            ledger.failed(index, exc)
        pre_stream_failure = find_execution_failure(exc)
        pre_stream_kind = failure_kind(exc)
        self._health.record_failure(
            attempt.resolved.provider_model_ref,
            failure_kind=(
                pre_stream_kind.value if pre_stream_kind is not None else None
            ),
            status_code=(
                None if pre_stream_failure is None else pre_stream_failure.status_code
            ),
        )
        self._trace_fallback(attempt, exc, request_id=request_id, index=index)
        if self._ends_the_route(exc):
            if ledger is not None:
                ledger.unreachable_after(index, exc)
            return True
        return False

    def _trace_fallback(
        self,
        attempt: MediaAttempt,
        exc: BaseException,
        *,
        request_id: str,
        index: int,
    ) -> None:
        reason = safe_exception_message(exc)
        logger.warning(
            "MEDIA FALLBACK: attempt {} '{}' failed before answering: {}",
            index,
            attempt.resolved.provider_model_ref,
            reason,
        )
        trace_event(
            stage="routing",
            event="my_claude_code.api.media.fallback",
            source="api",
            request_id=request_id,
            attempt=index,
            provider_id=attempt.resolved.provider_id,
            provider_model_ref=attempt.resolved.provider_model_ref,
            error_kind=type(exc).__name__,
            reason=reason,
        )


_MediaEjectKey = tuple[
    Literal["consecutive", "rate_based"],
    int,
    int,
    float,
    int,
    float,
]
_MEDIA_REGISTRIES: dict[_MediaEjectKey, RouteHealthRegistry] = {}


def media_route_health_registry(settings: Settings) -> RouteHealthRegistry:
    """The MEDIA bench registry for these ejection settings.

    A copy of ``route_health_registry`` with a cache of its own: the same
    settings decide ejection, but the books are separate, so a model a media
    endpoint benched is not benched for chat (and the other way round).
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
    typed_key = cast(_MediaEjectKey, key)
    registry = _MEDIA_REGISTRIES.get(typed_key)
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
        _MEDIA_REGISTRIES[typed_key] = registry
    return registry


def reset_media_route_health_registries() -> None:
    """Forget every media bench. Called by the test suite between tests."""
    _MEDIA_REGISTRIES.clear()
