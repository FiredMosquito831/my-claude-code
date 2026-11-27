"""Tests for the xAI OpenAI-chat provider profile."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from my_claude_code.application.model_kinds import resolve_model_kind
from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.config.provider_catalog import XAI_DEFAULT_BASE
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
def xai_provider():
    return profiled_provider(
        "xai",
        ProviderConfig(
            api_key="test-xai-key",
            base_url=XAI_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def test_init_uses_documented_endpoint(xai_provider):
    assert isinstance(xai_provider, OpenAIChatProvider)
    assert xai_provider._api_key == "test-xai-key"
    assert xai_provider._base_url == XAI_DEFAULT_BASE
    assert xai_provider._provider_name == "XAI"


def test_build_request_body_openai_chat(xai_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "grok-4.5",
            "max_tokens": 100,
            "messages": [Message(role="user", content="Hello")],
            "thinking": {"type": "enabled"},
        }
    )

    body = xai_provider._build_request_body(request, reasoning=reasoning_for(request))

    assert body["model"] == "grok-4.5"
    assert body["max_tokens"] == 100
    assert body["messages"] == [{"role": "user", "content": "Hello"}]


def test_default_base_url_constant():
    assert XAI_DEFAULT_BASE == "https://api.x.ai/v1"


@pytest.mark.asyncio
async def test_xai_discovers_image_and_video_models_and_isolates_from_chat(
    xai_provider,
):
    seen_paths: list[str] = []

    async def fake_get(path, *, cast_to=object, options=None):
        del cast_to, options
        seen_paths.append(path)
        if path == "/language-models":
            return SimpleNamespace(
                models=[SimpleNamespace(id="grok-4.5", aliases=["grok-4.5-latest"])]
            )
        if path == "/image-generation-models":
            return SimpleNamespace(
                models=[
                    SimpleNamespace(
                        id="grok-imagine-image",
                        aliases=["grok-2-image"],
                    )
                ]
            )
        if path == "/video-generation-models":
            return SimpleNamespace(
                models=[SimpleNamespace(id="grok-imagine-video", aliases=[])]
            )
        raise AssertionError(f"Unexpected path: {path}")

    xai_provider._client.get = AsyncMock(side_effect=fake_get)
    infos = await xai_provider.list_model_infos()
    by_id = {info.model_id: info for info in infos}

    assert seen_paths == [
        "/language-models",
        "/image-generation-models",
        "/video-generation-models",
    ]
    assert set(by_id) == {
        "grok-4.5",
        "grok-4.5-latest",
        "grok-imagine-image",
        "grok-2-image",
        "grok-imagine-video",
    }
    img = by_id["grok-imagine-image"]
    assert img.declared is not None
    assert img.declared.modalities == DeclaredModalities(
        inputs=("text",), outputs=("image",)
    )
    assert img.declared.model_type == "image_generation"
    assert by_id["grok-2-image"].declared == img.declared

    vid = by_id["grok-imagine-video"]
    assert vid.declared is not None
    assert vid.declared.modalities == DeclaredModalities(
        inputs=("text",), outputs=("video",)
    )
    assert vid.declared.model_type == "video_generation"

    def modalities(provider_id: str, model_id: str):
        if provider_id != "xai" or model_id not in by_id:
            return None, None
        decl = by_id[model_id].declared
        if decl is None or decl.modalities is None:
            return None, None
        return decl.modalities, ResolutionTier.PROVIDER_EXACT

    for media_id, expected_rail in (
        ("grok-imagine-image", "image"),
        ("grok-imagine-video", "video"),
    ):
        kind = resolve_model_kind(
            f"xai/{media_id}", modalities=modalities, placements={}
        )
        assert kind.known and not kind.chat_listable
        assert kind.offered_on(expected_rail)


@pytest.mark.asyncio
async def test_xai_supplementary_404_does_not_fail_primary_listing(xai_provider):
    async def fake_get(path, *, cast_to=object, options=None):
        del cast_to, options
        if path == "/language-models":
            return SimpleNamespace(models=[SimpleNamespace(id="grok-4.5", aliases=[])])
        raise RuntimeError("404 Not Found")

    xai_provider._client.get = AsyncMock(side_effect=fake_get)
    infos = await xai_provider.list_model_infos()
    assert {info.model_id for info in infos} == {"grok-4.5"}
