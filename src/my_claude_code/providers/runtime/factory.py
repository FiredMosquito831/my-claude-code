"""Provider construction from declarative profiles and exceptional adapters."""

import dataclasses
from collections.abc import Callable

from my_claude_code.application.errors import UnknownProviderError
from my_claude_code.config.constants import (
    PROVIDER_RATE_LIMIT_DEFAULT,
    PROVIDER_RATE_WINDOW_DEFAULT,
    PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.provider_catalog import (
    PROVIDER_CATALOG,
    ProviderDescriptor,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
    profile_with_declared_surfaces,
    profile_with_learned_dialect,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .config import build_provider_config
from .opencode_credentials import build_opencode_provider
from .proxy_leg import ProxiedLegRateLimiter
from .proxy_rotating import ProxyRotatingProvider, ProxyRotationState
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

    return AnthropicProvider(config, rate_limiter=rate_limiter, provider_id="anthropic")


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
        provider_id="anthropic_oauth",
        account_id=config.oauth_account_id,
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

    # ``CHATGPT_OAUTH_ACCOUNT_ID`` pins the ``ChatGPT-Account-ID`` **header**,
    # and since 7.30.0 it pins it for the **first account only**: it is a
    # single-valued override from the days when there was one account, it is
    # marked deprecated in the dashboard's help text, and removing it outright
    # would break anyone relying on it today. Every other account sends its
    # own id, which is the only thing that can be right for it.
    pinned = config.oauth_account_id
    header_account_id = (
        settings.chatgpt_oauth_account_id
        if not pinned or pinned == _first_chatgpt_account_id()
        else ""
    )
    return ChatGPTOAuthProvider(
        config,
        rate_limiter=rate_limiter,
        account_id=header_account_id,
        pinned_account_id=pinned,
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
    """Create one provider instance bound to a single credential.

    The proxy seam. With no chain -- which is every provider on a fresh
    install, and every provider whose operator never opens the Proxying page --
    this returns exactly what it has always returned, from
    :func:`_create_leaf_provider` below. With a chain of two or more rungs it
    returns one object that still satisfies ``BaseProvider``, built as a
    fan-out over ``dataclasses.replace(config, proxy=...)``: the same line that
    already fans this provider out per credential, one dimension over.

    Deliberately *below* the credential pool. Everything above keeps receiving
    one provider, and on chain exhaustion receives the same exception object it
    receives today.
    """

    plan = config.proxy_chain
    if plan is None or len(plan.legs) < 2:
        # One rung is a static proxy by another name; zero is no chain at all.
        # Either way nothing rotates and nothing new is constructed.
        return _create_leaf_provider(descriptor, config, settings)

    legs = plan.legs
    labels = tuple(leg.label or DIRECT_PROXY_LABEL for leg in legs)
    connect_timeout = float(
        getattr(settings, "proxy_connect_timeout_seconds", None)
        or PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT
    )

    def build_leg(index: int) -> BaseProvider:
        """One leg's leaf provider, built the first time it is used.

        Index ``len(legs)`` is the direct fallback: the same leaf with no proxy
        at all, which is what a chain with nothing healthy left falls back to.
        Building these lazily is what lets a chain be as long as the operator
        wants -- the eager version of this line was the reason for the old
        twelve-entry cap, since five keys times twelve addresses was sixty
        connection pools built before the first request.
        """

        url = legs[index].url if index < len(legs) else ""
        if not url:
            # The direct rung. Nothing about it is proxied, so it is built
            # from exactly the line every release before 7.19 built it from.
            return _create_leaf_provider(
                descriptor,
                dataclasses.replace(config, proxy=url, proxy_chain=None),
                settings,
            )
        # Two things a proxied leg gets that nothing else does, both handed
        # over as configuration at this one line rather than by editing what
        # the leg is made of:
        #
        # * its own CONNECT timeout, because the provider's is sized for an
        #   origin and a public chain's dead entries are what actually spend
        #   a request's wall clock. Connect only -- read, write and pool are
        #   the provider's, untouched, on every surface.
        # * a limiter that will not dial the same dead address twice; see
        #   ``proxy_leg.ProxiedLegRateLimiter``.
        return _create_leaf_provider(
            descriptor,
            dataclasses.replace(
                config,
                proxy=url,
                proxy_chain=None,
                http_connect_timeout=connect_timeout,
            ),
            settings,
            proxied_leg=True,
        )

    state = ProxyRotationState(
        len(legs),
        plan.policy,
        labels=labels,
        provider_id=descriptor.provider_id,
        scope=plan.scope,
    )
    return ProxyRotatingProvider(
        config,
        build_leg,
        state,
        labels=labels,
        plan=plan,
        provider_id=descriptor.provider_id,
        max_open_legs=int(getattr(settings, "proxy_max_open_legs", 0) or 0),
        max_live_failures=int(getattr(settings, "proxy_max_live_failures", 0) or 0),
    )


def _create_leaf_provider(
    descriptor: ProviderDescriptor,
    config: ProviderConfig,
    settings: Settings,
    *,
    proxied_leg: bool = False,
) -> BaseProvider:
    """Create one provider instance bound to one credential and one address.

    ``proxied_leg`` is set only by the chain fan-out above, and only for a rung
    that has an address. It swaps in the limiter that surfaces a connect
    failure to the proxy pool on the first dial instead of knocking twice on
    an address that is not answering. Everything else about the leaf --
    every window, every bound, every provider class -- is identical either way.
    """
    # ``is None`` rather than ``or``: 0 is a meaningful value for the limit
    # -- it is the shipped default and it means "pace nothing" -- and ``or``
    # read it as unset and substituted 40, which is how the proactive window
    # stayed on for a release that had turned it off.
    limiter_class = ProxiedLegRateLimiter if proxied_leg else ProviderRateLimiter
    rate_limiter = limiter_class(
        rate_limit=(
            PROVIDER_RATE_LIMIT_DEFAULT
            if config.rate_limit is None
            else config.rate_limit
        ),
        rate_window=(
            PROVIDER_RATE_WINDOW_DEFAULT
            if not config.rate_window
            else config.rate_window
        ),
        max_concurrency=config.max_concurrency,
        max_retries=max(0, config.retry_attempts - 1),
        backoff_base_seconds=config.retry_backoff_base_seconds,
        backoff_max_seconds=config.retry_backoff_max_seconds,
        backoff_jitter_seconds=config.retry_backoff_jitter_seconds,
        routes_around_model=config.routes_around_model,
        cooldown=config.rate_limit_cooldown(),
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
        # The same seam for the *endpoints* this host serves. A declaration of
        # Chat Completions alone -- the default, and every entry written before
        # 7.33.0 -- leaves the profile exactly as it was.
        profile = profile_with_declared_surfaces(profile, descriptor.response_surfaces)
        return create_openai_chat_provider(
            descriptor.provider_id, config, rate_limiter, profile=profile
        )
    return create_openai_chat_provider(descriptor.provider_id, config, rate_limiter)


#: The two provider ids whose "credential" is a rotating OAuth token rather
#: than a secret the operator pasted. Their pool is a list of *accounts*, and
#: it lives in a store file, not in the env var -- which is why the
#: ``len(keys) <= 1`` short-circuit below can never see it: both of them
#: always present exactly one non-secret marker key.
_OAUTH_PROVIDER_IDS = frozenset({"anthropic_oauth", "chatgpt_oauth", "openai"})


def _anthropic_oauth_account_ids() -> list[str]:
    """Every stored Claude account id. Never raises, never a secret."""
    try:
        from my_claude_code.providers.anthropic_oauth.credentials import load_accounts

        return [record.id for record in load_accounts(migrate=False) if record.id]
    except Exception:  # pragma: no cover - a store problem is not a page
        return []


def _chatgpt_oauth_account_ids() -> list[str]:
    """Every stored ChatGPT account id. Never raises, never a secret."""
    try:
        from my_claude_code.providers.chatgpt_oauth.credentials import (
            load_chatgpt_accounts,
        )

        return [
            record.id for record in load_chatgpt_accounts(migrate=False) if record.id
        ]
    except Exception:  # pragma: no cover - a store problem is not a page
        return []


def _first_chatgpt_account_id() -> str:
    ids = _chatgpt_oauth_account_ids()
    return ids[0] if ids else ""


def oauth_account_ids(provider_id: str) -> list[str]:
    """The account ids that make up one OAuth provider's pool, in slot order.

    The list order **is** the slot order, exactly as the comma-separated
    ``.env`` value is for an API-key pool, so ``core/credential_rotation.py``
    keys on an integer index without knowing anything about accounts.
    """
    if provider_id == "anthropic_oauth":
        return _anthropic_oauth_account_ids()
    if provider_id in ("chatgpt_oauth", "openai"):
        return _chatgpt_oauth_account_ids()
    return []


def _oauth_account_pool(
    provider_id: str,
) -> Callable[[ProviderDescriptor, ProviderConfig, Settings], BaseProvider] | None:
    """The OAuth fan-out for this provider, or ``None`` when there is none.

    Returns ``None`` -- so construction falls through to the line it has
    always taken -- for every non-OAuth provider, and for an OAuth provider
    with fewer than two accounts. **One account keeps today's shape exactly**:
    a single leaf provider, no ``RotatingProvider``, no state, no labels. That
    is what keeps every single-account test green and the blast radius of this
    change the size of the feature rather than the size of the file.
    """
    if provider_id not in _OAUTH_PROVIDER_IDS:
        return None
    account_ids = oauth_account_ids(provider_id)
    if len(account_ids) < 2:
        return None

    def build(
        descriptor: ProviderDescriptor,
        config: ProviderConfig,
        settings: Settings,
    ) -> BaseProvider:
        providers = [
            _create_single_provider(
                descriptor,
                dataclasses.replace(
                    config,
                    credential_rotation="single",
                    oauth_account_id=account_id,
                ),
                settings,
            )
            for account_id in account_ids
        ]
        state = CredentialRotationState(
            len(providers),
            config.credential_rotation,
            rate_limit_seconds=config.rate_limit_cooldown_seconds,
            lockout_tiers=config.lockout_tiers,
            model_bench_escalation=config.credential_model_bench_escalation,
            cooldown=config.rate_limit_cooldown(),
        )
        # The label channel. ``mask_key_label`` of a rotating OAuth token is
        # meaningless -- it changes on every refresh, so the name the operator
        # gave the account would detach from it -- so the account id is the
        # label the pool, the health rows and the request log see, and
        # ``api/credential_display`` joins it to the name.
        return RotatingProvider(
            config,
            providers,
            state,
            key_labels=tuple(account_ids),
            provider_id=descriptor.provider_id,
            routes_around_model=config.routes_around_model,
        )

    return build


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

    # The one fan-out that is not about credentials the operator typed. Zen's
    # free tier is metered per key and the vendor's own client spends a shared
    # anonymous one; ``build_opencode_provider`` returns ``None`` -- and every
    # line below runs exactly as it did in 7.33.0 -- for every other provider
    # and whenever the operator opted out of it.
    opencode = build_opencode_provider(
        descriptor, settings, _create_credential_pool, _create_single_provider
    )
    if opencode is not None:
        return opencode

    config = build_provider_config(descriptor, settings)
    return _create_credential_pool(descriptor, config, settings)


def _create_credential_pool(
    descriptor: ProviderDescriptor,
    config: ProviderConfig,
    settings: Settings,
) -> BaseProvider:
    """One provider over the credentials its configuration names.

    Lifted out of :func:`create_provider` unchanged so the OpenCode split can
    build its paid side from the very same lines, rather than from a second
    copy of them that could drift.
    """
    provider_id = descriptor.provider_id
    oauth_pool = _oauth_account_pool(provider_id)
    if oauth_pool is not None:
        return oauth_pool(descriptor, config, settings)
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
        cooldown=config.rate_limit_cooldown(),
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
