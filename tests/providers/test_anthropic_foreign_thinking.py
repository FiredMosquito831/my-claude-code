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

8.12.2 adds the other half of the same rule: a Messages host that **signs
nothing** is not Anthropic, so its own unsigned thinking is not "another
model's". OpenCode Go measured 28 signatures beside 3,439 thinking responses
in 16,783 ``/messages`` requests. Applying the rule there dropped the host's
own reasoning and then suppressed the ``thinking`` field on the next turn
*because* it had dropped it -- 76.9 % of the turns that followed a
visible-thinking turn against 11.8 % otherwise. The last section pins that a
host declared ``signs_thinking=False`` keeps its own blocks, keeps asking for
reasoning on a tool follow-up, and still has the existing strip ladder behind
it if the host refuses what it is now sent.

**8.16.3 adds the third case, and it is the one this file's name no longer
describes exactly.** A carrier-bearing ``thinking`` block has both a readable
summary and an envelope, and the two have different rules: the envelope is
host-bound, the text is portable. On a host that signs nothing the block is
**kept** with its text and the ``signature`` key removed; on a host that signs
it is dropped whole, exactly as before. The sections at the end pin that split,
its idempotency, and the purity the whole cache argument rests on.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import (
    ReplayOrigin,
    connection_id,
    encode_carrier,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.anthropic_messages.request import (
    FOREIGN_THINKING_DROPPED,
    build_anthropic_messages_body,
    drop_foreign_thinking,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.messages_transport import MessagesTransport
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_STRIPPED,
)
from tests.providers.support import passthrough_rate_limiter

FOREIGN_TEXT = "agnes thought about the repo layout"
#: The shape of Anthropic's own ciphertext: never a JSON object or array.
CIPHERTEXT = "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIwxtE3rAFBa8cr3qpPkNRj"

#: An ``mcc:reasoning:v1:`` envelope, minted the way PR-2 mints one.
_CARRIER_ORIGIN = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="gpt-6.1-sol",
)


def _mint_carrier() -> str:
    """One ``mcc:reasoning:v1:`` envelope, minted the way PR-2 mints one."""

    carrier = encode_carrier(
        _CARRIER_ORIGIN,
        {
            "id": "rs_1",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "content": [],
            "encrypted_content": (
                "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"
            ),
        },
    )
    assert carrier is not None
    return carrier


CARRIER = _mint_carrier()

STRIPPED_NOTE = (
    "removed 1 reasoning carrier from a block whose text was kept "
    "(this host signs nothing)"
)


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


# --------------------------------------------------------------------------
# 8.12.2 -- a host that signs nothing keeps its own thinking
# --------------------------------------------------------------------------

#: The shape the 4,434 requests had: a tool loop whose final assistant turn
#: carries an unsigned ``thinking`` block and a ``tool_use``. On a host that
#: signs, that block is dropped and the reasoning field is suppressed; on a
#: host that signs nothing, both must not happen.
GO_TOOL_LOOP = [
    _user("Read main.py."),
    _assistant(_unsigned("Go's own reasoning."), _tool_use("toolu_go")),
    _user([_tool_result("toolu_go")]),
]


def test_a_host_that_signs_nothing_keeps_its_own_unsigned_thinking() -> None:
    history = [
        _user("a"),
        _assistant(_unsigned("one"), _text("t")),
        _user("b"),
        _assistant(_unsigned("two"), _text("u")),
    ]

    messages, blocks, chars = drop_foreign_thinking(history, signs_thinking=False)

    assert messages is history
    assert (blocks, chars) == (0, 0)


def test_a_host_that_signs_nothing_still_drops_mccs_own_wrapper() -> None:
    """The ``redacted_thinking`` rule is about the shape of ``data``, not a
    signature, so it is not the host's to declare away."""

    wrapped = {"type": "redacted_thinking", "data": '{"type":"reasoning.encrypted"}'}
    history = [_user("a"), _assistant(_unsigned("mine"), wrapped, _text("t"))]

    messages, blocks, chars = drop_foreign_thinking(history, signs_thinking=False)

    assert messages == [_user("a"), _assistant(_unsigned("mine"), _text("t"))]
    assert (blocks, chars) == (1, len('{"type":"reasoning.encrypted"}'))


def test_the_suppression_does_not_fire_where_the_host_signs_nothing() -> None:
    """The 4,434 shape: manual thinking on a tool follow-up still goes out."""

    body = build_anthropic_messages_body(
        _request(GO_TOOL_LOOP),
        reasoning=ReasoningPolicy.on(budget_tokens=2048),
        signs_thinking=False,
    )

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert body["messages"][1]["content"] == [
        _unsigned("Go's own reasoning."),
        _tool_use("toolu_go"),
    ]


