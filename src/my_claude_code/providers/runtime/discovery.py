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
from my_claude_code.providers.recovery import (
    learned_fact_store,
    upstream_status_code,
)

from . import models_dev
from .config import provider_credential
from .model_cache import ProviderModelCache
from .validation import provider_query_failure_reason

ProviderResolver = Callable[[str], BaseProvider]
ModelInfoCache = Callable[[str, Iterable[ProviderModelInfo]], None]


#: A sweep that comes back with a fraction of what the cache already holds is
#: far more likely to be one bad upstream response than a gateway that retired
#: most of its catalogue in an hour, and adopting it would mark hundreds of
#: learned facts stale for nothing. LiteLLM refuses a fetched map below half
#: the bundled count for the same reason; CLIProxyAPI provider-diffs before
#: hot-swapping. Below :data:`SHRINK_GUARD_MINIMUM_MODELS` the ratio is noise,
#: so the guard does not apply at all.
SHRINK_GUARD_MINIMUM_MODELS = 8
SHRINK_GUARD_RATIO = 0.5


class CatalogueShrankError(RuntimeError):
    """A sweep returned so few models that it is treated as a failed sweep."""


async def cache_enriched_model_infos(
    provider_id: str,
    model_infos: Iterable[ProviderModelInfo],
    cache: ModelInfoCache,
    *,
    previous_ids: frozenset[str] | None = None,
    quiet: bool = False,
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
    ones, so the catalogue's contents depended on which one filled it. It is
    therefore also the one place that can say what *changed*: pass
    ``previous_ids`` and a provider whose set actually moved gets one INFO
    line, while a provider whose set is identical gets nothing at all --
    57 unchanged lines an hour is noise, which is why ``quiet`` demotes the
    per-provider cache line to DEBUG for timer-driven sweeps.
    """
    enriched = await models_dev.enrich_provider_model_infos(
        model_infos, provider_id=provider_id
    )
    current_ids = frozenset(info.model_id for info in enriched)
    if previous_ids is not None:
        _guard_against_a_collapsed_catalogue(provider_id, previous_ids, current_ids)
    cache(provider_id, enriched)
    if previous_ids is not None:
        _report_catalogue_change(provider_id, previous_ids, current_ids)
    logger.log(
        "DEBUG" if quiet else "INFO",
        "Provider model discovery cached: provider={} models={}",
        provider_id,
        len(enriched),
    )
    return tuple(enriched)


def _guard_against_a_collapsed_catalogue(
    provider_id: str, previous_ids: frozenset[str], current_ids: frozenset[str]
) -> None:
    if len(previous_ids) < SHRINK_GUARD_MINIMUM_MODELS:
        return
    if len(current_ids) >= len(previous_ids) * SHRINK_GUARD_RATIO:
        return
    raise CatalogueShrankError(
        f"{provider_id} returned {len(current_ids)} models against "
        f"{len(previous_ids)} cached; treating the sweep as failed rather "
        f"than deleting {len(previous_ids) - len(current_ids)} models"
    )


def _report_catalogue_change(
    provider_id: str, previous_ids: frozenset[str], current_ids: frozenset[str]
) -> None:
    """Say what moved, and retire the facts of a model that is gone."""

    added = current_ids - previous_ids
    removed = previous_ids - current_ids
    if not added and not removed:
        return
    logger.info(
        "catalogue changed: +{} -{} for {} (now {} models)",
        len(added),
        len(removed),
        provider_id,
        len(current_ids),
    )
    # A model that left took its deployment with it, so a cap or a refusal
    # measured against it is no longer evidence about anything. Retired, never
    # deleted: the row stays visible, and a model that comes back has to earn
    # its facts again -- a deployment that reappeared is not proven to be the
    # same deployment.
    store = learned_fact_store()
    for model_id in removed:
        store.retire_model(provider_id, model_id)


def discovery_failure(
    provider_id: str, exc: BaseException, settings: Settings
) -> ProviderDiscoveryFailure:
    """Describe one discovery failure for both the log and the API response."""
    status = upstream_status_code(exc) if isinstance(exc, Exception) else None
    return ProviderDiscoveryFailure(
        provider_id=provider_id,
        error_type=type(exc).__name__,
        message=redact_sensitive_error_text(
            provider_query_failure_reason(exc, settings)
        ),
        status_code=status,
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
        *,
        quiet: bool = False,
    ) -> None:
        self._settings = settings
        self._provider_resolver = provider_resolver
        self._model_cache = model_cache
        self._connected_provider_ids = connected_provider_ids
        # A timer-driven sweep says nothing per provider unless the set moved.
        self._quiet = quiet

    async def warm_referenced_model_cache(self) -> ProviderModelRefreshResult:
        """Synchronously cache model metadata for routed providers."""
        return await self._refresh_model_infos(
            tuple(referenced_provider_ids(self._settings))
        )

    async def refresh_model_list_cache(
        self,
        *,
        only_missing: bool = False,
        skip_provider_ids: frozenset[str] = frozenset(),
    ) -> ProviderModelRefreshResult:
        """Best-effort refresh of model lists for usable providers.

        ``skip_provider_ids`` is the periodic sweep's back-off list: a
        provider that answered 401 or 403 is left alone for a few ticks
        instead of being asked again on the hour.
        """
        provider_ids = model_list_provider_ids_for_settings(
            self._settings, self._connected_provider_ids
        )
        if skip_provider_ids:
            provider_ids = tuple(
                provider_id
                for provider_id in provider_ids
                if provider_id not in skip_provider_ids
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
        # Read before anything is fetched: the diff below is against what the
        # catalogue held when this sweep started, not against itself.
        cached_ids = self._model_cache.cached_model_ids()
        for provider_id in provider_ids:
            # Resolving a provider builds an SSL context and an HTTP client --
            # tens of milliseconds each, entirely synchronous. Without a
            # suspension point the whole loop is one uninterruptible block on
            # the event loop: /v1/messages stalls behind it, and the
            # ``task.cancel()`` in ProviderRuntimeManager._cancel_refresh
            # cannot land until it finishes, so the next config apply waits
            # out a sweep it already asked to abandon.
            await asyncio.sleep(0)
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
                previous_ids = cached_ids.get(provider_id)
                try:
                    await cache_enriched_model_infos(
                        provider_id,
                        result,
                        self._model_cache.cache_model_infos,
                        previous_ids=previous_ids,
                        quiet=self._quiet,
                    )
                except CatalogueShrankError as exc:
                    failures.append(self._record_discovery_failure(provider_id, exc))
                    failed_provider_ids.append(provider_id)
                    continue
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
