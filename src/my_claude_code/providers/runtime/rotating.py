"""Provider wrapper that rotates requests across multiple credentials."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from my_claude_code.application.deadline_hints import limit_hint
from my_claude_code.application.errors import (
    ApplicationUnavailableError,
    ModelRateLimited,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.credential_attribution import (
    NO_CREDENTIAL_INDEX,
    NO_CREDENTIAL_LABEL,
    record_credential,
)
from my_claude_code.core.failures import find_execution_failure
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.core.upstream_ladder import record_upstream_try
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import (
    CredentialRotationState,
    credential_failure_class,
)
from my_claude_code.providers.http import maybe_await_aclose


class RotatingProvider(BaseProvider):
    """Fan requests out to one sub-provider per configured credential.

    The rotation policy picks the credential for each request. After that,
    the pool moves to another key only for a failure that is *about the key* --
    an auth rejection, a 429, or a transport fault. A model that answers slowly
    or not at all, a 5xx, a 410 or any other 4xx is not the credential's fault
    and would meet the same answer on every key in the pool, so it is raised
    and the executor's fallback chain tries the next *model* instead.

    A 429 is the one signal that stopped rotating in 6.20.0, when
    ``routes_around_model`` is on. The pool benches the (key, model) pair and
    raises :class:`~my_claude_code.application.errors.ModelRateLimited`
    without touching another key, because spending the pool on a limit that
    is about the *model* is what the measured incident did: all three keys
    429'd on ``moonshotai/kimi-k3`` inside 0.2s each while ``nemotron``
    answered on key 0 in the same second. ``report_failure`` still runs first
    and still decides health; only what happens after it changed.

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
        provider_id: str = "",
        routes_around_model: bool = False,
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
        # The registry id this pool serves. Carried only so a routed-around
        # 429 can name the provider the executor should look for another
        # model on; nothing else in the wrapper reads it.
        self._provider_id = provider_id
        # False by default so a pool built directly in a test keeps 6.19.0.
        self._routes_around_model = routes_around_model

    @property
    def credential_label(self) -> str | None:
        """No single label applies: the credential is chosen per request."""
        return None

    def _unavailable_now(self, model: str | None = None) -> frozenset[int]:
        """Credentials that are healthy but cannot serve this instant.

        Two reasons now. The provider's own limiter window, as before -- a
        rate-limited credential stays HEALTHY, so rotation used to select it
        anyway and the request sat inside that credential's limiter while an
        idle credential went unused. And, since a 429 is scoped to the (key,
        model) pair, a credential benched for *this* model while healthy for
        every other one. Both windows come from the provider's own response,
        never from a limit invented here.
        """
        benched = (
            frozenset(self._state.model_benched_indexes(model))
            if model
            else frozenset()
        )
        return benched | frozenset(
            index
            for index, provider in enumerate(self._providers)
            if provider.throttle_remaining() > 0
        )

    def _key_label(self, index: int) -> str | None:
        if 0 <= index < len(self._key_labels):
            return self._key_labels[index]
        return None

    def throttle_remaining(self, model: str | None = None) -> float:
        """The shortest wait before any credential can serve; 0 while one can.

        ``BaseProvider`` answers 0 unconditionally, which for a rotating
        provider would have claimed every key was free even with all of them
        rate-limited. Rotation already prefers an unthrottled sub-provider, so
        the value routing needs is the best case, not the first one.

        Three independent things bench a credential and all have to be read.
        The rate limiter is the provider's own throttle window. Health is the
        rotation engine's -- COOLDOWN (a 429 that cost the whole key) or
        LOCKED_OUT (an auth rejection). The third is the (key, model) bench a
        429 installs first, which leaves the slot HEALTHY and is invisible
        unless ``model`` is named. Reading only
        the limiter meant a pool whose every key was health-benched but not
        throttled still reported 0: routing skipped its step-over, committed
        the attempt, and the request paid a full round trip only to be told
        every key was in cooldown.

        ``model`` asks the question for one model. Without it the answer is
        the pool's best case over all models, which is what a caller with no
        model in hand should be told.

        The contract is therefore: 0 whenever any credential can serve, and
        otherwise the shortest wait until one can. Selectability comes from
        the engine rather than per-slot health so the forced-single policies
        agree with what ``acquire`` would actually hand out -- a single-key
        provider serves slot 0 regardless of its health, and must keep
        reporting itself free.
        """
        ready = self._state.selectable_indexes(model)
        if not ready:
            return self._state.bench_remaining_now(model)
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
                self._unavailable_now(request.model) | frozenset(attempted),
                model=request.model,
            )
            if index < 0:
                # Every credential is benched. Two different facts, and the
                # reader's next move differs: the whole pool is in cooldown,
                # or only this model is rate-limited on every key while the
                # provider's other models still answer.
                model_wait = await self._state.shortest_cooldown_remaining(
                    request.model
                )
                pool_wait = await self._state.shortest_cooldown_remaining()
                model_only = pool_wait <= 0 < model_wait
                wait = model_wait if model_only else pool_wait
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
                    kind="pool_benched_model" if model_only else "pool_benched",
                    error_kind="unavailable",
                    retry_after=wait,
                    source="bench",
                )
                if model_only:
                    raise ApplicationUnavailableError(
                        "All API keys for this provider are rate-limited for "
                        f"{request.model}. Retry in {max(1, int(wait))}s, or use "
                        "another model on this provider."
                        f"{limit_hint('RATE_LIMIT_COOLDOWN_SECONDS')}"
                    )
                raise ApplicationUnavailableError(
                    "All API keys for this provider are in cooldown. "
                    f"Retry in {max(1, int(wait))}s."
                    f"{limit_hint('RATE_LIMIT_COOLDOWN_SECONDS')}"
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
                if (
                    self._routes_around_model
                    and credential_failure_class(error) == "rate_limit"
                ):
                    # The pool benched (this key, this model) and stops here
                    # on purpose. Rotating would spend the rest of the pool on
                    # a limit that is about the model: all three keys 429'd on
                    # kimi-k3 inside 0.2s each while nemotron answered on key 0
                    # in the same second. The executor is told which provider
                    # and which model, and goes looking for another model
                    # behind the same key.
                    failure = find_execution_failure(error)
                    raise ModelRateLimited(
                        provider_id=self._provider_id,
                        model=request.model,
                        key_index=index,
                        retry_after=(
                            None if failure is None else failure.retry_after_seconds
                        ),
                        failure=error,
                    ) from error
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

    def stream_on_credential(
        self,
        key_index: int,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        """Stream one request on one named credential, bypassing selection.

        The executor's diagnostic probe has to ask its question of the key
        that just met the 429 -- an answer from a different key says nothing
        about this one. Selection, health accounting and rotation are all
        deliberately skipped: this is a measurement, and the executor decides
        what it means. Nothing else calls it, and no clock lives here; the
        probe's own bound is the executor's, where every deadline already is.
        """
        if not 0 <= key_index < len(self._providers):
            raise IndexError(f"no credential at index {key_index}")
        # Deliberately no ``record_credential``: the request's attribution
        # still belongs to the attempt that failed, and a measurement must
        # not rewrite whose key answered it.
        return self._providers[key_index].stream_response(
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    async def escalate_model_bench_to_key(
        self, key_index: int, model: str, retry_after: float | None
    ) -> bool:
        """Promote a (key, model) bench to a whole-key cooldown.

        Called only when the executor's probe met a 429 on a *different*
        model on this same credential: two models refused at once is the
        evidence that the limit is the key's, and the pool should stop
        offering it. Returns whether the pool holds this index at all.
        """
        if not 0 <= key_index < len(self._providers):
            return False
        await self._state.escalate_to_key_bench(
            key_index,
            model,
            retry_after,
            key_label=self._key_label(key_index),
        )
        return True

    def key_health(self) -> list[dict[str, Any]]:
        """Per-credential health snapshots (index-aligned with api_keys)."""
        metrics = self._state.get_metrics()
        for index, entry in enumerate(metrics):
            entry["index"] = index
            entry["key_label"] = self._key_label(index)
            entry["throttle_remaining"] = self._providers[index].throttle_remaining()
        return metrics
