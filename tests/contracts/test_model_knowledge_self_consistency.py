""" "Everything known" never disagrees with the Models page (7.86.0).

The knowledge view's ``used`` cell for every field is the cell the Models page
draws for the same row -- the same value, the same badge, the same rung, the
same second statement -- over every row the committed ladder fixtures produce
(``metadata_ladder_listing_rows.json`` through every listing dialect, the
trimmed models.dev, OpenRouter's live rows), with the live list absent, on and
switched off. Asked through the two real routes, in id order. And wherever the
page shows a value, the view names the one source whose statement it is,
except a wire surface MCC's own resolver chose (its badge already says so).

A failure here means the view would tell the operator one thing while the page
-- and the ladder behind it -- does another.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.providers.commandcode.models import extract_commandcode_model_infos
from my_claude_code.providers.model_listing import (
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
)
from my_claude_code.providers.openai_chat.profiles import (
    OPENAI_CHAT_PROFILES,
    OpenAIModelListing,
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
from tests.api.support import create_test_app, provider_manager_for_app

HERE = Path(__file__).parent
ROWS_PATH = HERE / "metadata_ladder_listing_rows.json"
MODELS_DEV_PATH = HERE / "metadata_ladder_models_dev.json"
LIVE_PATH = HERE / "metadata_ladder_openrouter_live.json"

#: Fake credentials of the right shape, so every fixture provider is in scope.
FAKE_KEYS: dict[str, str] = {
    "OPENROUTER_API_KEY": "sk-or-v1-" + "0" * 64,
    "NOUS_API_KEY": "sk-" + "1" * 40,
    "KILO_API_KEY": "kilo-" + "2" * 40,
    "NOVITA_API_KEY": "sk_" + "3" * 40,
    "AI_GATEWAY_API_KEY": "vck_" + "4" * 40,
    "COMMANDCODE_API_KEY": "user_" + "5" * 40,
    "NVIDIA_NIM_API_KEY": "nvapi-" + "6" * 40,
    "OPENCODE_API_KEY": "sk-" + "7" * 40,
    "HYPERCHARM_API_KEY": "hc-" + "8" * 40,
    "CLINE_API_KEY": "cline-" + "9" * 40,
}

#: A page cell whose value no single source states: a wire surface the
#: resolver chose from the registry, the profile or the default.
_RESOLVER_ONLY = frozenset({"response_surface"})


def _parse(provider_id: str, spec: dict[str, Any]) -> Any:
    payload = {spec["collection_field"]: spec["rows"]}
    if spec["dialect"] == "openrouter":
        return extract_openrouter_tool_model_infos(payload, provider_name=provider_id)
    if spec["dialect"] == "commandcode":
        return extract_commandcode_model_infos(payload, provider_name=provider_id)
    profile = OPENAI_CHAT_PROFILES.get(provider_id)
    listing = profile.model_listing if profile is not None else OpenAIModelListing()
    return extract_openai_model_infos(
        payload,
        provider_name=provider_id,
        collection_field=listing.collection_field,
        id_field=listing.id_field,
        aliases_field=listing.aliases_field,
        required_path_values=listing.required_path_values,
        required_null_field=listing.required_null_field,
        required_sequence_items=listing.required_sequence_items,
        exclude_missing_sequence_fields=listing.exclude_missing_sequence_fields,
        tags_field=listing.tags_field,
        thinking_tag=listing.thinking_tag,
        non_thinking_tag=listing.non_thinking_tag,
        thinking_boolean_path=listing.thinking_boolean_path,
    )


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live_mode: str | None):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    write_models_dev_cache(json.loads(MODELS_DEV_PATH.read_text(encoding="utf-8")))
    switch: dict[str, str] = {}
    if live_mode is not None:
        write_openrouter_live_cache(
            json.loads(LIVE_PATH.read_text(encoding="utf-8"))["data"],
            openrouter_live_cache_path(),
        )
        if live_mode == "off":
            switch["MODEL_METADATA_OPENROUTER_LIVE"] = "false"
    cached = read_models_dev_cache()
    assert cached is not None
    rows: dict[str, Any] = json.loads(ROWS_PATH.read_text(encoding="utf-8"))
    catalogues = {}
    for provider_id in sorted(rows):
        parsed = sorted(
            _parse(provider_id, rows[provider_id]), key=lambda i: i.model_id
        )
        catalogues[provider_id] = tuple(
            enrich_model_infos(parsed, cached.index, provider_id)
        )
    first = next(iter(catalogues["open_router"]))
    app = create_test_app(
        Settings.model_validate(
            {**FAKE_KEYS, **switch, "MODEL": f"open_router/{first.model_id}"}
        )
    )
    manager = provider_manager_for_app(app)
    for provider_id, infos in catalogues.items():
        manager.cache_model_infos(provider_id, infos)
    return app


def _cell(row: dict[str, Any], key: str) -> Any:
    if key == "kind":
        return row["kind"]
    if key.startswith("reasoning."):
        return row["capabilities"]["reasoning"][key.removeprefix("reasoning.")]
    return row["capabilities"][key]


@pytest.mark.parametrize("live_mode", [None, "on", "off"])
def test_every_used_cell_is_the_page_cell_for_every_row(
    monkeypatch, tmp_path, live_mode
) -> None:
    admin = TestClient(_app(monkeypatch, tmp_path, live_mode), client=("127.0.0.1", 1))
    page = admin.get("/admin/api/model-admin").json()
    rows = sorted(
        (model for provider in page["providers"] for model in provider["models"]),
        key=lambda model: model["model_ref"],
    )
    assert len(rows) >= 15
    mismatched: list[str] = []
    unattributed: list[str] = []
    fields_checked = 0
    for row in rows:
        response = admin.get(
            "/admin/api/models/knowledge", params={"ref": row["model_ref"]}
        )
        assert response.status_code == 200, (row["model_ref"], response.text)
        payload = response.json()
        listed = {field["key"] for field in payload["fields"]}
        for name, cell in row["capabilities"].items():
            if name not in ("reasoning", "reasoning_dialect") and cell is not None:
                assert name in listed, (row["model_ref"], name)
        for field in payload["fields"]:
            fields_checked += 1
            key = field["key"]
            if json.dumps(field["used"], sort_keys=True) != json.dumps(
                _cell(row, key), sort_keys=True
            ):
                mismatched.append(f"{row['model_ref']}: {key}")
            if (
                field["used_value"] is not None
                and field["used_source"] is None
                and key not in _RESOLVER_ONLY
            ):
                unattributed.append(f"{row['model_ref']}: {key}")
            marked = [s for s in field["statements"] if s["used"]]
            assert len(marked) == (0 if field["used_source"] is None else 1), (
                row["model_ref"],
                key,
            )
    assert not mismatched, mismatched
    assert not unattributed, unattributed
    assert fields_checked > 400
