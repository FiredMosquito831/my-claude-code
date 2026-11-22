"""Tests for the Chutes OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import CHUTES_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def chutes_provider():
    return profiled_provider(
        "chutes",
        ProviderConfig(
            api_key="test-chutes-key",
            base_url=CHUTES_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(chutes_provider):
    assert isinstance(chutes_provider, OpenAIChatProvider)
    assert chutes_provider._api_key == "test-chutes-key"
    assert chutes_provider._base_url == CHUTES_DEFAULT_BASE
    assert chutes_provider._provider_name == "CHUTES"


def test_build_request_body_openai_chat(chutes_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "Qwen/Qwen3-32B-TEE",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = chutes_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "Qwen/Qwen3-32B-TEE"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert CHUTES_DEFAULT_BASE == "https://llm.chutes.ai/v1"


def test_chutes_profile_retains_non_tool_and_media_rows():
    from my_claude_code.providers.model_listing import extract_openai_model_infos
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

    listing = OPENAI_CHAT_PROFILES["chutes"].model_listing
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(
            {
                "data": [
                    {
                        "id": "Qwen/Qwen3-32B-TEE",
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "supported_features": ["tools", "reasoning"],
                    },
                    {
                        "id": "chutes/flux-2",
                        "input_modalities": ["text"],
                        "output_modalities": ["image"],
                        "supported_features": [],
                    },
                ]
            },
            provider_name="CHUTES",
            required_sequence_items=listing.required_sequence_items,
            exclude_missing_sequence_fields=listing.exclude_missing_sequence_fields,
            tags_field=listing.tags_field,
        )
    }
    assert set(infos) == {"Qwen/Qwen3-32B-TEE", "chutes/flux-2"}
    assert infos["chutes/flux-2"].declared is not None
    assert infos["chutes/flux-2"].declared.modalities is not None
    assert infos["chutes/flux-2"].declared.modalities.outputs == ("image",)
