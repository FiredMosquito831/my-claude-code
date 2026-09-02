"""Chat Completions request fields land where the Anthropic protocol keeps them."""

import json
from unittest.mock import patch

import pytest

from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.openai_chat_completions import (
    ChatCompletionsConversionError,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
)

_ADAPTER = OpenAIChatCompletionsAdapter()


def _convert(payload: dict) -> dict:
    return _ADAPTER.to_anthropic_payload(
        OpenAIChatCompletionRequest.model_validate(payload)
    )


def _minimal(**extra) -> dict:
    return {
        "model": "nvidia_nim/test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        **extra,
    }


def test_a_plain_user_turn_becomes_a_streaming_anthropic_request() -> None:
    payload = _convert(_minimal())

    assert payload["model"] == "nvidia_nim/test-model"
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]
    # The internal pipeline is SSE end to end whatever the client asked for.
    assert payload["stream"] is True
    assert "system" not in payload


def test_system_and_developer_roles_both_become_the_system_prompt() -> None:
    payload = _convert(
        _minimal(
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "developer", "content": "and correct"},
                {"role": "user", "content": "Hello"},
            ]
        )
    )

    assert payload["system"] == "be terse\n\nand correct"
    assert [message["role"] for message in payload["messages"]] == ["user"]


def test_multi_part_content_carries_text_and_both_image_url_forms() -> None:
    payload = _convert(
        _minimal(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aGk="},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/a.png"},
                        },
                    ],
                }
            ]
        )
    )

    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "look"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
        },
        {
            "type": "image",
            "source": {"type": "url", "url": "https://example.test/a.png"},
        },
    ]


def test_a_data_url_that_is_not_base64_is_refused_by_name() -> None:
    with pytest.raises(ChatCompletionsConversionError) as excinfo:
        _convert(
            _minimal(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png,notbase64"},
                            }
                        ],
                    }
                ]
            )
        )

    assert excinfo.value.param == "messages"
    assert "base64 data URLs" in str(excinfo.value)


def test_an_assistant_tool_call_round_trips_into_tool_use_and_tool_result() -> None:
    payload = _convert(
        _minimal(
            messages=[
                {"role": "user", "content": "add"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "I should add",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "add",
                                "arguments": '{"a": 1, "b": 2}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "3"},
                {"role": "tool", "tool_call_id": "call_2", "content": "4"},
            ]
        )
    )

    assistant = payload["messages"][1]
    assert assistant["reasoning_content"] == "I should add"
    assert assistant["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "add", "input": {"a": 1, "b": 2}}
    ]
    # Two OpenAI tool messages, one Anthropic user turn holding both results.
    assert payload["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "3"},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "4"},
        ],
    }


def test_a_tool_message_without_tool_call_id_is_refused() -> None:
    with pytest.raises(ChatCompletionsConversionError) as excinfo:
        _convert(_minimal(messages=[{"role": "tool", "content": "3"}]))

    assert "tool_call_id" in str(excinfo.value)


def test_max_completion_tokens_outranks_max_tokens() -> None:
    assert _convert(_minimal(max_tokens=16, max_completion_tokens=64))[
        "max_tokens"
    ] == (64)
    assert _convert(_minimal(max_tokens=16))["max_tokens"] == 16


def test_sampling_fields_and_stop_sequences_travel() -> None:
    payload = _convert(
        _minimal(temperature=0.25, top_p=0.9, stop=["END", ""], user="u-7")
    )

    assert payload["temperature"] == 0.25
    assert payload["top_p"] == 0.9
    assert payload["stop_sequences"] == ["END"]
    assert payload["metadata"] == {"user_id": "u-7"}


def test_a_bare_string_stop_becomes_a_one_element_sequence() -> None:
    assert _convert(_minimal(stop="END"))["stop_sequences"] == ["END"]


