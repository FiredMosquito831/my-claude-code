"""Tests for DeepSeek OpenAI-compatible Chat Completions provider."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from openai import AsyncOpenAI

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.config.provider_catalog import DEEPSEEK_DEFAULT_BASE
from my_claude_code.core.anthropic.models import (
    ContentBlockImage,
    Message,
    MessagesRequest,
    Tool,
)
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.tool_result_media import (
    USER_DOCUMENT_STRIPPED_TEXT,
    USER_IMAGE_STRIPPED_TEXT,
    collect_tool_names,
    replace_request_media,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.deepseek.tool_choice import (
    clone_body_with_required_tool_choice,
    is_deepseek_tool_choice_rejection,
)
from tests.providers.support import (
    REASONING_OFF,
    REASONING_ON,
    passthrough_rate_limiter,
    reasoning_for,
)


def _make_deepseek_bad_request_error(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(400, request=request)
    body = {"error": {"message": message}}
    return openai.BadRequestError(message, response=response, body=body)


def _forced_tool_choice_request() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": {"type": "tool", "name": "Read"},
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
    )


def _fake_recovered_stream():
    async def _iter():
        chunk = MagicMock()
        chunk.choices = [
            MagicMock(
                delta=MagicMock(
                    content="Recovered", reasoning_content=None, tool_calls=None
                ),
                finish_reason="stop",
            )
        ]
        chunk.usage = MagicMock(completion_tokens=5, prompt_tokens=10)
        yield chunk

    return _iter()


@pytest.fixture
def deepseek_config():
    return ProviderConfig(
        api_key="test_deepseek_key",
        base_url=DEEPSEEK_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
    )


@pytest.fixture
def deepseek_provider(deepseek_config):
    return DeepSeekProvider(deepseek_config, rate_limiter=passthrough_rate_limiter())


async def _capture_openai_wire_body(body: dict) -> dict:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        captured.append(payload)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: [DONE]\n\n",
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="test",
        base_url="https://deepseek.invalid",
        http_client=http_client,
        max_retries=0,
    )
    try:
        stream = await client.chat.completions.create(**body, stream=True)
        await stream.close()
    finally:
        await client.close()

    assert len(captured) == 1
    return captured[0]


def test_default_base_url_alias():
    assert DEEPSEEK_DEFAULT_BASE == "https://api.deepseek.com"


def test_init(deepseek_config):
    with patch(
        "my_claude_code.providers.openai_chat.provider.AsyncOpenAI"
    ) as mock_client:
        provider = DeepSeekProvider(
            deepseek_config, rate_limiter=passthrough_rate_limiter()
        )
    assert provider._api_key == "test_deepseek_key"
    assert provider._base_url == "https://api.deepseek.com"
    assert mock_client.called


def test_build_request_body_openai_chat_shape(deepseek_provider):
    request = MessagesRequest(
        model="deepseek-v4-pro",
        max_tokens=100,
        messages=[Message(role="user", content="Hello")],
        system="S",
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["model"] == "deepseek-v4-pro"
    assert "stream" not in body
    assert body["messages"][0] == {"role": "system", "content": "S"}
    assert body["messages"][1]["role"] == "user"
    assert body["messages"][1] == {"role": "user", "content": "Hello"}
    assert body["max_tokens"] == 100
    assert "stream_options" not in body


def test_build_request_body_default_max_tokens(deepseek_provider):
    request = MessagesRequest(
        model="m",
        messages=[Message(role="user", content="x")],
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["max_tokens"] == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS


def test_build_request_body_thinking_enabled(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "thinking": {"type": "enabled", "budget_tokens": 2000},
        }
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["extra_body"]["thinking"] == {"type": "enabled"}


def test_build_request_body_tool_list_keeps_thinking(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "thinking": {"type": "enabled", "budget_tokens": 2000},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert body["tools"][0]["function"]["name"] == "Read"


def test_build_request_body_tool_choice_keeps_thinking(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "enabled", "budget_tokens": 2000},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert body["tool_choice"] == "auto"


def test_build_request_body_forced_tool_choice_reaches_wire_unmodified(
    deepseek_provider,
):
    """A forced named tool_choice must not be silently downgraded up front.

    DeepSeek's documented API supports a forced named tool_choice; the
    request builder must send it as-is. Rejection (if any) is handled
    reactively via DeepSeekProvider._get_retry_request_body.
    """
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": {"type": "tool", "name": "Read"},
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "thinking": {"type": "enabled", "budget_tokens": 2000},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert body["tool_choice"] == {"type": "function", "function": {"name": "Read"}}


def test_build_request_body_encodes_reasoning_off():
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
            rate_limit=1,
            rate_window=1,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "thinking": {"type": "enabled", "budget_tokens": 1},
        }
    )
    body = provider._build_request_body(request, reasoning=REASONING_OFF)
    assert body["extra_body"]["thinking"] == {"type": "disabled"}
    assert "stream_options" not in body


def test_non_tool_thinking_is_omitted_from_first_replay(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "plain",
                            "signature": None,
                        },
                        {"type": "text", "text": "out"},
                    ],
                }
            ],
        }
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["messages"][0] == {"role": "assistant", "content": "out"}


def test_strip_redacted_thinking_when_thinking_on(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "opaque"},
                        {"type": "text", "text": "out"},
                    ],
                }
            ],
        }
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["messages"][0] == {"role": "assistant", "content": "out"}


def test_tool_history_with_replayable_thinking_preserves_thinking(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "hidden",
                            "signature": "sig_123",
                        },
                        {"type": "redacted_thinking", "data": "opaque"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "thinking": {"type": "enabled", "budget_tokens": 2000},
            "context_management": {
                "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
            },
            "output_config": {"effort": "high"},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["reasoning_effort"] == "high"
    assert "context_management" not in body
    assert "output_config" not in body
    assistant = body["messages"][0]
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "hidden"
    assert assistant["tool_calls"][0]["function"]["name"] == "Read"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"file_path": "x"}'
    assert body["messages"][1] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "ok",
    }


def test_tool_history_with_unsigned_thinking_preserves_thinking(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "plain"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "thinking": {"type": "enabled"},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert body["messages"][0]["reasoning_content"] == "plain"


def test_tool_history_without_thinking_disables_thinking_and_hints(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "enabled", "budget_tokens": 2000},
            "context_management": {
                "edits": [
                    {"type": "clear_thinking_20251015", "keep": "all"},
                    {"type": "other_edit", "keep": "all"},
                ],
                "other": True,
            },
            "output_config": {"effort": "high", "format": "text"},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "disabled"}
    assert "context_management" not in body
    assert "output_config" not in body
    assert body["tools"][0]["function"]["name"] == "Read"
    assert body["tool_choice"] == "auto"
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "Read"
    assert body["messages"][1]["role"] == "tool"


def test_tool_history_with_empty_thinking_preserves_reasoning_state(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": ""},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "thinking": {"type": "enabled"},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert body["messages"][0]["reasoning_content"] == ""
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "Read"


def test_tool_history_with_empty_top_level_reasoning_preserves_reasoning_state(
    deepseek_provider,
):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        },
                    ],
                    "reasoning_content": "",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "thinking": {"type": "enabled"},
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert body["messages"][0]["reasoning_content"] == ""
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "Read"


def test_thinking_off_strips_thinking_history():
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
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
                    "content": [
                        {"type": "thinking", "thinking": "sec"},
                        {"type": "text", "text": "hi"},
                    ],
                }
            ],
        }
    )
    body = provider._build_request_body(request, reasoning=REASONING_OFF)
    assert "reasoning_content" not in body["messages"][0]
    assert "sec" not in str(body["messages"])


def test_thinking_off_still_replays_required_tool_reasoning():
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
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
                    "content": [
                        {"type": "thinking", "thinking": "required"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
        }
    )

    body = provider._build_request_body(request, reasoning=REASONING_OFF)

    assert body["extra_body"]["thinking"] == {"type": "disabled"}
    assert body["messages"][0]["reasoning_content"] == "required"


def test_passthrough_tool_use_and_result(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "n",
                            "input": {"a": 1},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
        }
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "n"
    assert body["messages"][1]["role"] == "tool"


def test_preflight_strips_user_image():
    """A blind model's image becomes the general placeholder, not a rejection.

    Until 6.49.0 this strip was DeepSeek's own private code. It is now the
    general rule -- the router replaces every visual block for any model
    published as not accepting images -- and DeepSeek is kept here as the
    regression witness for it rather than as its owner.
    """
    request = MessagesRequest(
        model="m",
        messages=[
            Message(
                role="user",
                content=[
                    ContentBlockImage(
                        type="image",
                        source={
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "YQ==",
                        },
                    )
                ],
            )
        ],
    )
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
            rate_limit=1,
            rate_window=1,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    # Should not raise; the router has already replaced the image.
    provider.preflight_stream(request, reasoning=REASONING_ON)
    replace_request_media(request.messages)
    body = provider._build_request_body(request, reasoning=reasoning_for(request))
    content = body["messages"][0]["content"]
    assert content == USER_IMAGE_STRIPPED_TEXT


def test_preflight_rejects_mcp_servers():
    request = MessagesRequest(
        model="m",
        messages=[Message(role="user", content="x")],
        mcp_servers=[{"type": "url", "url": "https://x"}],
    )
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
            rate_limit=1,
            rate_window=1,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    with pytest.raises(InvalidRequestError, match="mcp_servers"):
        provider.preflight_stream(request)


def test_preflight_rejects_listed_server_tools_in_tools_list():
    request = MessagesRequest(
        model="m",
        messages=[Message(role="user", content="x")],
        tools=[Tool(name="web_search", type="web_search_20250305", input_schema={})],
    )
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
            rate_limit=1,
            rate_window=1,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    with pytest.raises(InvalidRequestError, match="web_search"):
        provider.preflight_stream(request)


def test_preflight_rejects_server_tool_result_blocks():
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "server_tool_use",
                            "id": "s1",
                            "name": "web_search",
                            "input": {"q": "a"},
                        },
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "s1",
                            "content": [],
                        },
                    ],
                }
            ],
        }
    )
    provider = DeepSeekProvider(
        ProviderConfig(
            api_key="k",
            base_url=DEEPSEEK_DEFAULT_BASE,
            rate_limit=1,
            rate_window=1,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    with pytest.raises(InvalidRequestError, match=r"web_search_tool_result|server"):
        provider.preflight_stream(request)


def test_non_tool_top_level_reasoning_is_not_replayed(deepseek_provider):
    request = MessagesRequest(
        model="m",
        messages=[
            Message(
                role="assistant",
                content="hi",
                reasoning_content="r",
            )
        ],
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["messages"][0] == {"role": "assistant", "content": "hi"}


def test_tool_call_top_level_reasoning_is_replayed(deepseek_provider):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "x"},
                        }
                    ],
                    "reasoning_content": "required",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                        }
                    ],
                },
            ],
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["messages"][0]["reasoning_content"] == "required"


@pytest.mark.asyncio
async def test_wire_messages_keep_prefix_across_tool_thinking_fallback(
    deepseek_provider,
):
    prefix_messages = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "ordinary reasoning"},
                {"type": "text", "text": "answer"},
            ],
        },
        {"role": "user", "content": "use the first tool"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "required tool reasoning"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "one"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "one",
                }
            ],
        },
        {"role": "user", "content": "use the second tool"},
    ]
    continued_messages = [
        *prefix_messages,
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Read",
                    "input": {"file_path": "two"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": "two",
                }
            ],
        },
    ]

    def build(messages: list[dict]) -> dict:
        request = MessagesRequest.model_validate(
            {
                "model": "deepseek-v4-pro",
                "messages": messages,
                "thinking": {"type": "enabled"},
            }
        )
        return deepseek_provider._build_request_body(
            request, reasoning=reasoning_for(request)
        )

    first_wire = await _capture_openai_wire_body(build(prefix_messages))
    continued_wire = await _capture_openai_wire_body(build(continued_messages))
    first_messages = first_wire["messages"]
    continued = continued_wire["messages"]

    assert continued[: len(first_messages)] == first_messages
    assistant_messages = [
        message for message in first_messages if message["role"] == "assistant"
    ]
    assert "reasoning_content" not in assistant_messages[0]
    assert assistant_messages[1]["reasoning_content"] == "required tool reasoning"
    assert first_wire["thinking"] == {"type": "enabled"}
    assert continued_wire["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_stream_uses_chat_completions_and_maps_cache_usage(deepseek_provider):
    request = MessagesRequest(
        model="m",
        messages=[Message(role="user", content="hi")],
    )

    async def fake_stream():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="hello", reasoning_content=None, tool_calls=None
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None, reasoning_content=None, tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                completion_tokens=3,
                prompt_tokens=30,
                prompt_cache_hit_tokens=10,
                prompt_cache_miss_tokens=20,
            ),
        )

    create = AsyncMock(return_value=fake_stream())
    with patch.object(deepseek_provider._client.chat.completions, "create", create):
        chunks = [
            chunk
            async for chunk in deepseek_provider.stream_response(
                request, input_tokens=7, request_id="r1"
            )
        ]

    create.assert_awaited_once()
    await_args = create.await_args
    assert await_args is not None
    assert await_args.kwargs["model"] == "m"
    assert await_args.kwargs["stream"] is True
    assert await_args.kwargs["stream_options"] == {"include_usage": True}
    parsed = parse_sse_text("".join(chunks))
    usage = next(
        event.data["usage"] for event in parsed if event.event == "message_delta"
    )
    # DeepSeek reported prompt_tokens=30 as hit=10 + miss=20. Anthropic's
    # input_tokens is the uncached part, so the hit count comes out of it and
    # input + cache_read reconstructs the 30 tokens actually sent. Reporting
    # input_tokens=30 alongside both cache fields counted the prompt twice.
    assert usage == {
        "input_tokens": 20,
        "output_tokens": 3,
        "cache_read_input_tokens": 10,
    }
    assert usage["input_tokens"] + usage["cache_read_input_tokens"] == 30


def test_preserves_extra_body_for_openai_chat_request(deepseek_provider):
    raw = {
        "model": "m",
        "max_tokens": 3,
        "messages": [{"role": "user", "content": "x"}],
        "extra_body": {"note": 1},
    }
    r = MessagesRequest.model_validate(raw)
    body = deepseek_provider._build_request_body(r, reasoning=reasoning_for(r))
    assert body["extra_body"] == {"note": 1}


def test_normalizes_tool_result_content_array_to_string(deepseek_provider):
    """Test that tool_result content arrays are normalized to strings for DeepSeek API."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "list_dir",
                            "input": {"path": "/"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "text", "text": "file1.txt"},
                                {"type": "text", "text": "file2.txt"},
                            ],
                        }
                    ],
                },
            ],
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    tool_result = body["messages"][1]
    assert tool_result["role"] == "tool"
    assert isinstance(tool_result["content"], str)
    assert "file1.txt" in tool_result["content"]
    assert "file2.txt" in tool_result["content"]


