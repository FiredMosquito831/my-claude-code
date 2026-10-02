"""A Responses turn that ends with a tool call says so: ``stop_reason: tool_use``.

The Responses door -- ChatGPT OAuth and OpenCode's ``/responses`` surface share
one converter -- ended every turn ``end_turn``, even a turn whose only content
was a structured ``function_call``. The Chat door has always said ``tool_use``
there (``ledger.final_stop_reason``), and so does Anthropic. Claude Code runs
the call either way, but a Chat Completions client was told
``finish_reason: "stop"`` with tool calls in hand, because the egress maps the
stop reason before it looks at the calls.

Since 7.69.5 the Responses door asks the ledger the Chat door's question. A
turn without a tool call is byte-for-byte what it was, which the pinned frame
below holds; nothing sent upstream changes at all.
"""

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.core.anthropic import aggregate_anthropic_sse_to_message
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.openai_chat_completions import (
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import ResponsesStreamConverter
from tests.providers.support import passthrough_rate_limiter

_TEXT = [
    {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Hello"},
    {
        "type": "response.output_item.done",
        "item": {"id": "msg_1", "type": "message", "status": "completed"},
    },
]
_CALL = [
    {
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "Bash",
        },
    },
    {
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_1",
        "delta": '{"command":"ls"}',
    },
    {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "Bash",
            "arguments": '{"command":"ls"}',
        },
    },
]
_REASONING = [
    {
        "type": "response.reasoning_summary_text.delta",
        "item_id": "rs_1",
        "delta": "thinking it over",
    },
]
_COMPLETED = {
    "type": "response.completed",
    "response": {
        "status": "completed",
        "usage": {"input_tokens": 9, "output_tokens": 4},
    },
}


def _convert(frames: Iterable[dict[str, Any]], **finish: Any) -> str:
    ledger = AnthropicStreamLedger("msg_x", "gpt-6-sol", input_tokens=0)
    converter = ResponsesStreamConverter(ledger)
    out = [ledger.message_start()]
    for frame in frames:
        out.extend(converter.feed(frame))
    out.extend(converter.finish(**finish))
    return "".join(out)


def _stop_reason(sse: str) -> str:
    deltas = [
        event.data["delta"]["stop_reason"]
        for event in parse_sse_text(sse)
        if event.event == "message_delta"
    ]
    assert len(deltas) == 1, deltas
    return deltas[0]


# -- the converter ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        (_CALL, "tool_use"),
        ([*_TEXT, *_CALL], "tool_use"),
        ([*_REASONING, *_CALL], "tool_use"),
        ([{**_TEXT[0], "delta": "<think>plan</think>"}, *_CALL], "tool_use"),
        (_TEXT, "end_turn"),
        (_REASONING, "end_turn"),
        ([], "end_turn"),
    ],
    ids=[
        "call",
        "text+call",
        "reasoning+call",
        "think-tag+call",
        "text",
        "reasoning",
        "empty",
    ],
)
def test_a_turn_ends_for_its_tool_call_and_only_then(
    frames: list[dict[str, Any]], expected: str
) -> None:
    assert _stop_reason(_convert([*frames, _COMPLETED])) == expected


def test_a_stop_reason_the_caller_names_still_wins() -> None:
    assert _stop_reason(_convert([*_CALL, _COMPLETED], stop_reason="max_tokens")) == (
        "max_tokens"
    )


def test_a_text_only_turn_is_byte_identical_to_before() -> None:
    """The exact frames 7.69.4 sent for a text answer, pinned."""
    sse = _convert([*_TEXT, _COMPLETED])
    assert sse.endswith(
        'event: message_delta\ndata: {"type": "message_delta", "delta": '
        '{"stop_reason": "end_turn", "stop_sequence": null}, "usage": '
        '{"input_tokens": 9, "output_tokens": 4}}\n\n'
        'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    ), sse


# -- both providers that use it ----------------------------------------------------


def _sse_bytes(frames: Iterable[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames
    ).encode()


def _request() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "gpt-6-sol",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "list the files"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Run a command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    },
                }
            ],
        }
    )


