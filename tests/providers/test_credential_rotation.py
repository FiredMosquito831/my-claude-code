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
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
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


class _RetryableError(Exception):
    status_code = 429


class _InvalidRequestError(Exception):
    status_code = 400


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
    state = CredentialRotationState(3, "round_robin")
    assert await state.acquire() == 0
    assert await state.acquire() == 1
    assert await state.acquire() == 2
    assert await state.acquire() == 0


@pytest.mark.asyncio
async def test_on_error_state_sticks_then_fails_over():
    state = CredentialRotationState(2, "on_error")
    assert await state.acquire() == 0
    assert await state.acquire() == 0
    rotate = await state.report_failure(0, _RetryableError())
    assert rotate is True
    assert await state.acquire() == 1


@pytest.mark.asyncio
async def test_backed_off_keys_are_skipped_in_round_robin():
    state = CredentialRotationState(3, "round_robin")
    await state.report_failure(1, _RetryableError())
    assert await state.acquire() == 0
    assert await state.acquire() == 2
    assert await state.acquire() == 0


@pytest.mark.asyncio
async def test_least_used_picks_least_requested_healthy_key():
    state = CredentialRotationState(3, "least_used")
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
    state = CredentialRotationState(3, "failover")
    assert await state.acquire() == 0
    assert await state.acquire() == 0
    await state.report_failure(0, _RetryableError())
    assert await state.acquire() == 1
    assert await state.acquire() == 1


@pytest.mark.asyncio
async def test_cooldown_tiers_escalate_on_repeated_failures():
    state = CredentialRotationState(1, "failover")
    await state.report_failure(0, _RetryableError())
    metrics = state.get_metrics()[0]
    assert metrics["state"] == "COOLDOWN"
    assert metrics["tier"] == 1
    first = metrics["cooldown_remaining"]
    assert 9.0 < first <= 10.0

    await state.report_failure(0, _RetryableError())
    metrics = state.get_metrics()[0]
    assert metrics["tier"] == 2
    assert 29.0 < metrics["cooldown_remaining"] <= 30.0


@pytest.mark.asyncio
async def test_circuit_opens_after_three_consecutive_failures():
    state = CredentialRotationState(1, "failover")
    for _ in range(3):
        await state.report_failure(0, Exception("boom"))
    assert state.get_metrics()[0]["state"] == "CIRCUIT_OPEN"


@pytest.mark.asyncio
async def test_auth_failures_escalate_lockout_tiers():
    state = CredentialRotationState(2, "failover")

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
    state = CredentialRotationState(2, "round_robin")
    await state.report_failure(0, _RetryableError())
    await state.report_failure(1, _RetryableError())
    assert await state.acquire() == -1
    wait = await state.shortest_cooldown_remaining()
    assert 0 < wait <= 10.0


@pytest.mark.asyncio
async def test_report_success_restores_health():
    state = CredentialRotationState(1, "failover")
    await state.report_failure(0, _RetryableError())
    await state.report_success(0)
    metrics = state.get_metrics()[0]
    assert metrics["state"] == "HEALTHY"
    assert metrics["tier"] == 0
    assert await state.acquire() == 0


def test_error_justifies_rotation():
    assert error_justifies_rotation(_RetryableError()) is True
    assert error_justifies_rotation(_InvalidRequestError()) is False


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
    state = CredentialRotationState(len(providers), policy)
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
    state = CredentialRotationState(3, "single")
    for _ in range(4):
        assert await state.acquire() == 0
    metrics = state.get_metrics()
    assert metrics[0]["request_count"] == 4
    assert [m["request_count"] for m in metrics[1:]] == [0, 0]


@pytest.mark.asyncio
async def test_single_key_pool_counts_usage():
    """Regression: a one-key pool reported zero requests under any policy."""
    state = CredentialRotationState(1, "round_robin")
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

    state = CredentialRotationState(2, "failover")
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

    state = CredentialRotationState(1, "single")
    await state.report_failure(0, _AuthError())
    await state.report_success(0)
    await state.report_failure(0, _AuthError())
    metrics = state.get_metrics()[0]
    assert 290.0 < metrics["lockout_remaining"] <= 300.0


