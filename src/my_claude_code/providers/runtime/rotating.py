"""Provider wrapper that rotates requests across multiple credentials."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.credential_attribution import (
    NO_CREDENTIAL_INDEX,
    NO_CREDENTIAL_LABEL,
    record_credential,
)
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.core.upstream_ladder import record_upstream_try
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.http import maybe_await_aclose


class RotatingProvider(BaseProvider):
    """Fan requests out to one sub-provider per configured credential.

    The rotation policy picks the credential for each request. After that,
    the pool moves to another key only for a failure that is *about the key* --
    an auth rejection, a 429, or a transport fault. A model that answers slowly
    or not at all, a 5xx, a 410 or any other 4xx is not the credential's fault
    and would meet the same answer on every key in the pool, so it is raised
    and the executor's fallback chain tries the next *model* instead.

    No clock of this wrapper's own bounds a credential's turn. An earlier
    version divided the executor's per-attempt share by the untried keys and
    abandoned a credential that produced no first token inside its slice; with
    a three-key pool five models deep that worked out to a 25s timer nobody
    configured, which rotated keys on what was always a model-shaped stall.
    The executor still owns the outer deadline and ends the attempt itself.

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
        rotation engine's -- COOLDOWN (a 429 the provider timed) or
        LOCKED_OUT (an auth rejection). Reading only
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

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        """Every credential in a pool talks to the same upstream endpoint.

        So the first sub-provider's answer is the pool's answer, exactly as it
        already is for ``preflight_stream`` and ``list_model_infos``.
        """
        return self._providers[0].reasoning_dialect(model_id)

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
            # Credentials this request has already spent are steered away from
            # alongside the throttled ones. Rotation used to rely on the
            # failure having benched the key it left behind, which stopped
            # being true once a transport fault costs a credential nothing:
            # ``failover`` would re-pick slot 0 and the loop would give up
            # with the other keys untouched.
            index = await self._state.acquire(
                self._unavailable_now() | frozenset(attempted)
            )
            if index < 0:
                # Every credential is benched (cooldown or auth lockout).
                wait = await self._state.shortest_cooldown_remaining()
                # No key served this attempt, and the request-level baseline
                # (execution.py) has already claimed key 0 with a NULL label,
                # which renders as an ordinary keyless request. Say what
                # actually happened instead.
                record_credential(NO_CREDENTIAL_INDEX, NO_CREDENTIAL_LABEL)
                # A ladder row too, so the modal's try list ends with the
                # reason there was no try: the pool published a wait and this
                # attempt never reached a key.
                record_upstream_try(
                    key_index=NO_CREDENTIAL_INDEX,
                    key_label=NO_CREDENTIAL_LABEL,
                    kind="pool_benched",
                    error_kind="unavailable",
                    retry_after=wait,
                    source="bench",
                )
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
            attempted.add(index)
            record_credential(index, self._key_label(index))

            iterator = self._providers[index].stream_response(
                request,
                input_tokens,
                request_id=request_id,
                reasoning=reasoning,
            )
            try:
                first_chunk = await iterator.__anext__()
            except StopAsyncIteration:
                await self._state.report_success(index)
                return
            except Exception as error:
                last_error = error
                await maybe_await_aclose(iterator)
                # Two independent answers: whether this key's health record
                # moves at all, and whether another key is worth trying. Only
                # a key-shaped failure does the first; a timeout or a 5xx does
                # neither, and raising here is what hands the request to the
                # next model on the chain.
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
                    # Covers client disconnect and cancellation, where neither
                    # success nor failure is reported.
                    await maybe_await_aclose(iterator)
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
