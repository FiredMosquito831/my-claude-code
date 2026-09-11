"""OpenAI-compatible provider family."""

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .base_url import openai_v1_base_url
from .client_identity import ClientIdentity, identity_headers_for_body
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
from .identity_enforcement import free_quota_reset, observe_identity_enforcement
from .learned_dialect import (
    learned_effort_values,
    learned_named_effort_reasoning,
    profile_with_learned_dialect,
)
from .opencode_identity import (
    OPENCODE_CLIENT_IDENTITY,
    identity_wire_record,
    log_identity,
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
from .response_surface import (
    NPM_SURFACES,
    catalogue_surface,
    resolve_response_surface,
)
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
    # The constant half of a declared identity, or nothing at all. A
    # profile that declares none passes ``None``, which is byte-for-byte
    # what every profile passed before this seam existed.
    identity = resolved.client_identity
    headers = identity.default_headers() if identity is not None else None
    if headers:
        log_identity(provider_id, headers)
    return OpenAIChatProvider(
        config,
        profile=resolved,
        rate_limiter=rate_limiter,
        default_headers=headers,
        provider_id=provider_id,
    )


__all__ = [
    "GENERIC_OPENAI_PROFILE",
    "NO_REASONING",
    "NPM_SURFACES",
    "OPENAI_CHAT_PROFILES",
    "OPENAI_STANDARD_REASONING",
    "OPENCODE_CLIENT_IDENTITY",
    "ChatTemplateReasoning",
    "ClientIdentity",
    "NamedEffortReasoning",
    "OpenAIAsyncCredentialProvider",
    "OpenAIChatProfile",
    "OpenAIChatProvider",
    "OpenAIChatRequestPolicy",
    "ReasoningObject",
    "build_openai_chat_request_body",
    "catalogue_surface",
    "complaint_evidence_snippet",
    "create_openai_chat_provider",
    "free_quota_reset",
    "identity_headers_for_body",
    "identity_wire_record",
    "is_bad_request",
    "learned_effort_values",
    "learned_named_effort_reasoning",
    "matched_token",
    "observe_identity_enforcement",
    "openai_v1_base_url",
    "profile_with_learned_dialect",
    "resolve_response_surface",
    "sampling_parameter_evidence",
    "upstream_complaint",
    "usage_int",
    "validate_extra_body_does_not_override_canonical_fields",
    "validate_extra_body_does_not_override_reasoning_fields",
]
