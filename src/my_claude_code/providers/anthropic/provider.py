"""First-party Anthropic Messages API provider (Console API key).

This is the authentication path Anthropic documents for software that calls
Claude on a user's behalf: an API key issued by the Claude Console, billed per
token. See ``docs/ANTHROPIC-SUBSCRIPTION.md`` for why the subscription OAuth
credential is a separate provider with its own warning.
"""

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages import (
    AnthropicMessagesAuth,
    AnthropicMessagesProvider,
    ApiKeyAuth,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .models import extract_anthropic_model_infos

PROVIDER_NAME = "ANTHROPIC"


class AnthropicProvider(BaseProvider):
    """Stream native Anthropic Messages against ``api.anthropic.com``."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
        auth: AnthropicMessagesAuth | None = None,
        provider_name: str = PROVIDER_NAME,
        extra_headers: dict[str, str] | None = None,
        body_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(config)
        self._provider_name = provider_name
        self._base_url = config.base_url.rstrip("/")
        self._rate_limiter = rate_limiter
        self._auth = auth if auth is not None else ApiKeyAuth(config.api_key)
        self._extra_headers = dict(extra_headers or {})
        self._messages = AnthropicMessagesProvider(
            config,
            provider_name=provider_name,
            rate_limiter=rate_limiter,
            auth=self._auth,
            extra_headers=self._extra_headers,
            body_transform=body_transform,
        )
        self._client = httpx.AsyncClient(
            proxy=config.proxy or None,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        """Forward the Messages adapter's dialect: it is what builds the body.

        Without this the composed adapter's dialect was invisible on the
        ``anthropic`` provider id -- gating and the Models page both saw
        ``None`` -- and the fleet's only ``adaptive`` channel went unreported.
        """
        return self._messages.reasoning_dialect(model_id)

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._rate_limiter.remaining_wait()

    async def cleanup(self) -> None:
        try:
            await self._messages.cleanup()
        finally:
            await self._client.aclose()

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset(info.model_id for info in await self.list_model_infos())

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        payload = await self._list_models_payload()
        return extract_anthropic_model_infos(
            payload,
            provider_name=self._provider_name,
        )

    async def _list_models_payload(self) -> Any:
        headers = dict(self._extra_headers)
        headers.update(await self._auth.headers())
        try:
            response = await self._client.get(
                f"{self._base_url}/models",
                headers=headers,
                params={"limit": 1000},
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:
            raise classify_provider_failure(
                error,
                provider_name=self._provider_name,
                read_timeout_s=self._config.http_read_timeout,
                request_id=None,
                mark_rate_limited=self._rate_limiter.extend_reactive_block,
                cooldown_seconds=self._config.rate_limit_cooldown_seconds,
                mark_rate_limited_enabled=not self._config.routes_around_model,
            ) from error

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._messages.preflight_stream(request, reasoning=reasoning)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._messages.stream_response(
            request,
            input_tokens=input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )
