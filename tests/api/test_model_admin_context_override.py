"""The context window is overridable per model from the Models page (7.87.0).

End to end through the save route: the page row shows the operator's number
badged "operator override" with the extracted number and its rung beside it,
routing's lookup and ``/admin/api/catalogue-models`` read the same number, and
only that model moves. A 0, a negative, a fraction or a provider row is refused.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import with_operator_context_length
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.model_overrides import (
    StatedContextLength,
    reset_model_overrides_cache,
)
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, provider_manager_for_app

OVERRIDES = "/admin/api/model-admin/overrides"
EXTRA = "open_router/extra"


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("MODEL", "MODEL_VISIBILITY_ALLOW", "MODEL_VISIBILITY_DENY"):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()
    return tmp_path


def _app():
    settings = Settings()
    settings.model = "open_router/routed"
    settings.open_router_api_key = "open-router-key"
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("routed", max_output_tokens=16384),
            ProviderModelInfo("extra", context_length=524_288),
        },
    )
    return app


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _row(page: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(
        model
        for provider in page["providers"]
        for model in provider["models"]
        if model["model_ref"] == ref
    )


def _catalogue(client: TestClient) -> dict[str, dict[str, Any]]:
    body = client.get("/admin/api/catalogue-models?provenance=1").json()
    return {model["gateway_id"]: model for model in body["models"]}


def _save(client: TestClient, value: object, scope: str = "model", key: str = EXTRA):
    return client.post(
        OVERRIDES,
        json={"scope": scope, "key": key, "updates": {"context_length": value}},
    )


def test_a_forced_window_is_used_everywhere_with_the_extracted_one_beside_it(home):
    app = _app()
    client = _client(app)
    manager = provider_manager_for_app(app)
    before = _catalogue(client)

    saved = _save(client, 1_000_000)

    assert saved.status_code == 200
    row = _row(saved.json(), EXTRA)
    field = row["capabilities"]["context_length"]
    assert field["value"] == 1_000_000
    assert field["source"] == "operator"
    assert field["source_label"] == "operator override"
    assert field["also_stated"]["value"] == 524_288
    assert field["also_stated"]["source_label"] == "provider /models or models.dev"
    assert row["override"]["context_length"] == {"state": "value", "value": 1_000_000}
    # Routing's lookup and the agent catalogues read the same number.
    assert manager.model_context_length("open_router", "extra") == 1_000_000
    assert manager.model_context_length("open_router", "routed") is None
    after = _catalogue(client)
    assert after["anthropic/open_router/extra"]["context_length"] == 1_000_000
    assert (
        after["anthropic/open_router/extra"]["provenance"]["context_length"]["source"]
        == "operator"
    )
    assert (
        after["anthropic/open_router/routed"] == before["anthropic/open_router/routed"]
    )
    on_disk = json.loads((home / ".mcc" / "model_overrides.json").read_text("utf-8"))
    assert on_disk["models"][EXTRA] == {"context_length": 1_000_000}


def test_force_unset_reads_unknown_everywhere(home):
    app = _app()
    client = _client(app)

    row = _row(_save(client, None).json(), EXTRA)

    field = row["capabilities"]["context_length"]
    assert field["value"] is None
    assert field["source"] == "operator"
    assert field["also_stated"]["value"] == 524_288
    assert (
        provider_manager_for_app(app).model_context_length("open_router", "extra")
        is None
    )
    assert _catalogue(client)["anthropic/open_router/extra"]["context_length"] is None


def test_inherit_puts_the_extracted_window_back(home):
    app = _app()
    client = _client(app)
    _save(client, 1_000_000)

    row = _row(_save(client, "inherit").json(), EXTRA)

    assert row["capabilities"]["context_length"]["value"] == 524_288
    assert "context_length" not in row["override"]
    assert (
        provider_manager_for_app(app).model_context_length("open_router", "extra")
        == 524_288
    )


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "1000000"])
def test_an_unusable_window_is_refused_and_nothing_is_written(home, value):
    client = _client(_app())

    refused = _save(client, value)

    assert refused.status_code == 422
    assert "context_length" in refused.json()["detail"]
    assert not (home / ".mcc" / "model_overrides.json").exists()


def test_a_provider_row_window_is_refused(home):
    client = _client(_app())

    refused = _save(client, 1_000_000, scope="provider", key="open_router")

    assert refused.status_code == 422
    assert "per model" in refused.json()["detail"]


def test_nothing_stated_returns_the_row_unchanged():
    payload = {"context_length": {"value": 524_288, "source": "provider"}}

    assert with_operator_context_length(payload, None) == payload
    unknown = with_operator_context_length(
        {"context_length": {"value": None, "source": "unknown"}},
        StatedContextLength(4096),
    )["context_length"]
    assert unknown["value"] == 4096
    assert "also_stated" not in unknown


def test_the_everything_known_view_names_the_override_as_the_used_value(home):
    """7.86.0's view reads the page row, so its used cell is the operator's."""

    client = _client(_app())
    _save(client, 1_000_000)

    payload = client.get("/admin/api/models/knowledge", params={"ref": EXTRA}).json()

    field = next(item for item in payload["fields"] if item["key"] == "context_length")
    assert field["used"]["source"] == "operator"
    assert field["used_value"] == 1_000_000
    assert field["used_source"] == "operator"
    operator = [item for item in field["statements"] if item["source"] == "operator"]
    assert [(item["value"], item["used"]) for item in operator] == [(1_000_000, True)]
    # The extracted window stays beside it, as on the page.
    assert field["used"]["also_stated"]["value"] == 524_288
