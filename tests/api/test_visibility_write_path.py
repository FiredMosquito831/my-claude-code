"""What a Hide or Show click is allowed to cost.

Two independent mechanisms made one click take tens of seconds, and neither of
them was visibility work:

1. Both visibility write routes built the *entire* Models page -- every
   capability ladder, a full ``fnmatch`` sweep over every ref, and two
   aggregates over a multi-gigabyte request log -- and then used one dictionary
   out of it, the map from model ref to provider id.
2. ``MODEL_VISIBILITY_ALLOW``/``DENY`` were the only listing-only settings that
   did not declare ``affects_providers=False``, so every click also re-queried
   every provider's ``/models``.

These tests fail loudly on either behaviour returning, and the equality half of
each is an assertion that the cheap answer is the *same* answer.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.api.model_admin import (
    build_models_page_payload,
    model_refs_by_provider,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.admin.manifest import FIELD_BY_KEY, update_affects_providers
from my_claude_code.config.admin.persistence import PreparedAdminUpdate
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    reset_model_overrides_cache,
)
from my_claude_code.config.model_refs import configured_chat_model_refs
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.providers.runtime.discovery import (
    model_list_provider_ids_for_settings,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.api.support import create_test_app, provider_manager_for_app

MODELS_ENDPOINT = "/admin/api/model-admin"
BULK_ENDPOINT = f"{MODELS_ENDPOINT}/visibility/bulk"
MIGRATE_ENDPOINT = f"{MODELS_ENDPOINT}/visibility/migrate-globs"

#: One provider resolve is an SSL context plus an HTTP client; the fake below
#: makes it slower still, so "did any resolve happen" is answerable.
RESOLVE_SECONDS = 0.2


def _isolated_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in (
        "MODEL",
        "MODEL_VISIBILITY_ALLOW",
        "MODEL_VISIBILITY_DENY",
        "OPEN_ROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()


def _app_with_models(**visibility: str):
    settings = Settings()
    settings.model = "open_router/routed"
    settings.open_router_api_key = "open-router-key"
    for name, value in visibility.items():
        setattr(settings, name, value)
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("routed", max_output_tokens=16384),
            ProviderModelInfo("extra", context_length=128000),
            ProviderModelInfo("Zebra", context_length=8000),
        },
    )
    provider_manager_for_app(app).cache_model_infos(
        "groq",
        {ProviderModelInfo("apple", context_length=8000)},
    )
    return app


def _local_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _bulk(client: TestClient, **body):
    payload = {"scope": "provider", "action": "hide", "model_refs": []}
    payload.update(body)
    return client.post(BULK_ENDPOINT, json=payload)


# ------------------------------------------------------ the map, and equality


def test_the_bulk_map_equals_the_page_map_including_its_order(monkeypatch, tmp_path):
    """The equality oracle: same pairs, same order, from a hundredth of the work.

    Order is load-bearing, not cosmetic. The bulk route turns this map into the
    list of refs it writes patterns for, so a different iteration order would
    write a different (still correct, but different) set of patterns.
    """

    _isolated_home(monkeypatch, tmp_path)
    settings = Settings()
    settings.model = "open_router/routed"
    settings.open_router_api_key = "open-router-key"
    infos = (
        ProviderModelInfo("open_router/Zebra", context_length=8000),
        ProviderModelInfo("open_router/apple", context_length=8000),
        ProviderModelInfo("groq/llama", context_length=8000),
    )
    configured = tuple(configured_chat_model_refs(settings))

    page = build_models_page_payload(
        infos,
        configured,
        ModelVisibility(),
        ModelParameterOverrides(),
    )
    from_page: dict[str, str] = {}
    for provider in page["providers"]:
        for model in provider["models"]:
            from_page[str(model["model_ref"])] = str(provider["provider_id"])

    assert list(model_refs_by_provider(infos, configured).items()) == list(
        from_page.items()
    )


def test_the_map_unions_the_catalogue_with_the_configured_refs():
    """A configured ref no provider published still has to be hideable."""

    infos = (ProviderModelInfo("open_router/published", context_length=8000),)

    mapped = model_refs_by_provider(infos, configured_chat_model_refs(Settings()))

    assert mapped["open_router/published"] == "open_router"
    # Whatever the shipped default route names, it is in the map too.
    assert len(mapped) > 1


# ------------------------------------------- the routes no longer build a page


def _forbid_page_build(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError(
            "a visibility write must not build the Models page payload"
        )

    monkeypatch.setattr(admin_routes, "_models_page_payload", explode)


def test_bulk_visibility_does_not_build_the_models_page(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())
    _forbid_page_build(monkeypatch)

    body = _bulk(client, provider_id="open_router").json()

    assert body["visibility"]["deny"] == ["open_router/*"]
    assert body["wrote_glob"] == "open_router/*"


def test_bulk_visibility_on_a_selection_does_not_build_the_models_page(
    monkeypatch, tmp_path
):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())
    _forbid_page_build(monkeypatch)

    body = _bulk(
        client, provider_id="open_router", model_refs=["open_router/routed"]
    ).json()

    assert body["visibility"]["deny"] == ["open_router/routed"]
    assert [row["model_ref"] for row in body["results"]] == ["open_router/routed"]


def test_the_glob_migration_does_not_build_the_models_page(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models(
        model_visibility_deny="open_router/routed,open_router/extra,open_router/Zebra"
    )
    client = _local_client(app)
    _forbid_page_build(monkeypatch)

    body = client.post(MIGRATE_ENDPOINT, json={"apply": False}).json()

    # Every model of the provider is denied by an exact pattern, so the
    # migration has a fold to offer -- which it can only know from the map.
    assert body["added_patterns"] == ["open_router/*"]
    assert body["providers"] == ["open_router"]


def test_bulk_visibility_never_opens_the_request_log(monkeypatch, tmp_path):
    """The map costs no query at all; the page it replaced cost two aggregates."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    def explode(*_args, **_kwargs):
        raise AssertionError("a visibility write must not query the request log")

    monkeypatch.setattr(admin_routes, "_request_log_store_or_none", explode)

    assert _bulk(client, provider_id="open_router").status_code == 200


