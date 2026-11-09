"""A carrier minted for one host is never sent to a host that did not mint it.

PR-3 of the reasoning-continuity arc (8.16.1). PR-2 mints an opaque carrier
(``mcc:reasoning:v1:<base64url(compact JSON)>``) into a ``thinking`` block's
``signature`` or a ``redacted_thinking`` block's ``data``, and the client's
transcript is what carries it to the next turn. Nothing until 8.16.1 refused to
send it anywhere else, and there are three seams a carrier can reach:

* **A** — ``drop_foreign_thinking`` (``providers/anthropic_messages/request.py``),
  the only history trim of the Messages body builder. Serves ``anthropic``,
  ``anthropic_oauth``, ``commandcode`` and every ``MessagesTransport``,
  including OpenCode Go's ``/messages`` door. Before 8.16.1 a base64 envelope
  passed **both** existing rules untouched and went straight to the host.
* **B** — PR-2's replay pass (``providers/openai_responses/conversion.py``).
  ``accepts()`` already refuses a foreign origin there. **This PR adds no rule
  at that seam**; the cases below re-assert it so a regression is visible.
* **C** — ``apply_reasoning_details_replay`` (``providers/openai_chat/
  reasoning_details.py`` and ``providers/openrouter_gateway.py``). This one was
  a **live defect**, independent of PR-2: ``_redacted_reasoning_details`` turned
  any non-JSON ``redacted_thinking.data`` into
  ``{"type": "reasoning.encrypted", "data": …}``, while
  ``core/anthropic/conversion.py`` deliberately dropped that block. The
  conversion's guard was bypassed by a second pass that never learned about it.
  ``nous_portal`` — the largest provider in the request log — is in that path.

**The constraint that must not be violated.** The guard drops the **carrier
only**. Portable plaintext thinking still crosses models and providers exactly
as decision 9 authorises; the predicate is the prefix, not "any thinking
block". ``test_portable_plaintext_thinking_still_crosses_every_door`` is the
control that fails if a future change starts dropping readable reasoning.

**The trap.** The carrier test must run **before** the ``signs_thinking``
short-circuit, or OpenCode Go's ``/messages`` door forwards a carrier in
``thinking.signature`` while every Anthropic-door test passes. Mutation M2
exists for exactly this, and case 5 is the test it turns red.

Every assertion below spells out the whole list, never a "contains" check: a
``"carrier" in body`` test passes for a block in the wrong place.
"""

import base64
import copy
import json
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import (
    CARRIER_MAX_CHARS,
    CARRIER_PREFIX,
    ReplayOrigin,
    connection_id,
    encode_carrier,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import strip_request_content
from my_claude_code.providers.anthropic_messages.request import (
    FOREIGN_THINKING_DROPPED,
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.provider import _OpenAIChatStreamRunner
from my_claude_code.providers.openai_chat.reasoning_details import (
    carrier_foreign_note_for_history,
)
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_MALFORMED,
    build_responses_request_body,
)
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile
from tests.providers.support import passthrough_rate_limiter

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

#: The host a carrier in these tests was minted by.
ORIGIN = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="gpt-6.1-sol",
)

#: The opaque payload a fake upstream returns. Shaped like base64 on purpose:
#: the real ``encrypted_content`` is an opaque blob, never JSON.
E1 = "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"

#: A real Anthropic signature: base64 of a protobuf, no ``:`` in the alphabet.
REAL_SIGNATURE = "EqQBCkYIBxgCKkA1mZ0nQ2FycmllclRlc3Q"

#: Anthropic's own ``redacted_thinking`` ciphertext shape.
REAL_CIPHERTEXT = (
    "EmwKAhgBEgy3va3pzix/LafPsn4aDFIT2Xlxh0L5L8rLVyIwxtE3rAFBa8cr3qpPkNRj2YfW"
    "XGmKDxH4mPnZ5sQ7vB5URjTY+0RpvTnEkAFBp6Pm=="
)

