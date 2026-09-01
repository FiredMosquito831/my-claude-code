"""Admin custom provider CRUD endpoints against the real provider registry.

These tests used to inject a hand-written fake registry. Its ``add()`` took a
prebuilt entry, the real ``ProviderRegistry.add()`` takes the fields and
allocates the id itself, and nothing compared the two -- so every create
returned HTTP 500 in production while this file stayed green. The registry is
an in-memory dict plus one JSON file, so there is no reason to double it: point
the real thing at ``tmp_path`` and the contract cannot drift again.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_custom_routes
from my_claude_code.config.provider_registry import ProviderRegistry
from tests.api.support import (
    ModelListingProviderDouble,
    create_custom_provider_app,
    create_test_app,
    runtime_for_app,
)

_ENV_KEYS = ("FCC_ENV_FILE",)


def _registry(tmp_path: Path) -> ProviderRegistry:
    return ProviderRegistry(tmp_path / "custom_providers.json")


def _seeded_registry(tmp_path: Path, **overrides: Any) -> ProviderRegistry:
    """Return a registry holding one ``custom_acme`` entry."""
    registry = _registry(tmp_path)
    entry = registry.add(
        display_name="Acme",
        base_url="https://api.acme.example/v1",
        api_keys=("sk-acme-aaaa1111bbbb",),
        credential_rotation="failover",
    )
    if overrides:
        registry.update(entry.provider_id, **overrides)
    return registry


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _make_app(
    monkeypatch,
    tmp_path: Path,
    registry: ProviderRegistry,
    *,
    discovered: tuple[str, ...] = ("m2", "m1"),
    failure: str | None = None,
):
    """Build the app with a runtime whose scoped reload fills the catalogue.

    The double mirrors the production seam exactly: ``reload_providers`` is the
    *only* thing that puts models in the catalogue, and the route reads them
    back through ``cached_model_ids``. A route that answered from its own
    second probe would now report zero -- which is the regression this file
    exists to pin.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    app = create_test_app()
    runtime = runtime_for_app(app)
    cached: dict[str, frozenset[str]] = {}

    async def _reload(*, reason: str, refresh_provider_id: str | None = None):
        assert reason == "custom_provider_change"
        if refresh_provider_id is None:
            return {}
        if failure is not None:
            return {
                "provider_id": refresh_provider_id,
                "ok": False,
                "model_count": 0,
                "error_type": failure,
                "message": f"{failure}: upstream refused the model list",
            }
        cached[refresh_provider_id] = frozenset(discovered)
        return {
            "provider_id": refresh_provider_id,
            "ok": True,
            "model_count": len(discovered),
        }

    reload_providers = AsyncMock(side_effect=_reload)
    monkeypatch.setattr(runtime, "reload_providers", reload_providers)
    monkeypatch.setattr(runtime, "cached_model_ids", lambda: dict(cached))
    monkeypatch.setattr(
        runtime,
        "test_provider",
        AsyncMock(side_effect=AssertionError("create must not run a second probe")),
    )
    app.dependency_overrides[admin_custom_routes.get_custom_provider_registry] = (
        lambda: registry
    )
    return app, reload_providers


def test_list_custom_providers_empty(monkeypatch, tmp_path):
    app, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).get("/admin/api/custom-providers")

    assert response.status_code == 200
    assert response.json() == {"providers": []}


def test_list_custom_providers_serializes_entries(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).get("/admin/api/custom-providers")

    assert response.status_code == 200
    (provider,) = response.json()["providers"]
    assert provider["provider_id"] == "custom_acme"
    assert provider["display_name"] == "Acme"
    assert provider["base_url"] == "https://api.acme.example/v1"
    assert provider["key_count"] == 1
    assert provider["masked_keys"] == ["sk-acm…bbbb"]
    assert provider["credential_rotation"] == "failover"
    assert provider["proxy"] is None
    assert provider["enabled"] is True
    assert provider["status"] == "configured"
    assert provider["models"] == []
    assert provider["model_count"] == 0
    assert provider["added_at"].startswith("20")
    assert "sk-acme-aaaa1111bbbb" not in response.text


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"enabled": False}, "disabled"),
        ({"api_keys": ()}, "missing_key"),
        ({"api_keys": (), "enabled": False}, "disabled"),
    ],
)
def test_list_custom_providers_status_mapping(monkeypatch, tmp_path, overrides, status):
    registry = _seeded_registry(tmp_path, **overrides)
    app, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).get("/admin/api/custom-providers")

    assert response.json()["providers"][0]["status"] == status


