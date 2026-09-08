"""Replay a recorded upstream SSE stream through a real provider.

The chunk-shape change (6.63.0) replaced the OpenAI SDK's per-chunk pydantic
model with MCC's own object. The only proof that matters for a change like
that is that the *downstream* bytes do not move, so this drives the real
``stream_response`` over a real ``AsyncOpenAI`` client whose transport hands
back recorded upstream bytes, and returns the Anthropic SSE frames verbatim.

Kept out of a ``test_`` module on purpose: the same file is copied into a
detached worktree at the previous release to record the expected frames, so
the two sides of the comparison run identical replay code.
"""

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.cloudflare import CloudflareProvider
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.google_openai import GoogleOpenAIProvider
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.open_router import OpenRouterProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OpenAIChatProvider,
    create_openai_chat_provider,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter

FIXTURES_PATH = Path(__file__).with_name("sse_chunk_fixtures.json")
EXPECTED_PATH = Path(__file__).with_name("sse_chunk_expected.json")


def load_fixtures() -> list[dict[str, Any]]:
    """Read the recorded upstream streams."""
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def _config() -> ProviderConfig:
    # Holdback off: the commit holdback buffers early frames on a wall clock,
    # and a byte comparison must not depend on how fast the machine is.
    return ProviderConfig(
        api_key="test-key",
        base_url="http://sse.invalid/v1",
        rate_limit=1_000_000,
        rate_window=60,
        commit_holdback_seconds=0.0,
        commit_holdback_chars=0,
    )


def _build_provider(name: str) -> OpenAIChatProvider:
    config = _config()
    limiter = passthrough_rate_limiter()
    if name == "cloudflare":
        return CloudflareProvider(config, account_id="acct", rate_limiter=limiter)
    if name == "deepseek":
        return DeepSeekProvider(config, rate_limiter=limiter)
    if name == "mistral":
        return MistralProvider(config, rate_limiter=limiter)
    if name == "open_router":
        return OpenRouterProvider(config, rate_limiter=limiter)
    if name == "google_openai":
        return GoogleOpenAIProvider(
            config,
            profile=GENERIC_OPENAI_PROFILE,
            rate_limiter=limiter,
            provider_id="google_openai",
        )
    return create_openai_chat_provider(name, config, limiter)


def _mock_client(provider: OpenAIChatProvider, body: bytes) -> Any:
    from openai import AsyncOpenAI

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
            request=request,
        )

    return AsyncOpenAI(
        api_key="test-key",
        base_url=provider._config.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class _FixedUUID:
    """Deterministic ``uuid4`` so message and tool ids compare byte for byte."""

    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> uuid.UUID:
        self._next += 1
        return uuid.UUID(int=self._next)


async def replay(fixture: dict[str, Any]) -> list[str]:
    """Return the Anthropic SSE frames one recorded stream produces.

    A stream that fails is recorded as a final ``!error`` entry naming the
    exception type, so a fixture may pin a failure just as exactly as it pins
    a success.
    """
    provider = _build_provider(fixture["provider"])
    provider._client = _mock_client(provider, fixture["sse"].encode("utf-8"))
    request = make_messages_request(**fixture.get("request", {}))
    frames: list[str] = []
    try:
        with patch("uuid.uuid4", _FixedUUID()):
            # Stepped one frame at a time rather than looped, so a stream that
            # fails keeps the frames it had already emitted: where a failure
            # lands is as much of the contract as the frames themselves.
            stream = provider.stream_response(request, 11, request_id="req-fixed")
            while True:
                try:
                    frames.append(await anext(stream))
                except StopAsyncIteration:
                    break
                except Exception as error:
                    frames.append(f"!error {type(error).__name__}")
                    break
    finally:
        await provider.cleanup()
    return frames


async def replay_all() -> dict[str, list[str]]:
    """Replay every fixture, in file order."""
    return {fixture["name"]: await replay(fixture) for fixture in load_fixtures()}
