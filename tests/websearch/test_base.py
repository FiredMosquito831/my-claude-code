"""Base provider rotation-loop tests (acquire -> try -> report)."""

import pytest

from my_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchConfigError,
    WebSearchInvalidRequestError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.rotation import KeyHealthState
from tests.websearch.support import (
    DomainStubWebSearchProvider,
    FakeClock,
    StubWebSearchProvider,
    build_config,
)


@pytest.mark.asyncio
async def test_success_on_first_key_reports_success() -> None:
    provider = StubWebSearchProvider(build_config())
    response = await provider.search("q", max_results=3)
    assert response.provider == "stub"
    assert response.key_index == 0
    assert len(response.results) == 1
    assert provider.calls[0]["max_results"] == 3
    health = provider.key_pool.health_at(0)
    assert health.successes == 1
    assert health.state is KeyHealthState.HEALTHY


@pytest.mark.asyncio
async def test_auth_error_rotates_to_next_key_and_locks_first() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={0: WebSearchAuthError("stub", "bad key")},
    )
    response = await provider.search("q")
    assert response.key_index == 1
    first = provider.key_pool.health_at(0)
    assert first.state is KeyHealthState.LOCKED_OUT
    assert provider.key_pool.health_at(1).successes == 1


@pytest.mark.asyncio
async def test_rate_limit_error_cools_down_key_and_rotates() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={0: WebSearchRateLimitError("stub", "slow down")},
    )
    response = await provider.search("q")
    assert response.key_index == 1
    first = provider.key_pool.health_at(0)
    assert first.state is KeyHealthState.COOLDOWN
    assert first.rate_limits == 1


@pytest.mark.asyncio
async def test_invalid_request_raises_without_rotating() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={0: WebSearchInvalidRequestError("stub", "bad params")},
    )
    with pytest.raises(WebSearchInvalidRequestError):
        await provider.search("q")
    assert len(provider.calls) == 1  # key 1 never attempted
    # Request-shaped faults never reach the pool here: they are re-raised
    # before any report_failure call, so health stays completely untouched.
    health = provider.key_pool.health_at(0)
    assert health.failures == 0
    assert health.consecutive_failures == 0
    assert health.state is KeyHealthState.HEALTHY


@pytest.mark.asyncio
async def test_config_error_raises_without_rotating() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={0: WebSearchConfigError("stub", "misconfigured")},
    )
    with pytest.raises(WebSearchConfigError):
        await provider.search("q")
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_all_keys_failing_raises_last_error_with_key_index() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={
            0: WebSearchUpstreamError("stub", "boom-0"),
            1: WebSearchUpstreamError("stub", "boom-1"),
        },
    )
    with pytest.raises(WebSearchUpstreamError, match="boom-1") as exc_info:
        await provider.search("q")
    assert exc_info.value.key_index == 1
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_unexpected_exception_is_reported_and_raised() -> None:
    provider = StubWebSearchProvider(build_config(), behavior={0: RuntimeError("bug")})
    with pytest.raises(RuntimeError, match="bug"):
        await provider.search("q")
    assert provider.key_pool.health_at(0).failures == 1


@pytest.mark.asyncio
async def test_domains_ignored_when_provider_does_not_support_them() -> None:
    provider = StubWebSearchProvider(build_config())
    await provider.search("q", allowed_domains=("a.com",), blocked_domains=("b.com",))
    assert provider.calls[0]["allowed_domains"] == ()
    assert provider.calls[0]["blocked_domains"] == ()


@pytest.mark.asyncio
async def test_domains_passed_through_when_supported() -> None:
    provider = DomainStubWebSearchProvider(build_config())
    await provider.search("q", allowed_domains=("a.com",), blocked_domains=("b.com",))
    assert provider.calls[0]["allowed_domains"] == ("a.com",)
    assert provider.calls[0]["blocked_domains"] == ("b.com",)


@pytest.mark.asyncio
async def test_key_label_masks_the_serving_key() -> None:
    provider = StubWebSearchProvider(build_config(api_keys=("sk-live-0001wxyz",)))
    assert provider.key_label(0) == "sk-l…wxyz"
    response = await provider.search("q")
    assert provider.key_label(response.key_index) == "sk-l…wxyz"


@pytest.mark.asyncio
async def test_keyless_provider_uses_single_anonymous_slot() -> None:
    provider = StubWebSearchProvider(build_config(api_keys=(), rotation="single"))
    response = await provider.search("q")
    assert response.key_index == 0
    assert provider.key_pool.key_count == 1
    assert provider.key_label(0) == ""


@pytest.mark.asyncio
async def test_close_releases_client() -> None:
    import httpx

    provider = StubWebSearchProvider(build_config())
    provider._client = httpx.AsyncClient()
    await provider.close()
    assert provider._client is None
    await provider.close()  # idempotent


