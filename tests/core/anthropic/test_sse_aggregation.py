"""Tests for aggregate_anthropic_sse_to_message."""

from collections.abc import AsyncIterator

import pytest

from my_claude_code.core.anthropic import aggregate_anthropic_sse_to_message
from my_claude_code.core.anthropic.streaming import format_sse_event


async def _aiter(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_aggregate_concatenates_multi_chunk_signature_deltas() -> None:
    chunks = [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {"id": "msg_sig", "usage": {"input_tokens": 10}},
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "step"},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "PART1_"},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "PART2"},
            },
        ),
        format_sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]

    message, error = await aggregate_anthropic_sse_to_message(_aiter(chunks))

    assert error is None
    assert message["content"] == [
        {
            "type": "thinking",
            "thinking": "step",
            "signature": "PART1_PART2",
        }
    ]


@pytest.mark.asyncio
async def test_aggregate_preserves_single_and_empty_signature_deltas() -> None:
    chunks = [
        format_sse_event(
            "message_start",
            {"type": "message_start", "message": {"id": "msg_single"}},
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "SINGLE"},
            },
        ),
        format_sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "thinking", "thinking": "plain"},
            },
        ),
        format_sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 1},
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]

    message, error = await aggregate_anthropic_sse_to_message(_aiter(chunks))

    assert error is None
    assert message["content"] == [
        {"type": "thinking", "thinking": "", "signature": "SINGLE"},
        {"type": "thinking", "thinking": "plain", "signature": ""},
    ]


@pytest.mark.asyncio
async def test_aggregate_preserves_initial_citations_and_appends_citations_deltas() -> (
    None
):
    initial_citation = {
        "type": "web_search_result_location",
        "url": "https://example.com/start",
        "title": "Start Source",
        "cited_text": "initial quote",
    }
    delta_citation_1 = {
        "type": "web_search_result_location",
        "url": "https://example.com/one",
        "title": "First Delta",
        "cited_text": "first quote",
    }
    delta_citation_2 = {
        "type": "web_search_result_location",
        "url": "https://example.com/two",
        "title": "Second Delta",
        "cited_text": "second quote",
    }
    chunks = [
        format_sse_event(
            "message_start",
            {"type": "message_start", "message": {"id": "msg_cite"}},
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "text",
                    "text": "",
                    "citations": [initial_citation],
                },
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Cited answer."},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "citations_delta", "citation": delta_citation_1},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "citations_delta", "citation": delta_citation_2},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "citations_delta", "citation": "not-a-mapping"},
            },
        ),
        format_sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]

    message, error = await aggregate_anthropic_sse_to_message(_aiter(chunks))

    assert error is None
    assert message["content"] == [
        {
            "type": "text",
            "text": "Cited answer.",
            "citations": [initial_citation, delta_citation_1, delta_citation_2],
        }
    ]
