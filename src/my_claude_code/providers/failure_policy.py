"""Provider-owned SDK classification and retry qualification."""

import json
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

import httpx
import openai

from my_claude_code.core.diagnostics import (
    extract_upstream_error_detail,
    format_execution_failure_message,
    safe_exception_message,
)
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    find_execution_failure,
)
from my_claude_code.core.rate_limit import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
    retry_after_seconds,
)
from my_claude_code.providers.recovery import upstream_complaint

MarkRateLimited = Callable[[float], None]
ProviderFailureOverride = Callable[[Exception], ExecutionFailure | None]

_RATE_LIMIT_MARKERS = frozenset({"rate_limit", "rate limit", "too many requests"})
_OVERLOAD_MARKERS = frozenset(
    {
        "resourceexhausted",
        "resource exhausted",
        "limit reached",
        "overloaded",
        "capacity",
    }
)
_INTERNAL_ERROR_MARKERS = frozenset({"internal_server_error", "internal server error"})
_AUTHENTICATION_MESSAGE = "Provider authentication failed. Check API key."
_RATE_LIMIT_MESSAGE = "Provider rate limit reached. Please retry shortly."
_INVALID_REQUEST_MESSAGE = "Invalid request sent to provider."
_CONTEXT_LENGTH_MESSAGE = "Request exceeds this model's context window."
# Substrings that hold across every vendor wording measured in production:
# NVIDIA NIM "This model's maximum context length is ...", Nous Portal and
# OpenRouter "This endpoint's maximum context length is ...", plus the
# OpenAI-compatible machine code. Deliberately narrow: a false positive here
# would retry a genuinely malformed body on every model in the chain, so
# anything not clearly a window overflow stays INVALID_REQUEST.
_CONTEXT_LENGTH_MARKERS = frozenset(
    {
        "maximum context length",
        "context_length_exceeded",
        "context length exceeded",
        "exceeds the maximum context",
    }
)
_CONTEXT_LIMIT_PATTERN = re.compile(r"maximum context length is\s+(\d+)")
# "you requested about 256487 tokens" (OpenRouter, Nous) and "your messages
# resulted in 262294 tokens" (NVIDIA NIM).
_CONTEXT_REQUESTED_PATTERN = re.compile(
    r"(?:you requested about|resulted in)\s+(\d+)\s+tokens"
)
_OVERLOADED_MESSAGE = "Provider is currently overloaded. Please retry."

_QUOTA_MESSAGE = "Provider account is out of credits."
#: HTTP statuses on which a billing phrase is read as a quota failure. ``402``
#: is the status that *means* it and needs no phrase; ``400`` and ``403`` are
#: what the fleet actually sends -- Command Code answers an out-of-credit
#: account with a ``400`` whose body says ``invalid_request_error``, and the
#: request was never the problem.
_QUOTA_PHRASE_STATUSES = frozenset({400, 403})
_QUOTA_STATUS = 402

#: Explicit billing phrases, measured from the fleet. Phrases, never bare
#: words: "credit" alone appears in "credit card required for this model" and
#: in a prompt echoed back, and a false positive here would rotate a whole key
#: pool and bench every key on a request that was merely malformed. Each entry
#: below is a full statement about the *account*, not about the request.
#:
#: * ``insufficient credits`` / ``purchase more credits`` -- Command Code and
#:   the OpenAI-compatible gateways behind it (the 6.34.0 evidence).
#: * ``insufficient balance`` / ``not enough balance`` / ``not_enough_balance``
#:   -- Novita, which answers with the machine code in ``code``.
#: * ``quota exceeded`` / ``insufficient_quota`` -- the OpenAI-compatible
#:   machine codes.
#: * ``payment required`` / ``billing`` -- the generic gateway wordings.
#: * ``out of credits`` / ``credit balance is too low`` -- Anthropic's own.
#: * ``creditserror`` -- OpenCode Go's error class name, lowercased.
QUOTA_PHRASES: tuple[str, ...] = (
    "insufficient credits",
    "purchase more credits",
    "insufficient balance",
    "not enough balance",
    "not_enough_balance",
    "quota exceeded",
    "insufficient_quota",
    "payment required",
    "billing",
    "out of credits",
    "credit balance is too low",
    "creditserror",
)


