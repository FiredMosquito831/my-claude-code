"""An empty inline system message is left out on OpenAI-format routes (7.103.2).

Claude Code (2.1.296 onward) sends, after a compaction, a resume or an effort
change, a system message whose only payload is the per-turn effort::

    {"role": "system", "content": [], "output_config": {"effort": "high"}}

Anthropic accepts it. Until 7.103.2 the shared OpenAI-format converter refused
it ("... requires an inline Anthropic system message to contain text."), the
refusal is ``invalid_request``, and that kind ends the route -- so the whole
fallback chain stopped (23 Analytics rows in 30 days). Decisions of
2026-10-21 13:21, all pinned here:

* Q1: the message is left out on every OpenAI-format dialect, and the body is
  exactly the body of the same conversation without it;
* Q2: the refusal stays, word for word, when nothing else would be sent;
* Q3: the native Anthropic builder still renders it as it always did;
* Q4: ``params.wire`` says ``empty_system_messages_dropped`` -- counts only --
  and only on a request where one was left out.

Every other system message -- a string, even ``""``, and a list of text
blocks, even empty ones -- converts exactly as before.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic import (
    EMPTY_SYSTEM_MESSAGES_DROPPED,
    MessagesRequest,
    OpenAIConversionError,
    ReasoningReplayMode,
    build_base_request_body,
    empty_system_messages_marker,
    empty_system_messages_note,
)
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace, is_reasoning_key
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.deepseek.compat import build_deepseek_request_body
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from my_claude_code.providers.openai_chat import (
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import build_responses_request_body
from my_claude_code.providers.recovery import LearnedFactStore
from tests.providers.support import passthrough_rate_limiter, profiled_provider

REFUSAL = (
    "OpenAI chat conversion requires an inline Anthropic system message to "
    "contain text."
)
NOTE_ONE = "left out 1 empty system message (Claude Code's per-turn effort marker)"
REASONING = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
OFF = ReasoningPolicy.off()

MARKER: dict[str, Any] = {
    "role": "system",
    "content": [],
    "output_config": {"effort": "high"},
}
SUMMARY: dict[str, Any] = {"role": "user", "content": "invented compact summary"}
REPLY: dict[str, Any] = {
    "role": "assistant",
    "content": [{"type": "text", "text": "invented reply"}],
}
ASK: dict[str, Any] = {
    "role": "user",
    "content": [{"type": "text", "text": "invented next ask"}],
}
TOOLS = [
    {
        "name": "Bash",
        "description": "Run a command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
        },
    }
]


def _tool_run() -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.py"}
            ],
        },
    ]


def _request(
    messages: list[dict[str, Any]], model: str = "invented-model", **extra: Any
) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 256,
            "system": [{"type": "text", "text": "invented identity"}],
            "tools": TOOLS,
            "messages": messages,
            **extra,
        }
    )


def _without_markers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [message for message in messages if message is not MARKER]


#: Every position the marker was seen in or is reachable at (spec §1.3, §3 A):
#: right after the compact summary, trailing after the last user turn, after a
#: tool_result run, and twice.
LAYOUTS: dict[str, list[dict[str, Any]]] = {
    "after_the_summary": [SUMMARY, MARKER, REPLY, ASK],
    "trailing": [SUMMARY, REPLY, ASK, MARKER],
    "after_a_tool_result_run": [SUMMARY, *_tool_run(), MARKER, ASK],
    "twice": [SUMMARY, MARKER, REPLY, ASK, MARKER],
}


def _wire(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------
# Q1: left out, and the body is the body without it -- on every dialect
# --------------------------------------------------------------------------


def _chat(request: MessagesRequest) -> dict[str, Any]:
    return build_base_request_body(request)


def _chat_funnel(request: MessagesRequest) -> dict[str, Any]:
    policy = OpenAIChatRequestPolicy(
        provider_name="TEST", reasoning_replay=ReasoningReplayMode.REASONING
    )
    return build_openai_chat_request_body(request, reasoning=REASONING, policy=policy)


def _deepseek(request: MessagesRequest) -> dict[str, Any]:
    return build_deepseek_request_body(request, reasoning=REASONING)


def _nim(request: MessagesRequest) -> dict[str, Any]:
    return build_nim_request_body(
        request, NimSettings(), reasoning=REASONING, provider_id="nvidia_nim"
    )


def _responses(request: MessagesRequest) -> dict[str, Any]:
    return build_responses_request_body(request, reasoning=REASONING)


def _chatgpt(request: MessagesRequest) -> dict[str, Any]:
    return build_chatgpt_oauth_request_body(request, reasoning=REASONING)


BUILDERS = {
    "chat": _chat,
    "chat_funnel": _chat_funnel,
    "deepseek": _deepseek,
    "nvidia_nim": _nim,
    "responses": _responses,
    "chatgpt_oauth": _chatgpt,
}


@pytest.mark.parametrize("layout", list(LAYOUTS))
@pytest.mark.parametrize("builder", list(BUILDERS))
def test_the_body_is_the_conversation_without_the_marker(
    builder: str, layout: str
) -> None:
    messages = LAYOUTS[layout]
    build = BUILDERS[builder]
    with_marker = build(_request(messages))
    without = build(_request(_without_markers(messages)))
    assert _wire(with_marker) == _wire(without)
    assert "output_config" not in _wire(with_marker)


def test_no_empty_message_reaches_the_chat_wire() -> None:
    body = _chat(_request(LAYOUTS["after_the_summary"]))
    assert [message["role"] for message in body["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert all(message["content"] for message in body["messages"])


def test_a_marker_without_output_config_is_left_out_too() -> None:
    bare = {"role": "system", "content": []}
    body = _chat(_request([SUMMARY, bare, REPLY, ASK]))
    assert _wire(body) == _wire(_chat(_request([SUMMARY, REPLY, ASK])))


# --------------------------------------------------------------------------
# Every system message WITH text converts exactly as it always did
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "",
        "invented plain note",
        [{"type": "text", "text": ""}],
        [{"type": "text", "text": ""}, {"type": "text", "text": ""}],
        [{"type": "text", "text": "SessionStart: invented hook"}],
    ],
)
def test_a_system_message_with_text_is_still_sent(content: Any) -> None:
    message = {"role": "system", "content": content, "output_config": {"effort": "low"}}
    body = _chat(_request([SUMMARY, message, REPLY, ASK]))
    # Converted into the user turn it follows, as before 7.103.2: two user
    # messages coalesce, so the summary's text is followed by this one's.
    first_user = body["messages"][1]
    assert first_user["role"] == "user"
    assert first_user["content"] != SUMMARY["content"]
    assert first_user["content"].startswith(SUMMARY["content"] + "\n\n")
    assert empty_system_messages_note(_request([SUMMARY, message]).messages) == {}


def test_a_non_text_system_block_keeps_its_own_refusal() -> None:
    image = {
        "type": "image",
        "source": {"type": "url", "url": "https://example.invalid/a.png"},
    }
    with pytest.raises(OpenAIConversionError, match="content block 'image'"):
        _chat(_request([SUMMARY, {"role": "system", "content": [image]}, ASK]))


# --------------------------------------------------------------------------
# Q2: nothing left to send keeps the refusal, word for word
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "messages",
    [
        [MARKER],
        [MARKER, MARKER],
        [{"role": "system", "content": []}],
        [MARKER, {"role": "user", "content": []}],
    ],
    ids=["one", "two", "bare", "with_an_empty_user_turn"],
)
@pytest.mark.parametrize("builder", list(BUILDERS))
def test_nothing_left_to_send_is_still_refused(
    builder: str, messages: list[dict[str, Any]]
) -> None:
    # A top-level system prompt is present and still nothing is sent: the
    # transcript itself is empty.
    with pytest.raises((OpenAIConversionError, InvalidRequestError)) as raised:
        BUILDERS[builder](_request(messages))
    assert str(raised.value) == REFUSAL


def test_the_remaining_refusal_is_still_an_invalid_request() -> None:
    """The class the route reads is unchanged: ``invalid_request``."""
    with pytest.raises(InvalidRequestError) as raised:
        _chat_funnel(_request([MARKER]))
    assert raised.value.kind is FailureKind.INVALID_REQUEST
    assert raised.value.status_code == 400
    assert str(raised.value) == REFUSAL


def test_an_empty_transcript_with_no_marker_is_not_newly_refused() -> None:
    """The guard is about what was left out; it adds no refusal of its own."""
    body = _chat(_request([{"role": "user", "content": []}]))
    assert body["messages"] == [{"role": "system", "content": "invented identity"}]


# --------------------------------------------------------------------------
# Q3: the native Anthropic builder is untouched
# --------------------------------------------------------------------------


def test_the_native_builder_still_renders_the_marker_as_before() -> None:
    body = build_anthropic_messages_body(
        _request(LAYOUTS["after_the_summary"], model="claude-invented"),
        reasoning=OFF,
    )
    assert body["messages"][1] == {"role": "user", "content": []}
    notes: dict[str, str] = {}
    build_anthropic_messages_body(
        _request(LAYOUTS["after_the_summary"], model="claude-invented"),
        reasoning=OFF,
        wire_notes=notes,
    )
    assert EMPTY_SYSTEM_MESSAGES_DROPPED not in notes


# --------------------------------------------------------------------------
# Q4: the note -- counts only, only when something was left out
# --------------------------------------------------------------------------


def test_the_note_counts_what_was_left_out() -> None:
    assert empty_system_messages_note(_request(LAYOUTS["trailing"]).messages) == {
        EMPTY_SYSTEM_MESSAGES_DROPPED: NOTE_ONE
    }
    assert empty_system_messages_note(_request(LAYOUTS["twice"]).messages) == {
        EMPTY_SYSTEM_MESSAGES_DROPPED: (
            "left out 2 empty system messages (Claude Code's per-turn effort marker)"
        )
    }


def test_no_marker_means_no_note() -> None:
    assert empty_system_messages_note(_request([SUMMARY, REPLY, ASK]).messages) == {}
    assert empty_system_messages_marker({}) == {}
    assert empty_system_messages_marker(None) == {}
    assert empty_system_messages_marker({"history_thinking_omitted": "x"}) == {}


def test_the_note_is_never_read_as_a_reasoning_instruction() -> None:
    assert not is_reasoning_key(EMPTY_SYSTEM_MESSAGES_DROPPED)


def test_the_responses_build_counts_it_into_wire_notes() -> None:
    notes: dict[str, str] = {}
    build_responses_request_body(
        _request(LAYOUTS["after_the_summary"]), reasoning=REASONING, wire_notes=notes
    )
    assert notes == {EMPTY_SYSTEM_MESSAGES_DROPPED: NOTE_ONE}
    untouched: dict[str, str] = {}
    build_responses_request_body(
        _request([SUMMARY, REPLY, ASK]), reasoning=REASONING, wire_notes=untouched
    )
    assert untouched == {}


# --------------------------------------------------------------------------
# The note reaches params.wire on every sender; the body never carries it
# --------------------------------------------------------------------------

_CHAT_SSE = (
    'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},'
    '"finish_reason":null}]}\n\n'
    'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
).encode()


def _config() -> ProviderConfig:
    return ProviderConfig(
        api_key="test-key",
        base_url="http://chat.invalid/v1",
        rate_limit=1_000_000,
        rate_window=60,
        commit_holdback_seconds=0.0,
        commit_holdback_chars=0,
    )


def _recording_client(sent: list[dict[str, Any]]) -> AsyncOpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_CHAT_SSE,
            request=request,
        )

    return AsyncOpenAI(
        api_key="test-key",
        base_url="http://chat.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _chat_provider(name: str) -> tuple[Any, OpenAIChatProvider]:
    """(what to call ``stream_response`` on, the Chat provider that sends)."""
    limiter = passthrough_rate_limiter()
    if name == "deepseek":
        provider = DeepSeekProvider(_config(), rate_limiter=limiter)
        return provider, provider
    if name == "mistral":
        provider = MistralProvider(_config(), rate_limiter=limiter)
        return provider, provider
    if name == "commandcode":
        outer = CommandCodeProvider(_config(), rate_limiter=limiter)
        return outer, outer._openai
    provider = profiled_provider(name, _config(), rate_limiter=limiter)
    return provider, provider


def _params(trace: Any) -> dict[str, Any]:
    return trace.requests[max(trace.requests)].params


async def _send(name: str, messages: list[dict[str, Any]], model: str) -> Any:
    outer, sender = _chat_provider(name)
    sent: list[dict[str, Any]] = []
    sender._client = _recording_client(sent)
    trace = install_wire_trace()
    try:
        events = [
            event
            async for event in outer.stream_response(
                _request(messages, model=model), reasoning=OFF
            )
        ]
    finally:
        await outer.cleanup()
    assert any('"ok"' in event for event in events)
    return sent, _params(trace)


CHAT_SENDERS = {
    "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-large-latest",
    "commandcode": "deepseek/deepseek-v4.1-flash",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CHAT_SENDERS))
async def test_every_chat_sender_records_the_note_beside_the_body(name: str) -> None:
    model = CHAT_SENDERS[name]
    messages = LAYOUTS["after_the_summary"]
    sent, params = await _send(name, messages, model)
    plain_sent, plain_params = await _send(name, _without_markers(messages), model)

    assert sent == plain_sent, "the same bytes as the conversation without it"
    assert params[EMPTY_SYSTEM_MESSAGES_DROPPED] == NOTE_ONE
    assert EMPTY_SYSTEM_MESSAGES_DROPPED not in sent[0]
    assert EMPTY_SYSTEM_MESSAGES_DROPPED not in plain_params
    assert {k: v for k, v in params.items() if k != EMPTY_SYSTEM_MESSAGES_DROPPED} == (
        plain_params
    )


@pytest.mark.asyncio
async def test_a_chat_request_with_nothing_left_out_records_no_note() -> None:
    _sent, params = await _send("groq", [SUMMARY, REPLY, ASK], CHAT_SENDERS["groq"])
    assert EMPTY_SYSTEM_MESSAGES_DROPPED not in params


@pytest.mark.asyncio
async def test_the_chat_preflight_no_longer_refuses_the_marker() -> None:
    provider = profiled_provider("groq", _config())
    provider.preflight_stream(_request(LAYOUTS["after_the_summary"]), reasoning=OFF)
    with pytest.raises(InvalidRequestError, match="contain text"):
        provider.preflight_stream(_request([MARKER]), reasoning=OFF)
    await provider.cleanup()


def _responses_accepted() -> httpx.Response:
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
async def test_the_responses_transport_records_the_note_beside_the_body() -> None:
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
        return _responses_accepted()

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    trace = install_wire_trace()
    request = _request(LAYOUTS["after_the_summary"], model="invented-responses")
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
    assert _params(trace)[EMPTY_SYSTEM_MESSAGES_DROPPED] == NOTE_ONE
    assert EMPTY_SYSTEM_MESSAGES_DROPPED not in sent[0]


@pytest.mark.asyncio
async def test_chatgpt_oauth_records_the_note_beside_the_body() -> None:
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

    request = _request(LAYOUTS["after_the_summary"], model="gpt-5.6-terra")
    chunks = [
        chunk async for chunk in provider.stream_response(request, reasoning=REASONING)
    ]

    sent = provider._send_stream_request.await_args_list[0].kwargs["body"]
    assert any("ok" in chunk for chunk in chunks)
    assert EMPTY_SYSTEM_MESSAGES_DROPPED not in sent
    assert _params(trace)[EMPTY_SYSTEM_MESSAGES_DROPPED] == NOTE_ONE
    assert _wire(sent) == _wire(
        build_chatgpt_oauth_request_body(
            _request(_without_markers(LAYOUTS["after_the_summary"]), model="gpt-5.6-terra"),
            reasoning=REASONING,
        )
    )
