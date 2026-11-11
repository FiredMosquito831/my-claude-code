"""The carrier-surface drop count: every dropping cell, per door, per mode (8.20.0).

Q1's answer was *count instead of build*. A count that says "something was
dropped" without saying which cell is a shrug, so each cell has its own key and
its own assertion here. Every case is a fake upstream: a synthetic request
through the real conversion, or a scripted provider through the real handler.
No network, no server, no live call.
"""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.request_capture import RequestCapture, WireProtocol
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.carrier_surface import (
    CARRIER_SURFACE_DROPPED,
    CHAT_DETAILS_CARRIER,
    CHAT_DETAILS_OTHER,
    CHAT_REDACTED_THINKING,
    GEMINI_THOUGHT_PART,
    GEMINI_THOUGHT_SIGNATURE,
    GEMINI_THOUGHT_UNSIGNED,
    merge_carrier_surface_note,
)
from my_claude_code.core.gemini_api.input import (
    convert_request_to_anthropic_payload as convert_gemini_request,
)
from my_claude_code.core.gemini_api.models import GeminiGenerateContentRequest
from my_claude_code.core.openai_chat_completions.input import (
    convert_request_to_anthropic_payload as convert_chat_request,
)
from my_claude_code.core.openai_chat_completions.models import (
    OpenAIChatCompletionRequest,
)
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.wire_capture import is_reasoning_key
from tests.api.support import create_test_app

#: A real envelope, minted by the carrier module's own prefix.
CARRIER = "mcc:reasoning:v1:eyJvcmlnaW4iOnt9fQ"


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "carrier_surface.db")
    yield store
    store.close()


def _events(*payloads: dict[str, Any]) -> list[str]:
    return [
        f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"
        for payload in payloads
    ]


async def _run(capture: RequestCapture, frames: list[str]) -> None:
    async def body() -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    async for _ in capture.wrap(body()):
        pass


def _capture(
    store: RequestLogStore,
    *,
    protocol: WireProtocol = "openai_chat",
    stream: bool = True,
    ingress: dict[str, int] | None = None,
    egress: frozenset[str] = frozenset(),
) -> RequestCapture:
    return RequestCapture(
        store,
        request_id="req_test",
        endpoint="/v1/chat/completions",
        protocol=protocol,
        stream=stream,
        requested_model="m",
        input_text="hello",
        params=None,
        carrier_surface=ingress,
        carrier_surface_egress=egress,
    )


def _row(store: RequestLogStore) -> dict[str, Any]:
    # The writer thread owns the INSERT; closing drains it, which is how every
    # other capture test reads a row back in the same test.
    store.close()
    row = store.get_request("req_test")
    assert row is not None
    return row


def _note(store: RequestLogStore) -> dict[str, int] | None:
    params = _row(store).get("params") or {}
    return params.get(CARRIER_SURFACE_DROPPED)


def _chat_request(messages: list[dict[str, Any]]) -> OpenAIChatCompletionRequest:
    return OpenAIChatCompletionRequest.model_validate(
        {"model": "m", "messages": messages}
    )


# --------------------------------------------------------------------------
# Chat IN -- the client's ``reasoning_details``
# --------------------------------------------------------------------------


def test_a_chat_client_carrier_is_counted():
    counts: dict[str, int] = {}
    convert_chat_request(
        _chat_request(
            [
                {
                    "role": "assistant",
                    "content": "hi",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": CARRIER}
                    ],
                },
                {"role": "user", "content": "go"},
            ]
        ),
        carrier_surface=counts,
    )
    assert counts == {CHAT_DETAILS_CARRIER: 1}


def test_a_chat_client_detail_that_is_not_a_carrier_is_counted_separately():
    counts: dict[str, int] = {}
    convert_chat_request(
        _chat_request(
            [
                {
                    "role": "assistant",
                    "content": "hi",
                    "reasoning_details": [{"type": "reasoning.text", "text": "plan"}],
                }
            ]
        ),
        carrier_surface=counts,
    )
    assert counts == {CHAT_DETAILS_OTHER: 1}


def test_both_chat_ingress_cells_are_counted_together():
    counts: dict[str, int] = {}
    convert_chat_request(
        _chat_request(
            [
                {
                    "role": "assistant",
                    "content": "hi",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": CARRIER},
                        {"type": "reasoning.text", "text": "plan"},
                    ],
                }
            ]
        ),
        carrier_surface=counts,
    )
    assert counts == {CHAT_DETAILS_CARRIER: 1, CHAT_DETAILS_OTHER: 1}


def test_a_carrier_on_a_non_assistant_turn_is_still_counted():
    """The drop is the same drop wherever the client put the field."""

    counts: dict[str, int] = {}
    convert_chat_request(
        _chat_request(
            [
                {
                    "role": "user",
                    "content": "go",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": CARRIER}
                    ],
                }
            ]
        ),
        carrier_surface=counts,
    )
    assert counts == {CHAT_DETAILS_CARRIER: 1}


def test_a_chat_request_with_no_details_counts_nothing():
    counts: dict[str, int] = {}
    convert_chat_request(
        _chat_request([{"role": "user", "content": "go"}]), carrier_surface=counts
    )
    assert counts == {}


