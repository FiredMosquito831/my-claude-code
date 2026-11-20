"""Outbound `/v1/responses` replay for ``Message.encrypted_reasoning``,
list-content ``reasoning_content``, and flat ``_convert_tool_choice``.

Completes `#386`'s deferred PR-2 outbound half (`C-Q3`) and fixes
``_convert_tool_choice`` without ``tool_names`` (`C-Q5`), while keeping
``/v1/messages``, ``/v1/chat/completions``, and ``/v1beta`` byte-identical.
"""

import json
from types import SimpleNamespace
from typing import Any

from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import (
    ReplayOrigin,
    connection_id,
    encode_carrier,
)
from my_claude_code.core.openai_responses.input import (
    convert_request_to_anthropic_payload,
)
from my_claude_code.core.openai_responses.models import OpenAIResponsesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.gemini.client import GeminiProvider
from my_claude_code.providers.openai_chat.request_policy import (
    OpenAIChatRequestPolicy,
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_MALFORMED,
    HISTORY_CARRIER_REPLAYED,
    HISTORY_PLAINTEXT_REPLAYED,
    HISTORY_THINKING_OMITTED,
    build_responses_request_body,
)
from my_claude_code.providers.openai_responses.conversion import _carrier_items
from my_claude_code.providers.recovery import LearnedFactStore
from tests.providers.support import passthrough_rate_limiter

ORIGIN = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="gpt-5.6-terra",
)
FOREIGN_ORIGIN = ReplayOrigin(
    provider="opencode",
    protocol="responses",
    endpoint="https://opencode.ai/zen/v1",
    connection=connection_id("sk-other"),
    model="muse-spark-1.3-contributor-free",
)

RAW_REASONING_ITEM: dict[str, Any] = {
    "type": "reasoning",
    "id": "rs_native_1",
    "status": "completed",
    "summary": [],
    "content": [],
    "encrypted_content": "gAAAAABopaqueNativeBlob123",
}


def _responses_payload(input_items: list[Any]) -> dict[str, Any]:
    req = OpenAIResponsesRequest.model_validate(
        {"model": "gpt-5.6-terra", "input": input_items, "stream": True}
    )
    return convert_request_to_anthropic_payload(req)


def _carrier(
    origin: ReplayOrigin = ORIGIN,
    item_id: str = "rs_carrier_1",
    encrypted: str = "gAAAAABcarrierInner999",
) -> str:
    encoded = encode_carrier(
        origin,
        {
            "type": "reasoning",
            "id": item_id,
            "status": "completed",
            "summary": [],
            "content": [],
            "encrypted_content": encrypted,
        },
    )
    assert encoded is not None
    return encoded


