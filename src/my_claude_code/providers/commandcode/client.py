"""Command Code dual-protocol Provider API implementation."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.prompt_cache_lifetimes import prompt_cache_gateway_family
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages import (
    ANTHROPIC_GATEWAY_REASONING_DIALECT,
    AnthropicMessagesProvider,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.openai_chat import (
    COMMANDCODE_CHAT_PROFILE,
    OpenAIChatProvider,
)
from my_claude_code.providers.openai_responses import (
    ToolSchemaDialect,
    regex_construct,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import SchemaKeywordRefusal

from .models import (
    commandcode_uses_anthropic_messages,
    extract_commandcode_model_infos,
)

#: What Command Code's Chat validator refuses in a tool schema: a ``pattern``
#: carrying the ``\0`` regex escape, and nothing else that has been measured.
#: Claude Code's newer built-in ``Artifact`` tool constrains ``file_paths``
#: items with ``^[^\0]*$``, and the DeepSeek back end refused every request
#: carrying it -- 445 of 445 refusals on ``deepseek/deepseek-v4.1-flash``,
#: 2026-10-20/21, against 0 of 3,677 successes. 20 of them said why:
#: ``Invalid schema for function 'Artifact': "^[^\\0]*$" is not a "regex"``;
#: the other 425 said only ``invalid request error trace_id: ...``. The user's
#: probe of 2026-10-21 (``INVESTIGATION-COMMANDCODE-DEEPSEEK-RESUMED-400.md``
#: §16) settled it on the real 33-tool catalogue: unmodified, HTTP 400 in
#: 5,668 ms; the same body without only that one pattern, HTTP 200 in 1,220 ms.
#: The six other patterns that catalogue carries -- the ``Artifact`` contract
#: version (in 3,536 accepted requests), the Workflow run id (731), ``after``,
#: ``asset_ids`` and SendMessage's two -- were accepted beside it and stay.
#:
#: Data on this profile, never a model branch: the sweep is the Responses
#: route's own, the tools come back as the same objects when nothing offends,
#: and every other Chat host declares nothing, so Agnes, LongCat and native
#: DeepSeek -- which accept the pattern -- keep it.
COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT = ToolSchemaDialect(
    name="commandcode-chat",
    refused=(
        SchemaKeywordRefusal(
            keyword="pattern", construct=regex_construct("nul_escape")
        ),
    ),
)

_PROFILE = replace(
    COMMANDCODE_CHAT_PROFILE,
    chat_tool_schema_dialect=COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT,
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
            provider_id=COMMANDCODE_PROVIDER_ID,
            # Anthropic's protocol on a host that publishes no cache lifetime
            # (7.105.0): a lifetime only where an override row sets one, and
            # only on the models this delegate serves.
            prompt_cache_family=prompt_cache_gateway_family(),
            declared_reasoning_dialect=ANTHROPIC_GATEWAY_REASONING_DIALECT,
        )

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect | None:
        """Ask whichever protocol family actually serves this model.

        One Command Code catalogue is split across delegates with different
        wire shapes, so the dialect is per model even though nothing about the
        model itself is being read here -- only which delegate will encode it.
        """
        return self._delegate(model_id).reasoning_dialect(model_id)

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._openai.throttle_remaining(model)

    def set_response_observer(
        self, observer: Callable[[Mapping[str, str], int], None] | None
    ) -> None:
        """Install one response-header observer on *both* protocol delegates.

        Command Code is the one provider in the catalogue whose single
        catalogue is served by two classes: the Claude-shaped models go out on
        Anthropic Messages and everything else on Chat Completions (see
        :meth:`_delegate`). A response header is a property of the host, not of
        the protocol the request happened to use, so an observer installed on
        one delegate would report half the user's traffic and -- worse -- would
        look like a host that had simply been quiet.

        Added in 8.4.0 for the generic usage observer. When nothing installs
        one -- which is the default, and was the only state before this release
        -- neither delegate holds an observer and both behave exactly as they
        did, because the seam is nullable on each of them.
        """

        self._anthropic.set_response_observer(observer)
        self._openai.set_response_observer(observer)

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
        if commandcode_uses_anthropic_messages(model_id):
            return self._anthropic
        return self._openai
