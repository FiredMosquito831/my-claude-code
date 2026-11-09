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

**8.16.3 narrows what is dropped, never whether a foreign carrier is sent.**
A carrier and the readable summary beside it are different things with
different rules (decision 9): the envelope is host-bound, the text is portable.
So the one predicate now has three answers —

* **(a)** a carrier in a ``thinking`` block's ``signature`` **with** readable
  text is **stripped** (the ``signature`` key removed from a new dict, the block
  and its text kept) on a host that declares ``signs_thinking=False``, and still
  **dropped** on a host that signs;
* **(b)** the same carrier with **no** readable text is **dropped**;
* **(c)** a carrier in a ``redacted_thinking`` block's ``data`` is **dropped**.

The recovery is real on exactly one door today — OpenCode Go's ``/messages``
door — and the Anthropic door recovers **nothing**: an unsigned ``thinking``
block is a 400 there, so stripping would only move the drop one rule later.
That is a property of Anthropic's protocol, not a gap this release can close,
and no test below claims otherwise.

**The constraint that must not be violated.** The guard drops the **carrier
only**. Portable plaintext thinking still crosses models and providers exactly
as decision 9 authorises; the predicate is the prefix, not "any thinking
block". ``test_portable_plaintext_thinking_still_crosses_every_door`` is the
control that fails if a future change starts dropping readable reasoning.

**The trap.** The carrier test must run **before** the ``signs_thinking``
short-circuit, or OpenCode Go's ``/messages`` door forwards a carrier in
``thinking.signature`` while every Anthropic-door test passes. Mutation M2
exists for exactly this, and case 5 is the test it turns red.

**The cache rule.** A strip is a property of the **destination** and never of
the history: it is written into the outbound copy for one request, so a hop
through a non-signing host cannot change what a later turn to the signing model
sends. §6.4's fixtures are the proof, and they are the reason the strip is a
new dict rather than a ``pop``.