class TestOutboundEncryptedReasoningReplay:
    """``C-Q3a``: ``Message.encrypted_reasoning`` outbound replay & provenance."""

    def test_origin_none_never_leaks_raw_encrypted_reasoning_items(self) -> None:
        """M6: when carrier replay is off (``origin=None``), raw items stay out."""

        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                dict(RAW_REASONING_ITEM),
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
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=None,
            plaintext_replay=False,
            wire_notes=notes,
        )
        assert not [i for i in body["input"] if i.get("type") == "reasoning"]
        assert HISTORY_CARRIER_REPLAYED not in notes
        assert HISTORY_THINKING_OMITTED in notes

    def test_raw_native_encrypted_reasoning_replays_when_origin_matches(self) -> None:
        """M1: inbound ``reasoning`` item with ``encrypted_content`` replays outbound."""

        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                dict(RAW_REASONING_ITEM),
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
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            plaintext_replay=True,
            wire_notes=notes,
        )
        reasoning_items = [i for i in body["input"] if i.get("type") == "reasoning"]
        assert reasoning_items == [RAW_REASONING_ITEM]
        idx = body["input"].index(RAW_REASONING_ITEM)
        assert body["input"][idx + 1]["type"] == "function_call"
        assert notes == {
            HISTORY_CARRIER_REPLAYED: (
                "replayed 1 reasoning carrier from the client's history"
            )
        }

    def test_carrier_inside_encrypted_reasoning_replays_native_when_origin_matches(
        self,
    ) -> None:
        """M1: ``mcc:reasoning:v1:`` inside ``encrypted_reasoning`` decodes to native."""

        carrier = _carrier(ORIGIN, item_id="rs_inner_1", encrypted="E_INNER")
        wrapped_item = {
            "type": "reasoning",
            "id": "rs_outer",
            "status": "completed",
            "summary": [],
            "encrypted_content": carrier,
        }
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                wrapped_item,
                {"type": "message", "role": "assistant", "content": "done"},
                {"type": "message", "role": "user", "content": "next"},
            ]
        )
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            wire_notes=notes,
        )
        wire = json.dumps(body)
        assert "mcc:reasoning:v1:" not in wire
        reasoning_items = [i for i in body["input"] if i.get("type") == "reasoning"]
        assert reasoning_items == [
            {
                "type": "reasoning",
                "id": "rs_inner_1",
                "status": "completed",
                "summary": [],
                "content": [],
                "encrypted_content": "E_INNER",
            }
        ]
        assert HISTORY_CARRIER_REPLAYED in notes

    def test_foreign_carrier_inside_encrypted_reasoning_is_dropped(self) -> None:
        """M2: foreign ``mcc:reasoning:v1:`` inside ``encrypted_reasoning`` is dropped."""

        foreign = _carrier(FOREIGN_ORIGIN, item_id="rs_foreign", encrypted="E_FOREIGN")
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                {
                    "type": "reasoning",
                    "id": "rs_outer",
                    "summary": [],
                    "encrypted_content": foreign,
                },
                {"type": "message", "role": "assistant", "content": "done"},
                {"type": "message", "role": "user", "content": "next"},
            ]
        )
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            wire_notes=notes,
        )
        assert not [i for i in body["input"] if i.get("type") == "reasoning"]
        assert "mcc:reasoning:v1:" not in json.dumps(body)
        assert "E_FOREIGN" not in json.dumps(body)
        assert HISTORY_CARRIER_FOREIGN in notes

    def test_malformed_carrier_inside_encrypted_reasoning_is_dropped(self) -> None:
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                {
                    "type": "reasoning",
                    "id": "rs_bad",
                    "summary": [],
                    "encrypted_content": "mcc:reasoning:v1:!!!not-base64!!!",
                },
                {"type": "message", "role": "assistant", "content": "done"},
                {"type": "message", "role": "user", "content": "next"},
            ]
        )
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            wire_notes=notes,
        )
        assert not [i for i in body["input"] if i.get("type") == "reasoning"]
        assert "mcc:reasoning:v1:" not in json.dumps(body)
        assert HISTORY_CARRIER_MALFORMED in notes

    def test_malformed_and_non_list_encrypted_reasoning_entries_are_ignored_safely(
        self,
    ) -> None:
        bad_message = SimpleNamespace(
            role="assistant",
            content="done",
            encrypted_reasoning="not-a-list",
        )
        bad_entries = SimpleNamespace(
            role="assistant",
            content="done",
            encrypted_reasoning=[
                "not-a-dict",
                {"type": "message", "encrypted_content": "ignored"},
                {"type": "reasoning", "encrypted_content": ""},
                {"type": "reasoning", "encrypted_content": 123},
            ],
        )
        counts: dict[str, int] = {}
        items, replayed = _carrier_items(
            [bad_message, bad_entries],
            ORIGIN,
            counts,
        )
        assert items == {}
        assert replayed == set()
        assert counts == {}

    def test_two_encrypted_reasoning_items_preserve_order_and_deduplicate(self) -> None:
        second = {**RAW_REASONING_ITEM, "id": "rs_native_2", "encrypted_content": "E2"}
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                dict(RAW_REASONING_ITEM),
                dict(second),
                dict(RAW_REASONING_ITEM),
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
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            wire_notes=notes,
        )
        reasoning_items = [i for i in body["input"] if i.get("type") == "reasoning"]
        assert reasoning_items == [RAW_REASONING_ITEM, second]
        assert notes == {
            HISTORY_CARRIER_REPLAYED: (
                "replayed 2 reasoning carriers from the client's history"
            )
        }

    def test_chatgpt_oauth_and_responses_transport_both_replay_encrypted_reasoning(
        self,
    ) -> None:
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                dict(RAW_REASONING_ITEM),
                {"type": "message", "role": "assistant", "content": "done"},
                {"type": "message", "role": "user", "content": "next"},
            ]
        )
        request = MessagesRequest(**payload)
        oauth_body = build_chatgpt_oauth_request_body(
            request, reasoning=ReasoningPolicy.off(), origin=ORIGIN
        )
        assert [i for i in oauth_body["input"] if i.get("type") == "reasoning"] == [
            RAW_REASONING_ITEM
        ]

        transport = ResponsesTransport(
            ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
            base_url="https://example.invalid/v1",
            provider_name="MUSE",
            identity=None,
            api_key=None,
            rate_limiter=passthrough_rate_limiter(),
            memory=LearnedFactStore().memory_for("muse_gateway"),
        )
        transport_body, _ = transport.build_body(
            request, reasoning=ReasoningPolicy.off(), max_output_tokens=256
        )
        assert [i for i in transport_body["input"] if i.get("type") == "reasoning"] == [
            RAW_REASONING_ITEM
        ]


