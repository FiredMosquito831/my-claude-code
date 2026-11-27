"""Unit tests for ``core.chatgpt_oauth_media`` request builder & SSE answer parser."""

import base64
import json

import pytest

from my_claude_code.core.chatgpt_oauth_media import (
    ChatGPTOAuthMediaAnswerError,
    chatgpt_oauth_image_request,
    parse_chatgpt_oauth_image_answer,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 24
PNG_B64 = base64.b64encode(PNG).decode("ascii")
PNG2_B64 = base64.b64encode(PNG[::-1]).decode("ascii")


def _sse(*frames: dict | str) -> bytes:
    lines: list[str] = []
    for frame in frames:
        if isinstance(frame, str):
            lines.append(f"data: {frame}\n\n")
        else:
            event_type = frame.get("type", "message")
            lines.append(f"event: {event_type}\ndata: {json.dumps(frame)}\n\n")
    return "".join(lines).encode("utf-8")


def test_chatgpt_oauth_image_request_generate_and_edit() -> None:
    gen = chatgpt_oauth_image_request(
        "draw a ceramic owl",
        "gpt-image-1",
        instructions="codex instructions",
        tool_options={
            "size": "1024x1024",
            "quality": "high",
            "background": "transparent",
            "output_format": "png",
            "moderation": "low",
            "ignored": "nope",
        },
    )
    assert gen == {
        "model": "gpt-image-1",
        "store": False,
        "stream": True,
        "instructions": "codex instructions",
        "tools": [
            {
                "type": "image_generation",
                "size": "1024x1024",
                "quality": "high",
                "background": "transparent",
                "output_format": "png",
                "moderation": "low",
            }
        ],
        "tool_choice": {"type": "image_generation"},
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "draw a ceramic owl"}],
            }
        ],
    }

    edit = chatgpt_oauth_image_request(
        "make the owl cobalt blue",
        "gpt-image-1",
        instructions="codex instructions",
        image_urls=[f"data:image/png;base64,{PNG_B64}", "https://example.test/ref.png"],
    )
    assert edit["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "make the owl cobalt blue"},
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{PNG_B64}",
                },
                {"type": "input_image", "image_url": "https://example.test/ref.png"},
            ],
        }
    ]
    assert edit["tools"] == [{"type": "image_generation"}]


def test_parse_sse_from_output_item_done_and_completed_deduplicates_by_id() -> None:
    sse = _sse(
        {
            "type": "response.output_item.added",
            "item": {
                "id": "ig_1",
                "type": "image_generation_call",
                "status": "in_progress",
            },
        },
        {
            "type": "response.image_generation_call.partial_image",
            "item_id": "ig_1",
            "partial_image_b64": "AAAA",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "ig_1",
                "type": "image_generation_call",
                "status": "completed",
                "result": PNG_B64,
                "revised_prompt": "A glossy ceramic owl on slate",
                "output_format": "png",
                "size": "1024x1024",
                "quality": "high",
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Generated your owl."}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "created_at": 1_700_000_999,
                "status": "completed",
                "output": [
                    {
                        "id": "ig_1",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": PNG_B64,
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 80, "total_tokens": 92},
            },
        },
        "[DONE]",
    )

    answer = parse_chatgpt_oauth_image_answer(sse)
    payload = json.loads(answer.body)
    assert payload == {
        "created": 1_700_000_999,
        "data": [
            {"b64_json": PNG_B64, "revised_prompt": "A glossy ceramic owl on slate"}
        ],
        "size": "1024x1024",
        "quality": "high",
        "output_format": "png",
        "usage": {"input_tokens": 12, "output_tokens": 80, "total_tokens": 92},
    }
    assert answer.usage == {"input_tokens": 12, "output_tokens": 80, "total_tokens": 92}
    assert answer.text == "Generated your owl."


def test_parse_sse_from_completed_output_only_with_multiple_images() -> None:
    sse = _sse(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "id": "ig_a",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": PNG_B64,
                    },
                    {
                        "id": "ig_b",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": PNG2_B64,
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 15},
            },
        }
    )

    answer = parse_chatgpt_oauth_image_answer(sse, created=1_700_000_111)
    payload = json.loads(answer.body)
    assert payload == {
        "created": 1_700_000_111,
        "data": [{"b64_json": PNG_B64}, {"b64_json": PNG2_B64}],
        "usage": {"input_tokens": 5, "output_tokens": 15, "total_tokens": 20},
    }


@pytest.mark.parametrize(
    ("sse_bytes", "fragment"),
    [
        (
            _sse(
                {"type": "error", "error": {"message": "bad prompt", "code": "invalid"}}
            ),
            "upstream error: bad prompt",
        ),
        (
            _sse(
                {
                    "type": "response.failed",
                    "response": {
                        "status": "failed",
                        "error": {"message": "safety block"},
                    },
                }
            ),
            "failed response: safety block",
        ),
        (
            _sse(
                {
                    "type": "response.incomplete",
                    "response": {"status": "incomplete", "output": []},
                }
            ),
            "incomplete without an image",
        ),
        (
            _sse(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "id": "msg_1",
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "I cannot draw that.",
                                    }
                                ],
                            }
                        ],
                    },
                }
            ),
            "without an image",
        ),
        (
            _sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "ig_bad",
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": "not-valid-base64!!!",
                    },
                }
            ),
            "without an image",
        ),
    ],
)
def test_parse_sse_errors_raise_answer_error(sse_bytes: bytes, fragment: str) -> None:
    with pytest.raises(ChatGPTOAuthMediaAnswerError, match=fragment):
        parse_chatgpt_oauth_image_answer(sse_bytes)
