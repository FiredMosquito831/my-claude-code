"""The proxy chain moves a request along, and changes nothing above it.

This is the release where a proxy chain stopped being a stored preference and
became the request path, so the tests that matter most are the ones about what
does *not* happen. A chain is built below the credential pool, as a fan-out
over ``dataclasses.replace(config, proxy=...)``; everything above keeps
receiving one object satisfying ``BaseProvider``, and on chain exhaustion
receives the same exception object it received before the feature existed.

The nine files named in the specification's 3.2 carry an empty ``git diff`` for
this release, and the six invariant test files are unedited. These tests are
the other half of that claim: they assert the properties those files rely on,
from the outside.
"""

import inspect

import httpx
import pytest

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.config.proxy_chains import REFUSED_TRIGGER_KINDS
from my_claude_code.config.settings import Settings
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import (
    DIRECT_PROXY_LABEL,
    current_proxy,
    install_proxy_attribution,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_REACHABILITY_TIERS,
    PROXY_REFUSED_TRIGGER_KINDS,
    PROXY_TUNING,
    reset_proxy_health,
)
from my_claude_code.core.upstream_ladder import (
    install_ladder_trace,
    ladder_payload,
    ladder_proxy_label,
    record_upstream_try,
)
from my_claude_code.providers.base import ProviderConfig, ProxyChainPlan, ProxyLeg
from my_claude_code.providers.credential_rotation import credential_failure_class
from my_claude_code.providers.runtime import proxy_rotating
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.proxy_rotating import (
    ProxyRotatingProvider,
    ProxyRotationState,
    proxy_reachability_failure,
)
from tests.providers.test_credential_rotation import (
    _FakeProvider,
    _request,
)


@pytest.fixture(autouse=True)
def _clean_ledgers():
    """Both ledgers are process-wide on purpose; tests must not inherit them."""

    reset_proxy_health()
    yield
    reset_proxy_health()


def _plan(
    labels: tuple[str, ...],
    *,
    policy: str = "failover",
    on: frozenset[str] = frozenset({"quota", "rate_limit", "timeout"}),
    scope: str = "provider",
    max_switches: int = 2,
) -> ProxyChainPlan:
    return ProxyChainPlan(
        legs=tuple(
            ProxyLeg(
                url="" if label == DIRECT_PROXY_LABEL else f"http://{label}",
                label=label,
            )
            for label in labels
        ),
        policy=policy,
        on=on,
        scope=scope,
        max_switches=max_switches,
    )


def _pool(
    providers: list[_FakeProvider],
    labels: tuple[str, ...],
    **kwargs,
) -> ProxyRotatingProvider:
    plan = _plan(labels, **kwargs)
    state = ProxyRotationState(
        len(providers),
        plan.policy,
        labels=labels,
        provider_id="fake_provider",
        scope=plan.scope,
    )
    return ProxyRotatingProvider(
        ProviderConfig(api_key="k", base_url="http://x", proxy_chain=plan),
        providers,
        state,
        labels=labels,
        plan=plan,
        provider_id="fake_provider",
    )


async def _drain(provider) -> list[str]:
    return [chunk async for chunk in provider.stream_response(_request())]


def _failure(kind: FailureKind, status: int, retry_after: float | None = None):
    return ExecutionFailure(
        kind=kind,
        status_code=status,
        message=f"upstream {status}",
        retryable=True,
        retry_after_seconds=retry_after,
    )


# --------------------------------------------------------- the seam itself


def test_a_chainless_provider_is_built_exactly_as_it_was() -> None:
    """No chain, no wrapper. The default path constructs nothing new.

    This is the whole safety argument in one assertion: an operator who never
    opens the Proxying page gets the object they got before this release, and
    the class that could change a request's route is never instantiated.
    """

    provider = create_provider("nvidia_nim", Settings(nvidia_nim_api_key="k1"))

    assert not isinstance(provider, ProxyRotatingProvider)
    assert provider._config.proxy_chain is None
    assert provider._config.proxy == ""


