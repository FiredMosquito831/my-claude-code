"""Command Code dual-protocol Provider API implementation."""

import asyncio
from collections.abc import AsyncIterator

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.openai_chat import (
    NamedEffortReasoning,
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
    validate_extra_body_does_not_override_canonical_fields,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .models import extract_commandcode_model_infos, is_anthropic_messages_model

_EFFORTS = (
    # Command Code publishes no effort vocabulary of its own: every model row
    # in /v1/models carries a null ``supported_parameters``. The vocabulary
    # below is the one the gateway itself names when it rejects an unknown
    # value -- "Invalid option: expected one of low|medium|high|xhigh|max" --
    # so it is the documented enum for the whole gateway, not a per-model guess.
    # FCC's MINIMAL has no counterpart there ("minimal" is rejected) and folds
    # onto the nearest representable rung.
    (ReasoningEffort.MINIMAL, "low"),
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.MEDIUM, "medium"),
    (ReasoningEffort.HIGH, "high"),
    (ReasoningEffort.XHIGH, "xhigh"),
    (ReasoningEffort.MAX, "max"),
)

_PROFILE = OpenAIChatProfile(
    OpenAIChatRequestPolicy(
        provider_name="COMMANDCODE",
        # The gateway streams reasoning as OpenRouter-style ``reasoning``
        # deltas, never as ``<think>`` tags in ``content``, so assistant
        # history must be replayed through the same field it was received on.
        reasoning_replay=ReasoningReplayMode.REASONING,
        include_extra_body=True,
        extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
    ),
    # ``reasoning_effort`` is the only reasoning knob this gateway parses. A
    # top-level ``reasoning`` object, a ``thinking`` object and
    # ``chat_template_kwargs`` are all accepted and silently discarded -- a
    # deliberately invalid ``reasoning: {"effort": "bogus_value"}`` still
    # returns 200, while an invalid ``reasoning_effort`` returns 400.
    #
    # No disabled value: the enum has no "none"/"off" rung and the gateway
    # 400s on both, so reasoning OFF sends no reasoning field at all.
    #
    # ``enabled_value="max"`` since 5.71.0. It was deliberately omitted in
    # 5.69.0 on the belief that "with nothing sent, the gateway's own default
    # is already its most verbose reasoning setting", so naming a rung would
    # reduce reasoning. A live A/B on 2026-08-29 refutes that outright --
    # identical prompt, ``max_tokens: 3000``, ``deepseek/deepseek-v4-flash``:
    #
    #     bare (no reasoning_effort)  HTTP 200  reasoning_tokens=132
    #     reasoning_effort: "max"     HTTP 200  reasoning_tokens=1046
    #
    # -- eight times the thinking, not less. Without an enabled value this
    # encoder had *nothing to emit* for the one policy per-model gating
    # produces most often here: ``ReasoningPolicy.on()``, control ON with the
    # effort discarded, which is what both ``_drop_controls`` and the
    # toggle-only branch return. Gating logged "enabling thinking" while the
    # body left carrying no reasoning instruction at all, on every Command Code
    # model whose models.dev row publishes no effort control.
    NamedEffortReasoning(_EFFORTS, enabled_value="max"),
    # Delta field, not the ``reasoning_content`` default: the gateway emits
    # ``reasoning`` (plus ``reasoning_details``). Reading the wrong field is
    # why reasoning that did arrive was dropped before reaching the client.
    reasoning_delta_field="reasoning",
    reasoning_delta_fallback_field="reasoning_content",
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