def test_strips_document_blocks_for_deepseek(deepseek_provider):
    """Document blocks (e.g. PDFs from Claude Code) are stripped since DeepSeek can't process them."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "PDF text extracted",
                        },
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": "file_abc"},
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                },
            ],
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["messages"][0] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "PDF text extracted",
    }


def test_strips_image_blocks_for_deepseek(deepseek_provider):
    """Image blocks are stripped for DeepSeek since it doesn't support vision."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc",
                            },
                        },
                    ],
                },
            ],
        }
    )

    replace_request_media(request.messages)
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["messages"][0] == {
        "role": "user",
        "content": f"describe this\n{USER_IMAGE_STRIPPED_TEXT}",
    }


def test_normalizes_tool_result_content_dict_to_string(deepseek_provider):
    """Test that tool_result content dicts are normalized to JSON strings."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "get_data",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": {"status": "success", "data": [1, 2, 3]},
                        }
                    ],
                },
            ],
        }
    )

    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    tool_result = body["messages"][1]
    assert tool_result["role"] == "tool"
    assert isinstance(tool_result["content"], str)
    assert "status" in tool_result["content"]
    assert "success" in tool_result["content"]


def test_strips_image_block_inside_tool_result(deepseek_provider):
    """Image blocks nested inside tool_result.content are stripped, not rejected."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"path": "shot.png"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {"type": "text", "text": "screenshot saved"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "abc",
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
        }
    )

    replace_request_media(
        request.messages, tool_names=collect_tool_names(request.messages)
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    tool_result = body["messages"][1]
    assert tool_result["role"] == "tool"
    # After the general strip, no base64 and no image marker survives.
    assert isinstance(tool_result["content"], str)
    assert "screenshot saved" in tool_result["content"]
    assert "does not accept images" in tool_result["content"]
    assert "'Read' tool" in tool_result["content"]
    assert "base64" not in tool_result["content"]


def test_image_only_tool_result_replaced_with_placeholder(deepseek_provider):
    """A tool_result whose only inner block is an image becomes a placeholder string."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Screenshot",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "abc",
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
        }
    )

    replace_request_media(
        request.messages, tool_names=collect_tool_names(request.messages)
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    tool_result = body["messages"][1]
    assert tool_result["role"] == "tool"
    assert isinstance(tool_result["content"], str)
    assert tool_result["content"] != ""
    assert "does not accept images" in tool_result["content"]


def test_document_only_tool_result_replaced_with_generic_placeholder(
    deepseek_provider,
):
    """A document-only tool_result uses the generic attachment placeholder."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "paper.pdf"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {
                                    "type": "document",
                                    "source": {
                                        "type": "file",
                                        "file_id": "file_pdf",
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
        }
    )

    replace_request_media(
        request.messages, tool_names=collect_tool_names(request.messages)
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    tool_result = body["messages"][1]
    assert tool_result["role"] == "tool"
    assert isinstance(tool_result["content"], str)
    assert "does not accept documents" in tool_result["content"]
    assert "does not accept images" not in tool_result["content"]


def test_image_only_message_replaced_with_placeholder(deepseek_provider):
    """A top-level image-only message remains non-empty after stripping."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc",
                            },
                        },
                    ],
                },
            ],
        }
    )

    replace_request_media(
        request.messages, tool_names=collect_tool_names(request.messages)
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    content = body["messages"][0]["content"]
    assert content == USER_IMAGE_STRIPPED_TEXT


def test_document_only_message_replaced_with_placeholder(deepseek_provider):
    """A top-level document-only message remains non-empty after stripping."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": "file_pdf"},
                        },
                    ],
                },
            ],
        }
    )

    replace_request_media(
        request.messages, tool_names=collect_tool_names(request.messages)
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    content = body["messages"][0]["content"]
    assert content == USER_DOCUMENT_STRIPPED_TEXT


def test_warns_when_stripping_attachment_blocks(deepseek_provider, caplog):
    """Both a pasted image and a tool-returned one become placeholders.

    The warning that used to live in DeepSeek's own strip now comes from the
    router, which is where the decision is made; what this still witnesses is
    that a blind model's request reaches DeepSeek carrying sentences instead of
    base64, at both nesting depths at once.
    """
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "abc",
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Screenshot",
                            "input": {},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "abc",
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
        }
    )

    assert (
        replace_request_media(
            request.messages, tool_names=collect_tool_names(request.messages)
        )
        == 2
    )
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )

    assert body["messages"][0]["content"] == (
        "look" + chr(10) + USER_IMAGE_STRIPPED_TEXT
    )
    assert "does not accept images" in body["messages"][2]["content"]
    assert "abc" not in json.dumps(body["messages"])


