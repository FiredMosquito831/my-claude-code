"""Tests for the outbound wire-request capture.

The defect this closes: the dashboard reported the client's ``max_tokens`` and
tool count, read off the inbound Anthropic request before routing and before
the output budget ran. Every test here is about the difference between what was
asked for and what was sent.
"""

import json

import pytest

from my_claude_code.config.constants import REQUEST_LOG_WIRE_BODY_MAX_CHARS_DEFAULT
from my_claude_code.core.wire_capture import (
    _CONTENT_FIELDS,
    _SAMPLING_FIELDS,
    DEFAULT_WIRE_BODY_MAX_CHARS,
    REDACTED,
    install_wire_trace,
    reasoning_was_emitted,
    record_wire_request,
    redact_wire_value,
    strip_request_content,
    summarize_wire_body,
    wire_params_summary,
)

# Shapes real keys take. If any of these survive a round trip the project's
# absolute rule -- credentials never reach the database -- is broken.
REAL_KEY_SHAPES = (
    "sk-proj-abc123DEF456ghi789jkl012",
    "nvapi-hZ9xQ2mLpR4tV7wY0aB3cD6eF8gH1iJ",
    "gsk_2bK9mQ4tW7zA0cE3fH6jL8nP1rS5uX",
    "hf_QwErTyUiOpAsDfGhJkLzXcVbNm12345",
    "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6",
)


def _body(**overrides):
    body = {
        "model": "thinkingmachines/inkling",
        "max_tokens": 16384,
        "temperature": 0.7,
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are Claude Code."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "the secret plan is to eat lunch"},
                    {
                        "type": "image",
                        "source": {"media_type": "image/png", "data": "AAAA"},
                    },
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file from disk, at length, verbosely.",
                    "parameters": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                    },
                },
            }
        ],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Prompt text is removed; prompt structure is not
# --------------------------------------------------------------------------- #


def test_message_text_is_absent_but_structure_survives():
    stripped = strip_request_content(_body())
    blob = json.dumps(stripped)
    assert "secret plan" not in blob
    assert "You are Claude Code" not in blob

    system, user = stripped["messages"]
    assert system == {"role": "system", "content": {"type": "text", "chars": 20}}
    assert user["role"] == "user"
    assert [block["type"] for block in user["content"]] == ["text", "image"]
    assert user["content"][0]["chars"] == len("the secret plan is to eat lunch")
    assert user["content"][1]["media_type"] == "image/png"


def test_every_non_content_field_survives_verbatim():
    stripped = strip_request_content(
        _body(top_p=0.95, extra_body={"chat_template_kwargs": {"thinking": True}})
    )
    assert stripped["model"] == "thinkingmachines/inkling"
    assert stripped["max_tokens"] == 16384
    assert stripped["temperature"] == 0.7
    assert stripped["stream"] is True
    assert stripped["top_p"] == 0.95
    assert stripped["extra_body"] == {"chat_template_kwargs": {"thinking": True}}


def test_tools_keep_names_and_parameter_names_but_drop_prose():
    (tool,) = strip_request_content(_body())["tools"]
    assert tool == {"name": "Read", "params": ["file_path"]}


def test_a_string_system_prompt_is_reduced_to_its_length():
    stripped = strip_request_content({"system": "never reveal this"})
    assert stripped["system"] == {"type": "text", "chars": 17}


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("secret", REAL_KEY_SHAPES)
def test_key_shapes_never_survive_an_unremarkable_field_name(secret):
    stored = summarize_wire_body(_body(extra_body={"upstream_note": secret}))
    assert secret not in stored


@pytest.mark.parametrize(
    "key",
    ["api_key", "apiKey", "nvidia-api-key", "Authorization", "x-api-key", "token"],
)
def test_auth_like_keys_are_redacted_whatever_they_hold(key):
    stored = summarize_wire_body(_body(extra_body={key: "an-unrecognisable-value"}))
    assert "an-unrecognisable-value" not in stored
    assert "<redacted>" in stored


