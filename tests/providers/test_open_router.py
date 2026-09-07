"""Tests for the OpenRouter OpenAI-chat provider."""

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import (
    parse_sse_text,
    text_content,
)
from my_claude_code.core.reported_cost import install_reported_cost
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.open_router import OpenRouterProvider
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from tests.providers.request_factory import make_messages_request
from tests.providers.support import (
    REASONING_OFF,
    passthrough_rate_limiter,
    reasoning_for,
)


class AsyncStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def make_request(**overrides):
    return make_messages_request("moonshotai/kimi-k2.6:free", **overrides)


@pytest.fixture
def open_router_provider():
    return OpenRouterProvider(
        ProviderConfig(
            api_key="test_openrouter_key",
            base_url="https://openrouter.ai/api/v1",
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


def _chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    reasoning_details: list[dict] | None = None,
    finish_reason: str | None = None,
):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=None,
    )
    if reasoning_details is not None:
        delta.reasoning_details = reasoning_details
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def test_init_uses_openai_chat_provider(open_router_provider):
    assert isinstance(open_router_provider, OpenAIChatProvider)
    assert open_router_provider._api_key == "test_openrouter_key"
    assert open_router_provider._base_url == "https://openrouter.ai/api/v1"


def test_build_request_body_uses_openai_chat_shape(open_router_provider):
    body = open_router_provider._build_request_body(make_request())

    assert body["model"] == "moonshotai/kimi-k2.6:free"
    assert body["temperature"] == 0.5
    assert body["messages"] == [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Hello"},
    ]
    assert body["max_tokens"] == 100
    assert "extra_body" not in body


def test_build_request_body_default_max_tokens(open_router_provider):
    body = open_router_provider._build_request_body(make_request(max_tokens=None))

    assert body["max_tokens"] == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS


def test_openrouter_extra_body_rejects_overriding_reserved_fields(
    open_router_provider,
):
    with pytest.raises(InvalidRequestError, match="model"):
        open_router_provider._build_request_body(
            make_request(extra_body={"model": "hijack"})
        )


def test_openrouter_extra_body_allows_provider_keys(open_router_provider):
    body = open_router_provider._build_request_body(
        make_request(extra_body={"transforms": ["no-web"], "plugins": []}),
        reasoning=REASONING_OFF,
    )

    assert body["extra_body"] == {
        "transforms": ["no-web"],
        "plugins": [],
        "reasoning": {"enabled": False},
    }


def test_build_request_body_disables_reasoning_when_client_disables_it(
    open_router_provider,
):
    request = make_request(thinking={"type": "disabled"})
    body = open_router_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["reasoning"] == {"enabled": False}


def test_build_request_body_maps_thinking_budget_to_reasoning_max_tokens(
    open_router_provider,
):
    request = make_request(thinking={"type": "enabled", "budget_tokens": 4096})
    body = open_router_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["reasoning"] == {"max_tokens": 4096}


def test_build_request_body_replays_openrouter_reasoning_details(
    open_router_provider,
):
    detail = {"type": "reasoning.encrypted", "data": "opaque"}
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "redacted_thinking",
                            "data": '{"type":"reasoning.encrypted","data":"opaque"}',
                        },
                        {"type": "text", "text": "Need a tool."},
                    ],
                },
                {"role": "user", "content": "continue"},
            ],
        }
    )

    body = open_router_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assistant = next(msg for msg in body["messages"] if msg["role"] == "assistant")
    assert assistant["reasoning_details"] == [detail]


@pytest.mark.asyncio
async def test_stream_maps_reasoning_content_and_details(open_router_provider):
    redacted = {"type": "reasoning.encrypted", "data": "opaque"}
    stream = AsyncStream(
        [
            _chunk(reasoning_content="plan "),
            _chunk(reasoning_details=[redacted]),
            _chunk(content="done", finish_reason="stop"),
        ]
    )
    with patch.object(
        open_router_provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        events = [
            event
            async for event in open_router_provider.stream_response(make_request())
        ]

    event_text = "".join(events)
    assert "thinking_delta" in event_text
    assert "plan " in event_text
    assert "redacted_thinking" in event_text
    assert "opaque" in event_text
    assert "done" in text_content(parse_sse_text(event_text))
    assert stream.closed


@pytest.mark.asyncio
async def test_model_infos_filter_tool_models_and_thinking_metadata(
    open_router_provider,
):
    open_router_provider._client.models.list = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="tool-model",
                    supported_parameters=["tools", "reasoning"],
                ),
                SimpleNamespace(id="plain-model", supported_parameters=[]),
            ]
        )
    )

    infos = await open_router_provider.list_model_infos()

    assert {(info.model_id, info.supports_thinking) for info in infos} == {
        ("tool-model", True)
    }


