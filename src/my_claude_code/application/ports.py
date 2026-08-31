"""Typed capabilities consumed by application use cases."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.reasoning import ReasoningDialect, ReasoningPolicy

from .model_metadata import ModelReasoningCapability, ProviderModelInfo


class ProviderPort(Protocol):
    """Minimal provider capability required to execute one request."""

    @property
    def credential_label(self) -> str | None:
        """Masked label of the credential in use, or ``None`` if per-request."""
        ...

    def throttle_remaining(self, model: str | None = None) -> float:
        """Seconds this provider is in rate-limit cooldown for, 0 when free.

        Routing reads this so a chain can step over a provider that is only
        going to sleep inside its own limiter. Every provider reports it
        already; nothing above the provider layer used to look.

        ``model`` narrows the question to one model, because a pooled
        provider's 429 is scoped to the (key, model) pair. Providers with a
        single credential and one limiter ignore it; omitting it asks for the
        provider's best case over all models.
        """
        ...

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy,
    ) -> None: ...

    def stream_response(
        self,
        request: MessagesRequest,
        *,
        input_tokens: int,
        request_id: str,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class PooledCredentialPort(Protocol):
    """A provider that serves one request from a pool of credentials.

    Kept separate from :class:`ProviderPort` on purpose: only a rotating
    provider can answer either question, and every single-credential provider
    would otherwise have to grow two methods it can say nothing about. The
    executor asks with ``isinstance`` and does nothing when the answer is no,
    which is the same "skip the probe" branch a provider with no configured
    route already takes.
    """

    def stream_on_credential(
        self,
        key_index: int,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = ...,
    ) -> AsyncIterator[str]:
        """Stream one request on one named credential, bypassing selection."""
        ...

    async def escalate_model_bench_to_key(
        self, key_index: int, model: str, retry_after: float | None
    ) -> bool:
        """Promote a (key, model) bench to a whole-key cooldown."""
        ...


ProviderResolver = Callable[[str], ProviderPort]


class RequestRuntimeLease(Protocol):
    """One provider generation retained for a complete API response."""

    @property
    def generation_id(self) -> int: ...

    @property
    def settings(self) -> Settings: ...

    def is_provider_cached(self, provider_id: str) -> bool: ...

    def resolve_provider(self, provider_id: str) -> ProviderPort: ...

    async def release(self) -> None: ...

    async def __aenter__(self) -> RequestRuntimeLease: ...

    async def __aexit__(self, *_exc: object) -> None: ...


class RequestRuntimePort(Protocol):
    """Provider generation and model metadata required by application requests."""

    async def acquire(self) -> RequestRuntimeLease: ...

    def current_settings(self) -> Settings: ...

    def cached_model_supports_thinking(
        self, provider_id: str, model_id: str
    ) -> bool | None: ...

    def cached_model_supports_vision(
        self, provider_id: str, model_id: str
    ) -> bool | None: ...

    def model_reasoning_capability(
        self, provider_id: str, model_id: str
    ) -> ModelReasoningCapability | None: ...

    def model_reasoning_dialect(
        self, provider_id: str, model_id: str
    ) -> ReasoningDialect | None: ...

    def model_output_limit(self, provider_id: str, model_id: str) -> int | None: ...

    def model_context_length(self, provider_id: str, model_id: str) -> int | None: ...

    def cached_prefixed_model_infos(self) -> tuple[ProviderModelInfo, ...]: ...


@dataclass(frozen=True, slots=True)
class StopResult:
    """Implementation-neutral result retaining the existing ``/stop`` variants."""

    cancelled_count: int | None = None
    source: str | None = None


class TaskController(Protocol):
    """Stop managed work without exposing messaging or CLI resources."""

    async def stop_all(self) -> StopResult | None: ...
