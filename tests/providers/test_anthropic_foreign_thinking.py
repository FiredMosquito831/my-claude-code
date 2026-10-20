"""Thinking another model wrote never reaches an Anthropic-protocol host (7.102.1).

MCC opens the reasoning of every non-Anthropic model as a ``thinking`` block
with no signature, and Claude Code replays it as ``signature: ""``. Until
7.102.1 the shared Messages builder forwarded that block verbatim, Anthropic
answered ``400 messages.N.content.M: Invalid `signature` in `thinking`
block``, and the chain fell through to the next model -- 610 failed attempts
in 60 days, every one on ``anthropic_oauth``.

What is pinned here, on the outbound copy only:

* assistant ``thinking`` with an absent, non-string or blank signature is
  dropped; ``redacted_thinking`` whose ``data`` is a JSON object or array
  (MCC's own OpenRouter wrapper) is dropped;
* everything else stays, in its order: every non-blank signature, every
  opaque ``redacted_thinking``, every neighbour's ``cache_control``, every
  user turn;
* an assistant turn left with nothing is omitted; one that arrived empty is
  not;
* the trim is a pure function: twice is once, and an untouched history comes
  back as the very same list;
* the attempt's ``params.wire`` says ``foreign_thinking_dropped`` -- counts
  only -- when something was dropped, and nothing otherwise.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.anthropic_messages.request import (
    FOREIGN_THINKING_DROPPED,
    build_anthropic_messages_body,
    drop_foreign_thinking,
)
from my_claude_code.providers.base import ProviderConfig
from tests.providers.support import passthrough_rate_limiter

FOREIGN_TEXT = "agnes thought about the repo layout"
#: The shape of Anthropic's own ciphertext: never a JSON object or array.
CIPHERTEXT = "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIwxtE3rAFBa8cr3qpPkNRj"


def _signed(text: str = "Anthropic's own reasoning.") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "EqQBCkYIBxgC"}


def _blank(text: str = FOREIGN_TEXT, signature: Any = "") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def _unsigned(text: str = FOREIGN_TEXT) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text}


def _text(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "text", "text": text, **extra}


def _tool_use(tool_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"p": "a"}}


def _tool_result(tool_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "done"}


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": list(blocks)}


def _user(content: Any) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _request(messages: list[dict[str, Any]], **extra: Any) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {"model": "claude-haiku-5-5", "max_tokens": 4096, "messages": messages, **extra}
    )


def _body(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return build_anthropic_messages_body(
        _request(messages, **extra), reasoning=ReasoningPolicy.provider_default()
    )


# --------------------------------------------------------------------------
# What is dropped
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "foreign",
    [
        _blank(),
        _blank(signature="   "),
        _blank(signature="\n\t"),
        _unsigned(),
    ],
    ids=["empty", "spaces", "whitespace", "absent"],
)
def test_thinking_with_a_blank_or_absent_signature_is_dropped(
    foreign: dict[str, Any],
) -> None:
    body = _body([_user("hi"), _assistant(foreign, _text("Hello.")), _user("next")])

    assert body["messages"][1] == {"role": "assistant", "content": [_text("Hello.")]}


def test_a_non_string_signature_is_dropped() -> None:
    messages, blocks, chars = drop_foreign_thinking(
        [_assistant(_blank(signature=None), _blank(signature=7), _text("ok"))]
    )

    assert messages == [_assistant(_text("ok"))]
    assert (blocks, chars) == (2, 2 * len(FOREIGN_TEXT))


@pytest.mark.parametrize(
    "data",
    [
        json.dumps({"type": "reasoning.encrypted", "data": "abc", "id": "r1"}),
        json.dumps([{"type": "reasoning.summary", "summary": "s"}]),
        '  {"type": "reasoning.text"}',
    ],
    ids=["object", "array", "leading-space"],
)
def test_redacted_thinking_wrapping_json_is_dropped(data: str) -> None:
    body = _body(
        [
            _user("hi"),
            _assistant({"type": "redacted_thinking", "data": data}, _text("Yes.")),
        ]
    )

    assert body["messages"][1]["content"] == [_text("Yes.")]


# --------------------------------------------------------------------------
# What is kept
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [CIPHERTEXT, "opaque", "123", "null", '"a string"', "{not json", ""],
    ids=["ciphertext", "word", "number", "null", "string", "broken", "empty"],
)
def test_redacted_thinking_that_is_not_a_json_container_is_kept(data: str) -> None:
    block = {"type": "redacted_thinking", "data": data}
    messages = [_user("hi"), _assistant(block, _text("Yes."))]

    body = _body(messages)

    assert body["messages"][1]["content"] == [block, _text("Yes.")]


def test_every_non_blank_signature_is_kept_even_one_that_is_not_anthropics() -> None:
    third_party = {"type": "thinking", "thinking": "x", "signature": "relay-sig-1"}
    messages = [_user("hi"), _assistant(_signed(), third_party, _text("Yes."))]

    body = _body(messages)

    assert body["messages"][1]["content"] == [_signed(), third_party, _text("Yes.")]


def test_user_turns_are_never_read() -> None:
    odd = [_blank(), {"type": "text", "text": "a user pasting a block"}]

    body = _body([_user(odd), _assistant(_text("ok"))])

    assert body["messages"][0]["content"] == odd


def test_neighbours_keep_their_order_and_cache_control() -> None:
    cached_text = _text("Reading it.", cache_control={"type": "ephemeral"})
    cached_tool = {**_tool_use(), "cache_control": {"type": "ephemeral"}}
    messages = [
        _user([_text("Fix it.", cache_control={"type": "ephemeral"})]),
        _assistant(_signed("first"), _blank(), cached_text, _unsigned(), cached_tool),
        _user([_tool_result()]),
    ]

    body = _body(messages)

    assert body["messages"] == [
        messages[0],
        _assistant(_signed("first"), cached_text, cached_tool),
        messages[2],
    ]


# --------------------------------------------------------------------------
# Turns left empty
# --------------------------------------------------------------------------


def test_a_thinking_only_foreign_turn_is_omitted() -> None:
    body = _body([_user("Think."), _assistant(_blank()), _user("Go on.")])

    # The Messages API combines the two user turns into one.
    assert body["messages"] == [_user("Think."), _user("Go on.")]


def test_a_turn_of_only_foreign_thinking_and_wrapped_reasoning_is_omitted() -> None:
    wrapped = {"type": "redacted_thinking", "data": '{"type":"reasoning.encrypted"}'}

    body = _body([_user("a"), _assistant(_blank(), wrapped), _user("b")])

    assert body["messages"] == [_user("a"), _user("b")]


def test_an_assistant_turn_that_arrived_empty_is_kept() -> None:
    messages = [_user("a"), {"role": "assistant", "content": []}, _user("b")]

    body = _body(messages)

    assert body["messages"] == messages


def test_a_tool_turn_keeps_its_tool_use_when_its_thinking_is_foreign() -> None:
    body = _body(
        [
            _user("Read a."),
            _assistant(_blank(), _tool_use()),
            _user([_tool_result()]),
        ]
    )

    assert body["messages"][1] == _assistant(_tool_use())


def test_string_turns_are_untouched() -> None:
    messages = [_user("a"), {"role": "assistant", "content": "plain"}, _user("b")]

    assert _body(messages)["messages"] == messages


# --------------------------------------------------------------------------
# A pure function
# --------------------------------------------------------------------------


def test_an_untouched_history_comes_back_as_the_same_list() -> None:
    history = [
        _user("a"),
        _assistant(_signed(), {"type": "redacted_thinking", "data": CIPHERTEXT}),
        _user("b"),
        {"role": "assistant", "content": "plain"},
    ]

    messages, blocks, chars = drop_foreign_thinking(history)

    assert messages is history
    assert (blocks, chars) == (0, 0)


def test_trimming_twice_is_trimming_once() -> None:
    history = [
        _user("a"),
        _assistant(_blank("one"), _signed(), _text("t")),
        _user("b"),
        _assistant(_unsigned("two")),
        _user("c"),
    ]

    once, blocks, chars = drop_foreign_thinking(history)
    twice, again_blocks, again_chars = drop_foreign_thinking(once)

    assert twice == once
    assert twice is once
    assert (blocks, chars) == (2, len("one") + len("two"))
    assert (again_blocks, again_chars) == (0, 0)


def test_the_callers_history_is_not_modified() -> None:
    history = [_user("a"), _assistant(_blank(), _text("t"))]
    before = json.dumps(history)

    drop_foreign_thinking(history)

    assert json.dumps(history) == before


# --------------------------------------------------------------------------
# The note
# --------------------------------------------------------------------------


def test_the_build_writes_a_counts_only_note_when_it_dropped_something() -> None:
    notes: dict[str, str] = {}
    build_anthropic_messages_body(
        _request(
            [
                _user("a"),
                _assistant(_blank("x" * 1412), _text("t")),
                _user("b"),
                _assistant(_unsigned("yy"), _text("u")),
            ]
        ),
        reasoning=ReasoningPolicy.provider_default(),
        wire_notes=notes,
    )

    assert notes == {
        FOREIGN_THINKING_DROPPED: (
            "dropped 2 thinking blocks (1,414 chars) made by other models from "
            "earlier turns; Anthropic accepts only its own signed thinking"
        )
    }
    assert "x" * 20 not in notes[FOREIGN_THINKING_DROPPED]


def test_the_build_leaves_the_notes_alone_when_nothing_was_dropped() -> None:
    notes: dict[str, str] = {}
    build_anthropic_messages_body(
        _request([_user("a"), _assistant(_signed(), _text("t"))]),
        reasoning=ReasoningPolicy.provider_default(),
        wire_notes=notes,
    )

    assert notes == {}


# --------------------------------------------------------------------------
# End to end through the provider, against a host that enforces the rule
# --------------------------------------------------------------------------

_SSE = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
SIGNATURE_400 = "messages.1.content.0: Invalid `signature` in `thinking` block"


def _signature_rule(body: dict[str, Any]) -> httpx.Response:
    for message in body["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "thinking":
                continue
            signature = block.get("signature")
            if not isinstance(signature, str) or not signature.strip():
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": SIGNATURE_400,
                        },
                    },
                )
    return httpx.Response(200, content=_SSE)


def _provider(sent: list[dict[str, Any]]) -> AnthropicMessagesProvider:
    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-ant-test", base_url="https://upstream.invalid/v1"),
        provider_name="UPSTREAM",
        rate_limiter=passthrough_rate_limiter(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _signature_rule(sent[-1])

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


async def _drain(stream: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in stream]


@pytest.mark.asyncio
async def test_a_third_party_turn_no_longer_costs_the_anthropic_attempt() -> None:
    sent: list[dict[str, Any]] = []
    provider = _provider(sent)
    trace = install_wire_trace()
    request = _request(
        [
            _user("Summarise main.py."),
            _assistant(_blank(), _text("It prints hi.")),
            _user("And utils.py?"),
        ]
    )

    frames = await _drain(provider.stream_response(request))

    assert any("message_stop" in frame for frame in frames)
    assert len(sent) == 1
    assert sent[0]["messages"][1] == _assistant(_text("It prints hi."))
    params = trace.requests[max(trace.requests)].params
    assert params[FOREIGN_THINKING_DROPPED].startswith(
        f"dropped 1 thinking block ({len(FOREIGN_TEXT)} chars)"
    )
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_history_with_nothing_foreign_records_no_note() -> None:
    sent: list[dict[str, Any]] = []
    provider = _provider(sent)
    trace = install_wire_trace()
    request = _request([_user("a"), _assistant(_signed(), _text("t")), _user("b")])

    await _drain(provider.stream_response(request))

    assert len(sent) == 1
    assert sent[0]["messages"][1] == _assistant(_signed(), _text("t"))
    assert FOREIGN_THINKING_DROPPED not in trace.requests[max(trace.requests)].params
    await provider.cleanup()


@pytest.mark.asyncio
async def test_the_note_survives_a_retry_of_the_same_attempt() -> None:
    """A rung that rewrites the body does not change what the history lost."""

    sent: list[dict[str, Any]] = []
    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-ant-test", base_url="https://upstream.invalid/v1"),
        provider_name="UPSTREAM",
        rate_limiter=passthrough_rate_limiter(),
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        if "thinking" in sent[-1]:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking: Extra inputs are not permitted",
                    },
                },
            )
        return _signature_rule(sent[-1])

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    trace = install_wire_trace()
    request = _request([_user("a"), _assistant(_blank(), _text("t")), _user("b")])

    await _drain(
        provider.stream_response(
            request, reasoning=ReasoningPolicy.on(budget_tokens=2048)
        )
    )

    assert len(sent) == 2
    assert FOREIGN_THINKING_DROPPED in trace.requests[max(trace.requests)].params
    await provider.cleanup()
