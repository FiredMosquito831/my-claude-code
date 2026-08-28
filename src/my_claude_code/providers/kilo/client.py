"""Kilo AI Gateway provider implementation.

The Kilo gateway serves an OpenRouter-dialect ``/models`` payload
(``supported_parameters``, ``top_provider``, ``pricing``) and accepts the
``reasoning`` object, so it reuses the shared gateway behaviour.
"""

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openrouter_gateway import (
    OpenRouterGatewayProvider,
    openrouter_gateway_profile,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

_PROFILE = openrouter_gateway_profile("KILO")


class KiloProvider(OpenRouterGatewayProvider):
    """Kilo AI Gateway provider using the OpenAI-compatible Chat Completions API."""

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id="kilo",
        )
