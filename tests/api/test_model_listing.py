from fastapi.testclient import TestClient

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.settings import Settings
from my_claude_code.core.tier_refs import is_tier_ref
from tests.api.support import create_test_app, provider_manager_for_app


def _settings(
    *,
    model: str = "deepseek/deepseek-chat",
    model_fable: str | None = None,
    model_opus: str | None = "open_router/anthropic/claude-opus",
    model_haiku: str | None = "deepseek/deepseek-chat",
) -> Settings:
    return Settings.model_construct(
        model=model,
        model_fable=model_fable,
        model_opus=model_opus,
        model_sonnet=None,
        model_haiku=model_haiku,
        anthropic_auth_token="",
        deepseek_api_key="deepseek-key",
        open_router_api_key="open-router-key",
        wafer_api_key="wafer-key",
    )


def _cache_models(app, provider_id: str, *model_ids: str) -> None:
    provider_manager_for_app(app).cache_model_infos(
        provider_id,
        {ProviderModelInfo(model_id) for model_id in model_ids},
    )


def test_models_list_includes_configured_refs_cached_provider_models_and_aliases():
    app = create_test_app(_settings())
    _cache_models(app, "deepseek", "deepseek-chat")
    _cache_models(
        app,
        "open_router",
        "meta/llama-3.3",
        "anthropic/claude-opus",
    )

    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    data = response.json()
    ids = [item["id"] for item in data["data"]]
    assert ids[:6] == [
        "anthropic/deepseek/deepseek-chat",
        "claude-3-freecc-no-thinking/deepseek/deepseek-chat",
        "anthropic/open_router/anthropic/claude-opus",
        "claude-3-freecc-no-thinking/open_router/anthropic/claude-opus",
        "anthropic/open_router/meta/llama-3.3",
        "claude-3-freecc-no-thinking/open_router/meta/llama-3.3",
    ]
    assert ids.count("anthropic/deepseek/deepseek-chat") == 1
    assert ids.count("anthropic/open_router/anthropic/claude-opus") == 1
    display_names = {item["id"]: item["display_name"] for item in data["data"]}
    assert (
        display_names["anthropic/open_router/meta/llama-3.3"]
        == "open_router/meta/llama-3.3"
    )
    assert (
        display_names["claude-3-freecc-no-thinking/open_router/meta/llama-3.3"]
        == "open_router/meta/llama-3.3 (no thinking)"
    )
    assert "claude-sonnet-4-20250514" in ids
    assert "claude-fable-5" in ids
    assert data["first_id"] == ids[0]
    assert data["last_id"] == ids[-1]
    assert data["has_more"] is False


def test_models_list_uses_thinking_metadata_for_cached_models():
    app = create_test_app(_settings(model_opus=None))
    manager = provider_manager_for_app(app)
    _cache_models(app, "deepseek", "deepseek-chat")
    manager.cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("reasoning-model", supports_thinking=True),
            ProviderModelInfo("plain-model", supports_thinking=False),
        },
    )

    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert "anthropic/open_router/reasoning-model" in ids
    assert "claude-3-freecc-no-thinking/open_router/reasoning-model" in ids
    assert "anthropic/open_router/plain-model" not in ids
    assert "claude-3-freecc-no-thinking/open_router/plain-model" in ids


def test_models_list_uses_cached_metadata_for_configured_refs():
    app = create_test_app(
        _settings(
            model="open_router/plain-model",
            model_opus=None,
            model_haiku=None,
        )
    )
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {ProviderModelInfo("plain-model", supports_thinking=False)},
    )

    response = TestClient(app).get("/v1/models")

    ids = [item["id"] for item in response.json()["data"]]
    assert "anthropic/open_router/plain-model" not in ids
    assert ids[0] == "claude-3-freecc-no-thinking/open_router/plain-model"


def test_models_list_includes_cached_wafer_models():
    app = create_test_app(
        _settings(
            model="wafer/DeepSeek-V4-Pro",
            model_opus=None,
            model_haiku=None,
        )
    )
    _cache_models(app, "wafer", "DeepSeek-V4-Pro", "MiniMax-M2.7")

    response = TestClient(app).get("/v1/models")

    ids = [item["id"] for item in response.json()["data"]]
    assert "anthropic/wafer/DeepSeek-V4-Pro" in ids
    assert "claude-3-freecc-no-thinking/wafer/DeepSeek-V4-Pro" in ids
    assert "anthropic/wafer/MiniMax-M2.7" in ids
    assert "claude-3-freecc-no-thinking/wafer/MiniMax-M2.7" in ids


