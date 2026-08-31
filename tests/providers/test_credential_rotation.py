"""Tests for multi-credential parsing, rotation state, and the rotating wrapper."""

from collections.abc import AsyncIterator

import httpx
import openai
import pytest

from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.credential_rotation import PoolHealthState
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import (
    CredentialRotationState,
    error_justifies_rotation,
)
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import maybe_await_aclose
from my_claude_code.providers.runtime.config import (
    build_provider_config,
    credential_rotation_policy,
)
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.providers.support import rotation_state


class _RetryableError(Exception):
    status_code = 429


class _InvalidRequestError(Exception):
    status_code = 400


def _rate_limited(*, retry_after: float | None) -> ExecutionFailure:
    """A provider-classified 429, with or without the upstream's own window."""
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="Rate limited.",
        retryable=True,
        retry_after_seconds=retry_after,
    )


def _timeout() -> ExecutionFailure:
    """What a model that never produced a first token looks like here."""
    return ExecutionFailure(
        kind=FailureKind.TIMEOUT,
        status_code=504,
        message="Provider produced no first token.",
        retryable=True,
    )


def _classified_unavailable() -> ExecutionFailure:
    """How providers classify a dead socket: transport, not the credential."""
    return ExecutionFailure(
        kind=FailureKind.UNAVAILABLE,
        status_code=500,
        message="Connection failed.",
        retryable=True,
    )


def _settings(**overrides) -> Settings:
    # conftest disables dotenv loading for Settings during tests.
    return Settings(**overrides)


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="test-model",
        messages=[Message(role="user", content="hi")],
    )


def test_parse_credential_keys_splits_and_strips():
    assert parse_credential_keys("k1, k2 ,k3") == ("k1", "k2", "k3")
    assert parse_credential_keys("solo") == ("solo",)
    assert parse_credential_keys("") == ()


def test_credential_rotation_policy_defaults_to_single():
    descriptor = PROVIDER_CATALOG["nvidia_nim"]
    assert credential_rotation_policy(descriptor, _settings()) == "single"


def test_credential_rotation_policy_reads_process_env(monkeypatch):
    descriptor = PROVIDER_CATALOG["nvidia_nim"]
    monkeypatch.setenv("NVIDIA_NIM_API_KEY_ROTATION", "round_robin")
    assert credential_rotation_policy(descriptor, _settings()) == "round_robin"


def test_credential_rotation_policy_ignores_unknown_values(monkeypatch):
    descriptor = PROVIDER_CATALOG["nvidia_nim"]
    monkeypatch.setenv("NVIDIA_NIM_API_KEY_ROTATION", "bogus")
    assert credential_rotation_policy(descriptor, _settings()) == "single"


def test_build_provider_config_parses_multiple_keys():
    descriptor = PROVIDER_CATALOG["nvidia_nim"]
    config = build_provider_config(
        descriptor, _settings(nvidia_nim_api_key="k1,k2 , k3")
    )
    assert config.api_keys == ("k1", "k2", "k3")
    assert config.api_key == "k1"
    assert config.credential_rotation == "single"


@pytest.mark.asyncio
async def test_round_robin_state_advances():
    state = rotation_state(3, "round_robin")
    assert await state.acquire() == 0
    assert await state.acquire() == 1
    assert await state.acquire() == 2
    assert await state.acquire() == 0


@pytest.mark.asyncio
async def test_on_error_state_sticks_then_fails_over():
    state = rotation_state(2, "on_error")
    assert await state.acquire() == 0
    assert await state.acquire() == 0
    rotate = await state.report_failure(0, _RetryableError())
    assert rotate is True
    assert await state.acquire() == 1


@pytest.mark.asyncio
async def test_backed_off_keys_are_skipped_in_round_robin():
    state = rotation_state(3, "round_robin")
    await state.report_failure(1, _RetryableError())
    assert await state.acquire() == 0
    assert await state.acquire() == 2
    assert await state.acquire() == 0


@pytest.mark.asyncio
async def test_least_used_picks_least_requested_healthy_key():
    state = rotation_state(3, "least_used")
    assert await state.acquire() == 0
    assert await state.acquire() == 1
    assert await state.acquire() == 2
    # All used once; key 0 was used longest ago
    assert await state.acquire() == 0
    # Bench key 0; least-used must skip it
    await state.report_failure(0, _RetryableError())
    assert await state.acquire() == 1


@pytest.mark.asyncio
async def test_failover_sticks_to_first_healthy_key():
    state = rotation_state(3, "failover")
    assert await state.acquire() == 0
    assert await state.acquire() == 0
    await state.report_failure(0, _RetryableError())
    assert await state.acquire() == 1
    assert await state.acquire() == 1


@pytest.mark.asyncio
async def test_a_429_benches_for_the_configured_window_without_a_ladder():
    """No tier, no escalation: every 429 without a header waits the same."""
    state = rotation_state(1, "failover", rate_limit_seconds=45.0)

    for _ in range(4):
        await state.report_failure(0, _RetryableError())
        metrics = state.get_metrics()[0]
        assert metrics["state"] == "COOLDOWN"
        assert 44.0 < metrics["cooldown_remaining"] <= 45.0


