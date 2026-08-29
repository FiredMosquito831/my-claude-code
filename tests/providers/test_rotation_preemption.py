"""Rotation must not be preempted before its credentials get a turn.

Live symptom (v5.71.0): repeated ``MODEL DEADLINE: ... produced no first token
after 66.6s`` on one NVIDIA model walked all three keys up the 10/30/60/120s
cooldown ladder and tripped their breakers, after which every request was
refused outright with ``All API keys for this provider are in cooldown. Retry
in 119s.`` Three separate mechanisms produced that, and this module covers all
three; the rotation loop itself was never broken.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import openai
import pytest

from my_claude_code.core.attempt_budget import set_attempt_deadline
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.credential_rotation import (
    COOLDOWN_TIERS_SECONDS,
    CredentialRotationState,
)
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.providers.test_credential_rotation import (
    _FakeProvider,
    _health,
    _request,
    _RetryableError,
    _rotating,
    _ThrottledProvider,
)


def _classified(status: int) -> ExecutionFailure:
    """A provider-classified failure carrying an upstream status."""
    return ExecutionFailure(
        kind=FailureKind.AUTHENTICATION
        if status in (401, 403)
        else FailureKind.UPSTREAM,
        status_code=status,
        message=f"upstream {status}",
        retryable=status != 401 and status != 403,
    )


def _timeout_failure() -> ExecutionFailure:
    """The shape a provider raises when the upstream produced nothing in time."""
    return ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=504,
        message="Provider produced no first token.",
        retryable=True,
    )


# --- A timeout rotates, but charges the credential nothing ------------------


@pytest.mark.asyncio
async def test_timeout_leaves_health_byte_identical_but_still_rotates() -> None:
    state = CredentialRotationState(3, "round_robin")
    before = _health(state)

    for _ in range(10):
        # True: a different key may reach a faster replica, so rotate --
        # while charging this one's health nothing.
        assert await state.report_failure(0, _timeout_failure()) is True

    assert _health(state) == before
    assert all(entry["state"] == "HEALTHY" for entry in state.get_metrics())


@pytest.mark.asyncio
async def test_repeated_timeouts_never_exhaust_a_three_key_pool() -> None:
    """The live scenario end to end: timeouts must never dry the pool up."""
    state = CredentialRotationState(3, "round_robin")

    for _ in range(30):
        index = await state.acquire()
        assert index >= 0, "a timeout storm benched every credential"
        assert await state.report_failure(index, _timeout_failure()) is True

    assert await state.shortest_cooldown_remaining() == 0.0
    assert all(entry["state"] == "HEALTHY" for entry in state.get_metrics())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "expected_state"),
    [
        (lambda: _classified(401), "LOCKED_OUT"),
        (lambda: _classified(403), "LOCKED_OUT"),
        (lambda: _classified(429), "COOLDOWN"),
        (lambda: _classified(500), "COOLDOWN"),
        (lambda: _classified(503), "COOLDOWN"),
        (lambda: httpx.ConnectError("connection refused"), "COOLDOWN"),
        (
            lambda: openai.APIConnectionError(
                request=httpx.Request("POST", "http://x")
            ),
            "COOLDOWN",
        ),
    ],
    ids=["401", "403", "429", "500", "503", "transport", "sdk_transport"],
)
async def test_credential_implicating_failures_still_charge_health(
    error_factory, expected_state: str
) -> None:
    """Regression guard: only classified timeouts were exempted.

    Breaking the auth path once made multi-key failover silently dead, so
    every other class is pinned here rather than assumed.
    """
    state = CredentialRotationState(3, "round_robin")
    before = _health(state)

    assert await state.report_failure(1, error_factory()) is True

    metrics = state.get_metrics()
    assert metrics[1]["state"] == expected_state
    # A 429 escalates the ladder without touching the consecutive-failure
    # counter -- throttled is not broken -- so the guard is that the record
    # moved at all, whichever field carries it for this class.
    after = _health(state)
    assert after[1] != before[1]
    assert [after[0], after[2]] == [before[0], before[2]]


@pytest.mark.asyncio
async def test_an_unclassified_timeout_still_charges_health() -> None:
    """Deliberately narrow: only a provider-classified TIMEOUT is exempt."""
    state = CredentialRotationState(2, "round_robin")

    assert await state.report_failure(0, TimeoutError("read timed out")) is True

    assert state.get_metrics()[0]["state"] == "COOLDOWN"


# --- One slow key must not spend the whole model attempt --------------------


class _StallingProvider(_FakeProvider):
    """Sub-provider that never produces a first chunk."""

    def stream_response(
        self,
        request,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self.calls += 1

        async def _gen() -> AsyncIterator[str]:
            await asyncio.sleep(3600)
            yield "never"

        return _gen()


@pytest.mark.asyncio
async def test_a_stalled_credential_rotates_within_the_same_attempt() -> None:
    """The executor's attempt deadline used to wrap the whole loop as one unit.

    One key stalling therefore consumed the model's entire turn and keys
    2..N were never tried at all.
    """
    stalled = _StallingProvider()
    also_stalled = _StallingProvider()
    healthy = _FakeProvider(chunks=("answer",))
    rotating = _rotating([stalled, also_stalled, healthy], "round_robin")

    set_attempt_deadline(time.monotonic() + 18.0)
    try:
        chunks = [chunk async for chunk in rotating.stream_response(_request())]
    finally:
        set_attempt_deadline(None)

    assert chunks == ["answer"]
    assert stalled.calls == 1
    assert also_stalled.calls == 1
    assert healthy.calls == 1


@pytest.mark.asyncio
async def test_a_stalled_credential_costs_the_pool_no_health() -> None:
    stalled = _StallingProvider()
    healthy = _FakeProvider(chunks=("answer",))
    state = CredentialRotationState(2, "round_robin")
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=("k1", "k2"),
        credential_rotation="round_robin",
    )
    rotating = RotatingProvider(config, [stalled, healthy], state)
    before = _health(state)

    set_attempt_deadline(time.monotonic() + 12.0)
    try:
        chunks = [chunk async for chunk in rotating.stream_response(_request())]
    finally:
        set_attempt_deadline(None)

    assert chunks == ["answer"]
    after = _health(state)
    assert [entry["state"] for entry in after] == ["HEALTHY", "HEALTHY"]
    # Only the success counters moved; no failure was charged to either key.
    assert [entry["failure_count"] for entry in after] == [
        entry["failure_count"] for entry in before
    ]


@pytest.mark.asyncio
async def test_the_per_key_budget_never_outlives_the_attempt_deadline() -> None:
    """The executor keeps the outer bound; the pool only subdivides it."""
    rotating = _rotating([_StallingProvider(), _StallingProvider()], "round_robin")

    set_attempt_deadline(time.monotonic() + 10.0)
    started = time.monotonic()
    try:
        with pytest.raises(ExecutionFailure) as excinfo:
            [chunk async for chunk in rotating.stream_response(_request())]
    finally:
        set_attempt_deadline(None)

    assert excinfo.value.kind is FailureKind.TIMEOUT
    assert time.monotonic() - started < 11.0


@pytest.mark.asyncio
async def test_without_an_attempt_deadline_the_first_token_wait_is_unbounded() -> None:
    """No executor deadline leaves the wait exactly as unbounded as before."""
    rotating = _rotating([_FakeProvider(chunks=("a",))], "single")

    set_attempt_deadline(None)
    assert rotating._first_token_budget(1) is None
    assert [chunk async for chunk in rotating.stream_response(_request())] == ["a"]


def test_the_per_key_share_divides_the_attempt_across_untried_credentials() -> None:
    rotating = _rotating([_FakeProvider() for _ in range(3)], "round_robin")

    set_attempt_deadline(time.monotonic() + 60.0)
    try:
        assert rotating._first_token_budget(3) == pytest.approx(20.0, abs=0.5)
        assert rotating._first_token_budget(2) == pytest.approx(30.0, abs=0.5)
        # Clamped to what is left, never beyond it.
        assert rotating._first_token_budget(1) == pytest.approx(60.0, abs=0.5)
    finally:
        set_attempt_deadline(None)


def test_the_per_key_share_has_a_floor() -> None:
    """A large pool must not reject keys that were only connecting slowly."""
    rotating = _rotating([_FakeProvider() for _ in range(2)], "round_robin")

    set_attempt_deadline(time.monotonic() + 8.0)
    try:
        assert rotating._first_token_budget(20) == pytest.approx(5.0, abs=0.5)
    finally:
        set_attempt_deadline(None)


# --- throttle_remaining() must see health, not just rate limiters -----------


@pytest.mark.asyncio
async def test_throttle_remaining_reports_a_bench_when_every_key_is_benched() -> None:
    """A health-benched pool used to report itself free.

    ``throttle_remaining`` was a ``min()`` over the sub-providers' rate
    limiters only, so a pool with every key in COOLDOWN/CIRCUIT_OPEN -- but
    none of them rate-limited -- still answered 0. Routing skipped its
    step-over, committed the attempt, and the request ate a full
    ``ApplicationUnavailableError`` round trip.
    """
    rotating = _rotating([_FakeProvider(), _FakeProvider()], "round_robin")

    assert rotating.throttle_remaining() == 0.0

    await rotating._state.report_failure(0, _RetryableError())
    # One key still healthy: the pool can serve, so it is not throttled.
    assert rotating.throttle_remaining() == 0.0

    await rotating._state.report_failure(1, _RetryableError())
    remaining = rotating.throttle_remaining()
    assert 0 < remaining <= COOLDOWN_TIERS_SECONDS[0]


@pytest.mark.asyncio
async def test_a_single_key_provider_never_reports_itself_benched() -> None:
    """Forced-single policies serve slot 0 regardless of health; so must this."""
    rotating = _rotating([_FakeProvider()], "single")

    for _ in range(5):
        await rotating._state.report_failure(0, _RetryableError())

    assert rotating.throttle_remaining() == 0.0


@pytest.mark.asyncio
async def test_a_benched_key_no_longer_hides_a_throttled_healthy_one() -> None:
    """The limiter is read only across credentials that could actually serve."""
    benched = _FakeProvider()
    throttled = _ThrottledProvider(throttled_for=25.0)
    rotating = _rotating([benched, throttled], "round_robin")

    await rotating._state.report_failure(0, _RetryableError())

    assert rotating.throttle_remaining() == 25.0
