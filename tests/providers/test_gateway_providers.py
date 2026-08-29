"""Tests for the OpenRouter-dialect gateways (Nous Portal, Kilo) and Cline.

The model-list payloads below are trimmed copies of the real responses from
``inference-api.nousresearch.com/v1/models`` and ``api.kilo.ai/api/gateway/models``,
so the tool-capability filtering is exercised against the shape actually served.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.config.provider_catalog import (
    CLINE_DEFAULT_BASE,
    KILO_DEFAULT_BASE,
    NOUS_PORTAL_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.kilo import KiloProvider
from my_claude_code.providers.nous_portal import NousPortalProvider
from my_claude_code.providers.open_router import OpenRouterProvider
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, profiled_provider

# Trimmed from the live Nous Research /models response.
_NOUS_MODELS = SimpleNamespace(
    data=[
        {
            "id": "deepseek/deepseek-v4-flash-0731",
            "supported_parameters": ["max_tokens", "tools", "tool_choice", "reasoning"],
        },
        {
            "id": "nousresearch/hermes-4-405b",
            # Real catalog entry: reasoning but no tool support, so it must not
            # reach Claude Code's model picker.
            "supported_parameters": ["max_tokens", "reasoning", "temperature"],
        },
        {"id": "some/plain-model", "supported_parameters": ["tools"]},
    ]
)

# Trimmed from the live Kilo AI Gateway /models response.
_KILO_MODELS = SimpleNamespace(
    data=[
        {
            "id": "kilo-auto/balanced",
            "supported_parameters": ["max_tokens", "tools", "reasoning"],
        },
        {"id": "no-tools/model", "supported_parameters": ["max_tokens"]},
    ]
)


def _config(base_url: str) -> ProviderConfig:
    return ProviderConfig(
        api_key="test_key", base_url=base_url, rate_limit=10, rate_window=60
    )


@pytest.fixture
def nous_provider():
    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        return NousPortalProvider(
            _config(NOUS_PORTAL_DEFAULT_BASE),
            rate_limiter=passthrough_rate_limiter(),
        )


@pytest.fixture
def kilo_provider():
    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        return KiloProvider(
            _config(KILO_DEFAULT_BASE), rate_limiter=passthrough_rate_limiter()
        )


@pytest.fixture
def cline_provider():
    return profiled_provider(
        "cline", _config(CLINE_DEFAULT_BASE), rate_limiter=passthrough_rate_limiter()
    )


def test_default_base_url_constants():
    """Base URLs match the endpoints verified against the live services."""
    assert NOUS_PORTAL_DEFAULT_BASE == "https://inference-api.nousresearch.com/v1"
    assert KILO_DEFAULT_BASE == "https://api.kilo.ai/api/gateway"
    assert CLINE_DEFAULT_BASE == "https://api.cline.bot/api/v1"


@pytest.mark.parametrize(
    ("provider_id", "credential_env", "credential_attr", "proxy_attr"),
    [
        ("nous_portal", "NOUS_API_KEY", "nous_api_key", "nous_proxy"),
        ("kilo", "KILO_API_KEY", "kilo_api_key", "kilo_proxy"),
        ("cline", "CLINE_API_KEY", "cline_api_key", "cline_proxy"),
    ],
)
def test_catalog_descriptors(provider_id, credential_env, credential_attr, proxy_attr):
    descriptor = PROVIDER_CATALOG[provider_id]

    assert descriptor.credential_env == credential_env
    assert descriptor.credential_attr == credential_attr
    assert descriptor.proxy_attr == proxy_attr
    assert descriptor.dynamic is False
    assert descriptor.local is False
    assert descriptor.credential_url


def test_settings_expose_new_credentials():
    """Each descriptor's credential/proxy attribute must exist on Settings."""
    from my_claude_code.config.settings import Settings

    for provider_id in ("nous_portal", "kilo", "cline"):
        descriptor = PROVIDER_CATALOG[provider_id]
        assert descriptor.credential_attr in Settings.model_fields
        assert descriptor.proxy_attr in Settings.model_fields


@pytest.mark.asyncio
async def test_nous_lists_only_tool_capable_models(nous_provider):
    """Hermes advertises reasoning but not tools, so it must be filtered out."""
    nous_provider._client.models.list = AsyncMock(return_value=_NOUS_MODELS)

    assert await nous_provider.list_model_ids() == frozenset(
        {"deepseek/deepseek-v4-flash-0731", "some/plain-model"}
    )


@pytest.mark.asyncio
async def test_nous_reports_thinking_capability(nous_provider):
    nous_provider._client.models.list = AsyncMock(return_value=_NOUS_MODELS)

    thinking = {
        info.model_id: info.supports_thinking
        for info in await nous_provider.list_model_infos()
    }

    assert thinking == {
        "deepseek/deepseek-v4-flash-0731": True,
        "some/plain-model": False,
    }


@pytest.mark.asyncio
async def test_kilo_lists_only_tool_capable_models(kilo_provider):
    kilo_provider._client.models.list = AsyncMock(return_value=_KILO_MODELS)

    assert await kilo_provider.list_model_ids() == frozenset({"kilo-auto/balanced"})


@pytest.mark.parametrize("fixture_name", ["nous_provider", "kilo_provider"])
def test_gateways_share_the_openrouter_reasoning_profile(fixture_name, request):
    """Both gateways negotiate reasoning the way OpenRouter does."""
    provider = request.getfixturevalue(fixture_name)
    policy = provider._profile.request_policy

    assert policy.reasoning_replay is ReasoningReplayMode.REASONING_CONTENT
    assert policy.include_extra_body is True


def test_openrouter_dialect_is_shared_not_copied():
    """The refactor must leave OpenRouter on the same shared implementation."""
    from my_claude_code.providers.openrouter_gateway import OpenRouterGatewayProvider

    assert issubclass(OpenRouterProvider, OpenRouterGatewayProvider)
    assert issubclass(NousPortalProvider, OpenRouterGatewayProvider)
    assert issubclass(KiloProvider, OpenRouterGatewayProvider)


def test_provider_names_are_distinct():
    """Log lines and error messages must not conflate the three gateways."""
    with patch("my_claude_code.providers.openai_chat.provider.AsyncOpenAI"):
        names = {
            OpenRouterProvider(
                _config("https://openrouter.ai/api/v1"),
                rate_limiter=passthrough_rate_limiter(),
            )._provider_name,
            NousPortalProvider(
                _config(NOUS_PORTAL_DEFAULT_BASE),
                rate_limiter=passthrough_rate_limiter(),
            )._provider_name,
            KiloProvider(
                _config(KILO_DEFAULT_BASE), rate_limiter=passthrough_rate_limiter()
            )._provider_name,
        }

    assert names == {"OPENROUTER", "NOUS_PORTAL", "KILO"}


def test_cline_builds_request_body(cline_provider):
    body = cline_provider._build_request_body(
        make_messages_request("anthropic/claude-sonnet-4-6")
    )

    assert body["model"] == "anthropic/claude-sonnet-4-6"
    assert body["messages"][0]["role"] == "system"
    assert "max_tokens" in body


def test_cline_does_not_send_unverified_reasoning_fields(cline_provider):
    """Cline's catalog needs auth, so no reasoning parameter is assumed."""
    body = cline_provider._build_request_body(
        make_messages_request("anthropic/claude-sonnet-4-6")
    )

    assert "reasoning" not in body
    assert "reasoning_effort" not in body
