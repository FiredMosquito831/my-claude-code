"""The Google API error envelope, for MCC's Gemini-compatible ingress.

Google reports every failure as ``{"error": {"code": <http status>,
"message": "...", "status": "<CANONICAL_CODE>"}}`` and every client built on
``@google/genai`` or ``google-genai`` reads all three: the JS SDK raises an
``ApiError`` carrying ``status``, and Gemini CLI branches on the *string* when
it decides whether to retry, fall back or stop. A body missing ``status``
therefore does not merely read badly -- it changes what the client does.

The mapping is from MCC's neutral :class:`FailureKind`, never from an upstream
SDK's own vocabulary, exactly as ``core/openai_common/errors.py`` does for the
two OpenAI surfaces. That is what makes a rate limit read the same to a Gemini
client and to an OpenAI-SDK client.
"""

from typing import Any

from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.failures import ExecutionFailure, FailureKind

#: Neutral failure semantics -> Google's canonical error status. The names are
#: google.rpc.Code's, which is what Google's own HTTP mapping emits.
_FAILURE_STATUSES: dict[FailureKind, str] = {
    FailureKind.INVALID_REQUEST: "INVALID_ARGUMENT",
    FailureKind.CONTEXT_LENGTH: "INVALID_ARGUMENT",
    FailureKind.AUTHENTICATION: "UNAUTHENTICATED",
    FailureKind.PERMISSION: "PERMISSION_DENIED",
    FailureKind.QUOTA: "RESOURCE_EXHAUSTED",
    FailureKind.RATE_LIMIT: "RESOURCE_EXHAUSTED",
    FailureKind.OVERLOADED: "UNAVAILABLE",
    FailureKind.TIMEOUT: "DEADLINE_EXCEEDED",
    FailureKind.UPSTREAM: "INTERNAL",
    FailureKind.UNAVAILABLE: "UNAVAILABLE",
}

#: HTTP status -> canonical status, for the statuses this proxy answers with
#: that are not reachable through a :class:`FailureKind` at all (a missing
#: token, an unknown model path).
_STATUSES_BY_CODE: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    402: "RESOURCE_EXHAUSTED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    408: "DEADLINE_EXCEEDED",
    409: "ABORTED",
    413: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
    499: "CANCELLED",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}

_DEFAULT_STATUS = "UNKNOWN"


class GeminiConversionError(ValueError):
    """Raised when a ``generateContent`` request cannot be converted.

    Carries an optional ``field``: Google's own 400s name the offending field
    inside ``error.message``, and "which field" is the whole content of a 400
    to somebody wiring a new client up.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def gemini_status_for_code(status_code: int) -> str:
    """Return Google's canonical status string for one HTTP status."""

    return _STATUSES_BY_CODE.get(status_code, _DEFAULT_STATUS)


def gemini_status_for_failure(failure: FailureKind | ExecutionFailure) -> str:
    """Map neutral failure semantics to a Google canonical status.

    Two HTTP statuses refine what the kind alone says, the same two the OpenAI
    mapping refines: a 404 inside an invalid-request failure is a missing
    model rather than a malformed body, and a 402 inside a permission failure
    is billing, which Google spells ``FAILED_PRECONDITION`` because it has no
    billing status of its own.
    """

    if isinstance(failure, ExecutionFailure):
        if failure.kind == FailureKind.INVALID_REQUEST and failure.status_code == 404:
            return "NOT_FOUND"
        if failure.kind == FailureKind.PERMISSION and failure.status_code == 402:
            return "FAILED_PRECONDITION"
        kind = failure.kind
    else:
        kind = failure
    return _FAILURE_STATUSES[kind]


def gemini_error_payload(
    *, message: str, code: int, status: str | None = None
) -> dict[str, Any]:
    """Return a Google-compatible error envelope.

    ``code`` is the HTTP status repeated inside the body, which is what Google
    sends and what the SDKs read when the transport hid the status line.
    """

    return {
        "error": {
            "code": code,
            "message": redact_sensitive_error_text(message),
            "status": status or gemini_status_for_code(code),
        }
    }


def gemini_error_from_failure(failure: ExecutionFailure) -> dict[str, Any]:
    """Return the inner Google error object for a canonical failure."""

    return gemini_error_payload(
        message=failure.message,
        code=failure.status_code,
        status=gemini_status_for_failure(failure),
    )["error"]


def gemini_failure_payload(failure: ExecutionFailure) -> dict[str, Any]:
    """Return a Google-compatible envelope for a canonical failure."""

    return {"error": gemini_error_from_failure(failure)}
