"""The inbound Responses door reads back the ``encrypted_content`` it emits.

PR-CARRIER-SURFACE-SWEEP, Item 1. The Responses API returns an opaque
``encrypted_content`` blob beside a ``reasoning`` item, and on a ``store:
false`` conversation that blob is the only way reasoning survives to the next
turn. MCC asked for it, replayed it outbound (8.16.0), and -- until 8.19.0 --
threw it away on the way *in*: ``core/openai_responses/input.py``'s
``reasoning`` branch kept only the readable text, so a Codex CLI -> MCC
conversation lost the blob at the first turn boundary. The exact mirror of the
outbound bug 8.14.3 fixed.

**The design, and why it is a side channel.** The inbound door converts to an
**Anthropic** payload. Putting an OpenAI ciphertext into
``redacted_thinking.data`` would send it to an Anthropic host, which is a
guaranteed 400. So the blob rides on ``Message.encrypted_reasoning`` -- one
list per assistant turn, each entry the whole ``reasoning`` item verbatim,
never a projection, because a replay has to hand the host back exactly what it
minted -- and that field is declared ``Field(exclude=True)``.

**``exclude=True`` is the leak rule, and this file proves it rather than
asserting it.** Every ``model_dump()`` of a ``Message`` cannot carry the field,
by construction: not ``dump_messages_request`` (which DeepSeek then
re-validates), not the Messages door's own dump, not the request snapshot.
The alternative the spec first named -- strip it in one function -- relies on
that function remembering, and the grounding review found the function it
named was not even a strip.

The mutation controls this file is built to catch (each turns a named test
red) are recorded in the PR body:

* **M1** delete the append in ``_append_input_item`` -> the retention
  assertion fails.
* **M2** drop ``Field(exclude=True)`` -> the no-leak assertion fails.
"""

import json
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.request_serialization import dump_messages_request
from my_claude_code.core.openai_responses.input import (
    convert_request_to_anthropic_payload,
)
from my_claude_code.core.openai_responses.models import OpenAIResponsesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.deepseek.compat import build_deepseek_request_body

#: The item the client echoes back, byte for byte. Deliberately carries a
#: ``status`` and an empty ``summary``/``content`` -- the shape the real
#: measurement recorded (7.5 % Luna / 74.8 % Sol) -- because a projection that
#: kept only ``encrypted_content`` would pass a test written against a
#: one-key item.
REASONING_ITEM: dict[str, Any] = {
    "type": "reasoning",
    "id": "rs_1",
    "status": "completed",
    "summary": [],
    "content": [],
    "encrypted_content": "E1",
}


def _request(input_items: list[Any]) -> OpenAIResponsesRequest:
    return OpenAIResponsesRequest.model_validate(
        {"model": "gpt-5-codex", "input": input_items, "stream": True}
    )


def _payload(input_items: list[Any]) -> dict[str, Any]:
    return convert_request_to_anthropic_payload(_request(input_items))


def _spec_input() -> list[Any]:
    """The exact input the spec's §2.6 fixture names."""

    return [
        {"type": "message", "role": "user", "content": "go"},
        dict(REASONING_ITEM),
        {"type": "function_call", "call_id": "c1", "name": "Read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "message", "role": "user", "content": "next"},
    ]


class TestTheBlobIsRetained:
    """§2.6: the assistant turn's list is the item, byte for byte."""

    def test_the_user_turn_carries_none(self) -> None:
        payload = _payload(_spec_input())
        assert payload["messages"][0]["role"] == "user"
        assert "encrypted_reasoning" not in payload["messages"][0]

    def test_the_assistant_turn_carries_the_whole_item(self) -> None:
        payload = _payload(_spec_input())
        assistant = payload["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["encrypted_reasoning"] == [REASONING_ITEM]

    def test_the_item_survives_a_model_round_trip_verbatim(self) -> None:
        """The handler builds a ``MessagesRequest`` from the payload; the blob
        must be readable on the model, not merely in the intermediate dict."""

        request = MessagesRequest(**_payload(_spec_input()))
        assistant = request.messages[1]
        assert assistant.encrypted_reasoning == [REASONING_ITEM]
        # And it is the *whole* item: a projection would have lost these.
        carried = assistant.encrypted_reasoning
        assert carried is not None
        assert carried[0]["status"] == "completed"
        assert carried[0]["id"] == "rs_1"

    def test_two_reasoning_items_on_one_turn_are_both_kept_in_order(self) -> None:
        """A parallel-tool turn emits one item per unit of work. Appending is
        the rule; a replacement would silently keep only the last."""

        second = {**REASONING_ITEM, "id": "rs_2", "encrypted_content": "E2"}
        payload = _payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                dict(REASONING_ITEM),
                dict(second),
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "Read",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            ]
        )
        assistant = payload["messages"][1]
        assert assistant["encrypted_reasoning"] == [REASONING_ITEM, second]

    def test_a_reasoning_item_without_a_blob_contributes_nothing(self) -> None:
        """A summary-only item is text, exactly as before. It must not create
        an empty list -- that would change the payload for every existing
        conversation."""

        item = {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "thought"}],
        }
        payload = _payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                item,
                {"type": "message", "role": "assistant", "content": "done"},
            ]
        )
        assistant = payload["messages"][1]
        assert assistant["reasoning_content"] == "thought"
        assert "encrypted_reasoning" not in assistant

    @pytest.mark.parametrize("empty", ["", None, 0, [], {}])
    def test_an_empty_or_non_string_blob_contributes_nothing(self, empty) -> None:
        """The rule is "a non-empty string", not "the key is present": a client
        that sent ``""`` or ``null`` must not create a list of junk."""

        item = {**REASONING_ITEM, "encrypted_content": empty}
        payload = _payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                item,
                {"type": "message", "role": "assistant", "content": "done"},
            ]
        )
        assert "encrypted_reasoning" not in payload["messages"][1]