Every assertion below spells out the whole list, never a "contains" check: a
``"carrier" in body`` test passes for a block in the wrong place.
"""

import base64
import copy
import json
from itertools import pairwise
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
    _has_readable_thinking,
    _thinking_verdict,
    _ThinkingVerdict,
    build_anthropic_messages_body,
    drop_foreign_thinking,
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
    HISTORY_CARRIER_STRIPPED,
    build_responses_request_body,
    carrier_stripped_note,
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


def test_the_opencode_go_messages_door_keeps_the_text_and_strips_the_carrier() -> None:
    """§8 case 5. ``signs_thinking=False`` must not short-circuit the carrier test.

    OpenCode Go's ``/messages`` door declares that it mints no signature
    (``providers/openai_chat/profiles.py``, ``messages_signs_thinking=False``),
    and before 8.16.1 ``_is_foreign_thinking`` returned ``False`` for every
    ``thinking`` block there **before looking at the signature at all**. A
    carrier test placed after that short-circuit forwards the envelope to a host
    that did not mint it while every Anthropic-door test above still passes.

    ``signs_thinking`` is a statement about signatures. A carrier is not a
    signature.

    **8.16.3: the block is kept and only the envelope goes.** This door accepts
    an unsigned ``thinking`` block — that is what its declaration means — so the
    summary beside the carrier survives the hop. The signature key is *absent*,
    not blanked: the host's own unsigned blocks have no ``signature`` key at
    all (``ContentBlockThinking.signature: str | None = None`` plus
    ``model_dump(exclude_none=True)``), so removing it reproduces a shape the
    host has already seen, where ``"signature": ""`` would be one it has not.

    This is the only door the recovery is reachable on today, and it is the
    whole point of the release: ``plaintext → encrypted → plaintext`` now
    regains the text.
    """

    body, notes = _anthropic_body(_carrier_in_signature(), signs_thinking=False)

    assert _assistant_blocks(body) == [
        {"type": "thinking", "thinking": "a summary"},
        {"type": "text", "text": "hi"},
    ]
    assert notes[HISTORY_CARRIER_STRIPPED] == (
        "removed 1 reasoning carrier from a block whose text was kept "
        "(this host signs nothing)"
    )
    # A strip is not a drop, and the two sentences are opposites.
    assert HISTORY_CARRIER_FOREIGN not in notes
    assert FOREIGN_THINKING_DROPPED not in notes
    assert CARRIER not in json.dumps(body)
    # The text is byte-identical, not merely present.
    assert _assistant_blocks(body)[0]["thinking"] == "a summary"


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
# Seam A — the strip (8.16.3). Cases 5, 7, 8, 11 and the edge cases.
# --------------------------------------------------------------------------- #


def test_the_strip_removes_the_signature_key_and_nothing_else() -> None:
    """§6.2. The only permitted difference, asserted on the whole list.

    With a carrier and readable text on a ``signs_thinking=False`` door, the
    body differs in exactly one way: the assistant turn's ``thinking`` block is
    present with its text byte-identical and the ``signature`` key **absent**.
    Every other key of every other block and message is byte-identical, in the
    same order -- including a ``cache_control`` marker the client put on the
    block itself, which is not MCC's to drop.
    """

    marked = {
        "type": "thinking",
        "thinking": "a summary",
        "signature": CARRIER,
        "cache_control": {"type": "ephemeral"},
    }
    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [marked, {"type": "text", "text": "hi"}],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _anthropic_body(history, signs_thinking=False)

    assert _assistant_blocks(body) == [
        {
            "type": "thinking",
            "thinking": "a summary",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "hi"},
    ]
    assert set(_assistant_blocks(body)[0]) == {"type", "thinking", "cache_control"}
    assert notes[HISTORY_CARRIER_STRIPPED] == (
        "removed 1 reasoning carrier from a block whose text was kept "
        "(this host signs nothing)"
    )
    assert CARRIER not in json.dumps(body)


def test_a_carrier_with_no_readable_text_is_still_dropped_on_a_non_signing_host() -> (
    None
):
    """§8 case 7. Case (b): there is nothing to keep, so the block goes.

    The strip is not "a non-signing host keeps every carrier block". A carrier
    whose ``thinking`` is absent, empty or whitespace-only has no text a model
    could read, and the Messages API refuses a whitespace-only ``thinking``
    block outright — so the block is dropped and counted as a **drop**, not a
    strip.
    """

    for text in ("", "   ", "\n\t "):
        body, notes = _anthropic_body(
            [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": text, "signature": CARRIER},
                        {"type": "text", "text": "hi"},
                    ],
                },
                {"role": "user", "content": "again"},
            ],
            signs_thinking=False,
        )

        assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}], repr(text)
        assert notes[HISTORY_CARRIER_FOREIGN] == (
            "dropped 1 reasoning carrier minted by another host"
        ), repr(text)
        assert HISTORY_CARRIER_STRIPPED not in notes, repr(text)
        assert CARRIER not in json.dumps(body), repr(text)

    # And a block with no ``thinking`` key at all is the same case. It cannot
    # come through ``MessagesRequest`` -- ``ContentBlockThinking.thinking`` is
    # required -- so it is exercised on the predicate itself, which is where the
    # defensive branch lives.
    bare = {"type": "thinking", "signature": CARRIER}
    assert _has_readable_thinking(bare) is False
    assert _thinking_verdict(bare, signs_thinking=False) is _ThinkingVerdict.DROP
    messages, blocks, chars = drop_foreign_thinking(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [bare, {"type": "text", "text": "hi"}]},
        ],
        signs_thinking=False,
    )
    assert messages[1]["content"] == [{"type": "text", "text": "hi"}]
    assert (blocks, chars) == (0, 0)


def test_a_carrier_in_redacted_thinking_data_is_dropped_on_a_non_signing_host() -> None:
    """§8 case 8. Case (c): that block type has no text field at all.

    ``redacted_thinking`` carries ``data`` and nothing else, so there is never
    a summary to keep. The verdict is ``DROP`` on every door, signing or not.
    """

    body, notes = _anthropic_body(_carrier_in_data(), signs_thinking=False)

    assert _assistant_blocks(body) == [{"type": "text", "text": "hi"}]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert HISTORY_CARRIER_STRIPPED not in notes
    assert CARRIER not in json.dumps(body)


def test_a_turn_whose_only_block_is_a_stripped_carrier_is_still_sent() -> None:
    """The edge case a naive strip loses the envelope on.

    ``drop_foreign_thinking`` rebuilds a message only when it has something
    ``foreign`` in it. A turn whose **only** block is a carrier-bearing
    ``thinking`` block with text has nothing foreign — the block is *kept*, minus
    one key — so a strip that did not mark the message as changed would append
    the original message, signature and all, and the envelope would go out.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "only", "signature": CARRIER}],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _anthropic_body(history, signs_thinking=False)

    assert body["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "only"}],
    }
    assert notes[HISTORY_CARRIER_STRIPPED] == (
        "removed 1 reasoning carrier from a block whose text was kept "
        "(this host signs nothing)"
    )
    assert CARRIER not in json.dumps(body)


