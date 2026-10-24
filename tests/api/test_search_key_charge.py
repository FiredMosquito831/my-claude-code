"""The no-chain search key judge reads failures with the model path's rules."""

import httpx
import pytest

from my_claude_code.api.web_tools.search_key_charge import no_chain_key_charge
from my_claude_code.websearch.adapters.http import map_status_error
from my_claude_code.websearch.errors import (
    KIND_DETAIL_BAD_JSON,
    KIND_DETAIL_TIMEOUT,
    KIND_DETAIL_TRANSPORT,
    WebSearchConfigError,
    WebSearchUpstreamError,
)


def _http_error(status: int, message: str, headers: dict[str, str] | None = None):
    response = httpx.Response(
        status,
        headers=headers or {},
        json={"error": {"message": message}},
        request=httpx.Request("POST", "http://exa.test.invalid/search"),
    )
    return map_status_error("exa", response)


@pytest.mark.parametrize(
    ("status", "message", "charge"),
    [
        (401, "invalid api key", "auth"),
        (403, "forbidden", "auth"),
        (429, "too many requests", "rate_limit"),
        (402, "insufficient credits", "quota"),
        (402, "plan limit reached", None),  # a bare 402 is unsure: no charge
        (403, "This service is not available in your country", None),
        (500, "internal error", None),
        (502, "bad gateway", None),
        (503, "unavailable", None),
        (400, "unsupported parameter", None),
    ],
)
def test_an_http_failure_is_charged_only_when_it_is_about_the_key(
    status: int, message: str, charge: str | None
) -> None:
    assert no_chain_key_charge(_http_error(status, message)) == charge


@pytest.mark.parametrize(
    "detail", [KIND_DETAIL_TIMEOUT, KIND_DETAIL_TRANSPORT, KIND_DETAIL_BAD_JSON]
)
def test_a_timeout_a_refused_connection_or_a_bad_body_is_never_charged(
    detail: str,
) -> None:
    error = WebSearchUpstreamError("exa", "no answer", kind_detail=detail)
    assert no_chain_key_charge(error) is None


def test_an_unclassified_exception_or_a_config_error_is_never_charged() -> None:
    assert no_chain_key_charge(RuntimeError("bug")) is None
    assert no_chain_key_charge(WebSearchConfigError("exa", "no key")) is None
