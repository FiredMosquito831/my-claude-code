"""Process-wide web search provider cache shared by web_tools and the Admin API.

Providers are cached per provider id keyed by their resolved immutable config so
in-memory KeyPool rotation health survives across searches. When the effective
config changes (e.g. an Admin apply), the stale instance is closed and replaced
transparently.
"""

import asyncio
from typing import Any

from my_claude_code.config.settings import Settings
from my_claude_code.websearch.base import (
    BaseWebSearchProvider,
    WebSearchProviderConfig,
)
from my_claude_code.websearch.registry import build_provider

_lock = asyncio.Lock()
_cache: dict[str, tuple[WebSearchProviderConfig, BaseWebSearchProvider]] = {}


async def runtime_provider(
    settings: Settings, provider_id: str
) -> BaseWebSearchProvider:
    """Return the cached provider for ``provider_id``, rebuilding on config change."""

    candidate = build_provider(settings, provider_id)
    async with _lock:
        cached = _cache.get(provider_id)
        if cached is not None and cached[0] == candidate.config:
            await candidate.close()
            return cached[1]
        if cached is not None:
            await cached[1].close()
        _cache[provider_id] = (candidate.config, candidate)
        return candidate


def cached_key_pool_snapshot(provider_id: str) -> dict[str, Any] | None:
    """Live KeyPool health for a cached provider; None when never used."""

    cached = _cache.get(provider_id)
    if cached is None:
        return None
    return cached[1].key_pool.snapshot()


def cached_key_pool_facts(provider_id: str) -> list[dict[str, Any]] | None:
    """Why each key of a cached provider is benched (7.113.0); None when never used."""

    cached = _cache.get(provider_id)
    if cached is None:
        return None
    return cached[1].key_pool.health_facts()


def reset_cached_pool_key(provider_id: str, index: int) -> bool | None:
    """Restore one key of a cached provider's pool; None when never used."""

    cached = _cache.get(provider_id)
    if cached is None:
        return None
    return cached[1].key_pool.reset_key(index)


async def reset_runtime_providers() -> None:
    """Close and drop every cached provider (tests, teardown)."""

    async with _lock:
        providers = [entry[1] for entry in _cache.values()]
        _cache.clear()
    for provider in providers:
        await provider.close()