def test_a_stripped_block_never_reaches_the_foreign_thinking_sentence() -> None:
    """M9. A strip is not a drop, and its character count is not thinking text.

    ``foreign_thinking_dropped`` reports how many characters of another model's
    thinking were left out. A block that is **kept** was not left out, and its
    text was not lost — so it contributes to neither the count nor the
    ``dropped_chars`` sum, and the two lines stay two lines.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "kept", "signature": CARRIER},
                {"type": "redacted_thinking", "data": '{"type":"x"}'},
                {"type": "text", "text": "hi"},
            ],
        },
    ]
    body, notes = _anthropic_body(history, signs_thinking=False)

    # The only block dropped on this host is MCC's own JSON wrapper -- a blank
    # signature is kept there, which is 8.12.2's rule and not this one's.
    assert notes[FOREIGN_THINKING_DROPPED].startswith(
        "dropped 1 thinking block (12 chars)"
    )
    assert notes[HISTORY_CARRIER_STRIPPED] == (
        "removed 1 reasoning carrier from a block whose text was kept "
        "(this host signs nothing)"
    )
    assert _assistant_blocks(body) == [
        {"type": "thinking", "thinking": "kept"},
        {"type": "text", "text": "hi"},
    ]


def test_stripping_twice_is_stripping_once() -> None:
    """§8 case 11. Idempotency, and the second pass reports nothing.

    After a strip the block has no ``signature``, so on a non-signing host rule
    1 returns ``KEEP`` and the block is untouched. The count is not inflated by
    a second pass, which is what a non-idempotent strip would do to every retry
    of the same request.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "a summary", "signature": CARRIER},
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    first_counts: dict[str, int] = {}
    once, blocks, chars = drop_foreign_thinking(
        copy.deepcopy(history), signs_thinking=False, carrier_counts=first_counts
    )
    second_counts: dict[str, int] = {}
    twice, again_blocks, again_chars = drop_foreign_thinking(
        once, signs_thinking=False, carrier_counts=second_counts
    )

    assert first_counts == {HISTORY_CARRIER_STRIPPED: 1}
    assert second_counts == {}
    assert twice == once
    assert twice is once
    assert (blocks, chars) == (0, 0)
    assert (again_blocks, again_chars) == (0, 0)


def test_a_malformed_carrier_with_text_is_stripped_too() -> None:
    """The prefix decides, not a successful decode — on the strip path as well.

    Q1: requiring a decode would treat a **corrupted** MCC blob as "not ours".
    On a non-signing host the block's text is still readable, so a corrupted
    envelope is stripped exactly like a valid one, and no exception reaches the
    client.
    """

    for name, value in sorted(MALFORMED_CARRIERS.items()):
        body, notes = _anthropic_body(
            [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "a summary",
                            "signature": value,
                        },
                        {"type": "text", "text": "hi"},
                    ],
                },
            ],
            signs_thinking=False,
        )

        assert _assistant_blocks(body) == [
            {"type": "thinking", "thinking": "a summary"},
            {"type": "text", "text": "hi"},
        ], name
        assert notes[HISTORY_CARRIER_STRIPPED] == (
            "removed 1 reasoning carrier from a block whose text was kept "
            "(this host signs nothing)"
        ), name
        assert value not in json.dumps(body), name