@pytest.mark.asyncio
async def test_a_run_of_generic_failures_never_benches_a_key():
    """The breaker is gone: nothing but auth and 429 moves a key's health."""
    state = rotation_state(1, "failover")
    before = _health(state)

    for _ in range(10):
        assert await state.report_failure(0, Exception("boom")) is False

    assert state.get_metrics()[0]["state"] == "HEALTHY"
    assert _health(state) == before


@pytest.mark.asyncio
async def test_auth_failures_escalate_lockout_tiers():
    state = rotation_state(2, "failover")

    class _AuthError(Exception):
        status_code = 401

    await state.report_failure(0, _AuthError())
    metrics = state.get_metrics()[0]
    assert metrics["state"] == "LOCKED_OUT"
    assert 290.0 < metrics["lockout_remaining"] <= 300.0

    await state.report_failure(0, _AuthError())
    metrics = state.get_metrics()[0]
    assert 3500.0 < metrics["lockout_remaining"] <= 3600.0

    await state.report_failure(0, _AuthError())
    metrics = state.get_metrics()[0]
    assert 86300.0 < metrics["lockout_remaining"] <= 86400.0


@pytest.mark.asyncio
async def test_acquire_returns_minus_one_when_all_keys_benched():
    state = rotation_state(2, "round_robin")
    await state.report_failure(0, _RetryableError())
    await state.report_failure(1, _RetryableError())
    assert await state.acquire() == -1
    wait = await state.shortest_cooldown_remaining()
    assert 0 < wait <= 60.0


@pytest.mark.asyncio
async def test_report_success_restores_health():
    state = rotation_state(1, "failover")
    await state.report_failure(0, _RetryableError())
    await state.report_success(0)
    metrics = state.get_metrics()[0]
    assert metrics["state"] == "HEALTHY"
    assert await state.acquire() == 0


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (_RetryableError, True),
        (_InvalidRequestError, False),
        (lambda: openai.APITimeoutError(httpx.Request("POST", "http://x")), False),
        (lambda: httpx.ConnectError("refused"), True),
        (
            lambda: openai.APIConnectionError(
                request=httpx.Request("POST", "http://x")
            ),
            True,
        ),
    ],
    ids=["429", "400", "sdk_timeout", "transport", "sdk_transport"],
)
def test_error_justifies_rotation(factory, expected: bool):
    assert error_justifies_rotation(factory()) is expected


class _FakeProvider(BaseProvider):
    """Provider double yielding canned chunks with optional failure points."""

    def __init__(
        self,
        *,
        chunks: tuple[str, ...] = ("chunk",),
        fail_before_first: Exception | None = None,
        fail_after_first: Exception | None = None,
    ) -> None:
        super().__init__(ProviderConfig(api_key="k", base_url="http://x"))
        self._chunks = chunks
        self._fail_before_first = fail_before_first
        self._fail_after_first = fail_after_first
        self.calls = 0

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"test-model"})

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self.calls += 1
        chunks = self._chunks
        fail_before = self._fail_before_first
        fail_after = self._fail_after_first

        async def _gen() -> AsyncIterator[str]:
            if fail_before is not None:
                raise fail_before
            first = True
            for chunk in chunks:
                yield chunk
                if first and fail_after is not None:
                    raise fail_after
                first = False

        return _gen()


def _rotating(providers: list[_FakeProvider], policy: str) -> RotatingProvider:
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=tuple(f"k{i + 1}" for i in range(len(providers))),
        credential_rotation=policy,
    )
    state = rotation_state(len(providers), policy)
    return RotatingProvider(config, providers, state)


@pytest.mark.asyncio
async def test_rotating_provider_round_robin_across_requests():
    first = _FakeProvider(chunks=("a",))
    second = _FakeProvider(chunks=("b",))
    provider = _rotating([first, second], "round_robin")

    assert [c async for c in provider.stream_response(_request())] == ["a"]
    assert [c async for c in provider.stream_response(_request())] == ["b"]
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_rotating_provider_fails_over_before_first_chunk():
    first = _FakeProvider(fail_before_first=_RetryableError())
    second = _FakeProvider(chunks=("ok",))
    provider = _rotating([first, second], "on_error")

    assert [c async for c in provider.stream_response(_request())] == ["ok"]
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_rotating_provider_does_not_rotate_non_rotatable_errors():
    first = _FakeProvider(fail_before_first=_InvalidRequestError())
    second = _FakeProvider(chunks=("ok",))
    provider = _rotating([first, second], "on_error")

    with pytest.raises(_InvalidRequestError):
        [c async for c in provider.stream_response(_request())]
    assert second.calls == 0


@pytest.mark.asyncio
async def test_rotating_provider_does_not_retry_after_output_started():
    first = _FakeProvider(chunks=("partial",), fail_after_first=_RetryableError())
    second = _FakeProvider(chunks=("ok",))
    provider = _rotating([first, second], "on_error")

    chunks: list[str] = []
    with pytest.raises(_RetryableError):
        async for chunk in provider.stream_response(_request()):
            chunks.append(chunk)  # noqa: PERF401 - incremental capture must keep partial chunks
    assert chunks == ["partial"]
    assert second.calls == 0


