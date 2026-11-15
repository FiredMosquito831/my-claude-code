"""The refusal classifier: a 200 and a 403 that both mean "this client is refused".

Two shapes are byte-documented in the reference code and both are classified
here, from **structured fields only** never from a phrase (WORKING-NOTES IX.7:
"``billing`` as a bare word is not a phrase"):

* ``loadCodeAssist`` answers **HTTP 200** with ``allowedTiers`` present,
  **``currentTier`` absent**, and ``ineligibleTiers[].reasonCode ==
  "UNSUPPORTED_CLIENT"``. A client that checks only the status code reads
  success and fails later with the wrong error.
* ``streamGenerateContent``/``generateContent`` answers **403
  ``PERMISSION_DENIED``** with ``error.details[].reason == "TOS_VIOLATION"``
  (the ``google.rpc.ErrorInfo`` shape), optionally carrying
  ``metadata.appeal_url`` / ``appeal_url_link_text``.

Both map to :data:`~my_claude_code.core.failures.FailureKind.FREE_TIER`:
402 internally (so the credential pool charges no 403 lockout --
``providers/failure_policy.py:712-721``), ``permission_error``/403 on the wire
(`core/anthropic/errors.py:39`), ``retryable=False``, and the chain falls
through to the next model. ``FREE_TIER`` is deliberately **not** in
``CREDENTIAL_SHAPED_KINDS`` — the account is healthy and benching it is a false
conviction.

The wording names the real cause. It never says "top up", "wait", "check the
key" or "try again": none of those helps, and the first would be a lie.
"""

from typing import Any

from my_claude_code.core.failures import ExecutionFailure, FailureKind

from .constants import (
    APPEAL_URL_KEY,
    APPEAL_URL_LINK_TEXT_KEY,
    GOOGLE_ERROR_INFO_TYPE,
    REASON_SUBSCRIPTION_REQUIRED,
    REASON_TOS_VIOLATION,
    REASON_UNSUPPORTED_CLIENT,
    STATUS_PERMISSION_DENIED,
)

#: What a refusal is about, in one declared word. The card and the log read
#: this rather than matching on a message.
REFUSAL_UNSUPPORTED_CLIENT = "unsupported_client"
REFUSAL_TOS_VIOLATION = "tos_violation"
REFUSAL_SUBSCRIPTION_REQUIRED = "subscription_required"

_UNSUPPORTED_CLIENT_MESSAGE = (
    "Google refused this client identity as unsupported "
    "(UNSUPPORTED_CLIENT). The account itself is fine; the client Antigravity "
    "expects to see is a different one, so switching accounts or signing in "
    "again cannot help. Update MCC or choose another identity profile."
)
_TOS_MESSAGE = (
    "Google refused this account for using a client it does not support "
    "(TOS_VIOLATION). The account is not out of quota and the credential is "
    "valid; retrying, switching accounts, or signing in again cannot help."
)
_SUBSCRIPTION_MESSAGE = (
    "This account has no Antigravity entitlement (SUBSCRIPTION_REQUIRED). "
    "The credential is valid."
)