def quota_phrase(exc: BaseException) -> str | None:
    """The billing phrase the upstream named, or ``None`` if it named none.

    Read through :func:`~my_claude_code.providers.recovery.complaint.upstream_complaint`
    and nothing else: that reader prefers the *structured* error body and prunes
    the ``input``/``body``/``ctx`` keys under which a validation error echoes
    the submitted request back. A prompt containing the words "insufficient
    credits" is not an account out of credits, and reading the raw response
    text is exactly how it would become one.
    """
    if not isinstance(exc, Exception):
        return None
    complaint = upstream_complaint(exc)
    for phrase in QUOTA_PHRASES:
        if phrase in complaint:
            return phrase
    return None


def is_quota_error(exc: BaseException) -> bool:
    """Whether an upstream rejection is about the account's credits.

    Two shapes, and only two. A ``402`` says so by status alone. A ``400`` or
    a ``403`` says so only when the structured body names an explicit billing
    phrase -- if it does not, this answers ``False`` and the failure keeps the
    kind it has today. Unsure never means ``QUOTA``.
    """
    status = _quota_status(exc)
    if status == _QUOTA_STATUS:
        return True
    return status in _QUOTA_PHRASE_STATUSES and quota_phrase(exc) is not None


def quota_failure(
    exc: BaseException,
    cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
) -> ExecutionFailure:
    """Return the canonical credits-exhausted failure.

    ``retryable`` is False -- the same credential meets the same empty balance
    -- but the kind is deliberately *not* in ``FALLBACK_SKIP_KINDS``: another
    key may have credits, and behind the pool another model may be free. The
    route must keep going.

    ``retry_after_seconds`` carries the operator's ``RATE_LIMIT_COOLDOWN_SECONDS``
    only when a phrase was matched. A bare ``402`` with no recognisable wording
    is evidence enough to rotate and to fall through, and not evidence enough
    to take a credential out of the pool.
    """
    phrase = quota_phrase(exc)
    status = _quota_status(exc) or _QUOTA_STATUS
    message = _QUOTA_MESSAGE if phrase is None else f"{_QUOTA_MESSAGE} ({phrase})"
    return _failure(
        FailureKind.QUOTA,
        status,
        message,
        False,
        cooldown_seconds if phrase is not None else None,
    )


def _quota_status(exc: BaseException) -> int | None:
    """The status behind a possible quota failure, across error carriers."""
    if isinstance(exc, ExecutionFailure):
        return exc.status_code
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify_provider_failure(
    exc: Exception,
    *,
    provider_name: str,
    read_timeout_s: float | None,
    request_id: str | None,
    mark_rate_limited: MarkRateLimited,
    provider_failure_override: ProviderFailureOverride | None = None,
    cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    mark_rate_limited_enabled: bool = True,
) -> ExecutionFailure:
    """Return one detailed canonical failure after provider retries are exhausted.

    ``mark_rate_limited_enabled`` is False when the pool routes around a
    rate-limited model. The rotation engine's (key, model) bench is then the
    single record of that cooldown; installing a provider-wide reactive block
    as well spends the same sixty seconds twice -- once as a bench the router
    can step over, once as a sleep inside the credential that no router can
    see. Defaults True, so a caller that has no opinion keeps 6.19.0.
    """
    if isinstance(exc, ExecutionFailure):
        failure = exc
        message = failure.message
        request_id_line = f"Request ID: {request_id}" if request_id else None
        if request_id_line and request_id_line not in message:
            message = f"{message}\n\n{request_id_line}"
        return replace(failure, message=message)

    failure = (
        provider_failure_override(exc)
        if provider_failure_override is not None
        else None
    )
    if failure is None:
        failure = _classify_provider_failure(
            exc,
            read_timeout_s=read_timeout_s,
            mark_rate_limited=mark_rate_limited,
            cooldown_seconds=cooldown_seconds,
            mark_rate_limited_enabled=mark_rate_limited_enabled,
        )
    message = format_execution_failure_message(
        failure,
        extract_upstream_error_detail(exc),
        upstream_name=provider_name,
        request_id=request_id,
    )
    return replace(failure, message=message)


def overloaded_provider_failure() -> ExecutionFailure:
    """Return the canonical provider-overload meaning and stable wording."""
    return _failure(FailureKind.OVERLOADED, 529, _OVERLOADED_MESSAGE, True)


def retryable_transient_status(exc: BaseException) -> int | None:
    """Infer a retryable HTTP-like status from one upstream exception."""
    if isinstance(exc, ExecutionFailure):
        status = exc.status_code
        return status if exc.retryable and _is_retryable_status(status) else None
    if isinstance(exc, openai.RateLimitError):
        return 429
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status if _is_retryable_status(status) else None

    status = _status_from_exception(exc)
    if _is_retryable_status(status):
        return status

    body_status = _status_from_body(getattr(exc, "body", None))
    if _is_retryable_status(body_status):
        return body_status

    text = transient_error_text(exc)
    if _has_marker(text, _RATE_LIMIT_MARKERS):
        return 429
    if _has_marker(text, _OVERLOAD_MARKERS):
        return 503
    if _has_marker(text, _INTERNAL_ERROR_MARKERS):
        return 500
    return None


