"""Provider construction from declarative profiles and exceptional adapters."""

import dataclasses
from collections.abc import Callable

from my_claude_code.application.errors import UnknownProviderError
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.provider_catalog import (
    PROVIDER_CATALOG,
    ProviderDescriptor,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    profile_with_learned_dialect,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .config import build_provider_config
from .rotating import RotatingProvider

ProviderFactory = Callable[
    [ProviderConfig, Settings, ProviderRateLimiter], BaseProvider
]


def _create_nvidia_nim(
    config: ProviderConfig,
    settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.nvidia_nim import NvidiaNimProvider

    return NvidiaNimProvider(
        config,
        nim_settings=settings.nim,
        rate_limiter=rate_limiter,
    )


def _create_open_router(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.open_router import OpenRouterProvider

    return OpenRouterProvider(config, rate_limiter=rate_limiter)


def _create_nous_portal(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.nous_portal import NousPortalProvider

    return NousPortalProvider(config, rate_limiter=rate_limiter)


def _create_kilo(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.kilo import KiloProvider

    return KiloProvider(config, rate_limiter=rate_limiter)


def _create_anthropic(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.anthropic import AnthropicProvider

    return AnthropicProvider(config, rate_limiter=rate_limiter)


def _create_anthropic_oauth(
    config: ProviderConfig,
    settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.anthropic_oauth import AnthropicOAuthProvider

    return AnthropicOAuthProvider(
        config,
        rate_limiter=rate_limiter,
        require_claude_code_cli=bool(
            getattr(settings, "anthropic_oauth_require_claude_code", True)
        ),
    )


def _create_commandcode(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.commandcode import CommandCodeProvider

    return CommandCodeProvider(config, rate_limiter=rate_limiter)


def _create_mistral(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.mistral import MistralProvider

    return MistralProvider(config, rate_limiter=rate_limiter)


def _create_deepseek(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.deepseek import DeepSeekProvider

    return DeepSeekProvider(config, rate_limiter=rate_limiter)


def _create_lmstudio(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.lmstudio import LMStudioProvider

    return LMStudioProvider(config, rate_limiter=rate_limiter)


def _create_cloudflare(
    config: ProviderConfig,
    settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.cloudflare import CloudflareProvider

    return CloudflareProvider(
        config,
        account_id=settings.cloudflare_account_id,
        rate_limiter=rate_limiter,
    )


def _create_gemini(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.gemini import GeminiProvider

    return GeminiProvider(config, rate_limiter=rate_limiter)


def _create_vertex(
    config: ProviderConfig,
    settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.vertex import VertexProvider

    return VertexProvider(
        config,
        project_id=_required_setting(settings, "vertex_project_id"),
        location=settings.vertex_location,
        rate_limiter=rate_limiter,
    )


def _create_github_models(
    config: ProviderConfig,
    _settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.github_models import GitHubModelsProvider

    return GitHubModelsProvider(config, rate_limiter=rate_limiter)


def _create_chatgpt_oauth(
    config: ProviderConfig,
    settings: Settings,
    rate_limiter: ProviderRateLimiter,
) -> BaseProvider:
    from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider

    # The ``openai`` connected-account alias ships the Codex backend root
    # (``https://chatgpt.com/backend-api/codex``) as its base URL, matching
    # upstream. ``ChatGPTOAuthProvider`` appends ``/codex/responses`` itself,
    # so the trailing ``/codex`` must not be doubled.
    base_url = config.base_url.rstrip("/")
    if base_url.endswith("/codex"):
        base_url = base_url[: -len("/codex")]
    if base_url != config.base_url:
        config = dataclasses.replace(config, base_url=base_url)

    return ChatGPTOAuthProvider(
        config,
        rate_limiter=rate_limiter,
        account_id=settings.chatgpt_oauth_account_id,
    )


_SPECIAL_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "nvidia_nim": _create_nvidia_nim,
    "open_router": _create_open_router,
    "nous_portal": _create_nous_portal,
    "kilo": _create_kilo,
    "anthropic": _create_anthropic,
    "anthropic_oauth": _create_anthropic_oauth,
    "commandcode": _create_commandcode,
    "mistral": _create_mistral,
    "deepseek": _create_deepseek,
    "lmstudio": _create_lmstudio,
    "cloudflare": _create_cloudflare,
    "gemini": _create_gemini,
    "vertex": _create_vertex,
    "github_models": _create_github_models,
    "chatgpt_oauth": _create_chatgpt_oauth,
    # ``openai`` is a connected-account alias of the ChatGPT/Codex OAuth backend;
    # it routes through the same provider construction.
    "openai": _create_chatgpt_oauth,
}


def _required_setting(settings: Settings, attr_name: str) -> str:
    value = getattr(settings, attr_name, None)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"Provider config did not validate {attr_name!r}")
    return value


_profiled_ids = set(OPENAI_CHAT_PROFILES)
_special_ids = set(_SPECIAL_PROVIDER_FACTORIES)
if _profiled_ids & _special_ids or _profiled_ids | _special_ids != set(
    PROVIDER_CATALOG
):
    raise AssertionError(
        "Every provider must have exactly one construction owner: "
        f"profiles={_profiled_ids!r} special={_special_ids!r} "
        f"catalog={set(PROVIDER_CATALOG)!r}"
    )


def _create_single_provider(
    descriptor: ProviderDescriptor,
    config: ProviderConfig,
    settings: Settings,
) -> BaseProvider:
    """Create one provider instance bound to a single credential."""
    rate_limiter = ProviderRateLimiter(
        rate_limit=config.rate_limit or 40,
        rate_window=config.rate_window or 60.0,
        max_concurrency=config.max_concurrency,
        max_retries=max(0, config.retry_attempts - 1),
        backoff_base_seconds=config.retry_backoff_base_seconds,
        backoff_max_seconds=config.retry_backoff_max_seconds,
        backoff_jitter_seconds=config.retry_backoff_jitter_seconds,
        routes_around_model=config.routes_around_model,
    )
    factory = _SPECIAL_PROVIDER_FACTORIES.get(descriptor.provider_id)
    if factory is not None:
        return factory(config, settings, rate_limiter)
    if descriptor.dynamic:
        profile = OPENAI_CHAT_PROFILES.get(
            descriptor.provider_id, GENERIC_OPENAI_PROFILE
        )
        # The declaration seam. A static profile writes its effort table by
        # hand; a probed one arrives on the descriptor and is folded in here,
        # producing the same ``NamedEffortReasoning`` a declaration would.
        # Nothing past this line knows which of the two it got.
        if descriptor.reasoning_effort_enum:
            profile = profile_with_learned_dialect(
                profile, descriptor.reasoning_effort_enum
            )
        return create_openai_chat_provider(
            descriptor.provider_id, config, rate_limiter, profile=profile
        )
    return create_openai_chat_provider(descriptor.provider_id, config, rate_limiter)


def create_provider(provider_id: str, settings: Settings) -> BaseProvider:
    """Create a provider instance for a supported provider id.

    When multiple credentials are configured (comma-separated in the provider's
    key env var), one sub-provider is built per key and wrapped in a
    :class:`RotatingProvider` that applies the configured rotation policy.
    """
    descriptors = get_provider_registry().all_descriptors()
    descriptor = descriptors.get(provider_id)
    if descriptor is None:
        raise UnknownProviderError.for_provider(provider_id, descriptors)

    config = build_provider_config(descriptor, settings)
    keys = config.api_keys or ((config.api_key,) if config.api_key else ())
    if len(keys) <= 1:
        return _create_single_provider(descriptor, config, settings)

    providers = [
        _create_single_provider(
            descriptor,
            dataclasses.replace(
                config,
                api_key=key,
                api_keys=(key,),
                credential_rotation="single",
            ),
            settings,
        )
        for key in keys
    ]
    state = CredentialRotationState(
        len(providers),
        config.credential_rotation,
        rate_limit_seconds=config.rate_limit_cooldown_seconds,
        lockout_tiers=config.lockout_tiers,
        model_bench_escalation=config.credential_model_bench_escalation,
    )
    labels = tuple(mask_key_label(key) for key in keys)
    return RotatingProvider(
        config,
        providers,
        state,
        key_labels=labels,
        provider_id=descriptor.provider_id,
        routes_around_model=config.routes_around_model,
    )