def test_no_media_walk_on_a_text_only_request(deepseek_provider):
    """A request with nothing visual in it is untouched by the media rule."""
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert replace_request_media(request.messages) == 0
    body = deepseek_provider._build_request_body(
        request, reasoning=reasoning_for(request)
    )
    assert body["messages"][0] == {"role": "user", "content": "hello"}


def test_is_deepseek_tool_choice_rejection_matches_tool_choice_400():
    error = _make_deepseek_bad_request_error(
        "deepseek-reasoner does not support this tool_choice"
    )
    assert is_deepseek_tool_choice_rejection(error) is True


def test_is_deepseek_tool_choice_rejection_ignores_unrelated_400():
    error = _make_deepseek_bad_request_error(
        "This model's maximum context length is exceeded"
    )
    assert is_deepseek_tool_choice_rejection(error) is False


def test_is_deepseek_tool_choice_rejection_ignores_non_400():
    class _NotBadRequest(Exception):
        status_code = 500

    error = _NotBadRequest("tool_choice not supported")
    assert is_deepseek_tool_choice_rejection(error) is False


def test_clone_body_with_required_tool_choice_downgrades_named_choice():
    body = {
        "model": "m",
        "tool_choice": {"type": "function", "function": {"name": "Read"}},
    }
    retry_body = clone_body_with_required_tool_choice(body)
    assert retry_body is not None
    assert retry_body["tool_choice"] == "required"
    # original body is untouched
    assert body["tool_choice"] == {"type": "function", "function": {"name": "Read"}}


