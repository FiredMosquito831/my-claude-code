"""The carrier replay: where the item goes, and what it is byte for byte.

PR-2 of the reasoning-continuity arc. The Responses API returns an opaque
``encrypted_content`` blob beside a reasoning item, and on a ``store: false``
conversation that blob is the only way reasoning survives to the next turn.
MCC asked for it and threw it away until 8.16.0
(``RESPONSES_REASONING_REPLAY = DISABLED``): ``history_thinking_omitted`` on
13,856 of 26,566 measured ChatGPT OAuth attempts.

This file is the primary contract for the replay half. It is deterministic,
offline, and driven by a fake upstream that speaks the real SSE shape, so the
whole path is exercised: the stream converter mints the block, the client's
transcript carries it, and the request builder decodes it back into the
``reasoning`` item it came from.

**The invariant, stated once and asserted as a whole list:**

    For every assistant turn, its replayed ``reasoning`` items appear, in their
    original block order, at the head of that turn's ``input[]`` run -- before
    the turn's ``message`` item and before every ``function_call`` item -- and
    every ``function_call`` is immediately followed (after all calls of the
    turn) by its own ``function_call_output``, with no reasoning item
    interleaved among the outputs.

A ``"reasoning" in kinds`` check passes for an item in the wrong place, which
is why every assertion below spells the list out.

The mutation controls this file is built to catch (each turns a named test
red) are recorded in the PR body: M1/M2 move a reasoning item after the
``function_call`` / after its output, M3 reverses two items in one turn, M4
re-encodes ``native`` instead of replaying it verbatim, M5 mints a fresh id
when ``native`` has none, M6 drops the origin check.
"""

import json
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.openai_responses import (
    CARRIER_MAX_CHARS,
    CARRIER_PREFIX,
    ReplayOrigin,
    connection_id,
    decode_carrier,
    encode_carrier,
    normalise_endpoint,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_MALFORMED,
    HISTORY_CARRIER_REPLAYED,
    ResponsesStreamConverter,
    build_responses_request_body,
)

#: The destination every test replays to. Five strings, all non-empty, exactly
#: as a v1 envelope writes them.
DESTINATION = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="gpt-6.1-sol",
)

#: A different host: same protocol, different provider and endpoint.
OTHER_HOST = ReplayOrigin(
    provider="opencode",
    protocol="responses",
    endpoint="https://opencode.ai/zen/v1",
    connection=connection_id("sk-test"),
    model="muse-spark-1.3-contributor-free",
)

#: The opaque payload a fake upstream returns. Shaped like base64 on purpose:
#: the real ``encrypted_content`` is an opaque blob, never JSON.
E1 = "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"
E2 = "gAAAAABpQ2FycmllclR3b19FbmNyeXB0ZWRDb250ZW50X2FiY2RlZmdoaWo"


