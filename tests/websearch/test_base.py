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
