"""Gemini request/response translation, field by field.

The wire facts pinned here were read out of Gemini CLI 0.49.0's own bundle and
Google's published ``GenerateContentRequest``/``GenerateContentResponse``
schemas, not inferred: the point of this file is that a change to the adapter
that would break a real client fails here rather than in someone's terminal.
"""

import json

import pytest

from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.gemini_api import (
    GeminiConversionError,
    GeminiGenerateContentRequest,
    build_gemini_payload_from_loop,
    extract_gemini_web_search_spec,
    gemini_error_payload,
    gemini_failure_payload,
    gemini_model_entry,
    gemini_models_payload,
    gemini_status_for_failure,
    iter_gemini_sse_from_loop,
    model_resource_name,
    parse_model_method_path,
    strip_models_prefix,
)
from my_claude_code.core.gemini_api.assembler import GeminiStreamAssembler
from my_claude_code.core.gemini_api.events import finish_reason_for
from my_claude_code.core.gemini_api.input import (
    convert_request_to_anthropic_payload,
)
from my_claude_code.core.gemini_api.response import (
    generate_content_response_from_anthropic_message,
)
from my_claude_code.core.openai_common import AnthropicSseEvent


def _request(**body) -> GeminiGenerateContentRequest:
    return GeminiGenerateContentRequest.model_validate(body).with_model(
        body.pop("_model", "openrouter/qwen/qwen3-coder")
    )


def _convert(**body):
    return convert_request_to_anthropic_payload(_request(**body))


# ---------------------------------------------------------------- path parsing


@pytest.mark.parametrize(
    ("tail", "model", "method"),
    [
        ("models/gemini-3-pro:generateContent", "gemini-3-pro", "generateContent"),
        (
            "anthropic/openrouter/gpt-5:streamGenerateContent",
            "anthropic/openrouter/gpt-5",
            "streamGenerateContent",
        ),
        (
            "models/anthropic/openrouter/qwen/qwen3-coder:countTokens",
            "anthropic/openrouter/qwen/qwen3-coder",
            "countTokens",
        ),
    ],
)
def test_a_model_path_splits_on_the_last_colon(tail, model, method) -> None:
    """MCC ids carry slashes and Google puts the method after a colon."""

    parsed = parse_model_method_path(tail)

    assert parsed is not None
    assert parsed.model == model
    assert parsed.method == method


def test_a_path_with_no_method_is_not_a_generation_call() -> None:
    assert parse_model_method_path("models/gemini-3-pro") is None
    assert parse_model_method_path("") is None


def test_the_models_collection_prefix_round_trips() -> None:
    assert strip_models_prefix("models/anthropic/openrouter/gpt-5") == (
        "anthropic/openrouter/gpt-5"
    )
    assert model_resource_name("anthropic/openrouter/gpt-5") == (
        "models/anthropic/openrouter/gpt-5"
    )
    assert model_resource_name("models/gemini-3-pro") == "models/gemini-3-pro"


# ------------------------------------------------------------ request contents


def test_contents_become_anthropic_messages_with_model_renamed_assistant() -> None:
    payload = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "hello"}]},
            {"role": "user", "parts": [{"text": "again"}]},
        ]
    ).payload

    assert payload["stream"] is True
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert payload["messages"][1]["content"] == [{"type": "text", "text": "hello"}]


def test_a_bare_string_contents_is_one_user_turn() -> None:
    payload = _convert(contents="hello").payload

    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    ]


def test_a_system_instruction_becomes_the_anthropic_system_prompt() -> None:
    payload = _convert(
        systemInstruction={"parts": [{"text": "be terse"}, {"text": "and kind"}]},
        contents=[{"role": "user", "parts": [{"text": "hi"}]}],
    ).payload

    assert payload["system"] == "be terse\n\nand kind"


def test_inline_image_data_becomes_a_base64_image_block() -> None:
    payload = _convert(
        contents=[
            {
                "role": "user",
                "parts": [
                    {"text": "what is this"},
                    {"inlineData": {"mimeType": "image/png", "data": "aGk="}},
                ],
            }
        ]
    ).payload

    assert payload["messages"][0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
    }


def test_an_inline_pdf_becomes_a_document_block() -> None:
    payload = _convert(
        contents=[
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "application/pdf", "data": "aGk="}}
                ],
            }
        ]
    ).payload

    assert payload["messages"][0]["content"][0]["type"] == "document"


def test_inline_data_that_is_not_base64_is_a_named_400() -> None:
    with pytest.raises(GeminiConversionError) as excinfo:
        _convert(
            contents=[
                {
                    "role": "user",
                    "parts": [{"inlineData": {"mimeType": "image/png", "data": "!!!"}}],
                }
            ]
        )

    assert excinfo.value.field == "contents"
    assert "base64" in str(excinfo.value)


def test_a_function_call_and_its_response_pair_up_by_name() -> None:
    """Gemini's parts carry no id and Anthropic pairs a result to a call by id."""

    payload = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "read it"}]},
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "read_file", "args": {"p": "a"}}}],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "read_file",
                            "response": {"output": "contents"},
                        }
                    }
                ],
            },
        ]
    ).payload

    call = payload["messages"][1]["content"][0]
    result = payload["messages"][2]["content"][0]
    assert call == {
        "type": "tool_use",
        "id": "gemini_call_0",
        "name": "read_file",
        "input": {"p": "a"},
    }
    assert result == {
        "type": "tool_result",
        "tool_use_id": "gemini_call_0",
        "content": "contents",
    }


