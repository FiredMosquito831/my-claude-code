"""The marketplace profile row through the Models page routes (7.109.0).

The save route takes ``marketplace_profile`` on InferHub's provider row and
on its model rows (a name / ``null`` / inherit), refuses a name with no
profile behind it with a 422, and an untouched row -- the editor sends
``"inherit"`` -- leaves the override file byte for byte what the same save
wrote without the key (E6). The page carries the rows beside the cached half
as ``marketplaces`` (E4): with nothing set, the page minus that one key is
the page the release before served.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.marketplace import market_snapshot_cache
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    parse_profile,
    reset_marketplace_profiles_cache,
    save_marketplace_profiles,
)
from my_claude_code.config.marketplaces import INFERHUB_MARKETPLACE, snapshot_key
from my_claude_code.config.model_overrides import reset_model_overrides_cache
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, provider_manager_for_app

OVERRIDES = "/admin/api/model-admin/overrides"
PAGE = "/admin/api/model-admin"
REF = "inferhub/cc/claude-opus-4-6"


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in ("MODEL", "MODEL_VISIBILITY_ALLOW", "MODEL_VISIBILITY_DENY"):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()
    reset_marketplace_profiles_cache()
    return tmp_path


def _app():
    settings = Settings()
    settings.model = "inferhub/cc/claude-opus-4-6"
    settings.inferhub_api_key = "sk-airo-" + "x" * 24
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "inferhub",
        {ProviderModelInfo("cc/claude-opus-4-6"), ProviderModelInfo("cx/gpt-6-astra")},
    )
    return app


def _client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _save(client: TestClient, updates: dict[str, Any], scope="model", key=REF):
    return client.post(OVERRIDES, json={"scope": scope, "key": key, "updates": updates})


def _file(home: Path) -> Path:
    return home / ".mcc" / "model_overrides.json"


def _profile() -> None:
    profile, problem = parse_profile(
        "opus-cap", {"kind": "pct_of_official", "percent": 10}
    )
    assert profile is not None, problem
    save_marketplace_profiles(MarketplaceProfiles(profiles={"opus-cap": profile}))


def test_a_name_null_and_inherit_round_trip_on_both_rows(home):
    _profile()
    client = _client(_app())
    assert (
        _save(
            client, {"marketplace_profile": "opus-cap"}, "provider", "inferhub"
        ).status_code
        == 200
    )
    assert _save(client, {"marketplace_profile": None}).status_code == 200
    stored = json.loads(_file(home).read_text(encoding="utf-8"))
    assert stored["providers"]["inferhub"] == {"marketplace_profile": "opus-cap"}
    assert stored["models"][REF] == {"marketplace_profile": None}
    assert _save(client, {"marketplace_profile": "inherit"}).status_code == 200
    stored = json.loads(_file(home).read_text(encoding="utf-8"))
    assert REF not in stored["models"]


@pytest.mark.parametrize("value", ["missing", "Bad Name", 5])
def test_a_name_with_no_profile_behind_it_is_refused(home, value):
    _profile()
    response = _save(_client(_app()), {"marketplace_profile": value})
    assert response.status_code == 422
    assert "marketplace_profile" in response.json()["detail"]


def test_an_untouched_row_leaves_the_file_byte_identical(home):
    client = _client(_app())
    seed = {
        "providers": {"inferhub": {"top_k": 40}},
        "models": {REF: {"max_output_tokens": 65000, "temperature": 0.6}},
    }
    body = {
        "max_output_tokens": 64000,
        "reasoning_preference": "high",
        "context_length": 200000,
        "temperature": 0.8,
        "top_p": None,
        "prompt_cache_ttl": "inherit",
    }
    digests = []
    for updates in (body, {**body, "marketplace_profile": "inherit"}):
        _file(home).parent.mkdir(parents=True, exist_ok=True)
        _file(home).write_bytes((json.dumps(seed, indent=2) + "\n").encode())
        reset_model_overrides_cache()
        assert _save(client, updates).status_code == 200
        digests.append(hashlib.sha256(_file(home).read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_the_page_carries_the_rows_beside_the_cached_half(home):
    _profile()
    assert market_snapshot_cache().write(
        INFERHUB_MARKETPLACE.snapshot_entry,
        key=snapshot_key(INFERHUB_MARKETPLACE),
        payload={
            "fetched_at": "2026-10-24T01:07:35Z",
            "models": {
                "cc/claude-opus-4-6": {
                    "official": [5.0, 25.0],
                    "levels_in": [[0.75, 38], [2.5, 3]],
                    "levels_out": [[3.75, 38], [12.5, 3]],
                }
            },
            "notes": {"cc": "A one-line Claude Code client header is added."},
        },
        computed_at=0.0,
    )
    page = _client(_app()).get(PAGE).json()
    market = page["marketplaces"]
    assert market["override_key"] == "marketplace_profile"
    assert market["providers"] == {"inferhub": "inferhub_bid_headers"}
    assert market["profiles"][0]["name"] == "opus-cap"
    assert "No ceiling" in market["dialects"]["inferhub_bid_headers"]["warning"]
    row = market["models"][REF]
    assert row["offers"] == (
        "cheapest $0.75 / $3.75 per Mtok · 38 of 41 accounts at it · 2 levels · "
        "official $5 / $25 · snapshot 2026-10-24 01:07Z"
    )
    assert row["route_note"] == "A one-line Claude Code client header is added."
    assert market["models"]["inferhub/cx/gpt-6-astra"] == {}
    # Nothing about it is inside the cached half.
    assert "marketplace" not in json.dumps(page["providers"])
