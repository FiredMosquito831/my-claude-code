"""Rotation must not be preempted, and must not be charged for the model.

Live symptom (v5.71.0-v5.73.0): repeated ``MODEL DEADLINE: ... produced no
first token after 66.6s`` on one NVIDIA model walked all three keys up a
10/30/60/120s cooldown ladder and tripped their breakers, after which every
request was refused outright with ``All API keys for this provider are in
cooldown. Retry in 119s.`` The first fix exempted timeouts from health but
still rotated on them, and added a per-credential first-token timer that
divided the attempt's share by the untried keys -- which on a three-key pool
five models deep became a 25s clock nobody configured, visible in the log as
``Credential 3 produced no first token within 25.0s of its share of this
attempt``.

The rule this module pins: a key's health moves only for a key-shaped signal
(401/403, or a 429 and the window the provider asked for), rotation happens
only for those plus transport faults, and nothing in the pool holds a clock of
its own. A model that will not answer is the model's problem, so the failure
goes back to the executor and the fallback chain tries the next model.
"""

import importlib
import inspect

import httpx
import openai
import pytest

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.providers.support import rotation_state
from tests.providers.test_credential_rotation import (
    _failure_record,
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


def _rate_limited(retry_after: float | None = None) -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="Rate limited.",
        retryable=True,
        retry_after_seconds=retry_after,
    )


# --- A timeout is the model's problem: no health, and no rotation -----------


@pytest.mark.asyncio
async def test_timeout_leaves_health_untouched_and_does_not_rotate() -> None:
    """Rotating on a timeout spends the pool on a model that is not answering.

    Every key in the pool talks to the same upstream model, so a key that
    replaces a silent one is silent too -- it just costs the request another
    full first-token wait. Raising instead hands the attempt back to the
    executor, which has a chain of *different models* to try.
    """
    state = rotation_state(3, "round_robin")
    before = _health(state)

    for _ in range(10):
        assert await state.report_failure(0, _timeout_failure()) is False

    assert _health(state) == before
    assert all(entry["state"] == "HEALTHY" for entry in state.get_metrics())


@pytest.mark.asyncio
async def test_repeated_timeouts_never_exhaust_a_three_key_pool() -> None:
    """The live scenario end to end: timeouts must never dry the pool up."""
    state = rotation_state(3, "round_robin")

    for _ in range(30):
        index = await state.acquire()
        assert index >= 0, "a timeout storm benched every credential"
        assert await state.report_failure(index, _timeout_failure()) is False

    assert await state.shortest_cooldown_remaining() == 0.0
    assert all(entry["state"] == "HEALTHY" for entry in state.get_metrics())
    assert state.selectable_indexes() == (0, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "expected_state"),
    [
        (lambda: _classified(401), "LOCKED_OUT"),
        (lambda: _classified(403), "LOCKED_OUT"),
        (lambda: _rate_limited(), "COOLDOWN"),
    ],
    ids=["401", "403", "429"],
)
async def test_only_key_shaped_failures_charge_health(
    error_factory, expected_state: str
) -> None:
    """The two signals that are about the credential, and only those two.

    Breaking the auth path once made multi-key failover silently dead, so both
    classes are pinned here rather than assumed.
    """
    state = rotation_state(3, "round_robin")
    before = _health(state)

    assert await state.report_failure(1, error_factory()) is True

    metrics = state.get_metrics()
    assert metrics[1]["state"] == expected_state
    after = _health(state)
    assert after[1] != before[1]
    assert [after[0], after[2]] == [before[0], before[2]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: _classified(500),
        lambda: _classified(502),
        lambda: _classified(503),
        lambda: _classified(410),
        lambda: _timeout_failure(),
        lambda: ExecutionFailure(
            kind=FailureKind.OVERLOADED,
            status_code=529,
            message="overloaded",
            retryable=True,
        ),
        lambda: ExecutionFailure(
            kind=FailureKind.INVALID_REQUEST,
            status_code=400,
            message="bad body",
            retryable=False,
        ),
        lambda: ExecutionFailure(
            kind=FailureKind.CONTEXT_LENGTH,
            status_code=400,
            message="too long",
            retryable=False,
        ),
        lambda: httpx.ConnectError("connection refused"),
        lambda: openai.APIConnectionError(request=httpx.Request("POST", "http://x")),
        lambda: httpx.ReadTimeout("slow"),
        lambda: TimeoutError("read timed out"),
    ],
    ids=[
        "500",
        "502",
        "503",
        "410",
        "timeout",
        "overloaded",
        "invalid_request",
        "context_length",
        "transport",
        "sdk_transport",
        "read_timeout",
        "unclassified_timeout",
    ],
)
async def test_nothing_else_ever_charges_a_credential(error_factory) -> None:
    """A pool of three keys serving a chain of ten models sees all of these.

    Charging any of them is what produced 1,529 "all API keys are in cooldown"
    answers in a single day on a working three-key pool.
    """
    state = rotation_state(3, "round_robin")
    before = _health(state)

    for _ in range(10):
        await state.report_failure(0, error_factory())

    assert _health(state) == before
    assert all(entry["state"] == "HEALTHY" for entry in state.get_metrics())