#: The literal ``CARRIER_DATA`` fixture the merged PR-1 test uses. It is **not**
#: an MCC envelope (the scratch emitter that produced it used its own marker),
#: which is exactly why it is the "a bare base64 string that is not ours" case.
NOT_OURS = "mcc-carrier-v1:gAAAAABpQ2FycmllclRlc3RPcGFxdWVQYXlsb2Fk"


def _reasoning_item(item_id: str = "rs_1", encrypted: str = E1) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "content": [],
        "encrypted_content": encrypted,
    }


def _carrier(origin: ReplayOrigin = ORIGIN, item_id: str = "rs_1") -> str:
    carrier = encode_carrier(origin, _reasoning_item(item_id))
    assert carrier is not None
    return carrier


CARRIER = _carrier()


def _request(messages: list[dict[str, Any]], *, model: str = "claude-sonnet-5-5"):
    return MessagesRequest.model_validate(
        {"model": model, "max_tokens": 4096, "messages": copy.deepcopy(messages)}
    )


def _thinking(text: str, signature: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def _redacted(data: str) -> dict[str, Any]:
    return {"type": "redacted_thinking", "data": data}


def _carrier_in_signature() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", CARRIER),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]


def _carrier_in_data() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [_redacted(CARRIER), {"type": "text", "text": "hi"}],
        },
        {"role": "user", "content": "again"},
    ]


def _anthropic_body(
    messages: list[dict[str, Any]],
    *,
    signs_thinking: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One Messages body and the wire notes it recorded."""

    notes: dict[str, Any] = {}
    body = build_anthropic_messages_body(
        _request(messages),
        reasoning=ReasoningPolicy.off(),
        wire_notes=notes,
        signs_thinking=signs_thinking,
    )
    return body, notes


def _assistant_blocks(body: dict[str, Any], index: int = 1) -> list[dict[str, Any]]:
    return body["messages"][index]["content"]


def _chat_profile(provider_id: str):
    """The real profile for one Chat provider id, gateway or plain."""

    if provider_id in OPENAI_CHAT_PROFILES:
        return OPENAI_CHAT_PROFILES[provider_id]
    return openrouter_gateway_profile(provider_id.upper())


def _chat_body(provider_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    profile = _chat_profile(provider_id)
    return build_openai_chat_request_body(
        _request(messages, model="a-chat-model"),
        reasoning=ReasoningPolicy.off(),
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id=provider_id,
    )


def _chat_details(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``reasoning_details`` entry the body carries, in message order."""

    out: list[dict[str, Any]] = []
    for message in body["messages"]:
        details = message.get("reasoning_details")
        if isinstance(details, list):
            out.extend(details)
    return out


# --------------------------------------------------------------------------- #
# Seam A — the Messages door. Cases 1, 2, 3, 6, 7, 11.
# --------------------------------------------------------------------------- #


def test_a_carrier_in_a_thinking_signature_never_reaches_the_messages_door() -> None:
    """§8 case 1. Removed, counted, and every other byte equals the clean body.

    The comparison is the whole list, not a "contains" check: the carrier body's
    assistant turn must be exactly the clean body's assistant turn with the
    carrier-bearing block taken out.
    """

    body, notes = _anthropic_body(_carrier_in_signature())
    clean, clean_notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    _thinking("a summary", REAL_SIGNATURE),
                    {"type": "text", "text": "hi"},
                ],
            },
            {"role": "user", "content": "again"},
        ]
    )

    assert _assistant_blocks(clean) == [
        _thinking("a summary", REAL_SIGNATURE),
        {"type": "text", "text": "hi"},
    ]
    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    # Every other message is byte-identical.
    assert body["messages"][0] == clean["messages"][0]
    assert body["messages"][2] == clean["messages"][2]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    # The clean body records nothing at all.
    assert clean_notes == {}
    assert CARRIER not in json.dumps(body)


def test_a_carrier_in_a_redacted_thinking_data_never_reaches_the_messages_door() -> (
    None
):
    """§8 case 2. The other carrying block, same verdict."""

    body, notes = _anthropic_body(_carrier_in_data())

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert CARRIER not in json.dumps(body)


