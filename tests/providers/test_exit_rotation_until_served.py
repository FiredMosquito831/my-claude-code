"""A ticked chain keeps trying exits until one answers (7.81.0).

``specs/PR-EXIT-ROTATION-AND-REPEAT-MODELS-SPEC.md`` Part A, bound by the user's
decisions of 2026-10-06 19:49: a failure caused by the *exit* moves the same
request to another exit on the same model, instead of retrying the same exit or
benching the model -- bounded by the chain's EXISTING switch limit -- and the
refused exits are remembered across rebuilds.

Everything here is about a chain with ``until_served`` ticked. A chain without
it runs today's code: the existing proxy suites pass unmodified, and the last
section of this file drives the same scripts through an unticked chain to show
the old answers are still the answers.
"""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    DIRECT_PROXY_LABEL,
    install_proxy_attribution,
)
from my_claude_code.core.proxy_exit_memory import (
    BLOCKED,
    EXIT_MEMORY,
    SPENT,
    ExitMemory,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_COOLDOWN_SECONDS_DEFAULT,
    PROXY_HEALTH,
    PROXY_REACHABILITY,
    reset_proxy_health,
)
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
    ladder_root_cause,
    record_proxy_dial,
)
from my_claude_code.providers.base import ProviderConfig, ProxyChainPlan, ProxyLeg
from my_claude_code.providers.credential_rotation import (
    credential_failure_class,
    error_justifies_rotation,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.direct_leg import DirectFallbackLeg
from my_claude_code.providers.runtime.exit_rotation import (
    ExitsExhausted,
    exits_ran_out,
    proxy_transport_failure,
)
from my_claude_code.providers.runtime.proxy_leg import ProxiedLegRateLimiter
from my_claude_code.providers.runtime.proxy_rotating import (
    ProxyRotatingProvider,
    ProxyRotationState,
)
from tests.providers.test_credential_rotation import _FakeProvider, _request

pytestmark = pytest.mark.asyncio

LABELS = tuple(f"203.0.113.{index}:1080" for index in range(1, 7))
PROVIDER = "zen_like"
CREDENTIAL = "sha256:cred-a"


@pytest.fixture(autouse=True)
def _clean():
    reset_proxy_health()
    EXIT_MEMORY.clear()
    _LADDER.set(None)
    _CURRENT.set(None)
    yield
    reset_proxy_health()
    EXIT_MEMORY.clear()
    _LADDER.set(None)
    _CURRENT.set(None)


# ------------------------------------------------------------------ errors


def _rate_limit(retry_after: float | None = None) -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="Rate limit exceeded. Please try again later.",
        retryable=True,
        retry_after_seconds=retry_after,
    )


def _status_error(status: int, body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://host.test/v1/responses")
    response = httpx.Response(status, request=request, json=body)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _region(kind: FailureKind = FailureKind.MODEL_REJECTED) -> ExecutionFailure:
    """OpenCode's documented refusal, classified the way the provider does."""

    failure = ExecutionFailure(
        kind=kind, status_code=403, message="The model was rejected.", retryable=False
    )
    failure.__cause__ = _status_error(
        403,
        {
            "type": "error",
            "error": {
                "type": "RegionError",
                "message": "This model is not available in your country.",
            },
        },
    )
    return failure


def _model_rejected() -> ExecutionFailure:
    failure = ExecutionFailure(
        kind=FailureKind.MODEL_REJECTED,
        status_code=403,
        message="The model was rejected.",
        retryable=False,
    )
    failure.__cause__ = _status_error(
        403, {"error": {"message": "model `x` is not supported for this key"}}
    )
    return failure


def _quota() -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.QUOTA,
        status_code=402,
        message="Insufficient credits",
        retryable=False,
        retry_after_seconds=60.0,
    )


# -------------------------------------------------------------------- legs


class _Leg(_FakeProvider):
    """A rung answering a script of outcomes, one per stream it opens."""

    def __init__(self, *outcomes: Exception | str) -> None:
        super().__init__()
        self.script = list(outcomes) or ["ok"]
        self.wait = 0.0

    def throttle_remaining(self, model: str | None = None) -> float:
        return self.wait

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self.calls += 1
        outcome = self.script[min(self.calls - 1, len(self.script) - 1)]

        async def _gen() -> AsyncIterator[str]:
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "drop-after-first":
                yield "first"
                raise httpx.ReadError("dropped mid-stream")
            yield f"served by {self.label}"

        return _gen()

    label = ""