def test_the_same_history_still_loses_its_field_where_the_host_signs() -> None:
    """The guard is not weakened; it is scoped. Same history, default host."""

    body = build_anthropic_messages_body(
        _request(GO_TOOL_LOOP), reasoning=ReasoningPolicy.on(budget_tokens=2048)
    )

    assert "thinking" not in body
    assert body["messages"][1]["content"] == [_tool_use("toolu_go")]


def test_the_suppression_stays_off_even_when_the_wrapper_was_dropped() -> None:
    """The guard's own premise is the signature rule, not the drop.

    A tool follow-up can still lose a block on a host that signs nothing --
    MCC's own ``redacted_thinking`` wrapper, which no Messages host minted.
    The suppression exists because Anthropic requires its own signed thinking
    ahead of a tool call; this host requires nothing, so the field still goes.
    """

    wrapped = {"type": "redacted_thinking", "data": '{"type":"reasoning.encrypted"}'}
    body = build_anthropic_messages_body(
        _request(
            [
                _user("Read a."),
                _assistant(wrapped, _tool_use("toolu_w")),
                _user([_tool_result("toolu_w")]),
            ]
        ),
        reasoning=ReasoningPolicy.on(budget_tokens=2048),
        signs_thinking=False,
    )

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert body["messages"][1]["content"] == [_tool_use("toolu_w")]


def test_a_host_that_signs_nothing_records_no_note_for_its_own_thinking() -> None:
    notes: dict[str, str] = {}
    build_anthropic_messages_body(
        _request(GO_TOOL_LOOP),
        reasoning=ReasoningPolicy.on(budget_tokens=2048),
        wire_notes=notes,
        signs_thinking=False,
    )

    assert notes == {}


def test_the_note_names_the_right_rule_on_a_host_that_signs_nothing() -> None:
    """Only MCC's own wrapper can be dropped there, and the note says so."""

    notes: dict[str, str] = {}
    build_anthropic_messages_body(
        _request(
            [
                _user("a"),
                _assistant(
                    {"type": "redacted_thinking", "data": '{"type":"x"}'},
                    _text("t"),
                ),
            ]
        ),
        reasoning=ReasoningPolicy.provider_default(),
        wire_notes=notes,
        signs_thinking=False,
    )

    assert notes == {
        FOREIGN_THINKING_DROPPED: (
            "dropped 1 thinking block (12 chars) made by other models from "
            "earlier turns; this host accepts only the thinking it wrote itself"
        )
    }


# --------------------------------------------------------------------------
# 8.12.2 -- the declaration reaches the wire from the profile
# --------------------------------------------------------------------------


def test_only_the_go_profile_declares_that_its_messages_door_signs_nothing() -> None:
    """Declared data, not a provider-id branch: the table is the whole answer."""

    unsigned = sorted(
        name
        for name, profile in OPENAI_CHAT_PROFILES.items()
        if not profile.messages_signs_thinking
    )
    assert unsigned == ["opencode_go"]


GO_BASE_URL = "https://opencode.ai/go/v1"


def _go_transport() -> MessagesTransport:
    """The transport the real provider builds for ``opencode_go``.

    Built through :func:`create_openai_chat_provider` rather than by hand, so
    the whole declared-data chain -- profile field, provider, transport -- is
    the thing under test and a break anywhere along it fails here.
    """

    provider = create_openai_chat_provider(
        "opencode_go",
        ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
        passthrough_rate_limiter(),
    )
    return provider._messages


_MESSAGES_SSE = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


@pytest.mark.asyncio
async def test_the_go_transport_sends_the_thinking_field_a_signed_host_would_lose() -> (
    None
):
    """End to end through the transport the profile builds, not the kwarg."""

    transport = _go_transport()
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_MESSAGES_SSE,
        )

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    frames = [
        frame
        async for frame in transport.stream(
            _request(GO_TOOL_LOOP),
            input_tokens=3,
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
        )
    ]

    assert any("message_stop" in frame for frame in frames)
    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert sent[0]["messages"][1]["content"][0] == _unsigned("Go's own reasoning.")
    await inner.cleanup()


def _refuses_the_field(request: httpx.Request, sent: list[dict[str, Any]]) -> bool:
    """The 400 a host sends when it will not take the ``thinking`` field."""

    sent.append(json.loads(request.content))
    return "thinking" in sent[-1]