def test_two_function_responses_fold_into_one_user_turn() -> None:
    payload = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "go"}]},
            {
                "role": "model",
                "parts": [
                    {"functionCall": {"name": "a", "args": {}}},
                    {"functionCall": {"name": "b", "args": {}}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {"functionResponse": {"name": "a", "response": {"output": "1"}}},
                    {"functionResponse": {"name": "b", "response": {"output": "2"}}},
                ],
            },
        ]
    ).payload

    results = payload["messages"][2]["content"]
    assert [block["tool_use_id"] for block in results] == [
        "gemini_call_0",
        "gemini_call_1",
    ]


def test_a_function_response_with_no_matching_call_is_refused() -> None:
    with pytest.raises(GeminiConversionError) as excinfo:
        _convert(
            contents=[
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": "ghost", "response": {}}}],
                }
            ]
        )

    assert "answers no functionCall" in str(excinfo.value)


def test_an_echoed_thought_part_is_dropped_rather_than_replayed() -> None:
    """Anthropic rejects a thinking block with no signature."""

    payload = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "hi"}]},
            {
                "role": "model",
                "parts": [
                    {"text": "pondering", "thought": True},
                    {"text": "answer"},
                ],
            },
        ]
    ).payload

    assert payload["messages"][1]["content"] == [{"type": "text", "text": "answer"}]


# ------------------------------------------------------------ generationConfig


def test_generation_config_maps_onto_the_anthropic_sampling_fields() -> None:
    payload = _convert(
        contents="hi",
        generationConfig={
            "maxOutputTokens": 512,
            "temperature": 0.4,
            "topP": 0.9,
            "topK": 40,
            "stopSequences": ["STOP", ""],
        },
    ).payload

    assert payload["max_tokens"] == 512
    assert payload["temperature"] == 0.4
    assert payload["top_p"] == 0.9
    assert payload["top_k"] == 40
    assert payload["stop_sequences"] == ["STOP"]


def test_snake_case_generation_config_is_accepted_too() -> None:
    payload = _convert(
        contents="hi", generation_config={"max_output_tokens": 8, "top_p": 0.1}
    ).payload

    assert payload["max_tokens"] == 8
    assert payload["top_p"] == 0.1


def test_more_than_one_candidate_is_refused_by_name() -> None:
    with pytest.raises(GeminiConversionError) as excinfo:
        _convert(contents="hi", generationConfig={"candidateCount": 2})

    assert excinfo.value.field == "generationConfig.candidateCount"


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (0, {"type": "disabled", "enabled": False}),
        (-1, {"type": "adaptive"}),
        (2048, {"type": "enabled", "budget_tokens": 2048}),
    ],
)
def test_thinking_budget_carries_googles_two_reserved_values(budget, expected) -> None:
    payload = _convert(
        contents="hi", generationConfig={"thinkingConfig": {"thinkingBudget": budget}}
    ).payload

    assert payload["thinking"] == expected


def test_thinking_level_becomes_a_named_effort() -> None:
    """Gemini 3's rung, which Gemini CLI's own chat-base-3 preset sends."""

    payload = _convert(
        contents="hi",
        generationConfig={"thinkingConfig": {"thinkingLevel": "HIGH"}},
    ).payload

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}


def test_thinking_level_off_disables_thinking() -> None:
    payload = _convert(
        contents="hi", generationConfig={"thinkingConfig": {"thinkingLevel": "OFF"}}
    ).payload

    assert payload["thinking"] == {"type": "disabled", "enabled": False}
    assert "output_config" not in payload


def test_include_thoughts_is_reported_to_the_caller() -> None:
    assert (
        _convert(
            contents="hi",
            generationConfig={"thinkingConfig": {"includeThoughts": True}},
        ).include_thoughts
        is True
    )
    assert _convert(contents="hi").include_thoughts is False


def test_a_response_schema_populates_canonical_structured_output() -> None:
    schema = {"type": "OBJECT", "properties": {"n": {"type": "INTEGER"}}}
    payload = _convert(
        contents="hi",
        generationConfig={
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    ).payload

    expected_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert "system" not in payload
    assert payload["output_config"] == {
        "format": {"type": "json_schema", "schema": expected_schema}
    }
    assert payload["structured_output_spec"] == {
        "type": "json_schema",
        "name": "structured_output",
        "schema": expected_schema,
    }


def test_a_plain_json_mime_type_populates_json_object_spec() -> None:
    payload = _convert(
        contents="hi", generationConfig={"responseMimeType": "application/json"}
    ).payload

    assert "system" not in payload
    assert "output_config" not in payload
    assert payload["structured_output_spec"] == {"type": "json_object"}


# --------------------------------------------------------------------- tools


def test_function_declarations_become_anthropic_tools() -> None:
    payload = _convert(
        contents="hi",
        tools=[
            {
                "functionDeclarations": [
                    {
                        "name": "read_file",
                        "description": "read one file",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {"path": {"type": "STRING"}},
                            "required": ["path"],
                        },
                    }
                ]
            }
        ],
    ).payload

    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "read one file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]


