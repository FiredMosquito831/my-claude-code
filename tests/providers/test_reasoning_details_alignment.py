"""``reasoning_details`` replay lands on the turn that owns it (8.18.1).

Before this release the replay was a **positional walk**: it collected each
assistant turn's ``redacted_thinking`` details into a list (appending only when
a turn yielded any) and then attached them to the next assistant message **in
order**. A turn that yielded none was absent from the source list but still
present in the destination walk, so every later turn's details shifted up by
one -- a model read another turn's reasoning as its own.

The fix attaches during conversion, by **object identity**, reusing the
``reasoning_by_message`` in/out out-parameter ``convert_messages`` already
takes for the Responses door. There is no walk any more, so there is nothing to
shift.

Every assertion below spells out the **whole mapping**, never a "contains"
check: a ``"D2" in body`` test passes for a detail on the wrong turn, which is
exactly the defect.

The equality cost is stated in the module's last section: seven of the ten
histories are byte-identical and three move, each a correction.
"""

import copy
import json
from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest

from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.anthropic.conversion import (
    AnthropicToOpenAIConverter,
    build_base_request_body,
    hoist_tool_result_images,
    is_synthetic_openai_tool_turn_boundary,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_chat import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from my_claude_code.providers.openrouter_gateway import (
    assistant_reasoning_details as gateway_reasoning_details,
)
from my_claude_code.providers.openrouter_gateway import (
    openrouter_gateway_profile,
)

#: The five profiles that replay ``reasoning_details``. Two declare the
#: ``openai_chat`` copy and three the ``openrouter_gateway`` copy, so a fix to
#: one copy cannot pass.
CHAT_COPY_PROFILES = ("cline", "zenmux")
GATEWAY_COPY_PROFILES = ("open_router", "kilo", "nous_portal")
REPLAY_PROFILES = (*CHAT_COPY_PROFILES, *GATEWAY_COPY_PROFILES)

#: A 1x1 PNG, so a tool result can carry an image.
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------- #
# Fixtures — one builder per shape, so every case reads as the history it is
# --------------------------------------------------------------------------- #


def _detail(tag: str) -> dict[str, Any]:
    """One ``redacted_thinking`` block whose payload names the turn it owns."""

    return {
        "type": "redacted_thinking",
        "data": json.dumps({"type": "reasoning.encrypted", "data": tag}),
    }


def _assistant(text: str, *blocks: Any, detail: str | None = None) -> dict[str, Any]:
    content: list[Any] = []
    if detail is not None:
        content.append(_detail(detail))
    content.extend(blocks)
    content.append({"type": "text", "text": text})
    return {"role": "assistant", "content": content}


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _tool_use(tool_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": "look", "input": {}}


def _tool_result(tool_id: str, *content: Any) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": list(content) if content else "ok",
            }
        ],
    }


def _image() -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG},
    }


def _profile(provider_id: str):
    if provider_id in OPENAI_CHAT_PROFILES:
        return OPENAI_CHAT_PROFILES[provider_id]
    return openrouter_gateway_profile(provider_id.upper())


def _build(
    provider_id: str,
    messages: list[dict[str, Any]],
    *,
    system: Any = None,
) -> dict[str, Any]:
    """The real builder, the real profile, the real postprocessors."""

    payload: dict[str, Any] = {"model": "m", "max_tokens": 64, "messages": messages}
    if system is not None:
        payload["system"] = system
    profile = _profile(provider_id)
    return build_openai_chat_request_body(
        MessagesRequest.model_validate(payload),
        reasoning=ReasoningPolicy.off(),
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id=provider_id,
    )


def _mapping(body: dict[str, Any]) -> dict[int, list[str]]:
    """Every ``reasoning_details`` entry the body carries, by message index."""

    out: dict[int, list[str]] = {}
    for index, message in enumerate(body["messages"]):
        details = message.get("reasoning_details")
        if isinstance(details, list) and details:
            out[index] = [detail.get("data") for detail in details]
    return out


def _roles(body: dict[str, Any]) -> list[tuple[int, str]]:
    return [(index, m.get("role")) for index, m in enumerate(body["messages"])]


# --------------------------------------------------------------------------- #
# §6.1 — the six hard cases, each asserting the whole mapping
# --------------------------------------------------------------------------- #

