from typing import Any

import pytest

from my_claude_code.core.openai_chat_completions import (
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
)
from my_claude_code.core.openai_responses import (
    OpenAIResponsesAdapter,
    OpenAIResponsesRequest,
)

_ADAPTER = OpenAIResponsesAdapter()
_CONVERSION_ERROR = OpenAIResponsesAdapter.ConversionError


def _to_anthropic_payload(request: dict[str, Any]) -> dict[str, Any]:
    return _ADAPTER.to_anthropic_payload(OpenAIResponsesRequest.model_validate(request))


def test_responses_string_input_converts_to_anthropic_message() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "instructions": "System instructions",
            "input": "Hello",
            "max_output_tokens": 64,
            "temperature": 0.2,
            "top_p": 0.9,
            "metadata": {"trace": "abc"},
        }
    )

    assert payload["model"] == "nvidia_nim/test-model"
    assert payload["system"] == "System instructions"
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]
    assert payload["max_tokens"] == 64
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9
    assert payload["metadata"] == {"trace": "abc"}


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh"])
def test_responses_reasoning_effort_is_preserved_for_application_policy(
    effort: str,
) -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": "Hello",
            "reasoning": {"effort": effort},
        }
    )

    assert payload["output_config"] == {"effort": effort}
    assert "thinking" not in payload


def test_responses_messages_tools_and_tool_results_convert() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "deepseek/deepseek-chat",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Developer rules"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Use the tool"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"value":"FCC"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "FCC",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "Echo a value",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "name": "echo"},
        }
    )

    assert payload["system"] == "Developer rules"
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Use the tool"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "echo",
                    "input": {"value": "FCC"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "FCC",
                }
            ],
        },
    ]
    assert payload["tools"] == [
        {
            "name": "echo",
            "description": "Echo a value",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "tool", "name": "echo"}


def test_responses_tool_choice_none_disables_forwarded_tools() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "deepseek/deepseek-chat",
            "input": "Reply without tools",
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": "none",
        }
    )

    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_responses_namespace_tools_flatten_for_anthropic() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": "Use JS",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__node_repl",
                    "description": "Node tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "js",
                            "description": "Run JavaScript",
                            "parameters": {
                                "type": "object",
                                "properties": {"code": {"type": "string"}},
                                "required": ["code"],
                            },
                        }
                    ],
                }
            ],
            "tool_choice": {
                "type": "function",
                "namespace": "mcp__node_repl",
                "name": "js",
            },
        }
    )

    assert payload["tools"] == [
        {
            "name": "mcp__node_repl__js",
            "description": "Run JavaScript",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "tool", "name": "mcp__node_repl__js"}


def test_responses_namespaced_tool_choice_type_tool_flattens_for_anthropic() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": "Use JS",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__node_repl",
                    "tools": [
                        {
                            "type": "function",
                            "name": "js",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ],
            "tool_choice": {
                "type": "tool",
                "namespace": "mcp__node_repl",
                "name": "js",
            },
        }
    )

    assert payload["tool_choice"] == {"type": "tool", "name": "mcp__node_repl__js"}


def test_responses_custom_tool_converts_to_anthropic_string_tool() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": "Use apply_patch",
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Apply a repo patch",
                    "format": {
                        "type": "grammar",
                        "syntax": "lark",
                        "definition": "start: /.+/",
                    },
                }
            ],
            "tool_choice": {"type": "custom", "name": "apply_patch"},
        }
    )

    assert payload["tools"] == [
        {
            "name": "apply_patch",
            "description": (
                "Apply a repo patch\n\n"
                "Custom tool input format: grammar (lark): start: /.+/"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Free-form input for the custom tool.",
                    }
                },
                "required": ["input"],
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "tool", "name": "apply_patch"}


