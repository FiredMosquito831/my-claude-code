"""Canonical, protocol-neutral execution failure contracts."""

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

from my_claude_code.application.errors import (
    ApplicationUnavailableError,
    InvalidRequestError,
    UnknownProviderError,
)
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    failure_kind_name,
    find_execution_failure,
    parse_failure_kinds,
)


def test_failure_kind_has_only_protocol_neutral_semantics() -> None:
    assert tuple(FailureKind) == (
        FailureKind.INVALID_REQUEST,
        FailureKind.CONTEXT_LENGTH,
        FailureKind.AUTHENTICATION,
        FailureKind.PERMISSION,
        FailureKind.RATE_LIMIT,
        FailureKind.OVERLOADED,
        FailureKind.TIMEOUT,
        FailureKind.UPSTREAM,
        FailureKind.UNAVAILABLE,
    )
    assert tuple(kind.value for kind in FailureKind) == (
        "invalid_request",
        "context_length",
        "authentication",
        "permission",
        "rate_limit",
        "overloaded",
        "timeout",
        "upstream",
        "unavailable",
    )


def test_execution_failure_is_the_direct_frozen_slotted_exception() -> None:
    failure = ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="Provider rate limit reached.",
        retryable=True,
    )

    assert is_dataclass(failure)
    # ``retry_after_seconds`` carries the window a rate-limiting upstream
    # published for itself, so a bench downstream uses the provider's number
    # instead of one this stack invented. Optional, and None on every other
    # kind, so the four required fields keep their positions.
    assert tuple(field.name for field in fields(failure)) == (
        "kind",
        "status_code",
        "message",
        "retryable",
        "retry_after_seconds",
    )
    assert ExecutionFailure.__slots__ == (
        "kind",
        "status_code",
        "message",
        "retryable",
        "retry_after_seconds",
    )
    assert failure.retry_after_seconds is None
    assert str(failure) == "Provider rate limit reached."
    assert failure.args == ("Provider rate limit reached.",)

    with pytest.raises(ExecutionFailure) as raised:
        raise failure

    assert raised.value is failure
    with pytest.raises(FrozenInstanceError):
        failure.status_code = 500


def test_execution_failure_uses_exception_identity_not_value_equality() -> None:
    first = ExecutionFailure(
        kind=FailureKind.UPSTREAM,
        status_code=500,
        message="same",
        retryable=True,
    )
    second = ExecutionFailure(
        kind=FailureKind.UPSTREAM,
        status_code=500,
        message="same",
        retryable=True,
    )

    assert first is not second
    assert first != second


def test_find_execution_failure_recurses_through_nested_groups() -> None:
    failure = ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="provider is busy",
        retryable=True,
    )
    grouped = ExceptionGroup(
        "stream and cleanup failed",
        [
            RuntimeError("cleanup failed"),
            ExceptionGroup("provider failed", [failure]),
        ],
    )

    assert find_execution_failure(failure) is failure
    assert find_execution_failure(grouped) is failure


def test_find_execution_failure_leaves_unrelated_groups_unclassified() -> None:
    grouped = BaseExceptionGroup(
        "unrelated failures",
        [RuntimeError("socket closed"), KeyboardInterrupt()],
    )

    assert find_execution_failure(grouped) is None


# ---------------------------------------------------------- one vocabulary --


def test_an_application_error_is_named_by_its_kind_not_its_class():
    """`error_kind` mixed two vocabularies for the same failure.

    An ApplicationError carries a FailureKind as a class attribute, but only
    ExecutionFailure was ever read -- so the request log recorded `timeout` and
    `rate_limit` alongside `InvalidRequestError` and
    `ApplicationUnavailableError`, and grouping by the column split the same
    failure across two spellings.
    """
    assert failure_kind_name(InvalidRequestError("bad body")) == "invalid_request"
    assert failure_kind_name(ApplicationUnavailableError("no runtime")) == "unavailable"
    assert (
        failure_kind_name(UnknownProviderError.for_provider("nope", ["groq"]))
        == "invalid_request"
    )


def test_an_execution_failure_still_wins_and_is_found_inside_a_group():
    """The provider's own classification remains the most specific answer."""
    failure = ExecutionFailure(
        kind=FailureKind.RATE_LIMIT, status_code=429, message="slow", retryable=True
    )
    assert failure_kind_name(failure) == "rate_limit"
    assert failure_kind_name(BaseExceptionGroup("group", [failure])) == "rate_limit"


def test_an_unclassified_exception_falls_back_to_its_class_name():
    """Nothing is invented: a plain exception has no kind and says so."""
    assert failure_kind_name(ValueError("just wrong")) == "ValueError"
    assert failure_kind(ValueError("just wrong")) is None


def test_parsing_kind_lists_is_forgiving_about_shape_only():
    """Whitespace and blanks are noise; an unknown name is not silently kept."""
    assert parse_failure_kinds(" invalid_request , timeout ,, ") == frozenset(
        {FailureKind.INVALID_REQUEST, FailureKind.TIMEOUT}
    )
    assert parse_failure_kinds("") == frozenset()
    assert parse_failure_kinds(None) == frozenset()
    assert parse_failure_kinds("not_a_kind") == frozenset()
