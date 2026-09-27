"""A tool call a model writes as text comes back under the client's name.

Some Chat Completions models write a call as text -- ``● <function=read>``
with ``<parameter=…>`` lines -- instead of a structured ``tool_calls`` delta,
and :class:`HeuristicToolParser` turns that text into a ``tool_use`` block.
The model can only name the tool by the spelling it was shown on the wire:
an alias for a name a Chat host cannot take (> 64 characters, or outside
``[A-Za-z0-9_-]``), or OpenCode's own spelling on its free tier (``read`` for
Claude Code's ``Read``). Structured calls have been decoded back since 7.47.1;
until 7.58.2 a text-written call was not, so Claude Code would have received
``read`` -- a tool it does not have -- or an alias instead of its MCP name.

These drive the real ``stream_response`` of every Chat provider over a real
``AsyncOpenAI`` client whose transport records the outbound body and answers
with the calls written as text, by the wire names it was sent. No live
upstream is contacted.
"""

import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic.openai_tool_names import OpenAIToolNameCodec
from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
)
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.openai_chat import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.tool_calls import (
    OpenAIToolCallAssembler,
    iter_heuristic_tool_use_sse,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.sse_replay import _build_provider, _config
from tests.providers.support import passthrough_rate_limiter

#: 70 characters: the shape Claude Code gives a plugin's MCP tool.
LONG_MCP = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_console_messages"
#: Short, but not a portable Chat name.
DOTTED = "server.tool"
#: Portable; sent and answered unchanged.
SHORT = "Read"

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


def _frame(delta: dict[str, Any], finish: str | None = None) -> str:
    chunk = {
        "id": "chatcmpl-text-call",
        "object": "chat.completion.chunk",
        "created": 1757000000,
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _text_call(name: str, arguments: dict[str, str]) -> str:
    lines = [f"● <function={name}>"]
    lines.extend(
        f"<parameter={key}>{value}</parameter>" for key, value in arguments.items()
    )
    return "\n".join(lines) + "\n"


def _answer(calls: list[tuple[str, dict[str, str]]]) -> bytes:
    """One streamed turn that writes every call as text, one delta per call.

    One delta per call, not an arbitrary cut: the parser has a separate,
    pre-existing limit (a delta that ends right after ``<function=x>\\n``
    emits the call before its parameters arrive -- the same on v7.58.0), and
    this file is about the name, not about that.
    """

    frames = [_frame({"role": "assistant", "content": ""})]
    frames.append(_frame({"content": "Working on it.\n"}))
    frames.extend(_frame({"content": _text_call(n, a)}) for n, a in calls)
    frames.append(_frame({}, "stop"))
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _provider(provider_id: str) -> Any:
    if provider_id == "nvidia_nim":
        return NvidiaNimProvider(
            _config(),
            nim_settings=NimSettings(),
            rate_limiter=passthrough_rate_limiter(),
        )
    return _build_provider(provider_id)


async def _round_trip(
    provider_id: str,
    request: Any,
    answer_for: Any,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Return the wire names sent, the tool_use blocks received, and the SSE."""

    provider = _provider(provider_id)
    wire: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        wire.extend(tool["function"]["name"] for tool in body.get("tools", []))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_answer(answer_for(wire)),
            request=http_request,
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
                request, 11, request_id="req-text-call"
            )
        ]
    finally:
        await provider.cleanup()
    events = parse_sse_text("".join(frames))
    assert_anthropic_stream_contract(events)
    starts = [
        event.data["content_block"]
        for event in events
        if event.event == "content_block_start"
        and event.data["content_block"]["type"] == "tool_use"
    ]
    inputs = [
        event.data["delta"]["partial_json"]
        for event in events
        if event.event == "content_block_delta"
        and event.data["delta"].get("type") == "input_json_delta"
    ]
    return wire, starts, inputs


def _request(tools: list[dict[str, Any]], model: str = "fixture-model") -> Any:
    return make_messages_request(
        model,
        messages=[{"role": "user", "content": "go"}],
        tools=tools,
        thinking={"enabled": False},
        stream=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", CHAT_PROVIDERS)
async def test_an_aliased_name_written_as_text_comes_back_under_the_clients_name(
    provider_id: str,
) -> None:
    request = _request(
        [
            {"name": SHORT, "description": "read", "input_schema": _SCHEMA},
            {"name": LONG_MCP, "description": "long", "input_schema": _SCHEMA},
            {"name": DOTTED, "description": "dotted", "input_schema": _SCHEMA},
        ]
    )
    wire, starts, inputs = await _round_trip(
        provider_id,
        request,
        lambda wire: [(name, {"path": "a.txt"}) for name in wire],
    )
    # Outbound, unchanged: the long and the dotted name are aliased on the wire.
    assert wire[0] == SHORT
    assert wire[1] != LONG_MCP and len(wire[1]) <= 64
    assert wire[2] != DOTTED
    # Inbound: the calls the model wrote as text name the client's tools.
    assert [block["name"] for block in starts] == [SHORT, LONG_MCP, DOTTED]
    # Only the name is translated; the arguments are exactly as written.
    assert [json.loads(raw) for raw in inputs] == [{"path": "a.txt"}] * 3


CLAUDE_CODE_TOOLS = [
    {
        "name": "Bash",
        "description": "Run a command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        },
    },
    {
        "name": "Read",
        "description": "Read a file.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
        },
    },
    {
        "name": "Edit",
        "description": "Edit a file.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
        },
    },
    {
        "name": "Glob",
        "description": "Find files.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
        },
    },
    {
        "name": "Grep",
        "description": "Search files.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
        },
    },
]


@pytest.mark.asyncio
async def test_opencode_free_tier_spellings_written_as_text_come_back_as_claudes() -> (
    None
):
    """``read`` is OpenCode's name for Claude Code's ``Read`` on the free tier."""

    request = _request(CLAUDE_CODE_TOOLS, model="mimo-v2.6-flash-free")
    wire, starts, inputs = await _round_trip(
        "opencode",
        request,
        lambda _wire: [
            ("read", {"file_path": "/tmp/x.txt"}),
            ("bash", {"command": "ls"}),
            ("frobnicate", {"anything": "1"}),
        ],
    )
    assert wire == ["bash", "read", "edit", "glob", "grep"]
    # Catalogue spellings decode; a name nobody declared stays as written.
    assert [block["name"] for block in starts] == ["Read", "Bash", "frobnicate"]
    assert [json.loads(raw) for raw in inputs] == [
        {"file_path": "/tmp/x.txt"},
        {"command": "ls"},
        {"anything": "1"},
    ]


CODEX_TOOLS = [
    {
        "name": "exec_command",
        "description": "Run a command.",
        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    },
    {
        "name": "apply_patch",
        "description": "Apply a patch.",
        "input_schema": {"type": "object", "properties": {"input": {"type": "string"}}},
    },
    {
        "name": "write_stdin",
        "description": "Write to a session.",
        "input_schema": {"type": "object", "properties": {"chars": {"type": "string"}}},
    },
]


@pytest.mark.asyncio
async def test_a_stand_in_written_as_text_reaches_the_client_as_the_stand_in() -> None:
    """Codex has no ``read``; MCC offers a stand-in by that name, and a call to
    it reaches the client unchanged, exactly as a structured call to it does."""

    request = _request(CODEX_TOOLS, model="mimo-v2.6-flash-free")
    wire, starts, _inputs = await _round_trip(
        "opencode",
        request,
        lambda _wire: [("bash", {"cmd": "ls"}), ("read", {"path": "a"})],
    )
    assert "read" in wire and "bash" in wire
    assert [block["name"] for block in starts] == ["exec_command", "read"]


# --------------------------------------------------------------------------
# The seam itself, and the structured path it borrows its decoder from
# --------------------------------------------------------------------------


def _codec() -> OpenAIToolNameCodec:
    return OpenAIToolNameCodec.from_names([SHORT, LONG_MCP, DOTTED])


def _emit(
    tool_use: dict[str, Any], codec: OpenAIToolNameCodec | None
) -> dict[str, Any]:
    ledger = AnthropicStreamLedger("msg_1", "m", input_tokens=0)
    events = parse_sse_text(
        "".join(iter_heuristic_tool_use_sse(ledger, tool_use, tool_names=codec))
    )
    start = next(e for e in events if e.event == "content_block_start")
    return start.data["content_block"]


def test_the_text_path_decodes_with_the_structured_paths_own_codec() -> None:
    codec = _codec()
    assembler = OpenAIToolCallAssembler(tool_names=codec)
    assert assembler.tool_names is codec
    alias = codec.encode(LONG_MCP)
    block = _emit(
        {"type": "tool_use", "id": "toolu_1", "name": alias, "input": {"path": "a"}},
        assembler.tool_names,
    )
    assert block["name"] == LONG_MCP
    assert block["id"] == "toolu_1"


def test_without_a_codec_the_text_path_is_what_it_always_was() -> None:
    alias = _codec().encode(LONG_MCP)
    block = _emit(
        {"type": "tool_use", "id": "toolu_1", "name": alias, "input": {}}, None
    )
    assert block["name"] == alias


def test_a_decoded_task_call_still_runs_in_the_foreground() -> None:
    codec = OpenAIToolNameCodec.from_names(["Task"], catalogue={"Task": "task"})
    tool_use = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "task",
        "input": {"prompt": "x"},
    }
    block = _emit(tool_use, codec)
    assert block["name"] == "Task"
    assert tool_use["input"]["run_in_background"] is False
