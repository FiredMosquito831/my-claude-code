"""Shared Google behavior for OpenAI-compatible Gemini endpoints."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import connection_id
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

from .signature_cache import GOOGLE_THOUGHT_SIGNATURES
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
        # Restored from the durable store on construction (8.19.0); see
        # ``GeminiProvider`` for the reasoning. The store is keyed per
        # provider AND per credential, so a Vertex instance and an AI Studio
        # instance never see each other's signatures even on one key.
        self._tool_call_extra_content_by_id: dict[str, dict[str, Any]] = (
            GOOGLE_THOUGHT_SIGNATURES.rows_for(self._provider_id, self._credential_id)
        )

    @property
    def _credential_id(self) -> str:
        """Which credential this instance holds -- hashed, never the secret.

        ``connection_id`` of the credential's fingerprint, the rule
        ``core/openai_responses/carrier.py`` states. A Vertex instance often
        has no ``api_key`` at all (it renews an ADC token), which yields the
        stable constant ``connection_id("")`` -- one bucket for "this provider,
        no configured key", which is exactly the population such an instance
        can mint signatures for.
        """

        key = self._config.api_key
        return connection_id(credential_fingerprint(key) if key else "")

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
        evicted = GOOGLE_THOUGHT_SIGNATURES.record(
            self._provider_id,
            self._credential_id,
            tool_call_id,
            extra_content,
        )
        if evicted is not None and evicted[0:2] == (
            self._provider_id,
            self._credential_id,
        ):
            self._tool_call_extra_content_by_id.pop(evicted[2], None)

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
        wire_notes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_openai_chat_request_body(
            request,
            reasoning=reasoning,
            policy=self._profile.request_policy,
            postprocessors=(
                lambda body, _request_data, _policy: apply_google_thought_signatures(
                    body,
                    tool_call_extra_content_by_id=(self._tool_call_extra_content_by_id),
                    wire_notes=wire_notes,
                ),
                *self._profile.request_postprocessors,
            ),
            provider_id=self._provider_id,
        )
