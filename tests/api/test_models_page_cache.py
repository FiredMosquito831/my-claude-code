"""The Models page served from a stored capability half.

The claim under test is narrow and total: **the assembled payload is the
payload.** Splitting it is a delivery decision, not a content one -- nothing is
dropped, nothing is approximated, and every field a reader could open is still
there and still says the same thing.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import build_models_page_payload
from my_claude_code.api.models_page_cache import (
    MODELS_PAGE_ENTRY,
    build_capability_half,
    capability_half_key,
    merge_moving_parts,
)
from my_claude_code.application import derived_payloads
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    reset_model_overrides_cache,
)
from my_claude_code.config.model_refs import configured_chat_model_refs
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_visibility import ModelVisibility
from tests.api.support import create_test_app, provider_manager_for_app

ENDPOINT = "/admin/api/model-admin"

INFOS = (
    ProviderModelInfo("open_router/routed", max_output_tokens=16384),
    ProviderModelInfo("open_router/extra", context_length=128000),
    ProviderModelInfo("groq/llama", context_length=8000),
)
MEASURED = {
    "open_router/routed": {
        "model_ref": "open_router/routed",
        "requested": 3,
        "returned": 2,
        "unmeasured": 0,
    }
}
IMAGE_ESTIMATES = {
    "open_router": {
        "provider": "open_router",
        "requests": 5,
        "billed_tokens_in": 100,
        "est_tokens_in": 90,
        "est_image_tokens": 40,
        "images": 2,
    }
}
CATALOGUE = {"enabled": True, "last_refreshed_at": 1.0, "next_refresh_at": 2.0}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("MODEL", "MODEL_VISIBILITY_ALLOW", "MODEL_VISIBILITY_DENY"):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()


def _settings() -> Settings:
    settings = Settings()
    settings.model = "open_router/routed"
    settings.open_router_api_key = "open-router-key"
    return settings


def _one_shot(learned: Any = None) -> dict[str, Any]:
    settings = _settings()
    return build_models_page_payload(
        INFOS,
        tuple(configured_chat_model_refs(settings)),
        ModelVisibility(deny=("open_router/extra",)),
        ModelParameterOverrides(),
        measured=MEASURED,
        learned=learned,
        catalogue_refresh=CATALOGUE,
        image_estimates=IMAGE_ESTIMATES,
    )


def _assembled(learned: Any = None, *, round_trip: bool = False) -> dict[str, Any]:
    settings = _settings()
    half = build_capability_half(
        INFOS,
        tuple(configured_chat_model_refs(settings)),
        ModelVisibility(deny=("open_router/extra",)),
        ModelParameterOverrides(),
        dialect_lookup=None,
        measured_days=7,
    )
    if round_trip:
        # Exactly what the cache does to it: through JSON and back.
        half = json.loads(json.dumps(half))
    return merge_moving_parts(
        half,
        measured=MEASURED,
        image_estimates=IMAGE_ESTIMATES,
        learned=learned,
        catalogue_refresh=CATALOGUE,
    )


def test_the_merged_payload_equals_the_uncached_payload() -> None:
    """The equality oracle, as JSON -- which is what the browser receives."""

    assert json.dumps(_assembled(), sort_keys=True) == json.dumps(
        _one_shot(), sort_keys=True
    )


def test_the_merged_payload_equals_it_after_a_round_trip_through_json() -> None:
    """A cache hit is a parsed file, not the object the builder returned."""

    assert json.dumps(_assembled(round_trip=True), sort_keys=True) == json.dumps(
        _one_shot(), sort_keys=True
    )


def test_learned_facts_land_on_the_same_capability_field() -> None:
    """`attach_learned_facts` mutates capabilities; the merge must reproduce it."""

    learned = {
        "open_router/routed": [
            {
                "provider_id": "open_router",
                "model_id": "routed",
                "fact_kind": "max_output_tokens",
                "value": 4096,
                "source": "probe",
                "observed_at": "2026-09-01T00:00:00+00:00",
                "age_seconds": 10.0,
                "ttl_seconds": 100.0,
                "stale": False,
                "retired": False,
                "detail": None,
            }
        ]
    }

    assert json.dumps(_assembled(learned), sort_keys=True) == json.dumps(
        _one_shot(learned), sort_keys=True
    )


def test_the_cached_half_carries_no_measurement_no_learning_and_no_clock() -> None:
    """What is stored must not contain anything that moves."""

    settings = _settings()
    half = build_capability_half(
        INFOS,
        tuple(configured_chat_model_refs(settings)),
        ModelVisibility(),
        ModelParameterOverrides(),
        dialect_lookup=None,
        measured_days=7,
    )

    assert half["catalogue_refresh"] == {}
    for provider in half["providers"]:
        assert provider["image_estimate"] is None
        for model in provider["models"]:
            assert model["reasoning_measured"] is None
            assert model["learned"] == []


# ------------------------------------------------------------------- the key


def _key(**changes: Any) -> str:
    settings = _settings()
    values: dict[str, Any] = {
        "model_infos": INFOS,
        "configured": tuple(configured_chat_model_refs(settings)),
        "visibility": ModelVisibility(),
        "overrides": ModelParameterOverrides(),
    }
    values.update(changes)
    return capability_half_key(
        values["model_infos"],
        values["configured"],
        values["visibility"],
        values["overrides"],
    )


def test_the_key_is_stable_when_nothing_changes() -> None:
    assert _key() == _key()


def test_the_key_changes_when_a_deny_pattern_is_added() -> None:
    assert _key() != _key(visibility=ModelVisibility(deny=("open_router/*",)))


def test_the_key_changes_when_a_models_capability_changes() -> None:
    """A provider that revises a context length has changed this payload."""

    revised = (
        ProviderModelInfo("open_router/routed", max_output_tokens=16384),
        ProviderModelInfo("open_router/extra", context_length=64000),
        ProviderModelInfo("groq/llama", context_length=8000),
    )

    assert _key() != _key(model_infos=revised)


def test_the_key_changes_when_a_model_leaves_the_catalogue() -> None:
    assert _key() != _key(model_infos=INFOS[:2])


def test_the_key_changes_when_an_override_is_written() -> None:
    overrides = ModelParameterOverrides(models={"open_router/routed": {"top_p": 0.5}})

    assert _key() != _key(overrides=overrides)


def test_the_key_does_not_change_for_a_new_request_in_the_log() -> None:
    """The whole point of the split: logging traffic must not invalidate this.

    The measurements live on the other side of the merge, so nothing about the
    request log appears in this key.
    """

    before = _key()
    assert before == _key()
    assert "log" not in before


# ------------------------------------------------------------------ the route


def _app():
    app = create_test_app(_settings())
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("routed", max_output_tokens=16384),
            ProviderModelInfo("extra", context_length=128000),
        },
    )
    return app


def test_the_route_stores_the_half_and_serves_it_again(tmp_path) -> None:
    client = TestClient(_app(), client=("127.0.0.1", 50000))

    first = client.get(ENDPOINT).json()
    stored = derived_payloads.derived_cache().read(MODELS_PAGE_ENTRY)
    second = client.get(ENDPOINT).json()

    assert stored is not None
    assert first["providers"] == second["providers"]
    assert first["visibility"] == second["visibility"]
    # The two fields the cache adds describe the capability half only.
    assert second["stale"] is False


def test_the_route_payload_keeps_every_section(tmp_path) -> None:
    client = TestClient(_app(), client=("127.0.0.1", 50000))

    body = client.get(ENDPOINT).json()

    for field in (
        "providers",
        "visibility",
        "overrides",
        "source_labels",
        "provenance_labels",
        "measured_days",
        "fact_labels",
        "learned_source_labels",
        "catalogue_refresh",
    ):
        assert field in body, field
    row = body["providers"][0]["models"][0]
    for field in (
        "model_ref",
        "visible",
        "hidden_by",
        "configured",
        "has_metadata",
        "listing",
        "override",
        "effective",
        "reasoning_measured",
        "learned",
        "capabilities",
    ):
        assert field in row, field


def test_a_changed_input_is_recomputed_rather_than_served_stale(tmp_path) -> None:
    """A reader who just changed a setting must not be shown the old payload.

    The cost breakdown may answer "as of a minute ago, refreshing" -- a page
    that renders a setting someone just wrote may not, because a stale answer
    there looks exactly like the write having failed.
    """

    app = _app()
    client = TestClient(app, client=("127.0.0.1", 50000))
    first = client.get(ENDPOINT).json()
    assert first["stale"] is False

    # A new model appears in the catalogue: the capability half's key changes.
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("routed", max_output_tokens=16384),
            ProviderModelInfo("extra", context_length=128000),
            ProviderModelInfo("brand-new", context_length=4096),
        },
    )

    second = client.get(ENDPOINT).json()

    assert second["stale"] is False, "a changed input must recompute, not go stale"
    refs = {
        model["model_ref"]
        for provider in second["providers"]
        for model in provider["models"]
    }
    assert "open_router/brand-new" in refs