def test_a_real_anthropic_signature_is_kept_byte_for_byte() -> None:
    """§8 case 3. The false positive §1.3 is written to bound.

    A signature Anthropic minted is base64 of a protobuf, so byte 3 is never
    ``:``. It is kept in its original position and no carrier note is written.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a thought", REAL_SIGNATURE),
                _redacted(REAL_CIPHERTEXT),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _anthropic_body(history)

    assert _assistant_blocks(body) == history[1]["content"]
    assert notes == {}
    assert HISTORY_CARRIER_FOREIGN not in notes


def test_a_json_shaped_redacted_thinking_is_dropped_as_today_and_not_counted() -> None:
    """§8 case 6. MCC's own OpenRouter wrapper keeps its own rule and its own key.

    The block is absent because it always was; ``foreign_thinking_dropped``
    carries it exactly as before, and no carrier note appears -- the two
    concepts stay on two lines.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _redacted(json.dumps({"type": "reasoning.encrypted", "data": E1})),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _anthropic_body(history)

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert FOREIGN_THINKING_DROPPED in notes
    assert notes[FOREIGN_THINKING_DROPPED].startswith("dropped 1 thinking block")
    assert HISTORY_CARRIER_FOREIGN not in notes


def test_a_carrier_is_never_folded_into_the_foreign_thinking_sentence() -> None:
    """Two concepts, two lines -- and the char count stays a char count.

    ``foreign_thinking_dropped`` counts thinking another model wrote and reports
    its length. A carrier is an opaque envelope: its length is not a character
    count of anything a model wrote, and folding it in would both mislabel it
    and inflate the number by up to 32 KiB. This is the §7.4 control "the
    carrier count is written into ``foreign_thinking_dropped``'s sentence".
    """

    body, notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    _thinking("another model's thought", ""),
                    _redacted(CARRIER),
                    {"type": "text", "text": "hi"},
                ],
            },
            {"role": "user", "content": "again"},
        ]
    )

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert notes[FOREIGN_THINKING_DROPPED].startswith("dropped 1 thinking block")
    assert notes[FOREIGN_THINKING_DROPPED].count("1 thinking block") == 1
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )


def test_a_bare_base64_redacted_thinking_that_is_not_ours_is_kept() -> None:
    """§8 case 7. Today's behaviour, unchanged: only the prefix is ours."""

    body, notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [_redacted(NOT_OURS), {"type": "text", "text": "hi"}],
            },
            {"role": "user", "content": "again"},
        ]
    )

    assert _assistant_blocks(body) == [
        _redacted(NOT_OURS),
        {"type": "text", "text": "hi"},
    ]
    assert notes == {}


def test_an_oversize_carrier_is_dropped_and_never_truncated() -> None:
    """§8 case 11. Q6: "too big to be ours" is not a reason to forward it.

    ``CARRIER_MAX_CHARS`` is an encode-side guard. A value over it is still a
    carrier -- it may have been minted by an MCC with a higher guard, or by a
    future version -- and it is dropped, never truncated and never sent.
    """

    oversize = CARRIER_PREFIX + "A" * CARRIER_MAX_CHARS
    assert len(oversize) > CARRIER_MAX_CHARS

    body, notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [_redacted(oversize), {"type": "text", "text": "hi"}],
            },
            {"role": "user", "content": "again"},
        ]
    )

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert oversize not in json.dumps(body)


# --------------------------------------------------------------------------- #
# Seam A — the trap. Case 5, and mutation M2.
# --------------------------------------------------------------------------- #


