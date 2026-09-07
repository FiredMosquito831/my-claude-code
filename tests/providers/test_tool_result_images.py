"""An image a tool returned must reach the model as an image.

Before 6.49.0 a nested ``image`` block was JSON-dumped into the ``role: tool``
message, so a 213 KB screenshot cost ~324,000 prompt tokens and the model never
saw a picture. These tests pin every part of the fix that a future refactor
could quietly undo: the two-stage split, the hoist's exact message sequence, the
estimator's parity with a pasted image, and the strip for a model published as
blind.
"""

import json

import pytest

from my_claude_code.core.anthropic import (
    AnthropicToOpenAIConverter,
    build_base_request_body,
    is_synthetic_openai_tool_turn_boundary,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.request_modalities import request_image_inputs
from my_claude_code.core.anthropic.tokens import get_token_count
from my_claude_code.core.anthropic.tool_result_media import (
    HOISTED_IMAGE_BOUNDARY_TEXT,
    TOOL_DOCUMENT_STRIPPED_TEXT,
    TOOL_IMAGE_ATTACHED_TEXT,
    TOOL_IMAGE_STRIPPED_TEXT,
    MediaDelivery,
    collect_tool_names,
    media_delivery,
    replace_request_media,
)

# Long enough that the old flattening would be unmistakable in any assertion.
BIG_B64 = "iVBORw0KGgoAAAANSUhEUg" + "QUJDRUZH" * 4000


def _image_block(data: str = BIG_B64) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def _document_block() -> dict:
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": "JVBERi0xLjQK",
        },
    }


def _request(tool_result_content, *, tool_name: str = "take_screenshot") -> dict:
    return {
        "model": "some/model",
        "max_tokens": 256,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look at this"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": tool_name,
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": tool_result_content,
                    }
                ],
            },
        ],
    }


def _body(payload: dict) -> dict:
    return build_base_request_body(MessagesRequest.model_validate(payload))


# --------------------------------------------------------------------------
# Stage 1 -- the converter carries the image
# --------------------------------------------------------------------------


def test_stage_one_leaves_the_image_inside_the_tool_message():
    """The two stages are genuinely separable, not one function pretending."""
    request = MessagesRequest.model_validate(
        _request([{"type": "text", "text": "ok"}, _image_block()])
    )

    messages = AnthropicToOpenAIConverter.convert_messages(request.messages)

    tool_message = next(m for m in messages if m["role"] == "tool")
    assert isinstance(tool_message["content"], list)
    assert tool_message["content"][0] == {
        "type": "text",
        "text": f"ok\n{TOOL_IMAGE_ATTACHED_TEXT}",
    }
    assert tool_message["content"][1]["type"] == "image_url"
    assert tool_message["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    # Stage 1 alone inserts no user message: the hoist is stage 2's job.
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]


def test_tool_result_without_media_is_byte_for_byte_unchanged():
    """The no-regression guard for the 99.9% of requests that carry no image."""
    payload = _request([{"type": "text", "text": "plain output"}])

    messages = AnthropicToOpenAIConverter.convert_messages(
        MessagesRequest.model_validate(payload).messages
    )

    assert messages[-1] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "plain output",
    }
    assert _body(payload)["messages"][-1] == messages[-1]


# --------------------------------------------------------------------------
# Stage 2 -- the hoist
# --------------------------------------------------------------------------


def test_tool_result_image_is_hoisted_into_a_following_user_message():
    body = _body(_request([{"type": "text", "text": "ok"}, _image_block()]))

    messages = body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    tool_message = messages[2]
    assert tool_message["tool_call_id"] == "toolu_1"
    assert tool_message["content"] == f"ok\n{TOOL_IMAGE_ATTACHED_TEXT}"
    hoisted = messages[3]
    assert hoisted["content"][0] == {
        "type": "text",
        "text": HOISTED_IMAGE_BOUNDARY_TEXT,
    }
    assert hoisted["content"][1]["image_url"]["url"] == (
        f"data:image/png;base64,{BIG_B64}"
    )
    # The hoisted message belongs to the tool run, so it must NOT acquire the
    # synthetic assistant boundary a genuine following user turn gets.
    assert not any(is_synthetic_openai_tool_turn_boundary(m) for m in messages)
    # And no base64 may survive anywhere in a tool message.
    assert not any(
        m["role"] == "tool" and BIG_B64 in json.dumps(m["content"]) for m in messages
    )


def test_two_tool_results_with_images_share_one_hoisted_message():
    payload = {
        "model": "some/model",
        "max_tokens": 256,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "two shots"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "shot", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "shot", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [_image_block("AAAA1111")],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [_image_block("BBBB2222")],
                    },
                ],
            },
        ],
    }

    messages = _body(payload)["messages"]

    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert [m["tool_call_id"] for m in messages[2:4]] == ["t1", "t2"]
    hoisted = messages[4]["content"]
    assert hoisted[0]["text"] == HOISTED_IMAGE_BOUNDARY_TEXT
    assert [part["image_url"]["url"] for part in hoisted[1:]] == [
        "data:image/png;base64,AAAA1111",
        "data:image/png;base64,BBBB2222",
    ]


def test_image_only_tool_result_never_produces_empty_tool_content():
    """An empty ``role: tool`` content is rejected outright by several hosts."""
    messages = _body(_request([_image_block()]))["messages"]

    assert messages[2]["content"] == TOOL_IMAGE_ATTACHED_TEXT