def _reasoning_item(
    item_id: str | None,
    encrypted: str,
    *,
    summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One reasoning item as the host writes it on ``output_item.done``.

    ``id`` is omitted entirely when ``item_id`` is ``None`` -- a legitimate
    state the envelope must carry faithfully (LiteLLM's "an item without an id
    is always accepted").
    """

    item: dict[str, Any] = {
        "type": "reasoning",
        "status": "completed",
        "summary": summary if summary is not None else [],
        "content": [],
        "encrypted_content": encrypted,
    }
    if item_id is not None:
        item = {"id": item_id, **item}
    return item


def _frames(
    reasoning: dict[str, Any], *, call_id: str | None = None, text: str = "OK"
) -> list[dict[str, Any]]:
    """One Responses stream: a reasoning item, an optional call, then text.

    The shape is the real one -- ``response.created``, ``output_item.added``,
    ``output_item.done``, ``response.output_text.delta``, ``response.completed``
    -- and it is the same shape ``_encrypted_only_frames()`` in
    ``test_redacted_thinking_carrier.py`` uses, which was proven against the
    converter.
    """

    frames: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {"id": "resp_1", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "id": reasoning.get("id", "rs_x")},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
    ]
    if call_id is not None:
        frames.append(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": "Read",
                },
            }
        )
        frames.append(
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": "Read",
                    "arguments": "{}",
                },
            }
        )
    frames.append(
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 2,
            "content_index": 0,
            "delta": text,
        }
    )
    frames.append(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [reasoning],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        }
    )
    return frames


def _run_stream(
    frames: list[dict[str, Any]],
    *,
    origin: ReplayOrigin | None = DESTINATION,
    carrier_replay: bool = True,
    output_reasoning: bool = True,
) -> tuple[str, ResponsesStreamConverter]:
    """Feed one fake stream through the real converter; return its SSE text."""

    ledger = AnthropicStreamLedger("msg_carrier", DESTINATION.model, input_tokens=0)
    converter = ResponsesStreamConverter(
        ledger,
        output_reasoning=output_reasoning,
        origin=origin,
        carrier_replay=carrier_replay,
    )
    chunks = [ledger.message_start()]
    for frame in frames:
        chunks.extend(converter.feed(frame))
    chunks.extend(converter.finish())
    return "".join(chunks), converter


def _blocks(sse: str) -> list[dict[str, Any]]:
    """Every ``content_block_start`` payload, in order."""

    from my_claude_code.core.anthropic.stream_contracts import parse_sse_text

    return [
        event.data["content_block"]
        for event in parse_sse_text(sse)
        if event.event == "content_block_start"
    ]


def _signatures(sse: str) -> list[str]:
    """Every ``signature_delta`` payload, in order."""

    from my_claude_code.core.anthropic.stream_contracts import parse_sse_text

    return [
        event.data["delta"]["signature"]
        for event in parse_sse_text(sse)
        if event.event == "content_block_delta"
        and event.data["delta"].get("type") == "signature_delta"
    ]


def _thinking_text(sse: str) -> str:
    """The concatenated ``thinking_delta`` text of one stream."""

    from my_claude_code.core.anthropic.stream_contracts import parse_sse_text

    return "".join(
        event.data["delta"]["thinking"]
        for event in parse_sse_text(sse)
        if event.event == "content_block_delta"
        and event.data["delta"].get("type") == "thinking_delta"
    )


def _client_carrier(sse: str) -> str:
    """The one carrier this stream handed the client."""

    carriers = [
        block["data"] for block in _blocks(sse) if block["type"] == "redacted_thinking"
    ]
    carriers.extend(_signatures(sse))
    assert len(carriers) == 1, carriers
    return carriers[0]


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": list(blocks)}


def _tool_result(tool_id: str, content: str = "done") -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        ],
    }


def _tool_use(tool_id: str, name: str = "Read") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _body(
    messages: list[dict[str, Any]],
    *,
    origin: ReplayOrigin | None = DESTINATION,
    model: str = "gpt-6.1-sol",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Build one outbound body and its wire notes."""

    notes: dict[str, str] = {}
    body = build_responses_request_body(
        MessagesRequest.model_validate(
            {"model": model, "max_tokens": 4096, "messages": messages}
        ),
        reasoning=ReasoningPolicy.off(),
        wire_notes=notes,
        origin=origin,
    )
    return body, notes


def _kinds(body: dict[str, Any]) -> list[str]:
    return [str(item.get("type")) for item in body["input"]]


# --------------------------------------------------------------------------- #
# 1. The three-turn fixture: position, pairing, and byte identity
# --------------------------------------------------------------------------- #


def test_turn_one_hands_the_client_a_redacted_thinking_carrier() -> None:
    """An encrypted-only item opens no thinking block, and mints a carrier.

    The empty-thinking-block decision stands -- the item carries no text, and an
    empty thinking block would claim otherwise -- so the carrier goes out as a
    standalone ``redacted_thinking`` block, which is a different block type and
    does not claim text.
    """

    item = _reasoning_item("rs_1", E1)
    sse, converter = _run_stream(_frames(item, call_id="call_1"))

    blocks = _blocks(sse)
    assert [block["type"] for block in blocks] == [
        "redacted_thinking",
        "tool_use",
        "text",
    ]
    assert blocks[0]["data"].startswith(CARRIER_PREFIX)
    assert blocks[1]["id"] == "call_1"
    # The ciphertext is never rendered as text anywhere in the client stream.
    assert E1 not in sse
    assert converter.carrier_counts == (1, 0)


def test_turn_two_puts_the_replayed_item_at_the_head_of_its_own_run() -> None:
    """The whole ``input[]`` list, spelled out -- not a "contains" check.

    Turn 2's history is the client's own: the carrier block, the tool call it
    answered, and the tool result. The replayed item belongs at the head of
    turn 1's run, before that turn's ``message`` and before its
    ``function_call``; the call and its output stay adjacent.
    """

    carrier = encode_carrier(DESTINATION, _reasoning_item("rs_1", E1))
    assert carrier is not None
    body, notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant(
                {"type": "redacted_thinking", "data": carrier},
                _tool_use("call_1"),
            ),
            _tool_result("call_1"),
        ]
    )

    assert _kinds(body) == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert body["input"][1] == _reasoning_item("rs_1", E1)
    assert notes[HISTORY_CARRIER_REPLAYED].startswith("replayed 1 reasoning carrier")