def _legs(*scripts: tuple[Exception | str, ...]) -> list[_Leg]:
    legs = []
    for index, script in enumerate(scripts):
        leg = _Leg(*script)
        leg.label = LABELS[index]
        legs.append(leg)
    return legs


def _plan(
    count: int,
    *,
    until_served: bool = True,
    on: frozenset[str] = frozenset({"quota", "rate_limit", "timeout"}),
    max_switches: int = 2,
    direct_fallback: bool = False,
) -> ProxyChainPlan:
    return ProxyChainPlan(
        legs=tuple(
            ProxyLeg(url=f"socks5h://{label}", label=label) for label in LABELS[:count]
        ),
        policy="failover",
        on=on,
        max_switches=max_switches,
        direct_fallback=direct_fallback,
        until_served=until_served,
    )


def _pool(
    legs: list[_Leg],
    *,
    until_served: bool = True,
    direct: _FakeProvider | None = None,
    gated_direct: _FakeProvider | None = None,
    max_live_failures: int = 0,
    memory: ExitMemory | None = None,
    clock=None,
    **plan_kwargs,
) -> ProxyRotatingProvider:
    """A chain over ``legs``; ``direct`` an ungated Direct leg, ``gated_direct``
    the factory's 7.79.2 Direct fallback around a leaf."""

    count = len(legs)
    plan = _plan(
        count,
        until_served=until_served,
        direct_fallback=bool(direct or gated_direct),
        **plan_kwargs,
    )
    labels = LABELS[:count]
    extra = {} if clock is None else {"clock": clock}
    state = ProxyRotationState(
        count,
        plan.policy,
        labels=labels,
        provider_id=PROVIDER,
        until_served=until_served,
        credential=CREDENTIAL,
        credential_label="…blic",
        memory=memory,
        **extra,
    )
    config = ProviderConfig(api_key="public", base_url="http://x", proxy_chain=plan)
    if gated_direct is not None:
        leaf = gated_direct

        def build(index: int):
            if index < count:
                return legs[index]
            return DirectFallbackLeg(
                config,
                build=lambda: leaf,
                state=lambda: state,
                labels=labels,
                scope=plan.scope,
                provider_id=PROVIDER,
                name="Zen Like",
            )

        return ProxyRotatingProvider(
            config,
            build,
            state,
            labels=labels,
            plan=plan,
            provider_id=PROVIDER,
            max_live_failures=max_live_failures,
            name="Zen Like",
        )
    prebuilt = [*legs, *([direct] if direct is not None else [])]
    return ProxyRotatingProvider(
        config,
        prebuilt,
        state,
        labels=labels,
        plan=plan,
        provider_id=PROVIDER,
        max_live_failures=max_live_failures,
        name="Zen Like",
    )


async def _drain(provider) -> list[str]:
    return [chunk async for chunk in provider.stream_response(_request())]


def _calls(legs: list[_Leg]) -> list[int]:
    return [leg.calls for leg in legs]


def _remembered(label: str):
    return EXIT_MEMORY.recall(PROVIDER, CREDENTIAL, label)


# ----------------------------------------------- refusals move, same request


async def test_a_429_moves_the_same_request_to_the_next_exit_and_is_remembered():
    legs = _legs((_rate_limit(412.0),), ("ok",), ("ok",))
    pool = _pool(legs)

    assert await _drain(pool) == [f"served by {LABELS[1]}"]

    assert _calls(legs) == [1, 1, 0]
    record = _remembered(LABELS[0])
    assert record is not None
    assert record.state == SPENT
    assert record.stated_wait == 412.0
    assert EXIT_MEMORY.remaining(record) == pytest.approx(412.0, abs=1.0)
    # The exit that answered is not remembered as anything.
    assert _remembered(LABELS[1]) is None


async def test_a_country_refusal_moves_without_the_model_rejected_chip():
    legs = _legs((_region(),), ("ok",))
    pool = _pool(legs, on=frozenset({"rate_limit"}))

    assert await _drain(pool) == [f"served by {LABELS[1]}"]

    record = _remembered(LABELS[0])
    assert record is not None and record.state == BLOCKED
    assert EXIT_MEMORY.remaining(record) == pytest.approx(
        PROXY_COOLDOWN_SECONDS_DEFAULT, abs=1.0
    )