class TestListContentReasoningContentPlaintextReplay:
    """``C-Q3b``: list-content ``Message.reasoning_content`` plaintext replay."""

    def test_list_content_message_replays_reasoning_content_when_plaintext_replay_on(
        self,
    ) -> None:
        """M3: list-content assistant turn keeps ``reasoning_content``."""

        request = MessagesRequest.model_validate(
            {
                "model": "gpt-5.6-terra",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "go"},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Answer"}],
                        "reasoning_content": "step by step",
                    },
                    {"role": "user", "content": "next"},
                ],
            }
        )
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            plaintext_replay=True,
            wire_notes=notes,
        )
        assert body["input"][1] == {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "step by step"}],
        }
        assert body["input"][2]["type"] == "message"
        assert notes == {
            HISTORY_PLAINTEXT_REPLAYED: (
                "replayed 1 readable reasoning block from the client's history"
            )
        }

    def test_list_content_tool_turn_replays_reasoning_content_without_duplicating_carrier(
        self,
    ) -> None:
        item_with_summary = {
            **RAW_REASONING_ITEM,
            "summary": [{"type": "summary_text", "text": "Need the tool."}],
        }
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                item_with_summary,
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
        request = MessagesRequest(**payload)

        # With carrier replay ON: encrypted_reasoning replays once; plaintext summary does not duplicate it.
        carrier_notes: dict[str, str] = {}
        carrier_body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            plaintext_replay=True,
            wire_notes=carrier_notes,
        )
        assert [i for i in carrier_body["input"] if i.get("type") == "reasoning"] == [
            item_with_summary
        ]
        assert HISTORY_PLAINTEXT_REPLAYED not in carrier_notes
        assert HISTORY_THINKING_OMITTED not in carrier_notes

        # With carrier replay OFF and plaintext replay ON: reasoning_content on the tool_use list turn replays.
        plain_notes: dict[str, str] = {}
        plain_body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=None,
            plaintext_replay=True,
            wire_notes=plain_notes,
        )
        assert [i for i in plain_body["input"] if i.get("type") == "reasoning"] == [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Need the tool."}],
            }
        ]
        assert plain_notes == {
            HISTORY_PLAINTEXT_REPLAYED: (
                "replayed 1 readable reasoning block from the client's history"
            )
        }

    def test_list_content_reasoning_content_replays_when_foreign_carrier_dropped(
        self,
    ) -> None:
        foreign = _carrier(FOREIGN_ORIGIN, item_id="rs_foreign", encrypted="E_FOREIGN")
        payload = _responses_payload(
            [
                {"type": "message", "role": "user", "content": "go"},
                {
                    "type": "reasoning",
                    "id": "rs_outer",
                    "summary": [{"type": "summary_text", "text": "portable thought"}],
                    "encrypted_content": foreign,
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
        request = MessagesRequest(**payload)
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=ORIGIN,
            plaintext_replay=True,
            wire_notes=notes,
        )
        assert [i for i in body["input"] if i.get("type") == "reasoning"] == [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "portable thought"}],
            }
        ]
        assert HISTORY_CARRIER_FOREIGN in notes
        assert HISTORY_PLAINTEXT_REPLAYED in notes

    def test_list_content_reasoning_content_omission_note_when_disabled(self) -> None:
        request = MessagesRequest.model_validate(
            {
                "model": "gpt-5.6-terra",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "go"},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Answer"}],
                        "reasoning_content": "secret plan",
                    },
                    {"role": "user", "content": "next"},
                ],
            }
        )
        notes: dict[str, str] = {}
        body = build_responses_request_body(
            request,
            reasoning=ReasoningPolicy.off(),
            origin=None,
            plaintext_replay=False,
            wire_notes=notes,
        )
        assert "secret plan" not in json.dumps(body)
        assert "<think" not in json.dumps(body)
        assert notes[HISTORY_THINKING_OMITTED].startswith(
            "omitted 1 earlier thinking block (11 chars)"
        )


