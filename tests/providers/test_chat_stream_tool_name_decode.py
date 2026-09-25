"""A tool name aliased on the way out comes back under the client's name.

Every Chat Completions host is sent the tool catalogue through
``build_openai_chat_request_body``, which aliases any name a Chat host could
not accept -- past 64 characters, or carrying a character outside
``[A-Za-z0-9_-]`` -- into ``<readable>_<16 hex>``. Claude Code's MCP tools
routinely pass 64 (``mcp__plugin_<plugin>_<server>__<tool>``).

The model then calls the tool by the only name it was shown: the alias. Until
7.47.1 the streamed ``tool_calls`` were decoded only when the host also had a
free-tier catalogue (OpenCode's, 7.28.0), so on every other Chat host Claude
Code received ``tool_use`` blocks naming a tool it never declared.

These tests drive the real ``stream_response`` of every Chat provider over a
real ``AsyncOpenAI`` client whose transport records the outbound body and
answers with a recorded-shape SSE stream calling the tool by its wire name.
No live upstream is contacted.
"""

import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.openai_chat import OPENAI_CHAT_PROFILES
from tests.providers.request_factory import make_messages_request
from tests.providers.sse_replay import _build_provider, _config
from tests.providers.support import passthrough_rate_limiter

#: 70 characters: the shape Claude Code gives a plugin's MCP tool.
LONG_MCP = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_console_messages"
#: Short, but not a portable Chat name.
DOTTED = "server.tool"
#: Portable; sent and answered unchanged.
SHORT = "Read"

#: Subclasses of the Chat provider with their own constructor; every declared
#: profile is built through ``create_openai_chat_provider``.
_SUBCLASSED = (
    "cloudflare",
    "deepseek",
    "mistral",
    "open_router",
    "google_openai",
    "nvidia_nim",
)
CHAT_PROVIDERS = tuple(sorted(OPENAI_CHAT_PROFILES)) + _SUBCLASSED

_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


def _request(model: str = "fixture-model") -> Any:
    return make_messages_request(
        model,
        messages=[{"role": "user", "content": "go"}],
        tools=[
            {"name": SHORT, "description": "read", "input_schema": _SCHEMA},
            {"name": LONG_MCP, "description": "long", "input_schema": _SCHEMA},
            {"name": DOTTED, "description": "dotted", "input_schema": _SCHEMA},
        ],
        thinking={"enabled": False},
        stream=True,
    )


def _frame(delta: dict[str, Any], finish: str | None = None) -> str:
    chunk = {
        "id": "chatcmpl-l7",
        "object": "chat.completion.chunk",
        "created": 1757000000,
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _answer(wire_names: list[str]) -> bytes:
    """One streamed turn calling each wire name, the second split in pieces."""
    frames = [_frame({"role": "assistant", "content": ""})]
    for index, name in enumerate(wire_names):
        frames.append(
            _frame(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
            )
        )
        frames.extend(
            _frame({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
            for piece in ('{"pa', 'th": "a.txt"}')
        )
    frames.append(_frame({}, "tool_calls"))
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


async def _round_trip(provider_id: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Return ``{client name: wire name}`` and the tool_use blocks emitted."""
    provider = (
        NvidiaNimProvider(
            _config(),
            nim_settings=NimSettings(),
            rate_limiter=passthrough_rate_limiter(),
        )
        if provider_id == "nvidia_nim"
        else _build_provider(provider_id)
    )
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire = [tool["function"]["name"] for tool in body.get("tools", [])]
        sent.update(zip((SHORT, LONG_MCP, DOTTED), wire, strict=True))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_answer(wire),
            request=request,
        )

    provider._client = AsyncOpenAI(
        api_key="test-key",
        base_url=provider._config.base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        frames = [
            frame
            async for frame in provider.stream_response(
                _request(), 11, request_id="req-l7"
            )
        ]
    finally:
        await provider.cleanup()
    events = parse_sse_text("".join(frames))
    starts = [
        event.data["content_block"]
        for event in events
        if event.event == "content_block_start"
        and event.data["content_block"]["type"] == "tool_use"
    ]
    return sent, starts


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", CHAT_PROVIDERS)
async def test_an_aliased_tool_call_streams_back_under_the_clients_name(
    provider_id: str,
) -> None:
    sent, starts = await _round_trip(provider_id)
    # The outbound half, unchanged: the long and the dotted name are aliased,
    # the portable one is not.
    assert sent[SHORT] == SHORT
    assert sent[LONG_MCP] != LONG_MCP and len(sent[LONG_MCP]) <= 64
    assert sent[DOTTED] != DOTTED
    # The inbound half: Claude Code sees the names it declared.
    assert [block["name"] for block in starts] == [SHORT, LONG_MCP, DOTTED]