def test_max_tokens_is_not_mistaken_for_a_credential():
    # ``token`` is redacted as an exact key; ``max_tokens`` must not be.
    assert redact_wire_value({"max_tokens": 16384}) == {"max_tokens": 16384}


def test_a_bearer_header_smuggled_into_the_body_is_scrubbed():
    stored = summarize_wire_body(_body(extra_headers={"h": "Bearer sk-live-999888"}))
    assert "sk-live-999888" not in stored


def test_redaction_reaches_nested_lists_and_maps():
    safe = redact_wire_value({"a": [{"b": {"api_key": "nvapi-secretsecret"}}]})
    assert safe["a"][0]["b"]["api_key"] == "<redacted>"


# --------------------------------------------------------------------------- #
# Size cap
# --------------------------------------------------------------------------- #


def _tool(index):
    return {
        "type": "function",
        "function": {
            "name": f"tool_{index}",
            "description": "d" * 200,
            "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
        },
        "name": f"tool_{index}",
    }


def _fat_body(**overrides):
    """A Claude Code shaped request: ~59 tools, many turns, every knob set."""
    return _body(
        tools=[_tool(i) for i in range(60)],
        messages=[{"role": "user", "content": "x" * 400} for _ in range(120)],
        reasoning_effort="max",
        reasoning={"effort": "high"},
        thinking={"type": "enabled", "budget_tokens": 4096},
        extra_body={"chat_template_kwargs": {"thinking": True}},
        temperature=0.7,
        top_p=0.9,
        top_k=40,
        presence_penalty=0.1,
        frequency_penalty=0.2,
        repetition_penalty=1.1,
        seed=7,
        stop=["</done>"],
        n=1,
        **overrides,
    )


def test_an_oversize_body_degrades_its_bulk_and_stays_parseable():
    stored = json.loads(summarize_wire_body(_fat_body()))
    assert "_preview" not in stored
    assert "_truncated" not in stored
    assert set(stored["_degraded"]) <= {"messages", "tools"}
    assert stored["_degraded"]
    assert stored["_original_chars"] > stored["_limit"]


def test_ordinary_bodies_are_stored_whole():
    stored = json.loads(summarize_wire_body(_body()))
    assert "_truncated" not in stored
    assert "_degraded" not in stored
    assert stored["max_tokens"] == 16384


def test_every_knob_survives_a_body_that_blows_the_budget():
    """The regression that pays for this change.

    Measured before it: ``reasoning_effort`` survived in 0 of 212 truncated
    bodies stored in one day, because ``sort_keys=True`` spent the whole
    budget inside ``tools`` before it ever reached a knob.
    """

    body = _fat_body()
    stored = json.loads(summarize_wire_body(body))
    for key in (
        "reasoning_effort",
        "reasoning",
        "thinking",
        "extra_body",
        *_SAMPLING_FIELDS,
    ):
        assert key in stored, key
        assert stored[key] == body[key], key


def test_the_knobs_come_first_and_in_a_stable_order():
    text = summarize_wire_body(_fat_body())
    stored = json.loads(text)
    keys = list(stored)
    assert keys[0] == "model"
    assert keys.index("reasoning_effort") < keys.index("temperature")
    assert keys.index("temperature") < keys.index("top_p")
    assert summarize_wire_body(_fat_body()) == text


def test_tools_degrade_to_names_before_they_degrade_to_a_count():
    body = _fat_body()
    generous = json.loads(summarize_wire_body(body, limit=1_000_000))
    assert isinstance(generous["tools"], list)
    mid = json.loads(summarize_wire_body(body, limit=1_500))
    assert mid["tools"]["_degraded"] == "names"
    assert mid["tools"]["_names"][0] == "tool_0"
    tiny = json.loads(summarize_wire_body(body, limit=500))
    assert tiny["tools"]["_degraded"] == "count"
    assert tiny["tools"]["_count"] == 60