def test_the_opencode_go_messages_door_drops_a_carrier_in_a_signature() -> None:
    """§8 case 5. ``signs_thinking=False`` must not short-circuit the carrier test.

    OpenCode Go's ``/messages`` door declares that it mints no signature
    (``providers/openai_chat/profiles.py``, ``messages_signs_thinking=False``),
    and before 8.16.1 ``_is_foreign_thinking`` returned ``False`` for every
    ``thinking`` block there **before looking at the signature at all**. A
    carrier test placed after that short-circuit forwards the envelope to a host
    that did not mint it while every Anthropic-door test above still passes.

    ``signs_thinking`` is a statement about signatures. A carrier is not a
    signature.
    """

    body, notes = _anthropic_body(_carrier_in_signature(), signs_thinking=False)

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert CARRIER not in json.dumps(body)


def test_the_opencode_go_messages_door_still_keeps_its_own_unsigned_thinking() -> None:
    """The 8.12.2 rule is untouched: an unsigned block is that host's own.

    Without this, the test above could be satisfied by dropping every thinking
    block on that door -- which is the regression 8.12.2 exists to prevent.
    """

    body, notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "its own"},
                    {"type": "text", "text": "hi"},
                ],
            },
            {"role": "user", "content": "again"},
        ],
        signs_thinking=False,
    )

    assert _assistant_blocks(body) == [
        {"type": "thinking", "thinking": "its own"},
        {"type": "text", "text": "hi"},
    ]
    assert notes == {}


# --------------------------------------------------------------------------- #
# The control that must fail if the guard ever drops readable reasoning.
# --------------------------------------------------------------------------- #


def test_portable_plaintext_thinking_still_crosses_every_door() -> None:
    """Decision 9: the CARRIER is host-bound, the TEXT is portable.

    A guard that also dropped readable reasoning would violate the user's
    cross-model requirement, so this is the control every other case is measured
    against:

    * the Messages door keeps a signed ``thinking`` block with its text, and
      keeps a bare base64 ``redacted_thinking`` that is not ours;
    * the Chat door still replays that text as ``reasoning_content``;
    * a carrier in a *sibling* block never takes the readable block with it.
    """

    plaintext = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("the plan is to read the test first", REAL_SIGNATURE),
                _redacted(NOT_OURS),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _anthropic_body(plaintext)
    assert _assistant_blocks(body) == plaintext[1]["content"]
    assert notes == {}

    # The same history with a carrier *beside* the readable block: the carrier
    # goes, the readable block stays, in its original position.
    mixed = [
        plaintext[0],
        {
            "role": "assistant",
            "content": [
                _thinking("the plan is to read the test first", REAL_SIGNATURE),
                _redacted(CARRIER),
                _redacted(NOT_OURS),
                {"type": "text", "text": "hi"},
            ],
        },
        plaintext[2],
    ]
    body, notes = _anthropic_body(mixed)
    assert _assistant_blocks(body) == plaintext[1]["content"]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )

    # The Chat door replays the readable text across models and providers.
    chat = _chat_body("opencode", plaintext)
    assistant = [m for m in chat["messages"] if m.get("role") == "assistant"]
    assert assistant[0]["reasoning_content"] == "the plan is to read the test first"


# --------------------------------------------------------------------------- #
# Seam C — the Chat door. Cases 8 and 9, parametrised over BOTH copies.
# --------------------------------------------------------------------------- #

#: ``cline`` and ``zenmux`` declare the ``openai_chat`` copy; the three
#: OpenRouter-dialect gateways declare the ``openrouter_gateway`` copy. The
#: parametrisation exists so a fix to one copy cannot pass (Q6, mutation M4).
CHAT_CARRIER_PROFILES = ["cline", "zenmux", "open_router", "kilo", "nous_portal"]


@pytest.mark.parametrize("provider_id", CHAT_CARRIER_PROFILES)
def test_a_carrier_never_reaches_a_chat_upstream(provider_id: str) -> None:
    """§8 case 8. The live defect: the second pass put the carrier back.

    ``convert_messages`` already drops every ``redacted_thinking`` block, so the
    converted Chat body was safe. ``apply_reasoning_details_replay`` then read
    ``request.messages`` directly -- the **client's** blocks, not the converted
    messages -- and turned any non-JSON ``data`` into
    ``{"type": "reasoning.encrypted", "data": …}``. A base64 ``mcc:`` envelope
    therefore reached the host verbatim.
    """

    body = _chat_body(provider_id, _carrier_in_data())

    assert _chat_details(body) == []
    assert CARRIER not in json.dumps(body)
    # The answer text still travels, so the assertion above is about the
    # carrier and not about an empty body.
    assert any(m.get("role") == "assistant" for m in body["messages"])


