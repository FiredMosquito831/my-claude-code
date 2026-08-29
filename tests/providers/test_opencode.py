"""Tests for the OpenCode OpenAI-compatible provider."""

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from tests.providers.support import (
    passthrough_rate_limiter,
    profiled_provider,
    reasoning_for,
)


def test_build_request_body_replays_reasoning_content_verbatim() -> None:
    """2026-08-29: OpenCode replays on ``reasoning_content``, not ``<think>``.

    This test previously asserted the field was dropped, which was a property of
    the THINK_TAGS replay mode the profile used to carry, not of OpenCode. The
    gateway streams reasoning as ``reasoning_content`` deltas, so history is now
    replayed through the same field it arrived on -- probed as accepted, HTTP
    200. An empty value is carried through rather than synthesised away: the
    converter deliberately preserves it (see
    ``test_convert_assistant_empty_top_level_reasoning_content_is_preserved``)
    because for the OpenAI-dialect providers that mandate the field on tool
    turns, present-and-empty and absent mean different things.
    """
    provider = profiled_provider(
        "opencode",
        ProviderConfig(
            api_key="test_opencode_key",
            base_url="https://example.invalid/v1",
            rate_limit=1,
            rate_window=1,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": "visible",
                    "reasoning_content": "",
                }
            ],
            "thinking": {"type": "enabled"},
        }
    )

    body = provider._build_request_body(request, reasoning=reasoning_for(request))

    assert body["messages"][0] == {
        "role": "assistant",
        "content": "visible",
        "reasoning_content": "",
    }
    assert "<think>" not in body["messages"][0]["content"]