async def _chatgpt_oauth(frames: list[dict[str, Any]]) -> str:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(api_key="test_token", base_url="https://example.invalid/v1"),
        rate_limiter=passthrough_rate_limiter(),
        account_id="test_account_id",
    )
    payload = _sse_bytes(frames)

    async def raw() -> AsyncIterator[bytes]:
        yield payload

    response = MagicMock()
    response.status_code = 200
    response.aiter_raw = raw
    response.aclose = AsyncMock()
    client = provider._client
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(return_value=response)
    try:
        return "".join(
            [
                chunk
                async for chunk in provider.stream_response(
                    _request(), request_id="req_1", reasoning=ReasoningPolicy.off()
                )
            ]
        )
    finally:
        await provider.cleanup()


async def _opencode_responses(frames: list[dict[str, Any]]) -> str:
    transport = ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        rate_limiter=passthrough_rate_limiter(),
    )
    payload = _sse_bytes(frames)

    def upstream(_request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield payload

        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    request = _request()
    reasoning = ReasoningPolicy.off()
    body, headers = transport.build_body(
        request, reasoning=reasoning, max_output_tokens=64
    )
    try:
        return "".join(
            [
                event
                async for event in transport.stream(
                    request,
                    input_tokens=0,
                    reasoning=reasoning,
                    body=body,
                    headers=headers,
                    surface_label="responses",
                )
            ]
        )
    finally:
        await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "door", [_chatgpt_oauth, _opencode_responses], ids=["chatgpt_oauth", "opencode"]
)
async def test_each_responses_provider_reports_tool_use_after_a_call(door: Any) -> None:
    sse = await door([*_CALL, _COMPLETED])
    assert _stop_reason(sse) == "tool_use"
    assert '"name": "Bash"' in sse


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "door", [_chatgpt_oauth, _opencode_responses], ids=["chatgpt_oauth", "opencode"]
)
async def test_each_responses_provider_still_ends_a_text_turn_end_turn(
    door: Any,
) -> None:
    assert _stop_reason(await door([*_TEXT, _COMPLETED])) == "end_turn"


# -- what each client shape finally receives ---------------------------------------


async def _chunks(sse: str) -> AsyncIterator[str]:
    yield sse


def _chat_request() -> OpenAIChatCompletionRequest:
    return OpenAIChatCompletionRequest.model_validate(
        {
            "model": "gpt-6-sol",
            "stream": True,
            "messages": [{"role": "user", "content": "list the files"}],
        }
    )


async def _chat_finish_reasons(sse: str) -> list[str]:
    adapter = OpenAIChatCompletionsAdapter()
    reasons: list[str] = []
    async for frame in adapter.iter_sse_from_anthropic(
        _chunks(sse), _chat_request(), completion_id="chatcmpl-1"
    ):
        if not frame.startswith("data: {"):
            continue
        choices = json.loads(frame[len("data: ") :]).get("choices", [])
        reasons.extend(c["finish_reason"] for c in choices if c.get("finish_reason"))
    return reasons


@pytest.mark.asyncio
async def test_a_chat_completions_client_is_told_tool_calls() -> None:
    assert await _chat_finish_reasons(_convert([*_CALL, _COMPLETED])) == ["tool_calls"]
    assert await _chat_finish_reasons(_convert([*_TEXT, _COMPLETED])) == ["stop"]


@pytest.mark.asyncio
async def test_a_non_streaming_client_reads_tool_use_and_tool_calls() -> None:
    message, error = await aggregate_anthropic_sse_to_message(
        _chunks(_convert([*_CALL, _COMPLETED]))
    )
    assert error is None
    assert message is not None
    assert message["stop_reason"] == "tool_use"
    completion = OpenAIChatCompletionsAdapter().completion_from_anthropic_message(
        message, _chat_request(), completion_id="chatcmpl-1"
    )
    assert completion["choices"][0]["finish_reason"] == "tool_calls"
