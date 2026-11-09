"""The plaintext replay's behaviour contract: §8's thirteen cases (8.17.0).

``test_carrier_replay_equality.py`` proves the *equality* claim -- off is
byte-identical, on adds only ``input`` items. This file proves the *behaviour*:
which block takes which path, what the item looks like, what the note says, and
that nothing reaches the client as an exception.

The dynamic rule (spec §2.1), restated because every case below is one row of
it:

============================  ==========================  ==================
what the block is             what it carries             what happens
============================  ==========================  ==================
``thinking``                  a matching carrier          carrier path
``thinking``                  a malformed carrier         text takes the
                                                          plaintext path
``thinking``                  a foreign carrier           text takes the
                                                          plaintext path
``thinking``                  no carrier, readable text   plaintext path
``thinking``                  no carrier, blank text      nothing
``redacted_thinking``         anything                    nothing (no text)
string turn w/                ``reasoning_content``       plaintext path
``reasoning_content``
============================  ==========================  ==================

**The 7.58.1 ban is absolute and is asserted in every case that matters.** No
``<think`` substring is ever written, nothing lands in ``output_text``, and the
only place a model's own words appear is a ``reasoning`` item's ``summary`` --
a field with no room for a tag.

All offline. No network, no live call, no fake upstream needed for the builder
cases; the two sender cases use ``httpx.MockTransport``.
"""

import copy
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import (
    ReplayOrigin,
    connection_id,
    encode_carrier,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_MALFORMED,
    HISTORY_CARRIER_REPLAYED,
    HISTORY_PLAINTEXT_REPLAYED,
    HISTORY_THINKING_OMITTED,
    build_responses_request_body,
    history_plaintext_marker,
)
from tests.contracts.carrier_replay_note_expectations import corrected_note

ORIGIN = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="gpt-5.6-terra",
)
OTHER_ORIGIN = ReplayOrigin(
    provider="opencode",
    protocol="responses",
    endpoint="https://opencode.ai/zen/v1",
    connection=connection_id("sk-test"),
    model="muse-spark-1.3-contributor-free",
)

E1 = "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"

#: The readable reasoning. One string per case would make "the text is in
#: ``input``" ambiguous; a distinct one per case keeps every assertion sharp.
T = "the previous model's readable plan"
T2 = "a second readable thought"


def _carrier(origin: ReplayOrigin = ORIGIN, item_id: str | None = "rs_1") -> str:
    item: dict[str, Any] = {
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "content": [],
        "encrypted_content": E1,
    }
    if item_id is not None:
        item = {"id": item_id, **item}
    encoded = encode_carrier(origin, item)
    assert encoded is not None
    return encoded


def _thinking(text: str, signature: str = "sig") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def _tool_use(tool_id: str, name: str = "Read") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tool_result(tool_id: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
    }


def _history(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": [block, {"type": "text", "text": "ok"}]},
        {"role": "user", "content": "two"},
    ]


def _build(
    messages: list[dict[str, Any]],
    *,
    origin: ReplayOrigin | None = None,
    plaintext_replay: bool = True,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    return build_responses_request_body(
        MessagesRequest.model_validate(
            {"model": "gpt-5.6-terra", "max_tokens": 1024, "messages": messages}
        ),
        reasoning=ReasoningPolicy.off(),
        origin=origin,
        plaintext_replay=plaintext_replay,
        wire_notes=notes,
    )


def _reasoning_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in body["input"] if item.get("type") == "reasoning"]