def test_a_google_hosted_tool_is_dropped_and_named_rather_than_refused() -> None:
    """Refusing would make Gemini CLI unusable the moment search is enabled."""

    conversion = _convert(
        contents="hi",
        tools=[
            {"googleSearch": {}},
            {"functionDeclarations": [{"name": "echo"}]},
        ],
    )

    assert conversion.dropped == ["googleSearch"]
    assert [tool["name"] for tool in conversion.payload["tools"]] == ["echo"]


def test_safety_settings_and_cached_content_are_dropped_and_named() -> None:
    conversion = _convert(
        contents="hi",
        safetySettings=[{"category": "HARM_CATEGORY_HARASSMENT"}],
        cachedContent="cachedContents/abc",
    )

    assert conversion.dropped == ["safety_settings", "cached_content"]


@pytest.mark.parametrize(
    ("mode", "allowed", "expected_choice", "expects_tools"),
    [
        ("AUTO", None, None, True),
        ("VALIDATED", None, None, True),
        ("ANY", None, {"type": "any"}, True),
        ("ANY", ["echo"], {"type": "tool", "name": "echo"}, True),
        ("ANY", ["echo", "other"], {"type": "any"}, True),
        ("NONE", None, None, False),
    ],
)
def test_function_calling_config_modes(
    mode, allowed, expected_choice, expects_tools
) -> None:
    config = {"mode": mode}
    if allowed is not None:
        config["allowedFunctionNames"] = allowed
    payload = _convert(
        contents="hi",
        tools=[{"functionDeclarations": [{"name": "echo"}, {"name": "other"}]}],
        toolConfig={"functionCallingConfig": config},
    ).payload

    assert ("tools" in payload) is expects_tools
    assert payload.get("tool_choice") == expected_choice


def test_an_unknown_function_calling_mode_is_a_named_400() -> None:
    with pytest.raises(GeminiConversionError) as excinfo:
        _convert(
            contents="hi",
            tools=[{"functionDeclarations": [{"name": "echo"}]}],
            toolConfig={"functionCallingConfig": {"mode": "MAGIC"}},
        )

    assert excinfo.value.field == "toolConfig"


# ------------------------------------------------------------------- streaming


def _event(name: str, **data) -> AnthropicSseEvent:
    return AnthropicSseEvent(event=name, data={"type": name, **data})


def _frames(chunks: list[str]) -> list[dict]:
    payloads = []
    for chunk in chunks:
        assert chunk.startswith("data: "), chunk
        assert chunk.endswith("\n\n")
        payloads.append(json.loads(chunk[len("data: ") : -2]))
    return payloads


def _drive(assembler: GeminiStreamAssembler, events) -> list[dict]:
    chunks: list[str] = []
    for event in events:
        chunks.extend(assembler.process_anthropic_event(event))
    chunks.extend(assembler.finish_if_needed())
    return _frames(chunks)


def test_text_streams_as_model_parts_and_finishes_with_stop() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    assembler.bind_response_id("resp1")

    frames = _drive(
        assembler,
        [
            _event("message_start", message={"usage": {"input_tokens": 7}}),
            _event(
                "content_block_start",
                index=0,
                content_block={"type": "text", "text": ""},
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "text_delta", "text": "Hel"},
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "text_delta", "text": "lo"},
            ),
            _event("content_block_stop", index=0),
            _event(
                "message_delta",
                delta={"stop_reason": "end_turn"},
                usage={"output_tokens": 2},
            ),
            _event("message_stop"),
        ],
    )

    text = "".join(
        part.get("text", "")
        for frame in frames
        for part in frame["candidates"][0]["content"]["parts"]
    )
    assert text == "Hello"
    assert frames[0]["responseId"] == "resp1"
    assert frames[0]["modelVersion"] == "m"
    assert frames[-1]["candidates"][0]["finishReason"] == "STOP"
    assert frames[-1]["usageMetadata"] == {
        "promptTokenCount": 7,
        "candidatesTokenCount": 2,
        "totalTokenCount": 9,
    }


def test_thoughts_reach_the_client_only_when_it_asked_for_them() -> None:
    events = [
        _event(
            "content_block_start",
            index=0,
            content_block={"type": "thinking", "thinking": ""},
        ),
        _event(
            "content_block_delta",
            index=0,
            delta={"type": "thinking_delta", "thinking": "hmm"},
        ),
        _event("content_block_stop", index=0),
        _event("message_stop"),
    ]

    silent = GeminiStreamAssembler("m", include_thoughts=False)
    loud = GeminiStreamAssembler("m", include_thoughts=True)

    assert all(
        not frame["candidates"][0]["content"]["parts"]
        for frame in _drive(silent, list(events))
    )
    thought_frames = [
        part
        for frame in _drive(loud, list(events))
        for part in frame["candidates"][0]["content"]["parts"]
    ]
    assert thought_frames == [{"text": "hmm", "thought": True}]