def test_the_conversion_is_unchanged_when_nobody_asks_for_a_count():
    """``carrier_surface=None`` -- every other caller -- records nothing."""

    payload = convert_chat_request(
        _chat_request(
            [
                {
                    "role": "assistant",
                    "content": "hi",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": CARRIER}
                    ],
                },
                {"role": "user", "content": "go"},
            ]
        )
    )
    assert payload["messages"][0] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "hi"}],
    }


# --------------------------------------------------------------------------
# Chat OUT -- the ``redacted_thinking`` block the door cannot hand over
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_a_chat_redacted_thinking_block_is_counted(store, stream):
    capture = _capture(store, stream=stream, egress=frozenset({CHAT_REDACTED_THINKING}))
    await _run(
        capture,
        _events(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "redacted_thinking", "data": "opaque"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ),
    )
    assert _note(store) == {CHAT_REDACTED_THINKING: 1}


@pytest.mark.asyncio
async def test_a_redacted_thinking_block_is_not_counted_for_another_door(store):
    """A Messages client is handed that block, so nothing was dropped."""

    capture = _capture(store, protocol="anthropic", egress=frozenset())
    await _run(
        capture,
        _events(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "redacted_thinking", "data": "opaque"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ),
    )
    assert _note(store) is None


# --------------------------------------------------------------------------
# Gemini IN -- the replayed thought part and its signature
# --------------------------------------------------------------------------


def _gemini_request(parts: list[dict[str, Any]]) -> GeminiGenerateContentRequest:
    return GeminiGenerateContentRequest.model_validate(
        {
            "model": "gemini-2.5-pro",
            "contents": [
                {"role": "model", "parts": parts},
                {"role": "user", "parts": [{"text": "go"}]},
            ],
        }
    )


def test_a_replayed_thought_part_is_counted():
    conversion = convert_gemini_request(
        _gemini_request([{"text": "I should read the file", "thought": True}])
    )
    assert conversion.carrier_surface == {GEMINI_THOUGHT_PART: 1}


def test_a_thought_signature_is_counted():
    conversion = convert_gemini_request(
        _gemini_request([{"text": "answer", "thoughtSignature": "c2lnbmF0dXJl"}])
    )
    assert conversion.carrier_surface == {GEMINI_THOUGHT_SIGNATURE: 1}


def test_a_thought_part_with_a_signature_counts_both_cells():
    conversion = convert_gemini_request(
        _gemini_request([{"text": "plan", "thought": True, "thoughtSignature": "sig"}])
    )
    assert conversion.carrier_surface == {
        GEMINI_THOUGHT_PART: 1,
        GEMINI_THOUGHT_SIGNATURE: 1,
    }


def test_an_empty_signature_is_not_a_carrier():
    conversion = convert_gemini_request(
        _gemini_request([{"text": "answer", "thoughtSignature": ""}])
    )
    assert conversion.carrier_surface == {}


def test_a_gemini_request_with_no_thoughts_counts_nothing():
    conversion = convert_gemini_request(_gemini_request([{"text": "answer"}]))
    assert conversion.carrier_surface == {}


# --------------------------------------------------------------------------
# Gemini OUT -- a thought emitted with no signature
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_an_unsigned_thought_is_counted(store, stream):
    capture = _capture(
        store,
        protocol="gemini",
        stream=stream,
        egress=frozenset({GEMINI_THOUGHT_UNSIGNED}),
    )
    await _run(
        capture,
        _events(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "plan"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ),
    )
    assert _note(store) == {GEMINI_THOUGHT_UNSIGNED: 1}


@pytest.mark.asyncio
async def test_an_unsigned_thought_is_not_counted_when_none_was_asked_for(store):
    """A client that did not ask for thoughts is handed none, so none dropped."""

    capture = _capture(store, protocol="gemini", egress=frozenset())
    await _run(
        capture,
        _events(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ),
    )
    assert _note(store) is None


# --------------------------------------------------------------------------
# The rules the note keeps
# --------------------------------------------------------------------------


def test_the_key_is_never_read_as_a_reasoning_instruction():
    assert is_reasoning_key(CARRIER_SURFACE_DROPPED) is False


@pytest.mark.asyncio
async def test_the_note_is_absent_not_zero(store):
    """A request that dropped nothing carries no key at all."""

    capture = _capture(store, egress=frozenset({CHAT_REDACTED_THINKING}))
    await _run(
        capture,
        _events(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ),
    )
    assert _note(store) is None


def test_the_note_carries_counts_only_never_content():
    params: dict[str, object] = {}
    merge_carrier_surface_note(params, {CHAT_DETAILS_CARRIER: 2})
    rendered = json.dumps(params)
    assert CARRIER not in rendered
    assert params[CARRIER_SURFACE_DROPPED] == {CHAT_DETAILS_CARRIER: 2}