def test_turn_three_carries_two_items_in_order_each_at_its_own_head() -> None:
    """Two turns, two carriers, one list: order and position both asserted.

    This is the shape PR-1 measured on the real client ("the carrier survives a
    SECOND hop -- turn 3 carried both carriers"), and it is the assertion M3
    (reverse the order of two reasoning items in one turn) turns red.
    """

    carrier_1 = encode_carrier(DESTINATION, _reasoning_item("rs_1", E1))
    carrier_2 = encode_carrier(
        DESTINATION,
        _reasoning_item("rs_2", E2, summary=[{"type": "summary_text", "text": "two"}]),
    )
    assert carrier_1 is not None and carrier_2 is not None
    body, notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant(
                {"type": "redacted_thinking", "data": carrier_1},
                _tool_use("call_1"),
            ),
            _tool_result("call_1"),
            _assistant(
                {
                    "type": "thinking",
                    "thinking": "two",
                    "signature": carrier_2,
                },
                _tool_use("call_2"),
            ),
            _tool_result("call_2"),
        ]
    )

    assert _kinds(body) == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    reasoning = [item for item in body["input"] if item["type"] == "reasoning"]
    assert [item["id"] for item in reasoning] == ["rs_1", "rs_2"]
    assert reasoning[0] == _reasoning_item("rs_1", E1)
    assert reasoning[1] == _reasoning_item(
        "rs_2", E2, summary=[{"type": "summary_text", "text": "two"}]
    )
    assert notes[HISTORY_CARRIER_REPLAYED].startswith("replayed 2 reasoning carriers")


def test_two_carriers_in_one_turn_keep_their_block_order() -> None:
    """Both items of one assistant turn, in the order the blocks had them.

    This is the assertion M3 (reverse the order of two reasoning items in one
    turn) turns red, and it is the shape §9 failure 10 names.
    """

    first = encode_carrier(DESTINATION, _reasoning_item("rs_1", E1))
    second = encode_carrier(DESTINATION, _reasoning_item("rs_2", E2))
    assert first is not None and second is not None
    body, notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant(
                {"type": "redacted_thinking", "data": first},
                {"type": "thinking", "thinking": "two", "signature": second},
                {"type": "text", "text": "done"},
            ),
            {"role": "user", "content": "again"},
        ]
    )

    assert _kinds(body) == ["message", "reasoning", "reasoning", "message", "message"]
    reasoning = [item for item in body["input"] if item["type"] == "reasoning"]
    assert [item["id"] for item in reasoning] == ["rs_1", "rs_2"]
    assert notes[HISTORY_CARRIER_REPLAYED].startswith("replayed 2 reasoning carriers")


def test_a_summary_rides_inside_the_envelope_and_never_as_text() -> None:
    """The anti-echo guarantee, scoped to what it actually protects.

    The 7.58.1 regression was **format**: prior reasoning written back as
    literal ``​think`` text in the assistant's visible output, which taught the
    model to write its next turn in that format. Two things keep that shut:

    * toward the **client**, the summary travels only inside the base64
      envelope -- the SSE stream carries the envelope and nothing readable;
    * toward the **upstream**, the summary appears only as the ``summary`` field
      of the replayed ``reasoning`` item -- the host's own structured field, in
      the shape the host itself writes in ``output[]``. It is never assistant
      text and never a ``​think`` tag.

    §13 item 6 of the spec words this as "no ``native["summary"]`` text appears
    in any outbound body outside the envelope", which cannot hold together with
    §4.4's "the reasoning item for rs_1 is byte-identical to the item the host
    sent" -- a verbatim item carries its summary. The byte-identity requirement
    is the one the rest of the spec builds on (M4 exists to enforce it), so the
    assertion below is the one that is true of the design: no ``​think`` tag
    anywhere, and no summary text in any assistant ``message`` item.
    """

    summary_text = "a plan the model wrote"
    carrier = encode_carrier(
        DESTINATION,
        _reasoning_item(
            "rs_1", E1, summary=[{"type": "summary_text", "text": summary_text}]
        ),
    )
    assert carrier is not None

    # Toward the client: the summary is the thinking block's text -- what the
    # host asked to be shown -- and the carrier is an opaque signature beside
    # it. No tag is ever written, and the ciphertext is never text.
    sse, _converter = _run_stream(
        _frames(
            _reasoning_item(
                "rs_1", E1, summary=[{"type": "summary_text", "text": summary_text}]
            )
        )
    )
    assert _thinking_text(sse) == summary_text
    assert "<thinking>" not in sse
    assert E1 not in sse

    # Toward the upstream: no tag, and no summary in assistant-visible text.
    body, _notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant(
                {"type": "thinking", "thinking": summary_text, "signature": carrier}
            ),
            {"role": "user", "content": "again"},
        ]
    )
    serialized = json.dumps(body)
    assert "<thinking>" not in serialized
    for item in body["input"]:
        if item.get("type") == "message":
            assert summary_text not in json.dumps(item)

    # It IS inside the envelope, which is the whole point.
    decoded = decode_carrier(carrier)
    assert decoded is not None
    assert decoded.native["summary"][0]["text"] == summary_text


