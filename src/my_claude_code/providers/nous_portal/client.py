"""Nous Portal provider implementation.

The Nous Research inference API is OpenRouter-dialect: its ``/models`` payload
carries ``supported_parameters``/``canonical_slug`` and reasoning is negotiated
with a ``reasoning`` object, so it reuses the shared gateway behaviour.

It differs in one undocumented way: an API-key caller must send a ``tags``
array containing a ``user=`` entry, or the gateway answers ``400 ... Additional
info: missing tags`` before the request reaches the model.
"""

from dataclasses import replace
from typing import Any

from my_claude_code.config.constants import NOUS_PORTAL_USER_TAG_DEFAULT
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openrouter_gateway import (
    OpenRouterGatewayProvider,
    openrouter_gateway_profile,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter


def apply_nous_user_tag(
    body: dict[str, Any],
    request_data: MessagesRequest,
    reasoning: ReasoningPolicy,
) -> None:
    """Attach the ``user=`` tag Nous Portal requires from API-key callers.

    The tag goes inside ``extra_body`` rather than at the top level of ``body``:
    ``body`` is splatted as ``**create_body`` into the OpenAI SDK's
    ``chat.completions.create``, which has no ``tags`` parameter, so a top-level
    key would raise locally instead of reaching the wire. The SDK flattens
    ``extra_body`` into the JSON root, which is where Nous expects it.

    ``setdefault`` throughout: a caller that already supplied its own tags keeps
    them. ``tags`` is not a canonical OpenAI field, so the gateway's extra-body
    validator permits a client to pass it.
    """
    extra_body = body.setdefault("extra_body", {})
    if not isinstance(extra_body, dict):
        raise TypeError("OpenAI extra_body must be an object.")
    extra_body.setdefault("tags", [NOUS_PORTAL_USER_TAG_DEFAULT])


_BASE_PROFILE = openrouter_gateway_profile("NOUS_PORTAL")
_PROFILE = replace(
    _BASE_PROFILE,
    postprocessors=(*_BASE_PROFILE.postprocessors, apply_nous_user_tag),
)


class NousPortalProvider(OpenRouterGatewayProvider):
    """Nous Portal provider using the OpenAI-compatible Chat Completions API."""

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(config, profile=_PROFILE, rate_limiter=rate_limiter)
