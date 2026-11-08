"""The opaque carrier: what Claude Code round-trips, and what MCC must report.

PR-1 of the reasoning-continuity arc. The question was whether Claude Code
stores a ``redacted_thinking`` block MCC emits and sends it back on the next
turn, because that would let MCC carry OpenAI's ``encrypted_content`` through
the client with no server-side state at all.

**The answer, measured against the real Claude Code 2.1.319 on 2026-11-08, is
YES — but only while ``message_start.message.model`` equals the model the
client asked for.** The evidence is in the PR body; the mechanism is
``Gct(message, currentModel)`` in the client's bundle, which classifies an
assistant message as *foreign* when its ``message.model`` differs from the
current model, after which ``Zvn``/``qYs`` strip every ``thinking`` and
``redacted_thinking`` block from it.

That condition is the whole reason this file exists, because MCC does not
satisfy it today. ``ModelRouter._route_for`` rewrites the request's model to
the resolved provider's (``application/routing.py:1207``), and the ledger that
opens the stream is built from that rewritten model
(``providers/openai_chat/responses_transport.py:881``). So a turn the client
asked for as ``claude-opus-5-5`` comes back stamped ``gpt-6.1-sol``, the client
treats it as another model's, and it drops **all** of that turn's thinking —
the carrier MCC would mint, and the summary-derived ``thinking`` blocks MCC
already emits. On the Anthropic door the two names coincide, which is exactly
why continuity measures intact there and nowhere else.

The tests below pin the three facts PR-3 is built on, so that changing any of
them is a visible, deliberate diff rather than a silent regression:

1. MCC's inbound model accepts a ``redacted_thinking`` block whose ``data`` is
   a base64 string, keeps it byte for byte, and keeps it in position. The
   carrier is usable at all only because this is true.
2. The model MCC stamps on ``message_start`` is the *routed* one, not the
   client's. This is the condition the client's round trip depends on, and it
   is currently the wrong side of it.
3. The shared Responses builder replays no reasoning item today, so the block
   a client sends back reaches the upstream as nothing. PR-3 changes this
   deliberately; the assertion is here so that change cannot be accidental.

Nothing in this file asserts a behaviour of Claude Code itself — a test cannot
run the client. It pins MCC's half of the contract and records the measured
answer in the docstrings, which is what the PR's acceptance asks for.
"""

import json

import pytest

from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import (
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    Message,
    MessagesRequest,
)
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_responses import ResponsesStreamConverter
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)

#: The prefix the scratch emitter put on ``data`` in the live experiment, so
#: the fixture below is the exact string the client was observed to replay.
CARRIER_MARKER = "mcc-carrier-v1:"

#: The opaque payload the fake upstream returned. Shaped like base64 on purpose:
#: ``drop_foreign_thinking`` keeps a string whose first non-space character is
#: not ``{`` or ``[``, and drops a JSON container.
CARRIER_DATA = (
    CARRIER_MARKER + "gAAAAABpQ2FycmllclRlc3RPcGFxdWVQYXlsb2FkRm9yUHJvamVjdE9uZQ"
    "BfUm91bmRUcmlwX0VuY3J5cHRlZENvbnRlbnRfMDEyMzQ1Njc4OWFiY2RlZg"
)

#: What the client asked for. A Claude alias, because that is what a Claude
#: Code session sends; the route below resolves it to another provider.
REQUESTED_MODEL = "claude-opus-5-5"

#: What that alias resolves to on the ChatGPT OAuth door.
PROVIDER_MODEL = "gpt-6.1-sol"
PROVIDER_REF = f"chatgpt_oauth/{PROVIDER_MODEL}"


def _settings() -> Settings:
    settings = Settings()
    settings.model = PROVIDER_REF
    settings.model_opus = PROVIDER_REF
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.model_fable = None
    settings.reasoning_policy = ReasoningPreference.OFF
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def _router() -> ModelRouter:
    return ModelRouter(
        _settings(),
        output_limit_lookup=lambda _provider, _model: None,
        reasoning_dialect_lookup=lambda _provider, _model: None,
    )


def _client_history() -> list[Message]:
    """The history Claude Code was observed to send back in the YES arm.

    A user turn, the assistant turn that carried the carrier, and the next
    user turn. The block order is the client's own: thinking, the carrier,
    then the answer text.
    """

    return [
        Message(role="user", content="say OK"),
        Message(
            role="assistant",
            content=[
                ContentBlockThinking(
                    type="thinking", thinking="a summary", signature="sig"
                ),
                ContentBlockRedactedThinking(
                    type="redacted_thinking", data=CARRIER_DATA
                ),
                ContentBlockText(type="text", text="OK"),
            ],
        ),
        Message(role="user", content="say DONE"),
    ]