@pytest.mark.asyncio
async def test_rate_limits_do_not_open_the_circuit():
    """429 escalates the cooldown ladder but never trips the breaker."""
    state = CredentialRotationState(2, "round_robin")
    for _ in range(5):
        await state.report_failure(0, _RetryableError())
    metrics = state.get_metrics()[0]
    assert metrics["state"] == "COOLDOWN"
    assert metrics["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_abandoned_probe_is_released():
    """Regression: an abandoned half-open probe benched a key permanently.

    ``acquire`` reserves a half-open credential by setting ``is_probing``; if
    the client disconnects, neither success nor failure is reported, so the
    reservation used to stick and the credential was never selectable again.
    """
    state = CredentialRotationState(2, "round_robin")
    await state.report_failure(0, _RetryableError())
    assert await state.reset_key(0) is True
    state.release_probe(0)
    assert state.get_metrics()[0]["is_probing"] is False


@pytest.mark.asyncio
async def test_client_disconnect_does_not_bench_the_credential():
    first = _FakeProvider(chunks=("a", "b", "c"))
    second = _FakeProvider(chunks=("z",))
    provider = _rotating([first, second], "round_robin")

    stream = provider.stream_response(_request())
    assert await stream.__anext__() == "a"
    await maybe_await_aclose(stream)  # client went away mid-stream

    metrics = provider.key_health()[0]
    assert metrics["is_probing"] is False
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
    state = CredentialRotationState(2, "round_robin")
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
    state = CredentialRotationState(2, "failover")
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
    state = CredentialRotationState(3, "round_robin")
    picks = {await state.acquire(frozenset({0})) for _ in range(6)}
    assert picks == {1, 2}


@pytest.mark.asyncio
async def test_acquire_falls_back_when_every_credential_is_unavailable() -> None:
    """Total throttling must queue on a limiter, not hard-fail the request."""
    state = CredentialRotationState(2, "round_robin")
    index = await state.acquire(frozenset({0, 1}))
    assert index in (0, 1)


@pytest.mark.asyncio
async def test_unavailable_credentials_are_still_skipped_when_benched() -> None:
    state = CredentialRotationState(3, "round_robin")
    await state.report_failure(2, _RetryableError())
    picks = {await state.acquire(frozenset({0})) for _ in range(4)}
    assert picks == {1}


class _ThrottledProvider(_FakeProvider):
    """Sub-provider whose credential is rate-limited for a fixed window."""

    def __init__(self, *, throttled_for: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._throttled_for = throttled_for

    def throttle_remaining(self) -> float:
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
    rotating = RotatingProvider(
        config, providers, CredentialRotationState(2, "round_robin")
    )
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


@pytest.mark.asyncio
async def test_half_open_admits_exactly_one_probe() -> None:
    """A recovered breaker admits ONE probe until that probe settles.

    While the probe is outstanding the credential must not be handed out
    again; only its success restores full service.
    """
    clock = _ManualClock()
    state = CredentialRotationState(2, "failover", clock=clock)
    for _ in range(3):
        await state.report_failure(0, Exception("boom"))
    assert state.get_metrics()[0]["state"] == "CIRCUIT_OPEN"

    # The longest cooldown tier elapses; key 0 wakes into HALF_OPEN...
    clock.now += 61.0
    # ...and key 1 benches only now, so it stays benched throughout.
    await state.report_failure(1, _RetryableError())

    assert await state.acquire() == 0
    # The outstanding probe reserves the credential: no second admission.
    assert await state.acquire() == -1

    await state.report_success(0)
    states = [entry["state"] for entry in state.get_metrics()]
    assert states == ["HEALTHY", "COOLDOWN"]
    assert await state.acquire() == 0
