"""Tests for PR 11 (EM-PR3): Provider Card, modal, and Models endpoint readouts."""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_custom_routes
from my_claude_code.api.admin_routes import _config_response
from my_claude_code.api.model_admin import (
    model_endpoint_readout,
    provider_endpoint_summary,
    with_surface_preferences,
)
from my_claude_code.application.model_metadata import (
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    reset_model_overrides_cache,
    save_model_overrides,
)
from my_claude_code.config.provider_registry import ProviderRegistry
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import (
    RequestLogStore,
    reset_request_log_stores,
    store_from_settings,
)
from my_claude_code.providers.model_listing import (
    reset_declared_endpoints_snapshot,
    set_declared_endpoints_snapshot,
)
from tests.api.support import create_test_app, provider_manager_for_app


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("MODEL", "FCC_ENV_FILE", "MCC_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()
    reset_declared_endpoints_snapshot()
    reset_request_log_stores()
    yield
    reset_model_overrides_cache()
    reset_declared_endpoints_snapshot()
    reset_request_log_stores()


def _seed_attempts(
    target: Path | RequestLogStore, rows: list[tuple[Any, ...]]
) -> RequestLogStore:
    store = target if isinstance(target, RequestLogStore) else RequestLogStore(target)
    with store._connection() as conn:
        for (
            req_id,
            attempt,
            provider,
            model_ref,
            outcome,
            params,
            ts_epoch,
            wire_body,
        ) in rows:
            conn.execute(
                "INSERT INTO request_attempts"
                " (request_id, attempt, provider, model_ref, outcome, params, ts_epoch, wire_body)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req_id,
                    attempt,
                    provider,
                    model_ref,
                    outcome,
                    params,
                    ts_epoch,
                    wire_body,
                ),
            )
    return store


def test_recent_wire_surfaces_bounded_and_handles_bad_rows(tmp_path: Path) -> None:
    now = time.time()
    db_path = tmp_path / "logs" / "requests.db"
    store = _seed_attempts(
        db_path,
        [
            (
                "req-1",
                0,
                "commandcode",
                "commandcode/deepseek/deepseek-v4.1-flash",
                "succeeded",
                json.dumps({"wire": {"surface": "chat_completions"}}),
                now - 120.0,
                "SHOULD_NOT_BE_READ",
            ),
            (
                "req-2",
                0,
                "commandcode",
                "commandcode/deepseek/deepseek-v4.1-flash",
                "succeeded",
                json.dumps({"wire": {"surface": "responses"}}),
                now - 10.0,
                "SHOULD_NOT_BE_READ",
            ),
            (
                "req-3",
                0,
                "groq",
                "groq/llama-3.3-70b",
                "error",
                json.dumps({"wire": {"surface": "chat_completions"}}),
                now - 5.0,
                None,
            ),
            (
                "req-4",
                0,
                "groq",
                "groq/bad-json",
                "succeeded",
                '{"wire": {"surface": "broken',
                now - 2.0,
                None,
            ),
        ],
    )

    result = store.recent_wire_surfaces(max_rows=50)
    by_model = result["by_model"]
    assert (
        by_model["commandcode/deepseek/deepseek-v4.1-flash"]["surface"] == "responses"
    )
    assert (
        by_model["commandcode/deepseek/deepseek-v4.1-flash"]["outcome"] == "succeeded"
    )
    assert by_model["groq/llama-3.3-70b"]["surface"] == "chat_completions"
    assert by_model["groq/llama-3.3-70b"]["outcome"] == "error"
    assert "groq/bad-json" not in by_model

    cc_obs = result["by_provider"]["commandcode"]
    assert [entry["surface"] for entry in cc_obs] == ["responses", "chat_completions"]
    assert cc_obs[0]["count"] == 1
    assert cc_obs[1]["count"] == 1

    # Tight max_rows limit bounds rows read newest-first (req-4 + req-3 only).
    limited_two = store.recent_wire_surfaces(max_rows=2)
    assert "groq/llama-3.3-70b" in limited_two["by_model"]
    assert "commandcode/deepseek/deepseek-v4.1-flash" not in limited_two["by_model"]

    # max_rows=3 reaches req-2 (responses) while still excluding req-1 (chat_completions).
    limited_three = store.recent_wire_surfaces(max_rows=3)
    assert (
        limited_three["by_model"]["commandcode/deepseek/deepseek-v4.1-flash"]["surface"]
        == "responses"
    )
    assert limited_three["by_provider"]["commandcode"][0]["surface"] == "responses"
    assert len(limited_three["by_provider"]["commandcode"]) == 1

    store.close()


