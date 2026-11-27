"""Tests for the SiliconFlow OpenAI-chat provider profile."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from my_claude_code.application.model_kinds import CHAT_KIND, resolve_model_kind
from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.config.provider_catalog import SILICONFLOW_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


@pytest.fixture
def siliconflow_provider():
    return profiled_provider(
        "siliconflow",
        ProviderConfig(
            api_key="test-siliconflow-key",
            base_url=SILICONFLOW_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(siliconflow_provider):
    assert isinstance(siliconflow_provider, OpenAIChatProvider)
    assert siliconflow_provider._api_key == "test-siliconflow-key"
    assert siliconflow_provider._base_url == SILICONFLOW_DEFAULT_BASE
    assert siliconflow_provider._provider_name == "SILICONFLOW"


def test_build_request_body_openai_chat(siliconflow_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "Qwen/Qwen3-32B",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = siliconflow_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["model"] == "Qwen/Qwen3-32B"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert SILICONFLOW_DEFAULT_BASE == "https://api.siliconflow.com/v1"


@pytest.mark.asyncio
async def test_siliconflow_discovers_media_models_and_isolates_from_chat(
    siliconflow_provider,
):
    seen_queries: list[dict[str, str]] = []

    async def fake_get(path, *, cast_to=object, options=None):
        del cast_to
        params = dict((options or {}).get("params") or {})
        seen_queries.append(params)
        sub_type = params.get("sub_type")
        if sub_type == "chat":
            return SimpleNamespace(data=[SimpleNamespace(id="Qwen/Qwen3-32B")])
        if sub_type == "text-to-image":
            return SimpleNamespace(
                data=[SimpleNamespace(id="black-forest-labs/FLUX.1-schnell")]
            )
        if sub_type == "audio":
            return SimpleNamespace(
                data=[SimpleNamespace(id="FunAudioLLM/CosyVoice2-0.5B")]
            )
        raise AssertionError(f"Unexpected query: {params}")

    siliconflow_provider._client.get = AsyncMock(side_effect=fake_get)

    infos = await siliconflow_provider.list_model_infos()
    by_id = {info.model_id: info for info in infos}

    assert seen_queries == [
        {"sub_type": "chat"},
        {"sub_type": "text-to-image"},
        {"sub_type": "audio"},
    ]
    assert set(by_id) == {
        "Qwen/Qwen3-32B",
        "black-forest-labs/FLUX.1-schnell",
        "FunAudioLLM/CosyVoice2-0.5B",
    }
    flux = by_id["black-forest-labs/FLUX.1-schnell"]
    assert flux.declared is not None
    assert flux.declared.modalities == DeclaredModalities(
        inputs=("text",), outputs=("image",)
    )
    assert flux.declared.model_type == "text-to-image"

    audio = by_id["FunAudioLLM/CosyVoice2-0.5B"]
    assert audio.declared is not None
    assert audio.declared.model_type == "audio"

    def declared_at(provider_id: str, model_id: str):
        if provider_id != "siliconflow" or model_id not in by_id:
            return None
        return by_id[model_id].declared, ResolutionTier.PROVIDER_EXACT

    def modalities(provider_id: str, model_id: str):
        found = declared_at(provider_id, model_id)
        if found is None or found[0] is None or found[0].modalities is None:
            return None, None
        return found[0].modalities, found[1]

    def kind_words(provider_id: str, model_id: str):
        found = declared_at(provider_id, model_id)
        if found is None or found[0] is None or found[0].model_type is None:
            return None, None
        return (found[0].model_type,), found[1]

    flux_kind = resolve_model_kind(
        "siliconflow/black-forest-labs/FLUX.1-schnell",
        modalities=modalities,
        placements={},
        kind_words=kind_words,
    )
    assert flux_kind.known and not flux_kind.chat_listable
    assert flux_kind.offered_on("image")

    audio_kind = resolve_model_kind(
        "siliconflow/FunAudioLLM/CosyVoice2-0.5B",
        modalities=modalities,
        placements={},
        kind_words=kind_words,
    )
    assert audio_kind.known and not audio_kind.chat_listable
    assert CHAT_KIND not in (audio_kind.kinds or frozenset())
    assert audio_kind.offered_on("tts") and audio_kind.offered_on("asr")


@pytest.mark.asyncio
async def test_siliconflow_supplementary_failures_tolerate_gracefully(
    siliconflow_provider,
):
    async def fake_get(path, *, cast_to=object, options=None):
        del path, cast_to
        sub_type = ((options or {}).get("params") or {}).get("sub_type")
        if sub_type == "chat":
            return SimpleNamespace(data=[SimpleNamespace(id="Qwen/Qwen3-32B")])
        raise RuntimeError("404 Not Found")

    siliconflow_provider._client.get = AsyncMock(side_effect=fake_get)
    infos = await siliconflow_provider.list_model_infos()
    assert {info.model_id for info in infos} == {"Qwen/Qwen3-32B"}