def _summary_item(text: str) -> dict[str, Any]:
    return {"type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}


def _wire(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Case 1 -- a plaintext thinking block, no carrier, setting on
# --------------------------------------------------------------------------- #


def test_case_1_a_plaintext_block_is_replayed_as_a_summary_item() -> None:
    """The item, its key set, and both halves of the 7.58.1 ban."""

    body = _build(_history(_thinking(T)), origin=ORIGIN)

    assert body["input"][1] == _summary_item(T)
    # The whole key set, not a "contains" check: no id, no encrypted_content,
    # no status, no content, no role.
    assert sorted(body["input"][1]) == ["summary", "type"]
    # The format ban, absolute.
    assert "<think" not in _wire(body)
    assert "</think>" not in _wire(body)
    # And nothing landed in output_text: the assistant message item is exactly
    # what the setting-off body carried.
    off = _build(_history(_thinking(T)), origin=ORIGIN, plaintext_replay=False)
    assistant_off = [
        item
        for item in off["input"]
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    assistant_on = [
        item
        for item in body["input"]
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    assert assistant_on == assistant_off


def test_case_1_the_text_appears_only_inside_a_reasoning_items_summary() -> None:
    """§6.4's scoped assertion: present, and present *only* there.

    This is the assertion that replaces the "and the thought" half of
    ``test_no_input_item_carries_a_think_tag_or_the_thought``. The
    ``<think``/``</think>`` half of that test is unchanged and still runs.
    """

    body = _build(_history(_thinking(T)), origin=ORIGIN)
    for item in body["input"]:
        if item.get("type") == "reasoning":
            assert [part["text"] for part in item["summary"]] == [T]
            continue
        assert T not in _wire(item)


def test_case_1_the_note_counts_the_replayed_block_as_replayed() -> None:
    """M8: a replayed block is never also counted as omitted."""

    notes: dict[str, str] = {}
    _build(_history(_thinking(T)), origin=ORIGIN, notes=notes)
    assert HISTORY_THINKING_OMITTED not in notes
    assert notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )
    assert T not in _wire(notes)


# --------------------------------------------------------------------------- #
# Case 2 -- the same history, setting off
# --------------------------------------------------------------------------- #


def test_case_2_with_the_setting_off_the_text_is_nowhere() -> None:
    body = _build(_history(_thinking(T)), origin=ORIGIN, plaintext_replay=False)
    assert not _reasoning_items(body)
    assert T not in _wire(body["input"])
    notes: dict[str, str] = {}
    _build(
        _history(_thinking(T)),
        origin=ORIGIN,
        plaintext_replay=False,
        notes=notes,
    )
    assert HISTORY_PLAINTEXT_REPLAYED not in notes
    assert notes[HISTORY_THINKING_OMITTED].endswith(
        "the Responses surface replays no reasoning as text"
    )


def _note_cell(text: str) -> str:
    """The ``sha256:… bytes:…`` cell the note-expectation file records.

    The same cell shape the historical baselines store, so a recorded
    correction is a before/after *digest* pair rather than a copy of the
    sentence -- which is what makes "the sentence moved" and "the sentence did
    not move" both mechanical.
    """

    raw = text.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()} bytes:{len(raw)}"


def test_case_2_the_setting_off_note_is_the_8_16_3_sentence() -> None:
    """§6.3: the note's tail moves only when the setting is on.

    Recorded in ``carrier_replay_note_expectations.json`` with a before/after
    digest pair, the same discipline PR-2 used for its one deliberate note
    change.

    The fixture carries **two** assistant blocks: a plaintext ``thinking``
    block, which the setting replays, and a ``redacted_thinking`` block, which
    has no text and is therefore still omitted. That is the only shape in which
    the sentence is reachable with the setting on -- when everything was
    replayed there is no note at all, which the next test asserts.
    """

    mixed = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                _thinking(T),
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "two"},
    ]
    off_notes: dict[str, str] = {}
    on_notes: dict[str, str] = {}
    _build(mixed, plaintext_replay=False, notes=off_notes)
    _build(mixed, plaintext_replay=True, notes=on_notes)

    historical = off_notes[HISTORY_THINKING_OMITTED]
    assert historical.endswith("the Responses surface replays no reasoning as text")
    # The recorded correction: before is the setting-off cell, after is the
    # setting-on one. ``corrected_note`` fails if the historical bytes moved or
    # if the correction corrects nothing.
    corrected = corrected_note(
        "plaintext", "plain_thinking_block/responses_wire_notes", _note_cell(historical)
    )
    assert corrected != _note_cell(historical)
    # And the live setting-on build produces exactly the recorded value: the
    # count drops by one (the replayed block), the chars drop with it, and the
    # tail is the new sentence.
    live = on_notes[HISTORY_THINKING_OMITTED]
    assert _note_cell(live) == corrected
    assert live.endswith(
        "the Responses surface replays reasoning only as structured items"
    )
    assert live.startswith("omitted 1 earlier thinking")
    assert on_notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )


