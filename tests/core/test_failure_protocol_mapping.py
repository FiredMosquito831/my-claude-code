"""Canonical execution failures and their protocol-owned wire mappings."""

import pytest

from my_claude_code.core.anthropic.errors import (
    anthropic_error_type_for_failure,
    anthropic_failure_payload,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.openai_responses.errors import (
    openai_error_type_for_failure,
    openai_failure_payload,
)


@pytest.mark.parametrize(
    ("kind", "anthropic_type", "openai_type"),
    [
        (FailureKind.INVALID_REQUEST, "invalid_request_error", "invalid_request_error"),
        (FailureKind.AUTHENTICATION, "authentication_error", "authentication_error"),
        (FailureKind.PERMISSION, "permission_error", "permission_error"),
        # An account out of credits is a billing fact on both wires, and both
        # protocols already had the type for it -- it was only ever reachable
        # through a PERMISSION failure that happened to carry a 402.
        (FailureKind.QUOTA, "billing_error", "billing_error"),
        (FailureKind.RATE_LIMIT, "rate_limit_error", "rate_limit_error"),
        (FailureKind.OVERLOADED, "overloaded_error", "overloaded_error"),
        # Existing finalized transport timeouts are exposed as api_error; the
        # semantic kind becomes more precise without changing that wire contract.
        (FailureKind.TIMEOUT, "api_error", "api_error"),
        (FailureKind.UPSTREAM, "api_error", "api_error"),
        (FailureKind.UNAVAILABLE, "api_error", "api_error"),
    ],
)
def test_each_protocol_owns_failure_kind_to_wire_type_mapping(
    kind: FailureKind,
    anthropic_type: str,
    openai_type: str,
) -> None:
    assert anthropic_error_type_for_failure(kind) == anthropic_type
    assert openai_error_type_for_failure(kind) == openai_type


def test_finalized_status_502_timeout_keeps_existing_api_error_wire_type() -> None:
    failure = ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=502,
        message="Provider request timed out.",
        retryable=True,
    )

    assert anthropic_failure_payload(failure)["error"]["type"] == "api_error"
    assert openai_failure_payload(failure)["error"]["type"] == "api_error"
