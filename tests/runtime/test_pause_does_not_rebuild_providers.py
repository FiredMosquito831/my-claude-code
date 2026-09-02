"""A pause must not rebuild providers, and a sweep must not hold the loop.

The pause click cost ~2 s in 6.35.0 and none of it was pause work: the admin
apply always asked ``ProviderRuntimeManager.replace`` for its default
background sweep, so 19-22 provider clients were constructed inside the call
and every ``/models`` was re-queried for a change no provider can read. These
tests fail loudly on that behaviour.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from my_claude_code.config.admin.manifest import (
    FIELD_BY_KEY,
    update_affects_providers,
)
from my_claude_code.config.admin.persistence import PreparedAdminUpdate
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.providers.runtime.discovery import (
    ProviderModelDiscovery,
    model_list_provider_ids_for_settings,
)
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

#: One provider resolve is an SSL context plus an HTTP client: ~78 ms measured
#: on the fleet. The fake is slower still, so "did any resolve happen" is a
#: question the wall clock can answer on its own.
RESOLVE_SECONDS = 0.2


class SlowResolveRuntime(ProviderRuntime):
    """A runtime whose provider construction is expensive and counted."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.resolve_calls: list[str] = []

    def resolve_provider(self, provider_id: str) -> BaseProvider:
        self.resolve_calls.append(provider_id)
        time.sleep(RESOLVE_SECONDS)
        raise RuntimeError(f"discovery is not the subject: {provider_id}")


class SlowResolveFactory:
    def __init__(self) -> None:
        self.runtimes: list[SlowResolveRuntime] = []

    def __call__(self, settings: Settings) -> ProviderRuntime:
        runtime = SlowResolveRuntime(settings)
        self.runtimes.append(runtime)
        return runtime

    @property
    def resolve_calls(self) -> list[str]:
        return [call for runtime in self.runtimes for call in runtime.resolve_calls]


def _settings(**overrides: str) -> Settings:
    return Settings().model_copy(
        update={
            "model": "nvidia_nim/nvidia/model-a",
            "model_opus_fallbacks": "nvidia_nim/nvidia/model-a",
            "nvidia_api_key": "nvidia-test-key",
            "open_router_api_key": "open-router-test-key",
            "groq_api_key": "groq-test-key",
            "port": 8123,
            **overrides,
        }
    )


def _prepared(settings: Settings, tmp_path) -> PreparedAdminUpdate:
    return PreparedAdminUpdate(
        target_values={"MODEL": settings.model},
        settings=settings,
        errors=(),
        pending_fields=(),
        path=tmp_path / ".env",
    )


def _applied_response() -> dict[str, object]:
    return {
        "applied": True,
        "valid": True,
        "errors": [],
        "warnings": [],
        "env_preview": "MODEL=updated\n",
        "path": ".env",
        "pending_fields": [],
    }


async def _apply(runtime, updates, prepared) -> float:
    """Apply one admin update with the real replace path, returning seconds."""
    with (
        patch(
            "my_claude_code.runtime.application.prepare_admin_update",
            return_value=prepared,
        ),
        patch(
            "my_claude_code.runtime.application.commit_prepared_admin_update",
            side_effect=lambda _prepared: _applied_response(),
        ),
    ):
        started = time.perf_counter()
        await runtime.apply_admin_config(updates)
        return time.perf_counter() - started


def test_the_manifest_owns_the_provider_inert_answer() -> None:
    """The decision is a manifest fact, not a key list written at the caller."""
    assert FIELD_BY_KEY["MODEL_OPUS_PAUSED"].affects_providers is False
    assert FIELD_BY_KEY["NVIDIA_NIM_API_KEY"].affects_providers is True
    for key in (
        "MODEL_PAUSED",
        "MODEL_FABLE_PAUSED",
        "MODEL_OPUS_PAUSED",
        "MODEL_SONNET_PAUSED",
        "MODEL_HAIKU_PAUSED",
        "MODEL_VISION_PAUSED",
    ):
        assert update_affects_providers([key]) is False, key
    # A key the manifest does not own gets the expensive, safe answer.
    assert update_affects_providers(["A_KEY_ADDED_TOMORROW"]) is True
    # One provider-affecting key in the batch is enough.
    assert update_affects_providers(["MODEL_OPUS_PAUSED", "NVIDIA_NIM_API_KEY"]) is True


