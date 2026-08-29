"""DeepSeek provider implementation (OpenAI-compatible Chat Completions)."""

from typing import Any

from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    NO_REASONING,
    OpenAIChatProfile,
    OpenAIChatProvider,
    usage_int,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .compat import DEEPSEEK_REQUEST_POLICY, build_deepseek_request_body
from .tool_choice import (
    clone_body_with_required_tool_choice,
    is_deepseek_tool_choice_rejection,
)

_PROFILE = OpenAIChatProfile(
    DEEPSEEK_REQUEST_POLICY,
    # 2026-08-29 audit: this ``NO_REASONING`` is deliberate and is *not* the
    # "profile says nothing, so nothing is sent" defect it resembles.
    # ``DeepSeekProvider`` overrides ``_build_request_body`` and never routes
    # through ``OpenAIChatProfile.request_postprocessors``, so this encoder is
    # never consulted at all; DeepSeek's reasoning is encoded by
    # ``_apply_deepseek_chat_extras`` in ``.compat``, which owns the wire shape
    # together with the tool-follow-up rules that have to disable thinking.
    # Putting an encoder here would be dead configuration that reads as live.
    NO_REASONING,
)


DEEPSEEK_REASONING_DIALECT = ReasoningDialect(
    effort_values=frozenset(ReasoningEffort),
    toggle=True,
    off=True,
    effort_field="reasoning_effort",
    toggle_field="thinking.type",
)
"""DeepSeek's own enum is FCC's ladder, value for value.

It named it itself when rejecting an invalid one: ``none``, ``minimal``,
``low``, ``medium``, ``high``, ``xhigh``, ``max`` -- so the whole
vocabulary is expressible, and ``thinking.type`` carries on/off beside
it. DeepSeek overrides ``_build_request_body`` and never consults the
profile encoder, so this cannot be read off the profile.
"""


class DeepSeekProvider(OpenAIChatProvider):
    """DeepSeek using ``https://api.deepseek.com`` Chat Completions."""

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect:
        """See :data:`DEEPSEEK_REASONING_DIALECT`."""
        return DEEPSEEK_REASONING_DIALECT

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id="deepseek",
        )

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> dict:
        return build_deepseek_request_body(
            request,
            reasoning=reasoning,
            provider_id=self._provider_id,
        )

    def _get_retry_request_body(self, error: Exception, body: dict) -> dict | None:
        """Retry once with a forced named tool_choice downgraded to "required".

        DeepSeek's request-level retry hook is called at most once per
        request (``used_retry_kinds`` in ``OpenAIChatProvider._create_stream``
        gates the "provider_specific" retry kind), so a second rejection of
        "required" itself is not recovered -- it propagates as a failure.
        """
        if not is_deepseek_tool_choice_rejection(error):
            return None
        retry_body = clone_body_with_required_tool_choice(body)
        if retry_body is None:
            return None
        logger.warning(
            "DEEPSEEK_STREAM: retrying with tool_choice downgraded to 'required' "
            "after upstream rejection of forced named tool_choice"
        )
        return retry_body

    def _anthropic_usage_fields(self, usage_info: Any) -> dict[str, int]:
        """Map DeepSeek's hit/miss split onto Anthropic's cache fields.

        ``prompt_cache_hit_tokens`` + ``prompt_cache_miss_tokens`` make up
        ``prompt_tokens``. Reporting the hit count is enough: the streaming
        path subtracts it from ``prompt_tokens`` to get Anthropic's
        ``input_tokens``, which lands exactly on the miss count. Reporting the
        misses again as cache *creation* would count that same slice twice --
        a miss is a token the cache did not serve, not a token written at a
        premium, which is what Anthropic's field means.
        """
        cache_hit_tokens = usage_int(usage_info, "prompt_cache_hit_tokens")
        if cache_hit_tokens is None:
            return {}
        return {"cache_read_input_tokens": cache_hit_tokens}