@pytest.mark.asyncio
async def test_cleanup_closes_openai_client(open_router_provider):
    open_router_provider._client = MagicMock()
    open_router_provider._client.close = AsyncMock()

    await open_router_provider.cleanup()

    open_router_provider._client.close.assert_awaited_once()


# --------------------------------------------------------------------------
# Reported cost (6.54.0). Read off the final usage block, which is the one
# place every OpenAI-shaped host's usage passes through -- nothing in the
# provider layer names OpenRouter, and any host reporting the same keys is
# read the same way.
# --------------------------------------------------------------------------


def _usage_chunk(usage, *, finish_reason="stop"):
    """The final SSE chunk, in the shape OpenRouter actually sends it.

    OpenRouter deliberately deviates from OpenAI here: rather than an empty
    ``choices`` array it sends one choice with an empty delta repeating the
    finish reason. Detecting the usage chunk by ``choices.length === 0`` --
    the obvious reading -- therefore misses it every time.
    """
    delta = SimpleNamespace(content=None, reasoning_content=None, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


async def _run(provider, chunks):
    stream = AsyncStream(chunks)
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        return [event async for event in provider.stream_response(make_request())]


@pytest.mark.asyncio
async def test_usage_cost_is_read_from_the_final_stream_chunk(open_router_provider):
    slot = install_reported_cost()
    await _run(
        open_router_provider,
        [
            _chunk(content="hi"),
            _usage_chunk(
                {
                    "prompt_tokens": 194,
                    "completion_tokens": 2,
                    "cost": 0.95,
                    "cost_details": {"upstream_inference_cost": None},
                    "is_byok": False,
                }
            ),
        ],
    )
    assert slot.cost_usd == 0.95
    assert slot.total_usd == 0.95


@pytest.mark.asyncio
async def test_the_usage_chunk_is_detected_by_usage_not_by_empty_choices(
    open_router_provider,
):
    """The chunk that carries the cost still has a choice in it."""
    chunk = _usage_chunk({"cost": 0.1, "cost_details": {}, "prompt_tokens": 1})
    assert chunk.choices, "the trap: this is not an empty-choices chunk"
    slot = install_reported_cost()
    await _run(open_router_provider, [_chunk(content="hi"), chunk])
    assert slot.cost_usd == 0.1


@pytest.mark.asyncio
async def test_a_byok_response_is_not_priced_at_the_surcharge_alone(
    open_router_provider,
):
    """On BYOK, ``cost`` is OpenRouter's ~5% cut and not the bill."""
    slot = install_reported_cost()
    await _run(
        open_router_provider,
        [
            _chunk(content="hi"),
            _usage_chunk(
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "cost": 0.05,
                    "cost_details": {"upstream_inference_cost": 1.0},
                    "is_byok": True,
                }
            ),
        ],
    )
    assert slot.cost_usd == 0.05
    assert slot.total_usd == pytest.approx(1.05)


@pytest.mark.asyncio
async def test_a_host_that_reports_no_cost_leaves_the_request_unpriced(
    open_router_provider,
):
    slot = install_reported_cost()
    await _run(
        open_router_provider,
        [
            _chunk(content="hi"),
            _usage_chunk({"prompt_tokens": 100, "completion_tokens": 10}),
        ],
    )
    assert slot.cost_usd is None
    assert slot.total_usd is None


def _float_every_value(mapping: Mapping[str, Any]) -> dict[str, float]:
    """The obvious, wrong way to read a ``pricing`` object."""
    return {key: float(value) for key, value in mapping.items()}


def test_a_models_pricing_object_with_overrides_and_discount_parses():
    """``pricing`` carries an array and a number beside its decimal strings.

    A naive "map every value to float" over that object raises. MCC reads the
    ``/models`` pricing object nowhere today -- the price ladder comes from
    models.dev and LiteLLM -- and this pins the shape so a future reader
    starts from the real one rather than from the documented one.
    """
    pricing: dict[str, Any] = {
        "prompt": "0.00001",
        "completion": "0.00005",
        "input_cache_read": "0.00000025",
        "input_cache_write": "0.0000125",
        "web_search": "0.01",
        "overrides": [{"provider": "anthropic", "prompt": "0.000009"}],
        "discount": 0.25,
    }
    numeric = {
        key: float(value) for key, value in pricing.items() if isinstance(value, str)
    }
    assert numeric["prompt"] == 1e-05
    assert isinstance(pricing["overrides"], list)
    assert isinstance(pricing["discount"], float)
    with pytest.raises(TypeError):
        # The naive read, which is what raises: `overrides` is an array and
        # `float()` refuses it. Written through a helper so the expression is
        # about the runtime shape rather than about what a checker can prove.
        _float_every_value(pricing)
