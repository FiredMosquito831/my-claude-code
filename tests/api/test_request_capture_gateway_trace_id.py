"""``params.gateway_trace_id`` is written for an id-only gateway 400 and nothing else.

The equality contract (7.105.2): for every recorded error shape that is not an
id-only gateway refusal, the attempt row -- ``error_message`` and ``params`` --
is exactly what it was before the release. For the id-only refusal, the
message is still stored verbatim and ``params`` differs by one key.

The corpus is every 4xx body shape the live request log held in the 60 days to
2026-10-22 (measured read-only, structure only), with invented ids, numbers
and model names, plus the 429/5xx/benched/cancelled messages attempts carry.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import openai
import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.providers.failure_policy import classify_provider_failure

CC_TRACE = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
BAI_ID = "20261022093000987654321a123b456cAbCdEf"
TRUNCATED = {"chars": 12, "stop_reason_sent": "end_turn", "ended_cleanly": True}


def _commandcode_trace_body(trace_id: str = CC_TRACE) -> dict[str, Any]:
    inner = {
        "message": f"invalid request error trace_id: {trace_id}",
        "type": "invalid_request_error",
    }
    return {
        "message": json.dumps(inner, separators=(",", ":")) + "\n",
        "type": "invalid_request_error",
    }


def _recorded(status: int, body: Any, provider: str) -> str:
    request = httpx.Request("POST", "https://gateway.test/v1/chat/completions")
    if isinstance(body, str):
        response = httpx.Response(status, request=request, text=body)
    else:
        response = httpx.Response(status, request=request, json=body)
    error_type: type[openai.APIStatusError] = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        404: openai.NotFoundError,
        429: openai.RateLimitError,
    }.get(status, openai.APIStatusError)
    failure = classify_provider_failure(
        error_type("upstream error", response=response, body=body),
        provider_name=provider,
        read_timeout_s=30.0,
        request_id="req_fedcba9876543210fedcba9876543210",
        mark_rate_limited=Mock(),
    )
    return failure.message


# (status, provider, body) for every measured shape that must stay unlabelled.
_UNLABELLED_SHAPES: list[tuple[int, str, Any]] = [
    (402, "HYPERCHARM", {"message": "You're out of credits. Add more at https://hyper.charm.test", "type": "billing_error", "code": "insufficient_credits"}),
    (403, "OPENCODE", {"type": "error", "error": {"type": "FreeTierError", "message": "This model is not available on the free tier."}}),
    (429, "OPENCODE", {"type": "error", "error": {"type": "FreeUsageLimitError", "message": "Rate limit exceeded. Please try again later."}}),
    (429, "CUSTOM", {"message": "Too many requests for this key.", "type": "AgnesAI_error", "param": "", "code": "rate_limit_exceeded"}),
    (410, "OPENCODE", {"type": "error", "error": {"type": "ModelDeprecated", "message": "Model example-free has been deprecated."}}),
    (429, "CHATGPT_OAUTH", {"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached", "plan_type": "plus"}}),
    (400, "COMMANDCODE", {"message": "You have insufficient credits to make this request. Please purchase more credits to continue using the service.", "type": "invalid_request_error", "code": "BAD_REQUEST"}),
    (400, "OPENCODE", {"model": "example-free", "error": {"param": "name", "type": "invalid_request_error", "message": "Error from provider (Console): Upstream request failed: [invalid_request_error] `name` must be at most 64 characters, got 80"}}),
    (401, "ANTHROPIC_OAUTH", {"type": "error", "error": {"type": "authentication_error", "message": "OAuth access token has been revoked."}}),
    (429, "NOUS_PORTAL", {"status": 429, "message": "You are being rate limited.", "reason": "rate_limited", "retry_after": 12, "alternates": ["example/a"]}),
    (400, "CUSTOM", {"message": "credit insufficient balance: balance=0 required=12 (request id: 20261022093000987654321a123b456cAbCdEg)", "type": "api_error", "param": "", "code": "insufficient_user_quota"}),
    (429, "COMMANDCODE", {"message": "Rate limited, retry later.", "type": "rate_limit_error", "code": "RATE_LIMITED"}),
    (404, "OPEN_ROUTER", {"message": "This model is unavailable for free. The paid version is available now - use this slug instead: example/paid", "code": 404}),
    (404, "NOUS_PORTAL", {"status": 404, "message": "Model 'example/x' not found."}),
    (404, "NOUS_PORTAL", {"status": "error", "code": 404, "message": "Application not found", "request_id": "wLnMHvPPTnK-hrug1o2EoQ"}),
    (404, "NVIDIA_NIM", ""),
    (400, "NOUS_PORTAL", {"status": 400, "message": "This request is not valid. Additional info: This endpoint's maximum context length is 131072 tokens."}),
    (400, "NVIDIA_NIM", {"status": 400, "title": "Bad Request", "detail": "Function id '1b2c3d4e-0000-4000-8000-123456789abc': DEGRADED function cannot be invoked"}),
    (400, "NVIDIA_NIM", {"message": "This model's maximum context length is 131072 tokens. However, your messages resulted in 140000 tokens.", "type": "Bad Request", "code": 400}),
    (400, "CUSTOM", {"message": "upstream: BadRequestError: OpenAIException - {\"object\":\"error\",\"message\":\"The input (300000 tokens) is longer than the model's context length (262144 tokens).\",\"type\":\"BadRequestError\",\"param\":null,\"code\":400}\nmodel=example-flash", "type": "invalid_request_error", "param": "", "code": "400"}),
    (400, "CHATGPT_OAUTH", {"error": {"message": "Invalid JSON schema: regex lookaround is not supported. Found at $.properties.videoScale.pattern.", "type": "invalid_request_error", "param": "tools", "code": "invalid_json_schema"}}),
    (400, "OPENCODE", {"message": "Your request resulted in more tokens than this model's maximum context length allows.", "type": "invalid_request_error", "code": "context_length_exceeded"}),
    (400, "CUSTOM", {"message": "The request is invalid: read body failed. Please check the request body, required fields, and request format.", "type": "gateway_error", "param": "", "code": "400"}),
    (400, "COMMANDCODE", {"message": json.dumps({"error": {"message": "Invalid schema for function 'Artifact': \"^[^\\\\0]*$\" is not a \"regex\" (request_id: 1b2c3d4e-0000-4000-8000-123456789abc)", "type": "AI_APICallError", "param": {"statusCode": 400}}}), "type": "invalid_request_error"}),
    (400, "OPENCODE", {"type": "invalid_request_error", "message": "Error from provider (Console): Upstream request failed: [invalid_request_error] invalid request"}),
    (400, "CUSTOM", {"message": "The request is invalid: 图片输入格式/解析错误. Please check the request body.", "type": "invalid_request_error", "param": "", "code": "400"}),
    (400, "CUSTOM", {"message": "max_tokens exceeds the limit of 65536 (request id: 20261022093000987654321abcdz)", "type": "AgnesAI_error", "param": "", "code": "invalid_request"}),
    (400, "ANTHROPIC_OAUTH", {"type": "error", "error": {"type": "invalid_request_error", "message": "messages.3.content.0: Invalid `signature` in `thinking` block"}, "request_id": "req_011CeoAbCdEfGhIjKlMnOpQr"}),
    (400, "HYPERCHARM", {"message": "This request exceeds the model's context window of 200000 tokens.", "type": "invalid_request_error", "code": None}),
    # The id-only phrase at a status it was never measured at.
    (429, "COMMANDCODE", _commandcode_trace_body()),
    (500, "COMMANDCODE", _commandcode_trace_body()),
    (502, "COMMANDCODE", _commandcode_trace_body()),
]

# Messages attempts carry that are not upstream diagnostics at all.
_NON_DIAGNOSTIC_MESSAGES = [
    "never reached",
    "paused by you on Model Config",
    "client cancelled before the stream finished",
    "request budget spent before this model was tried",
    "benched: 5 upstream errors in the last 10 attempts (rate_based >= 50%), 22 s left",
    "Provider request timed out after 30s.",
    "Could not connect to provider.",
]


@pytest.fixture
def store(tmp_path) -> Path:
    """A folder; each recorded attempt gets its own log, closed before it is read."""
    return tmp_path


def _stored_attempt(
    folder: Path,
    request_id: str,
    message: str | None,
    *,
    truncated: dict[str, object] | None = None,
) -> dict[str, Any]:
    store = RequestLogStore(folder / f"{request_id}.db")
    capture = RequestCapture(
        store,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-5",
        input_text="hello",
        params={"max_tokens": 100},
    )
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="commandcode",
            model_ref="commandcode/deepseek/deepseek-v4.1-flash",
            outcome="failed",
            error_kind="model_rejected",
            error_message=message,
            truncated=truncated,
        )
    )
    capture.finish_success("ok")
    store.close()
    row = store.get_request(request_id)
    assert row is not None
    (attempt,) = row["route_attempts"]
    return attempt


def test_the_trace_id_refusal_gains_one_parsed_key_and_keeps_its_text(
    store: Path,
) -> None:
    message = _recorded(400, _commandcode_trace_body(), "COMMANDCODE")

    attempt = _stored_attempt(store, "req_gateway", message)

    assert attempt["error_message"] == message
    assert attempt["error_kind"] == "model_rejected"
    assert attempt["params"] == {"gateway_trace_id": CC_TRACE}


def test_params_differ_only_by_the_parsed_key(store: Path) -> None:
    labelled = _stored_attempt(
        store,
        "req_gateway_t",
        _recorded(400, _commandcode_trace_body(), "COMMANDCODE"),
        truncated=TRUNCATED,
    )
    plain = _stored_attempt(store, "req_plain_t", "Could not connect to provider.", truncated=TRUNCATED)

    extra = dict(labelled["params"])
    assert extra.pop("gateway_trace_id") == CC_TRACE
    assert extra == plain["params"] == {"truncated_after_commit": TRUNCATED}


def test_the_new_api_request_id_refusal_is_parsed(store: Path) -> None:
    body = {
        "code": "invalid_request",
        "message": f"Invalid request body. (request id: {BAI_ID})",
        "type": "api_error",
    }

    attempt = _stored_attempt(store, "req_bai", _recorded(400, body, "CUSTOM"))

    assert attempt["params"] == {"gateway_trace_id": BAI_ID}


@pytest.mark.parametrize(
    ("status", "provider", "body"),
    _UNLABELLED_SHAPES,
    ids=[f"{i:02d}-{s}-{p.lower()}" for i, (s, p, _b) in enumerate(_UNLABELLED_SHAPES)],
)
def test_every_other_recorded_shape_stores_exactly_what_it_did(
    store: Path, status: int, provider: str, body: Any
) -> None:
    """Byte-identical: the message verbatim and no params at all, as before."""
    message = _recorded(status, body, provider)

    attempt = _stored_attempt(store, "req_shape", message)

    assert attempt["error_message"] == message
    assert attempt["params"] is None


@pytest.mark.parametrize("message", _NON_DIAGNOSTIC_MESSAGES)
def test_non_diagnostic_messages_store_exactly_what_they_did(
    store: Path, message: str
) -> None:
    attempt = _stored_attempt(store, "req_text", message, truncated=TRUNCATED)

    assert attempt["error_message"] == message
    assert attempt["params"] == {"truncated_after_commit": TRUNCATED}