def test_responses_namespaced_custom_tool_flattens_for_anthropic() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": "Use shell",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__shell",
                    "tools": [
                        {
                            "type": "custom",
                            "name": "exec",
                            "description": "Run shell text",
                            "format": {"type": "text"},
                        }
                    ],
                }
            ],
            "tool_choice": {
                "type": "custom",
                "namespace": "mcp__shell",
                "custom": {"name": "exec"},
            },
        }
    )

    assert payload["tools"][0]["name"] == "mcp__shell__exec"
    assert payload["tools"][0]["description"] == (
        "Run shell text\n\nCustom tool input format: unconstrained text."
    )
    assert payload["tool_choice"] == {"type": "tool", "name": "mcp__shell__exec"}


def test_responses_passive_codex_built_in_tools_are_ignored() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": "Hello",
            "tools": [
                {"type": "web_search", "external_web_access": True},
                {"type": "image_generation", "output_format": "png"},
                {"type": "tool_search"},
                {
                    "type": "function",
                    "name": "echo",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        }
    )

    assert payload["tools"] == [
        {"name": "echo", "input_schema": {"type": "object", "properties": {}}}
    ]


def test_responses_namespaced_prior_function_call_flattens_tool_use_name() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "namespace": "mcp__node_repl",
                    "name": "js",
                    "arguments": '{"code":"1+1"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "2",
                },
            ],
        }
    )

    assert payload["messages"][0]["content"][0]["name"] == "mcp__node_repl__js"


def test_responses_prior_custom_tool_call_flattens_tool_use_name() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "namespace": "mcp__shell",
                    "name": "exec",
                    "input": "printf FCC",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_1",
                    "output": "FCC",
                },
            ],
        }
    )

    assert payload["messages"] == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "mcp__shell__exec",
                    "input": {"input": "printf FCC"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "FCC",
                }
            ],
        },
    ]


def test_responses_groups_consecutive_prior_tool_calls() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"value":"FCC"}',
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "call_2",
                    "name": "apply_patch",
                    "input": "*** Begin Patch",
                },
            ],
        }
    )

    assert payload["messages"] == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "echo",
                    "input": {"value": "FCC"},
                },
                {
                    "type": "tool_use",
                    "id": "call_2",
                    "name": "apply_patch",
                    "input": {"input": "*** Begin Patch"},
                },
            ],
        }
    ]


def test_responses_groups_consecutive_prior_tool_outputs() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"value":"FCC"}',
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "echo",
                    "arguments": '{"value":"Codex"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "FCC",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_2",
                    "output": "Codex",
                },
            ],
        }
    )

    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "assistant"
    assert len(payload["messages"][0]["content"]) == 2
    assert payload["messages"][1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "FCC",
            },
            {
                "type": "tool_result",
                "tool_use_id": "call_2",
                "content": "Codex",
            },
        ],
    }


def test_responses_reasoning_between_tool_call_and_output_attaches_to_tool_message() -> (
    None
):
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"value":"FCC"}',
                },
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "Need the result."}],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "FCC",
                },
            ],
        }
    )

    assert payload["messages"][0]["reasoning_content"] == "Need the result."
    assert payload["messages"][0]["content"][0]["id"] == "call_1"
    assert payload["messages"][1]["content"][0]["tool_use_id"] == "call_1"


def test_responses_empty_reasoning_attaches_to_prior_tool_call() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"value":"FCC"}',
                },
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": ""}],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "FCC",
                },
            ],
        }
    )

    assert payload["messages"][0]["reasoning_content"] == ""


def test_responses_unsupported_tool_type_is_clear() -> None:
    with pytest.raises(_CONVERSION_ERROR, match="Unsupported Responses tool type"):
        _to_anthropic_payload(
            {
                "model": "nvidia_nim/test-model",
                "input": "Hello",
                "tools": [{"type": "web_search_preview"}],
            }
        )


