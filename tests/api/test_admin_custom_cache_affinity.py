"""Admin API round-trip and validation tests for custom provider ``cache_affinity`` (8.29.0)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.provider_registry import (
    CUSTOM_CACHE_AFFINITY_MODES,
    ProviderRegistry,
    get_provider_registry,
)
from tests.api.support import create_test_app, runtime_for_app

KEY = "sk-mistral-custom-" + "a" * 24


def _admin_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    app = create_test_app()
    runtime = runtime_for_app(app)
    monkeypatch.setattr(runtime, "reload_providers", AsyncMock(return_value={}))
    monkeypatch.setattr(runtime, "cached_model_ids", lambda: {})
    monkeypatch.setattr(
        runtime, "probe_custom_provider_dialect", AsyncMock(return_value={})
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def _create(client: TestClient, **extra: Any) -> httpx.Response:
    return client.post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Mistral Custom",
            "base_url": "https://api.mistral.ai/v1",
            "api_key": KEY,
            **extra,
        },
    )


def _stored(registry: ProviderRegistry) -> dict[str, Any]:
    raw = json.loads(registry._storage_path().read_text(encoding="utf-8"))
    (entry,) = raw["providers"]
    return entry


def test_custom_cache_affinity_defaults_off_and_omits_disk_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _admin_client(monkeypatch, tmp_path)

    created = _create(client)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["cache_affinity"] == "off"
    assert [entry["value"] for entry in body["available_cache_affinity_modes"]] == list(
        CUSTOM_CACHE_AFFINITY_MODES
    )
    registry = get_provider_registry()
    assert "cache_affinity" not in _stored(registry)


@pytest.mark.parametrize("mode", ["body", "header", "both", "off"])
def test_custom_cache_affinity_round_trips_through_post_patch_and_get(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _admin_client(monkeypatch, tmp_path)
    created = _create(client, cache_affinity=mode)
    assert created.status_code == 200, created.text
    assert created.json()["cache_affinity"] == mode

    listed = client.get("/admin/api/custom-providers").json()["providers"]
    assert listed[0]["cache_affinity"] == mode

    registry = get_provider_registry()
    stored = _stored(registry)
    if mode == "off":
        assert "cache_affinity" not in stored
    else:
        assert stored["cache_affinity"] == mode

    patched = client.patch(
        "/admin/api/custom-providers/custom_mistral_custom",
        json={"cache_affinity": "both" if mode != "both" else "off"},
    )
    assert patched.status_code == 200, patched.text
    expected_next = "both" if mode != "both" else "off"
    assert patched.json()["cache_affinity"] == expected_next


def test_custom_cache_affinity_rejects_invalid_values_with_422(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _admin_client(monkeypatch, tmp_path)

    refused_create = _create(client, cache_affinity="invalid-mode")
    assert refused_create.status_code == 422

    created = _create(client)
    assert created.status_code == 200

    refused_patch = client.patch(
        "/admin/api/custom-providers/custom_mistral_custom",
        json={"cache_affinity": "cookie"},
    )
    assert refused_patch.status_code == 422
    entry = get_provider_registry().get("custom_mistral_custom")
    assert entry is not None
    assert entry.cache_affinity == "off"