def test_two_interleaved_tool_calls_emit_whole_function_calls_in_close_order() -> None:
    """Gemini has no partial functionCall: args are whole or the client throws."""

    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    frames = _drive(
        assembler,
        [
            _event(
                "content_block_start",
                index=0,
                content_block={"type": "tool_use", "id": "toolu_a", "name": "alpha"},
            ),
            _event(
                "content_block_start",
                index=1,
                content_block={"type": "tool_use", "id": "toolu_b", "name": "beta"},
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '{"x":'},
            ),
            _event(
                "content_block_delta",
                index=1,
                delta={"type": "input_json_delta", "partial_json": '{"y":2}'},
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": "1}"},
            ),
            _event("content_block_stop", index=1),
            _event("content_block_stop", index=0),
            _event("message_delta", delta={"stop_reason": "tool_use"}),
            _event("message_stop"),
        ],
    )

    calls = [
        part["functionCall"]
        for frame in frames
        for part in frame["candidates"][0]["content"]["parts"]
        if "functionCall" in part
    ]
    assert calls == [
        {"id": "toolu_b", "name": "beta", "args": {"y": 2}},
        {"id": "toolu_a", "name": "alpha", "args": {"x": 1}},
    ]
    # Gemini reports STOP for a turn that called a function; the client
    # dispatches on the parts, not on the reason.
    assert frames[-1]["candidates"][0]["finishReason"] == "STOP"


def test_a_tool_call_whose_block_never_closed_is_still_emitted() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    frames = _drive(
        assembler,
        [
            _event(
                "content_block_start",
                index=0,
                content_block={"type": "tool_use", "id": "t", "name": "alpha"},
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '{"x":1}'},
            ),
        ],
    )

    assert frames[0]["candidates"][0]["content"]["parts"] == [
        {"functionCall": {"id": "t", "name": "alpha", "args": {"x": 1}}}
    ]


def test_truncated_tool_arguments_become_an_empty_object_not_a_string() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    frames = _drive(
        assembler,
        [
            _event(
                "content_block_start",
                index=0,
                content_block={"type": "tool_use", "id": "t", "name": "alpha"},
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '{"x":'},
            ),
            _event("content_block_stop", index=0),
        ],
    )

    assert (
        frames[0]["candidates"][0]["content"]["parts"][0]["functionCall"]["args"] == {}
    )


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("end_turn", "STOP"),
        ("stop_sequence", "STOP"),
        ("max_tokens", "MAX_TOKENS"),
        ("tool_use", "STOP"),
        ("pause_turn", "STOP"),
        ("refusal", "SAFETY"),
        (None, "STOP"),
        ("something_new", "STOP"),
    ],
)
def test_stop_reasons_translate_to_googles_enum(stop_reason, finish_reason) -> None:
    assert finish_reason_for(stop_reason) == finish_reason


def test_cache_reads_are_folded_into_the_prompt_count_and_reported() -> None:
    """Google's promptTokenCount includes the cache; Anthropic's excludes it."""

    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    frames = _drive(
        assembler,
        [
            _event(
                "message_start",
                message={
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 90,
                        "output_tokens": 0,
                    }
                },
            ),
            _event("message_delta", delta={}, usage={"output_tokens": 5}),
            _event("message_stop"),
        ],
    )

    assert frames[-1]["usageMetadata"] == {
        "promptTokenCount": 100,
        "candidatesTokenCount": 5,
        "totalTokenCount": 105,
        "cachedContentTokenCount": 90,
    }


def test_thought_tokens_are_estimated_and_capped_by_the_real_output_count() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=True)
    frames = _drive(
        assembler,
        [
            _event("message_start", message={"usage": {"input_tokens": 1}}),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "thinking_delta", "thinking": "a" * 400},
            ),
            _event("message_delta", delta={}, usage={"output_tokens": 3}),
            _event("message_stop"),
        ],
    )

    assert frames[-1]["usageMetadata"]["thoughtsTokenCount"] == 3


def test_a_mid_stream_failure_carries_an_error_and_an_abnormal_finish() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    assembler.process_anthropic_event(_event("message_start", message={}))
    failure = ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="upstream is busy",
        retryable=True,
    )

    frames = _frames(assembler.fail_execution(failure))

    assert frames[0]["error"] == {
        "code": 429,
        "message": "upstream is busy",
        "status": "RESOURCE_EXHAUSTED",
    }
    assert frames[0]["candidates"][0]["finishReason"] == "OTHER"
    assert assembler.terminal is True


def test_a_gemini_stream_carries_no_done_sentinel() -> None:
    """``[DONE]`` is OpenAI's convention; @google/genai would try to parse it."""

    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    chunks = assembler.complete()

    assert all("[DONE]" not in chunk for chunk in chunks)


# ------------------------------------------------------------ non-stream shape


def test_a_complete_message_assembles_the_same_parts_the_stream_emits() -> None:
    body = generate_content_response_from_anthropic_message(
        {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "id": "t", "name": "alpha", "input": {"x": 1}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 4, "output_tokens": 6},
        },
        model="m",
        response_id="resp",
        include_thoughts=True,
    )

    assert body["candidates"][0]["content"]["parts"] == [
        {"text": "hmm", "thought": True},
        {"text": "Hello"},
        {"functionCall": {"id": "t", "name": "alpha", "args": {"x": 1}}},
    ]
    assert body["candidates"][0]["finishReason"] == "STOP"
    assert body["usageMetadata"]["promptTokenCount"] == 4
    assert body["modelVersion"] == "m"
    assert body["responseId"] == "resp"


def test_a_client_that_did_not_ask_for_thoughts_never_receives_them() -> None:
    body = generate_content_response_from_anthropic_message(
        {
            "content": [{"type": "thinking", "thinking": "hmm"}],
            "stop_reason": "end_turn",
        },
        model="m",
        response_id="resp",
        include_thoughts=False,
    )

    assert body["candidates"][0]["content"]["parts"] == []


# ---------------------------------------------------------------- error shapes


