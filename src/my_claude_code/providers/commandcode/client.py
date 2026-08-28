"""Command Code dual-protocol Provider API implementation."""

import asyncio
from collections.abc import AsyncIterator

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.openai_chat import (
    NO_REASONING,
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .models import extract_commandcode_model_infos, is_anthropic_messages_model

_PROFILE = OpenAIChatProfile(
    OpenAIChatRequestPolicy(
        provider_name="COMMANDCODE",
        reasoning_replay=ReasoningReplayMode.THINK_TAGS,
    ),
    NO_REASONING,
)


COMMANDCODE_PROVIDER_ID = "commandcode"


class CommandCodeProvider(BaseProvider):
    """Route one Command Code catalog through its documented protocol family."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
    ) -> None:
        super().__init__(config)
        self._rate_limiter = rate_limiter
        self._openai = OpenAIChatProvider(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id=COMMANDCODE_PROVIDER_ID,
        )
        self._anthropic = AnthropicMessagesProvider(
            config,
            provider_name="COMMANDCODE",
            rate_limiter=rate_limiter,
        )

    def throttle_remaining(self) -> float:
        return self._openai.throttle_remaining()

    async def cleanup(self) -> None:
        results = await asyncio.gather(
            self._openai.cleanup(),
            self._anthropic.cleanup(),
            return_exceptions=True,
        )
        error = next(
            (item for item in results if isinstance(item, BaseException)), None
        )
        if error is not None:
            raise error

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset(info.model_id for info in await self.list_model_infos())

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        payload = await self._openai.list_models_payload()
        return extract_commandcode_model_infos(
            payload,
            provider_name="COMMANDCODE",
        )

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        self._delegate(request.model).preflight_stream(request, reasoning=reasoning)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._delegate(request.model).stream_response(
            request,
            input_tokens=input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    def _delegate(self, model_id: str) -> BaseProvider:
        if is_anthropic_messages_model(model_id):
            return self._anthropic
        return self._openai