#: ``(history, expected mapping)``. The expected mapping is the **full** answer:
#: every index that carries details, and nothing else.
HARD_CASES: dict[str, tuple[list[dict[str, Any]], dict[int, list[str]]]] = {
    # Case 6 — the plain defect. Turn 2 yields nothing, so before the fix turn
    # 3's detail landed on turn 2's message and turn 3 got nothing.
    "plain three turns, middle has none": (
        [
            _user("hi"),
            _assistant("a1", detail="D1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3", detail="D3"),
        ],
        {1: ["D1"], 5: ["D3"]},
    ),
    # Case 7 — the synthetic boundary. ``_close_openai_tool_result_turns``
    # inserts an assistant message at 3 whose only content is ``" "``; the
    # gateway copy used to attach D2 **to it**.
    "one tool round": (
        [
            _user("hi"),
            {"role": "assistant", "content": [_detail("D1"), _tool_use("t1")]},
            _tool_result("t1"),
            _user("continue"),
            _assistant("a2", detail="D2"),
        ],
        {1: ["D1"], 5: ["D2"]},
    ),
    # Case 8 — the split turn. One source turn emits the pre-tool message AND a
    # deferred post-tool message, so the source and destination lists have
    # different cardinality for reasons a postprocessor cannot see.
    "assistant split by a tool_use": (
        [
            _user("hi"),
            {
                "role": "assistant",
                "content": [
                    _detail("D1"),
                    _tool_use("t1"),
                    {"type": "text", "text": "after the call"},
                ],
            },
            _tool_result("t1"),
            _user("go"),
            _assistant("a2", detail="D2"),
        ],
        {1: ["D1"], 5: ["D2"]},
    ),
    # Case 10 — the user coalescing moves the index and identity follows it.
    "consecutive user turns": (
        [_user("hi"), _user("again"), _assistant("a1", detail="D1")],
        {1: ["D1"]},
    ),
    # A turn whose only block is the opaque detail still owns it.
    "thinking-only assistant turn": (
        [
            _user("hi"),
            {"role": "assistant", "content": [_detail("D1")]},
            _user("q2"),
            {"role": "assistant", "content": [_detail("D2")]},
        ],
        {1: ["D1"], 3: ["D2"]},
    ),
    # Two tool rounds, each turn's text BEFORE its tool_use, so each turn
    # produces exactly one converted assistant message.
    "two tool rounds back to back": (
        [
            _user("hi"),
            {
                "role": "assistant",
                "content": [
                    _detail("D1"),
                    {"type": "text", "text": "a1"},
                    _tool_use("t1"),
                ],
            },
            _tool_result("t1"),
            {
                "role": "assistant",
                "content": [
                    _detail("D2"),
                    {"type": "text", "text": "a2"},
                    _tool_use("t2"),
                ],
            },
            _tool_result("t2"),
        ],
        {1: ["D1"], 3: ["D2"]},
    ),
}


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
@pytest.mark.parametrize("case", sorted(HARD_CASES))
def test_the_details_land_on_the_turn_that_owns_them(
    provider_id: str, case: str
) -> None:
    """§6.1. The whole mapping, on both copies, on every hard case."""

    history, expected = HARD_CASES[case]
    body = _build(provider_id, copy.deepcopy(history))

    assert _mapping(body) == expected


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_the_synthetic_boundary_never_carries_details(provider_id: str) -> None:
    """§6.2 M9. The gateway copy's old defect, pinned from the other side.

    ``_close_openai_tool_result_turns`` inserts an assistant message MCC itself
    wrote; a turn's reasoning must never land on it. Asserted from both ends:
    the boundary carries nothing, **and** every detail the body does carry sits
    on a real assistant message. The second half is what catches a mutation
    that redirects the write to some other assistant message.
    """

    body = _build(provider_id, copy.deepcopy(HARD_CASES["one tool round"][0]))

    boundaries = [
        index
        for index, message in enumerate(body["messages"])
        if is_synthetic_openai_tool_turn_boundary(message)
    ]
    assert boundaries, "the fixture must produce a synthetic boundary"
    for index in boundaries:
        assert "reasoning_details" not in body["messages"][index]

    for index in _mapping(body):
        assert not is_synthetic_openai_tool_turn_boundary(body["messages"][index])
    assert _mapping(body) == {1: ["D1"], 5: ["D2"]}


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_turn_that_yields_nothing_sends_nothing(provider_id: str) -> None:
    """§6.2 M2/M4. The middle turn carries no key at all — not an empty list.

    An empty ``reasoning_details: []`` would be a different body from every
    release before this one and says nothing; absence is the pre-8.16.2 shape.
    """

    body = _build(
        provider_id, copy.deepcopy(HARD_CASES["plain three turns, middle has none"][0])
    )

    assert "reasoning_details" not in body["messages"][3]
    assert _mapping(body) == {1: ["D1"], 5: ["D3"]}


