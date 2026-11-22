"""Tests for the Nebius Token Factory OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import NEBIUS_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def nebius_provider():
    return profiled_provider(
        "nebius",
        ProviderConfig(
            api_key="test-nebius-key",
            base_url=NEBIUS_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(nebius_provider):
    assert isinstance(nebius_provider, OpenAIChatProvider)
    assert nebius_provider._api_key == "test-nebius-key"
    assert nebius_provider._base_url == NEBIUS_DEFAULT_BASE
    assert nebius_provider._provider_name == "NEBIUS"


def test_build_request_body_openai_chat(nebius_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "Qwen/Qwen3-30B-A3B",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = nebius_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "Qwen/Qwen3-30B-A3B"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert NEBIUS_DEFAULT_BASE == "https://api.tokenfactory.nebius.com/v1"


def test_nebius_profile_retains_multimodal_and_non_chat_rows():
    from my_claude_code.providers.model_listing import extract_openai_model_infos
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

    listing = OPENAI_CHAT_PROFILES["nebius"].model_listing
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(
            {
                "data": [
                    {
                        "id": "Qwen/Qwen3-VL-72B",
                        "architecture": {"modality": "text+image->text"},
                    },
                    {
                        "id": "black-forest-labs/flux-dev",
                        "architecture": {"modality": "text->image"},
                    },
                ]
            },
            provider_name="NEBIUS",
            required_path_values=listing.required_path_values,
        )
    }
    assert set(infos) == {"Qwen/Qwen3-VL-72B", "black-forest-labs/flux-dev"}
    assert infos["Qwen/Qwen3-VL-72B"].supports_vision is True
    assert infos["black-forest-labs/flux-dev"].declared is not None
    assert infos["black-forest-labs/flux-dev"].declared.modalities is not None
    assert infos["black-forest-labs/flux-dev"].declared.modalities.outputs == ("image",)