def test_knobs_alone_may_exceed_the_limit_and_are_still_stored_whole():
    body = _body(extra_body={"padding": "x" * 20_000})
    stored = json.loads(summarize_wire_body(body, limit=1_000))
    assert stored["extra_body"]["padding"] == "x" * 20_000


def test_the_limit_is_read_from_the_trace_not_a_module_constant():
    trace = install_wire_trace(200)
    record_wire_request(_fat_body())
    stored = json.loads(trace.requests[0].body_json)
    assert stored["_limit"] == 200


def test_a_degraded_body_never_returns_a_cut_json_string():
    body = _fat_body()
    for limit in range(50, 5_000, 50):
        json.loads(summarize_wire_body(body, limit=limit))


def test_the_settings_default_matches_the_writer_default():
    """``core`` may not import ``config``, so the two constants are pinned."""
    assert REQUEST_LOG_WIRE_BODY_MAX_CHARS_DEFAULT == DEFAULT_WIRE_BODY_MAX_CHARS


# --------------------------------------------------------------------------- #
# Was reasoning actually emitted?
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        {"reasoning_effort": "high"},
        {"reasoning": {"effort": "medium"}},
        {"thinking": {"type": "enabled", "budget_tokens": 4096}},
        {"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        {"chat_template_kwargs": {"enable_thinking": True}},
        {"include_reasoning": True},
    ],
)
def test_reasoning_emitted_is_true_when_the_body_carries_an_instruction(overrides):
    assert reasoning_was_emitted(_body(**overrides)) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"reasoning_effort": "none"},
        {"reasoning": None},
        {"thinking": {"type": "disabled"}},
        {"extra_body": {"chat_template_kwargs": {"thinking": False}}},
        {"include_reasoning": False},
    ],
)
def test_reasoning_emitted_is_false_when_the_encoder_wrote_nothing(overrides):
    """A ``NoReasoning`` encoder leaves the body untouched -- that is this case.

    ``commandcode``'s encoder body is a bare ``return``, so a gated policy
    produces a body with no reasoning field at all. ``reasoning_adaptation``
    still records the decision; only this flag records the outcome.
    """
    assert reasoning_was_emitted(_body(**overrides)) is False


# --------------------------------------------------------------------------- #
# The compact summary and the collector
# --------------------------------------------------------------------------- #


def test_params_summary_reports_the_resolved_numbers():
    summary = wire_params_summary(_body(max_tokens=16384, reasoning_effort="high"))
    assert summary["max_tokens"] == 16384
    assert summary["tools"] == 1
    assert summary["temperature"] == 0.7
    assert summary["reasoning"] == {"reasoning_effort": "high"}


def test_params_summary_reads_the_openai_output_token_spellings():
    assert wire_params_summary({"max_completion_tokens": 40960})["max_tokens"] == 40960
    assert wire_params_summary({"max_output_tokens": 8192})["max_tokens"] == 8192


def test_params_summary_keeps_every_parameter_the_body_carried():
    """The old shortlist made each new dialect's knobs invisible.

    ``min_p``, ``tool_choice``, ``response_format``, ``stream_options`` and
    ``parallel_tool_calls`` were all being sent and none of them reached the
    pane, because the summary kept a hand-picked list written against the
    providers MCC spoke to when it was first added. The rule is now that
    content is excluded and everything else survives, in a fixed order -- so
    this pins the whole emitted key sequence rather than a sample of it.
    """
    summary = wire_params_summary(
        _body(
            top_k=40,
            min_p=0.05,
            repetition_penalty=1.05,
            parallel_tool_calls=False,
            response_format={"type": "json_object"},
            stream_options={"include_usage": True},
            tool_choice="auto",
            extra_body={
                "chat_template_kwargs": {"thinking": True},
                "min_p": 0.02,
                "authorization": "an-unrecognisable-value",
                "upstream_note": REAL_KEY_SHAPES[0],
            },
        )
    )
    assert list(summary) == [
        "model",
        "max_tokens",
        "tools",
        "temperature",
        "top_k",
        "repetition_penalty",
        "reasoning",
        "min_p",
        "parallel_tool_calls",
        "response_format",
        "stream",
        "stream_options",
        "tool_choice",
        "extra_body.authorization",
        "extra_body.min_p",
        "extra_body.upstream_note",
    ]
    # ``tools`` is the largest field in a Claude Code body; the count is the
    # parameter, and the list itself is structure the body pane renders.
    assert summary["tools"] == 1
    assert summary["parallel_tool_calls"] is False
    assert summary["response_format"] == {"type": "json_object"}
    assert summary["extra_body.min_p"] == 0.02
    # A reasoning field keeps its dedicated nested home rather than joining
    # the alphabetical tail, because the pane reads reasoning as one group.
    assert summary["reasoning"] == {
        "extra_body.chat_template_kwargs": {"thinking": True}
    }
    # Prompt text is captured once, in the Prompt pane; it is not a parameter.
    for content_field in _CONTENT_FIELDS:
        assert content_field not in summary