@pytest.mark.asyncio
async def test_a_pause_apply_does_not_sweep_every_provider(tmp_path) -> None:
    """A pause must construct no provider and start no discovery at all."""
    factory = SlowResolveFactory()
    settings = _settings()
    manager = ProviderRuntimeManager(settings, runtime_factory=factory)
    runtime = ApplicationRuntime(manager, transcriber=None)
    # More than one provider is discoverable, so a sweep would be visible.
    assert len(model_list_provider_ids_for_settings(settings)) > 1

    elapsed = await _apply(
        runtime,
        {"MODEL_OPUS_PAUSED": "nvidia_nim/nvidia/model-a"},
        _prepared(_settings(), tmp_path),
    )

    assert factory.resolve_calls == []
    assert manager._refresh_task is None
    assert elapsed < RESOLVE_SECONDS
    assert manager.current_generation_id == 2
    await manager.close()
    assert factory.resolve_calls == []


@pytest.mark.asyncio
async def test_a_no_op_pause_costs_no_provider_work_either(tmp_path) -> None:
    """The byte-identical pause paid the same 2 s; now it pays for nothing."""
    factory = SlowResolveFactory()
    manager = ProviderRuntimeManager(_settings(), runtime_factory=factory)
    runtime = ApplicationRuntime(manager, transcriber=None)

    updates = {"MODEL_OPUS_PAUSED": ""}
    first = await _apply(runtime, updates, _prepared(_settings(), tmp_path))
    second = await _apply(runtime, updates, _prepared(_settings(), tmp_path))

    assert factory.resolve_calls == []
    assert max(first, second) < RESOLVE_SECONDS
    await manager.close()


@pytest.mark.asyncio
async def test_a_provider_key_change_still_rebuilds_and_sweeps(tmp_path) -> None:
    """The negative case: the fast path must not swallow a real change."""
    factory = SlowResolveFactory()
    manager = ProviderRuntimeManager(_settings(), runtime_factory=factory)
    runtime = ApplicationRuntime(manager, transcriber=None)

    await _apply(
        runtime,
        {"NVIDIA_NIM_API_KEY": "a-different-key"},
        _prepared(_settings(nvidia_api_key="a-different-key"), tmp_path),
    )

    task = manager._refresh_task
    assert task is not None
    await asyncio.gather(task, return_exceptions=True)
    assert factory.resolve_calls, "a provider key change must re-sweep discovery"
    await manager.close()


@pytest.mark.asyncio
async def test_a_cancelled_sweep_does_not_block_the_next_replace() -> None:
    """The resolve loop yields, so ``task.cancel()`` lands inside it.

    Without the yield the loop runs to the end before its first suspension
    point, so a cancel cannot take effect and the next admin apply waits out a
    sweep it already asked to abandon.
    """
    settings = _settings()
    provider_ids = model_list_provider_ids_for_settings(settings)
    assert len(provider_ids) > 2
    resolved: list[str] = []

    def resolver(provider_id: str) -> BaseProvider:
        resolved.append(provider_id)
        raise RuntimeError("discovery is not the subject")

    discovery = ProviderModelDiscovery(settings, resolver, ProviderModelCache(), ())
    task = asyncio.create_task(discovery.refresh_model_list_cache())
    # One scheduling turn: the loop has started and yielded, not finished.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(resolved) < len(provider_ids), (
        "the cancel landed only after the whole loop ran: there is no yield"
    )