def test_document_in_a_tool_result_is_never_hoisted():
    messages = _body(
        _request([{"type": "text", "text": "the pdf"}, _document_block()])
    )["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[2]["content"] == f"the pdf\n{TOOL_DOCUMENT_STRIPPED_TEXT}"


def test_repeated_placeholders_within_one_tool_result_are_collapsed():
    messages = _body(_request([_document_block(), _document_block()]))["messages"]

    assert messages[2]["content"] == TOOL_DOCUMENT_STRIPPED_TEXT


# --------------------------------------------------------------------------
# The strip, made in the router's layer
# --------------------------------------------------------------------------


def test_media_delivery_treats_only_a_published_no_as_no():
    assert media_delivery(False) is MediaDelivery.STRIP
    assert media_delivery(True) is MediaDelivery.ATTACH
    assert media_delivery(None) is MediaDelivery.ATTACH


def test_blind_model_strips_the_tool_result_image_with_a_placeholder():
    request = MessagesRequest.model_validate(
        _request([{"type": "text", "text": "ok"}, _image_block()])
    )

    replaced = replace_request_media(
        request.messages, tool_names=collect_tool_names(request.messages)
    )

    assert replaced == 1
    body = build_base_request_body(request)
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool"]
    assert "take_screenshot" in body["messages"][2]["content"]
    assert "does not accept images" in body["messages"][2]["content"]
    assert BIG_B64 not in json.dumps(body["messages"])


def test_stripping_an_image_only_tool_result_leaves_the_placeholder_alone():
    request = MessagesRequest.model_validate(_request([_image_block()]))

    replace_request_media(request.messages)

    body = build_base_request_body(request)
    assert body["messages"][2]["content"] == TOOL_IMAGE_STRIPPED_TEXT


def test_stripping_replaces_a_top_level_image_too():
    request = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see"},
                        _image_block(),
                    ],
                }
            ],
        }
    )

    assert replace_request_media(request.messages) == 1
    body = build_base_request_body(request)
    assert body["messages"][0]["content"] == (
        "see\n[image omitted: this model does not accept images]"
    )


def test_unknown_vision_capability_still_sends_the_image():
    """Silence is not a refusal -- most providers publish nothing at all."""
    assert media_delivery(None) is MediaDelivery.ATTACH
    messages = _body(_request([_image_block()]))["messages"]
    assert any(
        part.get("type") == "image_url"
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )


# --------------------------------------------------------------------------
# The estimator, and its agreement with routing
# --------------------------------------------------------------------------


def test_nested_image_costs_the_same_as_a_pasted_one():
    """The test that would have caught 99 tokens versus 204,216."""
    pasted = MessagesRequest.model_validate(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": [_image_block()]}],
        }
    )
    nested = MessagesRequest.model_validate(_request([_image_block()]))

    pasted_tokens = get_token_count(pasted.messages)
    nested_tokens = get_token_count(nested.messages)

    # Within the per-block framing overhead of each other, not 2000x apart.
    assert abs(nested_tokens - pasted_tokens) < 200
    assert nested_tokens < 500


def test_the_hoisted_image_is_the_same_image_modalities_found():
    """Routing and conversion must agree, block for block and byte for byte."""
    request = MessagesRequest.model_validate(_request([_image_block()]))

    found = request_image_inputs(request)
    body = build_base_request_body(request)
    urls = [
        part["image_url"]["url"]
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]

    assert len(found) == len(urls) == 1
    assert urls[0].endswith(found[0].data or "")


@pytest.mark.parametrize("content", ["plain", [{"type": "text", "text": "plain"}]])
def test_a_text_only_tool_result_is_invisible_to_the_media_walk(content):
    request = MessagesRequest.model_validate(_request(content))

    assert request_image_inputs(request) == ()
    assert replace_request_media(request.messages) == 0


# --------------------------------------------------------------------------
# The per-request `detail` knob
# --------------------------------------------------------------------------


def test_detail_absent_by_default():
    """No `detail` key unless the operator asked for one.

    OpenAI applies `auto` when the field is absent, so emitting it would change
    nothing on the wire while making every body larger; every other dialect
    ignores or rejects it. This is the unchanged-behaviour guard.
    """
    body = _body(_request([{"type": "text", "text": "ok"}, _image_block()]))
    dumped = json.dumps(body)

    assert '"detail"' not in dumped


def test_detail_auto_still_emits_nothing():
    body = build_base_request_body(
        MessagesRequest.model_validate(_request([_image_block()])),
        image_detail="auto",
    )

    assert '"detail"' not in json.dumps(body)


def test_detail_low_reaches_every_image_part():
    """Both the hoisted tool image and a pasted one carry it."""
    payload = _request([_image_block()])
    payload["messages"][0]["content"].append(_image_block())
    body = build_base_request_body(
        MessagesRequest.model_validate(payload), image_detail="low"
    )

    parts = [
        part
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    assert len(parts) == 2
    assert all(part["image_url"]["detail"] == "low" for part in parts)


def test_detail_never_changes_the_token_estimate():
    """The estimate is a function of pixels and family, never of `detail`.

    LiteLLM keys its count on `detail`, which is why every Anthropic image
    costs it a flat 85 tokens: Anthropic blocks carry no such field, so the
    `auto` branch always wins. This is the guard against copying that.
    """
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1920, 1080), "red").save(buffer, format="PNG")
    real = base64.b64encode(buffer.getvalue()).decode("ascii")
    request = MessagesRequest.model_validate(_request([_image_block(real)]))

    # Two families, two different numbers, both a function of the picture's
    # real dimensions -- and neither reachable from a `detail` value, because
    # `get_token_count` has no such parameter to pass.
    anthropic = get_token_count(request.messages, image_token_family="anthropic")
    tiled = get_token_count(request.messages, image_token_family="openai_tile")
    assert anthropic != tiled
    assert "detail" not in get_token_count.__code__.co_varnames
