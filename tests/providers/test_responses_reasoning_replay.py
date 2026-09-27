"""The Responses surface replays no earlier thinking, and says so (7.58.1).

Until 7.58.1 ``build_responses_request_body`` replayed every earlier thinking
block into ``input`` as literal ``<think>`` output text, and the models behind
it -- ChatGPT OAuth and OpenCode's Responses-only ``muse-spark`` -- wrote their
next turns in the same format: 1,308 logged answers carried literal tags.

What is pinned here:

* no ``<think>`` in any ``input`` item, on both builders (ChatGPT's own and
  the OpenCode transport's), wherever the thinking sat in the history;
* the history is otherwise the history: text, tool calls, tool results and
  images all still go, and a thinking-only turn leaves no empty message item;
* ``params.wire`` carries ``history_thinking_omitted`` -- counts only, never
  the text -- on every attempt row of both senders, and nothing when the
  history held no thinking.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.core.anthropic.conversion import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import (
    HISTORY_THINKING_OMITTED,
    RESPONSES_REASONING_REPLAY,
    build_responses_request_body,
    history_thinking_marker,
)
from my_claude_code.providers.recovery import LearnedFactStore
from tests.providers.support import passthrough_rate_limiter

REASONING = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
SECRET_THOUGHT = "the user's secret plan is to refactor utils.py"
_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


def _image() -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG},
    }


def _history() -> list[dict[str, Any]]:
    """Thinking in every position a history can hold it."""

    return [
        {"role": "user", "content": [{"type": "text", "text": "Look:"}, _image()]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": SECRET_THOUGHT, "signature": "s1"},
                {"type": "text", "text": "Reading it."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Read",
                    "input": {"file_path": "utils.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": "def f(): pass"}, _image()],
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Only a thought.", "signature": "s2"}
            ],
        },
        {"role": "user", "content": "Go on."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
                {"type": "thinking", "thinking": "After the call.", "signature": "s3"},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "a.py"}
            ],
        },
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "String-turn reasoning.",
        },
        {"role": "user", "content": "Thanks."},
    ]


TOOLS = [
    {
        "name": "Read",
        "description": "Read a file.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
        },
    },
    {
        "name": "Bash",
        "description": "Run a command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        },
    },
]


def _request(
    messages: list[dict[str, Any]] | None = None, model: str = "gpt-5.6-terra"
) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 1024,
            "messages": messages if messages is not None else _history(),
            "tools": TOOLS,
        }
    )


def _wire(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_the_responses_surface_replays_no_reasoning() -> None:
    assert RESPONSES_REASONING_REPLAY is ReasoningReplayMode.DISABLED


@pytest.mark.parametrize("builder", ["chatgpt_oauth", "generic"])
def test_no_input_item_carries_a_think_tag_or_the_thought(builder: str) -> None:
    if builder == "chatgpt_oauth":
        body = build_chatgpt_oauth_request_body(_request(), reasoning=REASONING)
    else:
        body = build_responses_request_body(
            _request(), reasoning=REASONING, tool_name_max_length=64
        )
    for item in body["input"]:
        assert "<think>" not in _wire(item)
        assert "</think>" not in _wire(item)
    wire = _wire(body["input"])
    for thought in (
        SECRET_THOUGHT,
        "Only a thought.",
        "After the call.",
        "String-turn reasoning.",
    ):
        assert thought not in wire


def test_the_rest_of_the_history_still_goes_in_order() -> None:
    body = build_chatgpt_oauth_request_body(_request(), reasoning=REASONING)
    shape = [
        (item["type"], item.get("role"), item.get("name") or item.get("call_id"))
        for item in body["input"]
    ]
    assert shape == [
        ("message", "user", None),
        ("message", "assistant", None),
        ("function_call", None, "Read"),
        ("function_call_output", None, "toolu_1"),
        # The thinking-only turn leaves no empty assistant item behind.
        ("message", "user", None),
        ("function_call", None, "Bash"),
        ("function_call_output", None, "toolu_2"),
        ("message", "assistant", None),
        ("message", "user", None),
    ]
    items = body["input"]
    assert items[0]["content"][1]["type"] == "input_image"
    assert items[1]["content"] == [{"type": "output_text", "text": "Reading it."}]
    assert any(part.get("type") == "input_image" for part in items[3]["output"])
    assert items[7]["content"] == [{"type": "output_text", "text": "done"}]


def test_what_was_left_out_is_counted_into_wire_notes() -> None:
    notes: dict[str, str] = {}
    build_responses_request_body(_request(), reasoning=REASONING, wire_notes=notes)
    chars = sum(
        len(text)
        for text in (
            SECRET_THOUGHT,
            "Only a thought.",
            "After the call.",
            "String-turn reasoning.",
        )
    )
    assert notes == {
        HISTORY_THINKING_OMITTED: (
            f"omitted 4 earlier thinking blocks ({chars} chars) from input; "
            "the Responses surface replays no reasoning as text"
        )
    }
    assert SECRET_THOUGHT not in notes[HISTORY_THINKING_OMITTED]


def test_a_history_without_thinking_records_nothing() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "hello"},
            ],
        },
        {"role": "user", "content": "bye"},
    ]
    notes: dict[str, str] = {}
    build_responses_request_body(
        _request(messages), reasoning=REASONING, wire_notes=notes
    )
    assert notes == {}
    assert history_thinking_marker(notes) == {}
    assert history_thinking_marker(None) == {}


def test_one_block_is_spelled_in_the_singular() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "abc", "signature": "s"}],
        },
        {"role": "user", "content": "bye"},
    ]
    notes: dict[str, str] = {}
    build_responses_request_body(
        _request(messages), reasoning=REASONING, wire_notes=notes
    )
    assert notes[HISTORY_THINKING_OMITTED].startswith(
        "omitted 1 earlier thinking block (3 chars)"
    )


# --------------------------------------------------------------------------
# The record reaches params.wire on both senders
# --------------------------------------------------------------------------


def _params(trace: Any) -> dict[str, Any]:
    return trace.requests[max(trace.requests)].params


def _accepted(_body: dict[str, Any]) -> httpx.Response:
    frames = [
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.completed", "response": {"output": []}},
    ]
    payload = "".join(
        f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames
    ).encode()

    async def _stream():
        yield payload

    return httpx.Response(
        200, content=_stream(), headers={"content-type": "text/event-stream"}
    )


@pytest.mark.asyncio
async def test_the_opencode_transport_records_the_omission_beside_the_body() -> None:
    transport = ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        rate_limiter=passthrough_rate_limiter(),
        memory=LearnedFactStore().memory_for("muse_gateway"),
    )
    sent: list[dict[str, Any]] = []

    def _handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return _accepted(sent[-1])

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    trace = install_wire_trace()
    request = _request(model="muse-spark-1.3-contributor-free")
    notes: dict[str, str] = {}
    body, headers = transport.build_body(
        request, reasoning=REASONING, max_output_tokens=512, wire_notes=notes
    )
    events = [
        event
        async for event in transport.stream(
            request,
            input_tokens=0,
            reasoning=REASONING,
            body=body,
            headers=headers,
            surface_label="responses",
            wire_notes=notes,
        )
    ]
    await transport.aclose()

    assert any("ok" in event for event in events)
    assert "<think>" not in _wire(sent[0])
    marker = _params(trace)[HISTORY_THINKING_OMITTED]
    assert marker.startswith("omitted 4 earlier thinking blocks")
    # Recorded beside the body, never sent in it.
    assert HISTORY_THINKING_OMITTED not in sent[0]


@pytest.mark.asyncio
async def test_chatgpt_oauth_records_the_omission_beside_the_body() -> None:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=CHATGPT_OAUTH_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    trace = install_wire_trace()

    async def _raw_stream():
        yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        yield b'data: {"type":"response.completed","response":{}}\n\n'

    response = MagicMock(status_code=200)
    response.aiter_raw = _raw_stream
    response.aclose = AsyncMock()
    provider._send_stream_request = AsyncMock(return_value=response)

    chunks = [
        chunk
        async for chunk in provider.stream_response(_request(), reasoning=REASONING)
    ]

    sent = provider._send_stream_request.await_args_list[0].kwargs["body"]
    assert any("ok" in chunk for chunk in chunks)
    assert "<think>" not in _wire(sent)
    assert HISTORY_THINKING_OMITTED not in sent
    assert _params(trace)[HISTORY_THINKING_OMITTED].startswith(
        "omitted 4 earlier thinking blocks"
    )


def test_the_marker_is_never_read_as_a_reasoning_instruction() -> None:
    """``reasoning_emitted`` must mean what the body sent, not what the note says."""

    from my_claude_code.core.wire_capture import is_reasoning_key, reasoning_was_emitted

    assert not is_reasoning_key(HISTORY_THINKING_OMITTED)
    off = build_chatgpt_oauth_request_body(_request(), reasoning=ReasoningPolicy.off())
    notes: dict[str, str] = {}
    build_responses_request_body(
        _request(), reasoning=ReasoningPolicy.off(), wire_notes=notes
    )
    assert not reasoning_was_emitted({**off, **history_thinking_marker(notes)})