@pytest.mark.asyncio
async def test_the_existing_strip_ladder_still_answers_a_refused_reasoning_field() -> (
    None
):
    """The ladder this host already had is reachable through the Go transport.

    Nothing here is new: one rung, one retry, the same body minus the field.
    """

    transport = _go_transport()
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _refuses_the_field(request, sent):
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
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_MESSAGES_SSE,
        )

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    frames = [
        frame
        async for frame in transport.stream(
            _request(GO_TOOL_LOOP),
            input_tokens=3,
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
        )
    ]

    assert any("message_stop" in frame for frame in frames)
    assert len(sent) == 2
    assert "thinking" not in sent[1]
    await inner.cleanup()


@pytest.mark.asyncio
async def test_a_host_that_refuses_the_replayed_block_costs_one_retry_and_no_more() -> (
    None
):
    """The accepted risk, measured rather than assumed.

    Go's ``/messages`` door has never received a replayed unsigned thinking
    block, so it may refuse one. What the existing ladder does about that is
    exactly what it does about any 400 that names ``thinking``: one retry with
    the reasoning *field* removed. It does not rewrite the history, and this
    release adds no second retry, no pre-emptive strip and no fallback -- so a
    host that refuses the block costs this request, and the chain moves on.
    """

    transport = _go_transport()
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        refused = any(
            block.get("type") == "thinking"
            for message in sent[-1]["messages"]
            if message.get("role") == "assistant"
            for block in message.get("content", [])
            if isinstance(block, dict)
        )
        if refused:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "messages.1.content.0: `thinking` blocks are not "
                        "accepted in the history",
                    },
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_MESSAGES_SSE,
        )

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(Exception) as raised:
        [
            frame
            async for frame in transport.stream(
                _request(GO_TOOL_LOOP),
                input_tokens=3,
                reasoning=ReasoningPolicy.on(budget_tokens=2048),
            )
        ]

    assert "400" in str(raised.value)
    # Exactly the ladder's one retry: no third attempt, and the retry differs
    # from the first only by the removed field.
    assert len(sent) == 2
    assert "thinking" in sent[0] and "thinking" not in sent[1]
    assert sent[0]["messages"] == sent[1]["messages"]
    await inner.cleanup()


# --------------------------------------------------------------------------
# 8.16.3 -- a carrier-bearing block: strip the envelope, keep the text
# --------------------------------------------------------------------------


def _carrier_block(text: str = "B's summary", **extra: Any) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": CARRIER, **extra}


def test_a_carrier_with_text_is_stripped_where_the_host_signs_nothing() -> None:
    """The release, in one assertion: the block stays, the envelope does not.

    The ``signature`` key is **absent**, not blanked. The host's own unsigned
    blocks have no ``signature`` key at all -- ``ContentBlockThinking.signature``
    is ``str | None`` and ``model_dump(exclude_none=True)`` drops it -- so
    removing reproduces a shape the host has already seen, where ``""`` would be
    one it has not.
    """

    history = [_user("a"), _assistant(_carrier_block(), _text("t")), _user("b")]

    messages, blocks, chars = drop_foreign_thinking(history, signs_thinking=False)

    assert messages[1] == _assistant(
        {"type": "thinking", "thinking": "B's summary"}, _text("t")
    )
    assert (blocks, chars) == (0, 0)
    # A strip is not a drop: the block is in the body.
    assert messages[1]["content"][0]["thinking"] == "B's summary"


def test_a_carrier_with_text_is_dropped_where_the_host_signs() -> None:
    """The Anthropic door recovers nothing, and this release does not pretend it does.

    An unsigned ``thinking`` block is a 400 at Anthropic, so stripping there
    would only move the drop one rule later. The body is byte-identical to
    8.16.2's.
    """

    history = [_user("a"), _assistant(_carrier_block(), _text("t")), _user("b")]

    messages, blocks, chars = drop_foreign_thinking(history, signs_thinking=True)

    assert messages[1] == _assistant(_text("t"))
    assert (blocks, chars) == (0, 0)


def test_the_strip_reports_itself_under_its_own_key() -> None:
    """Two facts, two keys. A strip never touches ``history_carrier_foreign``."""

    counts: dict[str, int] = {}
    drop_foreign_thinking(
        [_user("a"), _assistant(_carrier_block(), _text("t"))],
        signs_thinking=False,
        carrier_counts=counts,
    )
    assert counts == {HISTORY_CARRIER_STRIPPED: 1}

    counts = {}
    drop_foreign_thinking(
        [_user("a"), _assistant(_carrier_block(), _text("t"))],
        signs_thinking=True,
        carrier_counts=counts,
    )
    assert counts == {HISTORY_CARRIER_FOREIGN: 1}