def test_create_custom_provider_registers_and_detects_models(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme AI",
            "base_url": "https://api.acme.example/v1/",
            "api_key": "sk-acme-aaaa1111bbbb",
            "credential_rotation": "round_robin",
            "proxy": "http://127.0.0.1:7890",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "custom_acme_ai"
    assert body["display_name"] == "Acme AI"
    assert body["base_url"] == "https://api.acme.example/v1"
    assert body["key_count"] == 1
    assert body["masked_keys"] == ["sk-acm…bbbb"]
    assert body["credential_rotation"] == "round_robin"
    assert body["proxy"] == "http://127.0.0.1:7890"
    assert body["status"] == "configured"
    assert body["models"] == ["m1", "m2"]
    assert body["model_count"] == 2
    assert "test_error" not in body
    assert "sk-acme-aaaa1111bbbb" not in response.text

    stored = registry.get("custom_acme_ai")
    assert stored is not None
    assert stored.api_keys == ("sk-acme-aaaa1111bbbb",)
    assert stored.enabled is True
    assert body["discovery"] == {
        "provider_id": "custom_acme_ai",
        "ok": True,
        "model_count": 2,
    }
    reload_providers.assert_awaited_once_with(
        reason="custom_provider_change", refresh_provider_id="custom_acme_ai"
    )


def test_create_custom_provider_test_failure_is_non_fatal(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, reload_providers = _make_app(
        monkeypatch, tmp_path, registry, failure="ConnectError"
    )

    response = _local_client(app).post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme AI",
            "base_url": "https://api.acme.example/v1",
            "api_key": "sk-acme-aaaa1111bbbb",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "custom_acme_ai"
    assert body["test_error"] == "ConnectError"
    assert body["models"] == []
    assert registry.get("custom_acme_ai") is not None
    reload_providers.assert_awaited_once()


def test_create_custom_provider_default_rotation(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers",
        json={
            "display_name": "Acme",
            "base_url": "https://api.acme.example/v1",
            "api_key": "sk-acme-aaaa1111bbbb",
        },
    )

    assert response.status_code == 200
    assert response.json()["credential_rotation"] == "failover"


def test_create_custom_provider_duplicate_slug_is_409(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, _ = _make_app(monkeypatch, tmp_path, registry)
    payload = {
        "display_name": "Acme",
        "base_url": "https://api.acme.example/v1",
        "api_key": "sk-acme-aaaa1111bbbb",
    }
    assert (
        _local_client(app).post("/admin/api/custom-providers", json=payload).is_success
    )

    response = _local_client(app).post("/admin/api/custom-providers", json=payload)

    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "", "base_url": "https://a.example", "api_key": "k"},
        {"display_name": "   ", "base_url": "https://a.example", "api_key": "k"},
        {"display_name": "!!!", "base_url": "https://a.example", "api_key": "k"},
        {"display_name": "Acme", "base_url": "not-a-url", "api_key": "k"},
        {"display_name": "Acme", "base_url": "ftp://a.example", "api_key": "k"},
        {"display_name": "Acme", "base_url": "https://a.example", "api_key": ""},
        {"display_name": "Acme", "base_url": "https://a.example", "api_key": "  "},
        {"display_name": "Acme", "base_url": "https://a.example", "api_key": "k1,k2"},
        {
            "display_name": "Acme",
            "base_url": "https://a.example",
            "api_key": "k",
            "credential_rotation": "random",
        },
        {
            "display_name": "Acme",
            "base_url": "https://a.example",
            "api_key": "k",
            "proxy": "gopher://proxy",
        },
    ],
)
def test_create_custom_provider_validation_errors(monkeypatch, tmp_path, payload):
    app, reload_providers = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).post("/admin/api/custom-providers", json=payload)

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


def test_update_custom_provider_applies_changes_and_reloads(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json={
            "display_name": "Acme Renamed",
            "base_url": "https://v2.acme.example/v1",
            "credential_rotation": "least_used",
            "enabled": False,
            "proxy": "http://127.0.0.1:8080",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Acme Renamed"
    assert body["base_url"] == "https://v2.acme.example/v1"
    assert body["credential_rotation"] == "least_used"
    assert body["enabled"] is False
    assert body["status"] == "disabled"
    assert body["proxy"] == "http://127.0.0.1:8080"
    assert "discovery" not in body
    reload_providers.assert_awaited_once_with(
        reason="custom_provider_change", refresh_provider_id=None
    )


def test_update_custom_provider_clears_proxy(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path, proxy="http://127.0.0.1:8080")
    app, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json={"proxy": ""},
    )

    assert response.status_code == 200
    assert response.json()["proxy"] is None


def test_update_custom_provider_unknown_is_404(monkeypatch, tmp_path):
    app, reload_providers = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_nope",
        json={"enabled": False},
    )

    assert response.status_code == 404
    reload_providers.assert_not_awaited()