@pytest.mark.parametrize("provider_id", CHAT_CARRIER_PROFILES)
def test_the_chat_door_counts_what_it_dropped(provider_id: str) -> None:
    """The count is a pure function of the request, and it is exactly 1."""

    request = _request(_carrier_in_data(), model="a-chat-model")
    assert carrier_foreign_note_for_history(request.messages) == {
        HISTORY_CARRIER_FOREIGN: "dropped 1 reasoning carrier minted by another host"
    }
    # And nothing at all for a history that carries none.
    clean = _request(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [_redacted(REAL_CIPHERTEXT), {"type": "text", "text": "hi"}],
            },
        ],
        model="a-chat-model",
    )
    assert carrier_foreign_note_for_history(clean.messages) == {}


@pytest.mark.parametrize("provider_id", CHAT_CARRIER_PROFILES)
def test_the_chat_door_does_not_count_a_carrier_it_could_never_send(
    provider_id: str,
) -> None:
    """A signature carrier has no path to the wire here, so it is not counted.

    The Messages door sends a ``thinking`` block whole and therefore counts a
    carrier in its ``signature``. This door never sends a signature at all --
    ``_convert_assistant_message`` reads the block's *text* and nothing else --
    so counting one would make ``history_carrier_foreign`` claim a drop that
    never happened. The line and the body must agree, which is the rule
    ``empty_system_messages_note`` states and this note inherits.
    """

    request = _request(_carrier_in_signature(), model="a-chat-model")
    assert carrier_foreign_note_for_history(request.messages) == {}
    # And the body carries neither the envelope nor a reasoning detail.
    body = _chat_body(provider_id, _carrier_in_signature())
    assert CARRIER not in json.dumps(body)
    assert _chat_details(body) == []


def test_the_chat_runner_records_the_carrier_note_in_its_wire_notes() -> None:
    """The line reaches ``params.wire``, not just the helper's return value.

    ``_OpenAIChatStreamRunner`` builds ``_wire_notes`` once, from the request,
    and records it on every send of that request -- the first, each
    create-level retry and each recovery continuation. This asserts the carrier
    note is in that mapping, which is the difference between "the function
    exists" and "the operator sees it".
    """

    profile = _chat_profile("cline")
    provider = create_openai_chat_provider(
        "cline",
        ProviderConfig(api_key="sk-test", base_url="https://api.cline.bot"),
        passthrough_rate_limiter(),
        profile=profile,
    )
    runner = _OpenAIChatStreamRunner(
        provider,
        request=_request(_carrier_in_data(), model="a-chat-model"),
        input_tokens=0,
        request_id=None,
        reasoning=ReasoningPolicy.off(),
    )

    assert runner._wire_notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )

    clean = _OpenAIChatStreamRunner(
        provider,
        request=_request([{"role": "user", "content": "hello"}], model="a-chat-model"),
        input_tokens=0,
        request_id=None,
        reasoning=ReasoningPolicy.off(),
    )
    assert clean._wire_notes == {}


@pytest.mark.parametrize("provider_id", ["cline", "zenmux", "open_router"])
def test_a_json_shaped_redacted_thinking_is_still_unwrapped(provider_id: str) -> None:
    """§8 case 9. The JSON branch is the round trip this function exists for.

    A carrier starts ``mcc:`` and a JSON container starts ``{`` or ``[``, so the
    prefix test cannot collide with it.
    """

    detail = {"type": "reasoning.encrypted", "data": "opaque"}
    body = _chat_body(
        provider_id,
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    _redacted(json.dumps(detail)),
                    {"type": "text", "text": "hi"},
                ],
            },
            {"role": "user", "content": "again"},
        ],
    )

    assert _chat_details(body) == [detail]