def is_transient_overload_error(exc: BaseException) -> bool:
    """Return whether an upstream exception reports overload or capacity pressure."""
    if isinstance(exc, ExecutionFailure):
        return exc.kind == FailureKind.OVERLOADED
    return _has_marker(transient_error_text(exc), _OVERLOAD_MARKERS)


def transient_error_text(exc: BaseException) -> str:
    """Combine exception, body, and response text for provider classification."""
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(_body_to_text(body))
    response = getattr(exc, "response", None)
    if response is not None:
        with suppress(Exception):
            parts.append(response.text)
    return " ".join(part for part in parts if part).lower()


def is_retryable_provider_error(exc: BaseException) -> bool:
    """Return whether provider policy permits stream retry or recovery."""
    if isinstance(exc, ExecutionFailure):
        return exc.retryable
    if isinstance(exc, openai.AuthenticationError | openai.BadRequestError):
        return False
    if retryable_transient_status(exc) is not None:
        return True
    return isinstance(
        exc,
        (
            TimeoutError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
            openai.APITimeoutError,
            openai.APIConnectionError,
        ),
    )


def retryable_upstream_status(exc: BaseException) -> int | None:
    """Return a status eligible for provider-opening backoff."""
    status = retryable_transient_status(exc)
    return status if status is not None and _is_retryable_status(status) else None


def upstream_status(exc: BaseException) -> int | None:
    """The status the upstream actually returned, retryable or not.

    :func:`retryable_upstream_status` deliberately answers ``None`` for a 400
    or a 401 -- it is a retry gate, not an observation. The retry ladder
    records what happened rather than what to do about it, so it asks this.
    """
    failure = find_execution_failure(exc)
    if failure is not None and failure.status_code:
        return failure.status_code
    status = _status_from_exception(exc)
    if status is not None:
        return status
    response = getattr(exc, "response", None)
    response_status = (
        getattr(response, "status_code", None) if response is not None else None
    )
    if isinstance(response_status, int):
        return response_status
    return _status_from_body(getattr(exc, "body", None))


def retryable_upstream_transport_error(exc: BaseException) -> bool:
    """Return whether a pre-response transport failure can be retried."""
    if isinstance(exc, ExecutionFailure):
        return exc.retryable and retryable_transient_status(exc) is None
    if isinstance(exc, openai.AuthenticationError | openai.BadRequestError):
        return False
    return isinstance(
        exc,
        (
            TimeoutError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
            openai.APITimeoutError,
            openai.APIConnectionError,
        ),
    )


def provider_error_message(
    exc: BaseException,
    *,
    read_timeout_s: float | None = None,
) -> str:
    """Map raw provider exception types to stable customer-facing wording."""
    if isinstance(exc, ExecutionFailure):
        return exc.message
    if isinstance(exc, httpx.ReadTimeout):
        if read_timeout_s is not None:
            return f"Provider request timed out after {read_timeout_s:g}s."
        return "Provider request timed out."
    if isinstance(exc, httpx.ConnectTimeout | httpx.ConnectError):
        return "Could not connect to provider."
    if isinstance(exc, httpx.RemoteProtocolError):
        return "Provider connection was interrupted before a response was received."
    if isinstance(exc, TimeoutError):
        if read_timeout_s is not None:
            return f"Provider request timed out after {read_timeout_s:g}s."
        return "Request timed out."
    if isinstance(exc, openai.RateLimitError):
        return _RATE_LIMIT_MESSAGE
    if isinstance(exc, openai.AuthenticationError):
        return _AUTHENTICATION_MESSAGE
    if isinstance(exc, openai.BadRequestError):
        return _INVALID_REQUEST_MESSAGE
    return safe_exception_message(exc)


def rate_limit_cooldown_seconds(
    exc: BaseException, default_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
) -> float:
    """How long the upstream says to wait, or a conservative default.

    Guessing a fixed minute either wastes a credential that resets in one
    second or hammers one that needs an hour. Providers publish the real reset
    on every 429, so use it when present.
    """
    seconds = retry_after_from_error(exc)
    if seconds is None:
        return default_seconds
    return seconds