def test_the_verdict_table_is_exactly_these_three_cases() -> None:
    """The predicate's whole truth table, pinned block shape by block shape.

    This is the committed form of §5's "the two existing rules do not change":
    every non-carrier shape returns exactly what ``_is_foreign_thinking``
    returned in 8.16.2, and the only new answers are the two carrier cases the
    release adds.
    """

    blank = {"type": "thinking", "thinking": "t", "signature": ""}
    absent = {"type": "thinking", "thinking": "t"}
    non_string = {"type": "thinking", "thinking": "t", "signature": 7}
    signed = {"type": "thinking", "thinking": "t", "signature": REAL_SIGNATURE}
    carrier_with_text = {"type": "thinking", "thinking": "t", "signature": CARRIER}
    carrier_no_text = {"type": "thinking", "thinking": "  ", "signature": CARRIER}
    json_object = {"type": "redacted_thinking", "data": '{"type":"x"}'}
    json_array = {"type": "redacted_thinking", "data": "[1]"}
    ciphertext = {"type": "redacted_thinking", "data": REAL_CIPHERTEXT}
    not_ours = {"type": "redacted_thinking", "data": NOT_OURS}
    carrier_data = {"type": "redacted_thinking", "data": CARRIER}
    text = {"type": "text", "text": "hi"}
    tool = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}

    table: list[tuple[Any, bool, _ThinkingVerdict]] = [
        # (block, signs_thinking, verdict)
        # -- not a dict / not a block type this function classifies -----------
        ("a string", True, _ThinkingVerdict.KEEP),
        (None, True, _ThinkingVerdict.KEEP),
        (text, True, _ThinkingVerdict.KEEP),
        (tool, True, _ThinkingVerdict.KEEP),
        # -- existing rule 1: a signed host refuses an unsigned block ---------
        (blank, True, _ThinkingVerdict.DROP),
        (absent, True, _ThinkingVerdict.DROP),
        (non_string, True, _ThinkingVerdict.DROP),
        (signed, True, _ThinkingVerdict.KEEP),
        # -- 8.12.2: a host that signs nothing keeps its own unsigned block --
        (blank, False, _ThinkingVerdict.KEEP),
        (absent, False, _ThinkingVerdict.KEEP),
        (non_string, False, _ThinkingVerdict.KEEP),
        (signed, False, _ThinkingVerdict.KEEP),
        # -- existing rule 2: MCC's own JSON wrapper is not any host's -------
        (json_object, True, _ThinkingVerdict.DROP),
        (json_array, True, _ThinkingVerdict.DROP),
        (ciphertext, True, _ThinkingVerdict.KEEP),
        (not_ours, True, _ThinkingVerdict.KEEP),
        (json_object, False, _ThinkingVerdict.DROP),
        (ciphertext, False, _ThinkingVerdict.KEEP),
        # -- (a) carrier + text: strip where the host signs nothing ----------
        (carrier_with_text, False, _ThinkingVerdict.STRIP_SIGNATURE),
        # -- (a) carrier + text: still a drop where the host signs -----------
        (carrier_with_text, True, _ThinkingVerdict.DROP),
        # -- (b) carrier with no readable text: always a drop ----------------
        (carrier_no_text, False, _ThinkingVerdict.DROP),
        (carrier_no_text, True, _ThinkingVerdict.DROP),
        # -- (c) carrier in redacted_thinking.data: always a drop ------------
        (carrier_data, False, _ThinkingVerdict.DROP),
        (carrier_data, True, _ThinkingVerdict.DROP),
    ]

    for block, signs_thinking, expected in table:
        assert _thinking_verdict(block, signs_thinking=signs_thinking) is expected, (
            block,
            signs_thinking,
        )

    # ``_is_foreign_thinking`` is exactly "not KEEP", and is still what
    # ``_final_tool_turn_lost_its_thinking`` reads.
    for block, signs_thinking, expected in table:
        assert (
            _thinking_verdict(block, signs_thinking=signs_thinking)
            is not _ThinkingVerdict.KEEP
        ) is (expected is not _ThinkingVerdict.KEEP)


