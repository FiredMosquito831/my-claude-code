"""Direct fallback only once EVERY proxy of the chain is unhealthy (7.79.2, PR-4).

The user's decision of 2026-10-06 23:03, verbatim: "direct fallback - we
should only fallback to real ip only once all proxies are unhealthy". The
frozen pool (``providers/runtime/proxy_rotating.py``) still decides when its
loop is over; the leg the factory builds at the fallback's index
(:class:`~my_claude_code.providers.runtime.direct_leg.DirectFallbackLeg`)
decides whether this computer's address may be used then. These tests drive
the frozen pool with that leg, built exactly as the factory builds it, and pin:

* every proxy unhealthy -- unreachable, refused, cooling down -- dials direct,
  exactly as before;
* a proxy still healthy when the loop ends (its live-failure bound spent)
  withholds it: nothing is dialled, the direct leaf is never even built, the
  log takes the ``direct`` it was announced under back, and the answer is a
  classified ``UNAVAILABLE`` the credential pool does not charge;
* the switch bound still ends the request before any of that, as it always
  did;
* under the ``single`` policy only the first entry -- all it ever dials -- is
  asked about;
* a Direct dial the system proxy carries says so in the log.
"""

from collections.abc import Callable

import httpx
import pytest

from my_claude_code.core.failures import ExecutionFailure, FailureKind, failure_kind
from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    DIRECT_PROXY_LABEL,
    install_proxy_attribution,
    record_proxy,
)
from my_claude_code.core.proxy_rotation import (
    PROXY_INTERCEPTION,
    PROXY_REACHABILITY,
    reset_proxy_health,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    amend_proxy_dial,
    current_ladder,
    install_ladder_trace,
    record_proxy_dial,
)
from my_claude_code.providers.base import (
    BaseProvider,
    ProviderConfig,
    ProxyChainPlan,
    ProxyLeg,
)
from my_claude_code.providers.credential_rotation import credential_failure_class
from my_claude_code.providers.runtime.direct_leg import (
    DirectFallbackLeg,
    DirectFallbackWithheld,
)
from my_claude_code.providers.runtime.proxy_rotating import (
    ProxyRotatingProvider,
    ProxyRotationState,
)
from tests.providers.test_credential_rotation import _FakeProvider, _request

PROVIDER = "gate_provider"
NAME = "Gate Co"
BASE_URL = "https://provider.test/v1"
DEAD = httpx.ConnectError("refused")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_proxy_health()
    _CURRENT.set(None)
    _LADDER.set(None)
    # No system proxy unless a test names one: the label tests set their own.
    monkeypatch.setattr("my_claude_code.config.system_proxy.getproxies", dict)
    yield
    reset_proxy_health()
    _CURRENT.set(None)
    _LADDER.set(None)


def _rate_limited() -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="free limit",
        retryable=True,
        retry_after_seconds=120.0,
    )


class _World:
    """The frozen pool, its rotation state and a gated direct leg, as the factory wires them."""

    def __init__(
        self,
        labels: tuple[str, ...],
        makers: dict[int, Callable[[], BaseProvider]],
        *,
        policy: str = "failover",
        scope: str = "provider",
        on: frozenset[str] = frozenset(),
        max_switches: int = 2,
        max_live_failures: int = 0,
        direct: Callable[[], BaseProvider] | None = None,
    ) -> None:
        self.plan = ProxyChainPlan(
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
            direct_fallback=True,
        )
        self.config = ProviderConfig(
            api_key="sk-gate-0000aaaa1111", base_url=BASE_URL, proxy_chain=self.plan
        )
        self.state = ProxyRotationState(
            len(labels), policy, labels=labels, provider_id=PROVIDER, scope=scope
        )
        self.built: list[int] = []
        self.direct_built = 0
        self._makers = makers
        self._direct = direct or (lambda: _FakeProvider(chunks=("direct",)))
        self.pool = ProxyRotatingProvider(
            self.config,
            self._build,
            self.state,
            labels=labels,
            plan=self.plan,
            provider_id=PROVIDER,
            max_live_failures=max_live_failures,
        )
        self.labels = labels

    def _direct_leaf(self) -> BaseProvider:
        self.direct_built += 1
        return self._direct()

    def _build(self, index: int) -> BaseProvider:
        self.built.append(index)
        if index >= len(self.labels):
            return DirectFallbackLeg(
                self.config,
                build=self._direct_leaf,
                state=lambda: self.state,
                labels=self.labels,
                scope=self.plan.scope,
                provider_id=PROVIDER,
                name=NAME,
            )
        maker = self._makers.get(index)
        return maker() if maker is not None else _FakeProvider(chunks=("proxied",))

    async def drain(self) -> list[str]:
        return [chunk async for chunk in self.pool.stream_response(_request())]