# --------------------------------------------------------------------------- #
# §6.1 cases 5 and 6 — the system-message shift, and its absence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_system_prompt_shifts_every_index_by_exactly_one(provider_id: str) -> None:
    """§6.2 M3. A leading system message moves every message down one."""

    history = copy.deepcopy(HARD_CASES["plain three turns, middle has none"][0])
    body = _build(provider_id, history, system="be brief")

    assert body["messages"][0]["role"] == "system"
    assert _mapping(body) == {2: ["D1"], 6: ["D3"]}


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_no_system_prompt_means_no_shift(provider_id: str) -> None:
    """§6.2 M5. The shift happens only when a system message was inserted."""

    body = _build(
        provider_id,
        copy.deepcopy(HARD_CASES["plain three turns, middle has none"][0]),
    )

    assert body["messages"][0]["role"] == "user"
    assert _mapping(body) == {1: ["D1"], 5: ["D3"]}


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_an_empty_system_prompt_does_not_shift(provider_id: str) -> None:
    """A ``system`` that converts to nothing inserts nothing, so nothing moves.

    ``convert_system_prompt`` returns ``None`` for an empty list, and the shift
    must follow the **insertion**, not the presence of the field.
    """

    body = _build(
        provider_id,
        copy.deepcopy(HARD_CASES["plain three turns, middle has none"][0]),
        system=[],
    )

    assert body["messages"][0]["role"] == "user"
    assert _mapping(body) == {1: ["D1"], 5: ["D3"]}


# --------------------------------------------------------------------------- #
# The image hoist — a second, non-constant mover
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_hoisted_tool_image_does_not_move_the_details(provider_id: str) -> None:
    """``hoist_tool_result_images`` INSERTs a message, so the mapping is re-resolved.

    A tool result carrying a picture gains a ``role: user`` message after the
    tool run, which moves every later message by one. The mapping is resolved by
    object identity against the hoisted list, so the detail still lands on the
    assistant turn that owns it rather than one message early.
    """

    history = [
        _user("hi"),
        {"role": "assistant", "content": [_detail("D1"), _tool_use("t1")]},
        _tool_result("t1", _image()),
        _user("continue"),
        _assistant("a2", detail="D2"),
    ]
    body = _build(provider_id, history)

    hoisted = [
        index
        for index, message in enumerate(body["messages"])
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message["content"]
        )
    ]
    assert hoisted, "the fixture must produce a hoisted image message"

    assistant_indexes = [index for index, role in _roles(body) if role == "assistant"]
    assert _mapping(body) == {
        assistant_indexes[0]: ["D1"],
        assistant_indexes[-1]: ["D2"],
    }
    # And specifically: the detail is NOT on the message before its owner.
    for index in _mapping(body):
        assert body["messages"][index]["role"] == "assistant"


def test_hoist_preserves_the_identity_of_every_assistant_message() -> None:
    """§6.1's last line, pinned: the mapping's whole mechanism depends on it.

    ``_resolve_after_hoist`` re-keys by ``id()``. If a later refactor starts
    copying messages instead of appending them, every detail would silently
    vanish — this is the test that fails first.
    """

    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 64,
            "messages": [
                _user("hi"),
                {"role": "assistant", "content": [_detail("D1"), _tool_use("t1")]},
                _tool_result("t1", _image()),
                _user("continue"),
                _assistant("a2", detail="D2"),
            ],
        }
    )
    converted = AnthropicToOpenAIConverter.convert_messages(request.messages)
    hoisted = hoist_tool_result_images(converted)

    converted_ids = {id(m) for m in converted if m.get("role") == "assistant"}
    hoisted_ids = {id(m) for m in hoisted if m.get("role") == "assistant"}
    assert converted_ids == hoisted_ids
    assert len(hoisted) == len(converted) + 1


