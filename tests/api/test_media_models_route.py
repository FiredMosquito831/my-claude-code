"""``GET /v1/media/models``: the media rails' models and their request fields.

7.98.0, user decisions 1 and 2 of 2026-10-16: behind the same proxy token as
every other route; one entry per model on any media rail, each with a JSON
Schema per operation it serves; and per rail which models accept, need,
refuse or are silent about every field. ``/v1/models`` keeps every media ref
out. Every key and ref is fake.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.providers.recovery import learned_fact_store
from tests.api.support import create_test_app

TOKEN = "proxy-" + "t" * 24
TOGETHER_IMAGE = "together/black-forest-labs/FLUX.1-schnell"
GROQ_IMAGE = "groq/not-an-image-model"
GROQ_TTS = "groq/playai-tts"
OPENROUTER_VIDEO = "open_router/google/veo-3.1"
DEEPINFRA_VIDEO = "deepinfra/Wan-AI/Wan2.1-T2V-14B"


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "GROQ_API_KEY": "gsk_" + "c" * 40,
        "OPENROUTER_API_KEY": "sk-or-" + "d" * 40,
        "DEEPINFRA_API_KEY": "di-" + "e" * 40,
        "MODEL": "deepseek/deepseek-chat",
        "MODEL_IMAGE": TOGETHER_IMAGE,
        "MODEL_IMAGE_FALLBACKS": GROQ_IMAGE,
        "MODEL_TTS": GROQ_TTS,
        "MODEL_VIDEO": OPENROUTER_VIDEO,
        "MODEL_VIDEO_FALLBACKS": DEEPINFRA_VIDEO,
        "MODEL_VIDEO_PAUSED": DEEPINFRA_VIDEO,
    }
    base.update(values)
    return Settings.model_validate(base)


def _get(client: TestClient, path: str = "/v1/media/models", token: str | None = TOKEN):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.get(path, headers=headers)


def test_the_route_takes_the_same_token_as_every_other_route(monkeypatch, tmp_path):
    client = TestClient(create_test_app(_settings(monkeypatch, tmp_path)))

    missing = _get(client, token=None)
    wrong = _get(client, token="wrong")
    assert (missing.status_code, wrong.status_code) == (401, 401)
    # Answered the way its sibling list, /v1/models, answers.
    assert wrong.json() == _get(client, "/v1/models", token="wrong").json()
    assert _get(client).status_code == 200
    assert (
        client.get("/v1/media/models", headers={"x-api-key": TOKEN}).status_code == 200
    )


def test_one_entry_per_rail_model_with_its_schemas(monkeypatch, tmp_path):
    client = TestClient(create_test_app(_settings(monkeypatch, tmp_path)))

    payload = _get(client).json()

    assert payload["object"] == "list"
    assert [entry["id"] for entry in payload["data"]] == [
        TOGETHER_IMAGE,
        GROQ_IMAGE,
        GROQ_TTS,
        OPENROUTER_VIDEO,
        DEEPINFRA_VIDEO,
    ]
    together = payload["data"][0]
    assert together["rails"] == [
        {"rail": "image", "position": "primary", "paused": False}
    ]
    assert list(together["operations"]) == ["image_generate"]
    generate = together["operations"]["image_generate"]
    assert generate["endpoint"] == "/v1/images/generations"
    schema = generate["request_schema"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["model"]["const"] == TOGETHER_IMAGE
    assert schema["required"] == ["prompt"]
    # A model whose provider serves nothing for its rail has no operation.
    assert payload["data"][1]["operations"] == {}
    paused = payload["data"][4]
    assert paused["rails"] == [
        {"rail": "video", "position": "fallback 1", "paused": True}
    ]
    assert paused["operations"]["video_create"]["endpoint"] == "/v1/videos"


def test_each_rail_says_which_models_accept_each_field(monkeypatch, tmp_path):
    """Decision 2: never only the primary, never only the overlap."""

    learned_fact_store().record(
        "open_router",
        "google/veo-3.1",
        "media_param_required",
        True,
        detail="video_create:mode",
        evidence="mode is required",
    )
    client = TestClient(create_test_app(_settings(monkeypatch, tmp_path)))

    rails = {rail["rail"]: rail for rail in _get(client).json()["rails"]}

    video = rails["video"]
    assert video["models"] == [OPENROUTER_VIDEO, DEEPINFRA_VIDEO]
    create = video["operations"]["video_create"]
    assert create["endpoint"] == "/v1/videos"
    assert create["models"] == [OPENROUTER_VIDEO, DEEPINFRA_VIDEO]
    mode = create["fields"]["mode"]
    assert mode["required_by"] == [OPENROUTER_VIDEO]
    # The other model never refused it: not known, never "unsupported".
    assert mode["not_known_for"] == [DEEPINFRA_VIDEO]
    assert mode["refused_by"] == []
    image = rails["image"]["operations"]
    assert image["image_generate"]["not_served_by"] == [GROQ_IMAGE]
    assert image["image_edit"]["models"] == []


def test_the_chat_list_still_excludes_every_media_ref(monkeypatch, tmp_path):
    client = TestClient(create_test_app(_settings(monkeypatch, tmp_path)))

    listed = {model["id"] for model in _get(client, "/v1/models").json()["data"]}
    media = {entry["id"] for entry in _get(client).json()["data"]}

    assert media
    assert listed.isdisjoint(media)


def test_an_install_with_no_media_rail_lists_nothing(monkeypatch, tmp_path):
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="",
        MODEL_IMAGE_FALLBACKS="",
        MODEL_TTS="",
        MODEL_VIDEO="",
        MODEL_VIDEO_FALLBACKS="",
        MODEL_VIDEO_PAUSED="",
    )
    client = TestClient(create_test_app(settings))

    payload = _get(client).json()

    assert payload["data"] == []
    assert [rail["models"] for rail in payload["rails"]] == [[], [], [], []]
