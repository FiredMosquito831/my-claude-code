"""Mistral La Plateforme provider implementation (Chat Completions & Responses)."""

from collections.abc import Mapping
from typing import Any, ClassVar

from loguru import logger

from my_claude_code.application.marketplace import MarketplaceChoice
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningDialect,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    MISTRAL_CHAT_PROFILE,
    NamedEffortReasoning,
    OpenAIChatProvider,
    apply_cache_affinity_body,
    build_openai_chat_request_body,
    model_accepts_pdf_for_provider,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import rejected_reasoning_field

from .reasoning import (
    apply_mistral_reasoning_request_shape,
    body_has_mistral_thinking,
    clone_body_without_mistral_reasoning,
    is_mistral_reasoning_rejection,
    normalize_mistral_stream,
    strip_top_p_on_greedy_temperature,
)

_PROFILE = MISTRAL_CHAT_PROFILE
_REQUEST_POLICY = _PROFILE.request_policy
_CACHE_AFFINITY = _PROFILE.cache_affinity

_MISTRAL_EFFORTS = tuple((effort, effort.value) for effort in ReasoningEffort)

MISTRAL_REASONING_ENCODER = NamedEffortReasoning(
    _MISTRAL_EFFORTS,
    disabled_value="none",
)
MISTRAL_REASONING_DIALECT = MISTRAL_REASONING_ENCODER.dialect
"""Mistral wide per-model reasoning dialect (all 6 rungs ``minimal..max`` + ``none``).

Capability gating intersects this wide host dialect with each model's
published/probed capabilities (preserving ``max`` / ``xhigh`` where supported),
and :meth:`OpenAIChatProvider.reasoning_dialect` narrows it per model on any
learned 400/422 field, value, or ``"none"`` rejection.
"""


class MistralProvider(OpenAIChatProvider):
    """Mistral API using ``https://api.mistral.ai/v1/chat/completions``.

    Serves both Mistral credentials. A Studio key (``MISTRAL_API_KEY``) and a
    Vibe plan key (``MISTRAL_VIBE_KEY``) dial the same host with the same
    request shape and differ only in what they draw on, so they are one class
    with one ``provider_id`` argument rather than two copies of the reasoning
    shape and the once-only rejection retry. Copying the class would put that
    retry in two places and let the two drift.

    ``provider_id`` defaults to ``"mistral"``, which is what every call site
    before the second credential passed implicitly, so the Studio instance is
    constructed exactly as it was. It is not decoration: the id keys the
    user's own model-parameter overrides, the learned-facts store and the
    zero-cost table, and it is what the request log records per attempt. Two
    credentials spending two different budgets must not share those.
    """

    _supports_observed_reasoning_replay: ClassVar[bool] = False

    def _declared_reasoning_dialect(self) -> ReasoningDialect:
        """See :data:`MISTRAL_REASONING_DIALECT`."""
        return MISTRAL_REASONING_DIALECT

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
        provider_id: str = "mistral",
    ):
        super().__init__(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id=provider_id,
        )

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
        wire_notes: dict[str, Any] | None = None,
    ) -> dict:
        # ``wire_notes`` is accepted and ignored: Mistral's build records
        # nothing about its body. The keyword is here so the runner can hand
        # every provider the same mapping without a special case.
        del wire_notes
        body = build_openai_chat_request_body(
            request,
            reasoning=reasoning,
            policy=_REQUEST_POLICY,
            postprocessors=(strip_top_p_on_greedy_temperature,),
            provider_id=self._provider_id,
            accepts_documents=model_accepts_pdf_for_provider(
                self._provider_id, request.model
            ),
            suppress_structured_output=self._recovery_memory.structured_output_refused(
                request.model
            ),
        )
        apply_mistral_reasoning_request_shape(
            body,
            reasoning=reasoning,
            dialect=self.reasoning_dialect(request.model),
            strip_history_thinking=self._recovery_memory.history_thinking_refused_for(
                request.model
            ),
        )
        apply_cache_affinity_body(
            body,
            request,
            _PROFILE.cache_affinity,
            unsupported_body_keys=_REQUEST_POLICY.unsupported_body_keys,
        )
        return body

    def _get_retry_request_body(self, error: Exception, body: dict) -> dict | None:
        """Retry once without Mistral reasoning fields when a model rejects them."""
        if not is_mistral_reasoning_rejection(error):
            return None
        retry_body = clone_body_without_mistral_reasoning(body)
        if retry_body is None:
            return None
        logger.warning(
            "MISTRAL_STREAM: retrying without reasoning after upstream rejection"
        )
        return retry_body

    def _stripped_provider_reasoning_field(
        self, error: Exception, body: dict[str, Any]
    ) -> str | None:
        """Identify ``reasoning_effort`` when the rejection blames it over history."""
        if rejected_reasoning_field(error, body) == "reasoning_effort":
            return "reasoning_effort"
        if "reasoning_effort" in body and not body_has_mistral_thinking(body):
            return "reasoning_effort"
        return None

    def _remember_provider_recovery(
        self, body: dict[str, Any], prior_body: dict[str, Any], evidence: str
    ) -> None:
        """Persist ``history_thinking_rejected`` once a thinking-stripped retry succeeds."""
        if not body_has_mistral_thinking(prior_body) or body_has_mistral_thinking(body):
            return
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return
        if self._recovery_memory.remember_history_thinking_refusal(
            model, evidence=evidence
        ):
            logger.warning(
                "MISTRAL_STREAM: history thinking blocks learned as rejected for {} "
                "-- later requests omit them",
                model,
            )

    async def _create_stream(
        self,
        body: dict,
        *,
        surface_label: str = "",
        wire_notes: Mapping[str, Any] | None = None,
        marketplace: MarketplaceChoice | None = None,
        affinity_digest: str | None = None,
    ) -> tuple[Any, dict]:
        # ``marketplace`` is passed through untouched (7.109.0); Mistral
        # declares no marketplace, so it is always ``None`` here.
        stream, final_body = await super()._create_stream(
            body,
            surface_label=surface_label,
            wire_notes=wire_notes,
            marketplace=marketplace,
            affinity_digest=affinity_digest,
        )
        return normalize_mistral_stream(stream), final_body
