"""The prompt-cache lifetime override through the Models page routes (7.105.0).

The save route takes ``prompt_cache_ttl`` on a model row and on a provider row
exactly as it takes the other keys (inherit / ``null`` / a value), refuses a
value outside whole minutes 1-60 with a 422, and an untouched lifetime -- the
editor sends ``"inherit"`` for it -- leaves the override file byte for byte
what the same save wrote without the key. The page carries the control's
state and bounds per row, and beside the cached half what the row's default
line needs: the Model Config lifetimes, the setting each route reads, and the
gateways' Messages models.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import (
    PROMPT_CACHE_GATEWAY_LABEL,
    prompt_cache_gateway_models,
    prompt_cache_lifetimes_payload,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.model_overrides import reset_model_overrides_cache
from my_claude_code.config.model_refs import ConfiguredChatModelRef
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app, provider_manager_for_app

OVERRIDES = "/admin/api/model-admin/overrides"
PAGE = "/admin/api/model-admin"
REF = "open_router/extra"


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
    settings.model_opus = "open_router/extra"
    settings.prompt_cache_ttl_opus = "30"
    settings.open_router_api_key = "open-router-key"
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {ProviderModelInfo("routed"), ProviderModelInfo("extra")},
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


def _provider(page: dict[str, Any], provider_id: str) -> dict[str, Any]:
    return next(p for p in page["providers"] if p["provider_id"] == provider_id)


def _save(client: TestClient, updates: dict[str, Any], scope="model", key=REF):
    return client.post(OVERRIDES, json={"scope": scope, "key": key, "updates": updates})


def _file(home: Path) -> Path:
    return home / ".mcc" / "model_overrides.json"


def test_a_model_row_value_null_and_inherit_round_trip(home):
    client = _client(_app())

    saved = _save(client, {"prompt_cache_ttl": 10})
    assert saved.status_code == 200
    row = _row(saved.json(), REF)
    assert row["override"]["prompt_cache_ttl"] == {"state": "value", "value": 10}
    assert row["preferences"]["prompt_cache_ttl"]["state"] == "value"
    assert row["preferences"]["prompt_cache_ttl"]["minimum"] == 1
    assert row["preferences"]["prompt_cache_ttl"]["maximum"] == 60
    assert json.loads(_file(home).read_text("utf-8"))["models"][REF] == {
        "prompt_cache_ttl": 10
    }

    unset = _row(_save(client, {"prompt_cache_ttl": None}).json(), REF)
    assert unset["override"]["prompt_cache_ttl"] == {"state": "unset", "value": None}

    cleared = _row(_save(client, {"prompt_cache_ttl": "inherit"}).json(), REF)
    assert "prompt_cache_ttl" not in cleared["override"]
    assert json.loads(_file(home).read_text("utf-8"))["models"] == {}


def test_a_provider_row_takes_the_lifetime_too(home):
    client = _client(_app())

    saved = _save(client, {"prompt_cache_ttl": 45}, scope="provider", key="open_router")

    assert saved.status_code == 200
    provider = _provider(saved.json(), "open_router")
    assert provider["override"]["prompt_cache_ttl"] == {"state": "value", "value": 45}
    assert provider["preferences"]["prompt_cache_ttl"]["state"] == "value"
    assert json.loads(_file(home).read_text("utf-8"))["providers"]["open_router"] == {
        "prompt_cache_ttl": 45
    }


@pytest.mark.parametrize("value", [0, 61, -1, 1.5, "30", True, [10], {"m": 1}])
@pytest.mark.parametrize("scope", ["model", "provider"])
def test_a_bad_lifetime_is_refused_with_a_422_and_nothing_is_written(
    home, value: object, scope: str
):
    client = _client(_app())

    refused = _save(
        client,
        {"prompt_cache_ttl": value, "temperature": 0.2},
        scope=scope,
        key=REF if scope == "model" else "open_router",
    )

    assert refused.status_code == 422
    assert "prompt_cache_ttl" in refused.json()["detail"]
    assert not _file(home).exists()


def test_an_untouched_lifetime_leaves_the_file_byte_identical(home):
    """The editor sends every key; ``"inherit"`` for the new one writes nothing."""

    client = _client(_app())
    seed = {
        "providers": {"open_router": {"top_k": 40, "max_output_tokens": 8000}},
        "models": {
            REF: {"max_output_tokens": 65000, "temperature": 0.6},
            "open_router/routed": {"context_length": 1_000_000, "top_p": None},
        },
    }
    body_7_104 = {
        "max_output_tokens": 64000,
        "reasoning_preference": "high",
        "context_length": 200000,
        "frequency_penalty": "inherit",
        "temperature": 0.8,
        "top_p": None,
    }
    digests = []
    for updates in (body_7_104, {**body_7_104, "prompt_cache_ttl": "inherit"}):
        _file(home).parent.mkdir(parents=True, exist_ok=True)
        _file(home).write_bytes((json.dumps(seed, indent=2) + "\n").encode())
        reset_model_overrides_cache()
        assert _save(client, updates).status_code == 200
        digests.append(hashlib.sha256(_file(home).read_bytes()).hexdigest())

    assert digests[0] == digests[1]


def test_the_page_names_the_kind_and_the_tables_the_default_line_reads(home):
    page = _client(_app()).get(PAGE).json()

    assert page["overrides"]["preference_parameters"] == {
        "max_output_tokens": "integer",
        "prompt_cache_ttl": "minutes",
        "reasoning_preference": "enum",
    }
    table = page["prompt_cache_lifetimes"]
    assert table["override_key"] == "prompt_cache_ttl"
    assert table["settings"]["PROMPT_CACHE_TTL"] == "client"
    assert table["settings"]["PROMPT_CACHE_TTL_OPUS"] == "30"
    assert table["routes"]["open_router/extra"] == ["PROMPT_CACHE_TTL_OPUS"]
    assert table["routes"]["open_router/routed"] == ["PROMPT_CACHE_TTL"]
    assert table["providers"] == {
        "anthropic": "anthropic_messages",
        "anthropic_oauth": "anthropic_messages",
    }
    gateway = table["families"]["anthropic_messages_gateway"]
    assert gateway["lifetimes_known"] is False
    assert gateway["rows_only"] is True
    assert gateway["automatic_marker"] is False
    assert table["gateway_label"] == PROMPT_CACHE_GATEWAY_LABEL


def test_the_7_104_0_keys_of_the_table_are_unchanged():
    table = prompt_cache_lifetimes_payload()
    anthropic = table["families"]["anthropic_messages"]

    assert list(anthropic)[:5] == [
        "label",
        "wire_field",
        "lifetimes",
        "automatic_marker",
        "source",
    ]
    assert anthropic["lifetimes"] == [
        {"wire": "5m", "label": "5 min", "minutes": 5, "write_price": "1.25x"},
        {"wire": "1h", "label": "1 h", "minutes": 60, "write_price": "2x"},
    ]
    assert list(table)[:6] == [
        "families",
        "providers",
        "refused_fact_kind",
        "note",
        "unknown_label",
        "unknown_note",
    ]


def test_the_routes_map_every_tier_route_to_its_own_setting():
    configured = [
        ConfiguredChatModelRef("p/a", "p", "a", ("MODEL_HAIKU", "MODEL_FALLBACKS")),
        ConfiguredChatModelRef("p/b", "p", "b", ("MODEL_FABLE_FALLBACKS",)),
        ConfiguredChatModelRef("p/c", "p", "c", ("MODEL_VISION",)),
        ConfiguredChatModelRef("p/d", "p", "d", ("MODEL_MYTHOS", "MODEL_SONNET")),
    ]

    routes = prompt_cache_lifetimes_payload(configured=configured)["routes"]

    assert routes == {
        "p/a": ["PROMPT_CACHE_TTL_HAIKU", "PROMPT_CACHE_TTL"],
        "p/b": ["PROMPT_CACHE_TTL_FABLE"],
        "p/c": ["PROMPT_CACHE_TTL"],
        "p/d": ["PROMPT_CACHE_TTL_MYTHOS", "PROMPT_CACHE_TTL_SONNET"],
    }


def test_the_gateway_models_are_the_ones_the_messages_protocol_serves():
    page = {
        "providers": [
            {
                "provider_id": "commandcode",
                "models": [
                    {
                        "model_ref": "commandcode/claude-opus-5-5",
                        "model_id": "claude-opus-5-5",
                    },
                    {
                        "model_ref": "commandcode/deepseek/deepseek-v4-flash",
                        "model_id": "deepseek/deepseek-v4-flash",
                    },
                ],
            },
            {
                "provider_id": "opencode",
                "models": [
                    {
                        "model_ref": "opencode/claude-fable-5-1",
                        "model_id": "claude-fable-5-1",
                        "capabilities": {"response_surface": {"value": "messages"}},
                    },
                    {
                        "model_ref": "opencode/gpt-6",
                        "model_id": "gpt-6",
                        "capabilities": {"response_surface": {"value": "responses"}},
                    },
                ],
            },
            {
                "provider_id": "anthropic_oauth",
                "models": [
                    {
                        "model_ref": "anthropic_oauth/claude-opus-5-5",
                        "model_id": "claude-opus-5-5",
                    }
                ],
            },
            {
                "provider_id": "nvidia_nim",
                "models": [
                    {"model_ref": "nvidia_nim/claude-ish", "model_id": "claude-ish"}
                ],
            },
        ]
    }

    assert prompt_cache_gateway_models(page) == {
        "commandcode/claude-opus-5-5": "anthropic_messages_gateway",
        "opencode/claude-fable-5-1": "anthropic_messages_gateway",
    }
