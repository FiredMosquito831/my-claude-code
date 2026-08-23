"""Provider wrapper that rotates requests across multiple credentials."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.credential_attribution import record_credential
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.http import maybe_await_aclose


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
        """The shortest cooldown across credentials; 0 while any key can serve.

        ``BaseProvider`` answers 0 unconditionally, which for a rotating
        provider would have claimed every key was free even with all of them
        rate-limited. Rotation already prefers an unthrottled sub-provider, so
        the value routing needs is the best case, not the first one.
        """
        return min(
            (provider.throttle_remaining() for provider in self._providers),
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
                rotate = await self._state.report_failure(index, error)
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
                await self._state.report_failure(index, error)
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
