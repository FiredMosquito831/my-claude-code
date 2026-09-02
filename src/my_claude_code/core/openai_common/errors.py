"""The OpenAI error envelope, shared by every OpenAI-compatible ingress.

``/v1/responses`` and ``/v1/chat/completions`` are different protocols that
report failure identically: one ``{"error": {...}}`` object whose ``type`` is
derived from MCC's neutral :class:`FailureKind`, never from an upstream SDK's
own vocabulary. Keeping the mapping in one place is what makes a rate limit
read the same to a Codex client and to an OpenAI-SDK client, which is the only
way a user comparing the two can conclude anything from the difference.
"""

from typing import Any

from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.failures import ExecutionFailure, FailureKind

_FAILURE_ERROR_TYPES = {
    FailureKind.INVALID_REQUEST: "invalid_request_error",
    FailureKind.CONTEXT_LENGTH: "invalid_request_error",
    FailureKind.AUTHENTICATION: "authentication_error",
    FailureKind.PERMISSION: "permission_error",
    FailureKind.RATE_LIMIT: "rate_limit_error",
    FailureKind.OVERLOADED: "overloaded_error",
    FailureKind.TIMEOUT: "api_error",
    FailureKind.UPSTREAM: "api_error",
    FailureKind.UNAVAILABLE: "api_error",
}


def openai_error_type_for_failure(
    failure: FailureKind | ExecutionFailure,
) -> str:
    """Map neutral failure semantics to an OpenAI-compatible error type."""
    if isinstance(failure, ExecutionFailure):
        if failure.kind == FailureKind.PERMISSION and failure.status_code == 402:
            return "billing_error"
        if failure.kind == FailureKind.INVALID_REQUEST:
            if failure.status_code == 404:
                return "not_found_error"
            if failure.status_code == 413:
                return "request_too_large"
        if failure.kind == FailureKind.TIMEOUT and failure.status_code == 504:
            return "timeout_error"
        kind = failure.kind
    else:
        kind = failure
    return _FAILURE_ERROR_TYPES[kind]


def openai_error_payload(*, message: str, error_type: str) -> dict[str, Any]:
    """Return an OpenAI-compatible error envelope."""

    return {
        "error": {
            "message": redact_sensitive_error_text(message),
            "type": error_type,
            "param": None,
            "code": None,
        }
    }


def openai_error_from_failure(failure: ExecutionFailure) -> dict[str, Any]:
    """Return the inner OpenAI error object for a canonical failure."""
    return openai_error_payload(
        message=failure.message,
        error_type=openai_error_type_for_failure(failure),
    )["error"]


def openai_failure_payload(failure: ExecutionFailure) -> dict[str, Any]:
    """Return an OpenAI-compatible envelope for a canonical failure."""
    return {"error": openai_error_from_failure(failure)}