# --------------------------------------------------------------------------- #
# 2. Origin scoping -- five mismatch cases, one matching control
# --------------------------------------------------------------------------- #


def _origin_with(**overrides: str) -> ReplayOrigin:
    fields = DESTINATION.to_dict()
    fields.update(overrides)
    return ReplayOrigin(**fields)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "opencode"),
        ("protocol", "messages"),
        ("endpoint", "https://opencode.ai/zen/v1"),
        ("connection", connection_id("another-account")),
        ("model", "gpt-6-luna"),
    ],
)
def test_a_carrier_from_another_origin_is_never_sent(field: str, value: str) -> None:
    """Each of the five fields, mismatched in turn, drops and counts.

    Strict on all five, ``model`` included: OmniRoute documents a hard 400 on a
    cross-model replay, and MCC routes opus to sol and haiku to luna inside one
    session, so without the model in the match a luna blob would be replayed to
    sol on the very next turn.
    """

    carrier = encode_carrier(
        _origin_with(**{field: value}), _reasoning_item("rs_1", E1)
    )
    assert carrier is not None
    history = [
        {"role": "user", "content": "go"},
        _assistant({"type": "redacted_thinking", "data": carrier}),
        {"role": "user", "content": "again"},
    ]
    body, notes = _body(history)
    assert "reasoning" not in _kinds(body)
    assert notes[HISTORY_CARRIER_FOREIGN].startswith("dropped 1 reasoning carrier")
    assert E1 not in json.dumps(body)

    # The control: the same history with the destination's own origin replays.
    matching = encode_carrier(DESTINATION, _reasoning_item("rs_1", E1))
    assert matching is not None
    history[1] = _assistant({"type": "redacted_thinking", "data": matching})
    replayed, replayed_notes = _body(history)
    assert _kinds(replayed) == ["message", "reasoning", "message"]
    assert replayed_notes[HISTORY_CARRIER_REPLAYED].startswith(
        "replayed 1 reasoning carrier"
    )


def test_an_empty_origin_field_fails_the_match_rather_than_wildcarding() -> None:
    """An empty field is evidence of a malformed envelope, not of a wildcard.

    free-claude-code's ``accepts()`` treats an empty source field as "match
    anything"; that rule exists for its legacy records. MCC's v1 envelope always
    writes all five, so an empty one is dropped as malformed.
    """

    carrier = encode_carrier(_origin_with(model=""), _reasoning_item("rs_1", E1))
    # The encoder writes what it is given; the decoder refuses it.
    assert carrier is not None
    assert decode_carrier(carrier) is None
    body, notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant({"type": "redacted_thinking", "data": carrier}),
            {"role": "user", "content": "again"},
        ]
    )
    assert "reasoning" not in _kinds(body)
    assert notes[HISTORY_CARRIER_MALFORMED].startswith("dropped 1 malformed")


# --------------------------------------------------------------------------- #
# 3. Malformed envelopes -- nine cases, drop and count, never raise
# --------------------------------------------------------------------------- #


def _payload(origin: Any, native: Any) -> str:
    """A hand-built envelope, so a malformed shape can be spelled exactly."""

    import base64

    raw = json.dumps(
        {"origin": origin, "native": native}, separators=(",", ":")
    ).encode("utf-8")
    return CARRIER_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


_GOOD_ORIGIN = DESTINATION.to_dict()
_GOOD_NATIVE = _reasoning_item("rs_1", E1)


def _truncated_base64() -> str:
    return CARRIER_PREFIX + "AAAA!!!!"


def _valid_base64_of_non_json() -> str:
    import base64

    return CARRIER_PREFIX + base64.urlsafe_b64encode(b"not json at all").decode()


def _valid_json_that_is_a_list() -> str:
    import base64

    return CARRIER_PREFIX + base64.urlsafe_b64encode(b"[1,2,3]").decode()


def _valid_json_with_no_origin() -> str:
    import base64

    raw = json.dumps({"native": _GOOD_NATIVE}, separators=(",", ":")).encode()
    return CARRIER_PREFIX + base64.urlsafe_b64encode(raw).decode()


