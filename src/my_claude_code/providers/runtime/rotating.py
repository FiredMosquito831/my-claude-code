"""Provider wrapper that rotates requests across multiple credentials."""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.attempt_budget import current_attempt_deadline
from my_claude_code.core.credential_attribution import record_credential
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.http import maybe_await_aclose

#: Floor for one credential's slice of the attempt's first-token budget. A
#: three-key pool on a 60s attempt gets 20s a key, but a pool large enough to
#: divide the share into single-digit seconds would reject keys that were only
#: connecting slowly. Below this the pool simply tries fewer credentials
#: within the attempt; the executor's own deadline still bounds the total.
MIN_CREDENTIAL_FIRST_TOKEN_SECONDS = 5.0


class RotatingProvider(BaseProvider):
    """Fan requests out to one sub-provider per configured credential.

    Failover only happens before the first SSE chunk of a request: once output
    has started streaming to the client, switching credentials would duplicate
    or corrupt the response, so mid-stream errors propagate unchanged.
    """

    def __init__(
        self,
        config: ProviderConfig,
        providers: Sequence[BaseProvider],
        state: CredentialRotationState,
        key_labels: Sequence[str] = (),
    ) -> None:
        super().__init__(config)
        if not providers:
            raise ValueError("RotatingProvider requires at least one sub-provider")
        self._providers = tuple(providers)
        self._state = state
        # Masked ``first4…last4`` labels, index-aligned with ``providers``. Used
        # only to identify a credential in analytics and admin views; the raw
        # key values stay inside the sub-providers.
        self._key_labels = tuple(key_labels)

    @property
    def credential_label(self) -> str | None:
        """No single label applies: the credential is chosen per request."""
        return None

    def _unavailable_now(self) -> frozenset[int]:
        """Credentials that are healthy but cannot serve this instant.

        A rate-limited credential stays HEALTHY -- being throttled is not a
        fault -- so rotation used to select it anyway and the request then sat
        waiting inside that credential's own limiter while an idle credential
        went unused. The throttle window comes from the provider's own
        response, never from a limit invented here.
        """
        return frozenset(
            index
            for index, provider in enumerate(self._providers)
            if provider.throttle_remaining() > 0
        )

    def _key_label(self, index: int) -> str | None:
        if 0 <= index < len(self._key_labels):
            return self._key_labels[index]
        return None

    def throttle_remaining(self) -> float:
        """The shortest wait before any credential can serve; 0 while one can.

        ``BaseProvider`` answers 0 unconditionally, which for a rotating
        provider would have claimed every key was free even with all of them
        rate-limited. Rotation already prefers an unthrottled sub-provider, so
        the value routing needs is the best case, not the first one.

        Two independent things bench a credential and both have to be read.
        The rate limiter is the provider's own throttle window. Health is the
        rotation engine's -- COOLDOWN, CIRCUIT_OPEN, LOCKED_OUT. Reading only
        the limiter meant a pool whose every key was health-benched but not
        throttled still reported 0: routing skipped its step-over, committed
        the attempt, and the request paid a full round trip only to be told
        every key was in cooldown.

        The contract is therefore: 0 whenever any credential can serve, and
        otherwise the shortest wait until one can. Selectability comes from
        the engine rather than per-slot health so the forced-single policies
        agree with what ``acquire`` would actually hand out -- a single-key
        provider serves slot 0 regardless of its health, and must keep
        reporting itself free.
        """
        ready = self._state.selectable_indexes()
        if not ready:
            return self._state.bench_remaining_now()
        return min(
            (
                self._providers[index].throttle_remaining()
                for index in ready
                if index < len(self._providers)
            ),
            default=0.0,
        )

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        """Validate the request once; all sub-providers share the same policy."""
        self._providers[0].preflight_stream(request, reasoning=reasoning)

    async def list_model_ids(self) -> frozenset[str]:
        return await self._providers[0].list_model_ids()

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        return await self._providers[0].list_model_infos()

    async def cleanup(self) -> None:
        errors: list[Exception] = []
        for provider in self._providers:
            try:
                await provider.cleanup()
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup("One or more sub-provider cleanups failed", errors)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream_with_rotation(
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    def _first_token_budget(self, untried: int) -> float | None:
        """How long one credential may take to produce a first token.

        The executor hands this attempt a share of the request budget and
        counts every model still behind it, so that a chain of models each
        gets a turn. Inside the attempt the same argument applies one level
        down: with the whole share spent on the first credential, keys two
        through N are never tried and a configured rotation pool looks
        ignored. The live symptom was a single key stalling for the full 66s
        attempt, five models deep, with two idle keys beside it.

        The share is divided by the credentials this request has not tried
        yet and clamped to what is actually left, so the total can never
        exceed what the executor already allowed -- the executor keeps the
        outer bound and this only subdivides it. ``None`` when the executor
        set no deadline, which leaves the wait exactly as unbounded as it was.
        """
        deadline = current_attempt_deadline()
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        share = remaining / max(1, untried)
        return min(remaining, max(share, MIN_CREDENTIAL_FIRST_TOKEN_SECONDS))

    async def _first_chunk(
        self, iterator: AsyncIterator[str], index: int, budget: float | None
    ) -> str:
        """Await this credential's first chunk within its slice of the attempt.

        A blown budget is reported as a canonical ``TIMEOUT`` failure so the
        rest of the stack reads it exactly as it reads an upstream timeout:
        it rotates to the next credential, and it charges that credential's
        health nothing, because a model that produced no first token would
        have been just as silent on any other key.
        """
        if budget is None:
            return await iterator.__anext__()
        try:
            async with asyncio.timeout(budget) as bound:
                return await iterator.__anext__()
        except TimeoutError as exc:
            if not bound.expired():
                # An upstream timeout of the provider's own, not this bound.
                raise
            raise ExecutionFailure(
                kind=FailureKind.TIMEOUT,
                status_code=504,
                message=(
                    f"Credential {index + 1} produced no first token within "
                    f"{budget:.1f}s of its share of this attempt."
                ),
                retryable=True,
            ) from exc

    async def _stream_with_rotation(
        self,
        request: MessagesRequest,
        input_tokens: int,
        *,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        attempted: set[int] = set()
        last_error: Exception | None = None

        while len(attempted) < len(self._providers):
            index = await self._state.acquire(self._unavailable_now())
            if index < 0:
                # Every credential is benched (cooldown/circuit-open/lockout).
                wait = await self._state.shortest_cooldown_remaining()
                raise ApplicationUnavailableError(
                    "All API keys for this provider are in cooldown. "
                    f"Retry in {max(1, int(wait))}s."
                )
            if index in attempted:
                # The pool handed back a credential this request already tried,
                # so it has nothing better left. Stop rather than reaching past
                # the pool for an untried index, which would bypass the health
                # checks and could dispatch to a locked-out credential.
                break
            # Counts this credential and every one still behind it, and is
            # recomputed per credential, so time an early key did not spend
            # flows forward to the ones after it.
            budget = self._first_token_budget(len(self._providers) - len(attempted))
            attempted.add(index)
            record_credential(index, self._key_label(index))

            iterator = self._providers[index].stream_response(
                request,
                input_tokens,
                request_id=request_id,
                reasoning=reasoning,
            )
            try:
                first_chunk = await self._first_chunk(iterator, index, budget)
            except StopAsyncIteration:
                await self._state.report_success(index)
                return
            except Exception as error:
                last_error = error
                await maybe_await_aclose(iterator)
                rotate = await self._state.report_failure(
                    index, error, model=request.model
                )
                if not rotate:
                    raise
                continue

            settled = False
            try:
                yield first_chunk
                async for chunk in iterator:
                    yield chunk
            except Exception as error:
                # Output has already started, so this request cannot move to
                # another credential -- but the failure still has to count
                # against this one, or a credential that consistently dies
                # mid-stream would never cool down.
                settled = True
                await maybe_await_aclose(iterator)
                await self._state.report_failure(index, error, model=request.model)
                raise
            finally:
                if not settled:
                    await maybe_await_aclose(iterator)
                    # Covers client disconnect and cancellation, where neither
                    # success nor failure is reported: release any half-open
                    # probe so the credential is not benched permanently.
                    self._state.release_probe(index)
            await self._state.report_success(index)
            return

        if last_error is not None:
            raise last_error

    def key_health(self) -> list[dict[str, Any]]:
        """Per-credential health snapshots (index-aligned with api_keys)."""
        metrics = self._state.get_metrics()
        for index, entry in enumerate(metrics):
            entry["index"] = index
            entry["key_label"] = self._key_label(index)
            entry["throttle_remaining"] = self._providers[index].throttle_remaining()
        return metrics