@pytest.mark.parametrize(
    ("kind", "status_code", "expected"),
    [
        (FailureKind.INVALID_REQUEST, 400, "INVALID_ARGUMENT"),
        (FailureKind.INVALID_REQUEST, 404, "NOT_FOUND"),
        (FailureKind.CONTEXT_LENGTH, 400, "INVALID_ARGUMENT"),
        (FailureKind.AUTHENTICATION, 401, "UNAUTHENTICATED"),
        (FailureKind.PERMISSION, 403, "PERMISSION_DENIED"),
        (FailureKind.PERMISSION, 402, "FAILED_PRECONDITION"),
        (FailureKind.RATE_LIMIT, 429, "RESOURCE_EXHAUSTED"),
        (FailureKind.OVERLOADED, 529, "UNAVAILABLE"),
        (FailureKind.TIMEOUT, 504, "DEADLINE_EXCEEDED"),
        (FailureKind.UPSTREAM, 500, "INTERNAL"),
        (FailureKind.UNAVAILABLE, 503, "UNAVAILABLE"),
    ],
)
def test_failure_kinds_map_to_googles_canonical_statuses(
    kind, status_code, expected
) -> None:
    failure = ExecutionFailure(
        kind=kind, status_code=status_code, message="nope", retryable=False
    )

    assert gemini_status_for_failure(failure) == expected
    assert gemini_failure_payload(failure) == {
        "error": {"code": status_code, "message": "nope", "status": expected}
    }


def test_the_error_envelope_repeats_the_status_inside_the_body() -> None:
    assert gemini_error_payload(message="bad", code=400) == {
        "error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}
    }


# ------------------------------------------------------------- model listings


def test_a_model_entry_omits_a_limit_nobody_published() -> None:
    entry = gemini_model_entry(
        "anthropic/openrouter/gpt-5",
        display_name="openrouter/gpt-5",
        input_token_limit=None,
        output_token_limit=8192,
    )

    assert entry["name"] == "models/anthropic/openrouter/gpt-5"
    assert entry["displayName"] == "openrouter/gpt-5"
    assert "inputTokenLimit" not in entry
    assert entry["outputTokenLimit"] == 8192
    assert entry["supportedGenerationMethods"] == [
        "generateContent",
        "streamGenerateContent",
        "countTokens",
    ]


def test_the_listing_envelope_omits_an_empty_page_token() -> None:
    payload = gemini_models_payload([])

    assert payload == {"models": []}


# ------------------------------------------- opt-in web search & urlContext


def test_allow_web_search_keeps_non_web_hosted_tools_dropped_and_validates_schema() -> (
    None
):
    req = _request(
        contents="hi",
        tools=[
            {"googleSearch": {"excludeDomains": ["Example.COM", "spam.net"]}},
            {"urlContext": {}},
            {"codeExecution": {}},
            {"functionDeclarations": [{"name": "echo"}]},
        ],
    )
    conversion = convert_request_to_anthropic_payload(req, allow_web_search=True)
    assert conversion.dropped == ["codeExecution"]
    assert [t["name"] for t in conversion.payload["tools"]] == ["echo"]

    spec = extract_gemini_web_search_spec(
        req.tools, req.tool_config, allow_web_search=True
    )
    assert spec is not None
    assert spec.enable_search is True
    assert spec.enable_fetch is True
    assert spec.blocked_domains == ("example.com", "spam.net")

    # toolConfig mode NONE disables web tool execution cleanly.
    none_req = _request(
        contents="hi",
        tools=[{"googleSearch": {}}],
        toolConfig={"functionCallingConfig": {"mode": "NONE"}},
    )
    assert (
        extract_gemini_web_search_spec(
            none_req.tools, none_req.tool_config, allow_web_search=True
        )
        is None
    )


@pytest.mark.parametrize(
    "bad_tools",
    [
        [{"googleSearch": "invalid"}],
        [{"googleSearch": {"excludeDomains": "example.com"}}],
        [
            {
                "googleSearchRetrieval": {
                    "dynamicRetrievalConfig": {"dynamicThreshold": 1.5}
                }
            }
        ],
        [{"googleSearchRetrieval": {"dynamicRetrievalConfig": {"mode": "BOGUS"}}}],
        [{"urlContext": ["not-an-object"]}],
    ],
)
def test_malformed_gemini_web_tools_raise_schema_on_error(bad_tools) -> None:
    req = _request(contents="hi", tools=bad_tools)
    with pytest.raises(GeminiConversionError) as excinfo:
        extract_gemini_web_search_spec(
            req.tools, req.tool_config, allow_web_search=True
        )
    assert "Expected Gemini web tools" in str(excinfo.value)

    with pytest.raises(GeminiConversionError) as conv_exc:
        convert_request_to_anthropic_payload(req, allow_web_search=True)
    assert "Expected Gemini web tools" in str(conv_exc.value)