@pytest.mark.asyncio
async def test_single_policy_still_counts_usage():
    """Regression: the ``single`` fast path used to skip usage bookkeeping."""
    state = rotation_state(3, "single")
    for _ in range(4):
        assert await state.acquire() == 0
    metrics = state.get_metrics()
    assert metrics[0]["request_count"] == 4
    assert [m["request_count"] for m in metrics[1:]] == [0, 0]


@pytest.mark.asyncio
async def test_single_key_pool_counts_usage():
    """Regression: a one-key pool reported zero requests under any policy."""
    state = rotation_state(1, "round_robin")
    for _ in range(3):
        assert await state.acquire() == 0
    assert state.get_metrics()[0]["request_count"] == 3


@pytest.mark.asyncio
async def test_unrelated_failures_do_not_escalate_the_auth_lockout():
    """Regression: transient errors used to inflate the auth lockout tier.

    Two 5xx followed by a single 401 jumped straight to the 24-hour tier,
    benching a healthy credential for a day after one blip.
    """

    class _AuthError(Exception):
        status_code = 401

    state = rotation_state(2, "failover")
    await state.report_failure(0, _RetryableError())
    await state.report_failure(0, _RetryableError())
    await state.report_failure(0, _AuthError())

    metrics = state.get_metrics()[0]
    assert metrics["state"] == "LOCKED_OUT"
    # First auth failure => first tier (5 minutes), not the 24-hour tier.
    assert 290.0 < metrics["lockout_remaining"] <= 300.0


@pytest.mark.asyncio
async def test_success_clears_the_auth_escalation():
    class _AuthError(Exception):
        status_code = 401

    state = rotation_state(1, "single")
    await state.report_failure(0, _AuthError())
    await state.report_success(0)
    await state.report_failure(0, _AuthError())
    metrics = state.get_metrics()[0]
    assert 290.0 < metrics["lockout_remaining"] <= 300.0


@pytest.mark.asyncio
async def test_rate_limit_benches_for_the_window_the_provider_published():
    """The provider's own Retry-After wins over anything configured here."""
    state = rotation_state(2, "round_robin", rate_limit_seconds=600.0)

    assert await state.report_failure(0, _rate_limited(retry_after=7.0)) is True

    metrics = state.get_metrics()[0]
    assert metrics["state"] == "COOLDOWN"
    assert 6.0 < metrics["cooldown_remaining"] <= 7.0
    assert metrics["rate_limits"] == 1


@pytest.mark.asyncio
async def test_rate_limit_without_a_header_uses_the_configured_cooldown():
    """No signal from the provider is the only time our own number applies."""
    state = rotation_state(2, "round_robin", rate_limit_seconds=45.0)

    assert await state.report_failure(0, _rate_limited(retry_after=None)) is True

    metrics = state.get_metrics()[0]
    assert metrics["state"] == "COOLDOWN"
    # 45, not the engine's own 60.0 default: the setting reaches the pool.
    assert 44.0 < metrics["cooldown_remaining"] <= 45.0


@pytest.mark.asyncio
async def test_lockout_tiers_come_from_settings():
    """The one surviving ladder is the operator's, not a hardcoded one."""
    state = rotation_state(1, "single", lockout_tiers=(1.0, 2.0))

    class _AuthError(Exception):
        status_code = 401

    await state.report_failure(0, _AuthError())
    assert 0.0 < state.get_metrics()[0]["lockout_remaining"] <= 1.0

    await state.report_failure(0, _AuthError())
    assert 1.0 < state.get_metrics()[0]["lockout_remaining"] <= 2.0

    # Clamped at the last entry rather than running off the end.
    await state.report_failure(0, _AuthError())
    assert 1.0 < state.get_metrics()[0]["lockout_remaining"] <= 2.0


@pytest.mark.asyncio
async def test_no_half_open_state_exists() -> None:
    """The probe machinery is gone, and with it the way it stranded a key.

    A half-open slot was reserved on acquire and released only by an explicit
    success or failure, so a request that reported neither -- a disconnect, a
    cancellation, or any of the failure classes that no longer charge health
    -- left the credential permanently unselectable. A bench now expires
    straight back to HEALTHY.
    """
    assert not hasattr(PoolHealthState, "HALF_OPEN")
    assert "HALF_OPEN" not in {member.name for member in PoolHealthState}

    clock = _ManualClock()
    state = rotation_state(2, "round_robin", rate_limit_seconds=30.0, clock=clock)
    await state.report_failure(0, _RetryableError())
    assert state.get_metrics()[0]["state"] == "COOLDOWN"

    clock.now += 31.0
    assert state.selectable_indexes() == (0, 1)
    assert state.get_metrics()[0]["state"] == "HEALTHY"
    # Selectable twice running: nothing reserves the recovered credential.
    assert await state.acquire() == 0
    assert await state.acquire() == 1


@pytest.mark.asyncio
async def test_client_disconnect_does_not_bench_the_credential():
    first = _FakeProvider(chunks=("a", "b", "c"))
    second = _FakeProvider(chunks=("z",))
    provider = _rotating([first, second], "round_robin")

    stream = provider.stream_response(_request())
    assert await stream.__anext__() == "a"
    await maybe_await_aclose(stream)  # client went away mid-stream

    metrics = provider.key_health()[0]
    assert metrics["state"] == "HEALTHY"


