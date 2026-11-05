"""The Mistral image-tool parser and builder (8.14.0).

A chat answer carries the picture by reference, not by value, and no offline
source states which reference it carries (spec E11). So the parser SEARCHES
every form the answers are known to take, and the tests below are the census:
a file id in the tool call's arguments (as a JSON string, as an object, as a
bare id, as a ``/files/<id>`` path), an inline ``b64_json`` or image part, a
URL, and the shapes that must be refused rather than guessed at.
"""

import base64
import json

import pytest

from my_claude_code.core.mistral_media import (
    MistralAnswerError,
    image_tool_request,
    images_answer,
    parse_image_tool_answer,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
B64 = base64.b64encode(PNG).decode("ascii")


def _chat(message: dict, **extra: object) -> bytes:
    payload: dict[str, object] = {
        "id": "cmpl-1",
        "created": 1_700_000_000,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    payload.update(extra)
    return json.dumps(payload).encode()


def _tool_call(arguments: object, name: str = "image_generation") -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments
            if isinstance(arguments, str)
            else json.dumps(arguments),
        },
    }


# ----------------------------------------------------------------- requests


def test_the_request_is_one_user_message_and_the_tool() -> None:
    body = image_tool_request("a red fox", "mistral-medium-3-5", "image_generation")
    assert body == {
        "model": "mistral-medium-3-5",
        "messages": [{"role": "user", "content": "a red fox"}],
        "tools": [{"type": "image_generation"}],
        "stream": False,
    }


def test_the_request_carries_no_system_prompt_and_no_history() -> None:
    """The tool has no conversation: one user message, nothing else."""
    body = image_tool_request("p", "m", "image_generation")
    assert [message["role"] for message in body["messages"]] == ["user"]


def test_the_tool_spelling_is_the_callers_constant() -> None:
    """``core`` imports nothing from ``config`` (III.5), so it is handed in."""
    body = image_tool_request("p", "m", "some_other_tool")
    assert body["tools"] == [{"type": "some_other_tool"}]


# ------------------------------------------------------------ file id forms


def test_a_file_id_in_a_json_object_argument() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            }
        )
    )
    assert answer.file_id == "abc123def456"
    assert answer.inline_bytes == b""


def test_a_file_id_under_an_id_key() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"id": "file_9f8e7d6c5b"})],
            }
        )
    )
    assert answer.file_id == "file_9f8e7d6c5b"


def test_a_file_id_as_a_bare_argument_string() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("abc123def456")],
            }
        )
    )
    assert answer.file_id == "abc123def456"


def test_a_file_id_named_by_a_files_path_in_the_arguments() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"image": "/v1/files/abc123def456/content"})],
            }
        )
    )
    assert answer.file_id == "abc123def456"


def test_a_file_id_named_by_a_files_path_in_the_prose() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": "Here it is: /v1/files/abc123def456/content",
                "tool_calls": [],
            }
        )
    )
    assert answer.file_id == "abc123def456"


def test_a_file_id_in_a_nested_argument_object() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"result": {"file_id": "abc123def456"}})],
            }
        )
    )
    assert answer.file_id == "abc123def456"


def test_a_model_name_in_the_arguments_is_not_read_as_a_file_id() -> None:
    """``mistral-large-4`` is id-shaped; a wrong id is a charged fetch of nothing."""
    with pytest.raises(MistralAnswerError):
        parse_image_tool_answer(
            _chat(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_tool_call({"model": "mistral-large-4"})],
                }
            )
        )


def test_a_malformed_tool_call_json_is_not_a_crash() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "image_generation", "arguments": "{nope"},
                    },
                    _tool_call({"file_id": "abc123def456"}),
                ],
            }
        )
    )
    # The broken call is skipped; the good one after it is read.
    assert answer.file_id == "abc123def456"


# ------------------------------------------------------------ inline bytes


def test_inline_b64_json_in_the_message() -> None:
    answer = parse_image_tool_answer(
        _chat({"role": "assistant", "content": None, "b64_json": B64})
    )
    assert answer.inline_bytes == PNG
    assert answer.file_id is None


def test_inline_b64_json_inside_the_tool_arguments() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"b64_json": B64})],
            }
        )
    )
    assert answer.inline_bytes == PNG


def test_inline_base64_wins_over_a_file_id() -> None:
    """Bytes already in hand cost nothing; a file id costs a second call."""
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "b64_json": B64,
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            }
        )
    )
    assert answer.inline_bytes == PNG
    assert answer.file_id == "abc123def456"


def test_base64_that_does_not_decode_is_not_bytes() -> None:
    with pytest.raises(MistralAnswerError):
        parse_image_tool_answer(
            _chat({"role": "assistant", "content": None, "b64_json": "!!!!"})
        )


# -------------------------------------------------------------------- urls


def test_a_url_in_the_message() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "url": "https://cdn.example.test/a.png",
            }
        )
    )
    assert answer.url == "https://cdn.example.test/a.png"


def test_a_non_http_address_is_not_a_url() -> None:
    with pytest.raises(MistralAnswerError):
        parse_image_tool_answer(
            _chat({"role": "assistant", "content": None, "url": "file:///etc/passwd"})
        )


# --------------------------------------------------------- text and usage


def test_the_prose_is_carried_and_trimmed() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": "  Here is your image.  ",
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            }
        )
    )
    assert answer.text == "Here is your image."


def test_the_prose_may_be_a_content_part_list() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Here "},
                    {"type": "text", "text": "it is."},
                ],
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            }
        )
    )
    assert answer.text == "Here it is."


def test_the_usage_block_is_read_in_the_media_rows_spelling() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            },
            usage={"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
        )
    )
    assert answer.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "total_tokens": 46,
        "input_tokens": 12,
        "output_tokens": 34,
    }


def test_a_host_reported_cost_survives_for_the_reported_rung() -> None:
    """Q5 option B is free: the row already reads the host's own ``cost``."""
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            },
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "cost": 0.1,
                "is_byok": False,
            },
        )
    )
    assert answer.usage is not None
    assert answer.usage["cost"] == 0.1
    assert answer.usage["is_byok"] is False


def test_no_usage_block_is_none_not_zero() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            }
        )
    )
    assert answer.usage is None


def test_the_answers_own_created_is_carried() -> None:
    answer = parse_image_tool_answer(
        _chat(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call({"file_id": "abc123def456"})],
            }
        )
    )
    assert answer.created == 1_700_000_000


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("no choices", json.dumps({"id": "x"}).encode()),
        ("empty choices", json.dumps({"choices": []}).encode()),
        ("a message with nothing", _chat({"role": "assistant", "content": None})),
        ("prose only", _chat({"role": "assistant", "content": "I cannot do that."})),
        ("not json", b"<html>502</html>"),
        ("a json list", b"[1, 2, 3]"),
        (
            "a tool call with no arguments",
            _chat(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c", "type": "function"}],
                }
            ),
        ),
    ],
)
def test_an_answer_with_no_image_is_refused(name: str, body: bytes) -> None:
    with pytest.raises(MistralAnswerError):
        parse_image_tool_answer(body)


# ------------------------------------------------------- the client answer


def test_the_client_answer_is_an_openai_images_answer() -> None:
    rendered = json.loads(images_answer(created=1_700_000_000, data=PNG))
    assert rendered == {
        "created": 1_700_000_000,
        "data": [{"b64_json": B64}],
    }


def test_the_client_answer_can_carry_a_url_when_no_bytes_were_read() -> None:
    rendered = json.loads(
        images_answer(created=1, url="https://cdn.example.test/a.png")
    )
    assert rendered["data"] == [{"url": "https://cdn.example.test/a.png"}]
