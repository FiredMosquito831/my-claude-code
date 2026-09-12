"""A start reads the catalogue it already knows, then sweeps anyway.

`ProviderModelCache` was a dict in memory, so every start rediscovered every
provider's `/models` before the first Models page or the first `/v1/models`
could be answered. These hold the two halves of the fix: the load happens and
is complete, and it never pretends to be a sweep.
"""

import time

import pytest

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.runtime.catalogue_store import (
    catalogue_scope_key,
    store_catalogue,
)
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager


class _FakeRuntime(ProviderRuntime):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_cached(self, provider_id: str) -> bool:
        return False

    def resolve_provider(self, provider_id: str) -> BaseProvider:
        raise AssertionError("the stored catalogue must not ask a provider anything")

    async def cleanup(self) -> None:
        return None


def _manager() -> ProviderRuntimeManager:
    return ProviderRuntimeManager(Settings(), runtime_factory=_FakeRuntime)


def _seed(manager: ProviderRuntimeManager, written_at: float) -> str:
    """Write a catalogue for exactly the scope this manager will ask for."""

    scope = manager._model_cache.cached_scope()
    provider_id = sorted(scope)[0]
    catalogues = {
        provider_id: (
            ProviderModelInfo(model_id="one", context_length=128_000),
            ProviderModelInfo(model_id="two", supports_vision=True),
        )
    }
    key = catalogue_scope_key(scope)
    assert store_catalogue(catalogues, key, computed_at=written_at)
    return provider_id


@pytest.mark.asyncio
async def test_a_start_loads_the_stored_catalogue_before_the_sweep() -> None:
    """No provider is asked anything -- `_FakeRuntime` refuses to be resolved."""
    manager = _manager()
    try:
        written_at = time.time() - 2400.0
        provider_id = _seed(manager, written_at)

        assert manager.load_stored_catalogue() == 2

        cached = manager.cached_model_ids()
        assert cached[provider_id] == frozenset({"one", "two"})
        assert manager.model_context_length(provider_id, "one") == 128_000
        assert manager.cached_model_supports_vision(provider_id, "two") is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_the_loaded_catalogue_reports_the_age_it_was_written_with() -> None:
    """ "as of 40 min ago", and the page is told it was not a sweep."""
    manager = _manager()
    try:
        written_at = time.time() - 2400.0
        _seed(manager, written_at)
        manager.load_stored_catalogue()

        assert manager.last_catalogue_refresh_at == written_at
        assert manager.catalogue_from_store is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_no_stored_catalogue_leaves_the_start_exactly_as_it_was() -> None:
    """Every release before this one: nothing until the sweep answers."""
    manager = _manager()
    try:
        assert manager.load_stored_catalogue() == 0
        assert manager.cached_model_ids() == {}
        assert manager.last_catalogue_refresh_at is None
        assert manager.catalogue_from_store is False
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_document_from_another_scope_is_not_loaded() -> None:
    """A catalogue for a different installation is not this installation's."""
    manager = _manager()
    try:
        assert store_catalogue(
            {
                "somebody_elses_gateway": (
                    ProviderModelInfo(model_id="one"),
                    ProviderModelInfo(model_id="two"),
                )
            },
            catalogue_scope_key(("somebody_elses_gateway",)),
            computed_at=time.time(),
        )

        assert manager.load_stored_catalogue() == 0
        assert manager.cached_model_ids() == {}
        assert manager.catalogue_from_store is False
    finally:
        await manager.close()