# --------------------------------------------------------------------------- #
# §6.3 — the regression guard: a one-assistant-turn history is byte-identical
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_one_assistant_turn_history_is_byte_identical_to_before(
    provider_id: str,
) -> None:
    """§6.3. The shape ``tests/providers/test_open_router.py`` pins, spelled out.

    One assistant turn, one detail: the old walk attached it to the first
    assistant message, which is also the only one, so the body cannot move.
    This asserts the **whole body**, not the mapping, because "unchanged" is a
    claim about the bytes.
    """

    body = _build(provider_id, [_user("hi"), _assistant("a1", detail="D1")])

    assert body["messages"] == [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "a1",
            "reasoning_details": [
                {"type": "reasoning.encrypted", "data": "D1"},
            ],
        },
    ]


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_history_with_no_detail_carries_no_field(provider_id: str) -> None:
    """§3.2 cases 1 and 3. Nothing to replay means no key anywhere."""

    for history in (
        [_user("hi")],
        [_user("hi"), _assistant("a1")],
    ):
        body = _build(provider_id, copy.deepcopy(history))
        assert _mapping(body) == {}
        assert "reasoning_details" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# §6.2 M1 — the mutation the dict key exists to prevent
# --------------------------------------------------------------------------- #


def test_keying_by_ordinal_would_reintroduce_the_defect() -> None:
    """§6.2 M1, restated per REVIEW §7 item (4).

    The corrected mutation is **key by ordinal position among the
    detail-bearing turns** instead of by source message index. This reproduces
    it on the real history and asserts the answer is the *wrong* one, so the
    test states what the key buys: with ordinals, ``{0: D1, 1: D3}`` maps onto
    the first two assistant messages and D3 lands on turn 2.
    """

    history = HARD_CASES["plain three turns, middle has none"][0]
    details = [{"data": "D1"}, {"data": "D3"}]

    # The defect, reproduced: ordinals instead of source indexes.
    ordinal_keyed = {ordinal: [d["data"]] for ordinal, d in enumerate(details)}
    body = _build("cline", copy.deepcopy(history))
    assistant_indexes = [i for i, role in _roles(body) if role == "assistant"]

    wrong = {
        assistant_indexes[ordinal]: payload
        for ordinal, payload in ordinal_keyed.items()
    }
    assert wrong == {1: ["D1"], 3: ["D3"]}
    # And the real answer is not that.
    assert _mapping(body) == {1: ["D1"], 5: ["D3"]}


# --------------------------------------------------------------------------- #
# §6.1 case 8 — both copies agree, and the extractor is the same answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", sorted(HARD_CASES))
def test_both_copies_produce_the_same_messages(case: str) -> None:
    """§6.2 M6. A fix to one copy cannot pass.

    Compared on ``messages``: the profiles legitimately differ in ``extra_body``
    and ``max_tokens_field``, which is not the replay.
    """

    history, _expected = HARD_CASES[case]
    messages = {
        provider_id: _build(provider_id, copy.deepcopy(history))["messages"]
        for provider_id in REPLAY_PROFILES
    }
    reference = messages["cline"]
    for provider_id, produced in messages.items():
        assert produced == reference, provider_id


def test_the_two_extractors_return_the_same_mapping() -> None:
    """The two copies of the extractor, called directly on the same history.

    ``assistant_reasoning_details`` lives twice, in two files with no shared
    owner (Q6: both, or neither). This asserts the answer, not the source text,
    so a fix to one file that the other does not get fails here.
    """

    from my_claude_code.providers.openai_chat.reasoning_details import (
        assistant_reasoning_details as chat_copy,
    )

    history = HARD_CASES["plain three turns, middle has none"][0]
    request = MessagesRequest.model_validate(
        {"model": "m", "max_tokens": 64, "messages": history}
    )

    chat = chat_copy(request.messages)
    gateway = gateway_reasoning_details(request.messages)
    assert chat == gateway
    # Keyed by the SOURCE index, which is what the builder consumes.
    assert chat == {
        1: [{"type": "reasoning.encrypted", "data": "D1"}],
        5: [{"type": "reasoning.encrypted", "data": "D3"}],
    }


