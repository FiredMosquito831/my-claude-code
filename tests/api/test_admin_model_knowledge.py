"""``GET /admin/api/models/knowledge``: everything known about one model (7.86.0).

The Models page's "Everything known" disclosure loads this for one model when
it is opened: every source's statement of every field, beside the value the
ladder used, and every source's own row verbatim. These hold the route's
contract -- loopback admin only, computed off the event loop, a validated ref,
404 for a ref nothing lists -- and the view's: each ``used`` cell IS the cell
the Models page draws for that row, every source states its own value, the
rows are scrubbed, and none of it reaches the page payload, ``/v1/models`` or
the page's cache key. Every key and ref here is fake.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_routes
from my_claude_code.api.models_page_cache import capability_half_key
from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ProviderModelInfo,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.providers.chatgpt_oauth.codex_catalogue import (
    CodexCatalogue,
    CodexCatalogueEntry,
)
from my_claude_code.providers.model_listing import (
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
)
from my_claude_code.providers.runtime.litellm_prices import (
    litellm_cache_path,
    write_litellm_cache,
)
from my_claude_code.providers.runtime.models_dev import (
    enrich_model_infos,
    read_models_dev_cache,
    write_models_dev_cache,
)
from my_claude_code.providers.runtime.openrouter_catalogue import (
    openrouter_live_cache_path,
    write_openrouter_live_cache,
)
from my_claude_code.runtime.catalogue_store import (
    catalogue_cache,
    catalogue_scope_key,
    store_provider_rows,
)
from tests.api.support import create_test_app, provider_manager_for_app

#: A field only the raw row carries, to find it anywhere it must not be.
RAW_ONLY = "kinv-raw-only-marker"

NOVITA_ROW: dict[str, Any] = {
    "id": "acme/bucketed",
    "description": "Novita's own words.",
    "context_size": 128000,
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "tiered_billing_configs": [{"marker": RAW_ONLY}],
    "secret_looking": "sk-" + "Q" * 48,
}
NOUS_ROW: dict[str, Any] = {
    "id": "acme/copied",
    "architecture": {
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tokenizer": RAW_ONLY,
    },
    "top_provider": {"context_length": 64000, "is_moderated": False},
    "supported_parameters": ["tools"],
}
LIVE_ROWS: list[dict[str, Any]] = [
    {
        "id": "acme/bucketed",
        "description": "OpenRouter's words.",
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "context_length": 131072,
        "supported_parameters": ["tools", "reasoning"],
        "created": 1767225600,
    },
    {
        "id": "acme/copied",
        "description": "OpenRouter's words about the copy.",
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["tools"],
    },
]
MODELS_DEV: dict[str, Any] = {
    "novita-ai": {
        "id": "novita-ai",
        "models": {
            "acme/bucketed": {
                "id": "acme/bucketed",
                "description": "The bucket's words.",
                "limit": {"context": 100000, "output": 8000},
                "cost": {"input": 1.0, "output": 2.0},
                "modalities": {"input": ["text"], "output": ["text"]},
                "tool_call": True,
                "reasoning": True,
            }
        },
    },
    "openrouter": {
        "id": "openrouter",
        "models": {
            "acme/copied": {
                "id": "acme/copied",
                "limit": {"context": 64000, "output": 4000},
                "modalities": {"input": ["text"], "output": ["text"]},
                "tool_call": False,
            }
        },
    },
}
KEYS = {
    "NOVITA_API_KEY": "sk_" + "3" * 40,
    "NOUS_API_KEY": "sk-" + "1" * 40,
}
CONFIGURED_ONLY = "novita/acme/configured-only"


def _app(monkeypatch, tmp_path: Path, **settings: Any):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    write_models_dev_cache(MODELS_DEV)
    write_openrouter_live_cache(LIVE_ROWS, openrouter_live_cache_path())
    app = create_test_app(
        Settings.model_validate(
            {**KEYS, "MODEL": "novita/acme/bucketed", "MODEL_HAIKU": CONFIGURED_ONLY}
            | settings
        )
    )
    cache = read_models_dev_cache()
    assert cache is not None
    novita = enrich_model_infos(
        sorted(
            extract_openai_model_infos({"data": [NOVITA_ROW]}, provider_name="novita"),
            key=lambda info: info.model_id,
        ),
        cache.index,
        "novita",
    )
    nous = enrich_model_infos(
        sorted(
            extract_openrouter_tool_model_infos(
                {"data": [NOUS_ROW]}, provider_name="nous_portal"
            ),
            key=lambda info: info.model_id,
        ),
        cache.index,
        "nous_portal",
    )
    manager = provider_manager_for_app(app)
    manager.cache_model_infos("novita", novita)
    manager.cache_model_infos("nous_portal", nous)
    return app


def _admin(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _knowledge(admin: TestClient, ref: str) -> dict[str, Any]:
    response = admin.get("/admin/api/models/knowledge", params={"ref": ref})
    assert response.status_code == 200, response.text
    return response.json()


def _field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return next(field for field in payload["fields"] if field["key"] == key)


def _said(field: dict[str, Any], source: str) -> dict[str, Any]:
    return next(s for s in field["statements"] if s["source"] == source)


def test_it_is_for_the_local_admin_only(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)
    remote = TestClient(app, client=("203.0.113.9", 50000))
    response = remote.get(
        "/admin/api/models/knowledge", params={"ref": "novita/acme/bucketed"}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "   ",
        "noslash",
        "/acme/model",
        "novita/",
        "novita/ ",
        "a/b\x01c",
        "a/" + "x" * 600,
    ],
)
def test_a_ref_must_be_one_provider_model_reference(monkeypatch, tmp_path, ref) -> None:
    admin = _admin(_app(monkeypatch, tmp_path))
    response = admin.get("/admin/api/models/knowledge", params={"ref": ref})
    assert response.status_code == 400
    assert "provider/model" in response.json()["detail"]


def test_a_ref_nothing_lists_is_not_found(monkeypatch, tmp_path) -> None:
    admin = _admin(_app(monkeypatch, tmp_path))
    response = admin.get(
        "/admin/api/models/knowledge", params={"ref": "novita/acme/nothing"}
    )
    assert response.status_code == 404
    # A configured route with no record is on the page, so it is known.
    configured = _knowledge(admin, CONFIGURED_ONLY)
    assert configured["has_record"] is False


def test_it_is_computed_off_the_event_loop(monkeypatch, tmp_path) -> None:
    admin = _admin(_app(monkeypatch, tmp_path))
    real = asyncio.to_thread
    ran: list[Any] = []

    async def spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        ran.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(admin_routes.asyncio, "to_thread", spy)
    _knowledge(admin, "novita/acme/bucketed")
    assert admin_routes._model_knowledge in ran


def test_every_used_cell_is_the_cell_the_page_draws(monkeypatch, tmp_path) -> None:
    """The self-consistency proof, through the two real routes."""

    admin = _admin(_app(monkeypatch, tmp_path))
    page = admin.get("/admin/api/model-admin").json()
    rows = [model for provider in page["providers"] for model in provider["models"]]
    assert len(rows) >= 3
    checked = 0
    for row in rows:
        payload = _knowledge(admin, row["model_ref"])
        assert payload["model_ref"] == row["model_ref"]
        for field in payload["fields"]:
            key = field["key"]
            if key == "kind":
                cell = row["kind"]
            elif key.startswith("reasoning."):
                cell = row["capabilities"]["reasoning"][key.removeprefix("reasoning.")]
            else:
                cell = row["capabilities"][key]
            assert field["used"] == cell, (row["model_ref"], key)
            marked = [s for s in field["statements"] if s["used"]]
            if field["used_source"] is not None:
                assert len(marked) == 1, (row["model_ref"], key)
                assert marked[0]["source"] == field["used_source"]
            checked += 1
    assert checked > 60


def test_every_source_states_its_own_value(monkeypatch, tmp_path) -> None:
    admin = _admin(_app(monkeypatch, tmp_path))
    payload = _knowledge(admin, "novita/acme/bucketed")
    assert payload["models_dev_has_bucket"] is True
    description = _field(payload, "description")
    # Provider first: its own words are used; OpenRouter's and the bucket's
    # differ and are marked so.
    assert description["used_source"] == "provider_list"
    assert _said(description, "provider_list")["value"] == "Novita's own words."
    assert _said(description, "provider_list")["used"] is True
    live = _said(description, "openrouter_live")
    assert (live["value"], live["agrees_with_used"]) == ("OpenRouter's words.", False)
    bucket = _said(description, "models_dev_bucket")
    assert bucket["value"] == "The bucket's words."
    assert bucket["consulted"] is True
    output = _field(payload, "max_output_tokens")
    assert output["used_source"] == "models_dev_bucket"
    assert _said(output, "models_dev_bucket")["value"] == 8000
    context = _field(payload, "context_length")
    assert _said(context, "provider_list")["value"] == 128000
    assert _said(context, "openrouter_live")["value"] == 131072
    assert {source["id"] for source in payload["sources"]} >= {
        "provider_list",
        "openrouter_live",
        "models_dev_bucket",
        "models_dev_vote",
        "litellm",
        "learned",
        "operator",
        "vendor_client",
    }


def test_without_a_bucket_the_copy_is_read_and_marked_so(monkeypatch, tmp_path) -> None:
    admin = _admin(_app(monkeypatch, tmp_path))
    payload = _knowledge(admin, "nous_portal/acme/copied")
    assert payload["models_dev_has_bucket"] is False
    tools = _field(payload, "supports_tool_calls")
    assert tools["used_source"] == "provider_list"
    copy = _said(tools, "models_dev_openrouter")
    assert (copy["value"], copy["consulted"], copy["agrees_with_used"]) == (
        False,
        True,
        False,
    )


def test_each_sources_row_is_verbatim_and_scrubbed(monkeypatch, tmp_path) -> None:
    admin = _admin(_app(monkeypatch, tmp_path))
    payload = _knowledge(admin, "novita/acme/bucketed")
    rows = {row["source"]: row for row in payload["rows"]}
    provider = rows["provider_list"]
    assert provider["row"]["tiered_billing_configs"] == [{"marker": RAW_ONLY}]
    assert provider["row"]["secret_looking"] == "<redacted>"
    assert provider["match"].startswith("exact id")
    assert rows["openrouter_live"]["row"]["description"] == "OpenRouter's words."
    assert rows["models_dev_bucket"]["key"] == "novita-ai/acme/bucketed"
    assert "sk-" + "Q" * 48 not in json.dumps(payload)


def test_no_row_reaches_the_page_v1_models_or_the_page_key(
    monkeypatch, tmp_path
) -> None:
    app = _app(monkeypatch, tmp_path)
    admin = _admin(app)
    page_text = admin.get("/admin/api/model-admin").text
    assert RAW_ONLY not in page_text
    assert "published_row" not in page_text
    v1_text = TestClient(app).get("/v1/models").text
    assert RAW_ONLY not in v1_text
    infos = provider_manager_for_app(app).cached_prefixed_model_infos()
    assert any(info.published_row for info in infos)
    bare = tuple(replace(info, published_row=None) for info in infos)
    args = ((), ModelVisibility(), ModelParameterOverrides())
    assert capability_half_key(infos, *args) == capability_half_key(bare, *args)


def test_after_a_restart_the_stored_row_answers(monkeypatch, tmp_path) -> None:
    app = _app(monkeypatch, tmp_path)
    manager = provider_manager_for_app(app)
    with_rows = manager._model_cache.cached_model_infos_by_provider()
    store_provider_rows(
        with_rows,
        catalogue_scope_key(manager._model_cache.cached_scope()),
        computed_at=7.0,
        cache=catalogue_cache(),
    )
    # What a restart restores: the same records, without their rows.
    manager.cache_model_infos(
        "novita",
        tuple(replace(info, published_row=None) for info in with_rows["novita"]),
    )
    payload = _knowledge(_admin(app), "novita/acme/bucketed")
    provider = next(row for row in payload["rows"] if row["source"] == "provider_list")
    assert provider["row"]["description"] == "Novita's own words."
    assert "stored beside the catalogue" in provider["match"]


def test_the_vendor_client_row_for_a_model_its_catalogue_lists(
    monkeypatch, tmp_path
) -> None:
    app = _app(monkeypatch, tmp_path)
    provider_manager_for_app(app).cache_model_infos(
        "novita",
        (
            ProviderModelInfo(
                "acme/vendor",
                listing=ModelListingEvidence(
                    provenance=ModelListingProvenance.VENDOR_CLIENT,
                    retirement_at="2027-01-01T00:00:00Z",
                ),
            ),
        ),
    )
    raw = {
        "slug": "acme/vendor",
        "context_window": 272000,
        "max_context_window": 1000000,
        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
    }
    monkeypatch.setattr(
        admin_routes,
        "load_codex_catalogue",
        lambda: CodexCatalogue(
            version="9.9.9",
            source_path="codex",
            entries=(CodexCatalogueEntry(slug="acme/vendor", raw=raw),),
        ),
    )
    payload = _knowledge(_admin(app), "novita/acme/vendor")
    vendor = _said(_field(payload, "context_length"), "vendor_client")
    assert (vendor["value"], vendor["rung"]) == (272000, "Codex CLI 9.9.9")
    retires = _field(payload, "retires_at")
    assert retires["used_source"] == "vendor_client"
    efforts = _field(payload, "reasoning.supported_efforts")
    assert _said(efforts, "vendor_client")["value"] == ["high", "low"]
    row = next(r for r in payload["rows"] if r["source"] == "vendor_client")
    assert row["row"]["max_context_window"] == 1000000


def test_litellm_is_shown_only_while_its_pricing_is_on(monkeypatch, tmp_path) -> None:
    def lite_app(enabled: bool):
        app = _app(
            monkeypatch,
            tmp_path,
            COST_SOURCE_LITELLM_ENABLED="true" if enabled else "false",
        )
        write_litellm_cache(
            {
                "novita/acme/bucketed": {
                    "litellm_provider": "novita",
                    "max_input_tokens": 120000,
                    "input_cost_per_token": 3e-07,
                    "supports_function_calling": True,
                    "deprecation_date": "2027-06-30",
                }
            },
            litellm_cache_path(),
        )
        return app

    off = _knowledge(_admin(lite_app(False)), "novita/acme/bucketed")
    assert not [r for r in off["rows"] if r["source"] == "litellm"]
    on = _knowledge(_admin(lite_app(True)), "novita/acme/bucketed")
    price = _said(_field(on, "input_price"), "litellm")
    assert price["value"] == 0.3
    assert _said(_field(on, "context_length"), "litellm")["value"] == 120000
    assert _said(_field(on, "retires_at"), "litellm")["value"] == "2027-06-30"
    assert [r["key"] for r in on["rows"] if r["source"] == "litellm"] == [
        "novita/acme/bucketed"
    ]
