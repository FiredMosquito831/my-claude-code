"""Each list offers only its own kind of model, from declared data (7.78.2).

``/v1/models`` and the harness catalogues stop offering a model whose stated
kind is not chat; ``/admin/api/models`` tells every picker what kind each ref
is; the Models page and its Media section say it on the row. A model nobody
has described stays everywhere it was. Every key and ref here is fake.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache
from tests.api.support import create_test_app, provider_manager_for_app

CHAT = "together/acme/chat-1"
DRAWS = "together/acme/draw-1"
HEARS = "together/acme/hear-1"
QUIET = "together/acme/quiet-1"
PLACED = "together/acme/placed-image"


def _models_dev() -> None:
    write_models_dev_cache(
        {
            "togetherai": {
                "models": {
                    "acme/chat-1": {
                        "id": "acme/chat-1",
                        "modalities": {"input": ["text"], "output": ["text"]},
                    },
                    "acme/draw-1": {
                        "id": "acme/draw-1",
                        "modalities": {"input": ["text"], "output": ["image"]},
                    },
                    "acme/hear-1": {
                        "id": "acme/hear-1",
                        "modalities": {"input": ["audio"], "output": ["text"]},
                    },
                }
            }
        }
    )


def _app(monkeypatch, tmp_path: Path, **values: str):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    _models_dev()
    base = {
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "MODEL": CHAT,
        "MODEL_IMAGE": PLACED,
    }
    base.update(values)
    app = create_test_app(Settings.model_validate(base))
    provider_manager_for_app(app).cache_model_infos(
        "together",
        {
            ProviderModelInfo("acme/chat-1"),
            ProviderModelInfo("acme/draw-1"),
            ProviderModelInfo("acme/hear-1"),
            ProviderModelInfo("acme/quiet-1"),
            ProviderModelInfo("acme/placed-image"),
        },
    )
    return app


def _admin(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _v1_refs(app) -> set[str]:
    data = TestClient(app).get("/v1/models").json()["data"]
    refs = {item["display_name"].removesuffix(" (no thinking)") for item in data}
    return {ref for ref in refs if ref.startswith("together/")}


def test_v1_models_keeps_chat_and_unknown_and_drops_stated_media(
    monkeypatch, tmp_path
) -> None:
    refs = _v1_refs(_app(monkeypatch, tmp_path))

    assert refs == {CHAT, QUIET}


def test_a_media_model_saved_on_a_chat_rail_stays_listed(monkeypatch, tmp_path) -> None:
    refs = _v1_refs(_app(monkeypatch, tmp_path, MODEL_HAIKU_FALLBACKS=DRAWS))

    assert DRAWS in refs


def test_the_pickers_are_told_each_refs_stated_kind(monkeypatch, tmp_path) -> None:
    body = _admin(_app(monkeypatch, tmp_path)).get("/admin/api/models").json()

    # Every ref is still offered to the pickers; the kinds decide which picker.
    assert {CHAT, DRAWS, HEARS, QUIET, PLACED} <= set(body["models"])
    assert body["kinds"] == {
        CHAT: ["chat"],
        DRAWS: ["image"],
        HEARS: ["asr"],
        # Saved on the Image rail and on no chat rail: the operator said so.
        PLACED: ["image"],
    }
    assert QUIET not in body["kinds"]
    assert body["kind_labels"] == {
        "chat": "Chat",
        "image": "Image",
        "tts": "Speech",
        "asr": "Transcription",
        "video": "Video",
    }


def test_the_models_page_row_says_the_kind_and_who_stated_it(
    monkeypatch, tmp_path
) -> None:
    page = _admin(_app(monkeypatch, tmp_path)).get("/admin/api/model-admin").json()
    rows = {
        model["model_ref"]: model
        for provider in page["providers"]
        for model in provider["models"]
    }

    assert rows[DRAWS]["kind"]["kinds"] == ["image"]
    assert rows[DRAWS]["kind"]["source"] == "models_dev"
    assert rows[DRAWS]["kind"]["tier"] == "models.dev bucket, exact id"
    assert rows[PLACED]["kind"]["source"] == "media_rail"
    assert rows[QUIET]["kind"]["kinds"] is None
    assert page["kind_labels"]["tts"] == "Speech"


def test_the_media_section_marks_a_saved_model_of_another_kind(
    monkeypatch, tmp_path
) -> None:
    # QUIET is saved on a chat rail too, so nothing states its kind.
    app = _app(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE=CHAT,
        MODEL_IMAGE_FALLBACKS=QUIET,
        MODEL_HAIKU=QUIET,
    )
    payload = _admin(app).get("/admin/api/media/models").json()
    rows = {row["model_ref"]: row for row in payload["models"]}

    (chat_on_image,) = rows[CHAT]["placements"]
    assert chat_on_image["kind_matches"] is False
    assert rows[CHAT]["kind"]["kinds"] == ["chat"]
    # Unknown is never called a mismatch.
    (quiet_on_image,) = rows[QUIET]["placements"]
    assert quiet_on_image["kind_matches"] is None
