"""A host that says *when* in JSON is heard, and is heard for a whole day.

The OpenCode free tier answers a spent daily allowance with a 429 whose body
names ``FreeUsageLimitError`` and carries ``retryAfter`` -- the seconds to the
next UTC midnight, computed by the vendor's own limiter. MCC has parsed that
field since 6.69.0 (``providers/openai_chat/identity_enforcement.py:50,105``)
but only into a diagnostic fact; the call site says so in terms ("Observation
only: nothing about the classification, the ladder or the credential's health
changes"). So the bench fell through to ``DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS``
and the model was retried every sixty seconds for the rest of the day.

The invariant: the bench honours the vendor-stated reset, is never shorter than
it, and is capped at twenty-four hours.
"""

import httpx
import openai
import pytest

from my_claude_code.core.rate_limit import (
    MAX_HOST_STATED_COOLDOWN_SECONDS,
    retry_after_from_body,
)
from my_claude_code.providers.failure_policy import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
    rate_limit_cooldown_seconds,
    retry_after_from_error,
)

#: The shape measured in MCC's own request log, from before the module that
#: reads it existed.
FREE_TIER_BODY = {
    "type": "error",
    "error": {
        "type": "Account.FreeUsageLimitError",
        "message": "Free usage limit reached",
        "retryAfter": 27_953,
    },
}


def _openai_429(body: object) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://zen.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("rate limited", response=response, body=body)


def _httpx_429(payload: object, headers: dict[str, str] | None = None):
    request = httpx.Request("POST", "https://zen.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request, json=payload, headers=headers or {})
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


class TestTheReader:
    def test_a_nested_retry_after_is_found(self) -> None:
        assert retry_after_from_body(FREE_TIER_BODY) == 27_953.0

    def test_a_top_level_field_is_found(self) -> None:
        assert retry_after_from_body({"retryAfter": 12}) == 12.0

    @pytest.mark.parametrize(
        "key", ["retryAfter", "retry_after", "retryAfterSeconds", "retry_after_seconds"]
    )
    def test_every_spelling_measured_or_conventional(self, key: str) -> None:
        assert retry_after_from_body({"error": {key: 30}}) == 30.0

    def test_a_numeric_string_counts(self) -> None:
        assert retry_after_from_body({"detail": {"retryAfter": "90.5"}}) == 90.5

    def test_a_json_string_body_is_parsed(self) -> None:
        assert retry_after_from_body('{"error": {"retryAfter": 7}}') == 7.0

    @pytest.mark.parametrize(
        "body",
        [
            None,
            "not json",
            {},
            {"error": {"message": "slow down"}},
            {"retryAfter": True},
            {"retryAfter": -1},
            {"retryAfter": "soon"},
            {"retryAfter": {"seconds": 30}},
        ],
    )
    def test_anything_that_is_not_a_stated_wait_is_no_answer(
        self, body: object
    ) -> None:
        """``None`` means "it did not say", which is not the same as zero."""

        assert retry_after_from_body(body) is None

    def test_an_absurd_body_value_clamps_to_a_day(self) -> None:
        assert (
            retry_after_from_body({"error": {"retryAfter": 999_999}})
            == MAX_HOST_STATED_COOLDOWN_SECONDS
        )


class TestTheBench:
    def test_the_free_tier_bench_is_the_wait_the_host_published(self) -> None:
        """The defect, directly: 27,953 s, not 60."""

        error = _openai_429(FREE_TIER_BODY)

        assert retry_after_from_error(error) == 27_953.0
        assert rate_limit_cooldown_seconds(error) == 27_953.0

    def test_it_is_not_clamped_to_an_hour(self) -> None:
        """The 1 h header bound would have made this a 3,600 s bench."""

        bench = retry_after_from_error(_openai_429(FREE_TIER_BODY))

        assert bench is not None
        assert bench > MAX_RATE_LIMIT_COOLDOWN_SECONDS

    def test_an_absurd_body_value_clamps_to_a_day(self) -> None:
        error = _openai_429({"error": {"retryAfter": 99_999_999}})

        assert retry_after_from_error(error) == MAX_HOST_STATED_COOLDOWN_SECONDS

    def test_an_httpx_error_reads_its_response_text(self) -> None:
        """The branch every provider that is not OpenAI-shaped arrives on."""

        assert retry_after_from_error(_httpx_429(FREE_TIER_BODY)) == 27_953.0

    def test_a_header_still_wins_over_a_body(self) -> None:
        """Both present: the header is the more precise statement, and is read
        first, under its own one-hour bound."""

        error = _httpx_429(FREE_TIER_BODY, {"retry-after-ms": "1500"})

        assert retry_after_from_error(error) == pytest.approx(1.5)

    def test_a_silent_host_still_falls_back_to_the_operator_default(self) -> None:
        """No header and no body field: nothing was stated, so nothing is
        honoured, and the operator's cooldown applies as it always has."""

        error = _openai_429({"error": {"message": "slow down"}})

        assert retry_after_from_error(error) is None
        assert rate_limit_cooldown_seconds(error) == DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