def retry_after_from_error(exc: BaseException) -> float | None:
    """The wait the upstream itself published, or ``None`` when it published none.

    Deliberately separate from :func:`rate_limit_cooldown_seconds`: that one
    always answers with a number, so a caller cannot tell "the server told us
    7s" from "we fell back to the configured default". Everything that benches
    a credential or a route needs that distinction -- the provider's number is
    authoritative, ours is only a stand-in -- so this returns the header value
    alone, capped at the one-hour sanity bound a single header may request.
    """
    response = getattr(exc, "response", None)
    seconds = retry_after_seconds(getattr(response, "headers", None))
    if seconds is None:
        return None
    return min(seconds, MAX_RATE_LIMIT_COOLDOWN_SECONDS)


def is_context_length_error(exc: BaseException) -> bool:
    """Whether a 400 means the body outgrew the model's context window.

    This is the one 400 a fallback chain can fix: the same body that overflows
    a 256k window fits a 1M one, so it must not be classified as the malformed
    request that ends the route.
    """
    return _has_marker(transient_error_text(exc), _CONTEXT_LENGTH_MARKERS)


def context_length_failure(exc: BaseException) -> ExecutionFailure:
    """Return the canonical context-overflow failure, naming the numbers if given.

    ``retryable`` stays False: it means "safe to retry the same credential",
    and the same model rejects the same body again. Only the chain helps.
    """
    text = transient_error_text(exc)
    limit = _CONTEXT_LIMIT_PATTERN.search(text)
    requested = _CONTEXT_REQUESTED_PATTERN.search(text)
    message = _CONTEXT_LENGTH_MESSAGE
    if limit and requested:
        message = (
            f"{_CONTEXT_LENGTH_MESSAGE} Needed about {requested.group(1)} tokens; "
            f"this model holds {limit.group(1)}."
        )
    return _failure(FailureKind.CONTEXT_LENGTH, 400, message, False)


def _mark_rate_limited_when_positive(
    mark_rate_limited: MarkRateLimited, seconds: float
) -> None:
    """Install the reactive block only for a real wait.

    ``extend_reactive_block`` refuses durations <= 0, and upstreams do send
    ``Retry-After: 0`` -- meaning "nothing to wait for", not "misconfigured".
    Skip the mark instead of crashing out of classification.
    """

    if seconds > 0:
        mark_rate_limited(seconds)


