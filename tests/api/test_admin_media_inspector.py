"""Tests for PR-M7 Media Inspector API, overrides editor, learned facts forget, and request provenance (8.66.0)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import request_endpoint_routing
from my_claude_code.config.media_overrides import (
    EMPTY_MEDIA_OVERRIDES,
    MediaOperationOverride,
    load_media_overrides,
    reset_media_overrides_cache,
    save_media_overrides,
    with_media_override,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import (
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    get_request_log_store,
)
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.recovery.facts import (
    FACT_MEDIA_ENCODING_REFUSED,
    FACT_MEDIA_ENDPOINT_REFUSED,
    FACT_MEDIA_ENDPOINT_SURFACE,
    FACT_MEDIA_PARAM_ENUM,
    FACT_MEDIA_PARAM_REFUSED,
    FACT_MEDIA_PARAM_REQUIRED,
)
from tests.api.support import create_test_app

GEMINI_IMAGE = "gemini/gemini-2.5-flash-image"
GEMINI_VIDEO = "gemini/veo-3.1-generate-preview"
TOGETHER_IMAGE = "together/black-forest-labs/FLUX.1-schnell"


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    reset_media_overrides_cache()
    base = {
        "GEMINI_API_KEY": "gm-" + "a" * 40,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "MODEL_IMAGE": GEMINI_IMAGE,
        "MODEL_IMAGE_FALLBACKS": TOGETHER_IMAGE,
        "MODEL_VIDEO": GEMINI_VIDEO,
    }
    base.update(values)
    return Settings.model_validate(base)


def _local_client(settings: Settings) -> TestClient:
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _row(payload: dict, ref: str) -> dict:
    return next(row for row in payload["models"] if row["model_ref"] == ref)


def test_media_models_payload_includes_ladders_capability_facts_and_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    store = learned_fact_store()
    store.record(
        "gemini",
        "veo-3.1-generate-preview",
        FACT_MEDIA_ENDPOINT_SURFACE,
        "gemini_veo_lro",
        detail="video_create",
        evidence="200 OK on predictLongRunning",
    )
    store.record(
        "gemini",
        "veo-3.1-generate-preview",
        FACT_MEDIA_ENDPOINT_REFUSED,
        True,
        detail="video_create:openai_videos",
        evidence="404 on /videos",
    )
    store.record(
        "gemini",
        "*",
        FACT_MEDIA_PARAM_REFUSED,
        True,
        detail="video_create:seed",
        evidence="seed not supported",
    )
    save_media_overrides(
        with_media_override(
            EMPTY_MEDIA_OVERRIDES,
            GEMINI_IMAGE,
            "image_generate",
            MediaOperationOverride(
                preferred_surface="gemini_generate_content_image",
                pin_surface=True,
                param_defaults={"aspect_ratio": "16:9"},
                unset_params=("response_format",),
            ),
        )
    )

    client = _local_client(settings)
    resp = client.get("/admin/api/media/models")
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    video_row = _row(payload, GEMINI_VIDEO)
    assert "capability" in video_row
    assert video_row["capability"]["sync_mode"] == "async_job"
    assert "video_create" in video_row["endpoint_ladders"]
    video_ladder = video_row["endpoint_ladders"]["video_create"]
    assert video_ladder["winner"]["surface_key"] == "gemini_veo_lro"
    assert video_ladder["winner"]["source"] == "learned"
    assert video_ladder["winner"]["refused"] is False
    refused = [
        c for c in video_ladder["candidates"] if c["surface_key"] == "openai_videos"
    ]
    assert len(refused) == 1
    assert refused[0]["refused"] is True

    fact_pairs = {
        (f["fact_kind"], f["model_id"], f["detail"]) for f in video_row["learned_facts"]
    }
    assert (
        FACT_MEDIA_ENDPOINT_SURFACE,
        "veo-3.1-generate-preview",
        "video_create",
    ) in fact_pairs
    assert (
        FACT_MEDIA_ENDPOINT_REFUSED,
        "veo-3.1-generate-preview",
        "video_create:openai_videos",
    ) in fact_pairs
    assert (FACT_MEDIA_PARAM_REFUSED, "*", "video_create:seed") in fact_pairs

    image_row = _row(payload, GEMINI_IMAGE)
    image_ladder = image_row["endpoint_ladders"]["image_generate"]
    assert image_ladder["winner"]["surface_key"] == "gemini_generate_content_image"
    assert image_ladder["winner"]["source"] == "operator"
    assert image_ladder["winner"]["detail"] == "pinned"
    assert image_row["overrides"]["effective"]["image_generate"] == {
        "preferred_surface": "gemini_generate_content_image",
        "pin_surface": True,
        "param_defaults": {"aspect_ratio": "16:9"},
        "unset_params": ["response_format"],
    }


def test_media_overrides_save_and_remove_routes(monkeypatch, tmp_path: Path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    client = _local_client(settings)

    save_resp = client.post(
        "/admin/api/media/overrides",
        json={
            "modelRef": GEMINI_VIDEO,
            "operation": "video_create",
            "preferredSurface": "gemini_veo_lro",
            "pinSurface": True,
            "paramDefaults": {"aspect_ratio": "16:9"},
            "unsetParams": ["size", " size ", "quality"],
        },
    )
    assert save_resp.status_code == 200, save_resp.text
    saved = save_resp.json()
    assert saved["saved"] is True
    assert saved["target"] == GEMINI_VIDEO
    assert saved["override"] == {
        "preferred_surface": "gemini_veo_lro",
        "pin_surface": True,
        "param_defaults": {"aspect_ratio": "16:9"},
        "unset_params": ["size", "quality"],
    }

    on_disk = json.loads((tmp_path / "media_overrides.json").read_text("utf-8"))
    assert (
        on_disk["models"][GEMINI_VIDEO]["video_create"]["preferred_surface"]
        == "gemini_veo_lro"
    )

    models_payload = client.get("/admin/api/media/models").json()
    ladder = _row(models_payload, GEMINI_VIDEO)["endpoint_ladders"]["video_create"]
    assert ladder["winner"]["surface_key"] == "gemini_veo_lro"
    assert ladder["winner"]["source"] == "operator"

    rem_resp = client.post(
        "/admin/api/media/overrides/remove",
        json={"modelRef": GEMINI_VIDEO, "operation": "video_create"},
    )
    assert rem_resp.status_code == 200, rem_resp.text
    assert rem_resp.json()["removed"] is True
    assert load_media_overrides().is_empty

    rem_again = client.post(
        "/admin/api/media/overrides/remove",
        json={"modelRef": GEMINI_VIDEO, "operation": "video_create"},
    )
    assert rem_again.status_code == 200
    assert rem_again.json()["removed"] is False


def test_media_overrides_validation_and_loopback_guard(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    client = _local_client(settings)

    # Unknown surface for provider/operation -> 400
    bad_surface = client.post(
        "/admin/api/media/overrides",
        json={
            "modelRef": GEMINI_VIDEO,
            "operation": "video_create",
            "preferredSurface": "nonexistent_surface",
        },
    )
    assert bad_surface.status_code == 400

    # Invalid param_defaults field key -> 400
    bad_param = client.post(
        "/admin/api/media/overrides",
        json={
            "modelRef": GEMINI_VIDEO,
            "operation": "video_create",
            "paramDefaults": {"bad.key": 1},
        },
    )
    assert bad_param.status_code == 400

    # Unconfigured rail ref -> 404
    missing_ref = client.post(
        "/admin/api/media/overrides",
        json={
            "modelRef": "deepseek/deepseek-chat",
            "operation": "image_generate",
            "preferredSurface": "openai_images",
        },
    )
    assert missing_ref.status_code == 404

    # Non-loopback client -> 403
    remote = TestClient(create_test_app(settings), client=("203.0.113.10", 50000))
    assert (
        remote.post(
            "/admin/api/media/overrides",
            json={
                "modelRef": GEMINI_VIDEO,
                "operation": "video_create",
                "preferredSurface": "gemini_veo_lro",
            },
        ).status_code
        == 403
    )
    assert (
        remote.post(
            "/admin/api/media/overrides/remove",
            json={"modelRef": GEMINI_VIDEO, "operation": "video_create"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    ("fact_kind", "model_id", "detail", "value"),
    [
        (
            FACT_MEDIA_PARAM_REQUIRED,
            "veo-3.1-generate-preview",
            "video_create:mode",
            True,
        ),
        (
            FACT_MEDIA_PARAM_REFUSED,
            "veo-3.1-generate-preview",
            "video_create:seed",
            True,
        ),
        (
            FACT_MEDIA_ENDPOINT_SURFACE,
            "veo-3.1-generate-preview",
            "video_create",
            "gemini_veo_lro",
        ),
        (
            FACT_MEDIA_ENDPOINT_REFUSED,
            "veo-3.1-generate-preview",
            "video_create:openai_videos",
            True,
        ),
        (
            FACT_MEDIA_PARAM_ENUM,
            "veo-3.1-generate-preview",
            "video_create:aspect_ratio",
            ["16:9", "9:16"],
        ),
        (FACT_MEDIA_ENCODING_REFUSED, "*", "video_create:multipart", True),
    ],
)
def test_forget_accepts_every_fact_media_kind(
    monkeypatch, tmp_path: Path, fact_kind: str, model_id: str, detail: str, value
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    store = learned_fact_store()
    store.record("gemini", model_id, fact_kind, value, detail=detail, evidence="test")
    client = _local_client(settings)

    resp = client.post(
        "/admin/api/model-admin/learned/forget",
        json={
            "providerId": "gemini",
            "modelId": model_id,
            "factKind": fact_kind,
            "detail": detail,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["forgotten"] == 1
    assert store.all_facts() == ()


def test_request_endpoint_routing_media_provenance_and_legacy_untouched(
    monkeypatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    client = _local_client(settings)

    # Pure helper: legacy / chat rows do NOT get media_routing.
    chat_routing = request_endpoint_routing(
        {
            "endpoint": "/v1/messages",
            "protocol": "anthropic",
            "provider": "groq",
            "params": {"max_tokens": 100},
            "route_attempts": [],
        }
    )
    assert "media_routing" not in chat_routing

    # Pure helper: media row with params.media provenance gets media_routing.
    media_routing = request_endpoint_routing(
        {
            "endpoint": "/v1/videos",
            "protocol": "openai_videos",
            "provider": "gemini",
            "params": {
                "media": {
                    "operation": "video_create",
                    "surface": "gemini_veo_lro",
                    "surface_source": "provider_list",
                    "adapted": ["multipart->json", "seconds->durationSeconds"],
                }
            },
            "route_attempts": [],
        }
    )
    assert media_routing["media_routing"] == {
        "surface": "gemini_veo_lro",
        "surface_source": "provider_list",
        "adapted": ["multipart->json", "seconds->durationSeconds"],
    }

    # Persisted request log roundtrip on /admin/api/requests and /admin/api/requests/{id}.
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.enqueue(
        RequestRecord(
            id="req-media-prov",
            endpoint="/v1/videos",
            protocol="openai_videos",
            provider="gemini",
            resolved_model=GEMINI_VIDEO,
            status="success",
            params={
                "media": {
                    "operation": "video_create",
                    "surface": "gemini_veo_lro",
                    "surface_source": "learned",
                    "adapted": ["multipart->json"],
                }
            },
            attempts=(
                RouteAttempt(
                    attempt=0,
                    provider="gemini",
                    model_ref=GEMINI_VIDEO,
                    outcome=RouteAttemptOutcome.SUCCEEDED,
                ),
            ),
        )
    )
    store.close()

    detail = client.get("/admin/api/requests/req-media-prov")
    assert detail.status_code == 200, detail.text
    assert detail.json()["endpoint_routing"]["media_routing"] == {
        "surface": "gemini_veo_lro",
        "surface_source": "learned",
        "adapted": ["multipart->json"],
    }

    listing = client.get("/admin/api/requests")
    assert listing.status_code == 200
    (row,) = [r for r in listing.json()["rows"] if r["id"] == "req-media-prov"]
    assert row["endpoint_routing"]["media_routing"] == {
        "surface": "gemini_veo_lro",
        "surface_source": "learned",
        "adapted": ["multipart->json"],
    }
