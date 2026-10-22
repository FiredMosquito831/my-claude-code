"""The OpenAI-shaped bodies the 7.106.0 lifetime families must leave as they were.

Shared by ``test_prompt_cache_ttl_hosts_equality.py`` and by the one-off run
that wrote ``prompt_cache_ttl_hosts_baseline.json`` **from the 7.105.2 code,
before this change existed** -- which is why this module imports nothing the
change added. The 432 requests of ``test_anthropic_foreign_thinking_equality``
(``cache_control`` everywhere it is legal, every reasoning policy, every
client ``thinking`` object) are built as Chat Completions bodies for each host
the change declares something for: a Claude model and a non-Claude model on
OpenRouter, an Azure OpenAI deployment, and Agnes (a host that caches
automatically). A request the Chat conversion refuses is recorded by the
refusal's type, so a refusal that changed would be a diff too.
"""

from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.open_router import OpenRouterProvider
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from tests.contracts.test_anthropic_foreign_thinking_equality import corpus, digest
from tests.providers.support import passthrough_rate_limiter

#: ``key -> (provider id, the model id that provider is sent)``.
HOSTS: dict[str, tuple[str, str]] = {
    "open_router/claude": ("open_router", "anthropic/claude-haiku-5.5"),
    "open_router/open": ("open_router", "moonshotai/kimi-k2.6"),
    "azure_openai": ("azure_openai", "my-gpt-5-4"),
    "agnes": ("agnes", "agnes-3.0-flash"),
}


def chat_provider(provider_id: str) -> Any:
    config = ProviderConfig(api_key="sk-test", base_url="https://chat.invalid/v1")
    if provider_id == "open_router":
        return OpenRouterProvider(config, rate_limiter=passthrough_rate_limiter())
    return create_openai_chat_provider(
        provider_id,
        config,
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )


def host_request(request: MessagesRequest, model: str) -> MessagesRequest:
    """The corpus request addressed to one host's model, as routing hands it."""

    return request.model_copy(update={"model": model}, deep=True)


def build(provider: Any, request: MessagesRequest, policy: Any) -> Any:
    """The provider's own Chat body, or ``error:<type>`` when it refuses."""

    try:
        return provider._build_request_body(request, reasoning=policy)
    except Exception as exc:
        return f"error:{type(exc).__name__}"


def snapshot() -> dict[str, str]:
    """``host/fixture/policy/thinking -> sha256`` of every body's wire bytes."""

    out: dict[str, str] = {}
    requests = corpus()
    for host, (provider_id, model) in sorted(HOSTS.items()):
        provider = chat_provider(provider_id)
        for key, (request, policy) in sorted(requests.items()):
            out[f"{host}/{key}"] = digest(
                build(provider, host_request(request, model), policy)
            )
    return out