def test_the_pool_refuses_to_exist_for_a_single_rung() -> None:
    """One address is a static proxy by another name; nothing rotates.

    Built anyway it would cost a second client, a second rate limiter and a
    second recovery ladder to do nothing with them, and it would put a switch
    loop on the request path of a provider that has nowhere to switch to.
    """

    with pytest.raises(ValueError, match="at least two rungs"):
        _pool([_FakeProvider()], ("203.0.113.7:1080",))


# ---------------------------------------------- what escapes, and unwrapped


@pytest.mark.asyncio
async def test_the_last_error_is_re_raised_unwrapped_when_the_chain_is_exhausted() -> (
    None
):
    """The credential pool above must still be able to classify it.

    Wrapping the escaped failure in an ``ApplicationUnavailableError`` would
    make ``credential_failure_class`` answer ``None``, the credential pool
    would stop charging health, and both the (key, model) bench and the
    route-around would go quiet without a single test failing.
    """

    error = _failure(FailureKind.RATE_LIMIT, 429, retry_after=12.0)
    pool = _pool(
        [
            _FakeProvider(fail_before_first=error),
            _FakeProvider(fail_before_first=error),
        ],
        ("a:1", "b:2"),
        on=frozenset({"rate_limit"}),
    )

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is error
    assert not isinstance(caught.value, ApplicationUnavailableError)
    assert credential_failure_class(caught.value) == "rate_limit"


@pytest.mark.asyncio
async def test_the_pool_never_raises_model_rate_limited() -> None:
    """That signal belongs to the credential pool and carries a key index.

    The proxy pool has no key index, and a ``ModelRateLimited`` from here would
    send the executor looking for another model behind a credential this
    wrapper cannot name.
    """

    error = _failure(FailureKind.RATE_LIMIT, 429)
    pool = _pool(
        [_FakeProvider(fail_before_first=error) for _ in range(2)],
        ("a:1", "b:2"),
        on=frozenset({"rate_limit"}),
    )

    with pytest.raises(ExecutionFailure):
        await _drain(pool)


# ------------------------------------------------------- the motivating case


@pytest.mark.asyncio
async def test_a_quota_failure_switches_address_before_it_reaches_the_pool() -> None:
    """One provider, one key, three addresses, ``on=["quota"]``.

    The case this feature was built for: the first address is out of the
    provider's per-address allowance, the second answers, and the credential
    pool above never hears about it at all.
    """

    exhausted = _failure(FailureKind.QUOTA, 402, retry_after=300.0)
    first = _FakeProvider(fail_before_first=exhausted)
    second = _FakeProvider(chunks=("hello",))
    third = _FakeProvider(chunks=("never",))
    pool = _pool(
        [first, second, third],
        ("a:1", "b:2", DIRECT_PROXY_LABEL),
        on=frozenset({"quota"}),
    )

    assert await _drain(pool) == ["hello"]
    assert first.calls == 1
    assert second.calls == 1
    assert third.calls == 0


@pytest.mark.asyncio
async def test_an_authentication_failure_never_switches_address() -> None:
    """A 401 must reach the credential pool untouched.

    It is the one choice that is actively destructive: a new address does not
    fix a rejected key, and rotating on it burns the whole chain inside one
    request and earns every address a bench. The API refuses to arm it, the
    store drops it, and the runtime would not act on it even if both failed.
    """

    rejected = _failure(FailureKind.AUTHENTICATION, 401)
    first = _FakeProvider(fail_before_first=rejected)
    second = _FakeProvider(chunks=("never",))
    pool = _pool(
        [first, second],
        ("a:1", "b:2"),
        # Armed by hand, past both refusals, to prove the runtime is the third
        # place this is refused rather than the place that trusts the other two.
        on=frozenset({"authentication", "quota"}),
    )

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is rejected
    assert second.calls == 0
    assert frozenset(REFUSED_TRIGGER_KINDS) == PROXY_REFUSED_TRIGGER_KINDS


@pytest.mark.asyncio
async def test_a_failure_nobody_armed_goes_straight_to_the_model_chain() -> None:
    """A 500 is not the address's fault and must not spend the chain on it."""

    broken = _failure(FailureKind.UPSTREAM, 500)
    first = _FakeProvider(fail_before_first=broken)
    second = _FakeProvider(chunks=("never",))
    pool = _pool([first, second], ("a:1", "b:2"), on=frozenset({"quota"}))

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is broken
    assert second.calls == 0