@pytest.mark.asyncio
async def test_gemini_web_search_response_honest_nulls_and_no_done() -> None:
    from types import SimpleNamespace

    fetch_only = build_gemini_payload_from_loop(
        model="m",
        response_id="resp_1",
        include_thoughts=False,
        stop_reason="end_turn",
        web_calls=[
            SimpleNamespace(
                tool_kind="web_fetch",
                action={"type": "open_page", "url": "https://example.com/a"},
                status="completed",
                error_code=None,
            ),
            SimpleNamespace(
                tool_kind="web_fetch",
                action={"type": "open_page", "url": "http://127.0.0.1/secret"},
                status="failed",
                error_code="url_not_allowed",
            ),
            SimpleNamespace(
                tool_kind="web_fetch",
                action={"type": "open_page", "url": "https://down.example.com/"},
                status="failed",
                error_code="unavailable",
            ),
        ],
        gathered_sources=[
            SimpleNamespace(
                url="https://example.com/a",
                title="Page A",
                source_kind="fetch",
            )
        ],
        final_step_blocks=[{"type": "text", "text": "Summary without search."}],
        grounded_citations=[],
        loop_usage={"input_tokens": 10, "output_tokens": 5},
    )
    cand = fetch_only["candidates"][0]
    assert "groundingMetadata" not in cand
    assert cand["urlContextMetadata"] == {
        "urlMetadata": [
            {
                "retrievedUrl": "https://example.com/a",
                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
            },
            {
                "retrievedUrl": "http://127.0.0.1/secret",
                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_UNSAFE",
            },
            {
                "retrievedUrl": "https://down.example.com/",
                "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_ERROR",
            },
        ]
    }

    frames = [
        frame
        async for frame in iter_gemini_sse_from_loop(
            model="m",
            response_id="resp_2",
            include_thoughts=False,
            stop_reason="end_turn",
            web_calls=[
                SimpleNamespace(
                    tool_kind="web_search",
                    action={"type": "search", "query": "python 3.14"},
                    status="completed",
                    error_code=None,
                )
            ],
            gathered_sources=[
                SimpleNamespace(
                    url="https://docs.python.org/3.14/",
                    title="Python 3.14",
                    source_kind="search",
                )
            ],
            final_step_blocks=[
                {
                    "type": "text",
                    "text": "See [Python 3.14](https://docs.python.org/3.14/).",
                }
            ],
            grounded_citations=[
                {
                    "type": "url_citation",
                    "url": "https://docs.python.org/3.14/",
                    "title": "Python 3.14",
                    "start_index": 4,
                    "end_index": 47,
                }
            ],
            loop_usage={"input_tokens": 20, "output_tokens": 8},
        )
    ]
    joined = "".join(frames)
    assert "[DONE]" not in joined
    last = json.loads(frames[-1].removeprefix("data: ").strip())
    gm = last["candidates"][0]["groundingMetadata"]
    assert "confidenceScores" not in gm
    assert "searchEntryPoint" not in gm
    assert gm["webSearchQueries"] == ["python 3.14"]
    assert gm["groundingSupports"][0]["groundingChunkIndices"] == [0]
    assert "urlContextMetadata" not in last["candidates"][0]


# ------------------------------------------- PR 8: Gemini Door Parity


@pytest.mark.parametrize(
    ("error_type", "expected_code", "expected_status"),
    [
        ("rate_limit_error", 429, "RESOURCE_EXHAUSTED"),
        ("invalid_request_error", 400, "INVALID_ARGUMENT"),
        ("authentication_error", 401, "UNAUTHENTICATED"),
        ("permission_error", 403, "PERMISSION_DENIED"),
        ("billing_error", 402, "FAILED_PRECONDITION"),
        ("not_found_error", 404, "NOT_FOUND"),
        ("request_too_large", 413, "INVALID_ARGUMENT"),
        ("overloaded_error", 529, "UNAVAILABLE"),
        ("timeout_error", 504, "DEADLINE_EXCEEDED"),
        ("api_error", 500, "INTERNAL"),
        ("unknown_error_type", 500, "INTERNAL"),
    ],
)
def test_stream_assembler_error_event_maps_real_status(
    error_type: str, expected_code: int, expected_status: str
) -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    assembler.process_anthropic_event(_event("message_start", message={}))
    frames = _frames(
        assembler.process_anthropic_event(
            _event(
                "error",
                error={"type": error_type, "message": "upstream boom"},
            )
        )
    )
    assert frames[0]["error"] == {
        "code": expected_code,
        "message": "upstream boom",
        "status": expected_status,
    }
    assert frames[0]["candidates"][0]["finishReason"] == "OTHER"


def test_repeated_function_call_names_pair_fifo_and_survive_chat_and_responses() -> (
    None
):
    from my_claude_code.core.anthropic import (
        AnthropicToOpenAIConverter,
        MessagesRequest,
    )

    conversion = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "read two files"}]},
            {
                "role": "model",
                "parts": [
                    {"functionCall": {"name": "read_file", "args": {"path": "a.txt"}}},
                    {"functionCall": {"name": "read_file", "args": {"path": "b.txt"}}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "read_file",
                            "response": {"output": "alpha"},
                        }
                    },
                    {
                        "functionResponse": {
                            "name": "read_file",
                            "response": {"output": "beta"},
                        }
                    },
                ],
            },
        ]
    )
    tool_uses = conversion.payload["messages"][1]["content"]
    tool_results = conversion.payload["messages"][2]["content"]
    assert [b["id"] for b in tool_uses] == ["gemini_call_0", "gemini_call_1"]
    assert [b["tool_use_id"] for b in tool_results] == [
        "gemini_call_0",
        "gemini_call_1",
    ]

    # Must survive OpenAI Chat conversion without orphaned tool_use error.
    req = MessagesRequest(**conversion.payload)
    openai_msgs = AnthropicToOpenAIConverter.convert_messages(req.messages)
    tool_msgs = [m for m in openai_msgs if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == [
        "gemini_call_0",
        "gemini_call_1",
    ]


def test_mixed_explicit_and_omitted_function_response_ids_pair_cleanly() -> None:
    payload = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "go"}]},
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "call_explicit",
                            "name": "read_file",
                            "args": {"p": "a"},
                        }
                    },
                    {
                        "functionCall": {
                            "name": "read_file",
                            "args": {"p": "b"},
                        }
                    },
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call_explicit",
                            "name": "read_file",
                            "response": {"output": "1"},
                        }
                    },
                    {
                        "functionResponse": {
                            "name": "read_file",
                            "response": {"output": "2"},
                        }
                    },
                ],
            },
        ]
    ).payload

    results = payload["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["call_explicit", "gemini_call_1"]


def test_thinking_level_and_response_schema_merge_into_output_config() -> None:
    payload = _convert(
        contents="hi",
        generationConfig={
            "thinkingConfig": {"thinkingLevel": "HIGH"},
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {"ok": {"type": "BOOLEAN"}},
            },
        },
    ).payload

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {
        "effort": "high",
        "format": {
            "type": "json_schema",
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        },
    }
    assert payload["structured_output_spec"]["type"] == "json_schema"