def _dead() -> _FakeProvider:
    return _FakeProvider(fail_before_first=DEAD)


# ---------------------------------------------------- every proxy unhealthy


@pytest.mark.asyncio
async def test_every_proxy_unreachable_goes_direct_as_before() -> None:
    world = _World(("a:1", "b:2", "c:3"), {0: _dead, 1: _dead, 2: _dead})

    assert await world.drain() == ["direct"]
    assert world.built == [0, 1, 2, 3]
    assert world.direct_built == 1


@pytest.mark.asyncio
async def test_every_proxy_known_unhealthy_before_the_request_goes_direct() -> None:
    """Nothing is dialled through a known-bad address; Direct is the answer."""

    for label in ("a:1", "b:2"):
        PROXY_REACHABILITY.note_failure(label, "refused")
    PROXY_INTERCEPTION.mark("c:3", "breaks certificate validation")
    world = _World(("a:1", "b:2", "c:3"), {})

    assert await world.drain() == ["direct"]
    assert world.built == [3]


@pytest.mark.asyncio
async def test_proxies_cooling_down_after_a_refusal_count_as_unhealthy() -> None:
    """A trigger bench is the rotation's own "not now": it counts."""

    world = _World(
        ("a:1", "b:2"),
        {
            0: lambda: _FakeProvider(fail_before_first=_rate_limited()),
            1: lambda: _FakeProvider(fail_before_first=_rate_limited()),
        },
        on=frozenset({"rate_limit"}),
        max_switches=2,
    )

    assert await world.drain() == ["direct"]
    assert world.direct_built == 1


# ------------------------------------------------ a healthy proxy still left


@pytest.mark.asyncio
async def test_a_healthy_proxy_left_withholds_direct() -> None:
    """The live-failure bound ends the loop; two proxies were never tried."""

    install_proxy_attribution(on_dial=record_proxy_dial, on_amend=amend_proxy_dial)
    install_ladder_trace()
    world = _World(("a:1", "b:2", "c:3"), {0: _dead}, max_live_failures=1)

    with pytest.raises(DirectFallbackWithheld) as caught:
        await world.drain()

    failure = caught.value
    assert failure.kind is FailureKind.UNAVAILABLE
    assert failure.status_code == 503
    assert failure.retryable is False
    for phrase in (
        "Not sent from this computer's own address",
        f"2 of 3 proxies in {NAME}'s chain are not unhealthy (b:2, c:3)",
        "Direct fallback uses this computer's address only once every proxy",
        f"Proxying page -> {NAME} -> Direct fallback",
    ):
        assert phrase in failure.message, failure.message
    # Nothing was dialled from here: the direct leaf was never even built.
    assert world.built == [0, 3]
    assert world.direct_built == 0
    # The log says where the request really went: the dead exit, not Direct.
    slot = _CURRENT.get()
    assert slot is not None and slot.label == "a:1"
    ladder = current_ladder()
    assert ladder is not None
    assert [dial.proxy for dial in ladder.slot().dials] == ["a:1"]