@pytest.mark.parametrize("tool_choice", ["auto", "none", "required", None])
def test_clone_body_with_required_tool_choice_ignores_non_forced_choice(tool_choice):
    body = {"model": "m", "tool_choice": tool_choice}
    assert clone_body_with_required_tool_choice(body) is None


def test_get_retry_request_body_downgrades_on_tool_choice_rejection(
    deepseek_provider,
):
    body = {
        "model": "m",
        "tool_choice": {"type": "function", "function": {"name": "Read"}},
    }
    error = _make_deepseek_bad_request_error(
        "deepseek-reasoner does not support this tool_choice"
    )

    retry_body = deepseek_provider._get_retry_request_body(error, body)

    assert retry_body is not None
    assert retry_body["tool_choice"] == "required"


def test_get_retry_request_body_ignores_unrelated_error(deepseek_provider):
    body = {
        "model": "m",
        "tool_choice": {"type": "function", "function": {"name": "Read"}},
    }
    error = _make_deepseek_bad_request_error(
        "This model's maximum context length is exceeded"
    )

    assert deepseek_provider._get_retry_request_body(error, body) is None


@pytest.mark.asyncio
async def test_stream_response_retries_tool_choice_rejection_with_required(
    deepseek_provider, caplog
):
    request = _forced_tool_choice_request()
    error = _make_deepseek_bad_request_error(
        "deepseek-reasoner does not support this tool_choice"
    )

    with patch.object(
        deepseek_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.side_effect = [error, _fake_recovered_stream()]

        with caplog.at_level(logging.WARNING):
            events = [
                e
                async for e in deepseek_provider.stream_response(
                    request, reasoning=reasoning_for(request)
                )
            ]

    assert mock_create.await_count == 2
    first_call = mock_create.await_args_list[0].kwargs
    second_call = mock_create.await_args_list[1].kwargs
    assert first_call["tool_choice"] == {
        "type": "function",
        "function": {"name": "Read"},
    }
    assert second_call["tool_choice"] == "required"
    assert any("Recovered" in e for e in events)
    assert any(
        "downgraded to 'required'" in r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


@pytest.mark.asyncio
async def test_stream_response_second_tool_choice_rejection_is_not_recovered(
    deepseek_provider,
):
    """The provider-specific retry hook fires at most once per request.

    See OpenAIChatProvider._next_create_retry_body: it gates the
    "provider_specific" retry kind behind `used_retry_kinds`, so once one
    provider-specific retry has been consumed, a second rejection of the
    same kind is not retried again and propagates as a failure.
    """
    request = _forced_tool_choice_request()
    error = _make_deepseek_bad_request_error(
        "deepseek-reasoner does not support this tool_choice"
    )

    with patch.object(
        deepseek_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.side_effect = [error, error, error]

        with pytest.raises(ExecutionFailure):
            [
                e
                async for e in deepseek_provider.stream_response(
                    request, reasoning=reasoning_for(request)
                )
            ]

    # exactly one retry was attempted (named -> required), then it failed
    assert mock_create.await_count == 2


@pytest.mark.asyncio
async def test_stream_response_unrelated_bad_request_does_not_downgrade_tool_choice(
    deepseek_provider,
):
    request = _forced_tool_choice_request()
    error = _make_deepseek_bad_request_error(
        "This model's maximum context length is exceeded"
    )

    with patch.object(
        deepseek_provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.side_effect = error

        with pytest.raises(ExecutionFailure):
            [
                e
                async for e in deepseek_provider.stream_response(
                    request, reasoning=reasoning_for(request)
                )
            ]

    assert mock_create.await_count == 1


@pytest.mark.asyncio
async def test_stream_response_tool_choice_auto_passes_through_untouched(
    deepseek_provider,
):
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": {"type": "auto"},
        }
    )

    with patch.object(
        deepseek_provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=_fake_recovered_stream(),
    ) as mock_create:
        [
            e
            async for e in deepseek_provider.stream_response(
                request, reasoning=reasoning_for(request)
            )
        ]

    assert mock_create.await_count == 1
    assert mock_create.await_args_list[0].kwargs["tool_choice"] == "auto"