@pytest.mark.asyncio
async def test_mid_stream_failure_counts_against_the_credential():
    """Regression: failures after the first chunk were never recorded."""
    first = _FakeProvider(chunks=("partial",), fail_after_first=_RetryableError())
    second = _FakeProvider(chunks=("ok",))
    provider = _rotating([first, second], "round_robin")

    with pytest.raises(_RetryableError):
        async for _chunk in provider.stream_response(_request()):
            pass

    metrics = provider.key_health()[0]
    assert metrics["failure_count"] == 1
    assert metrics["state"] == "COOLDOWN"


@pytest.mark.asyncio
async def test_key_health_reports_index_and_masked_label():
    providers = [_FakeProvider(chunks=("a",)), _FakeProvider(chunks=("b",))]
    config = ProviderConfig(
        api_key="alpha-secret-0001",
        base_url="http://x",
        api_keys=("alpha-secret-0001", "beta-secret-0002"),
        credential_rotation="round_robin",
    )
    state = rotation_state(2, "round_robin")
    rotating = RotatingProvider(
        config,
        providers,
        state,
        key_labels=("alph…0001", "beta…0002"),
    )

    health = rotating.key_health()
    assert [entry["index"] for entry in health] == [0, 1]
    assert [entry["key_label"] for entry in health] == ["alph…0001", "beta…0002"]
    # The raw credential must never appear in a health snapshot.
    assert "alpha-secret-0001" not in repr(health)


@pytest.mark.asyncio
async def test_rotating_provider_records_the_credential_it_used():
    from my_claude_code.core.credential_attribution import install_attribution

    first = _FakeProvider(fail_before_first=_RetryableError())
    second = _FakeProvider(chunks=("ok",))
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=("k1", "k2"),
        credential_rotation="failover",
    )
    state = rotation_state(2, "failover")
    provider = RotatingProvider(
        config, [first, second], state, key_labels=("…key1", "…key2")
    )

    slot = install_attribution()
    assert [c async for c in provider.stream_response(_request())] == ["ok"]
    # After failover the credential that actually served the request wins.
    assert slot.index == 1
    assert slot.label == "…key2"


def test_base_provider_exposes_a_masked_credential_label():
    provider = _FakeProvider()
    assert provider.credential_label == "…"
    labelled = _FakeProvider()
    labelled._config = ProviderConfig(api_key="abcdefghijklmnop", base_url="http://x")
    assert labelled.credential_label == "abcd…mnop"


def test_rotating_provider_has_no_single_credential_label():
    provider = _rotating([_FakeProvider(), _FakeProvider()], "round_robin")
    assert provider.credential_label is None


def test_admin_manifest_exposes_rotation_select_for_nvidia_nim():
    field = FIELD_BY_KEY.get("NVIDIA_NIM_API_KEY_ROTATION")
    assert field is not None
    assert field.field_type == "select"
    assert set(field.options) == {"single", "round_robin", "least_used", "failover"}
    assert field.restart_required is True


def _classified(status: int) -> ExecutionFailure:
    """The failure shape a sub-provider actually raises for an upstream status."""
    request = httpx.Request("POST", "https://upstream.invalid/v1/chat")
    return classify_provider_failure(
        openai.APIStatusError(
            "upstream", response=httpx.Response(status, request=request), body=None
        ),
        provider_name="test",
        read_timeout_s=60.0,
        request_id="req_test",
        mark_rate_limited=lambda *_args, **_kwargs: None,
    )


@pytest.mark.parametrize("status", [401, 403])
def test_classified_auth_failures_justify_rotation(status: int) -> None:
    """Regression: a rejected credential must fail over, not fail the request.

    Providers classify their own failures before the rotating wrapper sees
    them, so a 401 arrives as ExecutionFailure(retryable=False). Rotation used
    to read that as "do not rotate", so a revoked or exhausted key failed the
    request outright instead of trying a working one.
    """
    failure = _classified(status)
    assert failure.retryable is False
    assert error_justifies_rotation(failure) is True


def test_classified_bad_request_still_does_not_rotate() -> None:
    assert error_justifies_rotation(_classified(400)) is False


@pytest.mark.asyncio
async def test_rotating_provider_fails_over_a_rejected_credential():
    """End-to-end: a 401 on key 0 must be served by key 1."""
    first = _FakeProvider(fail_before_first=_classified(401))
    second = _FakeProvider(chunks=("ok",))
    provider = _rotating([first, second], "failover")

    assert [c async for c in provider.stream_response(_request())] == ["ok"]
    assert first.calls == 1
    assert second.calls == 1
    assert provider.key_health()[0]["state"] == "LOCKED_OUT"


@pytest.mark.asyncio
async def test_acquire_avoids_unavailable_credentials() -> None:
    """A throttled credential must be skipped while another can serve."""
    state = rotation_state(3, "round_robin")
    picks = {await state.acquire(frozenset({0})) for _ in range(6)}
    assert picks == {1, 2}


@pytest.mark.asyncio
async def test_acquire_falls_back_when_every_credential_is_unavailable() -> None:
    """Total throttling must queue on a limiter, not hard-fail the request."""
    state = rotation_state(2, "round_robin")
    index = await state.acquire(frozenset({0, 1}))
    assert index in (0, 1)


