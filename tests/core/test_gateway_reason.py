"""A gateway 4xx that passed on a trace id and no reason is recognised (7.105.2).

Every message here is produced by the real failure path
(``classify_provider_failure`` -> ``format_execution_failure_message``), so the
classifier is tested against what the request log records, not a hand copy.
The bodies are the shapes measured in the request log on 2026-10-22 (last 60
days), with invented ids and invented numbers.
"""

import json
from typing import Any
from unittest.mock import Mock

import httpx
import openai
import pytest

from my_claude_code.core.failures import FailureKind
from my_claude_code.core.gateway_reason import NO_REASON_SHAPES, gateway_trace_id
from my_claude_code.providers.failure_policy import classify_provider_failure

CC_TRACE = "b5162a9f0c1d4e2f8a7b6c5d4e3f2a1b"
BAI_REQUEST_ID = "2026102212000012345678a123b456cDeFgHi"


def _commandcode_body(trace_id: str = CC_TRACE, **inner_extra: Any) -> dict[str, Any]:
    inner = {
        "message": f"invalid request error trace_id: {trace_id}",
        "type": "invalid_request_error",
        **inner_extra,
    }
    # The gateway's own envelope: the inner error as a JSON string, newline
    # included, exactly as the 1,204 recorded bodies carry it.
    return {
        "message": json.dumps(inner, separators=(",", ":")) + "\n",
        "type": "invalid_request_error",
    }


def _bai_body(request_id: str = BAI_REQUEST_ID) -> dict[str, Any]:
    return {
        "code": "invalid_request",
        "message": f"Invalid request body. (request id: {request_id})",
        "type": "api_error",
    }


def _status_error(status: int, body: Any) -> Exception:
    request = httpx.Request("POST", "https://gateway.test/v1/chat/completions")
    if isinstance(body, str):
        response = httpx.Response(status, request=request, text=body)
        parsed: Any = body
    else:
        response = httpx.Response(status, request=request, json=body)
        parsed = body
    error_type: type[openai.APIStatusError] = {
        400: openai.BadRequestError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
        502: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    return error_type("upstream error", response=response, body=parsed)


def _recorded(status: int, body: Any, provider: str = "COMMANDCODE") -> str:
    """The attempt's ``error_message`` exactly as the failure path writes it."""
    failure = classify_provider_failure(
        _status_error(status, body),
        provider_name=provider,
        read_timeout_s=30.0,
        request_id="req_0123456789abcdef0123456789abcdef",
        mark_rate_limited=Mock(),
    )
    return failure.message


# --------------------------------------------------------------- positives


def test_the_command_code_trace_id_form_is_recognised() -> None:
    message = _recorded(400, _commandcode_body())

    # The recorded text is the measured one: nested JSON in a string.
    assert "Upstream provider COMMANDCODE returned HTTP 400." in message
    assert '\\"message\\":\\"invalid request error trace_id: ' in message
    assert gateway_trace_id(message) == CC_TRACE


def test_classification_of_that_refusal_is_untouched() -> None:
    """IV.8: wording and a parsed field only -- the failure is what it was."""
    failure = classify_provider_failure(
        _status_error(400, _commandcode_body()),
        provider_name="COMMANDCODE",
        read_timeout_s=30.0,
        request_id=None,
        mark_rate_limited=Mock(),
    )

    assert failure.kind is FailureKind.MODEL_REJECTED
    assert failure.status_code == 400
    assert failure.retryable is False


def test_the_new_api_request_id_form_is_recognised() -> None:
    message = _recorded(400, _bai_body(), provider="CUSTOM")

    assert gateway_trace_id(message) == BAI_REQUEST_ID


def test_nested_json_in_a_string_under_an_error_envelope() -> None:
    """The same id-only phrase, one envelope deeper, still says nothing."""
    inner = json.dumps(
        {"message": f"invalid request error trace_id: {CC_TRACE}", "type": ""}
    )
    body = {"error": {"message": inner, "type": "invalid_request_error", "code": 400}}

    assert gateway_trace_id(_recorded(400, body)) == CC_TRACE


def test_a_plain_text_body_with_only_the_phrase() -> None:
    message = _recorded(400, f"invalid request error trace_id: {CC_TRACE}")

    assert gateway_trace_id(message) == CC_TRACE


# --------------------------------------------------------------- negatives


def test_a_400_that_names_a_field_is_not_labelled() -> None:
    body = _commandcode_body(param="tools")

    assert gateway_trace_id(_recorded(400, body)) is None


@pytest.mark.parametrize(
    "body",
    [
        # Command Code's own words, measured 46 times: a reason AND an id.
        {
            "message": json.dumps(
                {
                    "error": {
                        "message": "Invalid schema for function 'Artifact': "
                        '"^[^\\\\0]*$" is not a "regex" (request_id: '
                        "1b2c3d4e-0000-4000-8000-123456789abc)",
                        "type": "AI_APICallError",
                        "param": {"statusCode": 400, "name": "AI_APICallError"},
                    }
                }
            ),
            "type": "invalid_request_error",
        },
        # Agnes: a reason with an id inside the same sentence.
        {
            "message": "max_tokens exceeds the limit of 65536 (request id: "
            "20261022120000123456789abcdefz)",
            "type": "AgnesAI_error",
            "param": "",
            "code": "invalid_request",
        },
        # Anthropic: a field path in the message, the id beside it.
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "messages.3.content.0: Invalid `signature` in `thinking` block",
            },
            "request_id": "req_011CeoAbCdEfGhIjKlMnOpQr",
        },
        # Command Code's billing refusal: a reason, no id.
        {
            "message": "You have insufficient credits to make this request.",
            "type": "invalid_request_error",
            "code": "BAD_REQUEST",
        },
        # OpenRouter's documented envelope: the host's raw error rides along.
        {
            "error": {
                "message": "Provider returned error",
                "code": 400,
                "metadata": {"raw": '{"error":"bad"}', "provider_name": "X"},
            }
        },
        # An id-only message beside a code that IS the reason.
        {
            "message": f"invalid request error trace_id: {CC_TRACE}",
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        },
    ],
    ids=[
        "commandcode-regex-reason",
        "agnes-reason-with-id",
        "anthropic-field-path",
        "commandcode-credits",
        "openrouter-metadata",
        "reason-in-code",
    ],
)
def test_a_400_with_a_reason_is_not_labelled(body: dict[str, Any]) -> None:
    assert gateway_trace_id(_recorded(400, body)) is None


