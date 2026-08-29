"""Provider test doubles with explicit limiter ownership."""

from collections.abc import Callable
from typing import Any

from my_claude_code.application.reasoning import client_reasoning_policy
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.openai_chat import (
    OpenAIChatProvider,
    create_openai_chat_provider,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

REASONING_DEFAULT = ReasoningPolicy.provider_default()
REASONING_ON = ReasoningPolicy.on()
REASONING_OFF = ReasoningPolicy.off()


class PassthroughProviderRateLimiter(ProviderRateLimiter):
    """Skip retry timing while retaining the real concurrency context manager."""

    def __init__(self) -> None:
        super().__init__(
            rate_limit=1_000_000,
            rate_window=1.0,
            max_concurrency=1_000,
        )

    async def execute_with_retry(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        kwargs.pop("provider_failure_override", None)
        return await fn(*args, **kwargs)


class ImmediateRetryProviderRateLimiter(ProviderRateLimiter):
    """Run the real retry policy without exercising wall-clock backoff."""

    async def execute_with_retry(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        kwargs.update(base_delay=0.0, max_delay=0.0, jitter=0.0)
        return await super().execute_with_retry(fn, *args, **kwargs)

    def extend_reactive_block(self, seconds: float) -> None:
        """Leave reactive timing to the limiter's dedicated unit tests."""
        del seconds


#: What a stock install configures: RATE_LIMIT_COOLDOWN_SECONDS and the
#: default auth lockout ladder. Tests that care about either value pass their
#: own instead, which is the point of the keywords being required.
DEFAULT_RATE_LIMIT_SECONDS = 60.0
DEFAULT_LOCKOUT_TIERS = (300.0, 3600.0, 86400.0)


def rotation_state(
    key_count: int,
    policy: str = "single",
    *,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    lockout_tiers: tuple[float, ...] = DEFAULT_LOCKOUT_TIERS,
    clock: Callable[[], float] | None = None,
) -> CredentialRotationState:
    """Build a rotation pool with the settings a default install would give it."""
    if clock is None:
        return CredentialRotationState(
            key_count,
            policy,
            rate_limit_seconds=rate_limit_seconds,
            lockout_tiers=lockout_tiers,
        )
    return CredentialRotationState(
        key_count,
        policy,
        rate_limit_seconds=rate_limit_seconds,
        lockout_tiers=lockout_tiers,
        clock=clock,
    )


def passthrough_rate_limiter() -> ProviderRateLimiter:
    """Return a fresh limiter test double for one provider instance."""
    return PassthroughProviderRateLimiter()


def profiled_provider(
    provider_id: str,
    config: ProviderConfig,
    *,
    rate_limiter: ProviderRateLimiter | None = None,
) -> OpenAIChatProvider:
    """Construct one declarative provider for a focused behavior test."""
    return create_openai_chat_provider(
        provider_id,
        config,
        rate_limiter or passthrough_rate_limiter(),
    )


def retrying_rate_limiter() -> ProviderRateLimiter:
    """Return a limiter that exercises retry policy without elapsed time."""
    return ImmediateRetryProviderRateLimiter(
        rate_limit=1_000_000,
        rate_window=1.0,
        max_concurrency=1_000,
    )


def reasoning_for(request: MessagesRequest) -> ReasoningPolicy:
    """Resolve provider-test input through the production client boundary."""

    return client_reasoning_policy(request)