@pytest.mark.asyncio
async def test_unavailable_credentials_are_still_skipped_when_benched() -> None:
    state = rotation_state(3, "round_robin")
    await state.report_failure(2, _RetryableError())
    picks = {await state.acquire(frozenset({0})) for _ in range(4)}
    assert picks == {1}


class _ThrottledProvider(_FakeProvider):
    """Sub-provider whose credential is rate-limited for a fixed window."""

    def __init__(self, *, throttled_for: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._throttled_for = throttled_for

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._throttled_for


@pytest.mark.asyncio
async def test_rotating_provider_skips_a_rate_limited_credential():
    """Regression: a throttled key stayed HEALTHY and absorbed traffic.

    Rotation had no view of the limiter, so it selected the throttled
    credential and the request sat waiting inside that credential's own
    limiter while an idle credential went unused.
    """
    throttled = _ThrottledProvider(throttled_for=30.0, chunks=("slow",))
    idle = _FakeProvider(chunks=("fast",))
    provider = _rotating([throttled, idle], "round_robin")

    assert [c async for c in provider.stream_response(_request())] == ["fast"]
    assert throttled.calls == 0
    assert idle.calls == 1


@pytest.mark.asyncio
async def test_rotating_provider_uses_a_throttled_credential_when_all_are():
    """With nothing idle left, the request still goes out rather than failing."""
    first = _ThrottledProvider(throttled_for=30.0, chunks=("a",))
    second = _ThrottledProvider(throttled_for=30.0, chunks=("b",))
    provider = _rotating([first, second], "round_robin")

    chunks = [c async for c in provider.stream_response(_request())]
    assert chunks in (["a"], ["b"])


def test_key_health_reports_the_throttle_window() -> None:
    providers = [_ThrottledProvider(throttled_for=12.0), _FakeProvider()]
    config = ProviderConfig(api_key="k1", base_url="http://x", api_keys=("k1", "k2"))
    rotating = RotatingProvider(config, providers, rotation_state(2, "round_robin"))
    health = rotating.key_health()
    assert health[0]["throttle_remaining"] == 12.0
    assert health[1]["throttle_remaining"] == 0.0


def test_rotating_provider_reports_the_shortest_credential_cooldown() -> None:
    """Routing asks one provider "can you serve now"; any free key means yes.

    ``BaseProvider`` answers 0 unconditionally, so an un-overridden rotating
    provider claimed to be free with every one of its keys rate-limited --
    which is exactly the wait the model chain now steps over.
    """
    rotating = _rotating(
        [_ThrottledProvider(throttled_for=30.0), _ThrottledProvider(throttled_for=5.0)],
        "round_robin",
    )
    assert rotating.throttle_remaining() == 5.0


def test_rotating_provider_is_free_while_one_credential_can_serve() -> None:
    rotating = _rotating(
        [_ThrottledProvider(throttled_for=30.0), _FakeProvider()], "round_robin"
    )
    assert rotating.throttle_remaining() == 0.0


def test_a_fully_rate_limited_rotating_provider_reports_a_cooldown() -> None:
    rotating = _rotating(
        [
            _ThrottledProvider(throttled_for=30.0),
            _ThrottledProvider(throttled_for=30.0),
        ],
        "round_robin",
    )
    assert rotating.throttle_remaining() == 30.0


class _ManualClock:
    """Callable clock a test can advance deterministically."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- Request-shaped failures must not be charged to a credential ------------
#
# A malformed request fails identically on every key, so counting it against
# the key that happened to carry it escalated that key's cooldown ladder and,
# after three in a row, its circuit breaker. A pool of healthy keys could be
# driven entirely into cooldown by a bug in the outbound request.


def _request_failure(kind: FailureKind, message: str) -> ExecutionFailure:
    """The shape a provider raises for a request-shaped 400."""
    return ExecutionFailure(
        kind=kind, status_code=400, message=message, retryable=False
    )


def _invalid_request() -> ExecutionFailure:
    # The exact upstream wording from the live NVIDIA NIM incident.
    return _request_failure(
        FailureKind.INVALID_REQUEST,
        "Validation: top_p is immutable for this model and must be 0.95",
    )


def _context_length() -> ExecutionFailure:
    return _request_failure(
        FailureKind.CONTEXT_LENGTH,
        "Request exceeds this model's context window.",
    )


def _failure_record(state: CredentialRotationState) -> list[dict[str, object]]:
    """Everything a failure could move, and nothing a request moves anyway.

    ``request_count`` climbs on every acquire, success or not, so a snapshot
    that included it could never answer "did this failure cost the key
    anything".
    """
    return [
        {
            key: value
            for key, value in entry.items()
            if key not in {"request_count"} and not key.endswith("_remaining")
        }
        for entry in state.get_metrics()
    ]


def _health(state: CredentialRotationState) -> list[dict[str, object]]:
    """Full per-credential health snapshot, minus the wall-clock remainders."""
    return [
        {k: v for k, v in entry.items() if not k.endswith("_remaining")}
        for entry in state.get_metrics()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [_invalid_request, _context_length],
    ids=["invalid_request", "context_length"],
)
async def test_request_shaped_failure_leaves_health_byte_identical(
    error_factory,
) -> None:
    state = rotation_state(2, "round_robin")
    before = _health(state)

    for _ in range(10):
        assert await state.report_failure(0, error_factory()) is False

    assert _health(state) == before
    assert all(entry["state"] == "HEALTHY" for entry in state.get_metrics())


@pytest.mark.asyncio
async def test_duck_typed_400_also_leaves_health_untouched() -> None:
    """Not every provider raises a canonical failure; a bare 400 counts too."""
    state = rotation_state(2, "round_robin")
    before = _health(state)

    for _ in range(10):
        assert await state.report_failure(1, _InvalidRequestError()) is False

    assert _health(state) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_still_lock_out_and_escalate(status: int) -> None:
    """Regression guard: the failover multi-key rotation exists for."""
    state = rotation_state(2, "failover")

    assert await state.report_failure(0, _classified(status)) is True
    first = state.get_metrics()[0]
    assert first["state"] == "LOCKED_OUT"
    assert first["auth_failures"] == 1
    assert 290.0 < first["lockout_remaining"] <= 300.0

    assert await state.report_failure(0, _classified(status)) is True
    second = state.get_metrics()[0]
    assert second["auth_failures"] == 2
    assert 3590.0 < second["lockout_remaining"] <= 3600.0


@pytest.mark.asyncio
async def test_upstream_and_transport_failures_never_charge_health() -> None:
    """The live cause of 1,529 "all keys in cooldown" answers in one day.

    A 410 on one chain entry, 5xx from an overloaded upstream and first-token
    timeouts were all charged to whichever key carried them, walking three
    working credentials up a cooldown ladder for faults none of them caused.
    """
    state = rotation_state(2, "round_robin")
    before = _health(state)

    for _ in range(5):
        await state.report_failure(0, _classified(503))
        await state.report_failure(0, _classified(500))
        await state.report_failure(0, _classified(410))
        await state.report_failure(0, httpx.ConnectError("boom"))
        await state.report_failure(0, httpx.ReadTimeout("slow"))
        await state.report_failure(0, _timeout())

    assert _health(state) == before
    assert [entry["state"] for entry in state.get_metrics()] == ["HEALTHY"] * 2


@pytest.mark.asyncio
async def test_rotation_follows_the_credential_shaped_rule() -> None:
    """Rotating and charging health are separate answers to separate questions."""
    state = rotation_state(1, "single")

    # Key-shaped: rotate.
    assert await state.report_failure(0, _classified(401)) is True
    assert await state.report_failure(0, _RetryableError()) is True
    # Transport: rotate for free -- another key is another connection.
    assert await state.report_failure(0, httpx.ConnectError("boom")) is True
    assert await state.report_failure(0, _classified_unavailable()) is True
    # Model- or request-shaped: raise, so the chain tries the next model.
    assert await state.report_failure(0, _classified(503)) is False
    assert await state.report_failure(0, _classified(410)) is False
    assert await state.report_failure(0, _timeout()) is False
    assert await state.report_failure(0, _invalid_request()) is False
    assert await state.report_failure(0, _context_length()) is False
    assert await state.report_failure(0, _InvalidRequestError()) is False


@pytest.mark.asyncio
async def test_repeated_400s_never_dry_up_a_healthy_pool() -> None:
    """The live incident: three good keys, one malformed request, pool empty.

    Every request 400'd on `top_p is immutable`; each 400 benched the key that
    carried it, and within a few requests the pool answered "All API keys for
    this provider are in cooldown".
    """
    state = rotation_state(3, "round_robin")

    for _ in range(60):
        index = await state.acquire()
        assert index >= 0, "pool ran dry on request-shaped failures"
        assert await state.report_failure(index, _invalid_request()) is False

    assert [entry["state"] for entry in state.get_metrics()] == ["HEALTHY"] * 3


@pytest.mark.asyncio
async def test_rotating_provider_survives_a_run_of_invalid_requests() -> None:
    """End-to-end: the wrapper still raises the 400, and never benches a key."""
    providers = [_FakeProvider(fail_before_first=_invalid_request()) for _ in range(3)]
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=("k1", "k2", "k3"),
        credential_rotation="round_robin",
    )
    state = rotation_state(3, "round_robin")
    rotating = RotatingProvider(config, providers, state)

    for _ in range(20):
        with pytest.raises(ExecutionFailure):
            [c async for c in rotating.stream_response(_request())]

    assert [entry["state"] for entry in rotating.key_health()] == ["HEALTHY"] * 3


@pytest.mark.asyncio
async def test_rotating_provider_raises_a_timeout_without_touching_the_pool():
    """The behaviour the whole PR exists for.

    Live symptom: one three-key pool, a chain five models deep, and a
    ``Credential 3 produced no first token within 25.0s of its share of this
    attempt`` line for a timer nobody configured. A model that does not answer
    is the model's problem, so the pool must hand the failure straight back and
    let the executor try the next model -- with every key untouched, and the
    key that was actually tried recorded for analytics.
    """
    from my_claude_code.core.credential_attribution import install_attribution

    providers = [
        _FakeProvider(fail_before_first=_timeout()),
        _FakeProvider(chunks=("never",)),
        _FakeProvider(chunks=("never",)),
    ]
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=("k1", "k2", "k3"),
        credential_rotation="failover",
    )
    state = rotation_state(3, "failover")
    rotating = RotatingProvider(
        config, providers, state, key_labels=("…key1", "…key2", "…key3")
    )
    before = _failure_record(state)

    slot = install_attribution()
    with pytest.raises(ExecutionFailure) as excinfo:
        [c async for c in rotating.stream_response(_request())]

    assert excinfo.value.kind is FailureKind.TIMEOUT
    assert [p.calls for p in providers] == [1, 0, 0]
    assert _failure_record(state) == before
    assert [entry["state"] for entry in rotating.key_health()] == ["HEALTHY"] * 3
    assert (slot.index, slot.label) == (0, "…key1")


@pytest.mark.asyncio
async def test_rotating_provider_rotates_on_a_connection_error_for_free():
    """Transport faults still fail over -- and still cost the key nothing."""
    first = _FakeProvider(fail_before_first=httpx.ConnectError("refused"))
    second = _FakeProvider(chunks=("ok",))
    state = rotation_state(2, "failover")
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=("k1", "k2"),
        credential_rotation="failover",
    )
    rotating = RotatingProvider(config, [first, second], state)
    before = _failure_record(state)

    assert [c async for c in rotating.stream_response(_request())] == ["ok"]
    assert first.calls == 1
    assert second.calls == 1
    # Key 0's failure counters are untouched; key 1 only gained a success.
    assert _failure_record(state) == before
    assert [entry["state"] for entry in rotating.key_health()] == ["HEALTHY"] * 2


@pytest.mark.asyncio
async def test_a_five_hundred_walks_the_chain_not_the_pool():
    """A 5xx must reach the executor rather than burn the remaining keys."""
    providers = [_FakeProvider(fail_before_first=_classified(503)) for _ in range(3)]
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=("k1", "k2", "k3"),
        credential_rotation="round_robin",
    )
    state = rotation_state(3, "round_robin")
    rotating = RotatingProvider(config, providers, state)

    for _ in range(20):
        with pytest.raises(ExecutionFailure):
            [c async for c in rotating.stream_response(_request())]

    assert [entry["state"] for entry in rotating.key_health()] == ["HEALTHY"] * 3


# --------------------------------------------------------- the retry ladder --


@pytest.mark.asyncio
async def test_bench_decision_records_class_retry_after_and_cooldown() -> None:
    """The bench duration is read back out of the engine that decided it."""
    trace = install_ladder_trace()
    state = rotation_state(2, "failover")

    await state.report_failure(0, _rate_limited(retry_after=None))
    await state.report_failure(1, _rate_limited(retry_after=12.0))

    decisions = ladder_payload(trace.slot())["credentials"]
    assert [entry["key_index"] for entry in decisions] == [0, 1]
    assert decisions[0]["class"] == "rate_limit"
    assert decisions[0]["status"] == 429
    assert decisions[0]["retry_after"] is None
    # The operator's cooldown, not a number invented in the ladder.
    assert decisions[0]["benched_for_s"] == pytest.approx(60.0, abs=1.0)
    assert "no Retry-After" in decisions[0]["reason"]
    assert decisions[1]["retry_after"] == 12.0
    assert decisions[1]["benched_for_s"] == pytest.approx(12.0, abs=1.0)
    assert "Retry-After 12s" in decisions[1]["reason"]
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_uncharged_failure_records_a_null_class_with_its_reason() -> None:
    """ "Health unchanged" was a DEBUG line and nothing else, before this."""
    trace = install_ladder_trace()
    state = rotation_state(2, "failover")

    await state.report_failure(0, _InvalidRequestError())

    decision = ladder_payload(trace.slot())["credentials"][0]
    assert decision["class"] is None
    assert decision["benched_for_s"] is None
    assert decision["status"] == 400
    assert decision["reason"] == "400 is not credential-shaped"
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_an_auth_failure_records_its_lockout_tier() -> None:
    class _AuthError(Exception):
        status_code = 401

    trace = install_ladder_trace()
    state = rotation_state(2, "failover")

    await state.report_failure(0, _AuthError())

    decision = ladder_payload(trace.slot())["credentials"][0]
    assert decision["class"] == "auth"
    assert decision["status"] == 401
    assert "lockout tier" in decision["reason"]
    assert decision["benched_for_s"] is not None
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_recording_a_decision_does_not_change_the_health_verdict() -> None:
    """Behaviour is identical with the ladder switched off."""
    _LADDER.set(None)
    state = rotation_state(2, "failover")

    assert await state.report_failure(0, _rate_limited(retry_after=None)) is True
    assert await state.report_failure(1, _InvalidRequestError()) is False
    metrics = state.get_metrics()
    assert metrics[0]["state"] != "HEALTHY"
    assert metrics[1]["state"] == "HEALTHY"


# --------------------------------------------------------------------------
# The (key, model) 429 bench, as the async adapter records and reports it.
# The pool defaults to escalation 1 -- never scope -- so every test above
# still describes exactly the behaviour it was written for; these pass the
# runtime value (2) explicitly.
# --------------------------------------------------------------------------


def _kimi_request() -> MessagesRequest:
    return MessagesRequest(
        model="moonshotai/kimi-k3",
        messages=[Message(role="user", content="hi")],
    )


@pytest.mark.asyncio
async def test_report_failure_passes_the_model_through_to_the_engine() -> None:
    """Getting this string wrong makes every bench invisible to the step-over."""
    state = rotation_state(3, "round_robin", model_bench_escalation=2)

    await state.report_failure(
        0, _rate_limited(retry_after=None), model="moonshotai/kimi-k3"
    )

    slot = state._engine.slot(0)
    assert slot.state is PoolHealthState.HEALTHY
    assert set(slot.model_benches) == {"moonshotai/kimi-k3"}
    assert state.model_benched_indexes("moonshotai/kimi-k3") == (0,)
    assert state.model_benched_indexes("nvidia/nemotron-3-ultra-550b-a55b") == ()


@pytest.mark.asyncio
async def test_a_scoped_bench_records_the_model_on_the_credential_decision() -> None:
    trace = install_ladder_trace()
    state = rotation_state(3, "round_robin", model_bench_escalation=2)

    await state.report_failure(
        0, _rate_limited(retry_after=None), model="moonshotai/kimi-k3"
    )

    decision = ladder_payload(trace.slot())["credentials"][0]
    assert decision["class"] == "rate_limit"
    assert decision["status"] == 429
    assert decision["model"] == "moonshotai/kimi-k3"
    # The substring the unscoped assertion pins is still there.
    assert "no Retry-After" in decision["reason"]
    assert "moonshotai/kimi-k3 benched" in decision["reason"]
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_a_scoped_bench_leaves_benched_for_s_absent_and_sets_model_benched_for_s() -> (
    None
):
    """Two different facts: the pair's bench, and the credential's."""
    trace = install_ladder_trace()
    state = rotation_state(2, "round_robin", model_bench_escalation=2)

    await state.report_failure(
        0, _rate_limited(retry_after=None), model="moonshotai/kimi-k3"
    )

    decision = ladder_payload(trace.slot())["credentials"][0]
    assert decision["benched_for_s"] is None
    assert decision["model_benched_for_s"] == pytest.approx(60.0, abs=1.0)
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_an_unscoped_bench_omits_the_model_keys_entirely() -> None:
    """Absent rather than null, exactly like every other optional ladder term."""
    trace = install_ladder_trace()
    state = rotation_state(2, "round_robin")

    await state.report_failure(0, _rate_limited(retry_after=None))

    decision = ladder_payload(trace.slot())["credentials"][0]
    assert "model" not in decision
    assert "model_benched_for_s" not in decision
    assert decision["reason"] == "429, no Retry-After -- operator cooldown 60s"
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_an_escalated_bench_says_how_many_models_were_throttled() -> None:
    trace = install_ladder_trace()
    state = rotation_state(2, "round_robin", model_bench_escalation=2)

    await state.report_failure(
        0, _rate_limited(retry_after=None), model="moonshotai/kimi-k3"
    )
    await state.report_failure(
        0, _rate_limited(retry_after=None), model="nvidia/nemotron-3-ultra"
    )

    decision = ladder_payload(trace.slot())["credentials"][1]
    assert decision["benched_for_s"] == pytest.approx(60.0, abs=1.0)
    assert "2 models throttled on this key" in decision["reason"]
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_acquire_skips_a_key_benched_for_this_model_even_when_nothing_else_is_free() -> (
    None
):
    """The avoid-relaxing second choose() still carries the model.

    Falling back to a key benched for this very model would be a guaranteed
    429, which is the one case where relaxing ``avoid`` must not help.
    """
    state = rotation_state(2, "round_robin", model_bench_escalation=2)

    await state.report_failure(
        0, _rate_limited(retry_after=None), model="moonshotai/kimi-k3"
    )
    await state.report_failure(
        1, _rate_limited(retry_after=None), model="moonshotai/kimi-k3"
    )

    assert await state.acquire(frozenset({0, 1}), model="moonshotai/kimi-k3") == -1
    # Every other model on those same keys is still served.
    assert await state.acquire(frozenset(), model="nvidia/nemotron") in (0, 1)


@pytest.mark.asyncio
async def test_get_metrics_lists_live_model_benches_soonest_expiry_last() -> None:
    state = rotation_state(1, "single", model_bench_escalation=0)

    await state.report_failure(0, _rate_limited(retry_after=30.0), model="short")
    await state.report_failure(0, _rate_limited(retry_after=300.0), model="long")

    benches = state.get_metrics()[0]["model_benches"]
    assert [entry["model"] for entry in benches] == ["long", "short"]
    assert benches[0]["remaining"] == pytest.approx(300.0, abs=1.0)
    assert benches[1]["remaining"] == pytest.approx(30.0, abs=1.0)
    assert state.get_metrics()[0]["state"] == "HEALTHY"


@pytest.mark.asyncio
async def test_a_healthy_key_with_no_model_benches_reports_an_empty_list() -> None:
    state = rotation_state(1, "single")

    assert state.get_metrics()[0]["model_benches"] == []