@pytest.mark.parametrize("status", [429, 500, 502, 401, 403, 404])
def test_the_same_body_at_another_status_is_not_labelled(status: int) -> None:
    """A rate limit or a server error says something by its status alone."""
    assert gateway_trace_id(_recorded(status, _commandcode_body())) is None


def test_two_different_ids_are_not_labelled() -> None:
    body = {
        "message": f"invalid request error trace_id: {CC_TRACE}",
        "error": "invalid request error trace_id: 00000000000000000000000000000000",
        "type": "invalid_request_error",
    }

    assert gateway_trace_id(_recorded(400, body)) is None


def test_a_truncated_body_is_not_labelled() -> None:
    message = _recorded(400, _commandcode_body()).replace(
        '"type":"invalid_request_error"}\n',
        '"type":"invalid_request_error"}\n... [truncated after 8192 bytes]\n',
        1,
    )

    assert "... [truncated after" in message
    assert gateway_trace_id(message) is None


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "benched: 5 upstream errors in the last 10 attempts (rate_based >= 50%), 22 s left",
        "client cancelled before the stream finished",
        f"invalid request error trace_id: {CC_TRACE}",
        "This model rejected the request.",
    ],
)
def test_text_that_is_not_an_upstream_diagnostic_is_not_labelled(
    message: str | None,
) -> None:
    assert gateway_trace_id(message) is None


def test_a_pathologically_nested_body_is_total() -> None:
    """The failure path must never raise: a 50k-deep body answers None.

    Built by hand: the formatter itself would try to parse this body first.
    """
    message = (
        "Upstream provider COMMANDCODE returned HTTP 400.\n"
        "Category: invalid_request_error\n\nUpstream error:\n"
        + '{"message":"'
        + "[" * 50_000
        + '"}'
    )

    assert gateway_trace_id(message) is None


def test_every_declared_shape_names_its_id_group_and_its_evidence() -> None:
    for shape in NO_REASON_SHAPES:
        assert "id" in shape.message.groupindex, shape.name
        assert shape.statuses, shape.name
        assert shape.evidence.strip(), shape.name
        assert all(400 <= status < 500 for status in shape.statuses), shape.name