def test_responses_malformed_prior_function_call_is_quarantined() -> None:
    payload = _to_anthropic_payload(
        {
            "model": "nvidia_nim/test-model",
            "input": [
                {"role": "user", "content": "hello"},
                {
                    "type": "function_call",
                    "call_id": "call_bad",
                    "name": "echo",
                    "arguments": "{",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_bad",
                    "output": "stale output",
                },
                {
                    "type": "function_call",
                    "call_id": "call_good",
                    "name": "echo",
                    "arguments": '{"value":"ok"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_good",
                    "output": "ok",
                },
                {"role": "user", "content": "continue"},
            ],
        }
    )

    assert payload["messages"] == [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_good",
                    "name": "echo",
                    "input": {"value": "ok"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_good",
                    "content": "ok",
                }
            ],
        },
        {"role": "user", "content": "continue"},
    ]


def test_responses_malformed_only_function_call_still_has_no_routable_message() -> None:
    with pytest.raises(_CONVERSION_ERROR, match="must contain a message"):
        _to_anthropic_payload(
            {
                "model": "nvidia_nim/test-model",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_bad",
                        "name": "echo",
                        "arguments": "{",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_bad",
                        "output": "stale output",
                    },
                ],
            }
        )


def test_non_string_function_call_output_preserves_text_and_images():
    """Responses tool outputs preserve supported media parts alongside text."""
    payload = _to_anthropic_payload(
        {
            "model": "gpt-5",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": [
                        {"type": "output_text", "text": "first"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AQ==",
                        },
                    ],
                },
            ],
        }
    )

    tool_result = payload["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"] == [
        {"type": "text", "text": "first"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AQ==",
            },
        },
    ]


# ------------------------------------------------- 7.95.0: images attached in Codex

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _codex_turn(*parts: dict[str, Any]) -> dict[str, Any]:
    """A user message as Codex sends one with an attached image (``-i``)."""
    return {
        "model": "gpt-5.5",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<image name=[Image #1]>"},
                    *parts,
                    {"type": "input_text", "text": "</image>"},
                    {"type": "input_text", "text": "what is in this picture?"},
                ],
            }
        ],
    }


def test_user_input_image_becomes_an_image_block() -> None:
    """R1 (user decision 15): a data URL image is the Chat door's base64 block."""
    payload = _to_anthropic_payload(
        _codex_turn(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{_PNG_B64}",
                "detail": "auto",
            }
        )
    )

    (message,) = payload["messages"]
    assert message["role"] == "user"
    assert message["content"] == [
        {"type": "text", "text": "<image name=[Image #1]>"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
        },
        {"type": "text", "text": "</image>"},
        {"type": "text", "text": "what is in this picture?"},
    ]


def test_user_input_image_url_source() -> None:
    payload = _to_anthropic_payload(
        _codex_turn(
            {"type": "input_image", "image_url": "https://example.test/cat.png"}
        )
    )

    blocks = payload["messages"][0]["content"]
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.test/cat.png"},
    }


def test_the_block_is_byte_for_byte_the_chat_doors() -> None:
    """Equality: the same picture through both OpenAI doors, the same block."""
    for url in (f"data:image/jpeg;base64,{_PNG_B64}", "https://example.test/a.png"):
        responses = _to_anthropic_payload(
            {
                "model": "m",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": url}],
                    }
                ],
            }
        )
        chat = OpenAIChatCompletionsAdapter().to_anthropic_payload(
            OpenAIChatCompletionRequest.model_validate(
                {
                    "model": "m",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": url}}
                            ],
                        }
                    ],
                }
            )
        )
        assert responses["messages"][0]["content"] == chat["messages"][0]["content"]


@pytest.mark.parametrize(
    ("part", "said"),
    [
        (
            {"type": "input_image", "file_id": "file-abc123"},
            "input_image file_id 'file-abc123' is not supported",
        ),
        ({"type": "input_image"}, "input_image needs image_url"),
        (
            {"type": "input_image", "image_url": "file:///C:/cat.png"},
            "must be a base64 data URL or an http(s) URL",
        ),
        (
            {"type": "input_image", "image_url": "data:image/png;base64,!!!"},
            "not valid base64",
        ),
    ],
)
def test_input_image_file_id_is_a_clear_400(part: dict[str, Any], said: str) -> None:
    with pytest.raises(_CONVERSION_ERROR) as raised:
        _to_anthropic_payload(_codex_turn(part))
    assert said in str(raised.value)


