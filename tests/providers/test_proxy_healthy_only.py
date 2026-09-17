"""7.19.0: only healthy proxies carry traffic, and Direct is the last rung.

Four changes, each of which is a way a chain used to waste an operator's
request, asserted here from the outside:

1. A chain is as long as the operator wants, and a leg costs nothing until
   something goes out through it.
2. A bench expiring makes an address **due for a re-check**, never usable. Only
   a check that passes puts it back.
3. An address already known unhealthy is skipped for free -- it spends no
   switch and no live-failure budget.
4. When nothing healthy is left, the request goes out with no proxy at all,
   once, and the request log says ``Direct``.

The four "may not do" rules in ``providers/runtime/proxy_rotating``'s own
docstring are unchanged and are re-asserted in
``tests/providers/test_proxy_rotation.py``; the one this file re-states is the
first, because the direct rung is a new way out of the loop and it must not
become a new way to wrap an error.
"""

from collections.abc import Callable

import httpx
import pytest

from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import (
    DIRECT_PROXY_LABEL,
    current_proxy,
    install_proxy_attribution,
    record_proxy,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
    reset_proxy_health,
)
from my_claude_code.providers.base import (
    BaseProvider,
    ProviderConfig,
    ProxyChainPlan,
    ProxyLeg,
)
from my_claude_code.providers.runtime.proxy_rotating import (
    ProxyRotatingProvider,
    ProxyRotationState,
)
from tests.providers.test_credential_rotation import _FakeProvider, _request


@pytest.fixture(autouse=True)
def _clean_ledgers():
    reset_proxy_health()
    record_proxy(None)
    yield
    reset_proxy_health()
    record_proxy(None)


def _plan(
    labels: tuple[str, ...],
    *,
    policy: str = "failover",
    on: frozenset[str] = frozenset({"quota", "rate_limit", "timeout"}),
    max_switches: int = 2,
    direct_fallback: bool = True,
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
        scope="provider",
        max_switches=max_switches,
        direct_fallback=direct_fallback,
    )


class _Built:
    """A leg factory that records what it was asked to build, and when.

    The whole of the laziness claim is "how many of these exist", so the test
    counts them rather than inspecting a list that no longer exists.
    """

    def __init__(
        self, makers: dict[int, Callable[[], BaseProvider]] | None = None
    ) -> None:
        self.built: list[int] = []
        self._makers: dict[int, Callable[[], BaseProvider]] = dict(makers or {})
        self.made: dict[int, BaseProvider] = {}

    def __call__(self, index: int) -> BaseProvider:
        self.built.append(index)
        maker = self._makers.get(index)
        provider = maker() if maker is not None else _FakeProvider(chunks=("ok",))
        self.made[index] = provider
        return provider


def _pool(
    labels: tuple[str, ...],
    factory,
    *,
    max_open_legs: int = 0,
    max_live_failures: int = 0,
    **plan_kwargs,
) -> ProxyRotatingProvider:
    plan = _plan(labels, **plan_kwargs)
    state = ProxyRotationState(
        len(labels),
        plan.policy,
        labels=labels,
        provider_id="fake_provider",
        scope=plan.scope,
    )
    return ProxyRotatingProvider(
        ProviderConfig(api_key="k", base_url="http://x", proxy_chain=plan),
        factory,
        state,
        labels=labels,
        plan=plan,
        provider_id="fake_provider",
        max_open_legs=max_open_legs,
        max_live_failures=max_live_failures,
    )


async def _drain(provider) -> list[str]:
    return [chunk async for chunk in provider.stream_response(_request())]


def _failure(kind: FailureKind, status: int) -> ExecutionFailure:
    return ExecutionFailure(
        kind=kind, status_code=status, message=f"upstream {status}", retryable=True
    )


# ------------------------------------------------------ 1. an unlimited chain


def _long_labels(count: int) -> tuple[str, ...]:
    return tuple(f"198.51.100.{index // 250}:{9000 + index}" for index in range(count))


@pytest.mark.asyncio
async def test_a_three_hundred_entry_chain_builds_no_client_until_a_request() -> None:
    """The cap went because the cost went, and this is the cost being gone.

    Up to 7.18 the factory built one leaf -- one ``httpx.AsyncClient``, one
    rate limiter, one recovery ladder -- per rung per credential at
    construction, which is what twelve was a bound on. Nothing is built here
    until a request picks a rung, and then exactly one thing is.
    """

    factory = _Built()
    pool = _pool(_long_labels(300), factory)

    assert factory.built == []
    assert pool.open_leg_count() == 0

    assert await _drain(pool) == ["ok"]
    assert len(factory.built) == 1
    assert pool.open_leg_count() == 1


