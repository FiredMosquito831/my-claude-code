"""Build and keep every provider's media stack.

The factory shape of ``providers/runtime/factory.py`` (key pool over per-key
nodes, a proxy chain over lazily built legs, a limiter per leaf built from the
provider configuration), copied for media and fed from the same
``build_provider_config`` so a key, a base URL, a proxy chain or a retry setting
means the same thing on both sides.

The stacks outlive requests -- a key benched by a 429 must stay benched for the
next request -- and are rebuilt, with fresh books, when the provider's resolved
configuration changes, the way a chain save rebuilds the chat provider. A
replaced stack is closed at shutdown rather than immediately, so a request
still holding it finishes on the client it started with.

An accepted video job is read through the same stacks (``job_client``): the
key that accepted it is found again by its fingerprint -- never by position
alone, since the operator may have reordered or removed keys since -- and the
job's calls go out through that key's own leaf.
"""

import dataclasses

import httpx
from loguru import logger

from my_claude_code.application.errors import UnknownProviderError
from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.media.ports import (
    MediaJobClient,
    MediaProviderResolver,
)
from my_claude_code.config.constants import (
    PROVIDER_RATE_LIMIT_DEFAULT,
    PROVIDER_RATE_WINDOW_DEFAULT,
    PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT,
)
from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.config.credentials import mask_key_label, mask_proxy_label
from my_claude_code.config.media_surfaces import MediaSurface
from my_claude_code.config.provider_catalog import ProviderDescriptor
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.providers.base import MaskedRefusalPlan, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.config import build_provider_config
from my_claude_code.providers.runtime.proxy_leg import ProxiedLegRateLimiter

from .jobs import PinnedMediaClient
from .key_pool import MediaKeyPool
from .leaf import MediaLeaf, MediaNode
from .proxy_pool import MediaProxyPool, MediaProxyRotationState
from .refusal import MediaRefusalNode