def test_readable_thinking_is_text_and_not_whitespace() -> None:
    """The one helper the strip's case (b) rests on, pinned on its own."""

    assert _has_readable_thinking({"thinking": "a summary"}) is True
    assert _has_readable_thinking({"thinking": " a "}) is True
    assert _has_readable_thinking({"thinking": ""}) is False
    assert _has_readable_thinking({"thinking": "   "}) is False
    assert _has_readable_thinking({"thinking": "\n\t"}) is False
    assert _has_readable_thinking({"thinking": None}) is False
    assert _has_readable_thinking({"thinking": 7}) is False
    assert _has_readable_thinking({}) is False


# --------------------------------------------------------------------------- #
# §6.4 — the cache rules. Four fixtures, all mandatory.
# --------------------------------------------------------------------------- #


def test_the_strip_never_touches_the_clients_history() -> None:
    """§6.4.1, mutation M7. The strip is written into the outbound copy only.

    Two halves, and both are load-bearing.

    **Through the builder**, the copy chain is already in place:
    ``_native_message`` builds a fresh dict per message and ``_native_content``
    a fresh dict per block with ``deepcopy`` per field, so no block the client's
    ``MessagesRequest`` owns is reachable from the trim.

    **At the trim's own seam**, ``drop_foreign_thinking`` is documented as *"a
    pure function of the history"*, and that is the contract the cache argument
    rests on. An in-place ``pop`` would be *safe today* because the caller
    deep-copies first — and would make the function's contract depend on that
    copy discipline, which is the latent coupling that breaks two releases
    later. Calling it directly on the caller's own list is what catches that;
    the builder's copy hides it by construction, so the builder half alone
    cannot.
    """

    history = _carrier_in_signature()
    before = json.dumps(history)

    # The trim's own seam: the caller's list and its blocks come back untouched.
    trimmed, blocks, chars = drop_foreign_thinking(history, signs_thinking=False)
    assert json.dumps(history) == before
    assert history[1]["content"][0]["signature"] == CARRIER
    assert trimmed[1]["content"][0] == {"type": "thinking", "thinking": "a summary"}
    assert trimmed[1]["content"][0] is not history[1]["content"][0]
    assert (blocks, chars) == (0, 0)

    # And through the builder, end to end.
    body, _notes = _anthropic_body(history, signs_thinking=False)

    assert json.dumps(history) == before
    assert history[1]["content"][0]["signature"] == CARRIER
    assert "signature" not in body["messages"][1]["content"][0]
    assert body["messages"][1]["content"][0]["thinking"] == "a summary"


def test_the_three_hop_fixture_a_b_a() -> None:
    """§6.4.2, mutation M7. A's turn-3 body is what it would have been anyway.

    The property: *A's turn-3 body is byte-identical to what it would have been
    if turn 2 had never happened.* The hop is B — a Messages host that declares
    ``signs_thinking=False`` — and the strip happens while building **B's**
    body. A then builds from the same history and must be unaffected.

    The last three assertions are what make this a **hop** test rather than a
    purity test: they prove the same history yields two different, correct
    bodies for two different destinations, which is the whole claim — the strip
    is a property of the destination, never of the history.
    """

    text = "B's summary"
    history = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": text, "signature": CARRIER},
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "two"},
    ]
    pristine = copy.deepcopy(history)

    # The control: A's body, built from a pristine copy of the same history.
    control, control_notes = _anthropic_body(pristine, signs_thinking=True)

    # The hop: B's body first (the strip happens here), then A's body from the
    # SAME history object.
    b_body, b_notes = _anthropic_body(history, signs_thinking=False)
    a_body, a_notes = _anthropic_body(history, signs_thinking=True)

    assert json.dumps(a_body) == json.dumps(control)
    assert json.dumps(history) == json.dumps(pristine)
    assert a_notes == control_notes

    # And the two destinations really do see different shapes from one history.
    assert "signature" not in b_body["messages"][1]["content"][0]
    assert b_body["messages"][1]["content"][0]["thinking"] == text
    assert a_body["messages"][1]["content"] == [{"type": "text", "text": "ok"}]
    assert b_notes == {
        HISTORY_CARRIER_STRIPPED: (
            "removed 1 reasoning carrier from a block whose text was kept "
            "(this host signs nothing)"
        )
    }
    assert a_notes == {
        HISTORY_CARRIER_FOREIGN: "dropped 1 reasoning carrier minted by another host"
    }
    assert CARRIER not in json.dumps(b_body)
    assert CARRIER not in json.dumps(a_body)