def test_recent_wire_surfaces_normalizes_provenance_and_tracks_single_door_unlabeled(
    tmp_path: Path,
) -> None:
    now = time.time()
    store = _seed_attempts(
        tmp_path / "logs" / "requests.db",
        [
            (
                "req-reg",
                0,
                "mistral",
                "mistral/deepseek-v4-1-flash-1017",
                "succeeded",
                json.dumps({"wire": {"surface": "responses (registry)"}}),
                now - 5.0,
                None,
            ),
            (
                "req-ant",
                0,
                "anthropic",
                "anthropic/claude-sonnet-5",
                "succeeded",
                json.dumps({"wire": {"model": "claude-sonnet-5"}}),
                now - 3.0,
                None,
            ),
        ],
    )
    result = store.recent_wire_surfaces(max_rows=50)
    assert (
        result["by_model"]["mistral/deepseek-v4-1-flash-1017"]["surface"] == "responses"
    )
    assert "anthropic/claude-sonnet-5" not in result["by_model"]
    assert result["unlabeled_by_model"]["anthropic/claude-sonnet-5"]["outcome"] == (
        "succeeded"
    )
    ant_summary = provider_endpoint_summary(
        "anthropic",
        unlabeled_attempt=result["unlabeled_by_provider"].get("anthropic"),
    )
    assert ant_summary["observed"][0]["surface"] == "messages"
    ant_readout = model_endpoint_readout(
        "anthropic",
        "claude-sonnet-5",
        unlabeled_attempt=result["unlabeled_by_model"].get("anthropic/claude-sonnet-5"),
    )
    assert ant_readout["last_used"]["surface"] == "messages"
    store.close()


def test_provider_endpoint_summary_filters_capability_words_and_maps_single_doors() -> (
    None
):
    set_declared_endpoints_snapshot(
        {
            "mistral": (
                ProviderModelInfo(
                    "mistral-large",
                    declared=ProviderModelDeclaration(endpoints=("chat", "non_chat")),
                ),
            ),
            "commandcode": (
                ProviderModelInfo(
                    "deepseek/deepseek-v4.1-flash",
                    declared=ProviderModelDeclaration(
                        endpoints=("/chat/completions", "/responses")
                    ),
                ),
                ProviderModelInfo(
                    "claude-sonnet-5",
                    declared=ProviderModelDeclaration(endpoints=("/messages",)),
                ),
            ),
        }
    )

    mistral = provider_endpoint_summary("mistral")
    assert mistral["publishes"] == []
    assert [item["value"] for item in mistral["can_speak"]] == [
        "chat_completions",
        "responses",
    ]
    assert mistral["surface_preference"] is not None

    cc = provider_endpoint_summary(
        "commandcode",
        observed_rows=[
            {
                "surface": "responses",
                "count": 3,
                "last_used_at": "2026-11-25T10:00:00+00:00",
                "last_outcome": "succeeded",
            }
        ],
    )
    assert [item["value"] for item in cc["publishes"]] == [
        "/chat/completions",
        "/messages",
        "/responses",
    ]
    assert [item["value"] for item in cc["can_speak"]] == [
        "chat_completions",
        "responses",
        "messages",
    ]
    assert [item["value"] for item in cc["surface_preference"]["offered"]] == [
        "chat_completions",
        "responses",
        "messages",
    ]
    assert cc["observed"][0]["surface"] == "responses"
    assert cc["observed"][0]["count"] == 3
    assert "Speaks:" in cc["face_text"]

    # Single-door providers have non-empty can_speak and NO surface_preference.
    assert [
        item["value"] for item in provider_endpoint_summary("anthropic")["can_speak"]
    ] == ["messages"]
    assert provider_endpoint_summary("anthropic")["surface_preference"] is None
    assert [
        item["value"]
        for item in provider_endpoint_summary("chatgpt_oauth")["can_speak"]
    ] == ["responses"]
    assert [
        item["value"]
        for item in provider_endpoint_summary("antigravity_oauth")["can_speak"]
    ] == ["v1internal"]
    assert [
        item["value"] for item in provider_endpoint_summary("groq")["can_speak"]
    ] == ["chat_completions"]
    assert provider_endpoint_summary("groq")["surface_preference"] is None