def _origin_missing_model() -> str:
    origin = {key: value for key, value in _GOOD_ORIGIN.items() if key != "model"}
    return _payload(origin, _GOOD_NATIVE)


def _origin_model_empty() -> str:
    return _payload({**_GOOD_ORIGIN, "model": ""}, _GOOD_NATIVE)


def _native_not_an_object() -> str:
    return _payload(_GOOD_ORIGIN, "a string")


def _native_type_not_reasoning() -> str:
    return _payload(_GOOD_ORIGIN, {**_GOOD_NATIVE, "type": "message"})


def _payload_over_the_guard() -> str:
    return CARRIER_PREFIX + "A" * CARRIER_MAX_CHARS


MALFORMED: dict[str, Any] = {
    "truncated base64": _truncated_base64,
    "valid base64 of non-JSON": _valid_base64_of_non_json,
    "valid JSON that is a list": _valid_json_that_is_a_list,
    "valid JSON with no origin": _valid_json_with_no_origin,
    "origin missing model": _origin_missing_model,
    "origin.model empty": _origin_model_empty,
    "native not an object": _native_not_an_object,
    "native.type is not reasoning": _native_type_not_reasoning,
    "payload over CARRIER_MAX_CHARS": _payload_over_the_guard,
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_carrier_is_dropped_and_counted(case: str) -> None:
    """Nine shapes, one answer: no item, one count, every other byte as before.

    Decoding is total and never raises. Nothing is partially used and nothing
    falls back to "send the raw string as ``encrypted_content``" -- that is
    exactly the fake-blob failure Bifrost documents.
    """

    carrier = MALFORMED[case]()
    history = [
        {"role": "user", "content": "go"},
        _assistant({"type": "redacted_thinking", "data": carrier}),
        {"role": "user", "content": "again"},
    ]
    body, notes = _body(history)
    assert "reasoning" not in _kinds(body)
    assert notes[HISTORY_CARRIER_MALFORMED].startswith("dropped 1 malformed")

    # Every other byte is the setting-off body.
    off_body, off_notes = _body(history, origin=None)
    assert body == off_body
    assert HISTORY_CARRIER_MALFORMED not in off_notes


def test_a_block_that_is_not_our_prefix_is_never_parsed() -> None:
    """The prefix test is ``startswith`` and nothing else.

    A ``redacted_thinking`` block carrying a real Anthropic signature, MCC's
    OpenRouter JSON wrapper or a bare base64 string is not ours, is not counted,
    and is left exactly as today.
    """

    for data in ("sig-from-anthropic", '{"carrier": "x"}', "plain-base64-not-json"):
        body, notes = _body(
            [
                {"role": "user", "content": "go"},
                _assistant({"type": "redacted_thinking", "data": data}),
                {"role": "user", "content": "again"},
            ]
        )
        assert "reasoning" not in _kinds(body)
        assert not [key for key in notes if key.startswith("history_carrier_")]


def test_a_thinking_block_with_a_real_signature_is_left_alone() -> None:
    """A non-carrier ``thinking`` block is dropped exactly as it is today."""

    body, notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant({"type": "thinking", "thinking": "x", "signature": "sig"}),
            {"role": "user", "content": "again"},
        ]
    )
    assert "reasoning" not in _kinds(body)
    assert not [key for key in notes if key.startswith("history_carrier_")]


# --------------------------------------------------------------------------- #
# 4. The setting is the origin: off means byte-identical
# --------------------------------------------------------------------------- #


def test_with_no_origin_no_carrier_is_minted_or_replayed() -> None:
    """``origin=None`` is the setting off, and it is byte-identical.

    No carrier pass, no ``history_carrier_*`` note, and the stream emits the
    block it emitted before 8.16.0.
    """

    carrier = encode_carrier(DESTINATION, _reasoning_item("rs_1", E1))
    assert carrier is not None
    history = [
        {"role": "user", "content": "go"},
        _assistant({"type": "redacted_thinking", "data": carrier}),
        {"role": "user", "content": "again"},
    ]
    body, notes = _body(history, origin=None)
    assert "reasoning" not in _kinds(body)
    assert HISTORY_CARRIER_REPLAYED not in notes
    assert HISTORY_CARRIER_FOREIGN not in notes
    assert HISTORY_CARRIER_MALFORMED not in notes

    sse, converter = _run_stream(
        _frames(_reasoning_item("rs_1", E1)), origin=None, carrier_replay=False
    )
    assert "redacted_thinking" not in sse
    assert converter.carrier_counts == (0, 0)