@pytest.mark.parametrize("role", ["assistant", "system", "developer"])
def test_an_image_outside_a_user_turn_is_still_refused(role: str) -> None:
    """Only a user turn can carry a picture; the old error is unchanged."""
    with pytest.raises(_CONVERSION_ERROR) as raised:
        _to_anthropic_payload(
            {
                "model": "m",
                "input": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": role,
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": f"data:image/png;base64,{_PNG_B64}",
                            }
                        ],
                    },
                ],
            }
        )
    assert str(raised.value) == "Unsupported Responses content part type: 'input_image'"


@pytest.mark.parametrize(
    "tool_type",
    [
        "web_search",
        "web_search_2025_08_26",
        "web_search_preview",
        "web_search_preview_2025_03_11",
    ],
)
def test_opt_in_web_search_tools_preserve_client_tool_prefix_and_spec(
    tool_type: str,
) -> None:
    from my_claude_code.core.openai_responses import (
        OpenAIResponsesAdapter,
        OpenAIResponsesRequest,
        extract_responses_web_search_spec,
    )

    request_dict: dict[str, Any] = {
        "model": "m",
        "input": [
            {
                "type": "web_search_call",
                "id": "ws_prev",
                "status": "completed",
                "action": {"type": "search", "query": "python 3.14"},
            },
            {"role": "user", "content": "Follow up"},
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "type": tool_type,
                "filters": {"allowed_domains": ["docs.python.org", "python.org"]},
                "external_web_access": False,
                "search_context_size": "low",
                "user_location": {"type": "approximate", "country": "US"},
            },
        ],
        "tool_choice": {"type": tool_type},
    }
    req = OpenAIResponsesRequest(**request_dict)
    adapter = OpenAIResponsesAdapter()

    # Pure converter default stays untouched when allow_web_search=False.
    with pytest.raises(_CONVERSION_ERROR):
        adapter.to_anthropic_payload(req)

    payload = adapter.to_anthropic_payload(req, allow_web_search=True)
    assert [t["name"] for t in payload["tools"]] == ["get_weather"]
    assert payload["tool_choice"] == {"type": "web_search"}
    assert "python 3.14" in str(payload["messages"][0])

    spec = extract_responses_web_search_spec(
        req.tools, req.tool_choice, allow_web_search=True
    )
    assert spec is not None
    assert spec.allowed_domains == ("docs.python.org", "python.org")
    assert spec.external_web_access is False
    assert spec.search_context_size == "low"
    assert spec.forced is True


def test_opt_in_web_search_rejects_bad_schema_or_undeclared_tool_choice() -> None:
    from my_claude_code.core.openai_responses import (
        OpenAIResponsesAdapter,
        OpenAIResponsesRequest,
    )

    adapter = OpenAIResponsesAdapter()
    with pytest.raises(_CONVERSION_ERROR, match="Expected Responses web_search tool"):
        adapter.to_anthropic_payload(
            OpenAIResponsesRequest(
                model="m",
                input="q",
                tools=[{"type": "web_search", "search_context_size": "huge"}],
            ),
            allow_web_search=True,
        )

    with pytest.raises(_CONVERSION_ERROR, match="must be declared in tools"):
        adapter.to_anthropic_payload(
            OpenAIResponsesRequest(
                model="m",
                input="q",
                tools=[],
                tool_choice={"type": "web_search"},
            ),
            allow_web_search=True,
        )


