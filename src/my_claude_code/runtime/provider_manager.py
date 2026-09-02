"""Single-owner provider generations and application model catalog."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Protocol

from loguru import logger

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
    ProviderModelRefreshResult,
)
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.reasoning import ReasoningDialect
from my_claude_code.core.trace import trace_event
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.providers.runtime.discovery import (
    ProviderModelDiscovery,
    model_cache_provider_ids_for_settings,
)
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.models_dev import (
    model_context_length_tiered,
    model_output_limit_tiered,
    model_prices_tiered,
    model_tool_call_tiered,
    model_vision_tiered,
    resolve_model_reasoning_capability,
)
from my_claude_code.providers.runtime.validation import ConfiguredModelValidator

ProviderRuntimeFactory = Callable[[Settings], ProviderRuntime]
ConnectedProviderIds = Callable[[], tuple[str, ...]]
CommitConfig = Callable[[], None]


class ModelCatalogPublisher(Protocol):
    """Synchronize an external view of the application model inventory."""

    def ensure_exists(self, runtime: RequestRuntimePort) -> None: ...

    def publish(self, runtime: RequestRuntimePort) -> None: ...


@dataclass(slots=True, eq=False)
class _ProviderGeneration:
    generation_id: int
    settings: Settings
    runtime: ProviderRuntime
    active_leases: int = 0
    retired: bool = False
    closed: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_task: asyncio.Task[bool] | None = None

    def __post_init__(self) -> None:
        self.drained.set()


class ProviderGenerationLease:
    """Idempotent lease retaining one provider generation."""

    def __init__(
        self,
        manager: ProviderRuntimeManager,
        generation: _ProviderGeneration,
    ) -> None:
        self._manager = manager
        self._generation = generation
        self._released = False

    @property
    def generation_id(self) -> int:
        return self._generation.generation_id

    @property
    def settings(self) -> Settings:
        return self._generation.settings

    def is_provider_cached(self, provider_id: str) -> bool:
        return self._generation.runtime.is_cached(provider_id)

    def resolve_provider(self, provider_id: str) -> BaseProvider:
        return self._generation.runtime.resolve_provider(provider_id)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self._generation)

    async def __aenter__(self) -> ProviderGenerationLease:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()


class ProviderRuntimeManager:
    """Own provider generations, leases, discovery, and model metadata."""

    def __init__(
        self,
        settings: Settings,
        *,
        runtime_factory: ProviderRuntimeFactory = ProviderRuntime,
        connected_provider_ids: ConnectedProviderIds = tuple,
        model_catalog_publisher: ModelCatalogPublisher | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._connected_provider_ids = connected_provider_ids
        self._model_catalog_publisher = model_catalog_publisher
        self._replace_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._model_cache = ProviderModelCache(
            model_cache_provider_ids_for_settings(settings, connected_provider_ids())
        )
        self._refresh_task: asyncio.Task[None] | None = None
        self._next_generation_id = 2
        self._retired: dict[int, _ProviderGeneration] = {}
        self._unpublished: set[ProviderRuntime] = set()
        self._closing = False
        self._closed = False
        self._current = _ProviderGeneration(
            generation_id=1,
            settings=settings,
            runtime=runtime_factory(settings),
        )
        self._trace_published(self._current, previous=None, reason="startup")

    @property
    def current_generation_id(self) -> int:
        return self._current.generation_id

    async def acquire(self) -> ProviderGenerationLease:
        if self._closing or self._closed:
            raise ApplicationUnavailableError("Provider runtime is shutting down.")
        generation = self._current
        generation.active_leases += 1
        generation.drained.clear()
        return ProviderGenerationLease(self, generation)

    def current_settings(self) -> Settings:
        return self._current.settings

    def cached_model_ids(self) -> dict[str, frozenset[str]]:
        return self._model_cache.cached_model_ids()

    def cached_model_supports_thinking(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        return self._model_cache.cached_model_supports_thinking(provider_id, model_id)

    def cached_model_supports_vision(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        return self._model_cache.cached_model_supports_vision(provider_id, model_id)

    def model_reasoning_capability(
        self, provider_id: str, model_id: str
    ) -> ModelReasoningCapability | None:
        """Layer this provider's own thinking flag over models.dev metadata."""
        return resolve_model_reasoning_capability(
            provider_id,
            model_id,
            self._model_cache.cached_model_reasoning_capability(provider_id, model_id),
        )

    def model_reasoning_dialect(
        self, provider_id: str, model_id: str
    ) -> ReasoningDialect | None:
        """Which reasoning fields the HOST parses for this (provider, model).

        The second of the two facts gating needs, and the one nothing used to
        supply: ``model_reasoning_capability`` above says what the *model* can
        be told, this says what the *gateway in front of it* will read. A
        control reaches the wire only when both agree.

        Synchronous on purpose. ``ProviderRuntime.resolve_provider`` builds and
        caches the provider instance without awaiting anything, and a lease
        exists to hold a generation across ``await``s -- which a single
        in-line lookup never crosses. Constructing a provider can raise for one
        that is not configured at all, and that is simply unknown, so the whole
        lookup degrades to ``None`` rather than failing a request.
        """
        try:
            provider = self._current.runtime.resolve_provider(provider_id)
        except Exception:
            return None
        dialect = provider.reasoning_dialect(model_id)
        if dialect is None:
            return None
        return self._narrow_dialect_by_gateway(provider_id, model_id, dialect)

    def _narrow_dialect_by_gateway(
        self, provider_id: str, model_id: str, dialect: ReasoningDialect
    ) -> ReasoningDialect:
        """Narrow a provider-wide dialect by this model's own ``/models`` row.

        One gateway can parse different fields for different models --
        ``nous_portal`` publishes ``reasoning_effort`` for ``tencent/hy3:free``
        and not for ``meituan/longcat-2.0:free`` -- which is a statement no
        per-provider encoder can make. Only an authoritative rung (the
        provider's own catalogue, exact or tag-stripped) may narrow: a foreign
        catalogue does not know what this host parses.

        Narrowing only. Where the gateway advertises a field the profile has no
        way to emit, the dialect is left alone and the gap is logged: widening
        here would claim a wire shape the encoder cannot produce, and the fix
        for that is a profile change, not a runtime guess.
        """
        found = self._model_cache.cached_model_info_tiered(provider_id, model_id)
        if found is None or not found[1].is_authoritative:
            return dialect
        info, _tier = found
        published = info.supported_parameters
        if published is None:
            return dialect

        lists_effort = "reasoning_effort" in published
        lists_reasoning = "reasoning" in published
        effort_values = dialect.effort_values
        if effort_values is not None and not (
            lists_effort
            or (lists_reasoning and dialect.effort_field.startswith("reasoning."))
        ):
            effort_values = None
        # A chat-template or ``thinking``-object toggle is not an OpenAI
        # request parameter, so a gateway that never lists it is not denying
        # it. Only a toggle the gateway *could* have listed is narrowed away.
        toggle = dialect.toggle
        if toggle and dialect.toggle_field.startswith("reasoning"):
            toggle = lists_reasoning or lists_effort
        budget = dialect.budget
        if budget and dialect.budget_field.startswith("reasoning."):
            budget = lists_reasoning
        if lists_effort and dialect.effort_values is None:
            logger.debug(
                "REASONING DIALECT GAP: '{}/{}' advertises reasoning_effort but"
                " its provider profile has no effort field to send it through.",
                provider_id,
                model_id,
            )
        if (
            effort_values == dialect.effort_values
            and toggle == dialect.toggle
            and budget == dialect.budget
        ):
            return dialect
        return replace(
            dialect, effort_values=effort_values, toggle=toggle, budget=budget
        )

    def model_output_limit(self, provider_id: str, model_id: str) -> int | None:
        """Return the model's published output-token limit, if anything has one.

        The routing target's own ``/models`` payload is the authority and is
        consulted first; models.dev -- whose answer may itself come from the
        approximate cross-provider tier -- only fills the gap. The ordering is
        load-bearing: for ``nous_portal/tencent/hy3:free`` the gateway reports
        128,000 while the cross-provider modal value is 64,000, and letting the
        name match win would halve the model's real capacity for no reason
        (WORKING-NOTES 54).
        """
        return self.model_output_limit_tiered(provider_id, model_id)[0]

    def model_output_limit_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[int | None, ResolutionTier | None]:
        """The same ladder, plus the rung the number came from (tier 1-10).

        Tiers 1-2 are the routing target's own ``/models`` payload -- exact,
        then with the pricing/routing tag stripped. Tiers 3-10 are models.dev:
        its own bucket first, then the OpenRouter reference catalogue, then the
        approximate cross-provider vote last.
        """
        found = self._model_cache.cached_model_info_tiered(provider_id, model_id)
        if found is not None and found[0].max_output_tokens is not None:
            return found[0].max_output_tokens, found[1]
        return model_output_limit_tiered(provider_id, model_id)

    def model_context_length(self, provider_id: str, model_id: str) -> int | None:
        """Return the routed deployment's own context window, if it publishes one.

        Only the provider's ``/models`` payload answers this. models.dev's
        ``limit.context`` describes the model as its *originating* vendor ships
        it, which for a resold deployment is routinely wrong in the direction
        that matters -- a gateway serving a 262k model on a 32k deployment
        would have its real window overstated, and the output budget derived
        from it would not fit.
        """
        return self._model_cache.cached_model_context_length(provider_id, model_id)

    def model_context_length_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[int | None, ResolutionTier | None]:
        """The context window down the same ten rungs the output limit walks.

        A sibling of :meth:`model_output_limit_tiered`, not a replacement for
        :meth:`model_context_length`. The two answer different questions and
        both are needed: the output budget must only ever be derived from the
        *routed deployment's own* window, because a gateway serving a 262k
        model on a 32k deployment would have its real window overstated and
        the budget would not fit -- so :meth:`model_context_length` stays
        provider-only and ``application/output_tokens`` keeps reading it.

        What a *catalogue* publishes is the other question. A CLI picker shown
        no window at all does not fall back to a smaller number; it falls back
        to its own invented one, or refuses the document. There the honest
        ordering is the full ladder: the deployment's own answer still wins
        outright, and only where nothing was published at all does models.dev
        get to speak -- tagged with the rung, so an approximate answer is
        visibly approximate.
        """
        found = self._model_cache.cached_model_info_tiered(provider_id, model_id)
        if found is not None and found[0].context_length is not None:
            return found[0].context_length, found[1]
        return model_context_length_tiered(provider_id, model_id)

    def model_vision_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        """Image-input support down the same ladder, provider first."""
        found = self._model_cache.cached_model_info_tiered(provider_id, model_id)
        if found is not None and found[0].supports_vision is not None:
            return found[0].supports_vision, found[1]
        return model_vision_tiered(provider_id, model_id)

    def model_tool_call_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        """models.dev's ``tool_call`` boolean, tiers 3-10 only.

        No provider rung: a gateway states tool support through its
        ``supported_parameters`` list, which ``derive_supports_tool_calls``
        reads and which stays the first and preferred answer. This is only
        consulted where that list was never published.
        """
        return model_tool_call_tiered(provider_id, model_id)

    def model_prices_tiered(
        self, provider_id: str, model_id: str
    ) -> dict[str, tuple[float | None, ResolutionTier | None]]:
        """The four published rates, each down the same ladder.

        ``ProviderModelInfo`` carries only the two uncached rates, so the
        provider rung answers those and the cache rates resolve from tier 3
        down or stay unknown.
        """
        resolved = model_prices_tiered(provider_id, model_id)
        found = self._model_cache.cached_model_info_tiered(provider_id, model_id)
        if found is None:
            return resolved
        info, tier = found
        if info.input_price is not None:
            resolved["input_price"] = (info.input_price, tier)
        if info.output_price is not None:
            resolved["output_price"] = (info.output_price, tier)
        return resolved

    def cached_prefixed_model_infos(self) -> tuple[ProviderModelInfo, ...]:
        return self._model_cache.cached_prefixed_model_infos()

    def cache_model_infos(
        self,
        provider_id: str,
        model_infos: Iterable[ProviderModelInfo],
    ) -> None:
        self._model_cache.cache_model_infos(provider_id, model_infos)
        self._publish_model_catalog()

    async def warm_referenced_model_cache(self) -> ProviderModelRefreshResult:
        """Warm routed provider catalogs before clients perform model discovery."""
        lease = await self.acquire()
        try:
            discovery = ProviderModelDiscovery(
                lease.settings,
                lease.resolve_provider,
                self._model_cache,
            )
            result = await discovery.warm_referenced_model_cache()
            self._ensure_model_catalog()
            return result
        finally:
            await lease.release()

    def _synchronize_model_cache_scope(self) -> None:
        """Drop metadata whose settings or connected account is no longer usable."""

        self._model_cache.set_available_providers(
            model_cache_provider_ids_for_settings(
                self._current.settings, self._connected_provider_ids()
            )
        )

    async def connected_provider_changed(
        self, provider_id: str, *, connected: bool
    ) -> ProviderModelRefreshResult:
        """Synchronize one connected account without replacing a generation."""

        async with self._replace_lock:
            if self._closing or self._closed:
                raise ApplicationUnavailableError("Provider runtime is shutting down.")
            if not connected:
                self._model_cache.remove_provider(provider_id)
                self._publish_model_catalog()
                return ProviderModelRefreshResult()
            self._model_cache.add_provider(provider_id)
            discovery = ProviderModelDiscovery(
                self._current.settings,
                self._current.runtime.resolve_provider,
                self._model_cache,
                self._connected_provider_ids(),
            )
            result = await discovery.refresh_provider(provider_id)
            self._publish_model_catalog()
            return result

    async def refresh_provider_models(
        self,
        provider_id: str,
        *,
        attempts: int = 2,
        retry_delay: float = 0.25,
    ) -> ProviderModelRefreshResult:
        """Refresh exactly one mutated provider, with a bounded retry.

        A provider registered seconds ago is the one most likely to lose its
        first ``/models`` query -- a gateway that has not finished propagating
        the key answers 403 once and then serves the list fine. One retry turns
        that into a non-event; a periodic sweep is deliberately not the answer,
        because the sweep is what caused the race this replaces.
        """

        async with self._replace_lock:
            if self._closing or self._closed:
                raise ApplicationUnavailableError("Provider runtime is shutting down.")
            discovery = ProviderModelDiscovery(
                self._current.settings,
                self._current.runtime.resolve_provider,
                self._model_cache,
                self._connected_provider_ids(),
            )
            result = ProviderModelRefreshResult()
            for attempt in range(max(1, attempts)):
                if attempt:
                    await asyncio.sleep(retry_delay)
                result = await discovery.refresh_provider(provider_id)
                if not result.failed_provider_ids:
                    break
            self._publish_model_catalog()
            return result

    def _ensure_model_catalog(self) -> None:
        publisher = self._model_catalog_publisher
        if publisher is None:
            return
        self._run_model_catalog_publication(publisher.ensure_exists)

    def _publish_model_catalog(self) -> None:
        publisher = self._model_catalog_publisher
        if publisher is None:
            return
        self._run_model_catalog_publication(publisher.publish)

    def _run_model_catalog_publication(
        self,
        publication: Callable[[RequestRuntimePort], None],
    ) -> None:
        try:
            publication(self)
        except Exception as exc:
            logger.warning(
                "Model catalog publication failed: exc_type={}",
                type(exc).__name__,
            )

    async def validate_configured_models(self) -> None:
        lease = await self.acquire()
        try:
            validator = ConfiguredModelValidator(
                lease.settings,
                lease.resolve_provider,
                self._model_cache,
            )
            await validator.validate_configured_models()
        finally:
            await lease.release()

    def start_model_list_refresh(self) -> None:
        """Start one non-blocking refresh for the current generation."""
        if self._closing or self._closed:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        generation = self._current
        self._refresh_task = asyncio.create_task(
            self._refresh_generation_in_background(generation, only_missing=True)
        )

    async def refresh_model_list_cache(self) -> ProviderModelRefreshResult:
        """Run an explicit full refresh without racing replacement."""
        async with self._replace_lock:
            if self._closing or self._closed:
                raise ApplicationUnavailableError("Provider runtime is shutting down.")
            await self._cancel_refresh()
            return await self._refresh_generation(self._current, only_missing=False)

    async def replace(
        self,
        settings: Settings,
        *,
        commit: CommitConfig,
        reason: str = "admin_apply",
        background_refresh: bool = True,
    ) -> int:
        """Prepare, commit, and atomically publish one replacement generation.

        ``background_refresh=False`` suppresses the fire-and-forget sweep of
        *every* provider's ``/models``. A caller that already knows which one
        provider changed follows the replace with
        :meth:`refresh_provider_models` instead: the sweep raced that caller's
        own probe and hit a brand-new upstream twice within the same second.
        """
        async with self._replace_lock:
            if self._closing or self._closed:
                raise ApplicationUnavailableError("Provider runtime is shutting down.")
            await self._cancel_refresh()
            await self._retry_unpublished_cleanup()
            candidate_id = self._next_generation_id
            candidate_runtime: ProviderRuntime | None = None
            try:
                candidate_runtime = self._runtime_factory(settings)
                commit()
            except Exception as exc:
                trace_event(
                    stage="runtime",
                    event="provider_generation.replace_failed",
                    source="runtime",
                    current_generation_id=self._current.generation_id,
                    candidate_generation_id=candidate_id,
                    reason=reason,
                    exc_type=type(exc).__name__,
                )
                if candidate_runtime is not None:
                    await self._cleanup_unpublished(candidate_runtime)
                raise

            self._next_generation_id += 1
            assert candidate_runtime is not None
            previous = self._current
            candidate = _ProviderGeneration(
                generation_id=candidate_id,
                settings=settings,
                runtime=candidate_runtime,
            )
            self._current = candidate
            self._model_cache.set_available_providers(
                model_cache_provider_ids_for_settings(
                    settings, self._connected_provider_ids()
                )
            )
            self._publish_model_catalog()
            previous.retired = True
            self._retired[previous.generation_id] = previous
            self._trace_published(candidate, previous=previous, reason=reason)
            self._trace_retired(previous, reason=reason)

            if background_refresh:
                self._refresh_task = asyncio.create_task(
                    self._refresh_generation_in_background(
                        candidate, only_missing=False
                    )
                )
            if previous.active_leases == 0:
                await self._close_generation(previous, forced=False)
            return candidate.generation_id

    async def close(self) -> None:
        """Reject new leases, drain existing work, and close every generation."""
        async with self._close_lock:
            if self._closed:
                return
            async with self._replace_lock:
                self._closing = True
                await self._cancel_refresh()
                current = self._current
                if not current.retired:
                    current.retired = True
                    self._retired[current.generation_id] = current
                    self._trace_retired(current, reason="shutdown")
                generations = tuple(self._retired.values())

            await asyncio.gather(
                *(generation.drained.wait() for generation in generations)
            )
            generation_results = await asyncio.gather(
                *(
                    self._close_generation(generation, forced=False)
                    for generation in generations
                )
            )
            unpublished_closed = await self._retry_unpublished_cleanup()
            if not all(generation_results) or not unpublished_closed:
                raise RuntimeError("One or more provider runtimes failed to close.")
            self._model_cache.clear()
            self._closed = True

    async def _release(self, generation: _ProviderGeneration) -> None:
        if generation.active_leases <= 0:
            return
        generation.active_leases -= 1
        if generation.active_leases != 0:
            return
        generation.drained.set()
        if generation.retired and not self._closing:
            await self._close_generation(generation, forced=False)

    async def _refresh_generation(
        self,
        generation: _ProviderGeneration,
        *,
        only_missing: bool,
    ) -> ProviderModelRefreshResult:
        if generation.closed:
            return ProviderModelRefreshResult()
        generation.active_leases += 1
        generation.drained.clear()
        try:
            discovery = ProviderModelDiscovery(
                generation.settings,
                generation.runtime.resolve_provider,
                self._model_cache,
            )
            result = await discovery.refresh_model_list_cache(only_missing=only_missing)
            self._publish_model_catalog()
            return result
        finally:
            await self._release(generation)

    async def _refresh_generation_in_background(
        self,
        generation: _ProviderGeneration,
        *,
        only_missing: bool,
    ) -> None:
        try:
            await self._refresh_generation(generation, only_missing=only_missing)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Provider model discovery task failed: exc_type={}",
                type(exc).__name__,
            )

    async def _cancel_refresh(self) -> None:
        task = self._refresh_task
        self._refresh_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cleanup_unpublished(self, runtime: ProviderRuntime) -> bool:
        self._unpublished.add(runtime)
        try:
            await runtime.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Unpublished provider generation cleanup failed: exc_type={}",
                type(exc).__name__,
            )
            return False
        self._unpublished.discard(runtime)
        return True

    async def _retry_unpublished_cleanup(self) -> bool:
        all_closed = True
        for runtime in tuple(self._unpublished):
            if not await self._cleanup_unpublished(runtime):
                all_closed = False
        return all_closed

    async def _close_generation(
        self,
        generation: _ProviderGeneration,
        *,
        forced: bool,
    ) -> bool:
        if generation.closed:
            return True
        if generation.active_leases != 0:
            return False
        task = generation.cleanup_task
        if task is None:
            task = asyncio.create_task(
                self._run_generation_cleanup(generation, forced=forced),
                name=f"provider-generation-cleanup-{generation.generation_id}",
            )
            generation.cleanup_task = task
        return await asyncio.shield(task)

    async def _run_generation_cleanup(
        self,
        generation: _ProviderGeneration,
        *,
        forced: bool,
    ) -> bool:
        task = asyncio.current_task()
        try:
            try:
                await generation.runtime.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Provider generation cleanup failed: generation_id={} exc_type={}",
                    generation.generation_id,
                    type(exc).__name__,
                )
                return False

            generation.closed = True
            self._retired.pop(generation.generation_id, None)
            trace_event(
                stage="runtime",
                event="provider_generation.closed",
                source="runtime",
                generation_id=generation.generation_id,
                active_leases=generation.active_leases,
                forced=forced,
                outcome="ok",
            )
            return True
        finally:
            if not generation.closed and generation.cleanup_task is task:
                generation.cleanup_task = None

    @staticmethod
    def _trace_published(
        generation: _ProviderGeneration,
        *,
        previous: _ProviderGeneration | None,
        reason: str,
    ) -> None:
        trace_event(
            stage="runtime",
            event="provider_generation.published",
            source="runtime",
            generation_id=generation.generation_id,
            previous_generation_id=(
                previous.generation_id if previous is not None else None
            ),
            reason=reason,
        )

    @staticmethod
    def _trace_retired(generation: _ProviderGeneration, *, reason: str) -> None:
        trace_event(
            stage="runtime",
            event="provider_generation.retired",
            source="runtime",
            generation_id=generation.generation_id,
            active_leases=generation.active_leases,
            reason=reason,
        )
