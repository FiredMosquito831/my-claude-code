"""Tests for the Together AI OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import TOGETHER_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def together_provider():
    return profiled_provider(
        "together",
        ProviderConfig(
            api_key="test-together-key",
            base_url=TOGETHER_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(together_provider):
    assert isinstance(together_provider, OpenAIChatProvider)
    assert together_provider._api_key == "test-together-key"
    assert together_provider._base_url == TOGETHER_DEFAULT_BASE
    assert together_provider._provider_name == "TOGETHER"


def test_build_request_body_openai_chat(together_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "zai-org/GLM-5.2",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = together_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "zai-org/GLM-5.2"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert TOGETHER_DEFAULT_BASE == "https://api.together.ai/v1"


def test_together_profile_retains_non_chat_rows_with_declared_type():
    from my_claude_code.providers.model_listing import extract_openai_model_infos
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

    listing = OPENAI_CHAT_PROFILES["together"].model_listing
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(
            [
                {"id": "zai-org/GLM-5.2", "type": "chat"},
                {"id": "black-forest-labs/FLUX.2-pro", "type": "image"},
                {"id": "BAAI/bge-large-en-v1.5", "type": "embedding"},
            ],
            provider_name="TOGETHER",
            collection_field=listing.collection_field,
            required_path_values=listing.required_path_values,
        )
    }
    assert set(infos) == {
        "zai-org/GLM-5.2",
        "black-forest-labs/FLUX.2-pro",
        "BAAI/bge-large-en-v1.5",
    }
    assert infos["black-forest-labs/FLUX.2-pro"].declared is not None
    assert infos["black-forest-labs/FLUX.2-pro"].declared.model_type == "image"
    assert infos["BAAI/bge-large-en-v1.5"].declared is not None
    assert infos["BAAI/bge-large-en-v1.5"].declared.model_type == "embedding"
