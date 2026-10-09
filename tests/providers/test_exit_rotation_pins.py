"""Pin-it-first: what a chain does today, before "keep trying exits" (7.81.0).

``specs/PR-EXIT-ROTATION-AND-REPEAT-MODELS-SPEC.md`` A.6 asks for these to be
written before the change and to pass on the release before it (``fork/main``
``57a4620e``, 7.80.0) -- they pass there unchanged, and here. Each pins one fact
the design of the ticked chain rests on:

1. a chain without the box answers ``throttle_remaining`` from the legs that are
   *open* -- one reactive block on the only opened leg makes the whole chain
   look throttled while legs nobody has dialled are free (A.1 claim 2);
2. a chain's leg answering a 429 through an ``httpx`` status error puts that
   *leg's own limiter* into a reactive block -- the per-exit block the ticked
   chain leaves as it is;
3. an ``UNAVAILABLE`` from a chain makes a credential pool rotate without
   charging the key -- the reason a ticked chain ends its exits with one.
"""

import httpx
import pytest

from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.core.proxy_rotation import reset_proxy_health
from my_claude_code.providers.base import ProviderConfig, ProxyChainPlan, ProxyLeg
from my_claude_code.providers.credential_rotation import (
    credential_failure_class,
    error_justifies_rotation,
)
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.proxy_rotating import (
    ProxyRotatingProvider,
    ProxyRotationState,
)
from tests.providers.test_credential_rotation import _FakeProvider


@pytest.fixture(autouse=True)
def _clean_ledgers():
    reset_proxy_health()
    yield
    reset_proxy_health()


class _ThrottledLeg(_FakeProvider):
    def __init__(self, wait: float) -> None:
        super().__init__()
        self.wait = wait

    def throttle_remaining(self, model: str | None = None) -> float:
        return self.wait


def _chain(legs: list[_FakeProvider]) -> ProxyRotatingProvider:
    labels = tuple(f"exit{index}:1" for index in range(len(legs)))
    plan = ProxyChainPlan(
        legs=tuple(ProxyLeg(url=f"http://{label}", label=label) for label in labels),
        policy="failover",
        on=frozenset({"rate_limit"}),
    )
    built: dict[int, _FakeProvider] = {}

    def build(index: int) -> _FakeProvider:
        built[index] = legs[index] if index < len(legs) else _FakeProvider()
        return built[index]

    state = ProxyRotationState(
        len(legs), plan.policy, labels=labels, provider_id="pin_provider"
    )
    return ProxyRotatingProvider(
        ProviderConfig(api_key="k", base_url="http://x", proxy_chain=plan),
        build,
        state,
        labels=labels,
        plan=plan,
        provider_id="pin_provider",
    )


def test_one_throttled_open_leg_reads_as_the_whole_chain_throttled() -> None:
    """A.1 claim 2: only the opened leg is asked; the unopened one is ignored."""

    pool = _chain([_ThrottledLeg(30.0), _ThrottledLeg(0.0)])
    pool._pool.get(0)

    assert pool._pool.open_indexes == (0,)
    assert pool.throttle_remaining() == 30.0


def test_a_429_status_error_blocks_the_leg_s_own_limiter() -> None:
    """The per-exit reactive block a 429 leaves on the leg that met it."""

    limiter = ProviderRateLimiter(rate_limit=0, rate_window=60)
    request = httpx.Request("POST", "https://host.test/v1/chat/completions")
    error = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(
            429,
            request=request,
            headers={"retry-after": "7"},
            json={"type": "error", "error": {"type": "FreeUsageLimitError"}},
        ),
    )

    failure = classify_provider_failure(
        error,
        provider_name="PIN",
        read_timeout_s=None,
        request_id=None,
        mark_rate_limited=limiter.extend_reactive_block,
    )

    assert failure.kind is FailureKind.RATE_LIMIT
    assert limiter.remaining_wait() > 0


def test_unavailable_rotates_a_key_pool_without_charging_the_key() -> None:
    failure = ExecutionFailure(
        kind=FailureKind.UNAVAILABLE,
        status_code=502,
        message="every exit refused",
        retryable=False,
    )

    assert credential_failure_class(failure) is None
    assert error_justifies_rotation(failure) is True


def test_direct_is_never_an_exit_label_a_pool_benches() -> None:
    """The label the Direct leg records, and the one the memory keys on."""

    assert DIRECT_PROXY_LABEL == "direct"