def test_update_custom_provider_empty_body_is_422(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json={},
    )

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"display_name": "  "},
        {"base_url": "not-a-url"},
        {"credential_rotation": "random"},
    ],
)
def test_update_custom_provider_validation_errors(monkeypatch, tmp_path, payload):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).patch(
        "/admin/api/custom-providers/custom_acme",
        json=payload,
    )

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


def test_add_custom_provider_key_appends_and_reloads(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_acme/keys",
        json={"api_key": "sk-acme-cccc2222dddd"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 2
    assert body["masked_keys"] == ["sk-acm…bbbb", "sk-acm…dddd"]
    assert body["added"] == "sk-acm…dddd"
    stored_keys = registry.get("custom_acme")
    assert stored_keys is not None
    assert stored_keys.api_keys == (
        "sk-acme-aaaa1111bbbb",
        "sk-acme-cccc2222dddd",
    )
    reload_providers.assert_awaited_once_with(
        reason="custom_provider_change", refresh_provider_id="custom_acme"
    )
    assert "sk-acme-cccc2222dddd" not in response.text


def test_add_custom_provider_key_duplicate_is_409(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_acme/keys",
        json={"api_key": "sk-acme-aaaa1111bbbb"},
    )

    assert response.status_code == 409


def test_add_custom_provider_key_unknown_provider_is_404(monkeypatch, tmp_path):
    app, _ = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_nope/keys",
        json={"api_key": "sk-acme-aaaa1111bbbb"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("bad_key", ["", "   ", "k1,k2"])
def test_add_custom_provider_key_rejects_empty_or_comma_keys(
    monkeypatch, tmp_path, bad_key
):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).post(
        "/admin/api/custom-providers/custom_acme/keys",
        json={"api_key": bad_key},
    )

    assert response.status_code == 422
    reload_providers.assert_not_awaited()


def test_delete_custom_provider_key_removes_index(monkeypatch, tmp_path):
    registry = _seeded_registry(
        tmp_path, api_keys=("sk-acme-aaaa1111bbbb", "sk-acme-cccc2222dddd")
    )
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete(
        "/admin/api/custom-providers/custom_acme/keys/0"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 1
    assert body["masked_keys"] == ["sk-acm…dddd"]
    assert body["removed"] == "sk-acm…bbbb"
    reload_providers.assert_awaited_once_with(
        reason="custom_provider_change", refresh_provider_id="custom_acme"
    )


def test_delete_custom_provider_last_key_keeps_provider(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, _ = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete(
        "/admin/api/custom-providers/custom_acme/keys/0"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key_count"] == 0
    assert body["status"] == "missing_key"
    assert registry.get("custom_acme") is not None


def test_delete_custom_provider_key_out_of_range_is_404(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete(
        "/admin/api/custom-providers/custom_acme/keys/3"
    )

    assert response.status_code == 404
    reload_providers.assert_not_awaited()


def test_delete_custom_provider_removes_and_reloads(monkeypatch, tmp_path):
    registry = _seeded_registry(tmp_path)
    app, reload_providers = _make_app(monkeypatch, tmp_path, registry)

    response = _local_client(app).delete("/admin/api/custom-providers/custom_acme")

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["provider_id"] == "custom_acme"
    assert body["removed"] is True
    # Nothing routed to this provider, so nothing was rewritten -- but the key
    # is always present, so a caller never has to guess whether an empty list
    # means "no refs" or "an older server". The route write still runs: the
    # read of the current routes and the write of the new ones have to be one
    # critical section, and a dry run outside it is a second source of truth.
    assert body["removed_route_refs"] == []
    assert body["routes"]["applied"] is True
    assert registry.get("custom_acme") is None
    reload_providers.assert_awaited_once_with(
        reason="custom_provider_change", refresh_provider_id=None
    )


def test_delete_custom_provider_unknown_is_404(monkeypatch, tmp_path):
    app, reload_providers = _make_app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).delete("/admin/api/custom-providers/custom_nope")

    assert response.status_code == 404
    reload_providers.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/admin/api/custom-providers", None),
        (
            "post",
            "/admin/api/custom-providers",
            {
                "display_name": "Acme",
                "base_url": "https://a.example",
                "api_key": "k",
            },
        ),
        ("patch", "/admin/api/custom-providers/custom_acme", {"enabled": False}),
        (
            "post",
            "/admin/api/custom-providers/custom_acme/keys",
            {"api_key": "k2"},
        ),
        ("delete", "/admin/api/custom-providers/custom_acme/keys/0", None),
        ("delete", "/admin/api/custom-providers/custom_acme", None),
    ],
)
def test_custom_provider_endpoints_are_loopback_only(
    monkeypatch, tmp_path, method, path, payload
):
    registry = _seeded_registry(tmp_path)
    app, _ = _make_app(monkeypatch, tmp_path, registry)
    remote = TestClient(app, client=("203.0.113.10", 50000))

    response = remote.request(method, path, json=payload)

    assert response.status_code == 403


# --------------------------------------------------------------------------
# End-to-end: the registry, the hot reload, discovery, the model cache and the
# route, with only the upstream HTTP client doubled. Everything above this line
# stubs the runtime, and the defect these cover lives inside that stub.
# --------------------------------------------------------------------------

_CREATE_ACME = {
    "display_name": "Acme AI",
    "base_url": "https://api.acme.example/v1",
    "api_key": "sk-acme-aaaa1111bbbb",
}


def _create(client, payload=None):
    return client.post("/admin/api/custom-providers", json=payload or _CREATE_ACME)


def test_create_publishes_models_to_the_catalogue_not_only_the_card(
    monkeypatch, tmp_path
):
    upstream = ModelListingProviderDouble(("m1", "m2", "m3"))
    app, registry = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )
    client = _local_client(app)

    body = _create(client).json()

    assert body["model_count"] == 3
    assert body["models"] == ["m1", "m2", "m3"]
    assert body["discovery"]["ok"] is True
    assert registry.get("custom_acme_ai") is not None
    # The catalogue, not the response: the card used to be able to report
    # models that nothing else in the process had.
    listed = client.get("/admin/api/custom-providers").json()["providers"]
    assert listed[0]["model_count"] == 3


def test_create_fetches_the_new_provider_models_exactly_once(monkeypatch, tmp_path):
    upstream = ModelListingProviderDouble(("m1",))
    app, _ = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )

    assert _create(_local_client(app)).status_code == 200

    assert upstream.calls == 1


def test_create_reports_discovery_failure_in_the_response(monkeypatch, tmp_path):
    upstream = ModelListingProviderDouble(
        ("m1",), error=PermissionError("upstream refused the key"), failures=99
    )
    app, registry = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )

    body = _create(_local_client(app)).json()

    assert body["discovery"]["ok"] is False
    assert body["discovery"]["error_type"] == "PermissionError"
    assert body["discovery"]["message"]
    assert body["test_error"] == "PermissionError"
    assert body["model_count"] == 0
    # The provider is still registered, and the retry was bounded.
    assert registry.get("custom_acme_ai") is not None
    assert upstream.calls == 2