def test_the_strip_is_idempotent() -> None:
    """Stripping twice is stripping once, and the second pass reports nothing."""

    history = [_user("a"), _assistant(_carrier_block(), _text("t")), _user("b")]

    once, _, _ = drop_foreign_thinking(history, signs_thinking=False)
    counts: dict[str, int] = {}
    twice, blocks, chars = drop_foreign_thinking(
        once, signs_thinking=False, carrier_counts=counts
    )

    assert twice == once
    assert twice is once
    assert counts == {}
    assert (blocks, chars) == (0, 0)


def test_the_strip_does_not_modify_the_callers_block() -> None:
    """Purity, at the block level: the input dict keeps its envelope.

    ``drop_foreign_thinking`` is documented as a pure function of the history,
    and the cache argument rests on it. A ``pop`` would be safe today because
    the caller deep-copies first -- and would make the contract depend on that
    copy discipline, which is the coupling that breaks two releases later.
    """

    block = _carrier_block()
    history = [_user("a"), _assistant(block, _text("t"))]
    before = json.dumps(history)

    messages, _, _ = drop_foreign_thinking(history, signs_thinking=False)

    assert json.dumps(history) == before
    assert block["signature"] == CARRIER
    assert messages[1]["content"][0] is not block


def test_a_stripped_turn_is_not_an_unsigned_tool_turn() -> None:
    """A kept block satisfies Anthropic's rule, so the field is not suppressed.

    ``unsigned_tool_turn`` exists because manual thinking needs Anthropic's own
    thinking ahead of a tool call. A **stripped** block is in the body, so the
    rule is satisfied by the block itself -- and on a non-signing host the
    suppression never fires anyway (8.12.2). Both reasons point the same way.
    """

    body = build_anthropic_messages_body(
        _request(
            [
                _user("Read a."),
                _assistant(_carrier_block(), _tool_use("toolu_c")),
                _user([_tool_result("toolu_c")]),
            ]
        ),
        reasoning=ReasoningPolicy.on(budget_tokens=2048),
        signs_thinking=False,
    )

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert body["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "B's summary"},
        _tool_use("toolu_c"),
    ]


def test_the_build_writes_the_strip_note_and_never_the_drop_note() -> None:
    """The operator's line, on the door that can produce it."""

    notes: dict[str, str] = {}
    build_anthropic_messages_body(
        _request([_user("a"), _assistant(_carrier_block(), _text("t")), _user("b")]),
        reasoning=ReasoningPolicy.provider_default(),
        wire_notes=notes,
        signs_thinking=False,
    )

    assert notes == {HISTORY_CARRIER_STRIPPED: STRIPPED_NOTE}
    assert FOREIGN_THINKING_DROPPED not in notes
    assert HISTORY_CARRIER_FOREIGN not in notes
    assert CARRIER not in json.dumps(notes)


def test_a_clean_history_records_no_carrier_note_on_either_door() -> None:
    """Absent, not ``0`` -- so every other request's params are what they were."""

    for signs_thinking in (True, False):
        notes: dict[str, str] = {}
        build_anthropic_messages_body(
            _request([_user("a"), _assistant(_signed(), _text("t"))]),
            reasoning=ReasoningPolicy.provider_default(),
            wire_notes=notes,
            signs_thinking=signs_thinking,
        )
        assert notes == {}, signs_thinking


@pytest.mark.asyncio
async def test_the_go_transport_sends_the_kept_text_without_the_envelope() -> None:
    """End to end through the transport the profile builds, not the kwarg.

    The declaration reaches the wire: ``opencode_go`` is the one profile whose
    ``messages_signs_thinking`` is ``False``, and the body its transport sends
    carries the summary and no ``signature`` key.
    """

    transport = _go_transport()
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_MESSAGES_SSE,
        )

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    frames = [
        frame
        async for frame in transport.stream(
            _request(
                [_user("a"), _assistant(_carrier_block(), _text("t")), _user("b")]
            ),
            input_tokens=3,
            reasoning=ReasoningPolicy.off(),
        )
    ]

    assert any("message_stop" in frame for frame in frames)
    assert len(sent) == 1
    assert sent[0]["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "B's summary"},
        _text("t"),
    ]
    assert CARRIER not in json.dumps(sent[0])
    await inner.cleanup()
