"""``GET /admin/api/media/models``: the Models page's media rows (7.67.0).

One row per ref on any media rail -- where it sits on each rail, what its
provider declares, what models.dev says it produces, the MEDIA bench (never
the chat one) and whether its provider has a key. Every key and ref is fake.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.application.execution import route_health_registry
from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.media.rails import configured_media_model_refs
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
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
    # Absent from the cache: unknown, never "text only".
    assert _row(payload, GROQ_TTS)["modalities"] == {
        "output": None,
        "tier": None,
        "approximate": False,
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