@pytest.mark.asyncio
async def test_the_pool_holds_no_clock_of_its_own() -> None:
    """A slow first credential is not a reason to abandon it.

    The per-credential first-token budget divided the executor's per-model
    share by the untried keys; with three keys on a 75s share that is a 25s
    timer no operator configured, and every expiry was reported as a timeout
    on a key that was only connecting slowly.
    """
    from my_claude_code.providers.runtime import rotating as rotating_module

    assert not hasattr(rotating_module, "MIN_CREDENTIAL_FIRST_TOKEN_SECONDS")
    assert not hasattr(rotating_module.RotatingProvider, "_first_token_budget")

    # The context variable the executor published for it is gone too: nothing
    # downstream of routing is told when the attempt's share runs out.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("my_claude_code.core.attempt_budget")


@pytest.mark.asyncio
async def test_a_timeout_reaches_the_executor_with_the_pool_intact() -> None:
    """End to end: the first key's timeout raises; no other key is spent."""
    stalled = _FakeProvider(fail_before_first=_timeout_failure())
    untried = _FakeProvider(chunks=("answer",))
    another = _FakeProvider(chunks=("answer",))
    rotating = _rotating([stalled, untried, another], "round_robin")
    before = _failure_record(rotating._state)

    with pytest.raises(ExecutionFailure) as excinfo:
        [chunk async for chunk in rotating.stream_response(_request())]

    assert excinfo.value.kind is FailureKind.TIMEOUT
    assert [stalled.calls, untried.calls, another.calls] == [1, 0, 0]
    assert _failure_record(rotating._state) == before


# --- throttle_remaining() must see health, not just rate limiters -----------


@pytest.mark.asyncio
async def test_throttle_remaining_reports_a_bench_when_every_key_is_benched() -> None:
    """A health-benched pool used to report itself free.

    ``throttle_remaining`` was a ``min()`` over the sub-providers' rate
    limiters only, so a pool with every key benched -- but none of them
    rate-limited at the limiter -- still answered 0. Routing skipped its
    step-over, committed the attempt, and the request ate a full
    ``ApplicationUnavailableError`` round trip.
    """
    rotating = _rotating([_FakeProvider(), _FakeProvider()], "round_robin")

    assert rotating.throttle_remaining() == 0.0

    await rotating._state.report_failure(0, _rate_limited(retry_after=7.0))
    # One key still healthy: the pool can serve, so it is not throttled.
    assert rotating.throttle_remaining() == 0.0

    await rotating._state.report_failure(1, _rate_limited(retry_after=7.0))
    remaining = rotating.throttle_remaining()
    assert 0 < remaining <= 7.0


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


def test_the_state_requires_its_settings() -> None:
    """No hidden defaults: the pool is configured, or it does not build.

    ``CREDENTIAL_CIRCUIT_THRESHOLD`` used to be the one knob here and it
    configured a breaker that no longer exists. What is left -- the 429 window
    and the auth ladder -- has to come from settings, so both are required
    keywords rather than constants with a fallback.
    """
    parameters = inspect.signature(CredentialRotationState.__init__).parameters
    for name in ("rate_limit_seconds", "lockout_tiers"):
        assert parameters[name].default is inspect.Parameter.empty
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert "circuit_threshold" not in parameters


# --------------------------------------------------------------------------
# The pool wrapper's half of the (key, model) bench.
# --------------------------------------------------------------------------


def _model_request(model: str) -> MessagesRequest:
    return MessagesRequest(model=model, messages=[Message(role="user", content="hi")])


def _scoped_pool(count: int, policy: str = "round_robin") -> RotatingProvider:
    """A pool wired the way the factory wires it: escalation 2."""
    providers = [_FakeProvider() for _ in range(count)]
    config = ProviderConfig(
        api_key="k1",
        base_url="http://x",
        api_keys=tuple(f"k{i + 1}" for i in range(count)),
        credential_rotation=policy,
    )
    state = rotation_state(count, policy, model_bench_escalation=2)
    return RotatingProvider(config, providers, state)