def test_an_item_with_no_encrypted_content_mints_nothing() -> None:
    """Q8: a summary-only item has nothing to carry, and gets no carrier."""

    item = {
        "id": "rs_1",
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "a summary"}],
        "content": [],
    }
    sse, converter = _run_stream(_frames(item))
    blocks = _blocks(sse)
    assert [block["type"] for block in blocks] == ["thinking", "text"]
    assert _thinking_text(sse) == "a summary"
    assert converter.carrier_counts == (0, 0)


def test_one_block_per_item_when_a_summary_exists() -> None:
    """A summary item gets ONE thinking block, with the carrier as its signature.

    Never both a ``thinking`` block and a ``redacted_thinking`` block for one
    item: one block per item keeps the client's token accounting honest.
    """

    item = _reasoning_item(
        "rs_1", E1, summary=[{"type": "summary_text", "text": "a summary"}]
    )
    sse, converter = _run_stream(_frames(item))
    blocks = _blocks(sse)
    assert [block["type"] for block in blocks] == ["thinking", "text"]
    assert _thinking_text(sse) == "a summary"
    signatures = _signatures(sse)
    assert len(signatures) == 1
    assert signatures[0].startswith(CARRIER_PREFIX)
    assert converter.carrier_counts == (1, 0)


def test_a_carrier_over_the_guard_is_not_minted_and_is_counted() -> None:
    """Never truncate: a corrupted blob is worse than no continuity (Q6)."""

    huge = "A" * (CARRIER_MAX_CHARS + 1024)
    item = _reasoning_item("rs_1", huge)
    sse, converter = _run_stream(_frames(item))
    assert "redacted_thinking" not in sse
    assert converter.carrier_counts == (0, 1)


def test_the_second_item_never_borrows_the_first_items_thinking_block() -> None:
    """A carrier is appended only to the block that belongs to its own item.

    Nothing closes an open thinking block until text, a tool call or the end of
    the stream, so an item that arrives after a summary-bearing item still has
    that block open. Appending this item's carrier to it would bind the wrong
    blob to the wrong block.
    """

    first = _reasoning_item(
        "rs_1", E1, summary=[{"type": "summary_text", "text": "first"}]
    )
    second = _reasoning_item("rs_2", E2)
    frames = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.done", "output_index": 0, "item": first},
        {"type": "response.output_item.done", "output_index": 1, "item": second},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 2,
            "content_index": 0,
            "delta": "OK",
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    sse, converter = _run_stream(frames)
    blocks = _blocks(sse)
    assert [block["type"] for block in blocks] == [
        "thinking",
        "redacted_thinking",
        "text",
    ]
    signatures = _signatures(sse)
    assert len(signatures) == 1
    first_payload = decode_carrier(signatures[0])
    assert first_payload is not None
    assert first_payload.native["id"] == "rs_1"
    second_payload = decode_carrier(blocks[1]["data"])
    assert second_payload is not None
    assert second_payload.native["id"] == "rs_2"
    assert converter.carrier_counts == (2, 0)


def test_the_item_is_replayed_verbatim_with_no_id_minted() -> None:
    """M4/M5: ``native`` is the item the host sent, byte for byte.

    An item with no ``id`` stays without one. MCC never mints an id into the
    envelope: the host validates the ciphertext against the id it issued, and a
    synthesised id is exactly the fake-id/real-ciphertext pair Bifrost
    documents.
    """

    item = _reasoning_item(None, E1)
    carrier = encode_carrier(DESTINATION, item)
    assert carrier is not None
    decoded = decode_carrier(carrier)
    assert decoded is not None
    assert decoded.native == item
    assert "id" not in decoded.native

    body, _notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant({"type": "redacted_thinking", "data": carrier}),
            {"role": "user", "content": "again"},
        ]
    )
    reasoning = [item for item in body["input"] if item["type"] == "reasoning"]
    assert reasoning == [item]


# --------------------------------------------------------------------------- #
# 7. The wire log stores no substring of a carrier (§6 row 8)
# --------------------------------------------------------------------------- #