# --------------------------------------------------------------------------- #
# §6.1 case 9 / §6.2 M7 — the carrier skip still works, unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_carrier_is_still_skipped(provider_id: str) -> None:
    """§6.2 M7. The carrier skip stays inside ``_redacted_reasoning_details``.

    A history whose only detail is a carrier attaches nothing: the envelope is
    host-bound and must not reach a Chat upstream. Moving the skip out of the
    extractor turns this red.
    """

    carrier = "mcc:reasoning:v1:not-ours"
    history = [
        _user("hi"),
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": carrier},
                {"type": "text", "text": "a1"},
            ],
        },
    ]
    body = _build(provider_id, history)

    assert _mapping(body) == {}
    assert carrier not in json.dumps(body)


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_a_json_shaped_detail_is_unwrapped_as_today(provider_id: str) -> None:
    """§6.1 case 10. The JSON branch is untouched by this PR."""

    history = [
        _user("hi"),
        {
            "role": "assistant",
            "content": [
                {
                    "type": "redacted_thinking",
                    "data": json.dumps(
                        [
                            {"type": "reasoning.text", "text": "one"},
                            {"type": "reasoning.encrypted", "data": "two"},
                        ]
                    ),
                },
                {"type": "text", "text": "a1"},
            ],
        },
    ]
    body = _build(provider_id, history)

    assert body["messages"][1]["reasoning_details"] == [
        {"type": "reasoning.text", "text": "one"},
        {"type": "reasoning.encrypted", "data": "two"},
    ]


# --------------------------------------------------------------------------- #
# §6.1 case 11 — purity: the client's transcript is never mutated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
@pytest.mark.parametrize("case", sorted(HARD_CASES))
def test_the_request_history_is_not_mutated(provider_id: str, case: str) -> None:
    """§6.1 case 11. The replay is written into the outbound copy, never the input.

    Both halves are checked, because they fail for different reasons: the
    caller's own dict (what a test hands in) and the ``MessagesRequest`` the
    builder actually reads (what production hands in). A mutation that appends
    to the request model's blocks shows up in the second and not the first.
    """

    history, _expected = HARD_CASES[case]
    before_dict = json.dumps(history, sort_keys=True)

    payload = {"model": "m", "max_tokens": 64, "messages": history}
    request = MessagesRequest.model_validate(payload)
    before_request = json.dumps(request.model_dump(), sort_keys=True, default=str)

    profile = _profile(provider_id)
    build_openai_chat_request_body(
        request,
        reasoning=ReasoningPolicy.off(),
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id=provider_id,
    )

    assert json.dumps(history, sort_keys=True) == before_dict
    assert (
        json.dumps(request.model_dump(), sort_keys=True, default=str) == before_request
    )


# --------------------------------------------------------------------------- #
# §3.2 — the equality cost, stated as a test
# --------------------------------------------------------------------------- #


#: The ten histories of the spec's §3.2 table, with the mapping **before** this
#: release and the mapping after. Case 7 needs two "before" values: the chat
#: copy already skipped the synthetic boundary, so it was already right there,
#: while the gateway copy attached to the boundary. That asymmetry is the second
#: defect this PR fixes, so the table states it rather than averaging it away.
EQUALITY_TABLE: dict[
    str,
    tuple[
        list[dict[str, Any]],
        dict[int, list[str]],
        dict[int, list[str]],
        dict[int, list[str]],
    ],
] = {
    "1 no assistant turn": ([_user("hi")], {}, {}, {}),
    "2 one assistant turn, with details": (
        [_user("hi"), _assistant("a1", detail="D1")],
        {1: ["D1"]},
        {1: ["D1"]},
        {1: ["D1"]},
    ),
    "3 one assistant turn, no details": (
        [_user("hi"), _assistant("a1")],
        {},
        {},
        {},
    ),
    "4 two assistant turns, both with details": (
        [
            _user("hi"),
            _assistant("a1", detail="D1"),
            _user("q2"),
            _assistant("a2", detail="D2"),
        ],
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 3: ["D2"]},
    ),
    "5 two assistant turns, second has none": (
        [_user("hi"), _assistant("a1", detail="D1"), _user("q2"), _assistant("a2")],
        {1: ["D1"]},
        {1: ["D1"]},
        {1: ["D1"]},
    ),
    # MOVES — the middle turn's absence no longer shifts the last turn's detail.
    "6 three turns, middle has none": (
        HARD_CASES["plain three turns, middle has none"][0],
        {1: ["D1"], 3: ["D3"]},
        {1: ["D1"], 3: ["D3"]},
        {1: ["D1"], 5: ["D3"]},
    ),
    # MOVES on the GATEWAY copy only.
    "7 tool round, both turns carry details": (
        HARD_CASES["one tool round"][0],
        {1: ["D1"], 5: ["D2"]},
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 5: ["D2"]},
    ),
    # MOVES — the deferred post-tool message no longer takes the next turn's
    # detail.
    "8 split turn (text after tool_use)": (
        HARD_CASES["assistant split by a tool_use"][0],
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 5: ["D2"]},
    ),
    "9 two tool rounds, both carry details": (
        HARD_CASES["two tool rounds back to back"][0],
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 3: ["D2"]},
        {1: ["D1"], 3: ["D2"]},
    ),
    "10 consecutive user turns": (
        HARD_CASES["consecutive user turns"][0],
        {1: ["D1"]},
        {1: ["D1"]},
        {1: ["D1"]},
    ),
}


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
@pytest.mark.parametrize("case", sorted(EQUALITY_TABLE))
def test_the_after_mapping_is_the_correct_one(provider_id: str, case: str) -> None:
    """§3.2. Every case's AFTER value, on the real builder, on both copies."""

    history, _chat_before, _gateway_before, after = EQUALITY_TABLE[case]
    body = _build(provider_id, copy.deepcopy(history))
    assert _mapping(body) == after