@pytest.mark.asyncio
async def test_unavailable_now_includes_keys_benched_for_this_model() -> None:
    """Nothing pinned ``_unavailable_now`` before; the union shape is #216."""
    pool = _scoped_pool(3)

    await pool._state.report_failure(1, _rate_limited(429.0), model="kimi")

    assert pool._unavailable_now("kimi") == frozenset({1})
    assert pool._unavailable_now("nemotron") == frozenset()
    # Without a model the pool answers about limiter windows only, exactly as
    # it did before: the engine keeps a model-benched slot HEALTHY.
    assert pool._unavailable_now() == frozenset()


@pytest.mark.asyncio
async def test_the_step_over_asks_about_the_model_the_attempt_will_send() -> None:
    """``routed.request.model`` is the same string ``report_failure`` gets.

    Any other spelling -- the provider-prefixed ref, say -- would make every
    (key, model) bench invisible to the executor's cooldown step-over. This
    pins the two ends against each other rather than against a literal.
    """
    pool = _scoped_pool(1, "round_robin")
    request = _model_request("moonshotai/kimi-k3")
    seen: list[str | None] = []

    class _Recorder(_FakeProvider):
        def stream_response(
            self,
            request: MessagesRequest,
            input_tokens: int = 0,
            *,
            request_id: str | None = None,
            reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
        ):
            seen.append(request.model)
            return super().stream_response(
                request,
                input_tokens,
                request_id=request_id,
                reasoning=reasoning,
            )

    recorder = _Recorder()
    pool._providers = (recorder,)

    async for _ in pool.stream_response(request):
        pass

    assert seen == [request.model]
    # And the model the step-over would ask about is that same string.
    await pool._state.report_failure(0, _rate_limited(60.0), model=request.model)
    assert pool._state.model_benched_indexes(request.model) == (0,)


@pytest.mark.asyncio
async def test_the_pool_reports_no_key_for_this_model_distinctly_from_no_key_at_all() -> (
    None
):
    trace = install_ladder_trace()
    pool = _scoped_pool(2)

    for index in (0, 1):
        await pool._state.report_failure(index, _rate_limited(60.0), model="kimi")

    with pytest.raises(ApplicationUnavailableError) as scoped:
        async for _ in pool.stream_response(_model_request("kimi")):
            pass

    assert "rate-limited for kimi" in str(scoped.value)
    assert "use another model on this provider" in str(scoped.value).lower()
    rows = ladder_payload(trace.slot())["tries"]
    assert rows[-1]["kind"] == "pool_benched_model"
    _LADDER.set(None)

    # Now the whole pool, and the existing sentence byte for byte.
    trace = install_ladder_trace()
    whole = _scoped_pool(2)
    for index in (0, 1):
        await whole._state.report_failure(index, _RetryableError())

    with pytest.raises(ApplicationUnavailableError) as everything:
        async for _ in whole.stream_response(_model_request("kimi")):
            pass

    assert "All API keys for this provider are in cooldown." in str(everything.value)
    rows = ladder_payload(trace.slot())["tries"]
    assert rows[-1]["kind"] == "pool_benched"
    _LADDER.set(None)


@pytest.mark.asyncio
async def test_throttle_remaining_is_zero_for_a_model_whose_keys_are_free() -> None:
    pool = _scoped_pool(2)

    await pool._state.report_failure(0, _rate_limited(60.0), model="kimi")
    await pool._state.report_failure(1, _rate_limited(60.0), model="kimi")

    assert pool.throttle_remaining("kimi") > 0
    assert pool.throttle_remaining("nemotron") == 0.0
    # No model in hand: the pool's best case over all models, and every slot
    # is still HEALTHY.
    assert pool.throttle_remaining() == 0.0


@pytest.mark.asyncio
async def test_a_scoped_bench_is_visible_in_key_health() -> None:
    """What the dashboard reads, and the whole observable win of this PR."""
    pool = _scoped_pool(3)

    for index in range(3):
        await pool._state.report_failure(index, _rate_limited(None), model="kimi")

    health = pool.key_health()
    assert [entry["state"] for entry in health] == ["HEALTHY"] * 3
    for entry in health:
        assert [bench["model"] for bench in entry["model_benches"]] == ["kimi"]
        assert entry["model_benches"][0]["remaining"] == pytest.approx(60.0, abs=1.0)
