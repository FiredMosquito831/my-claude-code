"""Tests for the ZenMux OpenAI-chat provider profile."""

import pytest

from my_claude_code.config.provider_catalog import ZENMUX_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def zenmux_provider():
    return profiled_provider(
        "zenmux",
        ProviderConfig(
            api_key="test-zenmux-key",
            base_url=ZENMUX_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(zenmux_provider):
    assert isinstance(zenmux_provider, OpenAIChatProvider)
    assert zenmux_provider._api_key == "test-zenmux-key"
    assert zenmux_provider._base_url == ZENMUX_DEFAULT_BASE
    assert zenmux_provider._provider_name == "ZENMUX"


def test_build_request_body_openai_chat(zenmux_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "test-model",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = zenmux_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "test-model"
    assert body["max_completion_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert ZENMUX_DEFAULT_BASE == "https://zenmux.ai/api/v1"


def test_zenmux_profile_retains_media_rows():
    from my_claude_code.providers.model_listing import extract_openai_model_infos
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

    listing = OPENAI_CHAT_PROFILES["zenmux"].model_listing
    infos = {
        info.model_id: info
        for info in extract_openai_model_infos(
            {
                "data": [
                    {
                        "id": "zenmux/chat-1",
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "capabilities": {"reasoning": True},
                    },
                    {
                        "id": "zenmux/tts-1",
                        "input_modalities": ["text"],
                        "output_modalities": ["audio"],
                    },
                ]
            },
            provider_name="ZENMUX",
            required_sequence_items=listing.required_sequence_items,
            thinking_boolean_path=listing.thinking_boolean_path,
        )
    }
    assert set(infos) == {"zenmux/chat-1", "zenmux/tts-1"}
    assert infos["zenmux/tts-1"].declared is not None
    assert infos["zenmux/tts-1"].declared.modalities is not None
    assert infos["zenmux/tts-1"].declared.modalities.outputs == ("audio",)
