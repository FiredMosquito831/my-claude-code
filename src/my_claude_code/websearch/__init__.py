"""Web search providers: adapters, key rotation, registry, and analytics seam."""

from .base import BaseWebSearchProvider, WebSearchProviderConfig
from .errors import (
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from .registry import (
    SearchOutcome,
    SearchRecorder,
    active_provider,
    build_provider,
    build_providers,
    resolve_provider_id,
    search,
    search_with_logging,
)
from .rotation import (
    ROTATION_POLICIES,
    KeyPool,
    default_rotation_policy,
    mask_key_label,
)

__all__ = [
    "ROTATION_POLICIES",
    "BaseWebSearchProvider",
    "KeyPool",
    "SearchOutcome",
    "SearchRecorder",
    "WebSearchAuthError",
    "WebSearchConfigError",
    "WebSearchError",
    "WebSearchInvalidRequestError",
    "WebSearchProviderConfig",
    "WebSearchQuotaError",
    "WebSearchRateLimitError",
    "WebSearchUpstreamError",
    "active_provider",
    "build_provider",
    "build_providers",
    "default_rotation_policy",
    "mask_key_label",
    "resolve_provider_id",
    "search",
    "search_with_logging",
]