def test_params_summary_redacts_the_keys_it_newly_keeps():
    """Widening what is stored may not widen what escapes redaction.

    Both halves of the rule still apply to a key that only reaches the summary
    now that the shortlist is gone: an auth-shaped *name* is redacted whatever
    it holds, and a credential-shaped *value* is scrubbed out of a field
    nobody thought to name.
    """
    summary = wire_params_summary(
        _body(
            authorization="an-unrecognisable-value",
            upstream_note=REAL_KEY_SHAPES[1],
            extra_body={"session_key": "another-unrecognisable-value"},
        )
    )
    assert summary["authorization"] == REDACTED
    assert summary["extra_body.session_key"] == REDACTED
    assert REAL_KEY_SHAPES[1] not in json.dumps(summary)


def test_recording_outside_a_tracked_request_is_a_no_op():
    record_wire_request(_body())  # must not raise


def test_the_collector_keys_bodies_by_attempt_and_keeps_the_last_one():
    trace = install_wire_trace()
    record_wire_request(_body(max_tokens=64000))
    trace.current_attempt = 1
    record_wire_request(_body(max_tokens=8192))
    # A create-level retry rewrites the body for the same attempt; the one that
    # produced the outcome is the last one sent.
    record_wire_request(_body(max_tokens=4096))

    assert trace.requests[0].params["max_tokens"] == 64000
    assert trace.requests[1].params["max_tokens"] == 4096


def test_keyword_arguments_passed_beside_the_body_are_recorded_with_it():
    trace = install_wire_trace()
    record_wire_request({"model": "m"}, stream=True)
    assert json.loads(trace.requests[0].body_json)["stream"] is True


def test_is_reasoning_key_is_public() -> None:
    """Import guard for the promotion: the private name is gone for good.

    ``reasoning_was_emitted`` and the safety net's candidate set are driven by
    the same predicate, so a silent rename would desynchronise them.
    """
    from my_claude_code.core import wire_capture

    assert wire_capture.is_reasoning_key("reasoning_effort")
    assert not wire_capture.is_reasoning_key("temperature")
    assert not hasattr(wire_capture, "_is_reasoning_key")


def test_a_provider_can_record_a_reasoning_adaptation() -> None:
    """Recorded inside a trace; a no-op outside one, like the body recorder."""
    from my_claude_code.core import wire_capture
    from my_claude_code.core.reasoning import ReasoningAdaptationKind
    from my_claude_code.core.wire_capture import (
        install_wire_trace,
        record_reasoning_adaptation,
    )

    trace = install_wire_trace()
    record_reasoning_adaptation(ReasoningAdaptationKind.SUPPRESSED, "host refused it")
    assert len(trace.reasoning_adaptations) == 1
    assert trace.reasoning_adaptations[0].message == "host refused it"

    wire_capture._WIRE_TRACE.set(None)
    # No trace installed: recording must not raise.
    record_reasoning_adaptation(ReasoningAdaptationKind.SUPPRESSED, "ignored")