def test_the_breakdown_is_sorted_and_drops_zero_cells():
    params: dict[str, object] = {}
    merge_carrier_surface_note(
        params, {CHAT_DETAILS_OTHER: 1, CHAT_DETAILS_CARRIER: 0, GEMINI_THOUGHT_PART: 3}
    )
    breakdown = params[CARRIER_SURFACE_DROPPED]
    assert isinstance(breakdown, dict)
    assert breakdown == {CHAT_DETAILS_OTHER: 1, GEMINI_THOUGHT_PART: 3}
    assert list(breakdown) == sorted(breakdown)


# --------------------------------------------------------------------------
# End to end -- the handlers wire it
# --------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.preflight_stream = MagicMock()
        self.requests: list[Any] = []

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data, **_kwargs):
        self.requests.append(request_data)
        for chunk in self.chunks:
            yield chunk


def _stream(*blocks: tuple[str, dict[str, Any]]) -> list[str]:
    frames = [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 3, "output_tokens": 0}},
            },
        )
    ]
    for index, (kind, block) in enumerate(blocks):
        frames.append(
            format_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": kind, **block},
                },
            )
        )
        frames.append(
            format_sse_event(
                "content_block_stop", {"type": "content_block_stop", "index": index}
            )
        )
    frames.append(
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
        )
    )
    frames.append(format_sse_event("message_stop", {"type": "message_stop"}))
    return frames


def test_the_chat_door_records_both_halves_of_cell_nine(monkeypatch):
    from my_claude_code.core import request_log
    from tests.support.request_log_writes import FlushWatch

    watch = FlushWatch().install(monkeypatch)
    provider = _FakeProvider(_stream(("redacted_thinking", {"data": "opaque"})))
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "nvidia_nim/test-model",
                "max_tokens": 32,
                "messages": [
                    {
                        "role": "assistant",
                        "content": "hi",
                        "reasoning_details": [
                            {"type": "reasoning.encrypted", "data": CARRIER}
                        ],
                    },
                    {"role": "user", "content": "go"},
                ],
            },
        )
    assert response.status_code == 200
    request_id = response.headers["request-id"]
    watch.wait(request_id)
    store = request_log.get_request_log_store()
    assert store is not None
    row = store.get_request(request_id)
    assert row is not None
    assert row["params"][CARRIER_SURFACE_DROPPED] == {
        CHAT_DETAILS_CARRIER: 1,
        CHAT_REDACTED_THINKING: 1,
    }


def test_the_gemini_door_records_its_ingress_cells(monkeypatch):
    from my_claude_code.core import request_log
    from tests.support.request_log_writes import FlushWatch

    watch = FlushWatch().install(monkeypatch)
    provider = _FakeProvider(_stream(("text", {"text": "answer"})))
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1beta/models/gemini-2.5-pro:generateContent",
            json={
                "contents": [
                    {
                        "role": "model",
                        "parts": [
                            {
                                "text": "plan",
                                "thought": True,
                                "thoughtSignature": "sig",
                            }
                        ],
                    },
                    {"role": "user", "parts": [{"text": "go"}]},
                ]
            },
        )
    assert response.status_code == 200
    request_id = response.headers["request-id"]
    watch.wait(request_id)
    store = request_log.get_request_log_store()
    assert store is not None
    row = store.get_request(request_id)
    assert row is not None
    assert row["params"][CARRIER_SURFACE_DROPPED] == {
        GEMINI_THOUGHT_PART: 1,
        GEMINI_THOUGHT_SIGNATURE: 1,
    }


def test_a_request_that_drops_nothing_carries_no_note(monkeypatch):
    from my_claude_code.core import request_log
    from tests.support.request_log_writes import FlushWatch

    watch = FlushWatch().install(monkeypatch)
    provider = _FakeProvider(_stream(("text", {"text": "answer"})))
    app = create_test_app()
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "nvidia_nim/test-model",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "go"}],
            },
        )
    assert response.status_code == 200
    request_id = response.headers["request-id"]
    watch.wait(request_id)
    store = request_log.get_request_log_store()
    assert store is not None
    row = store.get_request(request_id)
    assert row is not None
    assert CARRIER_SURFACE_DROPPED not in (row["params"] or {})


def test_the_gemini_door_does_not_count_a_thought_nobody_asked_for(monkeypatch):
    """A client that did not ask for thoughts is handed none, so none dropped.

    The upstream still emits a ``thinking`` block; the Gemini assembler
    suppresses it. The count must follow what the door EMITTED, not what the
    upstream produced -- otherwise the note would claim a drop that never
    happened.
    """

    from my_claude_code.core import request_log
    from tests.support.request_log_writes import FlushWatch

    watch = FlushWatch().install(monkeypatch)
    provider = _FakeProvider(
        _stream(
            ("thinking", {"thinking": ""}),
            ("text", {"text": "answer"}),
        )
    )
    app = create_test_app()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1beta/models/gemini-2.5-pro:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "go"}]}]},
        )
    assert response.status_code == 200
    request_id = response.headers["request-id"]
    watch.wait(request_id)
    store = request_log.get_request_log_store()
    assert store is not None
    row = store.get_request(request_id)
    assert row is not None
    assert CARRIER_SURFACE_DROPPED not in (row["params"] or {})