def _prefix_bodies(
    history: list[dict[str, Any]], *, signs_thinking: bool
) -> list[dict[str, Any]]:
    """The bodies one growing conversation would send, turn by turn."""

    return [
        _anthropic_body(history[:turns], signs_thinking=signs_thinking)[0]
        for turns in range(1, len(history) + 1)
    ]


def _assert_each_turn_is_a_strict_prefix(bodies: list[dict[str, Any]]) -> None:
    for earlier, later in pairwise(bodies):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
        assert {k: v for k, v in later.items() if k != "messages"} == {
            k: v for k, v in earlier.items() if k != "messages"
        }


def test_turn_n_is_a_strict_prefix_of_turn_n_plus_one_without_a_carrier() -> None:
    """§6.4.3, first run. The v8.16.2 property, unchanged.

    A conversation that carries no carrier must keep its prompt-cache prefix
    byte for byte, on every turn, on both kinds of Messages host.
    """

    history: list[dict[str, Any]] = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "a", "signature": REAL_SIGNATURE},
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "two"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "b", "signature": REAL_SIGNATURE},
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "three"},
    ]

    for signs_thinking in (True, False):
        _assert_each_turn_is_a_strict_prefix(
            _prefix_bodies(history, signs_thinking=signs_thinking)
        )


def test_turn_n_is_a_strict_prefix_of_turn_n_plus_one_with_a_carrier() -> None:
    """§6.4.3, second run. The strip appears identically on both turns.

    **Why it holds:** the verdict is a pure function of ``(block,
    signs_thinking)`` and ``signs_thinking`` is fixed per door and model, so the
    same block produces the same stripped shape on every turn to that host. A
    strip can never differ between two turns of one chain — which is the whole
    reason it cannot poison the cache.

    The signing host is run too: there the block is dropped on every turn, so
    its prefix is stable as well. That is the v8.16.2 property, and this release
    does not move it.
    """

    history: list[dict[str, Any]] = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "a", "signature": CARRIER},
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "two"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "b", "signature": CARRIER},
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "three"},
    ]

    for signs_thinking in (True, False):
        bodies = _prefix_bodies(history, signs_thinking=signs_thinking)
        _assert_each_turn_is_a_strict_prefix(bodies)
        # And the carrier never appears in any of them.
        for body in bodies:
            assert CARRIER not in json.dumps(body)

    # The non-signing chain really is the strip: both turns kept their text.
    stripped = _prefix_bodies(history, signs_thinking=False)
    assert stripped[1]["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "a",
    }
    assert stripped[3]["messages"][3]["content"][0] == {
        "type": "thinking",
        "thinking": "b",
    }
    # And the signing chain really is the drop.
    dropped = _prefix_bodies(history, signs_thinking=True)
    assert dropped[1]["messages"][1]["content"] == [{"type": "text", "text": "ok"}]