async def test_a_country_refusal_filed_as_authentication_still_moves():
    """The host's words say the address is refused, whatever kind it got."""

    legs = _legs((_region(FailureKind.AUTHENTICATION),), ("ok",))
    pool = _pool(legs)

    assert await _drain(pool) == [f"served by {LABELS[1]}"]


async def test_a_403_without_the_region_words_is_not_the_exit_s():
    error = _model_rejected()
    legs = _legs((error,), ("ok",))
    pool = _pool(legs, on=frozenset({"rate_limit"}))

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is error
    assert _calls(legs) == [1, 0]
    assert _remembered(LABELS[0]) is None


async def test_a_dropped_connection_moves_and_puts_the_exit_on_the_ledger():
    legs = _legs((httpx.ReadError("peer closed"),), ("ok",))
    pool = _pool(legs)

    assert await _drain(pool) == [f"served by {LABELS[1]}"]

    assert PROXY_REACHABILITY.is_unhealthy(LABELS[0])
    assert "dropped" in PROXY_REACHABILITY.reason(LABELS[0])


async def test_a_drop_counts_against_live_failures_not_against_switches():
    legs = _legs(
        (httpx.RemoteProtocolError("no response"),),
        (httpx.ReadError("reset"),),
        (_rate_limit(),),
        ("ok",),
    )
    pool = _pool(legs, max_switches=1)

    assert await _drain(pool) == [f"served by {LABELS[3]}"]
    assert _calls(legs) == [1, 1, 1, 1]


async def test_a_drop_after_the_first_chunk_does_not_move():
    legs = _legs(("drop-after-first",), ("ok",))
    pool = _pool(legs)
    stream = pool.stream_response(_request())

    assert await anext(stream) == "first"
    with pytest.raises(httpx.ReadError):
        await anext(stream)

    assert _calls(legs) == [1, 0]


async def test_a_pool_timeout_is_this_computer_s_not_the_exit_s():
    assert proxy_transport_failure(httpx.PoolTimeout("pool"), proxied=True) is None
    assert proxy_transport_failure(httpx.ReadTimeout("slow"), proxied=True) == (
        "ReadTimeout"
    )
    assert proxy_transport_failure(httpx.ReadError("x"), proxied=False) is None
    assert (
        proxy_transport_failure(
            httpx.ReadError("x"), proxied=True, before_first_chunk=False
        )
        is None
    )


# --------------------------------------------------------- the existing cap


async def test_the_existing_switch_limit_bounds_the_exits_a_request_tries():
    """User decision 19:49: respect the existing "Switches per request"."""

    last = _rate_limit()
    legs = _legs((_rate_limit(),), (_rate_limit(),), (last,), ("ok",))
    pool = _pool(legs, max_switches=2)

    with pytest.raises(ExitsExhausted) as caught:
        await _drain(pool)

    assert _calls(legs) == [1, 1, 1, 0]
    failure = caught.value
    assert failure.kind is FailureKind.UNAVAILABLE
    assert failure.status_code == 502
    assert failure.retryable is False
    assert failure.__cause__ is last
    assert "switch limit (2 per request) was reached" in failure.message
    assert "3 \N{MULTIPLICATION SIGN} 429 rate limit" in failure.message
    # Not a key charge, not a (key, model) bench: the next model gets its turn.
    assert credential_failure_class(failure) is None
    assert error_justifies_rotation(failure) is True


async def test_quota_at_the_switch_limit_is_still_re_raised_verbatim():
    """A key out of credits is still charged as one."""

    quota = _quota()
    legs = _legs((_rate_limit(),), (_rate_limit(),), (quota,), ("ok",))
    pool = _pool(legs, max_switches=2)

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is quota
    assert credential_failure_class(caught.value) == "quota"


async def test_a_plain_model_rejection_keeps_today_s_bound_and_shape():
    rejected = _model_rejected()
    legs = _legs((_model_rejected(),), (rejected,), ("ok",))
    pool = _pool(legs, on=frozenset({"model_rejected"}), max_switches=1)

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is rejected
    assert _calls(legs) == [1, 1, 0]


