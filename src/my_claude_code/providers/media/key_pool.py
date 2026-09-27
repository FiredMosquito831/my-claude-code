"""The media copy of the credential pool's loop.

``providers/runtime/rotating.py`` ``RotatingProvider._stream_with_rotation``
line for line in effect, over media nodes instead of chat sub-providers, with a
``CredentialRotationState`` of its OWN: the frozen state class is imported and
instantiated fresh, never shared with the chat pool, so a 403 on an image
endpoint cannot lock the key out for chat (user decision 2026-09-26 03:38 #4).

The rules it keeps: rotate only on a failure that is about the key (auth, 429,
bare 402, a transport fault -- ``report_failure`` decides); with
``RATE_LIMIT_ROUTES_AROUND_MODEL`` a 429 benches the (key, model) pair and
raises :class:`ModelRateLimited` without touching another key; nothing is
retried once the first chunk has been yielded; an exhausted pool raises the
same three messages the chat pool raises.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from my_claude_code.application.deadline_hints import limit_hint, providers_hint
from my_claude_code.application.errors import (
    ApplicationUnavailableError,
    ModelRateLimited,
)
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaChunk,
    MediaRequest,
)
from my_claude_code.core.credential_attribution import (
    NO_CREDENTIAL_INDEX,
    NO_CREDENTIAL_LABEL,
    record_credential,
)
from my_claude_code.core.failures import find_execution_failure
from my_claude_code.core.upstream_ladder import record_upstream_try
from my_claude_code.providers.credential_rotation import (
    CredentialRotationState,
    credential_failure_class,
)
from my_claude_code.providers.http import maybe_await_aclose

from .leaf import MediaLeaf, MediaNode


class MediaKeyPool:
    """Fan one provider's media requests out over its keys."""

    def __init__(
        self,
        providers: Sequence[MediaNode],
        state: CredentialRotationState,
        *,
        key_labels: Sequence[str] = (),
        provider_id: str = "",
        routes_around_model: bool = False,
    ) -> None:
        if not providers:
            raise ValueError("MediaKeyPool requires at least one sub-provider")
        self._providers = tuple(providers)
        self._state = state
        self._key_labels = tuple(key_labels)
        self._provider_id = provider_id
        self._routes_around_model = routes_around_model

    @property
    def credential_label(self) -> str | None:
        return None

    @property
    def state(self) -> CredentialRotationState:
        return self._state

    def _unavailable_now(self, model: str | None = None) -> frozenset[int]:
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

    def supports(self, request: MediaRequest) -> bool:
        return self._providers[0].supports(request)

    def leaf_for(self, key_index: int, proxy_label: str | None) -> MediaLeaf | None:
        """Key ``key_index``'s own leaf (on its leg ``proxy_label``), or ``None``."""
        if not 0 <= key_index < len(self._providers):
            return None
        return self._providers[key_index].leaf_for(0, proxy_label)

    def preflight(self, attempt: MediaAttempt) -> None:
        self._providers[0].preflight(attempt)

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
            raise ExceptionGroup("One or more media key cleanups failed", errors)

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]:
        return self._execute_with_rotation(attempt, request_id=request_id)

    async def _execute_with_rotation(
        self, attempt: MediaAttempt, *, request_id: str | None
    ) -> AsyncIterator[MediaChunk]:
        model = attempt.resolved.provider_model
        attempted: set[int] = set()
        last_error: Exception | None = None

        while len(attempted) < len(self._providers):
            index = await self._state.acquire(
                self._unavailable_now(model) | frozenset(attempted),
                model=model,
            )
            if index < 0:
                model_wait = await self._state.shortest_cooldown_remaining(model)
                pool_wait = await self._state.shortest_cooldown_remaining()
                model_only = pool_wait <= 0 < model_wait
                wait = model_wait if model_only else pool_wait
                record_credential(NO_CREDENTIAL_INDEX, NO_CREDENTIAL_LABEL)
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
                        f"{model}. Retry in {max(1, int(wait))}s, or use "
                        "another model on this provider."
                        f"{limit_hint('RATE_LIMIT_COOLDOWN_SECONDS')}"
                    )
                if self._state.benched_only_for_credits():
                    raise ApplicationUnavailableError(
                        "All API keys for this provider reported exhausted "
                        f"credits.{providers_hint()}"
                    )
                raise ApplicationUnavailableError(
                    "All API keys for this provider are in cooldown. "
                    f"Retry in {max(1, int(wait))}s."
                    f"{limit_hint('RATE_LIMIT_COOLDOWN_SECONDS')}"
                )
            if index in attempted:
                break
            attempted.add(index)
            record_credential(index, self._key_label(index))

            iterator = self._providers[index].execute(attempt, request_id=request_id)
            try:
                first_chunk = await anext(iterator)
            except StopAsyncIteration:
                await self._state.report_success(index)
                return
            except Exception as error:
                last_error = error
                await maybe_await_aclose(iterator)
                rotate = await self._state.report_failure(index, error, model=model)
                if (
                    self._routes_around_model
                    and credential_failure_class(error) == "rate_limit"
                ):
                    failure = find_execution_failure(error)
                    raise ModelRateLimited(
                        provider_id=self._provider_id,
                        model=model,
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
                settled = True
                await maybe_await_aclose(iterator)
                await self._state.report_failure(index, error, model=model)
                raise
            finally:
                if not settled:
                    await maybe_await_aclose(iterator)
            await self._state.report_success(index)
            return

        if last_error is not None:
            raise last_error

    async def escalate_model_bench_to_key(
        self, key_index: int, model: str, retry_after: float | None
    ) -> bool:
        """Promote a (key, model) bench to a whole-key cooldown (media books)."""
        if not 0 <= key_index < len(self._providers):
            return False
        await self._state.escalate_to_key_bench(
            key_index,
            model,
            retry_after,
            key_label=self._key_label(key_index),
        )
        return True

    def key_throttle_remaining(self, key_index: int) -> float:
        """The reactive block on one key's own limiter; 0 when free."""
        if not 0 <= key_index < len(self._providers):
            return 0.0
        return self._providers[key_index].throttle_remaining()

    def key_health(self) -> list[dict[str, Any]]:
        metrics = self._state.get_metrics()
        for index, entry in enumerate(metrics):
            entry["index"] = index
            entry["key_label"] = self._key_label(index)
            entry["throttle_remaining"] = self._providers[index].throttle_remaining()
        return metrics
