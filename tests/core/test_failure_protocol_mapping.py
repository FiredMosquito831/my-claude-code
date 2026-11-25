"""Canonical execution failures and their protocol-owned wire mappings."""

import pytest

from my_claude_code.core.anthropic.errors import (
    anthropic_error_type_for_failure,
    anthropic_failure_payload,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_common.errors import openai_error_code_for_failure
from my_claude_code.core.openai_responses.errors import (
    openai_error_type_for_failure,
    openai_failure_payload,
)


@pytest.mark.parametrize(
    ("kind", "anthropic_type", "openai_type", "openai_code"),
    [
        (
            FailureKind.INVALID_REQUEST,
            "invalid_request_error",
            "invalid_request_error",
            None,
        ),
        (
            FailureKind.CONTEXT_LENGTH,
            "invalid_request_error",
            "invalid_request_error",
            "context_length_exceeded",
        ),
        (
            FailureKind.AUTHENTICATION,
            "authentication_error",
            "invalid_request_error",
            "invalid_api_key",
        ),
        (FailureKind.PERMISSION, "permission_error", "permission_error", None),
        (
            FailureKind.QUOTA,
            "billing_error",
            "insufficient_quota",
            "insufficient_quota",
        ),
        (
            FailureKind.RATE_LIMIT,
            "rate_limit_error",
            "rate_limit_error",
            "rate_limit_exceeded",
        ),
        (FailureKind.OVERLOADED, "overloaded_error", "server_error", "server_error"),
        (FailureKind.TIMEOUT, "api_error", "server_error", "timeout"),
        (FailureKind.UPSTREAM, "api_error", "server_error", "server_error"),
        (FailureKind.UNAVAILABLE, "api_error", "server_error", "server_error"),
    ],
)
def test_each_protocol_owns_failure_kind_to_wire_type_mapping(
    kind: FailureKind,
    anthropic_type: str,
    openai_type: str,
    openai_code: str | None,
) -> None:
    assert anthropic_error_type_for_failure(kind) == anthropic_type
    assert openai_error_type_for_failure(kind) == openai_type
    assert openai_error_code_for_failure(kind) == openai_code


def test_finalized_status_502_timeout_keeps_existing_api_error_wire_type() -> None:
    failure = ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=502,
        message="Provider request timed out.",
        retryable=True,
    )

    assert anthropic_failure_payload(failure)["error"]["type"] == "api_error"
    assert openai_failure_payload(failure)["error"]["type"] == "server_error"
    assert openai_failure_payload(failure)["error"]["code"] == "timeout"