def test_model_endpoint_readout_and_with_surface_preferences_preserve_contracts() -> (
    None
):
    set_declared_endpoints_snapshot(
        {
            "commandcode": (
                ProviderModelInfo(
                    "claude-sonnet-5",
                    declared=ProviderModelDeclaration(endpoints=("/messages",)),
                ),
                ProviderModelInfo(
                    "deepseek/deepseek-v4.1-flash",
                    declared=ProviderModelDeclaration(
                        endpoints=("/chat/completions", "/responses")
                    ),
                ),
            )
        }
    )

    page = with_surface_preferences(
        {
            "providers": [
                {
                    "provider_id": "groq",
                    "models": [{"model_id": "llama-3.3-70b"}],
                },
                {
                    "provider_id": "commandcode",
                    "models": [
                        {"model_id": "claude-sonnet-5"},
                        {
                            "model_id": "deepseek/deepseek-v4.1-flash",
                            "last_used_endpoint": {
                                "surface": "responses",
                                "outcome": "succeeded",
                                "ts_epoch": 1700000000.0,
                                "ts_iso": "2023-11-14T22:13:20+00:00",
                            },
                        },
                    ],
                },
            ]
        },
        ModelParameterOverrides(),
    )

    groq_row, cc_row = page["providers"]
    assert "surface_preference" not in groq_row
    assert "surface_preference" not in groq_row["models"][0]
    assert groq_row["endpoint_summary"]["surface_preference"] is None
    assert [item["value"] for item in groq_row["endpoint_summary"]["can_speak"]] == [
        "chat_completions"
    ]
    assert groq_row["models"][0]["endpoint_readout"]["effective"] == "chat_completions"

    cc_models = {m["model_id"]: m for m in cc_row["models"]}
    assert "surface_preference" not in cc_models["claude-sonnet-5"]
    assert cc_models["claude-sonnet-5"]["endpoint_readout"]["effective"] == "messages"
    assert cc_models["claude-sonnet-5"]["endpoint_readout"]["publishes"] == [
        "/messages"
    ]
    assert (
        cc_models["deepseek/deepseek-v4.1-flash"]["endpoint_readout"]["last_used"][
            "surface"
        ]
        == "responses"
    )

    single = model_endpoint_readout("groq", "llama-3.3-70b")
    assert single["effective"] == "chat_completions"
    assert single["last_used"] is None