async def test_the_live_failure_bound_still_applies():
    legs = _legs(
        (httpx.ConnectError("refused"),),
        (httpx.ReadError("reset"),),
        ("ok",),
    )
    pool = _pool(legs, max_live_failures=2)

    with pytest.raises(ExitsExhausted) as caught:
        await _drain(pool)

    assert _calls(legs) == [1, 1, 0]
    assert "tried 2 (2 \N{MULTIPLICATION SIGN} unreachable or dropped)" in (
        caught.value.message
    )


# ------------------------------------------------------------ the memory


async def test_remembered_exits_are_skipped_without_a_dial_after_a_rebuild():
    """The memory is process-wide: a new pool (a chain save) still skips them."""

    first = _legs((_rate_limit(300.0),), (_region(),), ("ok",))
    assert await _drain(_pool(first)) == [f"served by {LABELS[2]}"]

    rebuilt = _legs(("ok",), ("ok",), ("ok",))
    assert await _drain(_pool(rebuilt)) == [f"served by {LABELS[2]}"]

    assert _calls(rebuilt) == [0, 0, 1]


async def test_an_expired_memory_lets_the_exit_serve_and_a_success_clears_it():
    class Clock:
        now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    memory = ExitMemory(clock=clock)
    first = _legs((_rate_limit(60.0),), ("ok",))
    assert await _drain(_pool(first, memory=memory, clock=clock)) == [
        f"served by {LABELS[1]}"
    ]
    assert memory.recall(PROVIDER, CREDENTIAL, LABELS[0]) is not None

    clock.now += 61.0
    again = _legs(("ok",), ("ok",))
    assert await _drain(_pool(again, memory=memory, clock=clock)) == [
        f"served by {LABELS[0]}"
    ]
    assert memory.records() == ()


async def test_a_success_through_a_remembered_exit_clears_it():
    pool = _pool(_legs(("ok",), ("ok",)))
    EXIT_MEMORY.remember(
        PROVIDER, CREDENTIAL, LABELS[1], state=SPENT, seconds=300, reason="rate_limit"
    )

    await pool._state.report_success(1)

    assert _remembered(LABELS[1]) is None


async def test_every_exit_remembered_and_direct_off_dials_nothing():
    for label in LABELS[:3]:
        EXIT_MEMORY.remember(
            PROVIDER, CREDENTIAL, label, state=SPENT, seconds=300, reason="rate_limit"
        )
    legs = _legs(("ok",), ("ok",), ("ok",))
    pool = _pool(legs)

    with pytest.raises(ExitsExhausted) as caught:
        await _drain(pool)

    assert _calls(legs) == [0, 0, 0]
    assert caught.value.message.startswith(
        "No exit in Zen Like's proxy chain can carry a request right now"
    )
    assert "3 skipped from memory" in caught.value.message


async def test_every_exit_remembered_and_direct_on_goes_direct():
    """The 7.79.2 rule sees a remembered exit as the unhealthy one it is."""

    for label in LABELS[:3]:
        EXIT_MEMORY.remember(
            PROVIDER, CREDENTIAL, label, state=SPENT, seconds=300, reason="rate_limit"
        )
    legs = _legs(("ok",), ("ok",), ("ok",))
    home = _Leg("ok")
    home.label = DIRECT_PROXY_LABEL
    pool = _pool(legs, gated_direct=home)

    assert await _drain(pool) == ["served by direct"]
    assert _calls(legs) == [0, 0, 0]


async def test_the_switch_limit_never_ends_in_direct():
    legs = _legs((_rate_limit(),), (_rate_limit(),), ("ok",))
    home = _Leg("ok")
    pool = _pool(legs, gated_direct=home, max_switches=1)

    with pytest.raises(ExitsExhausted):
        await _drain(pool)

    assert home.calls == 0


async def test_a_healthy_exit_left_still_withholds_direct():
    """The live-failure bound ends the walk with an untried exit: no Direct."""

    legs = _legs((httpx.ConnectError("refused"),), ("ok",))
    home = _Leg("ok")
    pool = _pool(legs, gated_direct=home, max_live_failures=1)

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert home.calls == 0
    assert "Direct fallback" in caught.value.message


# ------------------------------------------------- throttle_remaining contract


async def test_throttle_is_zero_while_an_unopened_exit_is_free():
    legs = _legs(("ok",), ("ok",))
    pool = _pool(legs)
    legs[0].wait = 30.0
    pool._pool.get(0)

    assert pool.throttle_remaining() == 0.0