# --------------------------------------------------------------------------- #
# 1. MCC's inbound model carries the block, byte for byte, in position
# --------------------------------------------------------------------------- #


def test_a_base64_carrier_survives_the_inbound_model_unchanged():
    """The client's carrier parses, keeps its exact ``data``, and keeps its place.

    This is the premise of the whole client-carried design: if MCC's request
    model coerced, reordered or dropped the block, a client that round-trips it
    would still hand MCC nothing. It does none of those things — the block is a
    first-class member of the content union and ``data`` is a plain string.
    """

    request = MessagesRequest.model_validate(
        {
            "model": REQUESTED_MODEL,
            "max_tokens": 64,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "a summary",
                            "signature": "sig",
                        },
                        {"type": "redacted_thinking", "data": CARRIER_DATA},
                        {"type": "text", "text": "OK"},
                    ],
                }
            ],
        }
    )

    blocks = request.messages[0].content
    assert [getattr(block, "type", None) for block in blocks] == [
        "thinking",
        "redacted_thinking",
        "text",
    ]
    carrier = blocks[1]
    assert isinstance(carrier, ContentBlockRedactedThinking)
    assert carrier.data == CARRIER_DATA
    # The marker is what made the live round trip provable; keep it in the
    # fixture so a future re-run compares like with like.
    assert carrier.data.startswith(CARRIER_MARKER)


def test_a_json_container_data_is_not_the_carrier_and_is_kept_as_sent():
    """A JSON-shaped ``data`` is MCC's own OpenRouter wrapper, not ciphertext.

    ``drop_foreign_thinking`` is the rule that removes it on the Anthropic
    door; the request model itself never rewrites it, so the difference
    between the two shapes is decided in one place and not two.
    """

    payload = json.dumps({"carrier": CARRIER_DATA})
    request = MessagesRequest.model_validate(
        {
            "model": REQUESTED_MODEL,
            "max_tokens": 64,
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "redacted_thinking", "data": payload}],
                }
            ],
        }
    )

    carrier = request.messages[0].content[0]
    assert isinstance(carrier, ContentBlockRedactedThinking)
    assert carrier.data == payload


# --------------------------------------------------------------------------- #
# 2. The model MCC stamps on message_start is the ROUTED one
# --------------------------------------------------------------------------- #


def test_the_route_rewrites_the_clients_model_to_the_providers():
    """``claude-opus-5-5`` becomes ``gpt-6.1-sol`` before any encoder sees it.

    This is not a defect in itself — the upstream must be asked for the model
    it serves — but it is the value the stream ledger is built from, and so the
    value the client reads back on ``message_start``. The client compares that
    with the model it asked for; when they differ it treats the turn as another
    model's and drops the turn's thinking. PR-3 depends on this line.
    """

    routed = _router().resolve_messages_request(
        MessagesRequest(
            model=REQUESTED_MODEL,
            max_tokens=64,
            messages=[Message(role="user", content="hi")],
        )
    )

    assert routed.request.model == PROVIDER_MODEL
    assert routed.resolved.original_model == REQUESTED_MODEL
    assert routed.resolved.provider_model_ref == PROVIDER_REF


def test_message_start_carries_the_routed_model_not_the_requested_one():
    """The ledger echoes the routed model, so the client sees the provider's name.

    Measured consequence (Claude Code 2.1.319, 2026-11-08): with
    ``message.model == "gpt-6.1-sol"`` while the client asked for
    ``claude-opus-5-5``, the client dropped the carrier on the next turn; with
    the two equal it replayed the same 133-character ``data`` byte for byte.
    A change here is a change to that outcome, and must be re-measured.
    """

    routed = _router().resolve_messages_request(
        MessagesRequest(
            model=REQUESTED_MODEL,
            max_tokens=64,
            messages=[Message(role="user", content="hi")],
        )
    )
    ledger = AnthropicStreamLedger("msg_x", routed.request.model, input_tokens=0)

    start = json.loads(ledger.message_start().split("data: ", 1)[1])
    assert start["message"]["model"] == PROVIDER_MODEL
    assert start["message"]["model"] != REQUESTED_MODEL


# --------------------------------------------------------------------------- #
# 3. The shared Responses builder replays no reasoning item today
# --------------------------------------------------------------------------- #