def test_empty_response_json_schema_is_not_skipped_by_falsy_or() -> None:
    payload = _convert(
        contents="hi",
        generationConfig={
            "responseMimeType": "application/json",
            "responseJsonSchema": {},
            "responseSchema": {
                "type": "OBJECT",
                "properties": {"x": {"type": "STRING"}},
            },
        },
    ).payload

    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": {}}}
    assert payload["structured_output_spec"] == {
        "type": "json_schema",
        "name": "structured_output",
        "schema": {},
    }


def test_normalize_schema_handles_nested_openapi_and_json_schema_constructs() -> None:
    from my_claude_code.core.gemini_api.tools import normalize_schema

    raw = {
        "type": "OBJECT",
        "$defs": {
            "Sub": {
                "type": "INTEGER",
                "nullable": True,
            }
        },
        "definitions": {"Alt": {"type": "NUMBER"}},
        "properties": {
            "choice": {
                "any_of": [{"type": "STRING"}, {"type": "INTEGER"}],
                "nullable": True,
            },
            "pair": {
                "type": "ARRAY",
                "items": [{"type": "STRING"}, {"type": "BOOLEAN"}],
                "prefixItems": [{"type": "NUMBER"}],
            },
            "falseNullable": {"type": "STRING", "nullable": False},
        },
        "additionalProperties": {"type": "STRING"},
        "patternProperties": {"^x-": {"type": "BOOLEAN"}},
        "not": {"type": "NULL"},
    }

    normalized = normalize_schema(raw)
    assert normalized["type"] == "object"
    assert normalized["$defs"] == {"Sub": {"type": ["integer", "null"]}}
    assert normalized["definitions"] == {"Alt": {"type": "number"}}
    assert normalized["properties"]["choice"] == {
        "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]
    }
    assert "any_of" not in normalized["properties"]["choice"]
    assert normalized["properties"]["pair"] == {
        "type": "array",
        "items": [{"type": "string"}, {"type": "boolean"}],
        "prefixItems": [{"type": "number"}],
    }
    assert normalized["properties"]["falseNullable"] == {"type": "string"}
    assert normalized["additionalProperties"] == {"type": "string"}
    assert normalized["patternProperties"] == {"^x-": {"type": "boolean"}}
    assert normalized["not"] == {"type": "null"}


def test_text_x_enum_keeps_prompt_instruction_and_skips_json_schema_wrapper() -> None:
    payload = _convert(
        contents="pick one",
        generationConfig={
            "responseMimeType": "text/x.enum",
            "responseSchema": {"type": "STRING", "enum": ["YES", "NO"]},
        },
    ).payload

    assert "structured_output_spec" not in payload
    assert "output_config" not in payload
    assert "enum" in payload["system"]


def test_mode_none_with_tool_history_preserves_and_synthesizes_tools() -> None:
    conversion = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "go"}]},
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "read_file", "args": {"p": "a"}}}],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "read_file",
                            "response": {"output": "ok"},
                        }
                    }
                ],
            },
        ],
        tools=[{"functionDeclarations": [{"name": "other_tool"}]}],
        toolConfig={"functionCallingConfig": {"mode": "NONE"}},
    )

    assert conversion.payload["tool_choice"] == {"type": "none"}
    assert [t["name"] for t in conversion.payload["tools"]] == [
        "other_tool",
        "read_file",
    ]


def test_allowed_function_names_multi_and_ignored_generation_fields_are_traced() -> (
    None
):
    conversion = _convert(
        contents="hi",
        generationConfig={
            "maxOutputTokens": 64,
            "seed": 42,
            "presencePenalty": 0.5,
            "frequencyPenalty": 0.2,
            "responseLogprobs": True,
            "logprobs": 3,
            "mediaResolution": "MEDIA_RESOLUTION_HIGH",
        },
        tools=[{"functionDeclarations": [{"name": "a"}, {"name": "b"}]}],
        toolConfig={
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": ["a", "b"],
            }
        },
    )

    assert conversion.dropped == [
        "generationConfig.seed",
        "generationConfig.presencePenalty",
        "generationConfig.frequencyPenalty",
        "generationConfig.responseLogprobs",
        "generationConfig.logprobs",
        "generationConfig.mediaResolution",
        "toolConfig.functionCallingConfig.allowedFunctionNames",
    ]


