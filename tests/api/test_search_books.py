"""The web search chain's key books and its error translation (7.112.0).

``translate_search_error`` is search's adapter-boundary classification into
the model path's ``ExecutionFailure`` (spec §3.3 table); the books are the
model path's own objects built for search keys with the user's settings and
no new numbers (decision Q4).
"""

import httpx
import pytest

from my_claude_code.api.web_tools import search_books
from my_claude_code.api.web_tools.search_books import (
    SearchBooksPolicy,
    SearchKeyBooks,
    translate_search_error,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.rate_limit import RateLimitCooldown
from my_claude_code.websearch.adapters.http import map_status_error, request_json
from my_claude_code.websearch.errors import (
    WebSearchConfigError,
    WebSearchError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import build_provider
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS

_COOLDOWN = RateLimitCooldown(mode="provider", fallback_seconds=60.0, max_seconds=3600)
_REQUEST = httpx.Request("POST", "http://search.invalid/search")


def _status(code: int, body: object, **headers: str) -> WebSearchError:
    response = httpx.Response(
        code,
        json=body,
        headers=headers,
        request=_REQUEST,
    )
    return map_status_error("exa", response)


def _message(text: str) -> dict[str, object]:
    return {"error": {"message": text}}


@pytest.mark.parametrize(
    ("error", "kind", "status", "retryable", "retry_after"),
    [
        (
            _status(401, _message("bad key")),
            FailureKind.AUTHENTICATION,
            401,
            False,
            None,
        ),
        (_status(403, _message("forbidden")), FailureKind.PERMISSION, 403, False, None),
        # A bare 402 rotates and charges nothing: no evidence of credits.
        (_status(402, _message("plan limit")), FailureKind.QUOTA, 402, False, None),
        # An exact credits phrase benches for RATE_LIMIT_COOLDOWN_SECONDS.
        (
            _status(402, _message("insufficient credits")),
            FailureKind.QUOTA,
            402,
            False,
            60.0,
        ),
        (
            _status(400, _message("insufficient credits")),
            FailureKind.QUOTA,
            400,
            False,
            60.0,
        ),
        (
            _status(403, _message("out of credits")),
            FailureKind.QUOTA,
            403,
            False,
            60.0,
        ),
        (_status(429, _message("slow")), FailureKind.RATE_LIMIT, 429, True, None),
        (
            _status(429, _message("slow"), **{"Retry-After": "7"}),
            FailureKind.RATE_LIMIT,
            429,
            True,
            7.0,
        ),
        (
            _status(429, {"error": {"message": "slow", "retryAfter": 120}}),
            FailureKind.RATE_LIMIT,
            429,
            True,
            120.0,
        ),
        (
            _status(400, _message("bad param")),
            FailureKind.MODEL_REJECTED,
            400,
            False,
            None,
        ),
        (_status(404, _message("gone")), FailureKind.MODEL_REJECTED, 404, False, None),
        (_status(422, _message("no")), FailureKind.MODEL_REJECTED, 422, False, None),
        (_status(500, _message("boom")), FailureKind.UPSTREAM, 500, True, None),
        (_status(503, _message("down")), FailureKind.UPSTREAM, 503, True, None),
        # A ddgs engine limit carries no status: still a 429.
        (
            WebSearchRateLimitError("ddgs", "engine limit"),
            FailureKind.RATE_LIMIT,
            429,
            True,
            None,
        ),
        # An adapter's body-level complaint with no status: not retried.
        (
            WebSearchUpstreamError("serpapi", "search error: x"),
            FailureKind.UPSTREAM,
            502,
            False,
            None,
        ),
    ],
)
def test_the_translation_table(
    error: WebSearchError,
    kind: FailureKind,
    status: int,
    retryable: bool,
    retry_after: float | None,
) -> None:
    failure = translate_search_error(error, cooldown=_COOLDOWN)
    assert failure is not None
    assert (failure.kind, failure.status_code, failure.retryable) == (
        kind,
        status,
        retryable,
    )
    assert failure.retry_after_seconds == retry_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "kind", "status", "retryable"),
    [
        (httpx.ReadTimeout("slow", request=_REQUEST), FailureKind.TIMEOUT, 504, True),
        (
            httpx.ConnectError("refused", request=_REQUEST),
            FailureKind.UNAVAILABLE,
            503,
            True,
        ),
    ],
)
async def test_transport_faults_keep_their_detail(
    raised: Exception, kind: FailureKind, status: int, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WebSearchUpstreamError) as caught:
            await request_json(client, "exa", "POST", "http://search.invalid/search")
    failure = translate_search_error(caught.value, cooldown=_COOLDOWN)
    assert failure is not None
    assert (failure.kind, failure.status_code, failure.retryable) == (
        kind,
        status,
        retryable,
    )