# --------------------------------------------------------------- the bounds


@pytest.mark.asyncio
async def test_the_switch_bound_caps_the_attempt() -> None:
    """A ten-address chain of dead addresses tries exactly three.

    Every switch spends wall-clock inside one attempt and the executor's
    deadlines do not move to make room, which is the whole reason the bound
    exists and the reason five is a real ceiling.
    """

    error = _failure(FailureKind.QUOTA, 402, retry_after=1.0)
    providers = [_FakeProvider(fail_before_first=error) for _ in range(10)]
    pool = _pool(
        providers,
        tuple(f"p{index}:1" for index in range(10)),
        on=frozenset({"quota"}),
        max_switches=2,
    )

    with pytest.raises(ExecutionFailure):
        await _drain(pool)

    assert sum(provider.calls for provider in providers) == 3


@pytest.mark.asyncio
async def test_the_bound_is_configurable_within_the_range_the_user_asked_for() -> None:
    """One to five. Five tries six addresses, which is the ceiling."""

    error = _failure(FailureKind.QUOTA, 402, retry_after=1.0)
    providers = [_FakeProvider(fail_before_first=error) for _ in range(10)]
    pool = _pool(
        providers,
        tuple(f"p{index}:1" for index in range(10)),
        on=frozenset({"quota"}),
        max_switches=5,
    )

    with pytest.raises(ExecutionFailure):
        await _drain(pool)

    assert sum(provider.calls for provider in providers) == 6


@pytest.mark.asyncio
async def test_a_switch_never_happens_after_the_first_chunk() -> None:
    """Output has started, so moving address would corrupt the response.

    The mid-stream failure still counts against the address -- one that
    consistently dies after its first chunk would otherwise never be benched --
    but the request itself is over.
    """

    dying = _failure(FailureKind.QUOTA, 402, retry_after=5.0)
    first = _FakeProvider(chunks=("a", "b"), fail_after_first=dying)
    second = _FakeProvider(chunks=("never",))
    pool = _pool([first, second], ("a:1", "b:2"), on=frozenset({"quota"}))

    received: list[str] = []
    with pytest.raises(ExecutionFailure):
        # Collected one at a time on purpose: the point of the test is the
        # chunk that arrived *before* the failure, so the loop has to survive
        # the exception the comprehension would swallow the partial result of.
        async for chunk in pool.stream_response(_request()):
            received.append(chunk)  # noqa: PERF401

    assert received == ["a"]
    assert second.calls == 0
    assert pool.proxy_health()[0]["failures"] == 1


def test_the_proxy_pool_holds_no_clock_of_its_own() -> None:
    """The mirror of the credential pool's own test, for the same reason.

    An earlier rotation wrapper divided the executor's per-attempt share by the
    untried credentials and abandoned one that produced no first token inside
    its slice; on a three-key pool five models deep that was a 25s timer nobody
    configured. The executor owns every deadline, here too.
    """

    # The code, not the prose: the module docstring names the things it must
    # not contain, so a whole-module scan would fail on its own explanation.
    source = inspect.getsource(
        proxy_rotating.ProxyRotatingProvider
    ) + inspect.getsource(proxy_rotating.ProxyRotationState)

    assert "wait_for" not in source
    assert "asyncio.timeout" not in source
    assert "asyncio.sleep" not in source
    assert "timeout=" not in source
    # ``time.monotonic`` appears once, as the engine's default clock argument,
    # which is a reader of a clock and never a deadline of this pool's own.
    assert inspect.getsource(proxy_rotating).count("time.monotonic") == 1


# ---------------------------------------------------------- the two benches


def test_a_reachability_failure_is_named_from_the_exception_chain() -> None:
    """Providers wrap their SDK's errors; the original is the ``__cause__``."""

    wrapped = ExecutionFailure(
        kind=FailureKind.UNAVAILABLE,
        status_code=0,
        message="connection failed",
        retryable=True,
    )
    wrapped.__cause__ = httpx.ProxyError("CONNECT refused")

    assert proxy_reachability_failure(wrapped, proxied=True) == "ProxyError"
    # Direct is this machine's own address. Benching "the proxy" for a refused
    # connection there would bench the operator's own network.
    assert proxy_reachability_failure(wrapped, proxied=False) is None