def test_admin_routes_attach_endpoint_readouts_and_sync_preferred_surface(
    monkeypatch, tmp_path: Path
) -> None:
    # Cold _config_response() must work safely without a server or DB.
    cold_config = _config_response()
    cold_by_id = {p["provider_id"]: p for p in cold_config["provider_status"]}
    assert (
        cold_by_id["commandcode"]["endpoint_summary"]["surface_preference"] is not None
    )
    assert cold_by_id["groq"]["endpoint_summary"]["surface_preference"] is None

    settings = Settings.model_validate(
        {
            "COMMANDCODE_API_KEY": "cc-" + "1" * 24,
            "GROQ_API_KEY": "gsk_" + "2" * 24,
            "MODEL": "commandcode/deepseek/deepseek-v4.1-flash",
        }
    )
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        display_name="Single House",
        base_url="https://single.example/v1",
        api_keys=("sk-single-1111",),
        surfaces=("chat_completions",),
    )
    registry.add(
        display_name="Dual House",
        base_url="https://dual.example/v1",
        api_keys=("sk-dual-2222",),
        surfaces=("chat_completions", "responses"),
    )
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)

    app = create_test_app(settings)
    app.dependency_overrides[admin_custom_routes.get_custom_provider_registry] = (
        lambda: registry
    )
    manager = provider_manager_for_app(app)
    manager.cache_model_infos(
        "commandcode",
        (
            ProviderModelInfo(
                "deepseek/deepseek-v4.1-flash",
                declared=ProviderModelDeclaration(
                    endpoints=("/chat/completions", "/responses")
                ),
            ),
            ProviderModelInfo(
                "claude-sonnet-5",
                declared=ProviderModelDeclaration(endpoints=("/messages",)),
            ),
        ),
    )
    manager.cache_model_infos("groq", (ProviderModelInfo("llama-3.3-70b"),))
    manager.cache_model_infos("custom_single_house", (ProviderModelInfo("m-1"),))

    store = store_from_settings(settings)
    assert store is not None
    _seed_attempts(
        store,
        [
            (
                "req-cc-1",
                0,
                "commandcode",
                "commandcode/deepseek/deepseek-v4.1-flash",
                "succeeded",
                json.dumps({"wire": {"surface": "responses"}}),
                time.time() - 5.0,
                None,
            ),
            (
                "req-groq-1",
                0,
                "groq",
                "groq/llama-3.3-70b",
                "succeeded",
                json.dumps({"wire": {"surface": "chat_completions"}}),
                time.time() - 3.0,
                None,
            ),
        ],
    )
    try:
        client = TestClient(app, client=("127.0.0.1", 50000))

        # 1. POST preferred-surface for commandcode -> updates /admin/api/config & /admin/api/model-admin
        post_resp = client.post(
            "/admin/api/model-admin/preferred-surface",
            json={
                "scope": "provider",
                "key": "commandcode",
                "surface": "chat_completions",
            },
        )
        assert post_resp.status_code == 200, post_resp.text
        posted_providers = {p["provider_id"]: p for p in post_resp.json()["providers"]}
        assert (
            posted_providers["commandcode"]["surface_preference"]["preferred"]
            == "chat_completions"
        )
        assert (
            posted_providers["commandcode"]["endpoint_summary"]["surface_preference"][
                "preferred"
            ]
            == "chat_completions"
        )

        cfg_resp = client.get("/admin/api/config")
        assert cfg_resp.status_code == 200
        cfg_by_id = {p["provider_id"]: p for p in cfg_resp.json()["provider_status"]}
        assert (
            cfg_by_id["commandcode"]["endpoint_summary"]["surface_preference"][
                "preferred"
            ]
            == "chat_completions"
        )
        assert cfg_by_id["commandcode"]["endpoint_summary"]["observed"][0][
            "surface"
        ] == ("responses")
        assert cfg_by_id["groq"]["endpoint_summary"]["surface_preference"] is None
        assert cfg_by_id["groq"]["endpoint_summary"]["observed"][0]["surface"] == (
            "chat_completions"
        )

        # 2. Custom providers endpoint_summary (1-door vs multi-door)
        cp_resp = client.get("/admin/api/custom-providers")
        assert cp_resp.status_code == 200
        cp_by_id = {p["provider_id"]: p for p in cp_resp.json()["providers"]}
        assert cp_by_id["custom_single_house"]["surfaces"] == ["chat_completions"]
        assert (
            cp_by_id["custom_single_house"]["endpoint_summary"]["surface_preference"]
            is None
        )
        assert [
            item["value"]
            for item in cp_by_id["custom_single_house"]["endpoint_summary"]["can_speak"]
        ] == ["chat_completions"]
        assert (
            cp_by_id["custom_dual_house"]["endpoint_summary"]["surface_preference"]
            is not None
        )

        # 3. Models page un-gated endpoint_readout + last_used_endpoint
        models_resp = client.get("/admin/api/model-admin")
        assert models_resp.status_code == 200
        m_providers = {p["provider_id"]: p for p in models_resp.json()["providers"]}
        assert "surface_preference" not in m_providers["groq"]
        assert "surface_preference" not in m_providers["custom_single_house"]
        assert m_providers["groq"]["endpoint_summary"]["observed"][0]["surface"] == (
            "chat_completions"
        )
        groq_model = m_providers["groq"]["models"][0]
        assert groq_model["endpoint_readout"]["effective"] == "chat_completions"
        assert groq_model["last_used_endpoint"]["surface"] == "chat_completions"

        cc_models = {m["model_ref"]: m for m in m_providers["commandcode"]["models"]}
        assert "surface_preference" not in cc_models["commandcode/claude-sonnet-5"]
        assert (
            cc_models["commandcode/claude-sonnet-5"]["endpoint_readout"]["effective"]
            == "messages"
        )
        assert (
            cc_models["commandcode/deepseek/deepseek-v4.1-flash"]["last_used_endpoint"][
                "surface"
            ]
            == "responses"
        )
    finally:
        save_model_overrides(ModelParameterOverrides())
        store.close()


