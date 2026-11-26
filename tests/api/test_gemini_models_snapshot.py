"""Parity, off-loop rebuild, and snapshot-hit tests for ``/v1beta/models``."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.dependencies import get_settings
from my_claude_code.api.gemini_model_catalog import (
    build_gemini_models_payload,
    find_gemini_model_entry,
)
from my_claude_code.application import catalogue_snapshot
from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.application.ports import CatalogueSnapshotInputs
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, provider_manager_for_app


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate({"MODEL": "nvidia_nim/thinker", **overrides})


def _test_app(settings: Settings):
    app = create_test_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_v1beta_models_matches_legacy_payload_byte_for_byte_and_filters_twins() -> None:
    """Snapshot-served list and describe match direct ladder builds byte for byte."""

    settings = _settings()
    app = _test_app(settings)
    manager = provider_manager_for_app(app)
    manager.cache_model_infos(
        "nvidia_nim",
        (
            ProviderModelInfo(
                model_id="thinker",
                supports_thinking=True,
                context_length=128_000,
                max_output_tokens=16_384,
            ),
            ProviderModelInfo(
                model_id="plain-only",
                supports_thinking=False,
                context_length=32_768,
                max_output_tokens=4_096,
            ),
            ProviderModelInfo(
                model_id="model:batch",
                supports_thinking=True,
                context_length=64_000,
            ),
        ),
    )

    expected_list = build_gemini_models_payload(settings, manager)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        listed = client.get("/v1beta/models")
        assert listed.status_code == 200
        assert listed.text == json.dumps(expected_list, separators=(",", ":"))

        # Normal model, surviving no-thinking model re-projected to plain id,
        # suppressed twin, and excluded :batch tier.
        for model_id in (
            "anthropic/nvidia_nim/thinker",
            "anthropic/nvidia_nim/plain-only",
        ):
            expected_entry = find_gemini_model_entry(settings, manager, model_id)
            assert expected_entry is not None
            described = client.get(f"/v1beta/models/{model_id}")
            assert described.status_code == 200
            assert described.text == json.dumps(expected_entry, separators=(",", ":"))

        for hidden_id in (
            "claude-3-freecc-no-thinking/nvidia_nim/thinker",
            "claude-3-freecc-no-thinking/nvidia_nim/plain-only",
            "anthropic/nvidia_nim/model:batch",
        ):
            assert find_gemini_model_entry(settings, manager, hidden_id) is None
            assert client.get(f"/v1beta/models/{hidden_id}").status_code == 404


def test_v1beta_models_rebuilds_off_loop_once_and_hits_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misses rebuild off-loop once; hits perform zero ladder builds."""

    settings = _settings()
    app = _test_app(settings)
    manager = provider_manager_for_app(app)
    manager.cache_model_infos(
        "nvidia_nim", (ProviderModelInfo(model_id="thinker", supports_thinking=True),)
    )

    build_threads: list[int] = []
    real_build = catalogue_snapshot.build_catalogue_models

    def _recording_build(*args: Any, **kwargs: Any) -> tuple[CatalogueModel, ...]:
        build_threads.append(threading.get_ident())
        return real_build(*args, **kwargs)

    monkeypatch.setattr(catalogue_snapshot, "build_catalogue_models", _recording_build)

    loop_thread = threading.get_ident()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        first_list = client.get("/v1beta/models")
        assert first_list.status_code == 200
        assert len(build_threads) == 1
        assert build_threads[0] != loop_thread

        # Warm hit for list, describe, and /v1/models -- zero extra builds.
        second_list = client.get("/v1beta/models")
        described = client.get("/v1beta/models/anthropic/nvidia_nim/thinker")
        v1_list = client.get("/v1/models")
        assert second_list.status_code == 200
        assert described.status_code == 200
        assert v1_list.status_code == 200
        assert len(build_threads) == 1


def test_v1beta_models_invalidates_when_openrouter_live_cache_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An updated openrouter-models.json invalidates the snapshot without a publish."""

    cache_path = tmp_path / "openrouter-models.json"
    monkeypatch.setattr(
        catalogue_snapshot, "openrouter_live_cache_path", lambda: cache_path
    )
    settings = _settings(MODEL_METADATA_OPENROUTER_LIVE=True)
    app = _test_app(settings)
    manager = provider_manager_for_app(app)
    manager.cache_model_infos(
        "nvidia_nim", (ProviderModelInfo(model_id="thinker", supports_thinking=True),)
    )

    builds = 0
    real_build = catalogue_snapshot.build_catalogue_models

    def _counting_build(*args: Any, **kwargs: Any) -> tuple[CatalogueModel, ...]:
        nonlocal builds
        builds += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(catalogue_snapshot, "build_catalogue_models", _counting_build)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/v1beta/models").status_code == 200
        assert builds == 1
        assert client.get("/v1beta/models").status_code == 200
        assert builds == 1

        cache_path.write_text('{"data": []}', encoding="utf-8")
        assert client.get("/v1beta/models").status_code == 200
        assert builds == 2


def test_v1beta_models_isolates_snapshot_by_runtime_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot populated for app A is never served to app B."""

    settings_a = Settings.model_validate({"MODEL": "nvidia_nim/model-a"})
    settings_b = Settings.model_validate({"MODEL": "nvidia_nim/model-b"})
    app_a = _test_app(settings_a)
    app_b = _test_app(settings_b)
    manager_a = provider_manager_for_app(app_a)
    manager_b = provider_manager_for_app(app_b)
    manager_a.cache_model_infos(
        "nvidia_nim", (ProviderModelInfo(model_id="model-a", supports_thinking=True),)
    )
    manager_b.cache_model_infos(
        "nvidia_nim", (ProviderModelInfo(model_id="model-b", supports_thinking=True),)
    )
    monkeypatch.setattr(
        manager_a,
        "catalogue_snapshot_inputs",
        lambda: CatalogueSnapshotInputs(1, 0, None),
    )
    monkeypatch.setattr(
        manager_b,
        "catalogue_snapshot_inputs",
        lambda: CatalogueSnapshotInputs(1, 0, None),
    )

    with TestClient(app_a, client=("127.0.0.1", 50000)) as client_a:
        res_a = client_a.get("/v1beta/models")
    with TestClient(app_b, client=("127.0.0.1", 50000)) as client_b:
        res_b = client_b.get("/v1beta/models")

    assert res_a.status_code == 200
    assert res_b.status_code == 200
    names_a = {entry["name"] for entry in res_a.json()["models"]}
    names_b = {entry["name"] for entry in res_b.json()["models"]}
    assert "models/anthropic/nvidia_nim/model-a" in names_a
    assert "models/anthropic/nvidia_nim/model-a" not in names_b
    assert "models/anthropic/nvidia_nim/model-b" in names_b
