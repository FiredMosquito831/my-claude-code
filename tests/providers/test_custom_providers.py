"""Custom providers: settings validation, routing, factory, discovery, cache."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.provider_registry import (
    ProviderRegistry,
    get_provider_registry,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OpenAIChatProvider,
    create_openai_chat_provider,
)
from my_claude_code.providers.runtime import ProviderRuntime, models_dev
from my_claude_code.providers.runtime.config import build_provider_config
from my_claude_code.providers.runtime.discovery import (
    ProviderModelDiscovery,
    model_cache_provider_ids_for_settings,
    model_list_provider_ids_for_settings,
)
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.rotating import RotatingProvider
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.providers.support import passthrough_rate_limiter


@pytest.fixture
def custom_registry(tmp_path) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        "Acme AI",
        "https://api.acme.test/v1",
        ("sk-acme-1", "sk-acme-2"),
        credential_rotation="round_robin",
        proxy="http://proxy.test:8080",
    )
    return registry


@pytest.fixture(autouse=True)
def _patch_registry_singleton(monkeypatch, custom_registry):
    monkeypatch.setattr(
        "my_claude_code.config.provider_registry._registry", custom_registry
    )


@pytest.fixture
def make_settings(monkeypatch):
    def _make(**env: str) -> Settings:
        monkeypatch.setenv("MODEL", env.pop("MODEL", "nvidia_nim/test-model"))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings()

    return _make


def test_settings_validator_accepts_custom_provider_ref(make_settings) -> None:
    settings = make_settings(MODEL="custom_acme_ai/some-model")

    assert settings.model == "custom_acme_ai/some-model"


def test_settings_validator_rejects_unknown_custom_ref(make_settings) -> None:
    with pytest.raises(ValueError, match="custom_missing"):
        make_settings(MODEL="custom_missing/model")


def test_settings_validator_rejects_disabled_custom_ref(
    custom_registry: ProviderRegistry, make_settings
) -> None:
    custom_registry.update("custom_acme_ai", enabled=False)

    with pytest.raises(ValueError, match="custom_acme_ai"):
        make_settings(MODEL="custom_acme_ai/some-model")


def test_routing_resolves_direct_custom_provider_model(make_settings) -> None:
    router = ModelRouter(make_settings())

    resolved = router.resolve("custom_acme_ai/llama-3.3-70b")

    assert resolved.provider_id == "custom_acme_ai"
    assert resolved.provider_model == "llama-3.3-70b"


def test_routing_resolves_mapped_custom_provider_ref(make_settings) -> None:
    router = ModelRouter(make_settings(MODEL="custom_acme_ai/llama-3.3-70b"))

    resolved = router.resolve("claude-sonnet-4-5")

    assert resolved.provider_id == "custom_acme_ai"
    assert resolved.provider_model == "llama-3.3-70b"


def test_build_provider_config_reads_registry_entry(make_settings) -> None:
    descriptor = get_provider_registry().all_descriptors()["custom_acme_ai"]

    config = build_provider_config(descriptor, make_settings())

    assert config.api_key == "sk-acme-1"
    assert config.api_keys == ("sk-acme-1", "sk-acme-2")
    assert config.base_url == "https://api.acme.test/v1"
    assert config.proxy == "http://proxy.test:8080"
    assert config.credential_rotation == "round_robin"


def test_factory_builds_dynamic_provider_with_generic_profile(make_settings) -> None:
    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        provider = create_provider("custom_acme_ai", make_settings())

    assert isinstance(provider, RotatingProvider)
    assert len(provider._providers) == 2
    sub = provider._providers[0]
    assert isinstance(sub, OpenAIChatProvider)
    assert sub._profile is GENERIC_OPENAI_PROFILE
    assert sub._api_key == "sk-acme-1"
    assert sub._base_url == "https://api.acme.test/v1"


def test_factory_single_key_dynamic_provider_is_not_rotated(
    custom_registry: ProviderRegistry, make_settings
) -> None:
    custom_registry.update("custom_acme_ai", api_keys=("sk-only",))
    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        provider = create_provider("custom_acme_ai", make_settings())

    assert isinstance(provider, OpenAIChatProvider)
    assert provider._api_key == "sk-only"


def test_factory_unknown_provider_error_lists_custom_ids(make_settings) -> None:
    from my_claude_code.application.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError, match="custom_acme_ai"):
        create_provider("custom_missing", make_settings())


def test_discovery_includes_enabled_custom_providers_with_keys(make_settings) -> None:
    ids = model_cache_provider_ids_for_settings(make_settings())

    assert "custom_acme_ai" in ids
    list_ids = model_list_provider_ids_for_settings(make_settings())
    assert "custom_acme_ai" in list_ids


def test_discovery_excludes_custom_providers_without_keys(
    custom_registry: ProviderRegistry, make_settings
) -> None:
    custom_registry.update("custom_acme_ai", api_keys=())

    ids = model_cache_provider_ids_for_settings(make_settings())

    assert "custom_acme_ai" not in ids


def test_model_cache_accepts_custom_provider_ids() -> None:
    cache = ProviderModelCache()
    cache.cache_model_ids("custom_acme_ai", ["llama-3.3-70b"])

    refs = cache.cached_prefixed_model_refs()

    assert refs == ("custom_acme_ai/llama-3.3-70b",)


def test_model_cache_propagates_enriched_metadata() -> None:
    from my_claude_code.application.model_metadata import ProviderModelInfo

    cache = ProviderModelCache()
    cache.cache_model_infos(
        "custom_acme_ai",
        [
            ProviderModelInfo(
                model_id="llama-3.3-70b",
                context_length=131072,
                input_price=0.1,
                output_price=0.2,
            )
        ],
    )

    infos = cache.cached_prefixed_model_infos()

    assert infos[0].context_length == 131072
    assert infos[0].input_price == 0.1
    assert infos[0].output_price == 0.2


def test_generic_profile_builds_plain_openai_body() -> None:
    from tests.providers.request_factory import make_messages_request

    config = ProviderConfig(
        api_key="sk-x",
        base_url="https://api.acme.test/v1",
    )
    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        provider = create_openai_chat_provider(
            "custom_acme_ai",
            config,
            passthrough_rate_limiter(),
            profile=GENERIC_OPENAI_PROFILE,
        )

    body = provider._build_request_body(make_messages_request("some-model"))

    assert body["model"] == "some-model"
    assert "max_tokens" in body


@pytest.mark.asyncio
async def test_test_provider_caches_models_dev_enriched_infos(monkeypatch) -> None:
    """The Refresh models button and background discovery share one seam.

    They used to disagree: discovery cached models.dev-enriched infos, the
    button cached raw ones, so the catalogue's contents depended on which one
    filled it last.
    """
    upstream = MagicMock()
    upstream.list_model_infos = AsyncMock(
        return_value=frozenset({ProviderModelInfo("m1")})
    )
    upstream.cleanup = AsyncMock()
    manager = ProviderRuntimeManager(
        Settings(),
        runtime_factory=lambda snapshot: ProviderRuntime(
            snapshot, {"custom_acme_ai": cast(BaseProvider, upstream)}
        ),
    )
    runtime = ApplicationRuntime(manager, transcriber=None)

    async def _enrich(model_infos, path=None, provider_id=None):
        assert provider_id == "custom_acme_ai"
        return (ProviderModelInfo("m1", supports_thinking=True),)

    monkeypatch.setattr(models_dev, "enrich_provider_model_infos", _enrich)

    result = await runtime.test_provider("custom_acme_ai")

    assert result == {
        "provider_id": "custom_acme_ai",
        "ok": True,
        "models": ["m1"],
    }
    assert manager.cached_model_supports_thinking("custom_acme_ai", "m1") is True
    await manager.close()


@pytest.mark.asyncio
async def test_discovery_failure_for_one_provider_does_not_evict_others() -> None:
    cache = ProviderModelCache(("custom_acme_ai", "nvidia_nim"))
    healthy = MagicMock()
    healthy.list_model_infos = AsyncMock(
        return_value=frozenset({ProviderModelInfo("nim-1")})
    )
    refusing = MagicMock()
    refusing.list_model_infos = AsyncMock(
        side_effect=PermissionError("upstream refused the key")
    )
    providers = {"nvidia_nim": healthy, "custom_acme_ai": refusing}
    discovery = ProviderModelDiscovery(
        Settings(),
        lambda provider_id: cast(BaseProvider, providers[provider_id]),
        cache,
    )

    assert (await discovery.refresh_provider("nvidia_nim")).refreshed_provider_ids == (
        "nvidia_nim",
    )
    failed = await discovery.refresh_provider("custom_acme_ai")

    assert failed.failed_provider_ids == ("custom_acme_ai",)
    failure = failed.failure_for("custom_acme_ai")
    assert failure is not None
    assert failure.error_type == "PermissionError"
    assert failure.message
    # The one that answered keeps its catalogue.
    assert cache.cached_model_ids() == {"nvidia_nim": frozenset({"nim-1"})}