def test_the_two_reasoning_details_copies_agree_on_the_carrier() -> None:
    """Q6: both copies, or neither. The same input, the same verdict.

    The two functions are byte-identical today and live in two files with no
    shared owner, which is why a fix in one and not the other would pass every
    test that only exercises one profile. This asserts the verdict directly on
    both, so the parametrisation above cannot be satisfied by a profile that
    happens to route through the other copy.
    """

    from my_claude_code.providers.openai_chat.reasoning_details import (
        _redacted_reasoning_details as chat_copy,
    )
    from my_claude_code.providers.openrouter_gateway import (
        _redacted_reasoning_details as gateway_copy,
    )

    blocks = [_redacted(CARRIER), _redacted(REAL_CIPHERTEXT)]
    assert chat_copy(blocks) == gateway_copy(blocks)
    assert chat_copy(blocks) == [
        {"type": "reasoning.encrypted", "data": REAL_CIPHERTEXT}
    ]


# --------------------------------------------------------------------------- #
# Seam B — PR-2's replay pass. Cases 4 and 10: verify, do not duplicate.
# --------------------------------------------------------------------------- #


def _responses_body(
    messages: list[dict[str, Any]], *, origin: ReplayOrigin | None = ORIGIN
) -> tuple[dict[str, Any], dict[str, str]]:
    notes: dict[str, str] = {}
    body = build_responses_request_body(
        _request(messages, model="gpt-6.1-sol"),
        reasoning=ReasoningPolicy.off(),
        wire_notes=notes,
        origin=origin,
    )
    return body, notes


def test_a_carrier_replays_to_its_own_origin() -> None:
    """§8 case 10. The control that proves the guard is not "always drop"."""

    body, notes = _responses_body(_carrier_in_data())
    kinds = [item.get("type") for item in body["input"]]

    assert kinds == ["message", "reasoning", "message", "message"]
    assert body["input"][1]["id"] == "rs_1"
    assert body["input"][1]["encrypted_content"] == E1
    assert HISTORY_CARRIER_FOREIGN not in notes


@pytest.mark.parametrize(
    "field, value",
    [
        ("provider", "opencode"),
        ("protocol", "messages"),
        ("endpoint", "https://opencode.ai/zen/v1"),
        ("connection", connection_id("another-account")),
        ("model", "gpt-6-luna"),
    ],
)
def test_a_carrier_from_another_origin_is_never_sent(field: str, value: str) -> None:
    """§8 case 4. PR-2 §8.2's five-mismatch parametrisation, re-asserted.

    This PR adds **no** rule at this seam: a second origin check here would be a
    second rule, and the two would drift (IV.18). The case is here so a
    regression in ``accepts()`` is visible from this file too.
    """

    destination = ReplayOrigin(
        **{**ORIGIN.to_dict(), field: value},
    )
    body, notes = _responses_body(_carrier_in_data(), origin=destination)

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "message",
        "message",
    ]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert CARRIER not in json.dumps(body)


def test_an_oversize_carrier_is_dropped_at_the_responses_door_too() -> None:
    """§10 item 8: ``decode_carrier`` enforces the guard at decode, and it DROPS.

    It returns ``None`` for an over-size payload, so the block is counted
    malformed and never emitted -- not skipped, not forwarded.
    """

    oversize = CARRIER_PREFIX + "A" * CARRIER_MAX_CHARS
    body, notes = _responses_body(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [_redacted(oversize)]},
            {"role": "user", "content": "again"},
        ]
    )

    kinds = [item.get("type") for item in body["input"]]
    assert "reasoning" not in kinds
    assert notes[HISTORY_CARRIER_MALFORMED] == (
        "dropped 1 malformed reasoning carrier from the client's history"
    )
    assert oversize not in json.dumps(body)


# --------------------------------------------------------------------------- #
# §8.2 — malformed carriers: drop, count, never raise.
# --------------------------------------------------------------------------- #