def test_the_request_log_stores_no_substring_of_a_carrier() -> None:
    """A replayed item is recorded as ``{"type": "reasoning"}`` and nothing more.

    ``_message_shape`` keeps only ``role``, ``type``, ``name``, the content
    shape and the tool-call names, so neither the ciphertext nor the summary
    text reaches the database. The carrier note is counts only, for the same
    reason ``history_thinking_omitted`` is.
    """

    from my_claude_code.core.wire_capture import strip_request_content

    summary_text = "a plan the model wrote"
    carrier = encode_carrier(
        DESTINATION,
        _reasoning_item(
            "rs_1", E1, summary=[{"type": "summary_text", "text": summary_text}]
        ),
    )
    assert carrier is not None
    body, notes = _body(
        [
            {"role": "user", "content": "go"},
            _assistant(
                {"type": "thinking", "thinking": summary_text, "signature": carrier}
            ),
            {"role": "user", "content": "again"},
        ]
    )

    stored = json.dumps(strip_request_content(body))
    assert E1 not in stored
    assert summary_text not in stored
    assert carrier not in stored
    # The item is still visible as structure, which is the point of the record.
    assert '"type": "reasoning"' in stored

    # And the note carries no text either.
    assert E1 not in json.dumps(notes)
    assert summary_text not in json.dumps(notes)
    assert carrier not in json.dumps(notes)


def test_normalise_endpoint_strips_the_trailing_slash_and_the_query() -> None:
    """The ``endpoint`` field is scheme + host + path, nothing else."""

    assert (
        normalise_endpoint("https://ChatGPT.com/backend-api/")
        == "https://chatgpt.com/backend-api"
    )
    assert normalise_endpoint("https://opencode.ai/zen/v1/?x=1#y") == (
        "https://opencode.ai/zen/v1"
    )
    assert normalise_endpoint("https://example.test") == "https://example.test"
    assert normalise_endpoint("") == ""


# --------------------------------------------------------------------------- #
# 5. Fallback continuity -- the user's own design goal (§13 item 10)
# --------------------------------------------------------------------------- #


def _chat_body(
    provider_id: str, messages: list[dict[str, Any]], *, model: str
) -> dict[str, Any]:
    """One Chat-door body, through the real profile and request policy."""

    from my_claude_code.providers.openai_chat import create_openai_chat_provider
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
    from my_claude_code.providers.openai_chat.request_policy import (
        build_openai_chat_request_body,
    )
    from tests.providers.support import passthrough_rate_limiter

    provider = create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )
    return build_openai_chat_request_body(
        MessagesRequest.model_validate(
            {"model": model, "max_tokens": 4096, "messages": messages}
        ),
        reasoning=ReasoningPolicy.on(),
        policy=provider._profile.request_policy,
        postprocessors=provider._profile.request_postprocessors,
        provider_id=provider_id,
    )


def test_fallback_continuity_across_models_and_providers() -> None:
    """Turn 1 on model A, turn 2 falls back to model B, turn 3 returns to A.

    The user's own design goal, verbatim: *"I want most resilience and most
    quality without sacrificing the advanced fallback mechanism's quality and
    have proper perpetuity and continuity within it."*

    Two different things travel in the history, with opposite rules (R7b.1):

    * the **encrypted carrier** is host- AND model-bound, so it is replayed only
      to the origin that minted it -- here it was minted by a third host, so
      every hop drops it and counts it;
    * **plaintext thinking is portable**, so model B's request carries model A's
      readable reasoning and model A's third request still carries its own.

    Without this test the feature can ship and still fail what was asked for:
    the readable half is what survives a fallback, and nothing else proves it.
    """

    plan = "read the failing test first, then fix the assertion"
    carrier = encode_carrier(
        ReplayOrigin(
            provider="chatgpt_oauth",
            protocol="responses",
            endpoint="https://chatgpt.com/backend-api",
            connection=connection_id("some-other-account"),
            model="gpt-6.1-sol",
        ),
        _reasoning_item("rs_foreign", E1),
    )
    assert carrier is not None

    # The transcript after turn 1 was answered by model A on a Chat door. The
    # client keeps A's readable reasoning, and it also keeps an opaque carrier
    # some other host minted -- which must never be replayed anywhere.
    after_turn_one: list[dict[str, Any]] = [
        {"role": "user", "content": "fix the test"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": plan, "signature": carrier},
                {"type": "text", "text": "Reading it."},
            ],
        },
    ]
    turn_two = [*after_turn_one, {"role": "user", "content": "now run it"}]
    turn_three = [
        *turn_two,
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "the assertion was wrong",
                    "signature": "",
                },
                {"type": "text", "text": "Fixed."},
            ],
        },
        {"role": "user", "content": "thanks"},
    ]

    # (a) B's request carries A's readable reasoning, across model AND provider.
    body_b = _chat_body("opencode_go", turn_two, model="a-go-model")
    assistant_b = [m for m in body_b["messages"] if m.get("role") == "assistant"]
    assert assistant_b and assistant_b[0]["reasoning_content"] == plan

    # (b) A's third request still carries its own earlier reasoning.
    body_a = _chat_body("opencode", turn_three, model="a-paid-model")
    assistants = [m for m in body_a["messages"] if m.get("role") == "assistant"]
    assert assistants[0]["reasoning_content"] == plan
    assert assistants[1]["reasoning_content"] == "the assertion was wrong"

    # (c) The origin-mismatched carrier is dropped and counted on every hop,
    #     and never replayed -- on the Responses door, where the machinery is.
    for history in (after_turn_one, turn_two, turn_three):
        responses_body, notes = _body(history, origin=DESTINATION)
        assert "reasoning" not in _kinds(responses_body)
        assert notes[HISTORY_CARRIER_FOREIGN].startswith(
            "dropped 1 reasoning carrier minted by another host"
        )
        assert E1 not in json.dumps(responses_body)
        # And the Chat door never turns it into text either.
        chat_body = _chat_body("opencode", history, model="a-paid-model")
        assert E1 not in json.dumps(chat_body)
        assert carrier not in json.dumps(chat_body)