def test_seven_of_ten_cases_are_unchanged_and_three_move() -> None:
    """§3.2's count, asserted rather than asserted-in-prose.

    Measured on the **gateway** copy, which is where the spec's 7/3 split is
    stated: the chat copy already skipped the synthetic boundary, so case 7 was
    already correct there and only 2 of its 10 move. A change to either count is
    a change to this PR's stated equality cost, so it has to fail a test rather
    than a reviewer's memory.
    """

    gateway_unchanged = [
        name for name, (_h, _cb, gb, after) in EQUALITY_TABLE.items() if gb == after
    ]
    gateway_moved = [
        name for name, (_h, _cb, gb, after) in EQUALITY_TABLE.items() if gb != after
    ]
    assert len(gateway_unchanged) == 7
    assert sorted(gateway_moved) == [
        "6 three turns, middle has none",
        "7 tool round, both turns carry details",
        "8 split turn (text after tool_use)",
    ]

    # The chat copy's own split, which the spec's §3.2 table also states.
    chat_unchanged = [
        name for name, (_h, cb, _gb, after) in EQUALITY_TABLE.items() if cb == after
    ]
    chat_moved = [
        name for name, (_h, cb, _gb, after) in EQUALITY_TABLE.items() if cb != after
    ]
    assert len(chat_unchanged) == 8
    assert sorted(chat_moved) == [
        "6 three turns, middle has none",
        "8 split turn (text after tool_use)",
    ]


# --------------------------------------------------------------------------- #
# §4.3 — the cache proof: the replay is a pure function of the history
# --------------------------------------------------------------------------- #


def _conversation() -> list[dict[str, Any]]:
    """A conversation where SOME turns carry a detail and some do not."""

    return [
        _user("one"),
        _assistant("answer one", detail="TURN-1"),
        _user("two"),
        _assistant("answer two"),
        _user("three"),
        _assistant("answer three", detail="TURN-3"),
        _user("four"),
        _assistant("answer four"),
        _user("five"),
        _assistant("answer five", detail="TURN-5"),
    ]


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_turn_n_is_a_strict_prefix_of_turn_n_plus_one(provider_id: str) -> None:
    """§4.3 assertion 2. The prompt cache's prefix property, after the fix.

    Every cut is a prefix of the history, so every earlier turn's attachment
    must be identical in the longer body. **This is the assertion that fails on
    the pre-fix code**: the old walk attached by *relative* position among
    detail-bearing turns, so adding a turn could move an earlier turn's detail.
    """

    full = _conversation()
    bodies = [
        _build(provider_id, copy.deepcopy(full[:cut])) for cut in (3, 5, 7, 9, 10)
    ]
    for earlier, later in pairwise(bodies):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]


@pytest.mark.parametrize("provider_id", REPLAY_PROFILES)
def test_the_attachment_is_stable_as_the_conversation_grows(
    provider_id: str,
) -> None:
    """§4.3 assertion 1. A turn's details depend only on that turn's blocks."""

    full = _conversation()
    expected = {1: ["TURN-1"], 5: ["TURN-3"], 9: ["TURN-5"]}
    for cut in (3, 5, 7, 9, 10):
        body = _build(provider_id, copy.deepcopy(full[:cut]))
        for index, details in _mapping(body).items():
            assert details == expected[index]