def _envelope(payload: bytes) -> str:
    return CARRIER_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip(
        "="
    )


def _origin_json(**overrides: Any) -> dict[str, Any]:
    return {**ORIGIN.to_dict(), **overrides}


MALFORMED_CARRIERS: dict[str, str] = {
    "empty_payload": CARRIER_PREFIX,
    "invalid_base64": CARRIER_PREFIX + "!!!!",
    "valid_base64_of_non_json": _envelope(b"this is not json"),
    "json_list": _envelope(b'["a","b"]'),
    "missing_origin": _envelope(
        json.dumps({"native": {"type": "reasoning"}}).encode("utf-8")
    ),
    "short_origin": _envelope(
        json.dumps(
            {
                "origin": {
                    "provider": "chatgpt_oauth",
                    "protocol": "responses",
                    "endpoint": "https://chatgpt.com/backend-api",
                    "connection": "abc",
                },
                "native": {"type": "reasoning"},
            }
        ).encode("utf-8")
    ),
    "native_not_an_object": _envelope(
        json.dumps({"origin": _origin_json(), "native": "x"}).encode("utf-8")
    ),
    "native_is_not_a_reasoning_item": _envelope(
        json.dumps({"origin": _origin_json(), "native": {"type": "message"}}).encode(
            "utf-8"
        )
    ),
    "invalid_utf8": _envelope(b"\xff\xfe\x00\x01"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_CARRIERS))
def test_a_malformed_carrier_is_dropped_and_counted_never_raised(name: str) -> None:
    """§8.2. Nine shapes, one verdict: absent, counted once, no exception.

    Q1 is why the prefix alone decides: requiring a successful decode would
    treat a **corrupted** MCC blob as "not ours" and send it to Anthropic, which
    is the exact failure this PR exists to prevent.
    """

    value = MALFORMED_CARRIERS[name]
    body, notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [_redacted(value), {"type": "text", "text": "hi"}],
            },
            {"role": "user", "content": "again"},
        ]
    )

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert value not in json.dumps(body)


@pytest.mark.parametrize("name", sorted(MALFORMED_CARRIERS))
def test_a_malformed_carrier_never_reaches_a_chat_upstream(name: str) -> None:
    """The same nine shapes on the Chat door: absent, counted once, no exception."""

    value = MALFORMED_CARRIERS[name]
    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [_redacted(value), {"type": "text", "text": "hi"}],
        },
        {"role": "user", "content": "again"},
    ]
    body = _chat_body("cline", history)

    assert _chat_details(body) == []
    assert value not in json.dumps(body)
    assert carrier_foreign_note_for_history(
        _request(history, model="a-chat-model").messages
    ) == {HISTORY_CARRIER_FOREIGN: "dropped 1 reasoning carrier minted by another host"}


# --------------------------------------------------------------------------- #
# §6 rows 7 and 8 — the wire log, and ``reasoning_was_emitted``.
# --------------------------------------------------------------------------- #


def test_the_stored_wire_body_carries_no_substring_of_a_carrier() -> None:
    """§6 row 7. ``_block_shape`` keeps ``type`` and nothing else for these blocks.

    The ``chars`` loop reads ``text``/``thinking``/``content``/``input``/
    ``arguments`` and never ``data``; a ``thinking`` block's ``signature`` is
    never read at all. So no carrier content can reach the database -- proven
    here on the body that actually carries one.
    """

    body, _notes = _anthropic_body(_carrier_in_signature())
    stored = strip_request_content(body)
    serialized = json.dumps(stored)

    assert CARRIER not in serialized
    assert CARRIER_PREFIX not in serialized
    # And the shape is what it always was.
    assert stored["messages"][1]["content"] == [{"type": "text", "chars": 2}]


def test_the_carrier_note_is_not_a_reasoning_key() -> None:
    """§6 row 8. ``is_reasoning_key`` matches only the reasoning-instruction names."""

    from my_claude_code.core.wire_capture import is_reasoning_key

    assert is_reasoning_key(HISTORY_CARRIER_FOREIGN) is False
