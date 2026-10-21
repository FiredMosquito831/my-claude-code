"""Every model door stamps the operator's prompt-cache lifetime, and only then (7.104.0).

The lifetime is decided by the route (the tier) and adapted by the host, so
each of the four doors stamps its canonical request before the plan is built,
and the provider of each attempt receives the stamp on its own routed copy.
Nothing set stamps nothing.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import PromptCacheAsk
from my_claude_code.core.anthropic.streaming import format_sse_event
from tests.api.support import create_test_app

_MODEL = "nvidia_nim/test-model"

_STREAM = [
    format_sse_event("message_start", {"type": "message_start", "message": {}}),
    format_sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ),
    format_sse_event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        },
    ),
    format_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
    format_sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
    ),
    format_sse_event("message_stop", {"type": "message_stop"}),
]


class _Recorder:
    def __init__(self) -> None:
        self.preflight_stream = MagicMock()
        self.requests: list[Any] = []

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data: Any, **_kwargs: Any):
        self.requests.append(request_data)
        for chunk in _STREAM:
            yield chunk


DOORS: dict[str, tuple[str, dict[str, Any]]] = {
    "messages": (
        "/v1/messages",
        {
            "model": _MODEL,
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    ),
    "chat_completions": (
        "/v1/chat/completions",
        {
            "model": _MODEL,
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        },
    ),
    "responses": ("/v1/responses", {"model": _MODEL, "input": "Hello", "stream": True}),
    "gemini": (
        f"/v1beta/models/{_MODEL}:streamGenerateContent?alt=sse",
        {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}]},
    ),
}


def _routed(
    settings: Settings, door: str, payload: dict[str, Any] | None = None
) -> Any:
    provider = _Recorder()
    path, default = DOORS[door]
    # Both resolvers: the Gemini door has its own, and an unpatched one builds
    # a real provider client.
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(create_test_app(settings)) as client,
    ):
        response = client.post(path, json=payload or default)
        assert response.status_code == 200, response.text
    assert len(provider.requests) == 1
    return provider.requests[0]


@pytest.mark.parametrize("door", sorted(DOORS))
def test_a_setting_stamps_the_request_each_door_routes(door: str) -> None:
    settings = Settings.model_validate({"PROMPT_CACHE_TTL": "30"})

    routed = _routed(settings, door)

    assert routed.prompt_cache_ask == PromptCacheAsk(30, "PROMPT_CACHE_TTL")


@pytest.mark.parametrize("door", sorted(DOORS))
def test_nothing_set_stamps_nothing_at_any_door(door: str) -> None:
    routed = _routed(Settings.model_validate({}), door)

    assert routed.prompt_cache_ask is None


def test_the_tier_setting_reaches_a_claude_named_request() -> None:
    settings = Settings.model_validate(
        {"PROMPT_CACHE_TTL": "40", "PROMPT_CACHE_TTL_OPUS": "3", "MODEL_OPUS": _MODEL}
    )
    payload = {**DOORS["messages"][1], "model": "claude-opus-5-5"}

    routed = _routed(settings, "messages", payload)

    assert routed.prompt_cache_ask == PromptCacheAsk(3, "PROMPT_CACHE_TTL_OPUS")
