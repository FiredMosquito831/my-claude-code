"""The capture point must sit after every mutation of the outbound body.

This is the regression guard for the whole feature. The original defect was a
capture taken at the *start* of the chain; recording the same value in a new
column would have been no better. So the test that matters is the one where the
routed value differs from the client's: a client asking for 64,000 tokens
against a model whose upstream cap is 40,960 must be recorded as 40,960.
"""

from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.config.provider_catalog import GROQ_DEFAULT_BASE
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, profiled_provider


@pytest.fixture
def groq_provider():
    return profiled_provider(
        "groq",
        ProviderConfig(
            api_key="test_groq_key",
            base_url=GROQ_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def _client_body(provider, **kwargs):
    return provider._build_request_body(
        make_messages_request(
            "llama-3.3-70b-versatile",
            max_tokens=64000,
            thinking={"enabled": False},
            **kwargs,
        )
    )


@pytest.mark.asyncio
async def test_the_recorded_max_tokens_is_the_wire_value_not_the_client_ask(
    groq_provider,
):
    body = _client_body(groq_provider)
    assert body["max_completion_tokens"] == 64000, "the client's ask"

    # The upstream cap this model actually enforces, learned from a previous
    # 400. ``_apply_learned_output_cap`` rewrites the body inside
    # ``_create_stream`` -- after the request builder, after routing, after
    # every postprocessor.
    groq_provider._model_output_caps[body["model"]] = 40960

    trace = install_wire_trace()
    with patch.object(
        groq_provider._client.chat.completions,
        "create",
        AsyncMock(return_value=object()),
    ):
        await groq_provider._create_stream(body)

    recorded = trace.requests[0]
    assert recorded.params["max_tokens"] == 40960
    assert recorded.params["max_tokens"] != 64000


@pytest.mark.asyncio
async def test_a_create_level_retry_records_the_body_that_actually_worked(
    groq_provider,
):
    """The first send is not the sent body when the first send was rejected."""

    class _BadRequest(Exception):
        def __init__(self, message):
            super().__init__(message)
            self.status_code = 400
            self.body = None

    body = _client_body(groq_provider)
    error = _BadRequest("max_completion_tokens must be less than or equal to 8192")
    create = AsyncMock(side_effect=[error, object()])

    trace = install_wire_trace()
    with patch.object(groq_provider._client.chat.completions, "create", create):
        await groq_provider._create_stream(body)

    assert create.call_count == 2
    assert trace.requests[0].params["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_the_recorded_tool_count_is_the_encoded_wire_tools(groq_provider):
    body = _client_body(groq_provider)
    body["tools"] = [
        {"type": "function", "function": {"name": "Read", "parameters": {}}},
        {"type": "function", "function": {"name": "Write", "parameters": {}}},
    ]

    trace = install_wire_trace()
    with patch.object(
        groq_provider._client.chat.completions,
        "create",
        AsyncMock(return_value=object()),
    ):
        await groq_provider._create_stream(body)

    assert trace.requests[0].params["tools"] == 2


@pytest.mark.asyncio
async def test_no_reasoning_provider_records_reasoning_as_not_emitted(groq_provider):
    """A body a reasoning encoder never touched must report ``False``.

    Not ``None``, and not the gating decision: the point of the flag is that
    "gating asked for high effort" and "high effort was sent" are separate
    facts, and for ~23,000 requests only the first was recorded.
    """
    body = _client_body(groq_provider)
    body.pop("reasoning_effort", None)
    body.pop("reasoning", None)

    trace = install_wire_trace()
    with patch.object(
        groq_provider._client.chat.completions,
        "create",
        AsyncMock(return_value=object()),
    ):
        await groq_provider._create_stream(body)

    assert trace.requests[0].reasoning_emitted is False


@pytest.mark.asyncio
async def test_a_provider_that_emits_reasoning_records_it_as_emitted(groq_provider):
    body = _client_body(groq_provider)
    body["reasoning_effort"] = "high"

    trace = install_wire_trace()
    with patch.object(
        groq_provider._client.chat.completions,
        "create",
        AsyncMock(return_value=object()),
    ):
        await groq_provider._create_stream(body)

    assert trace.requests[0].reasoning_emitted is True


@pytest.mark.asyncio
async def test_the_recorded_body_carries_no_prompt_text_and_no_api_key(groq_provider):
    body = _client_body(groq_provider)
    body["extra_body"] = {"api_key": "nvapi-leakedleakedleaked"}

    trace = install_wire_trace()
    with patch.object(
        groq_provider._client.chat.completions,
        "create",
        AsyncMock(return_value=object()),
    ):
        await groq_provider._create_stream(body)

    stored = trace.requests[0].body_json
    assert "nvapi-leakedleakedleaked" not in stored
    assert "test_groq_key" not in stored
    # ``stream`` is passed to the SDK as a keyword, not inside the dict, so the
    # capture has to add it back or the record is not the whole call.
    assert '"stream": true' in stored
