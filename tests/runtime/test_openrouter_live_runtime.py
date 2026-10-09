"""The runtime binds OpenRouter's live list for listings and never for routing (7.84.0).

Q5 of the user's decisions (2026-10-08 21:33): OpenRouter's numbers never feed
request routing -- no output-token clamp, no context headroom, no reasoning
gating. Every lookup a request is built from is therefore compared here with
the live list absent, present and on, and present but switched off. The list
is fetched in the background after a catalogue sweep when it is due, one
download at a time, and a switch turned on fetches at once.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.providers.runtime import openrouter_catalogue
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache
from my_claude_code.providers.runtime.openrouter_catalogue import (
    OpenRouterLiveRefresh,
    openrouter_live_cache_path,
    write_openrouter_live_cache,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

ROW: dict[str, Any] = {
    "id": "z-ai/glm-5",
    "created": 1770829182,
    "context_length": 204800,
    "architecture": {
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
    },
    "pricing": {"prompt": "0.0000006", "completion": "0.00000192"},
    "top_provider": {"context_length": 198000, "max_completion_tokens": 128000},
    "supported_parameters": ["max_tokens", "reasoning", "tools"],
}

MODELS_DEV: dict[str, Any] = {
    "novita": {
        "id": "novita",
        "models": {
            "zai-org/glm-5": {
                "id": "zai-org/glm-5",
                "reasoning": False,
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 100000, "output": 8000},
            }
        },
    }
}

ROUTING_LOOKUPS = (
    "model_output_limit",
    "model_output_limit_tiered",
    "model_context_length",
    "model_reasoning_capability",
    "model_reasoning_dialect",
    "cached_model_supports_vision",
    "cached_model_supports_thinking",
)


def _settings(**update: Any) -> Settings:
    return Settings.model_validate({"OPENCODE_FREE_TIER_CREDENTIAL": "key", **update})


def _manager(settings: Settings) -> ProviderRuntimeManager:
    manager = ProviderRuntimeManager(settings)
    manager._model_cache.add_provider("nous_portal")
    manager._model_cache.add_provider("novita")
    manager._model_cache.cache_model_infos(
        "nous_portal", (ProviderModelInfo("z-ai/glm-5"),)
    )
    manager._model_cache.cache_model_infos("novita", ())
    return manager


def _routing(manager: ProviderRuntimeManager) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for provider_id, model_id in (
        ("nous_portal", "z-ai/glm-5"),
        ("novita", "zai-org/glm-5"),
        ("nous_portal", "unlisted/model"),
    ):
        for name in ROUTING_LOOKUPS:
            answers[f"{provider_id}/{model_id}:{name}"] = repr(
                getattr(manager, name)(provider_id, model_id)
            )
    return answers


def test_no_lookup_a_request_is_built_from_reads_the_live_list() -> None:
    write_models_dev_cache(MODELS_DEV)
    absent = _routing(_manager(_settings()))
    write_openrouter_live_cache([ROW], openrouter_live_cache_path())
    on = _manager(_settings())
    assert on.openrouter_live_catalogue() is not None
    off = _manager(_settings(MODEL_METADATA_OPENROUTER_LIVE="false"))
    assert off.openrouter_live_catalogue() is None
    assert _routing(on) == absent
    assert _routing(off) == absent


def test_whether_a_model_reasons_comes_with_its_rung() -> None:
    write_models_dev_cache(MODELS_DEV)
    manager = _manager(_settings())
    manager._model_cache.cache_model_infos(
        "nous_portal",
        (
            ProviderModelInfo(
                "z-ai/glm-5",
                reasoning_capability=ModelReasoningCapability(can_reason=True),
            ),
            ProviderModelInfo("acme/flag", supports_thinking=False),
        ),
    )
    assert manager.model_can_reason_tiered("nous_portal", "z-ai/glm-5") == (
        True,
        ResolutionTier.PROVIDER_EXACT,
    )
    assert manager.model_can_reason_tiered("nous_portal", "acme/flag") == (
        False,
        ResolutionTier.PROVIDER_EXACT,
    )
    assert manager.model_can_reason_tiered("novita", "zai-org/glm-5") == (
        False,
        ResolutionTier.MODELS_DEV_BUCKET_EXACT,
    )
    assert manager.model_can_reason_tiered("novita", "nothing/here") == (None, None)
    # The same answer routing's merged capability carries.
    merged = manager.model_reasoning_capability("novita", "zai-org/glm-5")
    assert merged is not None and merged.can_reason is False


def _recording_refresh(monkeypatch: pytest.MonkeyPatch, status: str = "fetched"):
    calls: list[tuple[Settings, Path]] = []

    async def refresh(settings: Settings, path: Path) -> OpenRouterLiveRefresh:
        calls.append((settings, path))
        return OpenRouterLiveRefresh(status=status)

    monkeypatch.setattr(
        "my_claude_code.runtime.provider_manager.refresh_openrouter_live", refresh
    )
    return calls


@pytest.mark.asyncio
async def test_a_sweep_fetches_the_list_when_it_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _recording_refresh(monkeypatch)
    manager = ProviderRuntimeManager(_settings())
    published: list[str] = []
    monkeypatch.setattr(
        manager, "_publish_model_catalog", lambda: published.append("p")
    )
    try:
        await manager.refresh_model_list_cache()
        swept = len(published)
        task = manager._openrouter_live_task
        assert task is not None
        await task
        assert len(calls) == 1
        assert calls[0][1] == openrouter_live_cache_path()
        # A stored list means the agent catalogues are republished on it.
        assert len(published) == swept + 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_no_fetch_when_off_fresh_or_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _recording_refresh(monkeypatch)
    off = ProviderRuntimeManager(_settings(MODEL_METADATA_OPENROUTER_LIVE="false"))
    try:
        await off.refresh_model_list_cache()
        await asyncio.sleep(0)
        assert calls == []
    finally:
        await off.close()

    write_openrouter_live_cache([ROW], openrouter_live_cache_path())
    fresh = ProviderRuntimeManager(_settings())
    try:
        await fresh.refresh_model_list_cache()
        await asyncio.sleep(0)
        assert calls == []
        # Past the models.dev freshness it is due again -- once at a time.
        old = time.time() - 3 * 86400
        os.utime(openrouter_live_cache_path(), (old, old))
        gate = asyncio.Event()

        async def slow(settings: Settings, path: Path) -> OpenRouterLiveRefresh:
            calls.append((settings, path))
            await gate.wait()
            return OpenRouterLiveRefresh(status="failed")

        monkeypatch.setattr(
            "my_claude_code.runtime.provider_manager.refresh_openrouter_live", slow
        )
        fresh.schedule_openrouter_live_refresh()
        fresh.schedule_openrouter_live_refresh()
        await asyncio.sleep(0)
        assert len(calls) == 1
        gate.set()
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_switching_the_rung_on_fetches_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _settings(MODEL_METADATA_OPENROUTER_LIVE="false")
    runtime = ApplicationRuntime(ProviderRuntimeManager(previous), transcriber=None)
    scheduled: list[Settings | None] = []
    monkeypatch.setattr(
        runtime.provider_manager,
        "schedule_openrouter_live_refresh",
        lambda settings=None: scheduled.append(settings),
    )
    runtime._started = True
    try:
        current = _settings()
        await runtime._apply_live_settings(previous, current)
        assert scheduled == [current]
        await runtime._apply_live_settings(current, current)
        assert len(scheduled) == 1
    finally:
        runtime._started = False
        await runtime.provider_manager.close()


def test_the_store_lives_beside_models_dev(tmp_path: Path) -> None:
    path = openrouter_live_cache_path()
    assert path.name == "openrouter-models.json"
    assert path.parent.name == "cache"
    write_openrouter_live_cache([ROW], path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {"fetched_at", "source_url", "data"}
    assert stored["data"] == [ROW]
    assert openrouter_catalogue.read_openrouter_live_rows(path) is not None
