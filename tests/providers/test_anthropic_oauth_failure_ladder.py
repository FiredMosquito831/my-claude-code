"""A token-endpoint failure must travel the *ordinary* provider failure path.

The binding decision for 6.43.0 is that the Claude subscription provider gets
no special-casing: a 429 from the token endpoint is classified with the same
``FailureKind`` an API-key provider's 429 gets, honours ``Retry-After`` the same
way, and is handed to the same retry ladder, backoff and provider-health
machinery. No new ``FailureKind``, and no OAuth-only retry loop.

Left to the shared classifier these errors fell through every branch to
``UPSTREAM``/502, which lost the status, lost any ``Retry-After``, reported
``api_error`` instead of ``rate_limit_error`` on the wire, and benched the
*model* for something that was never the model's fault.
"""

import httpx
import pytest

from my_claude_code.application.route_health import failure_counts_toward_bench
from my_claude_code.core.anthropic.errors import anthropic_error_type_for_failure
from my_claude_code.core.failures import FailureKind
from my_claude_code.providers.anthropic_oauth.credentials import (
    AnthropicOAuthRefreshRejected,
    AnthropicOAuthRefreshUnavailable,
    AnthropicOAuthUnavailableError,
)
from my_claude_code.providers.anthropic_oauth.provider import AnthropicOAuthProvider
from my_claude_code.providers.credential_rotation import (
    credential_failure_class,
    error_justifies_rotation,
)


@pytest.fixture
def classify():
    """The provider's own override, without building a live provider."""
    return AnthropicOAuthProvider._classify_credential_failure.__get__(
        object.__new__(AnthropicOAuthProvider)
    )


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json={"error": "x"})


def test_a_refresh_429_is_classified_as_a_rate_limit(classify) -> None:
    failure = classify(AnthropicOAuthRefreshUnavailable(429, response=_response(429)))

    assert failure is not None
    assert failure.kind is FailureKind.RATE_LIMIT
    assert failure.status_code == 429
    assert failure.retryable is True
    # Same wire vocabulary an API-key provider's 429 produces.
    assert anthropic_error_type_for_failure(failure) == "rate_limit_error"


def test_a_refresh_429_honours_a_published_retry_after(classify) -> None:
    failure = classify(
        AnthropicOAuthRefreshUnavailable(
            429, response=_response(429, {"retry-after": "42"})
        )
    )

    assert failure is not None
    assert failure.retry_after_seconds == pytest.approx(42.0)


def test_a_refresh_429_without_a_retry_after_publishes_none(classify) -> None:
    """``None`` means "the endpoint said nothing", which the ladder defaults."""
    failure = classify(AnthropicOAuthRefreshUnavailable(429, response=_response(429)))

    assert failure is not None
    assert failure.retry_after_seconds is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_refresh_5xx_is_overloaded_and_retryable(classify, status: int) -> None:
    failure = classify(
        AnthropicOAuthRefreshUnavailable(status, response=_response(status))
    )

    assert failure is not None
    assert failure.kind is FailureKind.OVERLOADED
    assert failure.retryable is True
    assert anthropic_error_type_for_failure(failure) == "overloaded_error"


def test_a_transport_failure_reaching_the_token_endpoint_is_retryable(
    classify,
) -> None:
    failure = classify(
        AnthropicOAuthRefreshUnavailable(503, detail="ConnectError reaching it")
    )

    assert failure is not None
    assert failure.kind is FailureKind.OVERLOADED
    assert failure.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_definitive_rejection_is_authentication_and_not_retryable(
    classify, status: int
) -> None:
    failure = classify(AnthropicOAuthRefreshRejected(status))

    assert failure is not None
    assert failure.kind is FailureKind.AUTHENTICATION
    assert failure.retryable is False
    assert anthropic_error_type_for_failure(failure) == "authentication_error"


def test_no_credential_at_all_is_unavailable(classify) -> None:
    failure = classify(AnthropicOAuthUnavailableError("nothing on disk"))

    assert failure is not None
    assert failure.kind is FailureKind.UNAVAILABLE
    assert failure.retryable is False


def test_an_unrelated_error_is_left_to_the_shared_classifier(classify) -> None:
    assert classify(httpx.ConnectError("upstream inference socket")) is None
    assert classify(ValueError("something else entirely")) is None


# ---------------------------------------------------------------------------
# what the existing machinery then does with them
# ---------------------------------------------------------------------------


def test_a_rate_limited_refresh_charges_the_credential_like_any_other_429(
    classify,
) -> None:
    failure = classify(AnthropicOAuthRefreshUnavailable(429, response=_response(429)))

    assert credential_failure_class(failure) == "rate_limit"
    assert error_justifies_rotation(failure) is True


def test_a_definitive_rejection_is_credential_shaped(classify) -> None:
    failure = classify(AnthropicOAuthRefreshRejected(401))

    assert credential_failure_class(failure) == "auth"
    assert error_justifies_rotation(failure) is True


def test_a_rate_limited_refresh_does_not_bench_the_model(classify) -> None:
    """A rate limit is about the credential's budget, not the model's health.

    This is the concrete gain from classifying properly. Before 6.43.0 these
    errors were ``UPSTREAM``, which *is* a bench-counting kind -- so every
    rate-limited refresh counted against the model, and enough of them would
    route traffic away from a model that was answering perfectly well.
    """
    failure = classify(AnthropicOAuthRefreshUnavailable(429, response=_response(429)))

    assert failure is not None
    assert failure_counts_toward_bench(failure.kind) is False
    assert failure_counts_toward_bench(FailureKind.UPSTREAM) is True


@pytest.mark.parametrize(
    ("error", "equivalent"),
    [
        (AnthropicOAuthRefreshUnavailable(429), FailureKind.RATE_LIMIT),
        (AnthropicOAuthRefreshUnavailable(503), FailureKind.OVERLOADED),
        (AnthropicOAuthRefreshRejected(401), FailureKind.AUTHENTICATION),
    ],
)
def test_bench_behaviour_matches_the_equivalent_api_key_failure(
    classify, error: Exception, equivalent: FailureKind
) -> None:
    """No special-casing, in either direction.

    A credential failure makes the whole provider unusable, so where an API-key
    provider's 401 or 5xx benches, this one benches too -- and where its 429
    does not, this one does not either. The point of the override is that the
    OAuth provider lands in the *same* bucket, not a privileged one.
    """
    failure = classify(error)

    assert failure is not None
    assert failure.kind is equivalent
    assert failure_counts_toward_bench(failure.kind) is failure_counts_toward_bench(
        equivalent
    )


def test_the_override_is_wired_into_the_messages_provider() -> None:
    """Classification and the retry ladder must be given the same override.

    Wiring only one produces a failure that is retried but reported wrong, or
    reported right and never retried.
    """
    import inspect

    import my_claude_code.providers.anthropic_messages.provider as messages

    text = inspect.getsource(messages)
    assert text.count("provider_failure_override=self._failure_override") == 2
    assert "def set_failure_override(" in text


def test_the_oauth_provider_registers_its_override() -> None:
    import inspect

    text = inspect.getsource(AnthropicOAuthProvider)
    assert "self.set_failure_override(self._classify_credential_failure)" in text