def _leaf_limiter(config: ProviderConfig, *, proxied_leg: bool) -> ProviderRateLimiter:
    """The limiter a chat leaf gets from ``_create_leaf_provider``, fresh."""

    limiter_class = ProxiedLegRateLimiter if proxied_leg else ProviderRateLimiter
    return limiter_class(
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


def _keys(config: ProviderConfig) -> tuple[str, ...]:
    return tuple(config.api_keys or ((config.api_key,) if config.api_key else ()))


def _pinned_key_index(
    keys: tuple[str, ...], fingerprint: str | None, key_index: int | None
) -> int | None:
    """Where the key that accepted a job sits now, or ``None`` if it is gone."""

    if fingerprint is not None:
        for index, key in enumerate(keys):
            if credential_fingerprint(key) == fingerprint:
                return index
        return None
    if not keys:
        # A keyless provider (a local host): its one leaf is the job's.
        return 0
    if key_index is not None and 0 <= key_index < len(keys):
        return key_index
    return None


class MediaRegistry:
    """The process-wide owner of every provider's media stack."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._entries: dict[str, tuple[ProviderConfig, MediaNode]] = {}
        # What each held stack was built to serve. Part of "has this provider
        # changed" beside its config, because a custom provider's declared
        # media endpoints (7.67.0) live on its descriptor, not in the config a
        # stack is keyed on; a static provider's never change.
        self._declared: dict[str, tuple[MediaSurface, ...]] = {}
        self._retired: list[MediaNode] = []

    def resolver(self, settings: Settings) -> MediaProviderResolver:
        return lambda provider_id: self.resolve(provider_id, settings)

    def resolve(self, provider_id: str, settings: Settings) -> MediaNode:
        descriptors = get_provider_registry().all_descriptors()
        descriptor = descriptors.get(provider_id)
        if descriptor is None:
            raise UnknownProviderError.for_provider(provider_id, descriptors)
        config = build_provider_config(descriptor, settings)
        entry = self._entries.get(provider_id)
        if (
            entry is not None
            and entry[0] == config
            and self._declared.get(provider_id) == descriptor.media_surfaces
        ):
            return entry[1]
        node = self._build(descriptor, config, settings)
        if entry is not None:
            self._retired.append(entry[1])
        self._entries[provider_id] = (config, node)
        self._declared[provider_id] = descriptor.media_surfaces
        return node

    def key_fingerprint(
        self, settings: Settings, provider_id: str, key_index: int | None
    ) -> str | None:
        """The stable id of the key at ``key_index`` (never the key itself)."""

        self.resolve(provider_id, settings)
        keys = _keys(self._entries[provider_id][0])
        if key_index is None or not 0 <= key_index < len(keys):
            return None
        return credential_fingerprint(keys[key_index])

    def job_client(
        self,
        settings: Settings,
        provider_id: str,
        *,
        key_fingerprint: str | None,
        key_index: int | None,
        proxy_label: str | None,
    ) -> MediaJobClient | None:
        """The pinned client for a job; ``None`` when the key that took it is gone."""

        node = self.resolve(provider_id, settings)
        keys = _keys(self._entries[provider_id][0])
        index = _pinned_key_index(keys, key_fingerprint, key_index)
        if index is None:
            return None
        leaf = node.leaf_for(index, proxy_label)
        if leaf is None:
            return None
        return PinnedMediaClient(
            leaf,
            key_index=index,
            state=node.state if isinstance(node, MediaKeyPool) else None,
            health=media_route_health_registry(settings),
        )

    def node(self, provider_id: str) -> MediaNode | None:
        """The stack currently held for ``provider_id``, if one was built."""

        entry = self._entries.get(provider_id)
        return None if entry is None else entry[1]

    def forget(self) -> None:
        """Drop every stack without closing it. For the test suite only."""

        self._entries.clear()
        self._declared.clear()
        self._retired = []

    async def close(self) -> None:
        nodes = [node for _config, node in self._entries.values()] + self._retired
        self._entries.clear()
        self._declared.clear()
        self._retired = []
        for node in nodes:
            try:
                await node.cleanup()
            except Exception as exc:
                logger.debug("Media stack close failed: {}", type(exc).__name__)

    def _leaf(
        self,
        descriptor: ProviderDescriptor,
        config: ProviderConfig,
        *,
        proxied_leg: bool,
        proxy_label: str = "",
    ) -> MediaLeaf:
        return MediaLeaf(
            provider_id=descriptor.provider_id,
            config=config,
            surfaces=descriptor.media_surfaces,
            rate_limiter=_leaf_limiter(config, proxied_leg=proxied_leg),
            transport=self._transport,
            proxy_label=proxy_label,
        )

    def _single(
        self,
        descriptor: ProviderDescriptor,
        config: ProviderConfig,
        settings: Settings,
    ) -> MediaNode:
        plan = config.proxy_chain
        if isinstance(plan, MaskedRefusalPlan):
            # Direct fallback off and nothing in the chain to route through
            # (7.78.8): the leaf below would have no proxy at all.
            return MediaRefusalNode(
                provider_id=descriptor.provider_id,
                config=config,
                surfaces=descriptor.media_surfaces,
                message=plan.reason,
            )
        if plan is None or len(plan.legs) < 2:
            # One fixed way out names itself in the log (7.79.2, C-9 b): a
            # one-entry chain's entry, else the static proxy's masked address.
            # No proxy at all records nothing, as before.
            return self._leaf(
                descriptor,
                config,
                proxied_leg=False,
                proxy_label=config.proxy_label
                or (mask_proxy_label(config.proxy) if config.proxy else ""),
            )
        legs = plan.legs
        labels = tuple(leg.label or DIRECT_PROXY_LABEL for leg in legs)
        connect_timeout = float(
            getattr(settings, "proxy_connect_timeout_seconds", None)
            or PROXY_CONNECT_TIMEOUT_SECONDS_DEFAULT
        )

        def build_leg(index: int) -> MediaNode:
            url = legs[index].url if index < len(legs) else ""
            if not url:
                return self._leaf(
                    descriptor,
                    dataclasses.replace(config, proxy=url, proxy_chain=None),
                    proxied_leg=False,
                )
            return self._leaf(
                descriptor,
                dataclasses.replace(
                    config,
                    proxy=url,
                    proxy_chain=None,
                    http_connect_timeout=connect_timeout,
                ),
                proxied_leg=True,
            )

        state = MediaProxyRotationState(
            len(legs),
            plan.policy,
            labels=labels,
            provider_id=descriptor.provider_id,
            scope=plan.scope,
        )
        return MediaProxyPool(
            build_leg,
            state,
            labels=labels,
            plan=plan,
            provider_id=descriptor.provider_id,
            max_open_legs=int(getattr(settings, "proxy_max_open_legs", 0) or 0),
            max_live_failures=int(getattr(settings, "proxy_max_live_failures", 0) or 0),
            name=descriptor.display_name,
            base_url=config.base_url,
        )

    def _build(
        self,
        descriptor: ProviderDescriptor,
        config: ProviderConfig,
        settings: Settings,
    ) -> MediaNode:
        keys = config.api_keys or ((config.api_key,) if config.api_key else ())
        if len(keys) <= 1:
            return self._single(descriptor, config, settings)
        providers = [
            self._single(
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
        return MediaKeyPool(
            providers,
            state,
            key_labels=tuple(mask_key_label(key) for key in keys),
            provider_id=descriptor.provider_id,
            routes_around_model=config.routes_around_model,
        )


#: The one registry the running server uses. Tests build their own.
MEDIA_REGISTRY = MediaRegistry()
