"""Classify native Anthropic SSE error payloads into neutral failures."""

from collections.abc import Mapping

from my_claude_code.core.failures import ExecutionFailure, FailureKind


def anthropic_stream_failure(payload: Mapping[str, object] | None) -> ExecutionFailure:
    """Translate one native Anthropic error event without provider dependencies."""
    error = payload.get("error") if payload is not None else None
    error_type = error.get("type") if isinstance(error, Mapping) else None
    # Not every host puts an object under ``error``. HyperCharm answers a bad
    # key with ``{"error": "authentication failed"}`` -- a bare string -- while
    # spelling every other failure as ``{"error": {"message": ...}}``, and it
    # is not alone in that. Reading only the object shape threw the host's own
    # words away and replaced them with the generic fallback, which is the
    # difference between a user seeing "authentication failed" and seeing
    # nothing actionable at all. There is no ``type`` in that shape, so the
    # kind still falls through to the UPSTREAM default; only the message is
    # recovered, which is exactly as much as the host said.
    if isinstance(error, str):
        message: object | None = error
    else:
        message = error.get("message") if isinstance(error, Mapping) else None
    stripped = message.strip() if isinstance(message, str) else ""
    safe_message = stripped or "Provider stream failed."
    kind, status, retryable = {
        "authentication_error": (FailureKind.AUTHENTICATION, 401, False),
        "permission_error": (FailureKind.PERMISSION, 403, False),
        "invalid_request_error": (FailureKind.INVALID_REQUEST, 400, False),
        "rate_limit_error": (FailureKind.RATE_LIMIT, 429, True),
        "overloaded_error": (FailureKind.OVERLOADED, 529, True),
    }.get(error_type, (FailureKind.UPSTREAM, 502, True))
    return ExecutionFailure(
        kind=kind,
        status_code=status,
        message=safe_message,
        retryable=retryable,
    )
