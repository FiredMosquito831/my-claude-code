"""The SSE chunk shape MCC builds itself must read exactly like the SDK's.

Every streamed reply of every OpenAI-chat-family provider used to be decoded
into a full ``ChatCompletionChunk`` pydantic model, several constructions per
frame. 6.63.0 builds MCC's own object instead, so the risk this file exists to
retire is that some host-specific field -- always an *undeclared* one, read
through ``model_extra`` -- quietly stops arriving and an answer changes in the
middle of a stream.

``test_recorded_streams_produce_the_expected_anthropic_frames`` is the
guarantee: ``sse_chunk_expected.json`` was recorded on a clean worktree at
``8cd9ad6d`` (v6.62.0, the SDK-model path) from the same
``sse_chunk_fixtures.json``, and the frames MCC emits downstream must still be
those bytes, frame for frame.
"""

import json
from types import SimpleNamespace

import pytest
from openai.types import CompletionUsage

from my_claude_code.core.wire_capture import ResponseShape
from my_claude_code.providers.openai_chat.chunks import (
    MccChunk,
    MccUsage,
    adopt_chat_stream,
    build_chunk,
)
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.tool_calls import tool_call_extra_content
from tests.providers.sse_replay import EXPECTED_PATH, load_fixtures, replay


def _expected() -> dict[str, list[str]]:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", load_fixtures(), ids=lambda f: f["name"])
@pytest.mark.asyncio
async def test_recorded_streams_produce_the_expected_anthropic_frames(fixture):
    """The downstream bytes are the ones v6.62.0 produced, frame for frame."""
    assert await replay(fixture) == _expected()[fixture["name"]]


@pytest.mark.asyncio
async def test_no_sdk_model_is_constructed_while_streaming(monkeypatch):
    """The whole point: zero pydantic constructions per chunk, not fewer."""
    from openai import _models

    calls: list[str] = []
    real_construct = _models.BaseModel.construct.__func__

    def counting_construct(cls, *args, **kwargs):
        calls.append(cls.__name__)
        return real_construct(cls, *args, **kwargs)

    monkeypatch.setattr(_models.BaseModel, "construct", classmethod(counting_construct))
    fixture = next(
        f for f in load_fixtures() if f["name"] == "groq_tool_calls_interleaved"
    )
    frames = await replay(fixture)

    assert frames, "the fixture must actually stream"
    assert calls == [], f"SDK models were still built: {sorted(set(calls))}"


def test_declared_fields_stay_declared_and_the_rest_become_extras():
    chunk = build_chunk(
        {
            "id": "chatcmpl-1",
            "model": "m",
            "created": 7,
            "object": "chat.completion.chunk",
            "provider": "some-gateway",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {
                        "role": "assistant",
                        "content": "hi",
                        "reasoning": "r1",
                        "reasoning_content": "r2",
                        "reasoning_details": [{"type": "reasoning.text"}],
                    },
                }
            ],
        }
    )
    assert isinstance(chunk, MccChunk)
    assert chunk.id == "chatcmpl-1"
    assert chunk.model == "m"
    assert chunk.created == 7
    assert chunk.usage is None
    # Declared on the SDK model, so never an extra.
    assert chunk.model_extra == {"provider": "some-gateway"}

    delta = chunk.choices[0].delta
    assert delta.content == "hi"
    assert delta.refusal is None
    assert delta.tool_calls is None
    # The three reasoning spellings every profile family reads, all undeclared
    # by the SDK and so all reachable exactly as pydantic made them reachable.
    assert delta.model_extra == {
        "reasoning": "r1",
        "reasoning_content": "r2",
        "reasoning_details": [{"type": "reasoning.text"}],
    }
    assert delta.__pydantic_extra__ is delta.model_extra
    # Read the way the profiles read them: by name, at run time.
    for name, value in (
        ("reasoning", "r1"),
        ("reasoning_content", "r2"),
        ("reasoning_details", [{"type": "reasoning.text"}]),
    ):
        assert getattr(delta, name) == value
    missing = "not_a_field"
    assert getattr(delta, missing, "fallback") == "fallback"
    with pytest.raises(AttributeError):
        getattr(delta, missing)


def test_every_profile_reasoning_field_is_reachable_on_the_shape():
    """No profile may name a delta field the shape cannot answer."""
    fields = {
        profile.reasoning_delta_field for profile in OPENAI_CHAT_PROFILES.values()
    }
    fields |= {
        profile.reasoning_delta_fallback_field
        for profile in OPENAI_CHAT_PROFILES.values()
        if profile.reasoning_delta_fallback_field is not None
    }
    assert fields == {"reasoning", "reasoning_content"}
    for field in fields:
        chunk = build_chunk({"choices": [{"delta": {field: "value"}}]})
        profile_delta = chunk.choices[0].delta
        assert getattr(profile_delta, field) == "value"


def test_tool_call_extra_content_is_found_on_every_rung():
    chunk = build_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "Read", "arguments": "{}"},
                                "extra_content": {
                                    "google": {"thought_signature": "SIG"}
                                },
                            }
                        ]
                    }
                }
            ]
        }
    )
    tool_call = chunk.choices[0].delta.tool_calls[0]
    assert tool_call.index == 0
    assert tool_call.id == "call_1"
    assert tool_call.type == "function"
    assert tool_call.function.name == "Read"
    assert tool_call.function.arguments == "{}"
    assert tool_call_extra_content(tool_call) == {
        "google": {"thought_signature": "SIG"}
    }


def test_usage_extras_and_details_read_like_the_sdk_model():
    usage = MccUsage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "total_tokens": 107,
            "prompt_tokens_details": {"cached_tokens": 64},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "cost": 0.5,
            "prompt_cache_hit_tokens": 64,
        }
    )
    assert usage.prompt_tokens == 100
    assert usage.prompt_tokens_details == {"cached_tokens": 64}
    assert usage.model_extra == {"cost": 0.5, "prompt_cache_hit_tokens": 64}
    assert getattr(usage, "cost", None) == 0.5


def test_recorded_usage_key_shape_does_not_move():
    """``ResponseShape`` records which usage keys a host answered with."""
    payload = {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
        "cost": 0.25,
    }
    sdk_shape = ResponseShape(started_at=0.0)
    # Built the way the SDK's own stream built it: no validation, extras kept.
    sdk_usage = CompletionUsage.construct(None, **payload)
    sdk_shape.note_usage(sdk_usage)
    mcc_shape = ResponseShape(started_at=0.0)
    mcc_shape.note_usage(MccUsage(payload))
    assert mcc_shape.usage_keys == sdk_shape.usage_keys


def test_a_frame_that_is_not_an_object_is_passed_through():
    assert build_chunk("just a string") == "just a string"
    assert build_chunk(None) is None


@pytest.mark.asyncio
async def test_adopting_anything_but_an_sdk_stream_leaves_it_alone():
    """Every test double and Mistral's normalizer must survive untouched."""
    double = SimpleNamespace(choices=[])
    assert adopt_chat_stream(double) is double