@pytest.mark.asyncio
async def test_an_unreadable_body_is_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<<not json>>", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(WebSearchUpstreamError) as caught:
            await request_json(client, "exa", "POST", "http://search.invalid/search")
    failure = translate_search_error(caught.value, cooldown=_COOLDOWN)
    assert failure is not None
    assert (failure.kind, failure.status_code, failure.retryable) == (
        FailureKind.UPSTREAM,
        502,
        False,
    )


def test_a_header_wait_meets_the_operators_ceiling() -> None:
    capped = RateLimitCooldown(mode="provider", fallback_seconds=60.0, max_seconds=30)
    failure = translate_search_error(
        _status(429, _message("slow"), **{"Retry-After": "900"}), cooldown=capped
    )
    assert failure is not None
    assert failure.retry_after_seconds == 30.0


def test_config_errors_and_foreign_exceptions_are_not_classified() -> None:
    assert (
        translate_search_error(
            WebSearchConfigError("exa", "no key"), cooldown=_COOLDOWN
        )
        is None
    )
    assert translate_search_error(RuntimeError("bug"), cooldown=_COOLDOWN) is None


def _settings(**values: str) -> Settings:
    return Settings.model_validate({**NO_WEB_SEARCH_CREDENTIALS, **values})


def test_the_books_use_the_operators_settings_and_no_new_numbers() -> None:
    settings = _settings(
        PROVIDER_RETRY_ATTEMPTS="4",
        PROVIDER_RETRY_BACKOFF_BASE_SECONDS="1.5",
        PROVIDER_RETRY_BACKOFF_MAX_SECONDS="9",
        PROVIDER_RETRY_BACKOFF_JITTER_SECONDS="0.25",
        RATE_LIMIT_COOLDOWN_MODE="fixed",
        RATE_LIMIT_COOLDOWN_SECONDS="45",
        RATE_LIMIT_COOLDOWN_MAX_SECONDS="900",
        CREDENTIAL_LOCKOUT_TIERS="120,600",
    )
    policy = SearchBooksPolicy.from_settings(settings, "round_robin")
    books = SearchKeyBooks(
        "exa", ("exa-a-key-1111", "exa-b-key-2222"), ("a", "b"), policy
    )

    assert books.state.policy == "round_robin"
    assert books.cooldown == RateLimitCooldown(
        mode="fixed", fallback_seconds=45.0, max_seconds=900.0
    )
    assert policy.lockout_tiers == (120.0, 600.0)
    assert len(books.limiters) == 2
    limiter = books.limiters[0]
    # PROVIDER_RETRY_ATTEMPTS counts tries; the limiter counts retries.
    assert limiter._max_retries == 3
    assert limiter._backoff_base_seconds == 1.5
    assert limiter._backoff_max_seconds == 9.0
    assert limiter._backoff_jitter_seconds == 0.25
    assert limiter._routes_around_model is False
    assert limiter._proactive_limiter.unlimited


def test_a_keyless_provider_has_one_slot_that_is_never_unselectable() -> None:
    policy = SearchBooksPolicy.from_settings(_settings(), "single")
    books = SearchKeyBooks("ddgs", (), (), policy)
    assert books.key_count == 1
    assert books.state.selectable_indexes() == (0,)


@pytest.mark.asyncio
async def test_the_books_are_rebuilt_when_the_keys_change() -> None:
    first = build_provider(_settings(EXA_API_KEY="exa-one-key-11112222"), "exa")
    again = build_provider(_settings(EXA_API_KEY="exa-one-key-11112222"), "exa")
    changed = build_provider(
        _settings(EXA_API_KEY="exa-one-key-11112222,exa-two-key-33334444"), "exa"
    )
    try:
        books = await search_books.search_key_books(_settings(), first)
        assert await search_books.search_key_books(_settings(), again) is books
        rebuilt = await search_books.search_key_books(_settings(), changed)
        assert rebuilt is not books
        assert rebuilt.key_count == 2
        # A settings change rebuilds them too.
        retuned = await search_books.search_key_books(
            _settings(PROVIDER_RETRY_ATTEMPTS="5"), changed
        )
        assert retuned is not rebuilt
    finally:
        for provider in (first, again, changed):
            await provider.close()