# --------------------------------------------------------------------------- #
# D1 — every OTHER Chat provider is byte-identical
# --------------------------------------------------------------------------- #


def test_only_the_replay_profiles_attach_the_field() -> None:
    """The gate, asserted on the profile table rather than on a body.

    ``build_openai_chat_request_body`` is the funnel for **every** Chat
    provider. A profile that declares no extractor attaches nothing, which is
    what keeps ``groq``, ``mistral`` and the rest byte-identical to what they
    sent before this release.
    """

    with_source = sorted(
        provider_id
        for provider_id, profile in OPENAI_CHAT_PROFILES.items()
        if profile.request_policy.reasoning_details_source is not None
    )
    assert with_source == sorted(CHAT_COPY_PROFILES)

    gateway = openrouter_gateway_profile("TEST")
    assert gateway.request_policy.reasoning_details_source is gateway_reasoning_details


def test_a_profile_without_an_extractor_carries_no_field() -> None:
    """The same gate, end to end, on a real non-replay provider."""

    body = _build("groq", [_user("hi"), _assistant("a1", detail="D1")])

    assert _mapping(body) == {}
    assert "reasoning_details" not in json.dumps(body)


def test_the_extractor_is_never_called_for_a_profile_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` means *skip all of it*, not *call something and discard it*.

    The spy replaces the extractor on a **real replay policy** for the duration
    (the policy is a frozen dataclass, so it is replaced rather than assigned),
    so a call from the builder is recorded; ``groq`` declares none, so the list
    stays empty while ``cline`` fills it. Both halves in one test, because
    "never called" is only meaningful beside "called when it should be".
    """

    calls: list[Any] = []

    def _spy(messages: Any) -> dict[int, list[dict[str, Any]]]:
        calls.append(messages)
        return {}

    cline = OPENAI_CHAT_PROFILES["cline"]
    spied = replace(
        cline,
        request_policy=replace(cline.request_policy, reasoning_details_source=_spy),
    )
    monkeypatch.setitem(OPENAI_CHAT_PROFILES, "cline", spied)

    _build("groq", [_user("hi"), _assistant("a1", detail="D1")])
    assert calls == []

    _build("cline", [_user("hi"), _assistant("a1", detail="D1")])
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# The core/provider split — positions in ``core``, the field in the provider
# --------------------------------------------------------------------------- #


def test_core_resolves_positions_and_never_writes_the_dialect_field() -> None:
    """III.5. ``build_base_request_body`` must not know the field's name.

    It resolves *where* the items go; the provider layer spells the key. This
    asserts both halves: the mapping comes back filled, and no message in the
    body it returns carries ``reasoning_details``.
    """

    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 64,
            "messages": HARD_CASES["plain three turns, middle has none"][0],
        }
    )
    mapping: dict[int, list[dict[str, Any]]] = {1: [{"type": "x"}], 5: [{"type": "y"}]}

    body = build_base_request_body(request, reasoning_by_message=mapping)

    assert mapping == {1: [{"type": "x"}], 5: [{"type": "y"}]}
    assert "reasoning_details" not in json.dumps(body)


def test_no_mapping_means_no_change_for_every_other_caller() -> None:
    """§4 rule 2. ``None`` skips all of it, so every other caller is untouched."""

    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 64,
            "messages": HARD_CASES["plain three turns, middle has none"][0],
        }
    )
    without = build_base_request_body(request)
    with_none = build_base_request_body(request, reasoning_by_message=None)

    assert without == with_none


def test_the_mapping_is_cleared_and_refilled_not_replaced() -> None:
    """The in/out contract: the caller's dict object is mutated, never rebound.

    ``request_policy`` holds the same object across the call, so a rebind would
    leave it holding the stale source-keyed mapping.
    """

    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 64,
            "messages": HARD_CASES["plain three turns, middle has none"][0],
        }
    )
    mapping: dict[int, list[dict[str, Any]]] = {1: [{"type": "x"}]}
    identity = id(mapping)

    build_base_request_body(request, reasoning_by_message=mapping)

    # Same object, and now keyed by the CONVERTED index -- source 1 became
    # converted 1 in this history, so the key survives and the value is the
    # caller's own list object, untouched.
    assert id(mapping) == identity
    assert mapping == {1: [{"type": "x"}]}