@pytest.mark.asyncio
async def test_idle_legs_are_closed_once_the_bound_is_full() -> None:
    """An LRU over the legs, so a long chain is a long list and not a socket
    farm. The leg that has been idle longest goes; the one in use never does,
    which is asserted by the fact that the leg serving the request that
    triggered the sweep is still open when it finishes.
    """

    dead = httpx.ConnectError("refused")
    factory = _Built(
        {index: (lambda: _FakeProvider(fail_before_first=dead)) for index in range(4)}
    )
    pool = _pool(
        _long_labels(6), factory, max_open_legs=2, max_live_failures=0, on=frozenset()
    )

    # Four dead rungs then a live one: five legs are built, two stay open.
    assert await _drain(pool) == ["ok"]
    assert len(factory.built) == 5
    assert pool.open_leg_count() == 2
    closed = [
        index
        for index, provider in factory.made.items()
        if index not in pool._pool.open_indexes
    ]
    assert len(closed) == 3


@pytest.mark.asyncio
async def test_cleanup_closes_every_open_leg() -> None:
    """One ``cleanup`` on the pool above, every client this chain ever opened."""

    class _Closing(_FakeProvider):
        def __init__(self) -> None:
            super().__init__(chunks=("ok",))
            self.closed = False

        async def cleanup(self) -> None:
            self.closed = True

    factory = _Built(dict.fromkeys(range(4), _Closing))
    pool = _pool(_long_labels(4), factory, policy="round_robin")

    for _ in range(3):
        await _drain(pool)
    opened = [
        provider for provider in factory.made.values() if isinstance(provider, _Closing)
    ]
    assert len(opened) >= 2

    await pool.cleanup()

    assert all(provider.closed for provider in opened)
    assert pool.open_leg_count() == 0


# --------------------------------------------- 2. a bench is not a countdown


@pytest.mark.asyncio
async def test_a_bench_expiring_does_not_put_an_address_back() -> None:
    """The defect this release exists to fix.

    Up to 7.18 an address that failed was selectable again sixty seconds later,
    with nothing having measured it -- so a free proxy that died stayed in the
    rotation forever, costing a connect timeout inside a real request once a
    minute. Time passing is not evidence.
    """

    PROXY_REACHABILITY.note_failure("a:1", "ConnectError")
    # Every tier has run out, several times over.
    PROXY_REACHABILITY.restore("a:1", 1, 0.0, "ConnectError")

    assert PROXY_REACHABILITY.remaining("a:1") == 0.0
    assert PROXY_REACHABILITY.due_for_reprobe("a:1") is True
    assert PROXY_REACHABILITY.is_unhealthy("a:1") is True

    factory = _Built()
    pool = _pool(("a:1", "b:2"), factory)

    assert await _drain(pool) == ["ok"]
    # Rung 0 was skipped entirely: never built, never dialled.
    assert factory.built == [1]


@pytest.mark.asyncio
async def test_a_passing_check_is_what_puts_an_address_back() -> None:
    """``note_success`` is the only door, and the checker is what knocks."""

    PROXY_REACHABILITY.note_failure("a:1", "ConnectError")
    assert PROXY_REACHABILITY.is_unhealthy("a:1") is True

    PROXY_REACHABILITY.note_success("a:1")

    assert PROXY_REACHABILITY.is_unhealthy("a:1") is False
    factory = _Built()
    pool = _pool(("a:1", "b:2"), factory)
    assert await _drain(pool) == ["ok"]
    assert factory.built == [0]


# ------------------------------- 3. an unhealthy address costs nothing to skip


@pytest.mark.asyncio
async def test_a_known_unhealthy_address_spends_no_switch() -> None:
    """Two hundred dead addresses and one live one is one attempt.

    The switch bound is two. If skipping cost a switch, the third rung would
    never be reached and this chain would answer nothing.
    """

    labels = _long_labels(200)
    for label in labels[:199]:
        PROXY_REACHABILITY.note_failure(label, "ConnectError")

    factory = _Built()
    pool = _pool(labels, factory, max_switches=2, max_live_failures=5)

    assert await _drain(pool) == ["ok"]
    assert factory.built == [199]


# ----------------------------------- 4. bounded live failures, then Direct


@pytest.mark.asyncio
async def test_live_failures_are_bounded_and_then_the_request_goes_direct() -> None:
    """Five dead addresses, then this machine's own, once.

    A *live* failure is one that cost a real connect attempt. The bound exists
    because those are the expensive ones: an unbounded chain of three hundred
    dead addresses is three hundred connect timeouts inside one attempt, and
    the executor's deadlines have not moved to make room.
    """

    dead = httpx.ConnectError("refused")
    labels = _long_labels(20)
    factory = _Built(
        {index: (lambda: _FakeProvider(fail_before_first=dead)) for index in range(20)}
    )
    factory._makers[20] = lambda: _FakeProvider(chunks=("direct",))
    pool = _pool(labels, factory, max_live_failures=5, on=frozenset())

    assert await _drain(pool) == ["direct"]
    # Five proxies dialled, then the direct leg -- index 20, one past the
    # chain, which is what makes it un-pickable by the rotation engine.
    assert factory.built == [0, 1, 2, 3, 4, 20]


