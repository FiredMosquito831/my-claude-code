"""Shared Google behavior for OpenAI-compatible Gemini endpoints."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OpenAIAsyncCredentialProvider,
    OpenAIChatProfile,
    OpenAIChatProvider,
    build_openai_chat_request_body,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .thought_signatures import apply_google_thought_signatures

_MAX_TOOL_CALL_EXTRA_CONTENT_CACHE = 4096


class GoogleOpenAIProvider(OpenAIChatProvider):
    """Shared thought-signature and request behavior for Google Gemini APIs."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        profile: OpenAIChatProfile,
        rate_limiter: ProviderRateLimiter,
        api_key_provider: OpenAIAsyncCredentialProvider | None = None,
        default_headers: Mapping[str, str] | None = None,
        provider_id: str = "",
    ) -> None:
        super().__init__(
            config,
            profile=profile,
            rate_limiter=rate_limiter,
            api_key_provider=api_key_provider,
            default_headers=default_headers,
            provider_id=provider_id,
        )
        self._tool_call_extra_content_by_id: dict[str, dict[str, Any]] = {}

    def _record_tool_call_extra_content(
        self, tool_call_id: str, extra_content: dict[str, Any]
    ) -> None:
        if (
            tool_call_id not in self._tool_call_extra_content_by_id
            and len(self._tool_call_extra_content_by_id)
            >= _MAX_TOOL_CALL_EXTRA_CONTENT_CACHE
        ):
            self._tool_call_extra_content_by_id.pop(
                next(iter(self._tool_call_extra_content_by_id))
            )
        self._tool_call_extra_content_by_id[tool_call_id] = deepcopy(extra_content)

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> dict[str, Any]:
        return build_openai_chat_request_body(
            request,
            reasoning=reasoning,
            policy=self._profile.request_policy,
            postprocessors=(
                lambda body, _request_data, _policy: apply_google_thought_signatures(
                    body,
                    tool_call_extra_content_by_id=(self._tool_call_extra_content_by_id),
                ),
                *self._profile.request_postprocessors,
            ),
            provider_id=self._provider_id,
        )
