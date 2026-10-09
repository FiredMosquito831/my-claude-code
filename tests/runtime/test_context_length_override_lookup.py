"""Routing's context-window lookup reads the operator's window first (7.87.0).

``ModelRouter`` (frozen) bounds the output budget by
``services.requests.model_context_length``; the override enters there, so the
router itself is unchanged. The catalogue's ladder lookup
(``model_context_length_tiered``) keeps saying only what the ladder says.
"""

import pytest

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime import ProviderRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager


class _FakeRuntime(ProviderRuntime):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_cached(self, provider_id: str) -> bool:
        return False

    def resolve_provider(self, provider_id: str) -> BaseProvider:
        raise AssertionError("a metadata lookup must not resolve a provider")

    async def cleanup(self) -> None:
        return None


def _settings() -> Settings:
    settings = Settings()
    settings.open_router_api_key = "open-router-key"
    return settings


TABLE = ModelParameterOverrides.from_document(
    {
        "models": {
            "open_router/agnes-3.0-flash": {"context_length": 1_000_000},
            "open_router/forced-unknown": {"context_length": None},
            "open_router/only-sampling": {"temperature": 0.3},
        }
    }
)


@pytest.mark.asyncio
async def test_the_operator_window_answers_before_the_cached_record() -> None:
    manager = ProviderRuntimeManager(
        _settings(), runtime_factory=_FakeRuntime, model_overrides=lambda: TABLE
    )
    try:
        manager.cache_model_infos(
            "open_router",
            (
                ProviderModelInfo("agnes-3.0-flash", context_length=524_288),
                ProviderModelInfo("forced-unknown", context_length=262_144),
                ProviderModelInfo("only-sampling", context_length=131_072),
                ProviderModelInfo("untouched", context_length=65_536),
            ),
        )

        lookup = manager.model_context_length
        assert lookup("open_router", "agnes-3.0-flash") == 1_000_000
        assert lookup("open_router", "forced-unknown") is None
        assert lookup("open_router", "only-sampling") == 131_072
        assert lookup("open_router", "untouched") == 65_536
        # The ladder's own answer, which the page shows beside the override.
        assert (
            manager.model_context_length_tiered("open_router", "agnes-3.0-flash")[0]
            == 524_288
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_with_no_override_file_the_lookup_is_the_cached_record() -> None:
    manager = ProviderRuntimeManager(_settings(), runtime_factory=_FakeRuntime)
    try:
        manager.cache_model_infos(
            "open_router",
            (ProviderModelInfo("agnes-3.0-flash", context_length=524_288),),
        )

        assert manager.model_context_length("open_router", "agnes-3.0-flash") == (
            524_288
        )
    finally:
        await manager.close()