def test_models_list_works_with_empty_discovery_catalog():
    app = create_test_app(_settings())

    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert ids[:4] == [
        "anthropic/deepseek/deepseek-chat",
        "claude-3-freecc-no-thinking/deepseek/deepseek-chat",
        "anthropic/open_router/anthropic/claude-opus",
        "claude-3-freecc-no-thinking/open_router/anthropic/claude-opus",
    ]
    assert "claude-sonnet-4-20250514" in ids


def _visibility_settings(*, allow: str = "", deny: str = "") -> Settings:
    return Settings.model_construct(
        model="nvidia_nim/thinkingmachines/inkling",
        model_fable=None,
        model_opus=None,
        model_sonnet=None,
        model_haiku=None,
        anthropic_auth_token="",
        nvidia_nim_api_key="nim-key",
        nous_api_key="nous-key",
        model_visibility_allow=allow,
        model_visibility_deny=deny,
    )


def _listed_refs(app) -> set[str]:
    """The provider refs behind the listed ids.

    Both alias families are excluded. The eight Claude protocol names have no
    ``/`` at all; the five coding-agent tier names do, but they are protocol
    names for MCC's own routes in exactly the same sense and are exempt from
    visibility for exactly the same reason -- see
    ``test_the_tier_aliases_survive_every_filter`` below.
    """
    return {
        item["display_name"].removesuffix(" (no thinking)")
        for item in TestClient(app).get("/v1/models").json()["data"]
        if "/" in item["id"] and not is_tier_ref(item["id"])
    }


def _app_with_two_providers(settings: Settings):
    app = create_test_app(settings)
    _cache_models(app, "nvidia_nim", "thinkingmachines/inkling", "openai/gpt-oss")
    _cache_models(app, "nous_portal", "tencent/hy3:free", "tencent/hy3")
    return app


def test_models_list_lists_everything_when_no_visibility_pattern_is_set():
    app = _app_with_two_providers(_visibility_settings())

    assert _listed_refs(app) == {
        "nvidia_nim/thinkingmachines/inkling",
        "nvidia_nim/openai/gpt-oss",
        "nous_portal/tencent/hy3:free",
        "nous_portal/tencent/hy3",
    }


def test_models_list_hides_everything_a_non_empty_allow_does_not_match():
    app = _app_with_two_providers(_visibility_settings(allow="nvidia_nim/*"))

    assert _listed_refs(app) == {
        "nvidia_nim/thinkingmachines/inkling",
        "nvidia_nim/openai/gpt-oss",
    }


def test_models_list_applies_deny_after_allow():
    app = _app_with_two_providers(_visibility_settings(allow="*", deny="*:free"))

    assert _listed_refs(app) == {
        "nvidia_nim/thinkingmachines/inkling",
        "nvidia_nim/openai/gpt-oss",
        "nous_portal/tencent/hy3",
    }


def test_models_list_keeps_the_claude_aliases_whatever_the_filter_says():
    """The aliases are protocol names, not provider refs.

    Hiding them would not shrink a catalogue, it would stop Claude Code from
    being able to name a model at all.
    """
    app = _app_with_two_providers(_visibility_settings(allow="nothing-matches-this/*"))

    ids = [item["id"] for item in TestClient(app).get("/v1/models").json()["data"]]
    assert _listed_refs(app) == set()
    assert "claude-sonnet-4-20250514" in ids
    assert "claude-fable-5" in ids


def test_the_tier_aliases_survive_every_filter_in_both_spellings():
    """The five coding-agent tiers are protocol names too.

    Filtering one would not hide a model: it would remove the id an agent's
    generated config already names, and that agent's next session would open on
    a model the gateway says does not exist. Both spellings are listed because
    Pi's bundled extension only accepts the gateway form while OpenCode and
    Codex send the bare one.
    """
    app = _app_with_two_providers(_visibility_settings(allow="nothing-matches-this/*"))

    ids = [item["id"] for item in TestClient(app).get("/v1/models").json()["data"]]
    for ref in ("mcc/best", "mcc/good", "mcc/medium", "mcc/cheap", "mcc/vision"):
        assert ref in ids
        assert f"anthropic/{ref}" in ids


def test_models_list_hides_a_configured_model_that_is_denied():
    """Hidden means hidden even for a model this proxy is configured to use.

    Routing is unaffected -- see
    tests/application/test_routing_chains.py::test_visibility_patterns_never
    _change_a_resolved_route -- so the entry is invisible but alive.
    """
    app = _app_with_two_providers(_visibility_settings(deny="*/thinkingmachines/*"))

    assert "nvidia_nim/thinkingmachines/inkling" not in _listed_refs(app)