class TestConvertToolChoiceResponsesShape:
    """``C-Q5``: forced ``tool_choice`` always emits flat Responses shape."""

    def test_forced_tool_choice_without_tool_names_uses_responses_shape(self) -> None:
        """M5: ``tool_names=None`` emits ``{"type": "function", "name": "echo"}``."""

        request = MessagesRequest.model_validate(
            {
                "model": "gpt-5.6-terra",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "call echo"}],
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo input",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
                "tool_choice": {"type": "tool", "name": "echo"},
            }
        )
        body = build_responses_request_body(
            request, reasoning=ReasoningPolicy.off(), tool_name_max_length=None
        )
        oauth_body = build_chatgpt_oauth_request_body(
            request, reasoning=ReasoningPolicy.off(), tool_name_max_length=None
        )
        assert body["tool_choice"] == {"type": "function", "name": "echo"}
        assert oauth_body["tool_choice"] == {"type": "function", "name": "echo"}
        assert "function" not in body["tool_choice"]

    def test_forced_tool_choice_with_tool_names_aliases_responses_shape(self) -> None:
        long_name = "mcp__plugin_very_long_server_name__" + ("x" * 40)
        request = MessagesRequest.model_validate(
            {
                "model": "gpt-5.6-terra",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "call long tool"}],
                "tools": [
                    {
                        "name": long_name,
                        "description": "Long tool",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
                "tool_choice": {"type": "tool", "name": long_name},
            }
        )
        body = build_responses_request_body(
            request, reasoning=ReasoningPolicy.off(), tool_name_max_length=64
        )
        assert body["tool_choice"]["type"] == "function"
        assert "function" not in body["tool_choice"]
        assert body["tool_choice"]["name"] == body["tools"][0]["name"]
        assert len(body["tool_choice"]["name"]) <= 64


class TestNonResponsesDoorsByteIdentical:
    """``/v1/messages``, ``/v1/chat/completions``, and ``/v1beta`` stay untouched."""

    def test_other_doors_ignore_encrypted_reasoning_byte_identically(self) -> None:
        with_blob = MessagesRequest(
            **_responses_payload(
                [
                    {"type": "message", "role": "user", "content": "go"},
                    dict(RAW_REASONING_ITEM),
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
        without_blob = MessagesRequest(
            **_responses_payload(
                [
                    {"type": "message", "role": "user", "content": "go"},
                    {
                        "type": "reasoning",
                        "id": "rs_native_1",
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
        msg_left = build_anthropic_messages_body(
            with_blob, reasoning=ReasoningPolicy.off()
        )
        msg_right = build_anthropic_messages_body(
            without_blob, reasoning=ReasoningPolicy.off()
        )
        assert json.dumps(msg_left, sort_keys=True) == json.dumps(
            msg_right, sort_keys=True
        )

        chat_policy = OpenAIChatRequestPolicy(
            provider_name="TEST",
            reasoning_replay=ReasoningReplayMode.DISABLED,
        )
        chat_left = build_openai_chat_request_body(
            with_blob, reasoning=ReasoningPolicy.off(), policy=chat_policy
        )
        chat_right = build_openai_chat_request_body(
            without_blob, reasoning=ReasoningPolicy.off(), policy=chat_policy
        )
        assert json.dumps(chat_left, sort_keys=True) == json.dumps(
            chat_right, sort_keys=True
        )

        gemini = GeminiProvider(
            ProviderConfig(api_key="AIza-test", base_url="https://example.invalid"),
            rate_limiter=passthrough_rate_limiter(),
        )
        gemini_left = gemini._build_request_body(
            with_blob, reasoning=ReasoningPolicy.off()
        )
        gemini_right = gemini._build_request_body(
            without_blob, reasoning=ReasoningPolicy.off()
        )
        assert json.dumps(gemini_left, sort_keys=True) == json.dumps(
            gemini_right, sort_keys=True
        )