def test_the_carrier_reaches_the_responses_upstream_as_nothing():
    """A history carrying the carrier produces an ``input`` with no reasoning item.

    ``RESPONSES_REASONING_REPLAY`` is ``DISABLED`` and ``_earlier_thinking``
    counts ``thinking`` blocks only, so today the block the client faithfully
    sends back is dropped on the floor. PR-3 turns this into a ``reasoning``
    input item carrying ``encrypted_content`` in its original position; when it
    does, this test is the one that must change, on purpose.
    """

    body = build_responses_request_body(
        MessagesRequest(
            model=PROVIDER_MODEL,
            max_tokens=64,
            messages=_client_history(),
        ),
        reasoning=ReasoningPolicy.off(),
    )

    kinds = [item.get("type") for item in body["input"] if isinstance(item, dict)]
    assert "reasoning" not in kinds
    # The answer text and the user turns still travel, so the assertion above
    # is about the carrier and not about an empty body.
    assert "message" in kinds
    assert CARRIER_DATA not in json.dumps(body)


def test_the_omission_note_counts_the_thinking_block_and_not_the_carrier():
    """``history_thinking_omitted`` counts ``thinking`` blocks, never the carrier.

    The note is the operator's only signal that reasoning was left out, so it
    stays honest only if it counts what it actually omits. The history here has
    exactly ONE ``thinking`` block and ONE carrier, and the count is 1 — the
    carrier is omitted too, and PR-3's first change is to make the count
    include it. An exact count is what makes that change visible; a "present or
    absent" assertion would pass either way.
    """

    notes: dict[str, str] = {}
    build_responses_request_body(
        MessagesRequest(
            model=PROVIDER_MODEL,
            max_tokens=64,
            messages=_client_history(),
        ),
        reasoning=ReasoningPolicy.off(),
        wire_notes=notes,
    )

    assert "history_thinking_omitted" in notes
    assert notes["history_thinking_omitted"].startswith(
        "omitted 1 earlier thinking block"
    )
    assert CARRIER_DATA not in json.dumps(notes)


@pytest.mark.parametrize("data", [CARRIER_DATA, "plain-base64-not-json"])
def test_a_carrier_is_never_turned_into_visible_text(data):
    """The carrier stays opaque: nothing about it becomes ``thinking`` text.

    The 7.58.1 anti-echo guarantee is that no prior reasoning is replayed as
    literal ``<thinking>`` text. The carrier is the one artefact that must
    never be rendered, so the body must not contain it in any form while replay
    is off.
    """

    body = build_responses_request_body(
        MessagesRequest(
            model=PROVIDER_MODEL,
            max_tokens=64,
            messages=[
                Message(role="user", content="hi"),
                Message(
                    role="assistant",
                    content=[
                        ContentBlockRedactedThinking(
                            type="redacted_thinking", data=data
                        )
                    ],
                ),
                Message(role="user", content="again"),
            ],
        ),
        reasoning=ReasoningPolicy.off(),
    )

    serialized = json.dumps(body)
    assert data not in serialized
    assert "<thinking>" not in serialized


def _encrypted_only_frames() -> list[dict]:
    """One Responses stream whose only reasoning payload is ``encrypted_content``.

    The shape the investigation measured as real (7.5% Luna / 74.8% Sol): a
    reasoning item with an empty ``summary`` and an opaque ``encrypted_content``,
    followed by a short answer.
    """

    item = {
        "type": "reasoning",
        "id": "rs_carrier",
        "summary": [],
        "content": [],
        "encrypted_content": CARRIER_DATA,
        "status": "completed",
    }
    return [
        {
            "type": "response.created",
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_carrier"},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 1,
            "content_index": 0,
            "delta": "OK",
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        },
    ]


def test_an_encrypted_only_item_opens_no_block_and_leaks_no_text():
    """The deliberate empty-thinking-block decision, pinned.

    ``providers/openai_responses/streaming.py`` opens nothing for a reasoning
    item whose only payload is ``encrypted_content``: there is no text anybody
    may read, and an empty thinking block would claim otherwise. The carrier is
    therefore invisible to the client today — which is exactly why PR-3 has to
    mint it deliberately rather than inherit it, and why nothing may start
    rendering the ciphertext as ``thinking`` text in the meantime.
    """

    ledger = AnthropicStreamLedger("msg_carrier", PROVIDER_MODEL, input_tokens=0)
    converter = ResponsesStreamConverter(ledger)
    chunks = [ledger.message_start()]
    for frame in _encrypted_only_frames():
        chunks.extend(converter.feed(frame))
    chunks.extend(converter.finish())
    sse = "".join(chunks)

    started = [
        event.data["content_block"]["type"]
        for event in parse_sse_text(sse)
        if event.event == "content_block_start"
    ]
    assert started == ["text"]
    assert CARRIER_DATA not in sse
    assert "redacted_thinking" not in sse