def test_default_ladder_preview_reflects_speakable_doors_and_client_family_order(
    monkeypatch, tmp_path: Path
) -> None:
    cc = provider_endpoint_summary("commandcode")
    cc_ladder = {entry["family"]: entry for entry in cc["default_ladder"]}
    assert list(cc_ladder) == ["anthropic", "openai"]
    assert cc_ladder["anthropic"]["harness_label"] == (
        "Harness requests Messages (/v1/messages)"
    )
    assert cc_ladder["anthropic"]["text"] == (
        "Messages (direct) \u2192 Responses (with translation) \u2192 Chat Completions (with translation)"
    )
    assert [step["value"] for step in cc_ladder["anthropic"]["steps"]] == [
        "messages",
        "responses",
        "chat_completions",
    ]
    assert [step["translated"] for step in cc_ladder["anthropic"]["steps"]] == [
        False,
        True,
        True,
    ]
    assert cc_ladder["openai"]["harness_label"] == (
        "Harness requests Responses (/v1/responses)"
    )
    assert cc_ladder["openai"]["text"] == (
        "Responses (direct) \u2192 Chat Completions (with translation) \u2192 Messages (with translation)"
    )

    mistral = provider_endpoint_summary("mistral")
    m_ladder = {entry["family"]: entry for entry in mistral["default_ladder"]}
    assert m_ladder["anthropic"]["text"] == (
        "Responses (with translation) \u2192 Chat Completions (with translation)"
    )
    assert m_ladder["openai"]["text"] == (
        "Responses (direct) \u2192 Chat Completions (with translation)"
    )

    custom_msg = provider_endpoint_summary(
        "custom_msg_house",
        custom_surfaces=("chat_completions", "messages"),
    )
    cm_ladder = {entry["family"]: entry for entry in custom_msg["default_ladder"]}
    assert cm_ladder["anthropic"]["text"] == (
        "Messages (direct) \u2192 Chat Completions (with translation)"
    )
    assert cm_ladder["openai"]["text"] == (
        "Chat Completions (with translation) \u2192 Messages (with translation)"
    )

    # Single-door providers never fabricate extra doors in default_ladder.
    for single_id in ("anthropic", "groq", "chatgpt_oauth"):
        assert provider_endpoint_summary(single_id)["default_ladder"] == []

    # Verify HTTP endpoints expose default_ladder identically.
    settings = Settings.model_validate(
        {
            "COMMANDCODE_API_KEY": "cc-" + "9" * 24,
            "MODEL": "commandcode/deepseek/deepseek-v4.1-flash",
        }
    )
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        display_name="Dual House",
        base_url="https://dual.example/v1",
        api_keys=("sk-dual-9999",),
        surfaces=("chat_completions", "responses"),
    )
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)
    app = create_test_app(settings)
    app.dependency_overrides[admin_custom_routes.get_custom_provider_registry] = (
        lambda: registry
    )
    provider_manager_for_app(app).cache_model_infos(
        "commandcode",
        (ProviderModelInfo("deepseek/deepseek-v4.1-flash"),),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    cfg_cc = next(
        p
        for p in client.get("/admin/api/config").json()["provider_status"]
        if p["provider_id"] == "commandcode"
    )
    assert cfg_cc["endpoint_summary"]["default_ladder"] == cc["default_ladder"]

    cp_dual = next(
        p
        for p in client.get("/admin/api/custom-providers").json()["providers"]
        if p["provider_id"] == "custom_dual_house"
    )
    assert [e["family"] for e in cp_dual["endpoint_summary"]["default_ladder"]] == [
        "anthropic",
        "openai",
    ]
    assert cp_dual["endpoint_summary"]["default_ladder"][1]["text"] == (
        "Responses (direct) \u2192 Chat Completions (with translation)"
    )

    models_cc = next(
        p
        for p in client.get("/admin/api/model-admin").json()["providers"]
        if p["provider_id"] == "commandcode"
    )
    assert models_cc["endpoint_summary"]["default_ladder"] == cc["default_ladder"]