async def test_throttle_reads_an_open_leg_s_own_wait_when_it_is_the_last_free():
    legs = _legs(("ok",), ("ok",))
    pool = _pool(legs)
    EXIT_MEMORY.remember(
        PROVIDER, CREDENTIAL, LABELS[1], state=SPENT, seconds=300, reason="rate_limit"
    )
    legs[0].wait = 12.0
    pool._pool.get(0)

    assert pool.throttle_remaining() == 12.0


async def test_throttle_is_the_soonest_expiry_when_every_exit_is_remembered():
    pool = _pool(_legs(("ok",), ("ok",)))
    EXIT_MEMORY.remember(
        PROVIDER, CREDENTIAL, LABELS[0], state=SPENT, seconds=300, reason="rate_limit"
    )
    EXIT_MEMORY.remember(
        PROVIDER, CREDENTIAL, LABELS[1], state=BLOCKED, seconds=40, reason="country"
    )

    assert pool.throttle_remaining() == pytest.approx(40.0, abs=1.0)


# --------------------------------------- an exit still inside its own wait


async def test_a_free_exit_is_preferred_to_one_still_in_its_wait():
    """A leg a 429 left in a reactive block would make the request sleep there."""

    legs = _legs(("ok",), ("ok",))
    pool = _pool(legs)
    legs[0].wait = 600.0
    pool._pool.get(0)

    assert await _drain(pool) == [f"served by {LABELS[1]}"]
    assert _calls(legs) == [0, 1]


async def test_with_every_exit_waiting_the_request_waits_as_it_does_today():
    """Nothing that was answered before is refused now."""

    legs = _legs(("ok",), ("ok",))
    pool = _pool(legs)
    for leg in legs:
        leg.wait = 600.0
    pool._pool.get(0)
    pool._pool.get(1)

    assert await _drain(pool) == [f"served by {LABELS[0]}"]


async def test_unticked_a_waiting_exit_is_dialled_as_before():
    legs = _legs(("ok",), ("ok",))
    pool = _pool(legs, until_served=False)
    legs[0].wait = 600.0
    pool._pool.get(0)

    assert await _drain(pool) == [f"served by {LABELS[0]}"]


# ------------------------------------------------------------ the request log


async def test_the_ladder_says_what_is_remembered_and_how_the_exits_ran_out():
    install_proxy_attribution(on_dial=record_proxy_dial)
    trace = install_ladder_trace()
    EXIT_MEMORY.remember(
        PROVIDER, CREDENTIAL, LABELS[3], state=SPENT, seconds=300, reason="rate_limit"
    )
    legs = _legs(
        (_rate_limit(412.0),),
        (_region(),),
        (httpx.ReadError("reset"),),
        ("ok",),
    )
    pool = _pool(legs, max_switches=1)

    with pytest.raises(ExitsExhausted) as caught:
        await _drain(pool)

    payload = ladder_payload(trace.ladders[0])
    memories = [row.get("memory", "") for row in payload["dials"]]
    assert memories[0].startswith("remembered spent until ")
    assert memories[0].endswith("UTC (stated 412 s)")
    assert memories[1].startswith("remembered blocked for its country until ")
    # The country refusal spent the one switch; the limit ended the walk there.
    assert len(memories) == 2
    assert payload["exits_skipped_by_memory"] == 1
    assert payload["exits_exhausted"] == caught.value.message
    assert ladder_root_cause(payload) == caught.value.message


async def test_a_served_request_records_only_what_it_skipped():
    install_proxy_attribution(on_dial=record_proxy_dial)
    trace = install_ladder_trace()
    EXIT_MEMORY.remember(
        PROVIDER, CREDENTIAL, LABELS[0], state=SPENT, seconds=300, reason="rate_limit"
    )
    pool = _pool(_legs(("ok",), ("ok",)))

    assert await _drain(pool) == [f"served by {LABELS[1]}"]

    payload = ladder_payload(trace.ladders[0])
    assert payload["exits_skipped_by_memory"] == 1
    assert "exits_exhausted" not in payload
    assert all("memory" not in row for row in payload["dials"])