@pytest.mark.asyncio
async def test_a_reachability_failure_advances_even_when_no_chip_is_selected() -> None:
    """Non-configurable on purpose.

    Making it selectable would let an operator build a chain that cannot route
    around a dead entry -- the one thing a chain exists to do.
    """

    dead = httpx.ConnectTimeout("no route")
    first = _FakeProvider(fail_before_first=dead)
    second = _FakeProvider(chunks=("hello",))
    pool = _pool([first, second], ("a:1", "b:2"), on=frozenset())

    assert await _drain(pool) == ["hello"]


@pytest.mark.asyncio
async def test_a_dead_address_walks_the_ladder_and_stops_being_selected() -> None:
    """60s, then 5m, then 1h, and out of the rotation while it waits.

    The reachability bench is what turns three dead addresses from a
    per-request cost into a once-an-hour one.
    """

    dead = httpx.ConnectError("refused")
    first = _FakeProvider(fail_before_first=dead)
    second = _FakeProvider(chunks=("hello",))
    pool = _pool([first, second], ("a:1", "b:2"), on=frozenset())

    assert await _drain(pool) == ["hello"]
    assert PROXY_REACHABILITY_TIERS[0] == 60.0
    health = pool.proxy_health()
    assert health[0]["state"] == "unreachable"
    assert health[0]["cooldown_remaining"] == pytest.approx(60.0, abs=1.0)

    # Second request: the benched address is not offered at all.
    first.calls = 0
    assert await _drain(pool) == ["hello"]
    assert first.calls == 0


@pytest.mark.asyncio
async def test_the_reachability_bench_is_global_across_providers() -> None:
    """A dead address is dead for everybody.

    Discovering that once per provider is three connect timeouts instead of
    one, and it is the one part of this feature that is deliberately not
    scoped per provider.
    """

    dead = httpx.ConnectError("refused")
    pool_a = _pool(
        [_FakeProvider(fail_before_first=dead), _FakeProvider(chunks=("a",))],
        ("shared:1", "b:2"),
        on=frozenset(),
    )
    await _drain(pool_a)

    first = _FakeProvider(chunks=("never",))
    pool_b = ProxyRotatingProvider(
        ProviderConfig(api_key="k", base_url="http://x"),
        [first, _FakeProvider(chunks=("b",))],
        ProxyRotationState(
            2,
            "failover",
            labels=("shared:1", "c:3"),
            provider_id="other_provider",
        ),
        labels=("shared:1", "c:3"),
        plan=_plan(("shared:1", "c:3")),
        provider_id="other_provider",
    )

    assert await _drain(pool_b) == ["b"]
    assert first.calls == 0


@pytest.mark.asyncio
async def test_a_trigger_bench_is_scoped_to_the_provider_by_default() -> None:
    """The user's own answer: the allowance is metered by address alone.

    So an address exhausted on one key is exhausted for every key of that
    provider. ``credential`` stays selectable for a provider that meters per
    (address, account); the default moved, not the mechanism.
    """

    exhausted = _failure(FailureKind.QUOTA, 402, retry_after=300.0)
    pool = _pool(
        [
            _FakeProvider(fail_before_first=exhausted),
            _FakeProvider(chunks=("hello",)),
        ],
        ("a:1", "b:2"),
        on=frozenset({"quota"}),
        scope="provider",
    )
    await _drain(pool)

    assert pool._state.scope_key("sk-1…abcd") == "fake_provider"
    assert 0 not in pool._state.selectable_indexes("fake_provider")


@pytest.mark.asyncio
async def test_the_credential_scope_narrows_the_bench_to_one_key() -> None:
    """Two keys sharing an address keep separate benches under ``credential``."""

    state = ProxyRotationState(
        2,
        "failover",
        labels=("a:1", "b:2"),
        provider_id="fake_provider",
        scope="credential",
    )

    assert state.scope_key("sk-1…aaaa") == "fake_provider:sk-1…aaaa"
    assert state.scope_key("sk-2…bbbb") == "fake_provider:sk-2…bbbb"

    await state.report_failure(
        0,
        _failure(FailureKind.QUOTA, 402, retry_after=300.0),
        scope_key=state.scope_key("sk-1…aaaa"),
        triggers=frozenset({"quota"}),
    )

    assert 0 not in state.selectable_indexes(state.scope_key("sk-1…aaaa"))
    assert 0 in state.selectable_indexes(state.scope_key("sk-2…bbbb"))