def test_reasoning_effort_is_preserved_as_a_named_output_config() -> None:
    assert _convert(_minimal(reasoning_effort="HIGH"))["output_config"] == {
        "effort": "high"
    }


def test_tools_and_required_tool_choice_convert_to_the_anthropic_shape() -> None:
    payload = _convert(
        _minimal(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "echo it",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            tool_choice="required",
        )
    )

    assert payload["tools"] == [
        {
            "name": "echo",
            "input_schema": {"type": "object", "properties": {}},
            "description": "echo it",
        }
    ]
    assert payload["tool_choice"] == {"type": "any"}


def test_a_named_tool_choice_selects_that_tool() -> None:
    payload = _convert(
        _minimal(
            tools=[{"type": "function", "function": {"name": "echo"}}],
            tool_choice={"type": "function", "function": {"name": "echo"}},
        )
    )

    assert payload["tool_choice"] == {"type": "tool", "name": "echo"}


def test_tool_choice_none_withholds_the_tools_entirely() -> None:
    payload = _convert(
        _minimal(
            tools=[{"type": "function", "function": {"name": "echo"}}],
            tool_choice="none",
        )
    )

    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_parallel_tool_calls_false_disables_parallel_use_even_with_no_choice() -> None:
    payload = _convert(
        _minimal(
            tools=[{"type": "function", "function": {"name": "echo"}}],
            parallel_tool_calls=False,
        )
    )

    assert payload["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }


def test_a_non_function_tool_type_is_refused_by_name() -> None:
    with pytest.raises(ChatCompletionsConversionError) as excinfo:
        _convert(_minimal(tools=[{"type": "web_search_preview"}]))

    assert excinfo.value.param == "tools"
    assert "web_search_preview" in str(excinfo.value)


def test_response_format_json_object_appends_a_json_instruction() -> None:
    payload = _convert(_minimal(response_format={"type": "json_object"}))

    assert "single valid JSON object" in payload["system"]


def test_response_format_json_schema_carries_the_schema_verbatim() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    payload = _convert(
        _minimal(
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema},
            }
        )
    )

    assert "named 'answer'" in payload["system"]
    assert json.dumps(schema, sort_keys=True) in payload["system"]


def test_an_unknown_response_format_type_is_refused_by_name() -> None:
    with pytest.raises(ChatCompletionsConversionError) as excinfo:
        _convert(_minimal(response_format={"type": "xml"}))

    assert excinfo.value.param == "response_format"
    assert "xml" in str(excinfo.value)


def test_more_than_one_choice_is_refused_rather_than_quietly_served_as_one() -> None:
    with pytest.raises(ChatCompletionsConversionError) as excinfo:
        _convert(_minimal(n=2))

    assert excinfo.value.param == "n"


def test_unhonourable_sampling_fields_are_dropped_and_recorded() -> None:
    with patch(
        "my_claude_code.core.openai_chat_completions.input.trace_event"
    ) as trace_event:
        payload = _convert(
            _minimal(
                seed=7,
                logprobs=True,
                top_logprobs=3,
                frequency_penalty=0.5,
                presence_penalty=0.5,
            )
        )

    assert set(payload) == {"model", "messages", "stream"}
    assert trace_event.call_args.kwargs["fields"] == [
        "frequency_penalty",
        "logprobs",
        "presence_penalty",
        "seed",
        "top_logprobs",
    ]


def test_an_unknown_role_is_refused_by_name() -> None:
    with pytest.raises(ChatCompletionsConversionError) as excinfo:
        _convert(_minimal(messages=[{"role": "narrator", "content": "hi"}]))

    assert "narrator" in str(excinfo.value)


def test_the_converted_payload_is_a_valid_messages_request() -> None:
    request = MessagesRequest(
        **_convert(
            _minimal(
                messages=[
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "Hello"},
                ],
                max_completion_tokens=32,
            )
        )
    )

    assert request.system == "be terse"
    assert request.max_tokens == 32
    assert request.stream is True