# ------------------------------------------------- and no longer sweeps models


class SlowResolveRuntime(ProviderRuntime):
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
            "nvidia_api_key": "nvidia-test-key",
            "open_router_api_key": "open-router-test-key",
            "groq_api_key": "groq-test-key",
            "port": 8123,
            **overrides,
        }
    )


def _prepared(settings: Settings, tmp_path) -> PreparedAdminUpdate:
    return PreparedAdminUpdate(
        target_values={"MODEL_VISIBILITY_DENY": settings.model_visibility_deny},
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
        "env_preview": "MODEL_VISIBILITY_DENY=updated\n",
        "path": ".env",
        "pending_fields": [],
    }


async def _apply(runtime, updates, prepared) -> float:
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


def test_visibility_keys_do_not_affect_providers() -> None:
    """Hide-only, by contract -- so it cannot change a provider client."""

    for key in ("MODEL_VISIBILITY_ALLOW", "MODEL_VISIBILITY_DENY"):
        assert FIELD_BY_KEY[key].affects_providers is False, key
        assert update_affects_providers([key]) is False, key
    # The batch rule is unchanged: one provider-affecting key still wins.
    assert (
        update_affects_providers(["MODEL_VISIBILITY_DENY", "NVIDIA_NIM_API_KEY"])
        is True
    )


@pytest.mark.asyncio
async def test_a_visibility_write_does_not_sweep_every_provider(tmp_path) -> None:
    factory = SlowResolveFactory()
    settings = _settings()
    manager = ProviderRuntimeManager(settings, runtime_factory=factory)
    runtime = ApplicationRuntime(manager, transcriber=None)
    assert len(model_list_provider_ids_for_settings(settings)) > 1

    elapsed = await _apply(
        runtime,
        {"MODEL_VISIBILITY_DENY": "open_router/*"},
        _prepared(_settings(model_visibility_deny="open_router/*"), tmp_path),
    )

    assert factory.resolve_calls == []
    assert manager._refresh_task is None
    assert elapsed < RESOLVE_SECONDS
    # The generation still swaps: the next request must see the new setting.
    assert manager.current_generation_id == 2
    await manager.close()


@pytest.mark.asyncio
async def test_a_provider_key_change_still_sweeps(tmp_path) -> None:
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
    assert factory.resolve_calls
    await manager.close()
