"""Tests for the outbound wire-request capture.

The defect this closes: the dashboard reported the client's ``max_tokens`` and
tool count, read off the inbound Anthropic request before routing and before
the output budget ran. Every test here is about the difference between what was
asked for and what was sent.
"""

import json

import pytest

from my_claude_code.core.wire_capture import (
    MAX_WIRE_BODY_CHARS,
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


def test_oversize_bodies_are_capped_and_say_so():
    huge = _body(extra_body={"padding": "x" * (MAX_WIRE_BODY_CHARS * 2)})
    stored = json.loads(summarize_wire_body(huge))
    assert stored["_truncated"] is True
    assert stored["_limit"] == MAX_WIRE_BODY_CHARS
    assert stored["_original_chars"] > MAX_WIRE_BODY_CHARS
    assert len(stored["_preview"]) == MAX_WIRE_BODY_CHARS


def test_ordinary_bodies_are_stored_whole():
    stored = json.loads(summarize_wire_body(_body()))
    assert "_truncated" not in stored
    assert stored["max_tokens"] == 16384


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
