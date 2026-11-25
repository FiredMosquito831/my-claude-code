"""The OpenAI error envelope, shared by every OpenAI-compatible ingress.

``/v1/responses`` and ``/v1/chat/completions`` are different protocols that
report failure identically: one ``{"error": {...}}`` object whose ``type`` is
derived from MCC's neutral :class:`FailureKind`, never from an upstream SDK's
own vocabulary. Keeping the mapping in one place is what makes a rate limit
read the same to a Codex client and to an OpenAI-SDK client, which is the only
way a user comparing the two can conclude anything from the difference.
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.failures import ExecutionFailure, FailureKind

_FAILURE_ERROR_TYPES = {
    FailureKind.INVALID_REQUEST: "invalid_request_error",
    FailureKind.MODEL_REJECTED: "invalid_request_error",
    FailureKind.CONTEXT_LENGTH: "invalid_request_error",
    FailureKind.AUTHENTICATION: "invalid_request_error",
    FailureKind.PERMISSION: "permission_error",
    FailureKind.QUOTA: "insufficient_quota",
    FailureKind.RATE_LIMIT: "rate_limit_error",
    FailureKind.OVERLOADED: "server_error",
    FailureKind.TIMEOUT: "server_error",
    FailureKind.UPSTREAM: "server_error",
    FailureKind.UNAVAILABLE: "server_error",
    # A tier refusal is a permission answer, not an authentication one: the
    # credential proved who it was and the host still declined to serve this
    # model on it.
    FailureKind.FREE_TIER: "permission_error",
}

_FAILURE_ERROR_CODES = {
    FailureKind.CONTEXT_LENGTH: "context_length_exceeded",
    FailureKind.AUTHENTICATION: "invalid_api_key",
    FailureKind.QUOTA: "insufficient_quota",
    FailureKind.RATE_LIMIT: "rate_limit_exceeded",
    FailureKind.OVERLOADED: "server_error",
    FailureKind.TIMEOUT: "timeout",
    FailureKind.UPSTREAM: "server_error",
    FailureKind.UNAVAILABLE: "server_error",
}


def openai_error_type_for_failure(
    failure: FailureKind | ExecutionFailure,
) -> str:
    """Map neutral failure semantics to an OpenAI-documented error type."""
    if isinstance(failure, ExecutionFailure):
        if failure.kind == FailureKind.PERMISSION and failure.status_code == 402:
            return "insufficient_quota"
        kind = failure.kind
    else:
        kind = failure
    return _FAILURE_ERROR_TYPES[kind]


def openai_error_code_for_failure(
    failure: FailureKind | ExecutionFailure,
) -> str | None:
    """Map neutral failure semantics to a canonical OpenAI ``error.code``."""
    if isinstance(failure, ExecutionFailure):
        if failure.kind == FailureKind.PERMISSION and failure.status_code == 402:
            return "insufficient_quota"
        if failure.kind == FailureKind.INVALID_REQUEST and failure.status_code == 404:
            return "model_not_found"
        kind = failure.kind
    else:
        kind = failure
    return _FAILURE_ERROR_CODES.get(kind)


def normalize_anthropic_error_to_openai(
    error_type: str, raw_code: str | None = None
) -> tuple[str, str | None]:
    """Map an internal/Anthropic ``error.type`` onto OpenAI ``(type, code)``."""
    code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else None
    if error_type == "rate_limit_error":
        return "rate_limit_error", code or "rate_limit_exceeded"
    if error_type in {"billing_error", "insufficient_quota"}:
        return "insufficient_quota", code or "insufficient_quota"
    if error_type == "authentication_error":
        return "invalid_request_error", code or "invalid_api_key"
    if error_type == "not_found_error":
        return "invalid_request_error", code or "model_not_found"
    if error_type in {"request_too_large", "invalid_request_error"}:
        return "invalid_request_error", code
    if error_type == "permission_error":
        return "permission_error", code
    if error_type == "timeout_error":
        return "server_error", code or "timeout"
    if error_type in {"overloaded_error", "api_error", "server_error"}:
        return "server_error", code or "server_error"
    return error_type, code


def stream_error_fields(error: Mapping[str, object]) -> tuple[str, str, str | None]:
    """Extract ``(raw_type, message, raw_code)`` from an internal SSE error dict."""
    raw_type = error.get("type")
    error_type = (
        raw_type.strip()
        if isinstance(raw_type, str) and raw_type.strip()
        else "api_error"
    )
    raw_message = error.get("message")
    message = (
        raw_message.strip()
        if isinstance(raw_message, str) and raw_message.strip()
        else "Provider request failed unexpectedly."
    )
    raw_code = error.get("code")
    code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else None
    return error_type, message, code


def openai_error_payload(
    *,
    message: str,
    error_type: str,
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    """Return an OpenAI-compatible error envelope.

    ``param`` names the request field the error is about, as OpenAI's own
    ``invalid_request_error`` does; ``code`` defaults to ``None`` for direct
    validation helpers and is populated on failure/SSE error paths.
    """

    return {
        "error": {
            "message": redact_sensitive_error_text(message),
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def openai_error_from_failure(failure: ExecutionFailure) -> dict[str, Any]:
    """Return the inner OpenAI error object for a canonical failure."""
    return openai_error_payload(
        message=failure.message,
        error_type=openai_error_type_for_failure(failure),
        code=openai_error_code_for_failure(failure),
    )["error"]


def openai_error_from_anthropic_error(data: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an internal Anthropic SSE ``error`` payload to OpenAI error object."""
    error = data.get("error")
    if not isinstance(error, Mapping):
        raw_type = "api_error"
        message = str(data)
        raw_code: str | None = None
    else:
        raw_type_val = error.get("type")
        raw_type = (
            raw_type_val.strip()
            if isinstance(raw_type_val, str) and raw_type_val.strip()
            else "api_error"
        )
        raw_msg = error.get("message")
        message = "" if raw_msg is None else str(raw_msg)
        raw_code_val = error.get("code")
        raw_code = (
            raw_code_val.strip()
            if isinstance(raw_code_val, str) and raw_code_val.strip()
            else None
        )
    error_type, code = normalize_anthropic_error_to_openai(raw_type, raw_code)
    return openai_error_payload(message=message, error_type=error_type, code=code)[
        "error"
    ]


def openai_failure_payload(failure: ExecutionFailure) -> dict[str, Any]:
    """Return an OpenAI-compatible envelope for a canonical failure."""
    return {"error": openai_error_from_failure(failure)}