async def test_only_exit_caused_failures_end_as_the_exits_running_out():
    assert exits_ran_out(None, "", region=False)
    assert exits_ran_out(_rate_limit(), "trigger", region=False)
    assert exits_ran_out(_region(), "trigger", region=True)
    assert exits_ran_out(httpx.ConnectError("x"), "reachability", region=False)
    assert not exits_ran_out(_quota(), "trigger", region=False)
    assert not exits_ran_out(_model_rejected(), "trigger", region=False)


# ------------------------------------------------ the leg's own retry ladder


class _Calls:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.count = 0

    async def __call__(self) -> str:
        self.count += 1
        raise self.error


def _limiter() -> ProxiedLegRateLimiter:
    """A proxied leg's limiter as the factory builds one, nothing armed."""

    return ProxiedLegRateLimiter(
        rate_limit=0,
        rate_window=60,
        max_retries=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.01,
        backoff_jitter_seconds=0.0,
        routes_around_model=False,
    )


async def _run(limiter: ProviderRateLimiter, error: BaseException) -> int:
    calls = _Calls(error)
    with pytest.raises(type(error)):
        await asyncio.wait_for(limiter.execute_with_retry(calls), 10)
    return calls.count


@pytest.mark.parametrize(
    "error",
    [
        _status_error(429, {"error": {"type": "FreeUsageLimitError"}}),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("closed"),
    ],
    ids=["429", "ReadError", "RemoteProtocolError"],
)
async def test_a_ticked_leg_surfaces_what_the_exit_refused_on_the_first_try(error):
    ticked = _limiter().arm_exit_stops(transport=True, rate_limit=True)
    unticked = _limiter()

    assert await _run(ticked, error) == 1
    # Today's leg, unticked: the same exit is knocked on again.
    assert await _run(unticked, error) == 4


async def test_a_ticked_leg_keeps_its_5xx_retries_and_the_unarmed_429():
    error = _status_error(502, {"error": {"message": "bad gateway"}})
    ticked = _limiter().arm_exit_stops(transport=True, rate_limit=True)
    assert await _run(ticked, error) == 4

    no_chip = _limiter().arm_exit_stops(transport=True, rate_limit=False)
    assert await _run(no_chip, _status_error(429, {"error": {}})) == 4


async def test_a_ticked_leg_s_stops_never_leak_a_cancellation():
    limiter = _limiter().arm_exit_stops(transport=True, rate_limit=True)
    await _run(limiter, httpx.ReadError("reset"))

    # The task carries on and can still sleep: no pending cancel was left.
    await asyncio.sleep(0.01)
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0


# --------------------------------------- without the box: today's answers


async def test_unticked_the_switch_limit_re_raises_the_429_verbatim():
    last = _rate_limit()
    legs = _legs((_rate_limit(),), (_rate_limit(),), (last,), ("ok",))
    pool = _pool(legs, until_served=False, max_switches=2)

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is last
    assert credential_failure_class(caught.value) == "rate_limit"
    assert EXIT_MEMORY.records() == ()


async def test_unticked_a_country_refusal_and_a_drop_end_the_chain_as_before():
    region = _region()
    pool = _pool(_legs((region,), ("ok",)), until_served=False)
    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)
    assert caught.value is region

    drop = httpx.ReadError("reset")
    pool = _pool(_legs((drop,), ("ok",)), until_served=False)
    with pytest.raises(httpx.ReadError) as dropped:
        await _drain(pool)
    assert dropped.value is drop
    assert not PROXY_REACHABILITY.is_unhealthy(LABELS[0])


async def test_unticked_the_throttle_is_still_the_open_legs_minimum():
    legs = _legs(("ok",), ("ok",))
    pool = _pool(legs, until_served=False)
    legs[0].wait = 30.0
    pool._pool.get(0)

    assert pool.throttle_remaining() == 30.0


async def test_unticked_a_trigger_bench_is_forgotten_by_a_rebuild_as_before():
    first = _legs((_rate_limit(300.0),), ("ok",))
    assert await _drain(_pool(first, until_served=False)) == [f"served by {LABELS[1]}"]
    assert PROXY_HEALTH.snapshot(PROVIDER, LABELS[0])["state"] == "cooldown"

    rebuilt = _legs(("ok",), ("ok",))
    assert await _drain(_pool(rebuilt, until_served=False)) == [
        f"served by {LABELS[0]}"
    ]
    assert EXIT_MEMORY.records() == ()
