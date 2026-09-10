"""Two requests, and an answer the Models page can show without overstating it.

The 400 asserted below is not invented. It is the shape OpenCode Zen really
returned on 2026-09-10 to a request carrying nothing but a bearer token, which
is what every MCC release before 6.69.0 sent.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_SESSION_HEADER,
)
from my_claude_code.providers.recovery import (
    FACT_CLIENT_IDENTITY_REQUIRED,
    PROVIDER_WIDE_MODEL_ID,
    learned_fact_store,
)
from my_claude_code.providers.runtime.identity_probe import (
    IdentityProbeOutcome,
    declared_identity_headers,
    probe_client_identity,
)

NOW = datetime(2026, 9, 10, 16, 45, tzinfo=UTC)

MISSING_SESSION_BODY = {
    "type": "error",
    "error": {
        "type": "MissingSessionID",
        "message": (
            "Error from provider (Console): OpenCode's free tier can only be "
            "used in OpenCode"
        ),
    },
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _free_limit_body() -> dict[str, object]:
    midnight = (NOW + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {
        "type": "FreeUsageLimitError",
        "retryAfter": int((midnight - NOW).total_seconds()),
    }


async def _probe(handler) -> IdentityProbeOutcome:
    """The probe, against a transport that never leaves the process."""
    outcome = await probe_client_identity(
        "opencode",
        "https://example.invalid/zen/v1",
        "sk-probe-key",
        "some-free-model",
        client=_client(handler),
        now=NOW,
    )
    assert outcome is not None
    return outcome


def _facts() -> list[tuple[str, str, object, str]]:
    return [
        (fact.model_id, fact.fact_kind, fact.value, fact.source)
        for fact in learned_fact_store().facts_for_provider("opencode")
    ]


def test_a_provider_that_declares_no_identity_is_never_probed() -> None:
    assert declared_identity_headers("groq") is None
    assert declared_identity_headers("opencode") is not None


def test_the_probe_omits_the_header_the_host_was_measured_refusing_without() -> None:
    """Not the one that looks most like an identity -- the one that is checked.

    On 2026-09-10 an *invalid* ``x-opencode-client`` was answered 200 and an
    absent ``x-opencode-session`` was answered 400. A probe aimed at the first
    would have recorded "no difference observed" about a host that was
    refusing us outright.
    """
    declared = declared_identity_headers("opencode")
    assert declared is not None
    assert declared[1] == OPENCODE_SESSION_HEADER


@pytest.mark.asyncio
async def test_the_probe_spends_exactly_two_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": []})

    await _probe(handler)

    assert len(seen) == 2
    assert OPENCODE_SESSION_HEADER in seen[0].headers
    # The one difference, and everything else is held constant, so a
    # difference in the two answers has exactly one cause.
    assert OPENCODE_SESSION_HEADER not in seen[1].headers
    assert seen[0].headers["x-opencode-client"] == "cli"
    assert seen[1].headers["x-opencode-client"] == "cli"


@pytest.mark.asyncio
async def test_two_hundreds_record_no_difference_observed_with_a_date() -> None:
    outcome = await _probe(lambda _r: httpx.Response(200, json={"choices": []}))

    assert outcome.required is False
    assert outcome.detail == "checked 2026-09-10, no difference observed"
    assert _facts() == [
        (PROVIDER_WIDE_MODEL_ID, FACT_CLIENT_IDENTITY_REQUIRED, False, "probe")
    ]


@pytest.mark.asyncio
async def test_the_live_refusal_is_read_as_enforcement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if OPENCODE_SESSION_HEADER not in request.headers:
            return httpx.Response(400, json=MISSING_SESSION_BODY)
        return httpx.Response(200, json={"choices": []})

    outcome = await _probe(handler)

    assert outcome.required is True
    assert outcome.detail == "refused the request without the identity"
    assert (outcome.with_identity_status, outcome.without_identity_status) == (200, 400)
    assert _facts() == [
        (PROVIDER_WIDE_MODEL_ID, FACT_CLIENT_IDENTITY_REQUIRED, True, "probe")
    ]


@pytest.mark.asyncio
async def test_a_quota_error_on_only_one_leg_is_enforcement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if OPENCODE_SESSION_HEADER not in request.headers:
            return httpx.Response(429, json=_free_limit_body())
        return httpx.Response(200, json={"choices": []})

    outcome = await _probe(handler)

    assert outcome.required is True
    assert outcome.detail == "quota error only without the identity"


@pytest.mark.asyncio
async def test_a_quota_error_on_both_legs_teaches_nothing_about_identity() -> None:
    """The daily allowance is simply spent. That is not evidence about a header."""
    outcome = await _probe(lambda _r: httpx.Response(429, json=_free_limit_body()))

    assert outcome.required is False


@pytest.mark.asyncio
async def test_a_host_that_is_down_is_unprobeable_and_teaches_nothing() -> None:
    outcome = await _probe(lambda _r: httpx.Response(500, text="Internal server error"))

    assert outcome.required is None
    assert _facts() == []


@pytest.mark.asyncio
async def test_the_probe_stores_no_response_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if OPENCODE_SESSION_HEADER not in request.headers:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "missing x-opencode-session; internal trace "
                            "abcdef0123456789 for account bob@example.com"
                        )
                    }
                },
            )
        return httpx.Response(200, json={"choices": []})

    await _probe(handler)

    for fact in learned_fact_store().facts_for_provider("opencode"):
        assert "bob@example.com" not in fact.evidence
        assert "abcdef0123456789" not in fact.evidence
        assert len(fact.evidence) <= 160


@pytest.mark.asyncio
async def test_the_probe_records_a_provider_wide_fact_not_a_per_model_one() -> None:
    await _probe(lambda _r: httpx.Response(200, json={"choices": []}))

    facts = learned_fact_store().facts_for_provider("opencode")
    assert [fact.model_id for fact in facts] == [PROVIDER_WIDE_MODEL_ID]
