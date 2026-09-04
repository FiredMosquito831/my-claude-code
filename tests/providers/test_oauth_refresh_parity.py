"""The two OAuth providers must agree about what a refresh failure means.

They drifted apart once, and it cost the Claude subscription provider its
entire working life: ``chatgpt_oauth`` classified ``{400, 401, 403}`` as a dead
credential and re-raised everything else as transient, while
``anthropic_oauth`` treated *any* status ``>= 400`` as definitive and told the
operator to "sign in again" -- advice that, on the 429 Anthropic's token
endpoint actually answers, rotates a perfectly good refresh token away.

This file is the guard rail. It is deliberately about the *classification*
rather than about either implementation's internals, because the two credential
layers are legitimately different shapes and only the policy has to match.
"""

import httpx
import pytest

from my_claude_code.providers.anthropic_oauth.credentials import (
    DEFINITIVE_REFRESH_STATUSES as ANTHROPIC_DEFINITIVE,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    classify_refresh_failure,
)
from my_claude_code.providers.chatgpt_oauth.credentials import (
    DEFINITIVE_REFRESH_STATUSES as CHATGPT_DEFINITIVE,
)

TRANSIENT_STATUSES = (408, 425, 429, 500, 502, 503, 504, 529)


def _Response(status_code: int, payload: object) -> httpx.Response:
    """A real response, so the classifier's own JSON parsing is exercised.

    ``payload=None`` means "not JSON at all" -- the edge-block shape.
    """
    if payload is None:
        return httpx.Response(status_code, text="error code: 1010")
    return httpx.Response(status_code, json=payload)


def test_anthropic_and_chatgpt_oauth_agree_on_refresh_failure_classes() -> None:
    assert ANTHROPIC_DEFINITIVE == CHATGPT_DEFINITIVE == frozenset({400, 401, 403})


@pytest.mark.parametrize("status", sorted(ANTHROPIC_DEFINITIVE))
def test_definitive_statuses_with_an_oauth_body_retire_the_credential(
    status: int,
) -> None:
    failure = classify_refresh_failure(_Response(status, {"error": "invalid_grant"}))
    assert failure.definitive is True
    assert failure.status_code == status


@pytest.mark.parametrize("status", TRANSIENT_STATUSES)
def test_transient_statuses_keep_the_credential_in_both_providers(
    status: int,
) -> None:
    assert status not in ANTHROPIC_DEFINITIVE
    assert status not in CHATGPT_DEFINITIVE
    failure = classify_refresh_failure(_Response(status, {"error": "whatever"}))
    assert failure.definitive is False


@pytest.mark.parametrize("status", sorted(ANTHROPIC_DEFINITIVE))
def test_a_definitive_status_without_an_oauth_body_is_still_transient(
    status: int,
) -> None:
    """Anthropic is stricter than ChatGPT here, on live evidence.

    The consented live test for 6.43.0 got a 403 with a 17-byte non-JSON body
    from the edge in front of the token endpoint -- a request that never
    reached the code that can judge a grant. The status alone is therefore not
    sufficient evidence about the credential, and treating it as such would
    retire a working one. This is a deliberate, one-directional divergence:
    Anthropic never retires a credential ChatGPT would have kept.
    """
    failure = classify_refresh_failure(_Response(status, None))
    assert failure.definitive is False


def test_the_transient_message_never_tells_the_operator_to_sign_in_again() -> None:
    """The single sentence that caused the outage this PR fixes."""
    for status in TRANSIENT_STATUSES:
        message = str(classify_refresh_failure(_Response(status, {"error": "x"})))
        assert "sign in again" not in message.lower()


def test_the_definitive_message_does_tell_the_operator_what_to_do() -> None:
    message = str(classify_refresh_failure(_Response(401, {"error": "invalid_grant"})))
    assert "sign in again" in message.lower()