# ---------------------------------------------- 7.112.1: no outage, model rules


def _model_rules(error: BaseException) -> str | None:
    """A stand-in judge: charge only the key-shaped kinds."""
    kind = getattr(error, "kind", None)
    return kind if kind in ("auth", "rate_limit") else None


def _with_clock(provider: StubWebSearchProvider) -> FakeClock:
    clock = FakeClock()
    provider.key_pool._clock = clock
    provider.key_pool._engine._clock = clock
    return clock


@pytest.mark.asyncio
async def test_a_failure_the_judge_declines_leaves_the_only_key_usable() -> None:
    provider = StubWebSearchProvider(
        build_config(), behavior={0: WebSearchUpstreamError("stub", "HTTP 500")}
    )
    with pytest.raises(WebSearchUpstreamError):
        await provider.search("q", key_charge=_model_rules)
    assert provider.key_pool.health_at(0).state is KeyHealthState.HEALTHY
    provider._behavior = {}
    response = await provider.search("q", key_charge=_model_rules)
    assert response.key_index == 0
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_locked_out_only_key_is_still_tried_by_the_next_search() -> None:
    provider = StubWebSearchProvider(
        build_config(), behavior={0: WebSearchAuthError("stub", "bad key")}
    )
    for _ in range(2):
        with pytest.raises(WebSearchAuthError):
            await provider.search("q", key_charge=_model_rules)
    assert len(provider.calls) == 2  # one call each: tried again, not looped
    health = provider.key_pool.health_at(0)
    assert health.state is KeyHealthState.LOCKED_OUT
    assert health.lockouts == 2  # the lockout ladder still climbs


@pytest.mark.asyncio
async def test_inside_a_429_wait_the_only_key_answers_without_a_call() -> None:
    provider = StubWebSearchProvider(
        build_config(),
        behavior={
            0: WebSearchRateLimitError(
                "stub", "slow down", status_code=429, retry_after_seconds=7.0
            )
        },
    )
    clock = _with_clock(provider)
    with pytest.raises(WebSearchRateLimitError):
        await provider.search("q", key_charge=_model_rules)
    provider._behavior = {}
    clock.advance(0.5)
    with pytest.raises(WebSearchRateLimitError, match=r"retry after 7s") as caught:
        await provider.search("q", key_charge=_model_rules)
    assert caught.value.retry_after_seconds == 7.0
    assert len(provider.calls) == 1  # no call inside the wait
    clock.advance(7.0)
    response = await provider.search("q", key_charge=_model_rules)
    assert response.key_index == 0
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_keyless_provider_is_never_benched() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=(), rotation="single"),
        behavior={0: WebSearchRateLimitError("stub", "slow down", status_code=429)},
    )
    with pytest.raises(WebSearchRateLimitError):
        await provider.search("q")
    assert provider.key_pool.health_at(0).state is KeyHealthState.HEALTHY
    provider._behavior = {}
    await provider.search("q")
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_two_key_pool_still_rotates_and_benches_a_rejected_key() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={0: WebSearchAuthError("stub", "bad key")},
    )
    response = await provider.search("q", key_charge=_model_rules)
    assert response.key_index == 1
    assert provider.key_pool.health_at(0).state is KeyHealthState.LOCKED_OUT
    await provider.search("q", key_charge=_model_rules)
    assert [call["key_index"] for call in provider.calls] == [0, 1, 1]


@pytest.mark.asyncio
async def test_a_fully_benched_pool_says_what_is_benched_and_for_how_long() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={
            0: WebSearchAuthError("stub", "bad key"),
            1: WebSearchRateLimitError("stub", "slow", retry_after_seconds=30.0),
        },
    )
    with pytest.raises(WebSearchRateLimitError):
        await provider.search("q", key_charge=_model_rules)
    with pytest.raises(WebSearchUpstreamError) as caught:
        await provider.search("q", key_charge=_model_rules)
    assert len(provider.calls) == 2  # the second search made no call
    assert caught.value.message == (
        "all 2 keys are benched: 1 locked out (the provider rejected the key "
        "or reported no credits), 1 rate limited; the first is usable again "
        "in 30s; no call made"
    )


@pytest.mark.asyncio
async def test_a_pool_benched_only_by_429s_is_rate_limited() -> None:
    provider = StubWebSearchProvider(
        build_config(api_keys=("k1-secret-0001", "k2-secret-0002")),
        behavior={
            0: WebSearchRateLimitError("stub", "slow", retry_after_seconds=7.0),
            1: WebSearchRateLimitError("stub", "slow", retry_after_seconds=9.0),
        },
    )
    with pytest.raises(WebSearchRateLimitError):
        await provider.search("q", key_charge=_model_rules)
    with pytest.raises(WebSearchRateLimitError, match=r"all 2 keys are inside"):
        await provider.search("q", key_charge=_model_rules)
    assert len(provider.calls) == 2