def _classify_provider_failure(
    exc: Exception,
    *,
    read_timeout_s: float | None,
    mark_rate_limited: MarkRateLimited,
    cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    mark_rate_limited_enabled: bool = True,
) -> ExecutionFailure:
    if isinstance(exc, ExecutionFailure):
        if exc.kind == FailureKind.RATE_LIMIT:
            if mark_rate_limited_enabled:
                _mark_rate_limited_when_positive(
                    mark_rate_limited,
                    rate_limit_cooldown_seconds(exc, cooldown_seconds),
                )
            published = retry_after_from_error(exc)
            if exc.retry_after_seconds is None and published is not None:
                # ExecutionFailure is frozen by design, so carry the header
                # forward on a new one rather than mutating this one.
                return _failure(
                    exc.kind, exc.status_code, exc.message, exc.retryable, published
                )
        return exc

    if is_quota_error(exc):
        # Before every status branch below: an account out of credits is
        # answered as a 400, a 402 or a 403 depending on the gateway, and only
        # the phrase tells the three apart from a malformed body and a revoked
        # key. Charging a key's lockout ladder for an empty wallet is what the
        # 401/403 branch would otherwise do.
        return quota_failure(exc, cooldown_seconds)
    if isinstance(exc, openai.AuthenticationError):
        return _failure(FailureKind.AUTHENTICATION, 401, _AUTHENTICATION_MESSAGE, False)
    if isinstance(exc, openai.RateLimitError):
        if mark_rate_limited_enabled:
            _mark_rate_limited_when_positive(
                mark_rate_limited,
                rate_limit_cooldown_seconds(exc, cooldown_seconds),
            )
        return _failure(
            FailureKind.RATE_LIMIT,
            429,
            _RATE_LIMIT_MESSAGE,
            True,
            retry_after_from_error(exc),
        )
    if isinstance(exc, openai.BadRequestError):
        if is_context_length_error(exc):
            return context_length_failure(exc)
        return _failure(
            FailureKind.INVALID_REQUEST, 400, _INVALID_REQUEST_MESSAGE, False
        )
    if isinstance(exc, openai.APITimeoutError):
        return _failure(FailureKind.TIMEOUT, 500, _stable_upstream(500), True)
    if isinstance(exc, openai.APIConnectionError):
        return _failure(FailureKind.UNAVAILABLE, 500, _stable_upstream(500), True)
    if isinstance(exc, openai.InternalServerError):
        status = retryable_transient_status(exc) or getattr(exc, "status_code", None)
        if is_transient_overload_error(exc):
            return overloaded_provider_failure()
        if isinstance(status, int) and 500 <= status <= 599:
            return _failure(
                FailureKind.UPSTREAM,
                status,
                _stable_upstream(status),
                True,
            )
        return _failure(FailureKind.UPSTREAM, 500, _stable_upstream(500), True)
    if isinstance(exc, openai.APIError):
        status = retryable_transient_status(exc)
        if status == 429:
            _mark_rate_limited_when_positive(
                mark_rate_limited,
                rate_limit_cooldown_seconds(exc, cooldown_seconds),
            )
            return _failure(
                FailureKind.RATE_LIMIT,
                429,
                _RATE_LIMIT_MESSAGE,
                True,
                retry_after_from_error(exc),
            )
        if is_transient_overload_error(exc):
            return overloaded_provider_failure()
        effective_status = status or getattr(exc, "status_code", None)
        if not isinstance(effective_status, int):
            effective_status = 500
        return _failure(
            FailureKind.UPSTREAM,
            effective_status,
            _stable_upstream(effective_status),
            is_retryable_provider_error(exc),
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return _failure(
                FailureKind.AUTHENTICATION, 401, _AUTHENTICATION_MESSAGE, False
            )
        if status == 429:
            _mark_rate_limited_when_positive(
                mark_rate_limited,
                rate_limit_cooldown_seconds(exc, cooldown_seconds),
            )
            return _failure(
                FailureKind.RATE_LIMIT,
                429,
                _RATE_LIMIT_MESSAGE,
                True,
                retry_after_from_error(exc),
            )
        if status == 400:
            if is_context_length_error(exc):
                return context_length_failure(exc)
            return _failure(
                FailureKind.INVALID_REQUEST, 400, _INVALID_REQUEST_MESSAGE, False
            )
        if status in (502, 503, 504):
            return overloaded_provider_failure()
        return _failure(
            FailureKind.UPSTREAM,
            status,
            _stable_upstream(status),
            _is_retryable_status(status),
        )

    kind = FailureKind.UPSTREAM
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        kind = FailureKind.TIMEOUT
    elif isinstance(exc, httpx.ConnectError | httpx.NetworkError):
        kind = FailureKind.UNAVAILABLE
    return _failure(
        kind,
        502,
        provider_error_message(exc, read_timeout_s=read_timeout_s),
        is_retryable_provider_error(exc),
    )


def _failure(
    kind: FailureKind,
    status_code: int,
    message: str,
    retryable: bool,
    retry_after_seconds: float | None = None,
) -> ExecutionFailure:
    return ExecutionFailure(
        kind=kind,
        status_code=status_code,
        message=message,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


def _stable_upstream(status_code: int) -> str:
    if status_code in (502, 503, 504):
        return "Provider is temporarily unavailable. Please retry."
    return "Provider API request failed."


def _status_from_exception(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _status_from_body(body: Any) -> int | None:
    for item in _body_candidates(body):
        if not isinstance(item, Mapping):
            continue
        for key in ("status", "status_code", "code"):
            status = _coerce_status(item.get(key))
            if status is not None:
                return status
        type_status = _status_from_type_fields(item)
        if type_status is not None:
            return type_status
    return None


def _body_candidates(body: Any) -> tuple[Any, ...]:
    if isinstance(body, str):
        try:
            return _body_candidates(json.loads(body))
        except ValueError:
            return (body,)
    if isinstance(body, bytes):
        return _body_candidates(body.decode("utf-8", errors="replace"))
    if isinstance(body, Mapping):
        nested = body.get("error")
        return (body, nested) if isinstance(nested, Mapping) else (body,)
    return (body,)


def _coerce_status(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _status_from_type_fields(item: Mapping[str, Any]) -> int | None:
    values = [
        value.lower()
        for key in ("type", "code")
        if isinstance((value := item.get(key)), str)
    ]
    text = " ".join(values)
    if _has_marker(text, _RATE_LIMIT_MARKERS):
        return 429
    if _has_marker(text, _OVERLOAD_MARKERS):
        return 503
    if _has_marker(text, _INTERNAL_ERROR_MARKERS):
        return 500
    return None


def _body_to_text(body: Any) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(body)


def _has_marker(text: str, markers: frozenset[str]) -> bool:
    return any(marker in text for marker in markers)


def _is_retryable_status(status: int | None) -> bool:
    return isinstance(status, int) and (status == 429 or 500 <= status <= 599)