def test_create_retries_the_mutated_provider_once(monkeypatch, tmp_path):
    upstream = ModelListingProviderDouble(
        ("m1", "m2"), error=PermissionError("not propagated yet"), failures=1
    )
    app, _ = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )

    body = _create(_local_client(app)).json()

    assert upstream.calls == 2
    assert body["discovery"]["ok"] is True
    assert body["model_count"] == 2


def test_enable_toggle_publishes_models_without_a_restart(monkeypatch, tmp_path):
    upstream = ModelListingProviderDouble(("m1", "m2"))
    app, _ = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )
    client = _local_client(app)
    assert _create(client).status_code == 200

    disabled = client.patch(
        "/admin/api/custom-providers/custom_acme_ai", json={"enabled": False}
    ).json()
    assert disabled["model_count"] == 0

    enabled = client.patch(
        "/admin/api/custom-providers/custom_acme_ai", json={"enabled": True}
    ).json()

    assert enabled["model_count"] == 2
    assert enabled["discovery"]["ok"] is True
    assert (
        client.get("/admin/api/custom-providers").json()["providers"][0]["model_count"]
        == 2
    )


def test_add_key_republishes_the_catalogue(monkeypatch, tmp_path):
    upstream = ModelListingProviderDouble(("m1", "m2"))
    app, _ = create_custom_provider_app(
        monkeypatch, tmp_path, {"custom_acme_ai": upstream}
    )
    client = _local_client(app)
    assert _create(client).status_code == 200
    calls_after_create = upstream.calls

    body = client.post(
        "/admin/api/custom-providers/custom_acme_ai/keys",
        json={"api_key": "sk-acme-cccc2222dddd"},
    ).json()

    assert body["key_count"] == 2
    assert body["model_count"] == 2
    assert body["discovery"]["ok"] is True
    assert upstream.calls == calls_after_create + 1