def test_image_generation_call_and_web_search_call_replay_without_base64_bloat() -> (
    None
):
    from my_claude_code.core.openai_responses import (
        OpenAIResponsesAdapter,
        OpenAIResponsesRequest,
    )

    giant_b64 = "A" * 4096
    req = OpenAIResponsesRequest(
        model="gpt-5.6-terra",
        input=[
            {
                "type": "reasoning",
                "id": "rs_img",
                "summary": [{"type": "summary_text", "text": "Planning artwork"}],
                "encrypted_content": "enc_img_1",
            },
            {
                "type": "image_generation_call",
                "id": "ig_1",
                "status": "completed",
                "output_format": "png",
                "revised_prompt": "A watercolor lighthouse at dusk",
                "result": giant_b64,
            },
            {
                "type": "image_generation_call",
                "id": "ig_2",
                "status": "completed",
                "result": giant_b64,
            },
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "lighthouse architecture"},
            },
            {"role": "user", "content": "Make the sky warmer"},
        ],
        tools=[
            {"type": "image_generation", "output_format": "png", "quality": "high"},
            {"type": "web_search", "external_web_access": True},
        ],
    )
    adapter = OpenAIResponsesAdapter()

    # Default allow_web_search=False / allow_image_generation=False still replays cleanly.
    payload_default = adapter.to_anthropic_payload(req)
    assert "tools" not in payload_default
    assert payload_default["messages"][0] == {
        "role": "assistant",
        "content": "[Prior image_generation_call: A watercolor lighthouse at dusk]",
        "reasoning_content": "Planning artwork",
        "encrypted_reasoning": [
            {
                "type": "reasoning",
                "id": "rs_img",
                "summary": [{"type": "summary_text", "text": "Planning artwork"}],
                "encrypted_content": "enc_img_1",
            }
        ],
    }
    assert payload_default["messages"][1] == {
        "role": "assistant",
        "content": "[Prior image_generation_call]",
    }
    assert payload_default["messages"][2] == {
        "role": "assistant",
        "content": "[Prior web_search_call: search: lighthouse architecture]",
    }
    assert giant_b64 not in str(payload_default["messages"])

    envelope = payload_default["responses_envelope"]
    assert [item["type"] for item in envelope["input"]] == [
        "reasoning",
        "image_generation_call",
        "image_generation_call",
        "web_search_call",
        "message",
    ]
    assert envelope["input"][1]["result"] == giant_b64
    assert [tool["type"] for tool in envelope["tools"]] == [
        "image_generation",
        "web_search",
    ]


def test_opt_in_allow_image_generation_validates_without_emitting_function_tool() -> (
    None
):
    from my_claude_code.core.openai_responses import (
        OpenAIResponsesAdapter,
        OpenAIResponsesRequest,
    )

    adapter = OpenAIResponsesAdapter()
    req = OpenAIResponsesRequest(
        model="gpt-5.6-terra",
        input="Draw a cat",
        tools=[
            {
                "type": "image_generation",
                "output_format": "png",
                "size": "1024x1024",
                "quality": "high",
                "partial_images": 2,
            },
            {
                "type": "function",
                "name": "ping",
                "parameters": {"type": "object", "properties": {}},
            },
        ],
        tool_choice={"type": "image_generation"},
    )

    with pytest.raises(_CONVERSION_ERROR):
        adapter.to_anthropic_payload(req)

    payload = adapter.to_anthropic_payload(req, allow_image_generation=True)
    assert [t["name"] for t in payload["tools"]] == ["ping"]
    assert payload["tool_choice"] == {"type": "image_generation"}

    with pytest.raises(_CONVERSION_ERROR, match="output_format"):
        adapter.to_anthropic_payload(
            OpenAIResponsesRequest(
                model="gpt-5.6-terra",
                input="Draw",
                tools=[{"type": "image_generation", "output_format": 123}],
            ),
            allow_image_generation=True,
        )

    with pytest.raises(_CONVERSION_ERROR, match="partial_images"):
        adapter.to_anthropic_payload(
            OpenAIResponsesRequest(
                model="gpt-5.6-terra",
                input="Draw",
                tools=[{"type": "image_generation", "partial_images": -1}],
            ),
            allow_image_generation=True,
        )