def test_case_2_a_fully_replayed_history_records_no_omission_note() -> None:
    """The stronger statement: when nothing was left out, there is no note."""

    notes: dict[str, str] = {}
    _build(_history(_thinking(T)), plaintext_replay=True, notes=notes)
    assert HISTORY_THINKING_OMITTED not in notes
    assert notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )


# --------------------------------------------------------------------------- #
# Case 3 -- blank text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", " ", "\n\t  \n"])
def test_case_3_a_blank_thinking_block_produces_nothing(blank: str) -> None:
    body = _build(_history(_thinking(blank)), origin=ORIGIN)
    assert not _reasoning_items(body)
    notes: dict[str, str] = {}
    _build(_history(_thinking(blank)), origin=ORIGIN, notes=notes)
    assert HISTORY_PLAINTEXT_REPLAYED not in notes
    assert HISTORY_THINKING_OMITTED in notes


# --------------------------------------------------------------------------- #
# Case 4 -- redacted_thinking has no text field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "data",
    ["opaque", json.dumps({"carrier": E1}), "cGxhaW4tYmFzZTY0LW5vdC1vdXJz"],
)
def test_case_4_a_redacted_block_produces_nothing(data: str) -> None:
    """M12: the pass must not read ``data``. There is no text in it."""

    body = _build(_history({"type": "redacted_thinking", "data": data}), origin=ORIGIN)
    assert not _reasoning_items(body)
    assert data not in _wire(body)
    notes: dict[str, str] = {}
    _build(
        _history({"type": "redacted_thinking", "data": data}),
        origin=ORIGIN,
        notes=notes,
    )
    assert HISTORY_PLAINTEXT_REPLAYED not in notes
    assert HISTORY_THINKING_OMITTED in notes


# --------------------------------------------------------------------------- #
# Case 5 -- a foreign carrier: refused, and its text replayed
# --------------------------------------------------------------------------- #


def test_case_5_a_foreign_carrier_is_refused_and_its_text_replayed() -> None:
    """§2.3, the single most important behavioural difference between doors."""

    notes: dict[str, str] = {}
    body = _build(
        _history(_thinking(T, _carrier(OTHER_ORIGIN))), origin=ORIGIN, notes=notes
    )

    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )
    assert body["input"][1] == _summary_item(T)
    # The envelope's own bytes never reach the wire.
    assert E1 not in _wire(body)


# --------------------------------------------------------------------------- #
# Case 6 -- the same history at its own origin: carrier, not doubled
# --------------------------------------------------------------------------- #


def test_case_6_an_accepted_carrier_is_not_doubled_by_its_text() -> None:
    """M5: a block whose carrier was accepted must not also replay its text."""

    notes: dict[str, str] = {}
    body = _build(_history(_thinking(T, _carrier(ORIGIN))), origin=ORIGIN, notes=notes)

    items = _reasoning_items(body)
    assert len(items) == 1
    assert "encrypted_content" in items[0]
    assert notes[HISTORY_CARRIER_REPLAYED] == (
        "replayed 1 reasoning carrier from the client's history"
    )
    assert HISTORY_PLAINTEXT_REPLAYED not in notes
    # The text is not sent twice.
    assert T not in _wire(body)


# --------------------------------------------------------------------------- #
# Case 7 -- a malformed carrier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "signature",
    [
        "mcc:reasoning:v1:",
        "mcc:reasoning:v1:not-base64!!!",
        "mcc:reasoning:v1:" + "eyJub3QiOiJhbiBvYmplY3QifQ",
    ],
)
def test_case_7_a_malformed_carrier_replays_its_text_and_never_raises(
    signature: str,
) -> None:
    notes: dict[str, str] = {}
    body = _build(_history(_thinking(T, signature)), origin=ORIGIN, notes=notes)

    assert notes[HISTORY_CARRIER_MALFORMED] == (
        "dropped 1 malformed reasoning carrier from the client's history"
    )
    assert notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )
    assert body["input"][1] == _summary_item(T)


# --------------------------------------------------------------------------- #
# Case 8 -- the string-content turn with reasoning_content
# --------------------------------------------------------------------------- #


def test_case_8_a_string_turn_replays_its_reasoning_content() -> None:
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "done", "reasoning_content": T},
        {"role": "user", "content": "two"},
    ]
    notes: dict[str, str] = {}
    body = _build(messages, origin=ORIGIN, notes=notes)

    assert body["input"][1] == _summary_item(T)
    assert notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )
    assert HISTORY_THINKING_OMITTED not in notes