class TestTheTextPathDidNotMove:
    """§2.6: ``messages[0].content`` is unchanged from v8.18.2."""

    def test_the_user_turn_is_byte_identical(self) -> None:
        payload = _payload(_spec_input())
        assert payload["messages"][0] == {"role": "user", "content": "go"}

    def test_a_summary_only_item_still_becomes_reasoning_content(self) -> None:
        item = {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "Need the result."}],
        }
        payload = _payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                item,
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "Read",
                    "arguments": "{}",
                },
            ]
        )
        assert payload["messages"][1]["reasoning_content"] == "Need the result."

    def test_a_blob_item_with_text_still_carries_both(self) -> None:
        """The two halves are independent: a ``content[].reasoning_text`` item
        that also carries a blob keeps its text AND its item."""

        item = {
            "type": "reasoning",
            "id": "rs_1",
            "content": [{"type": "reasoning_text", "text": "readable"}],
            "encrypted_content": "E1",
        }
        payload = _payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                item,
                {"type": "message", "role": "assistant", "content": "done"},
            ]
        )
        assistant = payload["messages"][1]
        assert assistant["reasoning_content"] == "readable"
        assert assistant["encrypted_reasoning"] == [item]


class TestTheBlobNeverLeaks:
    """§2.3: ``Field(exclude=True)`` makes the leak structurally impossible."""

    def test_the_field_is_declared_excluded(self) -> None:
        """The declaration itself, so a future edit that drops it fails here
        with the reason rather than only at the dump."""

        field = Message.model_fields["encrypted_reasoning"]
        assert field.exclude is True

    def test_dump_messages_request_carries_no_such_key(self) -> None:
        request = MessagesRequest(**_payload(_spec_input()))
        dumped = dump_messages_request(request)
        assert "encrypted_reasoning" not in json.dumps(dumped)

    def test_the_whole_model_dump_carries_no_such_key(self) -> None:
        """Stronger than the serialiser: nothing may reach it in the first
        place, so every other consumer of ``model_dump`` is covered too."""

        request = MessagesRequest(**_payload(_spec_input()))
        assert "encrypted_reasoning" not in json.dumps(request.model_dump())
        assert "encrypted_reasoning" not in json.dumps(
            request.model_dump(exclude_none=True)
        )

    def test_deepseek_is_byte_identical_with_and_without_the_blob(self) -> None:
        """The one Chat door that re-validates the dumped dict. Its body must
        not move by a byte when a client starts sending a blob."""

        with_blob = MessagesRequest(**_payload(_spec_input()))
        without_blob = MessagesRequest(
            **_payload(
                [
                    {"type": "message", "role": "user", "content": "go"},
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "status": "completed",
                        "summary": [],
                        "content": [],
                    },
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "Read",
                        "arguments": "{}",
                    },
                    {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                    {"type": "message", "role": "user", "content": "next"},
                ]
            )
        )
        left = build_deepseek_request_body(with_blob, reasoning=ReasoningPolicy.off())
        right = build_deepseek_request_body(
            without_blob, reasoning=ReasoningPolicy.off()
        )
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

    def test_the_anthropic_door_is_byte_identical_with_and_without_the_blob(
        self,
    ) -> None:
        """The door an OpenAI ciphertext would break if it ever reached it.

        The same conversation twice, differing only in whether the reasoning
        item carried a blob. The inbound door emits no assistant message for a
        ``reasoning`` item, so both histories convert to the same turns and the
        two bodies must be equal -- and the blob must appear in neither.
        """

        from my_claude_code.providers.anthropic_messages.request import (
            build_anthropic_messages_body,
        )

        def _input(blob: dict[str, Any]) -> list[Any]:
            return [
                {"type": "message", "role": "user", "content": "go"},
                blob,
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "Read",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                {"type": "message", "role": "user", "content": "next"},
            ]

        with_blob = MessagesRequest(**_payload(_input(dict(REASONING_ITEM))))
        without_blob = MessagesRequest(
            **_payload(
                _input(
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "status": "completed",
                        "summary": [],
                        "content": [],
                    }
                )
            )
        )
        left = build_anthropic_messages_body(with_blob, reasoning=ReasoningPolicy.off())
        right = build_anthropic_messages_body(
            without_blob, reasoning=ReasoningPolicy.off()
        )
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
        assert "E1" not in json.dumps(left)
