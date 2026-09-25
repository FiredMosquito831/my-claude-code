"""Every body the Zen free-tier harness families must leave exactly as it was.

Shared by ``test_opencode_free_tier_harness_families.py`` and by the one-off
script that wrote ``opencode_harness_families_golden.json`` **from the 7.49.0
code, before this change existed** -- which is why this module imports nothing
the change added. The test rebuilds the same bodies with the current code and
compares them to that file byte for byte, so "Claude Code is untouched" is a
comparison with the release before, not with the code under test.

Three surfaces per OpenCode case: Chat Completions (the body the OpenAI SDK
posts), Responses (``build_body``) and Messages (the bytes a fake
``{base}/messages`` received, because that door has no body builder of its
own -- the Anthropic provider inside it serialises the request).
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx

from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from tests.providers.support import passthrough_rate_limiter

REASONING = ReasoningPolicy.on()
FREE = "muse-spark-1.3-contributor-free"
PAID = "claude-sonnet-4-5"
GO_MODEL = "kimi-k2.6"

#: Claude Code's five, then the kind of company they keep on a real request:
#: built-ins OpenCode has no counterpart for, MCP tools, and names past the
#: 64-character ceiling. 105 tools in all, the size the request log shows.
CLAUDE_FIVE = ["Bash", "Read", "Edit", "Glob", "Grep"]
CLAUDE_EXTRAS = [
    "Write",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "Task",
    "NotebookEdit",
    "PowerShell",
    "mcp__exa__web_search_exa",
    "mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_console_messages",
    "mcp__plugin_chrome-devtools-mcp_chrome-devtools__performance_analyze_insight",
]
CLAUDE_FULL = [
    *CLAUDE_FIVE,
    *CLAUDE_EXTRAS,
    *(f"mcp__server_{index:02d}__tool_{index:02d}" for index in range(90)),
]

#: OpenCode 1.18.32's own catalogue, as its wire carried it (request log,
#: ``request_attempts.wire_body._names``, harness ``opencode``).
OPENCODE_NATIVE = [
    "bash",
    "edit",
    "glob",
    "grep",
    "read",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "websearch",
    "write",
]


def tool_request(
    model: str,
    names: list[str],
    *,
    history: list[str] | None = None,
    system: str = "You are Claude Code, Anthropic's official CLI for Claude.",
) -> MessagesRequest:
    """One streamed request carrying ``names``, with ``history`` replayed."""

    messages: list[dict[str, Any]] = [{"role": "user", "content": "list the files"}]
    for index, name in enumerate(history or ()):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{index:02d}",
                        "name": name,
                        "input": {"command": "ls"},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{index:02d}",
                        "content": "a.txt",
                    }
                ],
            }
        )
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 64,
        "stream": True,
        "system": system,
        "messages": messages,
    }
    if names:
        payload["tools"] = [
            {
                "name": name,
                "description": f"the {name} tool",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
            for name in names
        ]
    return MessagesRequest.model_validate(payload)


def opencode_provider(provider_id: str) -> Any:
    return create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )


_MESSAGES_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"type":"message","role":"assistant","model":"m","content":[],'
    '"stop_reason":null,"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":'
    '{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


async def _messages_wire_body(provider: Any, request: MessagesRequest) -> Any:
    seen: list[httpx.Request] = []

    def upstream(outbound: httpx.Request) -> httpx.Response:
        seen.append(outbound)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_MESSAGES_SSE.encode(),
            request=outbound,
        )

    transport = provider._messages
    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        async for _event in transport.stream(
            request, input_tokens=3, reasoning=REASONING
        ):
            pass
    finally:
        await inner._client.aclose()
    return json.loads(seen[0].content)


def opencode_bodies(provider_id: str, request: MessagesRequest) -> dict[str, Any]:
    """The body this request produces on each of the three OpenCode doors."""

    provider = opencode_provider(provider_id)
    responses, _headers = provider._responses.build_body(
        request, reasoning=REASONING, max_output_tokens=64
    )
    return {
        "chat": provider._build_request_body(request, reasoning=REASONING),
        "responses": responses,
        "messages": asyncio.run(_messages_wire_body(provider, request)),
    }


def golden_cases() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every (case, bodies) pair this series must leave byte-identical.

    Claude Code on a free model (the whole point), a Claude Code sub-request
    and a tool-less side request, then requests that *do* carry OpenCode's own
    spellings but go somewhere the free-tier catalogue never applies: a paid
    Zen model, an OpenCode Go model, NVIDIA NIM and ChatGPT OAuth.
    """

    claude = tool_request(FREE, CLAUDE_FULL, history=["Bash", "Read", CLAUDE_FULL[-1]])
    yield "claude_code_free", opencode_bodies("opencode", claude)
    yield (
        "claude_code_sub_request_free",
        opencode_bodies("opencode", tool_request(FREE, ["Bash"])),
    )
    yield "tool_less_free", opencode_bodies("opencode", tool_request(FREE, []))
    native_paid = tool_request(PAID, OPENCODE_NATIVE, history=["bash"])
    yield "opencode_native_paid_zen", opencode_bodies("opencode", native_paid)
    native_go = tool_request(GO_MODEL, OPENCODE_NATIVE, history=["bash"])
    yield "opencode_native_go", opencode_bodies("opencode_go", native_go)
    other = tool_request("some-model", [*OPENCODE_NATIVE, *CLAUDE_FIVE])
    yield (
        "nvidia_nim",
        {
            "chat": build_nim_request_body(
                other, NimSettings(), reasoning=REASONING, provider_id="nvidia_nim"
            )
        },
    )
    yield (
        "chatgpt_oauth",
        {"responses": build_chatgpt_oauth_request_body(other, reasoning=REASONING)},
    )


def canonical(value: Any) -> str:
    """The exact bytes compared: key order kept, nothing normalised away."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