@pytest.mark.parametrize("reasoning", ["", "   ", 123, None, ["a", "list"]])
def test_case_8_a_non_string_reasoning_content_is_skipped(reasoning: Any) -> None:
    """The request model refuses a non-string ``reasoning_content`` before the
    builder sees it, so the builder's own totality is asserted where it lives:
    :func:`_plaintext_items`, over a hand-built message object."""

    from my_claude_code.providers.openai_responses.conversion import _plaintext_items

    message = SimpleNamespace(role="assistant", content="done")
    message.reasoning_content = reasoning
    counts: dict[str, int] = {}
    items, replayed = _plaintext_items([message], counts, set())
    assert items == {}
    assert replayed == set()
    assert counts == {}


# --------------------------------------------------------------------------- #
# Case 9 -- the other doors never move
# --------------------------------------------------------------------------- #


def test_case_9_the_messages_and_chat_doors_are_byte_identical() -> None:
    """Held in ``test_carrier_replay_equality.py`` over the whole corpus; this
    is the same claim stated once, where a reader of this file will see it."""

    from tests.contracts.test_carrier_replay_equality import (
        CORPUS,
        POLICIES,
        _bodies,
        _digest,
    )

    for fixture in CORPUS:
        for policy_name in POLICIES:
            off = _bodies(fixture, policy_name, origin=None)
            on = _bodies(fixture, policy_name, origin=ORIGIN, plaintext_replay=True)
            for door in off:
                if door.startswith("responses/"):
                    continue
                assert _digest(on[door]) == _digest(off[door]), (fixture, door)


# --------------------------------------------------------------------------- #
# Case 10 -- both producers in one turn, carrier first
# --------------------------------------------------------------------------- #


def test_case_10_a_turn_carrying_both_replays_the_carrier_first() -> None:
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier(ORIGIN)),
                _thinking(T2),
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    body = _build(messages, origin=ORIGIN)

    types = [item.get("type") for item in body["input"]]
    assert types == ["message", "reasoning", "reasoning", "message", "message"]
    first, second = body["input"][1], body["input"][2]
    assert "encrypted_content" in first
    assert second == _summary_item(T2)


# --------------------------------------------------------------------------- #
# Case 11 -- a multi-turn tool loop, one item per turn at its own head
# --------------------------------------------------------------------------- #


def test_case_11_a_multi_turn_tool_loop_places_one_item_per_turn() -> None:
    """M11: the item goes at the head of its own turn's run, not at the end."""

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [_thinking("first thought"), _tool_use("call_1")],
        },
        _tool_result("call_1"),
        {
            "role": "assistant",
            "content": [_thinking("second thought"), _tool_use("call_2", "Grep")],
        },
        _tool_result("call_2"),
        {
            "role": "assistant",
            "content": [_thinking("third thought"), {"type": "text", "text": "done"}],
        },
        {"role": "user", "content": "again"},
    ]
    body = _build(messages, origin=ORIGIN)

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "message",
        "message",
    ]
    assert body["input"][1] == _summary_item("first thought")
    assert body["input"][4] == _summary_item("second thought")
    assert body["input"][7] == _summary_item("third thought")


# --------------------------------------------------------------------------- #
# Case 12 -- purity: the client's history is never mutated
# --------------------------------------------------------------------------- #


def test_case_12_the_history_is_never_mutated() -> None:
    """M10: the pass is a property of the destination, not of the transcript.

    Asserted on **both** histories a caller can see: the raw dicts handed in,
    and the ``MessagesRequest`` the builder actually walks. The second one is
    the one a careless implementation would touch -- ``request.messages`` is
    the object ``_plaintext_items`` iterates -- and pydantic copies the raw
    dicts, so a mutation there would be invisible to a check that only looked
    at the caller's list.
    """

    messages = _history(_thinking(T, _carrier(OTHER_ORIGIN)))
    before = copy.deepcopy(messages)
    request = MessagesRequest.model_validate(
        {"model": "gpt-5.6-terra", "max_tokens": 1024, "messages": messages}
    )
    request_before = request.model_dump(mode="json")

    body = build_responses_request_body(
        request,
        reasoning=ReasoningPolicy.off(),
        origin=ORIGIN,
        plaintext_replay=True,
    )

    assert json.dumps(messages, sort_keys=True) == json.dumps(before, sort_keys=True)
    assert request.model_dump(mode="json") == request_before
    # And the items the body carries are new dicts, not the block's own.
    for item in _reasoning_items(body):
        assert item is not messages[1]["content"][0]
    # Building twice gives the same answer: no accumulated state anywhere.
    again = build_responses_request_body(
        request,
        reasoning=ReasoningPolicy.off(),
        origin=ORIGIN,
        plaintext_replay=True,
    )
    assert json.dumps(again, sort_keys=True) == json.dumps(body, sort_keys=True)


