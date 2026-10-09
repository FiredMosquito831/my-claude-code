"""Every list reads the provider's own list first, through ONE lookup (7.80.0).

The five consumers of a model's kind -- ``/v1/models``, the harness
catalogues (``/admin/api/catalogue-models``), the pickers' ``/admin/api/models``,
the Models page and its Media section -- must agree on every ref, because each
asks the request runtime for the same two lookups. The records here carry what
a provider's ``/models`` row declared (kept on the record since 7.79.0); every
key and ref is fake.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache
from tests.api.support import create_test_app, provider_manager_for_app

#: models.dev says chat; the provider's list says chat + transcription.
LIST_HEARS = "together/acme/list-hears"
#: Nothing in models.dev; the provider's list says it draws.
LIST_DRAWS = "together/acme/list-draws"
#: Nothing in models.dev; only the endpoint the provider serves it on.
ENDPOINT_SPEAKS = "together/acme/endpoint-speaks"
#: Anthropic-style ``type: "model"``: not a kind, so nothing is stated.
TYPED_MODEL = "together/acme/typed-model"
#: models.dev says chat + transcription; the endpoints only imply chat.
WORDS_LOSE = "together/acme/words-lose"
#: Saved on the Image rail by its tagged name; its record is the untagged id.
TAGGED_DRAWS = f"{LIST_DRAWS}:free"
#: Saved on a chat fallback by its tagged name, so the Models page lists it.
TAGGED_HEARS = f"{LIST_HEARS}:free"


def _models_dev() -> None:
    write_models_dev_cache(
        {
            "togetherai": {
                "models": {
                    "acme/list-hears": {
                        "id": "acme/list-hears",
                        "modalities": {"input": ["text"], "output": ["text"]},
                    },
                    "acme/words-lose": {
                        "id": "acme/words-lose",
                        "modalities": {"input": ["audio", "text"], "output": ["text"]},
                    },
                }
            }
        }
    )


def _app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    _models_dev()
    app = create_test_app(
        Settings.model_validate(
            {
                "TOGETHER_API_KEY": "tg-" + "c" * 40,
                "MODEL": LIST_HEARS,
                "MODEL_IMAGE": TAGGED_DRAWS,
                "MODEL_TTS": ENDPOINT_SPEAKS,
                "MODEL_HAIKU_FALLBACKS": TAGGED_HEARS,
            }
        )
    )
    provider_manager_for_app(app).cache_model_infos(
        "together",
        {
            ProviderModelInfo(
                "acme/list-hears",
                declared=ProviderModelDeclaration(
                    modalities=DeclaredModalities(("audio", "text"), ("text",))
                ),
            ),
            ProviderModelInfo(
                "acme/list-draws",
                declared=ProviderModelDeclaration(
                    modalities=DeclaredModalities(("text",), ("image",))
                ),
            ),
            ProviderModelInfo(
                "acme/endpoint-speaks",
                declared=ProviderModelDeclaration(endpoints=("/v1/audio/speech",)),
            ),
            ProviderModelInfo(
                "acme/typed-model",
                declared=ProviderModelDeclaration(model_type="model"),
            ),
            ProviderModelInfo(
                "acme/words-lose",
                declared=ProviderModelDeclaration(endpoints=("/chat/completions",)),
            ),
        },
    )
    return app


def _admin(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _together(refs) -> set[str]:
    return {ref for ref in refs if ref.startswith("together/")}


def test_all_five_consumers_agree_on_every_ref(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)
    admin = _admin(app)

    v1 = _together(
        item["display_name"].removesuffix(" (no thinking)")
        for item in TestClient(app).get("/v1/models").json()["data"]
    )
    catalogue = _together(
        model["provider_model_ref"]
        for model in admin.get("/admin/api/catalogue-models").json()["models"]
    )
    pickers = admin.get("/admin/api/models").json()["kinds"]
    page = {
        model["model_ref"]: model["kind"]
        for provider in admin.get("/admin/api/model-admin").json()["providers"]
        for model in provider["models"]
    }
    media = {
        row["model_ref"]: row
        for row in admin.get("/admin/api/media/models").json()["models"]
    }

    # The chat lists: a stated non-chat kind leaves both, an unknown stays.
    expected_chat = {LIST_HEARS, TAGGED_HEARS, TYPED_MODEL, WORDS_LOSE}
    assert v1 == expected_chat
    assert catalogue == expected_chat

    # The pickers and the page state the same kinds for every ref.
    assert pickers[LIST_HEARS] == ["chat", "asr"]
    assert pickers[LIST_DRAWS] == ["image"]
    assert pickers[ENDPOINT_SPEAKS] == ["tts"]
    assert pickers[WORDS_LOSE] == ["chat", "asr"]
    assert pickers[TAGGED_DRAWS] == ["image"]
    assert pickers[TAGGED_HEARS] == ["chat", "asr"]
    assert TYPED_MODEL not in pickers
    for ref, kinds in pickers.items():
        if ref in page:
            assert page[ref]["kinds"] == kinds, ref
        if ref in media:
            assert media[ref]["kind"]["kinds"] == kinds, ref
    assert page[TYPED_MODEL]["kinds"] is None


def test_the_models_page_says_which_rung_stated_each_kind(
    monkeypatch, tmp_path
) -> None:
    page = {
        model["model_ref"]: model["kind"]
        for provider in _admin(_app(monkeypatch, tmp_path))
        .get("/admin/api/model-admin")
        .json()["providers"]
        for model in provider["models"]
    }

    assert page[LIST_HEARS]["source"] == "provider_listing"
    assert page[LIST_HEARS]["source_label"] == "the provider's model list"
    assert page[LIST_HEARS]["tier"] == "provider /models, exact id"
    assert page[ENDPOINT_SPEAKS]["source"] == "provider_words"
    assert page[ENDPOINT_SPEAKS]["source_label"] == (
        "the provider's model type or endpoints"
    )
    # Words never outrank models.dev's finer pair.
    assert page[WORDS_LOSE]["source"] == "models_dev"
    assert page[WORDS_LOSE]["tier"] == "models.dev bucket, exact id"
    # A configured tagged ref resolves through the runtime's tag-stripped rung.
    assert page[TAGGED_HEARS]["kinds"] == ["chat", "asr"]
    assert page[TAGGED_HEARS]["source"] == "provider_listing"
    assert page[TAGGED_HEARS]["tier"] == "provider /models, tag stripped"
    assert page[TYPED_MODEL]["source"] is None


def test_the_media_section_reads_the_provider_list_first(monkeypatch, tmp_path) -> None:
    rows = {
        row["model_ref"]: row
        for row in _admin(_app(monkeypatch, tmp_path))
        .get("/admin/api/media/models")
        .json()["models"]
    }

    drawn = rows[TAGGED_DRAWS]
    assert drawn["kind"]["source"] == "provider_listing"
    (placement,) = drawn["placements"]
    assert placement["kind_matches"] is True
    assert drawn["modalities"] == {
        "output": ["image"],
        "tier": "provider /models, tag stripped",
        "approximate": False,
        "source": "provider_listing",
        "source_label": "the provider's model list",
    }
    spoken = rows[ENDPOINT_SPEAKS]
    assert spoken["kind"]["kinds"] == ["tts"]
    assert spoken["kind"]["source"] == "provider_words"
    # Words name no modality, so the output list is still unknown.
    assert spoken["modalities"]["output"] is None