def test_the_strip_note_is_absent_not_zero_and_is_a_sibling_of_the_drop_note() -> None:
    """§4.1, mutation M8. Two facts, two keys, and neither on a clean request."""

    assert carrier_stripped_note(0) == {}
    assert carrier_stripped_note(1) == {
        HISTORY_CARRIER_STRIPPED: (
            "removed 1 reasoning carrier from a block whose text was kept "
            "(this host signs nothing)"
        )
    }
    assert carrier_stripped_note(2) == {
        HISTORY_CARRIER_STRIPPED: (
            "removed 2 reasoning carriers from blocks whose text was kept "
            "(this host signs nothing)"
        )
    }

    # A history with no carrier records nothing at all, on either door.
    clean, clean_notes = _anthropic_body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "t", "signature": REAL_SIGNATURE}
                ],
            },
        ],
        signs_thinking=False,
    )
    assert clean_notes == {}
    assert HISTORY_CARRIER_STRIPPED not in json.dumps(clean)


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
# Q1 — the Chat door needed no code change. These are the proof, not a fix.
# --------------------------------------------------------------------------- #

#: Every profile with a ``thinking`` block on the wire. ``cline`` declares
#: ``ReasoningReplayMode.DISABLED``, so it replays no text at all — carrier or
#: not — which is a pre-existing dialect property (Q4), not a carrier defect.
CHAT_TEXT_PROFILES = ["zenmux", "opencode", "open_router", "kilo", "nous_portal"]


@pytest.mark.parametrize("provider_id", CHAT_CARRIER_PROFILES)
def test_a_carrier_beside_readable_text_keeps_the_text_on_a_chat_door(
    provider_id: str,
) -> None:
    """§8 case 1. The text crosses; the envelope never does.

    **This is a proof obligation, not a code change.** ``_convert_assistant_message``
    reads ``block.thinking`` and nothing else — a ``thinking`` block's
    ``signature`` is never touched on this path — so the carrier has no route to
    the wire here and the summary beside it always survives. Nothing in
    ``reasoning_details.py`` or ``openrouter_gateway.py`` changed for this case;
    if it ever needs to, this test is what says so.
    """

    body = _chat_body(provider_id, _carrier_in_signature())

    assert CARRIER not in json.dumps(body)
    assert _chat_details(body) == []
    assert (
        carrier_foreign_note_for_history(
            _request(_carrier_in_signature(), model="a-chat-model").messages
        )
        == {}
    )

    if provider_id in CHAT_TEXT_PROFILES:
        field = "reasoning" if provider_id == "zenmux" else "reasoning_content"
        assistant = [m for m in body["messages"] if m.get("role") == "assistant"]
        assert assistant[0][field] == "a summary"


@pytest.mark.parametrize("provider_id", CHAT_CARRIER_PROFILES)
def test_a_carrier_with_no_text_emits_nothing_on_a_chat_door(provider_id: str) -> None:
    """§8 case 2. No reasoning text, no reasoning detail, no envelope.

    A whitespace-only ``thinking`` block still reaches the converter as an
    empty string, and ``_convert_assistant_message`` sets the replay field to
    that empty string — pre-existing behaviour, unchanged here. What must not
    happen is any **text** crossing, and the envelope must not cross at all.
    """

    body = _chat_body(
        provider_id,
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": CARRIER},
                    {"type": "text", "text": "hi"},
                ],
            },
        ],
    )

    assert CARRIER not in json.dumps(body)
    assert _chat_details(body) == []
    for message in body["messages"]:
        assert not message.get("reasoning")
        assert not message.get("reasoning_content")