def test_function_response_parts_multimodal_and_mixed_user_turn() -> None:
    payload = _convert(
        contents=[
            {"role": "user", "parts": [{"text": "inspect"}]},
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "screenshot", "args": {}}}],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "screenshot",
                            "response": {"output": "captured"},
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": "aGk=",
                                    }
                                },
                                {"text": "caption"},
                            ],
                        }
                    },
                    {"text": "what do you see?"},
                ],
            },
        ]
    ).payload

    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    user_blocks = payload["messages"][2]["content"]
    assert user_blocks[0]["type"] == "tool_result"
    assert user_blocks[0]["content"] == [
        {"type": "text", "text": "captured"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
        },
        {"type": "text", "text": "caption"},
    ]
    assert user_blocks[1] == {"type": "text", "text": "what do you see?"}


@pytest.mark.parametrize(
    "file_data",
    [
        {"fileUri": "https://example.com/v.mp4", "mimeType": "video/mp4"},
        {"fileUri": "https://example.com/no_mime"},
    ],
)
def test_file_data_rejects_non_image_non_pdf_mime_type(file_data) -> None:
    with pytest.raises(GeminiConversionError) as excinfo:
        _convert(contents=[{"role": "user", "parts": [{"fileData": file_data}]}])
    assert excinfo.value.field == "contents"


def test_egress_emits_thought_signature_and_assembler_reads_seeded_input() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    frames = _drive(
        assembler,
        [
            _event(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "call_seeded",
                    "name": "echo",
                    "input": {"k": "v"},
                    "extra_content": {"google": {"thought_signature": "sig-stream"}},
                },
            ),
            _event("content_block_stop", index=0),
            _event("message_delta", delta={"stop_reason": "tool_use"}),
            _event("message_stop"),
        ],
    )
    stream_part = frames[0]["candidates"][0]["content"]["parts"][0]
    assert stream_part == {
        "functionCall": {"id": "call_seeded", "name": "echo", "args": {"k": "v"}},
        "thoughtSignature": "sig-stream",
    }

    non_stream = generate_content_response_from_anthropic_message(
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_seeded",
                    "name": "echo",
                    "input": {"k": "v"},
                    "extra_content": {"google": {"thought_signature": "sig-stream"}},
                }
            ],
            "stop_reason": "tool_use",
        },
        model="m",
        response_id="r",
        include_thoughts=False,
    )
    assert non_stream["candidates"][0]["content"]["parts"][0] == stream_part


def test_assembler_seeded_input_is_replaced_on_first_partial_json_delta() -> None:
    assembler = GeminiStreamAssembler("m", include_thoughts=False)
    frames = _drive(
        assembler,
        [
            _event(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": "call_override",
                    "name": "echo",
                    "input": {"k": "old"},
                },
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={"type": "input_json_delta", "partial_json": '{"k":"new"}'},
            ),
            _event("content_block_stop", index=0),
            _event("message_stop"),
        ],
    )
    assert frames[0]["candidates"][0]["content"]["parts"][0] == {
        "functionCall": {"id": "call_override", "name": "echo", "args": {"k": "new"}}
    }


def test_usage_ledger_ignores_bool_values() -> None:
    from my_claude_code.core.gemini_api.usage import GeminiUsageLedger

    ledger = GeminiUsageLedger()
    ledger.absorb(
        {
            "input_tokens": True,
            "output_tokens": False,
            "cache_read_input_tokens": True,
            "cache_creation_input_tokens": True,
        }
    )
    assert ledger.has_counts() is False
    assert ledger.payload() == {
        "promptTokenCount": 0,
        "candidatesTokenCount": 0,
        "totalTokenCount": 0,
    }


def test_gemini_envelope_captures_tool_call_signatures_and_opt_in_thoughts() -> None:
    from my_claude_code.core.carrier_surface import (
        GEMINI_THOUGHT_PART,
        GEMINI_THOUGHT_SIGNATURE,
    )

    req = _request(
        contents=[
            {"role": "user", "parts": [{"text": "go"}]},
            {
                "role": "model",
                "parts": [
                    {"text": "thinking", "thought": True, "thoughtSignature": "t-sig"},
                    {
                        "functionCall": {"name": "read_file", "args": {"p": "a"}},
                        "thoughtSignature": "fc-sig",
                    },
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "read_file",
                            "response": {"output": "ok"},
                        }
                    }
                ],
            },
        ]
    )
    default_conv = convert_request_to_anthropic_payload(req)
    assert default_conv.payload["gemini_envelope"] == {
        "tool_call_signatures": {"gemini_call_0": "fc-sig"}
    }
    assert default_conv.carrier_surface == {
        GEMINI_THOUGHT_PART: 1,
        GEMINI_THOUGHT_SIGNATURE: 1,
    }

    opt_in_conv = convert_request_to_anthropic_payload(
        req, preserve_thought_signatures=True
    )
    assert opt_in_conv.carrier_surface == {}
    assert opt_in_conv.payload["gemini_envelope"] == {
        "tool_call_signatures": {"gemini_call_0": "fc-sig"},
        "thought_parts": [
            {
                "index": 1,
                "position": 0,
                "text": "thinking",
                "thought_signature": "t-sig",
            }
        ],
    }
