"""``GET /admin/api/media/models``: the Models page's media rows (7.67.0).

One row per ref on any media rail -- where it sits on each rail, what its
provider declares, what models.dev says it produces, the MEDIA bench (never
the chat one) and whether its provider has a key. Every key and ref is fake.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.execution import route_health_registry
from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.media.rails import configured_media_model_refs
from my_claude_code.config.media_request_fields import (
    EMPTY_STATEMENTS,
    OperatorFieldStatement,
    save_media_request_fields,
    with_statement,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache
from tests.api.support import create_test_app

TOGETHER_IMAGE = "together/black-forest-labs/FLUX.1-schnell"
DEEPINFRA_IMAGE = "deepinfra/stabilityai/sdxl-turbo"
GROQ_IMAGE = "groq/not-an-image-model"
GROQ_TTS = "groq/playai-tts"
GROQ_ASR = "groq/whisper-large-v3"
DEEPINFRA_ASR = "deepinfra/openai/whisper-large-v3"
OPENROUTER_VIDEO = "open_router/google/veo-3.1"


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "GROQ_API_KEY": "gsk_" + "c" * 40,
        "OPENROUTER_API_KEY": "sk-or-" + "d" * 40,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": TOGETHER_IMAGE,
        "MODEL_IMAGE_FALLBACKS": f"{DEEPINFRA_IMAGE},{GROQ_IMAGE}",
        "MODEL_TTS": GROQ_TTS,
        "MODEL_ASR": GROQ_ASR,
        "MODEL_ASR_FALLBACKS": DEEPINFRA_ASR,
        "MODEL_ASR_PAUSED": DEEPINFRA_ASR,
        "MODEL_VIDEO": OPENROUTER_VIDEO,
    }
    base.update(values)
    return Settings.model_validate(base)


def _payload(settings: Settings) -> dict:
    client = TestClient(create_test_app(settings), client=("127.0.0.1", 50000))
    response = client.get("/admin/api/media/models")
    assert response.status_code == 200, response.text
    return response.json()


def _row(payload: dict, ref: str) -> dict:
    (row,) = [row for row in payload["models"] if row["model_ref"] == ref]
    return row


def test_one_row_per_ref_with_its_rail_position_and_pause(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    payload = _payload(settings)

    refs = [row["model_ref"] for row in payload["models"]]
    assert refs == [
        TOGETHER_IMAGE,
        DEEPINFRA_IMAGE,
        GROQ_IMAGE,
        GROQ_TTS,
        GROQ_ASR,
        DEEPINFRA_ASR,
        OPENROUTER_VIDEO,
    ]
    # Exactly the media twin of the chat catalogue's list, in its order.
    assert refs == list(configured_media_model_refs(settings))
    assert [rail["label"] for rail in payload["rails"]] == [
        "Image",
        "Speech",
        "Transcription",
        "Video",
    ]
    placements = {
        row["model_ref"]: [
            (p["label"], p["position"], p["paused"]) for p in row["placements"]
        ]
        for row in payload["models"]
    }
    assert placements[TOGETHER_IMAGE] == [("Image", "primary", False)]
    assert placements[DEEPINFRA_IMAGE] == [("Image", "fallback 1", False)]
    assert placements[GROQ_IMAGE] == [("Image", "fallback 2", False)]
    assert placements[DEEPINFRA_ASR] == [("Transcription", "fallback 1", True)]
    assert placements[OPENROUTER_VIDEO] == [("Video", "primary", False)]


def test_a_ref_on_two_rails_is_one_row_with_two_placements(monkeypatch, tmp_path):
    payload = _payload(
        _settings(monkeypatch, tmp_path, MODEL_TTS_FALLBACKS=TOGETHER_IMAGE)
    )

    row = _row(payload, TOGETHER_IMAGE)
    assert [(p["label"], p["position"]) for p in row["placements"]] == [
        ("Image", "primary"),
        ("Speech", "fallback 1"),
    ]


def test_a_row_lists_the_operations_its_provider_declares(monkeypatch, tmp_path):
    payload = _payload(_settings(monkeypatch, tmp_path))

    together = _row(payload, TOGETHER_IMAGE)
    assert [op["operation"] for op in together["declared"]] == [
        "image_generate",
        "speech",
        "transcribe",
    ]
    speech = together["declared"][1]
    assert speech["formats"] == ["mp3", "wav", "raw"]
    assert speech["label"] == "speech"
    video = _row(payload, OPENROUTER_VIDEO)
    assert [op["operation"] for op in video["declared"]] == [
        "speech",
        "transcribe",
        "video_create",
        "video_retrieve",
        "video_content",
    ]


def test_a_provider_that_declares_nothing_for_the_rail_is_not_served(
    monkeypatch, tmp_path
):
    """Groq serves audio only: on the Image rail it is skipped, uncharged."""

    payload = _payload(_settings(monkeypatch, tmp_path))

    (groq_image,) = _row(payload, GROQ_IMAGE)["placements"]
    assert groq_image["served"] is False
    (together,) = _row(payload, TOGETHER_IMAGE)["placements"]
    assert together["served"] is True
    (groq_tts,) = _row(payload, GROQ_TTS)["placements"]
    assert groq_tts["served"] is True


def test_the_media_bench_is_read_and_never_the_chat_bench(monkeypatch, tmp_path):
    settings = _settings(
        monkeypatch,
        tmp_path,
        FALLBACK_BEHAVIOR="consecutive",
        FALLBACK_EJECT_AFTER_FAILURES="1",
        FALLBACK_BENCH_ENABLED="true",
    )
    # Chat benched it: the media row does not care.
    route_health_registry(settings).record_failure(
        GROQ_TTS, failure_kind="upstream", status_code=500
    )
    assert route_health_registry(settings).is_ejected(GROQ_TTS)
    assert _row(_payload(settings), GROQ_TTS)["health"] == {"benched": False}

    # The media books benched it: now the row says so, with the reason.
    media_route_health_registry(settings).record_failure(
        GROQ_TTS, failure_kind="upstream", status_code=500
    )
    health = _row(_payload(settings), GROQ_TTS)["health"]
    assert health["benched"] is True
    assert health["reason"].startswith("benched:")
    assert health["remaining_seconds"] > 0
    # ...and another ref the chat books never saw is untouched.
    assert _row(_payload(settings), GROQ_ASR)["health"] == {"benched": False}


def test_a_provider_without_a_key_says_missing_key(monkeypatch, tmp_path):
    payload = _payload(_settings(monkeypatch, tmp_path))

    assert _row(payload, DEEPINFRA_IMAGE)["key"] == {
        "status": "missing_key",
        "label": "Missing key",
        "key_count": 0,
    }
    assert _row(payload, TOGETHER_IMAGE)["key"] == {
        "status": "configured",
        "label": "Configured",
        "key_count": 1,
    }


def test_models_dev_output_modalities_are_advisory(monkeypatch, tmp_path):
    write_models_dev_cache(
        {
            "togetherai": {
                "models": {
                    "black-forest-labs/FLUX.1-schnell": {
                        "id": "black-forest-labs/FLUX.1-schnell",
                        "modalities": {"input": ["text"], "output": ["image"]},
                    }
                }
            }
        }
    )

    payload = _payload(_settings(monkeypatch, tmp_path))

    known = _row(payload, TOGETHER_IMAGE)["modalities"]
    assert known["output"] == ["image"]
    assert known["tier"] == "models.dev bucket, exact id"
    assert known["approximate"] is False
    # 7.80.0: who stated it, now that a provider's own list can answer first.
    assert known["source"] == "models_dev"
    assert known["source_label"] == "models.dev"
    # Absent from the cache: unknown, never "text only".
    assert _row(payload, GROQ_TTS)["modalities"] == {
        "output": None,
        "tier": None,
        "approximate": False,
        "source": None,
        "source_label": None,
    }


def test_the_provider_table_lists_every_media_provider(monkeypatch, tmp_path):
    registry = get_provider_registry()
    registry.add(
        display_name="Speaker",
        base_url="https://speaker.example/v1",
        api_keys=("sk-speaker-aaaa1111",),
        media_operations=["speech", "transcribe"],
    )
    registry.add(
        display_name="Chatty",
        base_url="https://chatty.example/v1",
        api_keys=("sk-chatty-aaaa1111",),
    )

    payload = _payload(_settings(monkeypatch, tmp_path))

    providers = {entry["provider_id"]: entry for entry in payload["providers"]}
    assert "anthropic" not in providers
    assert "custom_chatty" not in providers
    speaker = providers["custom_speaker"]
    assert speaker["custom"] is True
    assert speaker["enabled"] is True
    assert [op["operation"] for op in speaker["declared"]] == ["speech", "transcribe"]
    assert speaker["rails"] == ["Speech", "Transcription"]
    assert providers["together"]["rails"] == ["Image", "Speech", "Transcription"]
    assert providers["open_router"]["rails"] == ["Speech", "Transcription", "Video"]
    assert providers["together"]["custom"] is False


def test_a_custom_video_box_shows_its_content_fallback(monkeypatch, tmp_path):
    """7.95.0: the path asked only without a URL is shown as exactly that."""
    registry = get_provider_registry()
    registry.add(
        display_name="Vid",
        base_url="https://vid.example/v1",
        api_keys=("sk-vid-aaaa1111",),
        media_operations=["video"],
    )

    payload = _payload(_settings(monkeypatch, tmp_path))

    providers = {entry["provider_id"]: entry for entry in payload["providers"]}
    declared = providers["custom_vid"]["declared"]
    assert [(op["operation"], op["label"], op["path"]) for op in declared] == [
        ("video_create", "video create", "videos"),
        ("video_retrieve", "video status", "videos/{id}"),
        ("video_content", "video content (when no URL)", "videos/{id}/content"),
    ]
    # A built-in's declared content path keeps its plain label.
    (content,) = [
        op
        for op in providers["open_router"]["declared"]
        if op["operation"] == "video_content"
    ]
    assert content["label"] == "video content"


def test_a_disabled_custom_provider_on_a_rail_says_so(monkeypatch, tmp_path):
    """A rail may still name a switched-off custom provider; it cannot serve."""

    registry = get_provider_registry()
    registry.add(
        display_name="Speaker",
        base_url="https://speaker.example/v1",
        api_keys=("sk-speaker-aaaa1111",),
        media_operations=["speech"],
    )
    registry.update("custom_speaker", enabled=False)

    payload = _payload(
        _settings(monkeypatch, tmp_path, MODEL_TTS_FALLBACKS="custom_speaker/tts-1")
    )

    row = _row(payload, "custom_speaker/tts-1")
    assert row["provider_state"] == "disabled"
    assert row["custom"] is True
    assert row["key"]["status"] == "disabled"
    assert [(p["label"], p["position"]) for p in row["placements"]] == [
        ("Speech", "fallback 1")
    ]
    (speaker,) = [
        p for p in payload["providers"] if p["provider_id"] == "custom_speaker"
    ]
    assert speaker["enabled"] is False


def test_an_empty_install_has_rails_and_no_rows(monkeypatch, tmp_path):
    payload = _payload(
        _settings(
            monkeypatch,
            tmp_path,
            MODEL_IMAGE="",
            MODEL_IMAGE_FALLBACKS="",
            MODEL_TTS="",
            MODEL_ASR="",
            MODEL_ASR_FALLBACKS="",
            MODEL_ASR_PAUSED="",
            MODEL_VIDEO="",
        )
    )

    assert payload["models"] == []
    assert [rail["refs"] for rail in payload["rails"]] == [[], [], [], []]


def test_the_route_is_local_only(monkeypatch, tmp_path):
    client = TestClient(
        create_test_app(_settings(monkeypatch, tmp_path)),
        client=("203.0.113.9", 50000),
    )

    assert client.get("/admin/api/media/models").status_code == 403


# -- 7.98.0: each row's request fields ---------------------------------------

#: Every key a row carried before 7.98.0, in its order.
_ROW_KEYS_BEFORE_7_98 = [
    "model_ref",
    "provider_id",
    "provider_name",
    "model_id",
    "custom",
    "provider_state",
    "placements",
    "declared",
    "modalities",
    "kind",
    "health",
    "key",
]


def test_request_schema_is_additive(monkeypatch, tmp_path):
    """One key after every key a row had; with a learned fact and an operator
    statement present, every pre-existing key is exactly what it is without.
    """

    settings = _settings(monkeypatch, tmp_path)
    plain = _payload(settings)
    for row in plain["models"]:
        assert list(row) == [*_ROW_KEYS_BEFORE_7_98, "request_schema"]

    learned_fact_store().record(
        "open_router",
        "google/veo-3.1",
        "media_param_required",
        True,
        detail="video_create:mode",
        evidence="mode is required",
    )
    save_media_request_fields(
        with_statement(
            EMPTY_STATEMENTS,
            TOGETHER_IMAGE,
            "image_generate",
            "steps",
            OperatorFieldStatement(state="accepted", type="integer"),
        )
    )
    taught = _payload(settings)

    def without_schema(payload: dict) -> dict:
        return {
            **payload,
            "models": [
                {k: v for k, v in row.items() if k != "request_schema"}
                for row in payload["models"]
            ],
        }

    assert json.dumps(without_schema(taught), sort_keys=False) == json.dumps(
        without_schema(plain), sort_keys=False
    )
    video = _row(taught, OPENROUTER_VIDEO)["request_schema"]["video_create"]
    assert video["properties"]["mode"]["x-mcc"]["state"] == "required (learned)"
    image = _row(taught, TOGETHER_IMAGE)["request_schema"]["image_generate"]
    assert image["properties"]["steps"]["x-mcc"]["source"] == "operator"
    assert image["properties"]["steps"]["type"] == "integer"


def test_a_row_has_a_schema_only_for_operations_its_provider_serves(
    monkeypatch, tmp_path
):
    payload = _payload(_settings(monkeypatch, tmp_path))

    # Together declares image generation and no edits: one schema, not two.
    assert list(_row(payload, TOGETHER_IMAGE)["request_schema"]) == ["image_generate"]
    # Groq declares nothing for the Image rail: it is skipped, so no schema.
    assert _row(payload, GROQ_IMAGE)["request_schema"] == {}
    assert list(_row(payload, GROQ_TTS)["request_schema"]) == ["speech"]
    asr = _row(payload, DEEPINFRA_ASR)["request_schema"]
    assert list(asr) == ["transcribe", "translate"]


def _admin(settings: Settings) -> TestClient:
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def test_the_operator_editor_saves_and_removes_a_statement(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    client = _admin(settings)

    saved = client.post(
        "/admin/api/media/request-fields",
        json={
            "modelRef": OPENROUTER_VIDEO,
            "operation": "video_create",
            "field": "mode",
            "state": "required",
            "values": ["std", " pro ", ""],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["statement"]["values"] == ["std", "pro"]
    mode = _row(_payload(settings), OPENROUTER_VIDEO)["request_schema"]["video_create"][
        "properties"
    ]["mode"]
    assert mode["x-mcc"]["state"] == "required (stated)"
    assert mode["enum"] == ["std", "pro"]
    on_disk = json.loads((tmp_path / "media_request_fields.json").read_text("utf-8"))
    assert list(on_disk["models"]) == [OPENROUTER_VIDEO]

    removed = client.post(
        "/admin/api/media/request-fields/remove",
        json={
            "modelRef": OPENROUTER_VIDEO,
            "operation": "video_create",
            "field": "mode",
        },
    )
    assert removed.json()["removed"] is True
    schema = _row(_payload(settings), OPENROUTER_VIDEO)["request_schema"]
    assert "mode" not in schema["video_create"]["properties"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"operation": "video_create", "field": "mode", "state": "maybe"}, 400),
        ({"operation": "video_delete", "field": "mode", "state": "required"}, 400),
        ({"operation": "video_create", "field": "a.b", "state": "required"}, 400),
        (
            {
                "operation": "video_create",
                "field": "mode",
                "state": "required",
                "type": "uuid",
            },
            400,
        ),
    ],
)
def test_the_editor_refuses_what_it_cannot_store(monkeypatch, tmp_path, body, status):
    client = _admin(_settings(monkeypatch, tmp_path))

    response = client.post(
        "/admin/api/media/request-fields", json={"modelRef": OPENROUTER_VIDEO, **body}
    )

    assert response.status_code == status
    assert not (tmp_path / "media_request_fields.json").exists()


def test_the_editor_is_only_for_a_rail_model_and_only_local(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    body = {
        "modelRef": "deepseek/deepseek-chat",
        "operation": "image_generate",
        "field": "size",
        "state": "accepted",
    }
    assert (
        _admin(settings).post("/admin/api/media/request-fields", json=body).status_code
        == 404
    )
    remote = TestClient(create_test_app(settings), client=("203.0.113.9", 50000))
    assert (
        remote.post(
            "/admin/api/media/request-fields",
            json={**body, "modelRef": OPENROUTER_VIDEO, "operation": "video_create"},
        ).status_code
        == 403
    )


def test_forget_with_a_detail_retires_one_field_and_no_other(monkeypatch, tmp_path):
    """Forget on a media row names its field; without one, every row of the
    kind goes, exactly as before 7.98.0."""

    settings = _settings(monkeypatch, tmp_path)
    store = learned_fact_store()
    for field in ("mode", "quality"):
        store.record(
            "open_router",
            "google/veo-3.1",
            "media_param_required",
            True,
            detail=f"video_create:{field}",
        )
    client = _admin(settings)

    one = client.post(
        "/admin/api/model-admin/learned/forget",
        json={
            "providerId": "open_router",
            "modelId": "google/veo-3.1",
            "factKind": "media_param_required",
            "detail": "video_create:mode",
        },
    )
    assert one.json()["forgotten"] == 1
    assert [fact.detail for fact in store.all_facts()] == ["video_create:quality"]
    schema = _row(_payload(settings), OPENROUTER_VIDEO)["request_schema"]
    assert "mode" not in schema["video_create"]["properties"]
    assert "quality" in schema["video_create"]["required"]

    store.record(
        "open_router",
        "google/veo-3.1",
        "media_param_required",
        True,
        detail="video_create:mode",
    )
    every = client.post(
        "/admin/api/model-admin/learned/forget",
        json={
            "providerId": "open_router",
            "modelId": "google/veo-3.1",
            "factKind": "media_param_required",
        },
    )
    assert every.json() == {
        "forgotten": 2,
        "provider_id": "open_router",
        "model_id": "google/veo-3.1",
        "fact_kind": "media_param_required",
    }
