"""OpenAI-compatible provider family."""

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .base_url import openai_v1_base_url
from .complaint import (
    complaint_evidence_snippet,
    is_bad_request,
    matched_token,
    sampling_parameter_evidence,
    upstream_complaint,
)
from .extra_body import (
    validate_extra_body_does_not_override_canonical_fields,
    validate_extra_body_does_not_override_reasoning_fields,
)
from .profiles import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OPENAI_STANDARD_REASONING,
    OpenAIChatProfile,
)
from .provider import OpenAIAsyncCredentialProvider, OpenAIChatProvider
from .reasoning import (
    NO_REASONING,
    ChatTemplateReasoning,
    NamedEffortReasoning,
    ReasoningObject,
)
from .request_policy import OpenAIChatRequestPolicy, build_openai_chat_request_body
from .usage import usage_int


def create_openai_chat_provider(
    provider_id: str,
    config: ProviderConfig,
    rate_limiter: ProviderRateLimiter,
    profile: OpenAIChatProfile | None = None,
) -> OpenAIChatProvider:
    """Construct one profile-driven provider."""
    resolved = profile if profile is not None else OPENAI_CHAT_PROFILES.get(provider_id)
    if resolved is None:
        raise KeyError(f"No declarative OpenAI-chat profile for {provider_id!r}")
    return OpenAIChatProvider(
        config,
        profile=resolved,
        rate_limiter=rate_limiter,
        provider_id=provider_id,
    )


__all__ = [
    "GENERIC_OPENAI_PROFILE",
    "NO_REASONING",
    "OPENAI_CHAT_PROFILES",
    "OPENAI_STANDARD_REASONING",
    "ChatTemplateReasoning",
    "NamedEffortReasoning",
    "OpenAIAsyncCredentialProvider",
    "OpenAIChatProfile",
    "OpenAIChatProvider",
    "OpenAIChatRequestPolicy",
    "ReasoningObject",
    "build_openai_chat_request_body",
    "complaint_evidence_snippet",
    "create_openai_chat_provider",
    "is_bad_request",
    "matched_token",
    "openai_v1_base_url",
    "sampling_parameter_evidence",
    "upstream_complaint",
    "usage_int",
    "validate_extra_body_does_not_override_canonical_fields",
    "validate_extra_body_does_not_override_reasoning_fields",
]