# --------------------------------------------------------------------------- #
# Case 13 -- the wire log carries no text
# --------------------------------------------------------------------------- #


def test_case_13_the_wire_log_carries_no_reasoning_text() -> None:
    """Counts only, never text -- the rule every ``history_*`` note keeps."""

    from my_claude_code.core.wire_capture import strip_request_content

    notes: dict[str, str] = {}
    body = _build(_history(_thinking(T)), origin=ORIGIN, notes=notes)
    stripped = strip_request_content({**body, **notes})

    assert T not in json.dumps(stripped, ensure_ascii=False)
    assert notes[HISTORY_PLAINTEXT_REPLAYED] == (
        "replayed 1 readable reasoning block from the client's history"
    )
    # The marker is the only thing the note list adds, and it is a count.
    assert history_plaintext_marker(notes) == {
        HISTORY_PLAINTEXT_REPLAYED: notes[HISTORY_PLAINTEXT_REPLAYED]
    }
    assert history_plaintext_marker(None) == {}
    assert history_plaintext_marker({}) == {}


# --------------------------------------------------------------------------- #
# §8.1 -- no exception reaches the client, whatever the shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "block",
    [
        "not-a-dict",
        42,
        None,
        b"bytes",
        ["a", "list"],
        {"type": "thinking"},
        {"type": "thinking", "thinking": 123},
        {"type": "thinking", "thinking": None},
        {"type": "thinking", "thinking": ["a", "list"]},
        {"type": "thinking", "thinking": {"a": "dict"}},
        {"type": "thinking", "thinking": T, "signature": 42},
        {"type": "redacted_thinking"},
        {"type": "redacted_thinking", "data": 42},
        {"type": "text", "text": T},
        {"type": "tool_use", "id": "x", "name": "Read", "input": {}},
    ],
)
def test_no_shape_raises_and_none_is_partially_used(block: Any) -> None:
    """§2.5: a block that is not a dict, a ``thinking`` field that is not a
    string, a ``content`` that is neither list nor string -- each is **skipped**,
    never raised and never partially used.

    Asserted at the pass itself rather than through ``MessagesRequest``: the
    request model refuses most of these shapes before the builder is reached,
    which is a *different* gate. The builder's own totality is the claim here,
    so the message object is hand-built and the model is bypassed.
    """

    from my_claude_code.providers.openai_responses.conversion import _plaintext_items

    message = SimpleNamespace(role="assistant", content=[block])
    counts: dict[str, int] = {}
    items, replayed = _plaintext_items([message], counts, set())

    # Whatever happened, nothing non-string was ever used as a summary text.
    for produced in items.get(0, []):
        assert sorted(produced) == ["summary", "type"]
        for part in produced["summary"]:
            assert isinstance(part["text"], str)
    # And the count agrees with what was produced.
    assert counts.get(HISTORY_PLAINTEXT_REPLAYED, 0) == len(items.get(0, []))
    assert len(replayed) == len(items.get(0, []))


@pytest.mark.parametrize("content", [None, 42, {"a": "dict"}, b"bytes", ["a", "list"]])
def test_a_content_that_is_neither_list_nor_string_is_skipped(content: Any) -> None:
    from my_claude_code.providers.openai_responses.conversion import _plaintext_items

    message = SimpleNamespace(role="assistant", content=content)
    counts: dict[str, int] = {}
    items, replayed = _plaintext_items([message], counts, set())
    assert items == {}
    assert replayed == set()
    assert counts == {}


# --------------------------------------------------------------------------- #
# The setting reaches both senders, and the learned fact gates both
# --------------------------------------------------------------------------- #


