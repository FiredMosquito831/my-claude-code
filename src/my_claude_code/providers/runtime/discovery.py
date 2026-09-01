"""Provider model-list discovery and background refresh."""

import asyncio
from collections.abc import Callable, Iterable

from loguru import logger

from my_claude_code.application.model_metadata import (
    ProviderDiscoveryFailure,
    ProviderModelInfo,
    ProviderModelRefreshResult,
)
from my_claude_code.config.model_refs import configured_chat_model_refs
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.providers.base import BaseProvider

from . import models_dev
from .config import provider_credential
from .model_cache import ProviderModelCache
from .validation import provider_query_failure_reason

ProviderResolver = Callable[[str], BaseProvider]
ModelInfoCache = Callable[[str, Iterable[ProviderModelInfo]], None]


async def cache_enriched_model_infos(
    provider_id: str,
    model_infos: Iterable[ProviderModelInfo],
    cache: ModelInfoCache,
) -> tuple[ProviderModelInfo, ...]:
    """Enrich one provider's model list from models.dev, then cache it.

    Every provider is enriched from models.dev, not just the few that report
    nothing themselves. Enrichment only fills fields the provider left null, so
    a gateway that publishes its own modality metadata keeps it -- and the ~30
    providers that publish none stop being a blind spot. Without this, "this
    model cannot read images" was unanswerable for most of the catalog, and
    vision routing silently never fired.

    This is the single cache-and-publish seam. The admin "Refresh models"
    button used to cache raw infos while background discovery cached enriched
    ones, so the catalogue's contents depended on which one filled it.
    """
    enriched = await models_dev.enrich_provider_model_infos(
        model_infos, provider_id=provider_id
    )
    cache(provider_id, enriched)
    logger.info(
        "Provider model discovery cached: provider={} models={}",
        provider_id,
        len(enriched),
    )
    return tuple(enriched)


def discovery_failure(
    provider_id: str, exc: BaseException, settings: Settings
) -> ProviderDiscoveryFailure:
    """Describe one discovery failure for both the log and the API response."""
    return ProviderDiscoveryFailure(
        provider_id=provider_id,
        error_type=type(exc).__name__,
        message=redact_sensitive_error_text(
            provider_query_failure_reason(exc, settings)
        ),
    )


def referenced_provider_ids(settings: Settings) -> frozenset[str]:
    """Return provider ids referenced by configured chat model refs."""
    return frozenset(ref.provider_id for ref in configured_chat_model_refs(settings))


def model_cache_provider_ids_for_settings(
    settings: Settings,
    connected_provider_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return providers whose model metadata is valid for these settings."""
    descriptors = get_provider_registry().all_descriptors()
    available = {
        provider_id
        for provider_id, descriptor in descriptors.items()
        if descriptor.local
        or (
            descriptor.credential_env is not None
            and provider_credential(descriptor, settings).strip()
        )
        or (descriptor.dynamic and descriptor.static_credential)
    } | set(connected_provider_ids)
    return tuple(provider_id for provider_id in descriptors if provider_id in available)


def model_list_provider_ids_for_settings(
    settings: Settings,
    connected_provider_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return providers worth discovering for this process configuration."""
    descriptors = get_provider_registry().all_descriptors()
    referenced_ids = referenced_provider_ids(settings)
    return tuple(
        provider_id
        for provider_id in model_cache_provider_ids_for_settings(
            settings, connected_provider_ids
        )
        if not descriptors[provider_id].local or provider_id in referenced_ids
    )


class ProviderModelDiscovery:
    """Refresh provider model-list metadata for one provider runtime."""

    def __init__(
        self,
        settings: Settings,
        provider_resolver: ProviderResolver,
        model_cache: ProviderModelCache,
        connected_provider_ids: tuple[str, ...] = (),
    ) -> None:
        self._settings = settings
        self._provider_resolver = provider_resolver
        self._model_cache = model_cache
        self._connected_provider_ids = connected_provider_ids

    async def warm_referenced_model_cache(self) -> ProviderModelRefreshResult:
        """Synchronously cache model metadata for routed providers."""
        return await self._refresh_model_infos(
            tuple(referenced_provider_ids(self._settings))
        )

    async def refresh_model_list_cache(
        self, *, only_missing: bool = False
    ) -> ProviderModelRefreshResult:
        """Best-effort refresh of model lists for usable providers."""
        provider_ids = model_list_provider_ids_for_settings(
            self._settings, self._connected_provider_ids
        )
        if only_missing:
            provider_ids = tuple(
                provider_id
                for provider_id in provider_ids
                if not self._model_cache.has_provider(provider_id)
            )
        return await self._refresh_model_infos(provider_ids)

    async def refresh_provider(self, provider_id: str) -> ProviderModelRefreshResult:
        """Refresh exactly one dynamically changed provider."""

        return await self._refresh_model_infos((provider_id,))

    async def _refresh_model_infos(
        self, provider_ids: tuple[str, ...]
    ) -> ProviderModelRefreshResult:
        failed_provider_ids: list[str] = []
        failures: list[ProviderDiscoveryFailure] = []
        tasks: dict[str, asyncio.Task[frozenset[ProviderModelInfo]]] = {}
        for provider_id in provider_ids:
            try:
                provider = self._provider_resolver(provider_id)
            except Exception as exc:
                failures.append(self._record_discovery_failure(provider_id, exc))
                failed_provider_ids.append(provider_id)
                continue
            tasks[provider_id] = asyncio.create_task(provider.list_model_infos())

        refreshed_provider_ids: list[str] = []
        if tasks:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for (provider_id, _task), result in zip(
                tasks.items(), results, strict=True
            ):
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    failures.append(self._record_discovery_failure(provider_id, result))
                    failed_provider_ids.append(provider_id)
                    continue
                await cache_enriched_model_infos(
                    provider_id, result, self._model_cache.cache_model_infos
                )
                refreshed_provider_ids.append(provider_id)

        return ProviderModelRefreshResult(
            refreshed_provider_ids=tuple(refreshed_provider_ids),
            failed_provider_ids=tuple(failed_provider_ids),
            failures=tuple(failures),
        )

    def _record_discovery_failure(
        self, provider_id: str, exc: BaseException
    ) -> ProviderDiscoveryFailure:
        failure = discovery_failure(provider_id, exc, self._settings)
        logger.warning(
            "Provider model discovery skipped: provider={} reason={}",
            provider_id,
            failure.message,
        )
        return failure