@pytest.mark.asyncio
async def test_direct_is_attempted_at_most_once() -> None:
    """A failing direct leg is the end of the chain, not a second lap."""

    dead = httpx.ConnectError("refused")
    boom = _failure(FailureKind.UPSTREAM, 502)
    factory = _Built(
        {
            0: (lambda: _FakeProvider(fail_before_first=dead)),
            1: (lambda: _FakeProvider(fail_before_first=dead)),
            2: (lambda: _FakeProvider(fail_before_first=boom)),
        }
    )
    pool = _pool(("a:1", "b:2"), factory, on=frozenset())

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is boom
    assert factory.built == [0, 1, 2]


@pytest.mark.asyncio
async def test_direct_is_not_duplicated_when_the_chain_already_has_one() -> None:
    """An operator who wrote Direct into the chain gets one Direct, not two.

    That rung is an ordinary rung: the loop has already had its turn at it, and
    a second attempt would be the same request going out the same way twice.
    """

    dead = httpx.ConnectError("refused")
    factory = _Built(
        {
            0: (lambda: _FakeProvider(fail_before_first=dead)),
            1: (lambda: _FakeProvider(fail_before_first=dead)),
        }
    )
    pool = _pool(("a:1", DIRECT_PROXY_LABEL), factory, on=frozenset())

    with pytest.raises(httpx.ConnectError):
        await _drain(pool)

    # Two rungs, two legs. Index 2 -- the synthetic direct one -- is never
    # asked for.
    assert factory.built == [0, 1]


@pytest.mark.asyncio
async def test_the_direct_try_is_labelled_direct_in_the_request_log() -> None:
    """The per-try ``proxy`` label has to say which address carried it."""

    dead = httpx.ConnectError("refused")
    # The attribution slot is per-request and a provider driven directly has
    # none, so the request log's own entry point is what installs it.
    install_proxy_attribution()
    seen: list[str | None] = []

    class _Recording(_FakeProvider):
        def stream_response(self, request, input_tokens=0, **kwargs):
            seen.append(current_proxy())
            return super().stream_response(request, input_tokens, **kwargs)

    factory = _Built(
        {
            0: (lambda: _FakeProvider(fail_before_first=dead)),
            1: (lambda: _FakeProvider(fail_before_first=dead)),
            2: _Recording,
        }
    )
    pool = _pool(("a:1", "b:2"), factory, on=frozenset())

    assert await _drain(pool) == ["chunk"]
    assert seen == [DIRECT_PROXY_LABEL]


@pytest.mark.asyncio
async def test_an_intercepted_address_is_never_tried_on_the_direct_path() -> None:
    """The prohibition holds in every branch, including the new one.

    A bench is a preference and an interception is not: "everything else is
    benched" was never a reason to carry a credential through a tunnel somebody
    is reading, and neither is "we are about to give up".
    """

    PROXY_INTERCEPTION.mark("a:1", "breaks certificate validation")
    PROXY_INTERCEPTION.mark("b:2", "breaks certificate validation")
    factory = _Built({2: (lambda: _FakeProvider(chunks=("direct",)))})
    pool = _pool(("a:1", "b:2"), factory)

    assert await _drain(pool) == ["direct"]
    assert factory.built == [2]


@pytest.mark.asyncio
async def test_with_the_direct_fallback_off_the_pre_719_relax_is_kept() -> None:
    """Turning it off must restore exactly what a chain did before.

    The relax branch -- dispatch into a bench rather than answer nothing -- is
    the right answer when there is no honest alternative, and with the direct
    fallback off there is none. So it is still there, unchanged, and it still
    subtracts the refused set.
    """

    PROXY_REACHABILITY.note_failure("a:1", "ConnectError")
    PROXY_REACHABILITY.note_failure("b:2", "ConnectError")
    factory = _Built({0: (lambda: _FakeProvider(chunks=("benched",)))})
    pool = _pool(("a:1", "b:2"), factory, direct_fallback=False)

    assert await _drain(pool) == ["benched"]
    # The benched rung was dispatched into, and nothing went out Direct.
    assert factory.built == [0]


@pytest.mark.asyncio
async def test_exhaustion_still_re_raises_the_same_exception_object() -> None:
    """Rule 1 of this module's contract, with the direct rung in the loop.

    Wrapping would make ``credential_failure_class`` answer ``None`` and the
    credential pool would quietly stop charging health.
    """

    error = _failure(FailureKind.RATE_LIMIT, 429)
    factory = _Built(
        {index: (lambda: _FakeProvider(fail_before_first=error)) for index in range(3)}
    )
    pool = _pool(("a:1", "b:2"), factory, on=frozenset({"rate_limit"}))

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(pool)

    assert caught.value is error
