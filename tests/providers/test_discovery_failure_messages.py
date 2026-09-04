"""A skipped model discovery must say *why*, not just name an exception class.

The only OAuth line in 425 KB of the reporter's ``server.log`` was::

    Provider model discovery skipped: provider=anthropic_oauth
    reason=query failure: AnthropicOAuthRefreshError

The status code the exception was carefully built around never reached anybody,
so a rate limit and a dead credential looked identical -- and they call for
opposite actions.
"""

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.providers.anthropic_oauth.credentials import (
    AnthropicOAuthRefreshRejected,
    AnthropicOAuthRefreshUnavailable,
    AnthropicOAuthUnavailableError,
)
from my_claude_code.providers.anthropic_oauth.oauth_login import (
    AnthropicOAuthLoginError,
)
from my_claude_code.providers.runtime.validation import (
    provider_query_failure_reason,
)


@pytest.fixture
def settings() -> Settings:
    # Settings reads the environment once per process, so construct without it.
    return Settings.model_construct(log_api_error_tracebacks=False)


def test_discovery_failure_message_carries_the_refresh_status_code(
    settings: Settings,
) -> None:
    reason = provider_query_failure_reason(
        AnthropicOAuthRefreshUnavailable(429), settings
    )

    assert "429" in reason
    assert "AnthropicOAuthRefreshUnavailable" not in reason
    # And it says the thing an operator can act on.
    assert "rate-limiting" in reason


def test_discovery_failure_distinguishes_transient_from_definitive(
    settings: Settings,
) -> None:
    transient = provider_query_failure_reason(
        AnthropicOAuthRefreshUnavailable(429), settings
    )
    definitive = provider_query_failure_reason(
        AnthropicOAuthRefreshRejected(401), settings
    )

    assert transient != definitive
    assert "sign in again" not in transient.lower()
    assert "sign in again" in definitive.lower()


@pytest.mark.parametrize(
    "error",
    [
        AnthropicOAuthUnavailableError("No usable credential found (mcc (absent))"),
        AnthropicOAuthLoginError(400, "the pasted code was rejected"),
    ],
)
def test_every_mcc_credential_error_reports_its_own_text(
    settings: Settings, error: Exception
) -> None:
    reason = provider_query_failure_reason(error, settings)

    assert type(error).__name__ not in reason
    assert str(error) in reason


def test_an_unmarked_exception_still_falls_back_to_its_class_name(
    settings: Settings,
) -> None:
    """The marker is opt-in: an arbitrary exception's text is not assumed safe."""

    class SomeThirdPartyError(Exception):
        pass

    reason = provider_query_failure_reason(
        SomeThirdPartyError("token=sk-secret-value"), settings
    )

    assert reason == "query failure: SomeThirdPartyError"
    assert "sk-secret-value" not in reason