@pytest.mark.asyncio
async def test_the_withheld_answer_does_not_charge_the_key() -> None:
    """UNAVAILABLE: another key may try, and this one keeps its health."""

    world = _World(("a:1", "b:2"), {0: _dead}, max_live_failures=1)

    with pytest.raises(DirectFallbackWithheld) as caught:
        await world.drain()

    assert failure_kind(caught.value) is FailureKind.UNAVAILABLE
    assert credential_failure_class(caught.value) is None


@pytest.mark.asyncio
async def test_one_unreachable_among_healthy_never_goes_direct() -> None:
    PROXY_REACHABILITY.note_failure("a:1", "refused")
    world = _World(("a:1", "b:2", "c:3"), {})

    assert await world.drain() == ["proxied"]
    assert world.built == [1]
    assert world.direct_built == 0


@pytest.mark.asyncio
async def test_the_switch_bound_ends_the_request_before_any_fallback() -> None:
    """Unchanged frozen behaviour, re-asserted with the gated leg in place."""

    refusal = _rate_limited()
    world = _World(
        ("a:1", "b:2", "c:3"),
        {
            index: (lambda: _FakeProvider(fail_before_first=refusal))
            for index in range(3)
        },
        on=frozenset({"rate_limit"}),
        max_switches=1,
    )

    with pytest.raises(ExecutionFailure) as caught:
        await world.drain()

    assert caught.value is refusal
    assert world.built == [0, 1]
    assert world.direct_built == 0


# ------------------------------------------------------------- the policies


@pytest.mark.asyncio
async def test_under_single_only_the_first_entry_is_asked_about() -> None:
    """``single`` only ever dials entry 0, so entry 0 unhealthy is "all"."""

    world = _World(("a:1", "b:2"), {0: _dead}, policy="single")

    assert await world.drain() == ["direct"]
    assert world.built == [0, 2]


@pytest.mark.asyncio
async def test_under_credential_scope_the_key_s_own_bench_is_read() -> None:
    world = _World(
        ("a:1", "b:2"),
        {
            0: lambda: _FakeProvider(fail_before_first=_rate_limited()),
            1: lambda: _FakeProvider(fail_before_first=_rate_limited()),
        },
        scope="credential",
        on=frozenset({"rate_limit"}),
    )

    assert await world.drain() == ["direct"]
    assert world.direct_built == 1


# ------------------------------------------------------------ the log label


@pytest.mark.asyncio
async def test_direct_through_the_system_proxy_is_labelled_so(monkeypatch) -> None:
    monkeypatch.setattr(
        "my_claude_code.config.system_proxy.getproxies",
        lambda: {"https": "http://user:pw@corp-proxy.test:3128"},
    )
    install_proxy_attribution(on_dial=record_proxy_dial, on_amend=amend_proxy_dial)
    install_ladder_trace()
    seen: list[str | None] = []

    class _Recording(_FakeProvider):
        def stream_response(self, request, input_tokens=0, **kwargs):
            slot = _CURRENT.get()
            seen.append(None if slot is None else slot.label)
            return super().stream_response(request, input_tokens, **kwargs)

    world = _World(
        ("a:1", "b:2"), {0: _dead, 1: _dead}, direct=lambda: _Recording(chunks=("d",))
    )

    assert await world.drain() == ["d"]
    label = "direct via system proxy corp-proxy.test:3128"
    assert seen == [label]
    ladder = current_ladder()
    assert ladder is not None
    assert [dial.proxy for dial in ladder.slot().dials] == ["a:1", "b:2", label]


@pytest.mark.asyncio
async def test_no_system_proxy_keeps_the_plain_direct_label() -> None:
    install_proxy_attribution()
    world = _World(("a:1", "b:2"), {0: _dead, 1: _dead})

    assert await world.drain() == ["direct"]
    slot = _CURRENT.get()
    assert slot is not None and slot.label == DIRECT_PROXY_LABEL


def test_record_proxy_outside_a_request_is_still_a_no_op() -> None:
    record_proxy("a:1")
    assert _CURRENT.get() is None