def test_a_real_anthropic_signature_crosses_every_door_byte_for_byte() -> None:
    """§8 case 3. The false positive §1.3 is written to bound, on every door.

    A signature Anthropic minted is base64 of a protobuf, so byte 3 is never
    ``:`` and the prefix test cannot match it. The block is kept in its original
    position, with its signature, and no carrier note is written anywhere.
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

    for signs_thinking in (True, False):
        body, notes = _anthropic_body(history, signs_thinking=signs_thinking)
        assert _assistant_blocks(body) == history[1]["content"], signs_thinking
        assert notes == {}, signs_thinking

    for provider_id in CHAT_CARRIER_PROFILES:
        body = _chat_body(provider_id, history)
        assert CARRIER not in json.dumps(body)
        # A bare base64 ``redacted_thinking`` that is not ours still crosses as
        # an opaque detail -- that is today's behaviour and this release does
        # not touch it. What must not appear is the carrier.
        assert _chat_details(body) == [
            {"type": "reasoning.encrypted", "data": REAL_CIPHERTEXT}
        ]

    # And the Responses door replays nothing for it either -- the block is not a
    # carrier, so PR-2's pass leaves it to the omission rule it always had.
    responses, responses_notes = _responses_body(history)
    assert [item.get("type") for item in responses["input"]] == [
        "message",
        "message",
        "message",
    ]
    assert HISTORY_CARRIER_FOREIGN not in responses_notes


def test_a_plain_thinking_block_crosses_every_door() -> None:
    """§8 case 4. Decision 9: the plaintext half is portable, and stays so.

    A ``thinking`` block with a real signature and **no** envelope crosses the
    Messages door with its signature, and the Chat door with its text. This is
    the control that fails if the strip ever starts eating readable reasoning.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("the plan is to read the test first", REAL_SIGNATURE),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]

    for signs_thinking in (True, False):
        body, notes = _anthropic_body(history, signs_thinking=signs_thinking)
        assert _assistant_blocks(body) == history[1]["content"], signs_thinking
        assert notes == {}, signs_thinking

    for provider_id in CHAT_TEXT_PROFILES:
        field = "reasoning" if provider_id == "zenmux" else "reasoning_content"
        body = _chat_body(provider_id, history)
        assistant = [m for m in body["messages"] if m.get("role") == "assistant"]
        assert assistant[0][field] == "the plan is to read the test first"


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


def test_a_carrier_beside_readable_text_is_still_refused_at_a_foreign_origin() -> None:
    """§8 case 9. The text does not soften the Responses door's origin match.

    8.16.3 gives a carrier-bearing block a second chance on a Messages host that
    signs nothing. It gives it **none** here: the Responses door replays the
    whole ``native`` item, so the envelope and its origin are the same object,
    and a mismatch on any of the five fields still means nothing is emitted.
    """

    history = [
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
    destination = ReplayOrigin(**{**ORIGIN.to_dict(), "model": "gpt-6-luna"})
    body, notes = _responses_body(history, origin=destination)

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "message",
        "message",
    ]
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert HISTORY_CARRIER_STRIPPED not in notes
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


def test_the_stored_wire_body_carries_no_substring_of_a_stripped_carrier() -> None:
    """§5 row 7, §8 case 12. The strip case, on the body that actually went out.

    ``_block_shape`` keeps ``type`` and nothing else for these blocks: the
    ``chars`` loop reads ``text``/``thinking``/``content``/``input``/
    ``arguments`` and never ``data``, and a ``thinking`` block's ``signature``
    is never read at all. So neither the envelope nor the summary can reach the
    database — and the summary is **kept** here, which is exactly why this case
    needs its own assertion: the block survives the trim, so nothing but the
    recorder stands between it and the log.
    """

    body, _notes = _anthropic_body(_carrier_in_signature(), signs_thinking=False)
    stored = strip_request_content(body)
    serialized = json.dumps(stored)

    assert CARRIER not in serialized
    assert CARRIER_PREFIX not in serialized
    assert "a summary" not in serialized
    # And the shape is what it always was: type and a character count.
    assert stored["messages"][1]["content"] == [
        {"type": "thinking", "chars": 9},
        {"type": "text", "chars": 2},
    ]


def test_the_carrier_notes_are_not_reasoning_keys() -> None:
    """§6 row 8. ``is_reasoning_key`` matches only the reasoning-instruction names.

    Both carrier keys start with ``history``, so neither is read as a reasoning
    *instruction* — which would make a note about the history count as reasoning
    sent. The stripped key is the new one and is checked beside its sibling.
    """

    from my_claude_code.core.wire_capture import is_reasoning_key

    assert is_reasoning_key(HISTORY_CARRIER_FOREIGN) is False
    assert is_reasoning_key(HISTORY_CARRIER_STRIPPED) is False