@pytest.mark.asyncio
async def test_only_a_published_retry_after_shortens_an_address_bench() -> None:
    """A quota's ``retry_after_seconds`` is the operator's, not the host's.

    The classifier puts ``RATE_LIMIT_COOLDOWN_SECONDS`` there as its evidence
    flag that the body named a billing phrase. It is a cooldown for a
    *credential*; honouring it here would bench an exhausted address for the
    credential's minute when the operator asked for the address's five.
    """

    state = ProxyRotationState(
        2, "failover", labels=("a:1", "b:2"), provider_id="fake_provider"
    )
    await state.report_failure(
        0,
        _failure(FailureKind.QUOTA, 402, retry_after=60.0),
        scope_key="fake_provider",
        triggers=frozenset({"quota"}),
    )
    await state.report_failure(
        1,
        _failure(FailureKind.RATE_LIMIT, 429, retry_after=7.0),
        scope_key="fake_provider",
        triggers=frozenset({"rate_limit"}),
    )

    metrics = {row["index"]: row["cooldown_remaining"] for row in state.get_metrics()}
    assert metrics[0] == pytest.approx(300.0, abs=2.0)
    assert metrics[1] == pytest.approx(7.0, abs=2.0)


def test_the_proxy_tuning_never_escalates_to_the_whole_address() -> None:
    """Two credentials exhausting one address is not evidence it is broken.

    It is evidence the address is metered, which is the thing the operator
    configured. ``0`` is the engine's own documented "never escalate".
    """

    assert PROXY_TUNING.model_bench_escalation == 0
    assert PROXY_TUNING.rate_limit_mode == "fixed"
    assert PROXY_TUNING.rate_limit_seconds == 300.0
    assert PROXY_TUNING.lockout_tiers == PROXY_REACHABILITY_TIERS


def test_the_credential_engine_is_imported_and_never_edited() -> None:
    """The 6.34.0 precedent, asserted rather than remembered.

    ``core/credential_rotation.py`` already holds two tunings side by side and
    a third would have matched its convention. The tiebreaker was this
    release's acceptance condition: that file's ``git diff`` is empty.
    """

    from my_claude_code.core import credential_rotation

    assert "PROXY_TUNING" not in inspect.getsource(credential_rotation)


# ------------------------------------------------------------ observability


@pytest.mark.asyncio
async def test_a_try_records_the_address_it_went_through() -> None:
    """``LadderTry.proxy``, filled without one provider signature changing.

    The retry frame that records these rows sits below the pool and has never
    been told which address it is on, so the pool writes its choice into the
    same kind of per-request slot the credential pool already uses.
    """

    install_proxy_attribution()
    install_ladder_trace()
    exhausted = _failure(FailureKind.QUOTA, 402, retry_after=300.0)

    class _Recording(_FakeProvider):
        def stream_response(self, request, input_tokens=0, **kwargs):
            record_upstream_try(status=402 if self._fail_before_first else 200)
            return super().stream_response(request, input_tokens, **kwargs)

    pool = _pool(
        [
            _Recording(fail_before_first=exhausted),
            _Recording(chunks=("hello",)),
        ],
        ("a:1", DIRECT_PROXY_LABEL),
        on=frozenset({"quota"}),
    )
    assert await _drain(pool) == ["hello"]

    payload = ladder_payload(
        __import__("my_claude_code.core.upstream_ladder", fromlist=["current_ladder"])
        .current_ladder()
        .ladders[0]
    )
    assert [row.get("proxy") for row in payload["tries"]] == ["a:1", "direct"]
    # Denormalised onto the attempt row: the address that answered, because
    # that is the one the attempt's verdict belongs to.
    assert ladder_proxy_label(payload) == "direct"
    assert current_proxy() == "direct"


def test_direct_is_a_value_and_never_a_null() -> None:
    """NULL already means "not measured"; a rung the operator chose is not."""

    assert DIRECT_PROXY_LABEL == "direct"
    assert current_proxy() is None