def test_the_learned_refusal_turns_the_plaintext_pass_off() -> None:
    """M2: a host that refused a replayed item is never offered another one.

    The fact is keyed on ``(provider, model)`` and it disables both paths at
    once, because both send a ``reasoning`` input item. The setting is the
    operator's escape hatch when that is too coarse.
    """

    from my_claude_code.config import settings as config_settings
    from my_claude_code.providers.base import ProviderConfig
    from my_claude_code.providers.chatgpt_oauth.provider import (
        CHATGPT_OAUTH_DEFAULT_BASE,
        ChatGPTOAuthProvider,
    )
    from my_claude_code.providers.recovery import learned_fact_store
    from tests.providers.support import passthrough_rate_limiter

    config_settings.get_settings.cache_clear()
    try:
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
        model = "gpt-5.6-terra"
        assert provider._plaintext_replay(model) is True

        learned_fact_store().memory_for(
            "chatgpt_oauth"
        ).remember_responses_reasoning_item_refusal(
            model, evidence="Invalid 'input[3]': reasoning item not found"
        )

        assert provider._plaintext_replay(model) is False
        # And the carrier path is off from the same fact, from the same place.
        assert provider._replay_origin(None, model) is None
        # A different model on the same host is unaffected.
        assert provider._plaintext_replay("gpt-6-luna") is True
    finally:
        config_settings.get_settings.cache_clear()


def test_the_plaintext_setting_off_leaves_carriers_alone() -> None:
    """§9.4: the two settings are independent, which is the whole point.

    An operator whose host refuses a summary item turns this one off and keeps
    carriers. Proved by turning it off and finding the carrier path still open.
    """

    from my_claude_code.config import settings as config_settings
    from my_claude_code.providers.base import ProviderConfig
    from my_claude_code.providers.chatgpt_oauth.provider import (
        CHATGPT_OAUTH_DEFAULT_BASE,
        ChatGPTOAuthProvider,
    )
    from tests.providers.support import passthrough_rate_limiter

    def _provider() -> ChatGPTOAuthProvider:
        return ChatGPTOAuthProvider(
            ProviderConfig(
                api_key="test_token",
                base_url=CHATGPT_OAUTH_DEFAULT_BASE,
                rate_limit=10,
                rate_window=60,
                max_concurrency=5,
            ),
            rate_limiter=passthrough_rate_limiter(),
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        # Both on: the shipped default.
        monkeypatch.delenv("RESPONSES_PLAINTEXT_REPLAY", raising=False)
        monkeypatch.delenv("RESPONSES_CARRIER_REPLAY", raising=False)
        config_settings.get_settings.cache_clear()
        provider = _provider()
        assert provider._plaintext_replay("gpt-5.6-terra") is True
        assert provider._replay_origin(None, "gpt-5.6-terra") is not None

        # Plaintext off, carriers untouched.
        monkeypatch.setenv("RESPONSES_PLAINTEXT_REPLAY", "false")
        config_settings.get_settings.cache_clear()
        provider = _provider()
        assert provider._plaintext_replay("gpt-5.6-terra") is False
        assert provider._replay_origin(None, "gpt-5.6-terra") is not None

        # Carriers off, plaintext untouched.
        monkeypatch.setenv("RESPONSES_PLAINTEXT_REPLAY", "true")
        monkeypatch.setenv("RESPONSES_CARRIER_REPLAY", "false")
        config_settings.get_settings.cache_clear()
        provider = _provider()
        assert provider._plaintext_replay("gpt-5.6-terra") is True
        assert provider._replay_origin(None, "gpt-5.6-terra") is None
    finally:
        monkeypatch.undo()
        config_settings.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The note change is recorded, and the record bites
# --------------------------------------------------------------------------- #


def test_the_recorded_note_correction_is_not_vacuous() -> None:
    """A correction that corrects nothing is a hole in the guard."""

    with pytest.raises(AssertionError):
        corrected_note(
            "plaintext",
            "plain_thinking_block/responses_wire_notes",
            "sha256:0000 bytes:0",
        )


def test_the_recorded_note_correction_covers_a_real_case() -> None:
    from tests.contracts.carrier_replay_note_expectations import corrections

    assert "plain_thinking_block/responses_wire_notes" in corrections("plaintext")
