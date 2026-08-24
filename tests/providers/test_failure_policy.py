"""Raw provider failure classification into the canonical neutral model."""

from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import Mock

import httpx
import openai
import pytest

from my_claude_code.core.anthropic.errors import (
    anthropic_error_type_for_failure,
    anthropic_status_for_error_type,
)
from my_claude_code.core.diagnostics import (
    ERROR_DETAIL_DISPLAY_CAP_BYTES,
    attach_upstream_error_body,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_responses.errors import openai_error_type_for_failure
from my_claude_code.providers.failure_policy import classify_provider_failure


def _openai_status_error(
    error_type: type[openai.APIStatusError],
    *,
    status_code: int,
    message: str,
    body: object | None = None,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return error_type(
        message,
        response=response,
        body=body or {"error": {"message": message}},
    )


def _statusless_openai_error(message: str, body: object | None) -> openai.APIError:
    return openai.APIError(
        message,
        request=httpx.Request("POST", "https://provider.test/v1/chat/completions"),
        body=body,
    )


def _http_status_error(status_code: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(
        status_code,
        request=request,
        json={"error": {"message": message, "api_key": "SECRET"}},
    )
    return httpx.HTTPStatusError(message, request=request, response=response)


@dataclass(frozen=True, slots=True)
class _ClassificationCase:
    name: str
    error: Callable[[], Exception]
    kind: FailureKind
    status_code: int
    retryable: bool
    rate_limit_block_seconds: int | None = None


_CASES = (
    _ClassificationCase(
        "openai_authentication",
        lambda: _openai_status_error(
            openai.AuthenticationError,
            status_code=401,
            message="Unauthorized",
        ),
        FailureKind.AUTHENTICATION,
        401,
        False,
    ),
    _ClassificationCase(
        "openai_rate_limit",
        lambda: _openai_status_error(
            openai.RateLimitError,
            status_code=429,
            message="Too many requests",
        ),
        FailureKind.RATE_LIMIT,
        429,
        True,
        60,
    ),
    _ClassificationCase(
        "openai_bad_request",
        lambda: _openai_status_error(
            openai.BadRequestError,
            status_code=400,
            message="bad tool shape",
        ),
        FailureKind.INVALID_REQUEST,
        400,
        False,
    ),
    _ClassificationCase(
        "openai_overload_marker",
        lambda: _openai_status_error(
            openai.InternalServerError,
            status_code=500,
            message="No capacity available",
        ),
        FailureKind.OVERLOADED,
        529,
        True,
    ),
    _ClassificationCase(
        "openai_generic_503_preserved",
        lambda: _openai_status_error(
            openai.InternalServerError,
            status_code=503,
            message="generic server failure",
        ),
        FailureKind.UPSTREAM,
        503,
        True,
    ),
    _ClassificationCase(
        "statusless_openai_rate_limit_body",
        lambda: _statusless_openai_error(
            "stream embedded error",
            {"error": {"message": "too many requests", "code": 429}},
        ),
        FailureKind.RATE_LIMIT,
        429,
        True,
        60,
    ),
    _ClassificationCase(
        "statusless_openai_overload_body",
        lambda: _statusless_openai_error(
            "ResourceExhausted: limit reached",
            {"error": {"message": "ResourceExhausted: limit reached"}},
        ),
        FailureKind.OVERLOADED,
        529,
        True,
    ),
    _ClassificationCase(
        "statusless_openai_unknown_is_not_retryable",
        lambda: _statusless_openai_error(
            "stream embedded error",
            {"error": {"message": "unknown provider failure"}},
        ),
        FailureKind.UPSTREAM,
        500,
        False,
    ),
    _ClassificationCase(
        "http_403_keeps_authentication_quirk",
        lambda: _http_status_error(403, "Forbidden"),
        FailureKind.AUTHENTICATION,
        401,
        False,
    ),
    _ClassificationCase(
        "http_502_keeps_overload_quirk",
        lambda: _http_status_error(502, "Bad gateway"),
        FailureKind.OVERLOADED,
        529,
        True,
    ),
    _ClassificationCase(
        "http_599_preserves_status",
        lambda: _http_status_error(599, "Upstream failure"),
        FailureKind.UPSTREAM,
        599,
        True,
    ),
    _ClassificationCase(
        "http_405_is_not_retryable",
        lambda: _http_status_error(405, "Wrong endpoint"),
        FailureKind.UPSTREAM,
        405,
        False,
    ),
    _ClassificationCase(
        "read_timeout_keeps_pre_start_status",
        lambda: httpx.ReadTimeout(
            "",
            request=httpx.Request("POST", "https://provider.test/v1/messages"),
        ),
        FailureKind.TIMEOUT,
        502,
        True,
    ),
    _ClassificationCase(
        "openai_connection_error_keeps_status",
        lambda: openai.APIConnectionError(
            request=httpx.Request("POST", "https://provider.test/v1/chat/completions")
        ),
        FailureKind.UNAVAILABLE,
        500,
        True,
    ),
    _ClassificationCase(
        "unknown_exception_keeps_gateway_status",
        lambda: RuntimeError("unexpected provider failure"),
        FailureKind.UPSTREAM,
        502,
        False,
    ),
)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_raw_provider_failure_maps_to_canonical_failure(
    case: _ClassificationCase,
) -> None:
    mark_rate_limited = Mock()

    failure = classify_provider_failure(
        case.error(),
        provider_name="TEST_PROVIDER",
        read_timeout_s=30.0,
        request_id="req_classification",
        mark_rate_limited=mark_rate_limited,
    )

    assert isinstance(failure, ExecutionFailure)
    assert failure.kind is case.kind
    assert failure.status_code == case.status_code
    assert failure.retryable is case.retryable
    assert failure.message.strip()
    assert "Request ID: req_classification" in failure.message
    assert "SECRET" not in failure.message
    if case.rate_limit_block_seconds is None:
        mark_rate_limited.assert_not_called()
    else:
        mark_rate_limited.assert_called_once_with(case.rate_limit_block_seconds)


def test_classification_preserves_useful_body_while_redacting_credentials() -> None:
    error = _http_status_error(
        400,
        "unsupported model format authorization: Bearer AUTH_SECRET",
    )

    failure = classify_provider_failure(
        error,
        provider_name="LOCAL",
        read_timeout_s=60.0,
        request_id="req_body",
        mark_rate_limited=Mock(),
    )

    assert failure.kind is FailureKind.INVALID_REQUEST
    assert failure.status_code == 400
    assert "Upstream provider LOCAL returned HTTP 400." in failure.message
    assert "unsupported model format" in failure.message
    assert "Request ID: req_body" in failure.message
    assert "AUTH_SECRET" not in failure.message
    assert "SECRET" not in failure.message


def test_auth_failure_preserves_model_error_body_instead_of_masking_it() -> None:
    error = _openai_status_error(
        openai.AuthenticationError,
        status_code=401,
        message="Unauthorized",
        body={
            "type": "error",
            "error": {
                "type": "ModelError",
                "message": ("Model qwen3.7-max is not supported for format oa-compat"),
            },
        },
    )

    failure = classify_provider_failure(
        error,
        provider_name="OPENCODE_GO",
        read_timeout_s=60.0,
        request_id="req_model",
        mark_rate_limited=Mock(),
    )

    assert failure.kind is FailureKind.AUTHENTICATION
    assert failure.status_code == 401
    assert "Category: ModelError" in failure.message
    assert "Provider authentication failed. Check API key." in failure.message
    assert "Model qwen3.7-max is not supported for format oa-compat" in failure.message
    assert "Request ID: req_model" in failure.message


def test_empty_http_error_body_is_reported_explicitly() -> None:
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(500, request=request, content=b"")
    error = httpx.HTTPStatusError(
        "Server Error",
        request=request,
        response=response,
    )

    failure = classify_provider_failure(
        error,
        provider_name="EMPTY",
        read_timeout_s=30.0,
        request_id="req_empty",
        mark_rate_limited=Mock(),
    )

    assert failure.kind is FailureKind.UPSTREAM
    assert failure.status_code == 500
    assert "Upstream provider EMPTY returned HTTP 500." in failure.message
    assert "(empty upstream error body)" in failure.message


def test_http_405_diagnostic_names_rejected_upstream_endpoint() -> None:
    failure = classify_provider_failure(
        _http_status_error(405, "Method Not Allowed"),
        provider_name="LOCAL",
        read_timeout_s=30.0,
        request_id="req_405",
        mark_rate_limited=Mock(),
    )

    assert failure.kind is FailureKind.UPSTREAM
    assert failure.status_code == 405
    assert (
        "Upstream provider LOCAL rejected the request method or endpoint (HTTP 405)."
        in failure.message
    )
    assert "Request ID: req_405" in failure.message


def test_connection_cause_chain_is_redacted_and_capped() -> None:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    error = openai.APIConnectionError(request=request)
    error.__cause__ = httpx.ConnectError(
        "connect failed authorization: Bearer CAUSE_SECRET "
        + "x" * (ERROR_DETAIL_DISPLAY_CAP_BYTES + 10),
        request=request,
    )

    failure = classify_provider_failure(
        error,
        provider_name="NIM",
        read_timeout_s=30.0,
        request_id="req_cause",
        mark_rate_limited=Mock(),
    )

    assert "Caused by:" in failure.message
    assert "ConnectError: connect failed authorization: <redacted>" in failure.message
    assert "CAUSE_SECRET" not in failure.message
    assert f"truncated after {ERROR_DETAIL_DISPLAY_CAP_BYTES} bytes" in failure.message
    assert "Request ID: req_cause" in failure.message


def test_attached_streamed_error_body_remains_bounded() -> None:
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(500, request=request, content=b"")
    error = httpx.HTTPStatusError(
        "Server Error",
        request=request,
        response=response,
    )
    attach_upstream_error_body(
        error,
        "x" * (ERROR_DETAIL_DISPLAY_CAP_BYTES + 10),
    )

    failure = classify_provider_failure(
        error,
        provider_name="LONG",
        read_timeout_s=30.0,
        request_id="req_long",
        mark_rate_limited=Mock(),
    )

    assert f"truncated after {ERROR_DETAIL_DISPLAY_CAP_BYTES} bytes" in failure.message
    assert "x" * 100 in failure.message


# The three upstream wordings measured on 153,198 production requests: half of
# every `invalid_request` failure was one of these, and each ended a route that
# had a configured fallback chain sitting unused.
_NIM_CONTEXT_OVERFLOW = (
    "This model's maximum context length is 262144 tokens. However, your "
    "messages resulted in 262294 tokens. Please reduce the length of the messages."
)
_NOUS_CONTEXT_OVERFLOW = (
    "This request is not valid. Check the model name and other parameters. "
    "Additional info: This endpoint's maximum context length is 262144 tokens. "
    "However, you requested about 266577 tokens (165073 of text input, 37504 of "
    "tool input, 64000 in the output). Please reduce the length of either one, "
    "or use the context-compression plugin to compress your prompt automatically."
)
_OPENROUTER_CONTEXT_OVERFLOW = (
    "This endpoint's maximum context length is 256000 tokens. However, you "
    "requested about 256487 tokens (151032 of text input, 41455 of tool input, "
    "64000 in the output). Please reduce the length of either one, or use the "
    "context-compression plugin to compress your prompt automatically."
)


def _classify(error: Exception) -> ExecutionFailure:
    return classify_provider_failure(
        error,
        provider_name="OPENROUTER",
        read_timeout_s=30.0,
        request_id=None,
        mark_rate_limited=Mock(),
    )


@pytest.mark.parametrize(
    ("vendor", "message"),
    (
        ("nvidia_nim", _NIM_CONTEXT_OVERFLOW),
        ("nous_portal", _NOUS_CONTEXT_OVERFLOW),
        ("open_router", _OPENROUTER_CONTEXT_OVERFLOW),
    ),
)
def test_a_context_overflow_400_is_not_a_malformed_request(vendor, message) -> None:
    """Every vendor spells it differently; none of them means "the body is wrong"."""
    sdk = _classify(
        _openai_status_error(openai.BadRequestError, status_code=400, message=message)
    )
    raw = _classify(_http_status_error(400, message))

    for failure in (sdk, raw):
        assert failure.kind is FailureKind.CONTEXT_LENGTH, vendor
        assert failure.status_code == 400, vendor
        # Not retryable: the same model rejects the same body again. This flag
        # is about reusing the credential, not about the fallback chain.
        assert failure.retryable is False, vendor


@pytest.mark.parametrize(
    ("vendor", "message", "requested", "limit"),
    (
        ("nvidia_nim", _NIM_CONTEXT_OVERFLOW, "262294", "262144"),
        ("nous_portal", _NOUS_CONTEXT_OVERFLOW, "266577", "262144"),
        ("open_router", _OPENROUTER_CONTEXT_OVERFLOW, "256487", "256000"),
    ),
)
def test_a_context_overflow_names_both_numbers(
    vendor, message, requested, limit
) -> None:
    """ "Needed 256487, this model holds 256000" is the whole diagnosis."""
    failure = _classify(_http_status_error(400, message))

    assert f"Needed about {requested} tokens" in failure.message, vendor
    assert f"this model holds {limit}" in failure.message, vendor


def test_an_ordinary_malformed_request_still_ends_the_route() -> None:
    """The narrow match is the point: a real 400 must keep aborting the chain."""
    for error in (
        _openai_status_error(
            openai.BadRequestError,
            status_code=400,
            message="messages: field required",
        ),
        _http_status_error(400, "Unsupported value for parameter 'temperature'."),
    ):
        failure = _classify(error)
        assert failure.kind is FailureKind.INVALID_REQUEST
        assert failure.status_code == 400
        assert failure.retryable is False


def test_a_context_overflow_still_serializes_on_both_wire_protocols() -> None:
    """A new kind missing from a wire map is a KeyError at the commit boundary."""
    failure = _classify(_http_status_error(400, _OPENROUTER_CONTEXT_OVERFLOW))

    assert anthropic_error_type_for_failure(failure) == "invalid_request_error"
    assert anthropic_status_for_error_type("invalid_request_error") == 400
    assert openai_error_type_for_failure(failure) == "invalid_request_error"


# ---------------------------------------------------------------- zero waits
#
# "Retry-After: 0" says nothing to wait for. It used to flow straight into
# extend_reactive_block, which refuses durations <= 0, so a single 429
# carrying it exploded out of failure classification instead of producing
# one. Same for RATE_LIMIT_COOLDOWN_SECONDS=0 with no header present.


def _openai_rate_limit_error(headers: dict[str, str]) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(429, headers=headers, request=request)
    return openai.RateLimitError(
        "Too many requests",
        response=response,
        body={"error": {"message": "Too many requests"}},
    )


def _api_status_error_429(headers: dict[str, str]) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(429, headers=headers, request=request)
    return openai.APIStatusError(
        "stream embedded error",
        response=response,
        body={"error": {"message": "too many requests", "code": 429}},
    )


def _http_status_error_429(headers: dict[str, str]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(
        429,
        headers=headers,
        request=request,
        json={"error": {"message": "Too many requests"}},
    )
    return httpx.HTTPStatusError(
        "Too many requests", request=request, response=response
    )


@pytest.mark.parametrize(
    ("builder",),
    (
        pytest.param(_openai_rate_limit_error, id="openai_rate_limit_error"),
        pytest.param(_api_status_error_429, id="api_status_error_429"),
        pytest.param(_http_status_error_429, id="http_status_error"),
    ),
)
@pytest.mark.parametrize(
    ("header_name", "header_value"),
    (("retry-after", "0"), ("retry-after-ms", "0")),
)
def test_a_zero_retry_after_installs_no_reactive_block(
    builder: Callable[[dict[str, str]], Exception],
    header_name: str,
    header_value: str,
) -> None:
    """A zero-second upstream wait must classify cleanly and mark nothing."""
    marks: list[float] = []

    failure = classify_provider_failure(
        builder({header_name: header_value}),
        provider_name="ZERO",
        read_timeout_s=30.0,
        request_id=None,
        mark_rate_limited=marks.append,
    )

    assert failure.kind is FailureKind.RATE_LIMIT
    assert failure.status_code == 429
    assert marks == [], "a zero-second wait must install no reactive block"


def test_a_zero_configured_cooldown_installs_no_reactive_block() -> None:
    """RATE_LIMIT_COOLDOWN_SECONDS=0 is published as 'does not pause'.

    With no header to obey, the configured default went straight into
    extend_reactive_block and raised -- punishing exactly the operators who
    chose never to pause.
    """
    marks: list[float] = []

    failure = classify_provider_failure(
        _openai_rate_limit_error({}),
        provider_name="ZERO",
        read_timeout_s=30.0,
        request_id=None,
        mark_rate_limited=marks.append,
        cooldown_seconds=0.0,
    )

    assert failure.kind is FailureKind.RATE_LIMIT
    assert marks == []