class AntigravityRefusal(Exception):
    """A classified policy refusal, carrying its own honest failure.

    Raised by the provider at the point the refusal is read, so the shared
    classifier's provider override can recognise it even after it has travelled
    through the transport layer.
    """

    def __init__(
        self,
        *,
        kind: str,
        status_code: int,
        message: str,
        appeal_url: str = "",
        appeal_url_link_text: str = "",
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.appeal_url = appeal_url
        self.appeal_url_link_text = appeal_url_link_text
        self.message = message
        super().__init__(message)

    def failure(self) -> ExecutionFailure:
        """The canonical failure: FREE_TIER, 402 internally, never retryable."""

        return ExecutionFailure(
            kind=FailureKind.FREE_TIER,
            status_code=402,
            message=self.message,
            retryable=False,
        )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    error = _as_dict(payload.get("error"))
    details = error.get("details")
    if not isinstance(details, list):
        return []
    return [detail for detail in details if isinstance(detail, dict)]


def _error_info(detail: dict[str, Any]) -> dict[str, Any]:
    """The ``ErrorInfo`` payload, whether inlined or nested under ``metadata``."""

    if str(detail.get("@type") or "") == GOOGLE_ERROR_INFO_TYPE or "reason" in detail:
        return detail
    return detail


def appeal_url_of(detail: dict[str, Any]) -> tuple[str, str]:
    """``(appeal_url, appeal_url_link_text)`` off one ``ErrorInfo`` detail."""

    metadata = _as_dict(detail.get("metadata"))
    url = metadata.get(APPEAL_URL_KEY)
    text = metadata.get(APPEAL_URL_LINK_TEXT_KEY)
    return (
        url if isinstance(url, str) else "",
        text if isinstance(text, str) else "",
    )


def refusal_from_load_code_assist(payload: Any) -> AntigravityRefusal | None:
    """The 200-that-means-refused, or ``None`` when the answer is genuinely fine.

    The discriminator is the structured pair the spec records: **no
    ``currentTier``** together with an ``ineligibleTiers`` entry whose
    ``reasonCode`` is ``UNSUPPORTED_CLIENT``. A 200 that merely lacks a project
    is not a refusal — it triggers onboarding.
    """

    document = _as_dict(payload)
    if document.get("currentTier"):
        return None
    ineligible = document.get("ineligibleTiers")
    if not isinstance(ineligible, list):
        return None
    for entry in ineligible:
        candidate = _as_dict(entry)
        if str(candidate.get("reasonCode") or "") != REASON_UNSUPPORTED_CLIENT:
            continue
        return AntigravityRefusal(
            kind=REFUSAL_UNSUPPORTED_CLIENT,
            status_code=200,
            message=_UNSUPPORTED_CLIENT_MESSAGE,
            appeal_url="",
        )
    return None


def refusal_from_error_response(
    status_code: int, payload: Any
) -> AntigravityRefusal | None:
    """A generate-side refusal read from a structured error body, or ``None``.

    Only the two measured identifiers match. A non-TOS 403 and any 429 must
    fall through to the ordinary classification — the reference's own test
    asserts exactly that.
    """

    document = _as_dict(payload)
    error = _as_dict(document.get("error"))
    status_word = str(error.get("status") or "")
    for detail in _details(document):
        reason = str(detail.get("reason") or "")
        if reason == REASON_TOS_VIOLATION and (
            status_code == 403 or status_word == STATUS_PERMISSION_DENIED
        ):
            appeal_url, appeal_text = appeal_url_of(detail)
            message = _TOS_MESSAGE
            if appeal_url:
                label = appeal_text or "appeal"
                message = f"{message} {label}: {appeal_url}"
            return AntigravityRefusal(
                kind=REFUSAL_TOS_VIOLATION,
                status_code=status_code,
                message=message,
                appeal_url=appeal_url,
                appeal_url_link_text=appeal_text,
            )
        if reason == REASON_SUBSCRIPTION_REQUIRED and status_code == 403:
            return AntigravityRefusal(
                kind=REFUSAL_SUBSCRIPTION_REQUIRED,
                status_code=status_code,
                message=_SUBSCRIPTION_MESSAGE,
            )
    return None


def is_refusal_failure(failure: ExecutionFailure | None) -> bool:
    """Whether a failure is one of this module's policy refusals."""

    return failure is not None and failure.kind == FailureKind.FREE_TIER


def classify_antigravity_failure(error: Exception) -> ExecutionFailure | None:
    """The provider's failure override for policy refusals.

    Returns ``None`` for everything else, so the shared classifier keeps
    answering credential and transport problems exactly as it does for every
    other provider. The difference is the proof: fed the same 403 body, the
    generic path yields ``AUTHENTICATION`` and this one yields ``FREE_TIER``
    with no credential charge.
    """

    if isinstance(error, AntigravityRefusal):
        return error.failure()
    return None


__all__ = [
    "REFUSAL_SUBSCRIPTION_REQUIRED",
    "REFUSAL_TOS_VIOLATION",
    "REFUSAL_UNSUPPORTED_CLIENT",
    "AntigravityRefusal",
    "appeal_url_of",
    "classify_antigravity_failure",
    "is_refusal_failure",
    "refusal_from_error_response",
    "refusal_from_load_code_assist",
]