# --------------------------------------------------------------------------- #
# 6. The learned refusal -- R7b.4's second addition
# --------------------------------------------------------------------------- #


def test_a_host_that_refuses_a_replayed_item_is_remembered() -> None:
    """One 400 naming a replayed item stops the next request replaying it.

    ``reasoning_reject`` remembers a 400 naming a reasoning **field**; a 400 on
    a replayed **item** in ``input[]`` is a different shape, and without a
    learned fact the first host that refuses costs a failed request every turn
    -- the carrier is rebuilt from the client's transcript each time, so there
    is nothing a single request's retry could fix.

    Three conditions, all evidence: a 400/422, words located in the ``input``
    array that name a reasoning item, and a body that actually carried one.
    """

    from my_claude_code.providers.recovery import (
        LearnedFactStore,
        complaint_refuses_a_replayed_reasoning_item,
        input_carries_reasoning_items,
    )

    # The matcher, on the two shapes a host actually writes.
    assert complaint_refuses_a_replayed_reasoning_item(
        "Invalid 'input[3]': reasoning item not found"
    )
    assert complaint_refuses_a_replayed_reasoning_item(
        "Encrypted content item_id did not match the target item id"
    )
    # Not a replayed item: a top-level field refusal, and an unrelated input.
    assert not complaint_refuses_a_replayed_reasoning_item(
        "reasoning.effort: invalid value"
    )
    assert not complaint_refuses_a_replayed_reasoning_item(
        "Invalid 'input[2].name': string too long"
    )

    # The third condition.
    assert input_carries_reasoning_items([{"type": "reasoning"}])
    assert not input_carries_reasoning_items([{"type": "message"}])
    assert not input_carries_reasoning_items(None)

    # The fact round-trips through the memory, and the reader answers.
    store = LearnedFactStore()
    memory = store.memory_for("chatgpt_oauth")
    assert not memory.responses_reasoning_item_refused_for("gpt-6.1-sol")
    assert memory.remember_responses_reasoning_item_refusal(
        "gpt-6.1-sol", evidence="Invalid 'input[3]': reasoning item not found"
    )
    assert memory.responses_reasoning_item_refused_for("gpt-6.1-sol")
    # Per model, like every other Responses fact.
    assert not memory.responses_reasoning_item_refused_for("gpt-6-luna")


def test_a_learned_refusal_stops_the_origin_being_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader is the origin builder, so the body and the stream agree.

    ``replay_origin`` returns ``None`` while the fact is fresh, which is what
    makes the builder run no carrier pass and the converter mint nothing --
    the pre-8.16.0 body, without a second switch to keep in step.
    """

    from my_claude_code.config import settings as config_settings
    from my_claude_code.providers.openai_chat.responses_transport import (
        ResponsesTransport,
    )
    from my_claude_code.providers.recovery import LearnedFactStore
    from tests.providers.support import passthrough_rate_limiter

    monkeypatch.setenv("RESPONSES_CARRIER_REPLAY", "true")
    config_settings.get_settings.cache_clear()
    try:
        store = LearnedFactStore()
        memory = store.memory_for("opencode")
        transport = ResponsesTransport(
            ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
            base_url="https://opencode.ai/zen/v1",
            provider_name="OPENCODE",
            provider_id="opencode",
            identity=None,
            api_key="sk-test",
            rate_limiter=passthrough_rate_limiter(),
            memory=memory,
        )
        # With the setting on and nothing learned, an origin is built.
        assert transport.replay_origin("muse-spark-1.3") is not None
        memory.remember_responses_reasoning_item_refusal("muse-spark-1.3")
        assert transport.replay_origin("muse-spark-1.3") is None
        # A different model on the same host is unaffected.
        assert transport.replay_origin("another-model") is not None
    finally:
        config_settings.get_settings.cache_clear()
