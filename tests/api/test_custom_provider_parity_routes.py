"""The custom card's new readouts and gestures, end to end through the routes.

Three gaps, one file: the dialect a custom host was measured speaking, the
per-key health a custom pool has always had but never showed, and what happens
to a ``MODEL*`` route when its custom provider is disabled or deleted.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_custom_routes
from my_claude_code.config.provider_registry import ProviderRegistry
from tests.api.support import create_test_app, runtime_for_app


def _registry(tmp_path: Path) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        display_name="Acme",
        base_url="https://api.acme.example/v1",
        api_keys=("sk-acme-aaaa1111bbbb", "sk-acme-cccc3333dddd"),
    )
    return registry


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _app(monkeypatch, tmp_path: Path, registry: ProviderRegistry):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FCC_ENV_FILE", raising=False)
    app = create_test_app()
    runtime = runtime_for_app(app)
    monkeypatch.setattr(runtime, "reload_providers", AsyncMock(return_value={}))
    monkeypatch.setattr(
        runtime, "cached_model_ids", lambda: {"custom_acme": frozenset({"m1"})}
    )
    app.dependency_overrides[admin_custom_routes.get_custom_provider_registry] = (
        lambda: registry
    )
    return app, runtime


# --------------------------------------------------------------- dialect


def test_a_learned_vocabulary_is_stored_and_labelled(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, runtime = _app(monkeypatch, tmp_path, registry)
    monkeypatch.setattr(
        runtime,
        "probe_custom_provider_dialect",
        AsyncMock(
            side_effect=lambda provider_id: (
                registry.update(
                    provider_id,
                    reasoning_effort_enum=["low", "high", "max"],
                    reasoning_probe_status="learned",
                    reasoning_probed_at="2026-09-01T10:00:00+00:00",
                ),
                {"status": "learned", "effort_enum": ["low", "high", "max"]},
            )[1]
        ),
    )

    body = (
        _local_client(app)
        .post("/admin/api/custom-providers/custom_acme/reasoning-probe", json={})
        .json()
    )

    assert body["reasoning_effort_enum"] == ["low", "high", "max"]
    assert body["reasoning_dialect_label"] == "learned {low, high, max} on 2026-09-01"
    assert body["probe"]["status"] == "learned"


@pytest.mark.parametrize(
    ("status", "ignored", "expected"),
    [
        ("ignored", True, "ignored on 2026-09-01"),
        ("401", False, "unknown (401) on 2026-09-01"),
    ],
)
def test_the_card_says_what_the_probe_actually_established(
    monkeypatch, tmp_path, status: str, ignored: bool, expected: str
):
    registry = _registry(tmp_path)
    registry.update(
        "custom_acme",
        reasoning_field_ignored=ignored,
        reasoning_probe_status=status,
        reasoning_probed_at="2026-09-01T10:00:00+00:00",
    )
    app, _ = _app(monkeypatch, tmp_path, registry)

    (provider,) = (
        _local_client(app).get("/admin/api/custom-providers").json()["providers"]
    )

    assert provider["reasoning_dialect_label"] == expected
    assert provider["reasoning_effort_enum"] is None


def test_an_unprobed_provider_claims_nothing(monkeypatch, tmp_path):
    app, _ = _app(monkeypatch, tmp_path, _registry(tmp_path))

    (provider,) = (
        _local_client(app).get("/admin/api/custom-providers").json()["providers"]
    )

    assert provider["reasoning_dialect_label"] == "unknown (not probed)"
    assert provider["reasoning_field_ignored"] is False


def test_the_vocabulary_can_be_edited_by_hand_as_a_comma_list(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, _ = _app(monkeypatch, tmp_path, registry)

    body = (
        _local_client(app)
        .patch(
            "/admin/api/custom-providers/custom_acme",
            json={"reasoning_effort_enum": " Low , HIGH , max "},
        )
        .json()
    )

    assert body["reasoning_effort_enum"] == ["low", "high", "max"]
    stored = registry.get("custom_acme")
    assert stored is not None
    assert stored.reasoning_effort_enum == ("low", "high", "max")


def test_an_empty_edit_forgets_the_learned_vocabulary(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    registry.update("custom_acme", reasoning_effort_enum=["low", "high", "max"])
    app, _ = _app(monkeypatch, tmp_path, registry)

    body = (
        _local_client(app)
        .patch(
            "/admin/api/custom-providers/custom_acme",
            json={"reasoning_effort_enum": ""},
        )
        .json()
    )

    assert body["reasoning_effort_enum"] is None
    assert body["reasoning_dialect_label"].startswith("unknown (cleared)")


# --------------------------------------------------------------- key health


def test_a_custom_pool_reports_its_keys_masked(monkeypatch, tmp_path):
    app, _ = _app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).get("/admin/api/custom-providers/custom_acme/keys")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["keys"] == ["sk-acm…bbbb", "sk-acm…dddd"]
    assert body["credential_rotation"] == "failover"
    assert body["locked"] is False
    # Index-aligned with the key list, ``None`` where the runtime has nothing.
    assert body["health"] == [None, None]
    assert "sk-acme-aaaa1111bbbb" not in response.text


def test_an_unknown_pool_is_a_404(monkeypatch, tmp_path):
    app, _ = _app(monkeypatch, tmp_path, _registry(tmp_path))

    response = _local_client(app).get("/admin/api/custom-providers/custom_nope/keys")

    assert response.status_code == 404


# --------------------------------------------------------------- disable/delete


def _routed_app(monkeypatch, tmp_path, registry):
    """An app whose config writes are captured rather than persisted."""
    # The route reads the registry through the FastAPI dependency; ``Settings``
    # reads it through the process singleton. Both have to be this one, or the
    # provider is unknown to validation and every write fails.
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)
    app, runtime = _app(monkeypatch, tmp_path, registry)
    written: list[dict[str, Any]] = []

    async def _apply(build):
        from my_claude_code.config.settings import Settings

        monkeypatch.setenv("MODEL", "custom_acme/m1")
        monkeypatch.setenv("MODEL_OPUS_FALLBACKS", "custom_acme/m1")
        updates = dict(build(Settings()))
        written.append(updates)
        return {"applied": True, "values": updates}

    monkeypatch.setenv("MODEL", "custom_acme/m1")
    monkeypatch.setenv("MODEL_OPUS_FALLBACKS", "custom_acme/m1")
    monkeypatch.setattr(runtime, "apply_admin_config_with", _apply)
    return app, written


def test_disabling_a_routed_provider_pauses_its_chain_entries(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    app, written = _routed_app(monkeypatch, tmp_path, registry)

    body = (
        _local_client(app)
        .patch("/admin/api/custom-providers/custom_acme", json={"enabled": False})
        .json()
    )

    assert written == [
        {
            "MODEL_PAUSED": "custom_acme/m1",
            "MODEL_OPUS_PAUSED": "custom_acme/m1",
        }
    ]
    assert body["routes"]["action"] == "paused"
    assert {entry["paused_key"] for entry in body["routes"]["paused"]} == {
        "MODEL_PAUSED",
        "MODEL_OPUS_PAUSED",
    }
    # Recorded, so re-enabling lifts its own pauses and nobody else's.
    stored = registry.get("custom_acme")
    assert stored is not None
    assert stored.auto_paused_refs == (
        ("MODEL_PAUSED", "custom_acme/m1"),
        ("MODEL_OPUS_PAUSED", "custom_acme/m1"),
    )


def test_re_enabling_lifts_only_the_pauses_the_disable_added(monkeypatch, tmp_path):
    registry = _registry(tmp_path)
    registry.update(
        "custom_acme",
        enabled=False,
        auto_paused_refs=(("MODEL_PAUSED", "custom_acme/m1"),),
    )
    app, written = _routed_app(monkeypatch, tmp_path, registry)
    monkeypatch.setenv("MODEL_PAUSED", "custom_acme/m1")

    body = (
        _local_client(app)
        .patch("/admin/api/custom-providers/custom_acme", json={"enabled": True})
        .json()
    )

    assert written == [{"MODEL_PAUSED": ""}]
    assert body["routes"]["action"] == "unpaused"
    stored = registry.get("custom_acme")
    assert stored is not None
    assert stored.auto_paused_refs == ()


def test_deleting_a_routed_provider_removes_its_refs_and_lists_them(
    monkeypatch, tmp_path
):
    registry = _registry(tmp_path)
    app, written = _routed_app(monkeypatch, tmp_path, registry)

    body = _local_client(app).delete("/admin/api/custom-providers/custom_acme").json()

    assert written == [{"MODEL": "", "MODEL_OPUS_FALLBACKS": ""}]
    assert sorted(body["removed_route_refs"]) == [
        "MODEL=custom_acme/m1",
        "MODEL_OPUS_FALLBACKS=custom_acme/m1",
    ]
    assert registry.get("custom_acme") is None
