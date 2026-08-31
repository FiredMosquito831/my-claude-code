"""Replay the request that spent 51 of its 57 seconds asleep.

``req_cf646eed209a4c7f95064201d4a2a339`` (2026-08-31T14:10:35Z) asked for
``nvidia_nim/moonshotai/kimi-k3`` on a three-key round-robin pool. Fifteen
tries -- three keys times five -- met fourteen 429s and one 502 in 6.1 seconds
of actual upstream time, and 51.0 seconds of MCC's own exponential backoff.
Attempt 0 took 57.3 seconds; the whole request took 81.4. Attempt 7,
``nvidia/nemotron-3-ultra-550b-a55b``, was on the same three keys and was never
reached, because 6.18.0 had health-benched every credential. At 14:13:07Z that
same model answered 200 on key 0 with nothing changed but the clock.

The fixture beside this file is that request's stored ladder. What is replayed
here is not the ladder but the *shape*: a pool whose limited model refuses in
0.2s with no ``Retry-After``, exactly as NIM did, and a healthy sibling model
one chain slot away.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.application.execution import (
    ProviderExecutor,
    RouteExecutionPolicy,
)
from my_claude_code.application.ports import ProviderPort
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.credential_attribution import install_attribution
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
)
from my_claude_code.core.waiting_clock import install_waiting_clock
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.providers.support import rotation_state

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ladder_req_cf646ee.json"

KIMI = "moonshotai/kimi-k3"
NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"

#: What the fixture measured, as the numbers the assertions below are about.
MEASURED_TRIES = 15
MEASURED_SLEEP_MS = 50_967.6
MEASURED_ATTEMPT_MS = 57_284.3

#: NIM's own refusal time. Slowed by 1000x here, because the point of the test
#: is the ratio of upstream time to sleep, not either number on its own.
REFUSAL_SECONDS = 0.0002


@pytest.fixture(scope="module")
def incident() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _NimKey(BaseProvider):
    """One credential on a NIM-shaped gateway.

    ``limited`` models refuse in 0.2ms with no ``Retry-After``, which is the
    fixture's body, scaled. Everything else answers. Every call goes through
    the real ``ProviderRateLimiter``, so the retry ladder, the reactive block
    and the ladder recording are the shipped ones.
    """

    def __init__(self, limiter: ProviderRateLimiter, limited: frozenset[str]) -> None:
        super().__init__(ProviderConfig(api_key="nvapi-x", base_url="http://nim"))
        self._limiter = limiter
        self._limited = limited
        self.calls: list[str] = []

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
        return frozenset(self._limited)

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._limiter.remaining_wait()

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        return self._stream(request)

    async def _stream(self, request: MessagesRequest) -> AsyncIterator[str]:
        async def call() -> str:
            self.calls.append(request.model)
            await asyncio.sleep(REFUSAL_SECONDS)
            if request.model in self._limited:
                raise ExecutionFailure(
                    kind=FailureKind.RATE_LIMIT,
                    status_code=429,
                    message="Rate limited.",
                    retryable=True,
                    # NIM publishes none, which is the whole reason the
                    # operator's sixty seconds was applied instead.
                    retry_after_seconds=None,
                )
            return "event: message_stop\ndata: {}\n\n"

        yield await self._limiter.execute_with_retry(call)


def _pool(
    *,
    keys: int,
    limited: frozenset[str],
    routes_around_model: bool,
    retry_attempts: int = 5,
) -> RotatingProvider:
    config = ProviderConfig(
        api_key="nvapi-x",
        base_url="http://nim",
        api_keys=tuple(f"nvapi-{index}" for index in range(keys)),
        credential_rotation="round_robin",
        routes_around_model=routes_around_model,
    )
    providers = [
        _NimKey(
            ProviderRateLimiter(
                rate_limit=1000,
                rate_window=60,
                max_retries=retry_attempts - 1,
                backoff_base_seconds=2.0,
                backoff_max_seconds=10.0,
                backoff_jitter_seconds=0.0,
                routes_around_model=routes_around_model,
            ),
            limited,
        )
        for _ in range(keys)
    ]
    return RotatingProvider(
        config,
        providers,
        rotation_state(keys, "round_robin", model_bench_escalation=2),
        key_labels=tuple(f"nvap...{index}" for index in range(keys)),
        provider_id="nvidia_nim",
        routes_around_model=routes_around_model,
    )


def _keys(pool: RotatingProvider) -> tuple[_NimKey, ...]:
    """The doubles behind one pool, typed so their call counters are readable."""
    return tuple(
        provider for provider in pool._providers if isinstance(provider, _NimKey)
    )


def _routed(provider_id: str, model: str) -> RoutedMessagesRequest:
    return RoutedMessagesRequest(
        request=MessagesRequest(
            model=model,
            messages=[Message(role="user", content="hello")],
            stream=True,
        ),
        resolved=ResolvedModel(
            original_model="claude-sonnet-4",
            provider_id=provider_id,
            provider_model=model,
            provider_model_ref=f"{provider_id}/{model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


def _executor(
    providers: Mapping[str, ProviderPort], *, rate_limit_attempts: int = 3
) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
        policy=RouteExecutionPolicy(
            first_token_timeout=0.0,
            total_timeout=0.0,
            stall_timeout=0.0,
            reasoning_answer_timeout=0.0,
            rate_limit_attempts=rate_limit_attempts,
        ),
    )


async def _run(executor: ProviderExecutor, plan: RoutedMessagesPlan) -> list[str]:
    stream = executor.stream(
        plan,
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_cf646ee_replay",
    )
    return [chunk async for chunk in stream]


@pytest.fixture
def recording():
    install_attribution()
    install_waiting_clock()
    ladder = install_ladder_trace()
    try:
        yield ladder
    finally:
        _LADDER.set(None)


def test_the_fixture_still_describes_the_incident_this_test_is_about(
    incident,
) -> None:
    """The numbers in this file's docstring, read off the stored row."""
    assert len(incident["tries"]) == MEASURED_TRIES
    assert incident["attempt"]["duration_ms"] == pytest.approx(MEASURED_ATTEMPT_MS)
    slept = sum(entry.get("waited_ms", 0.0) for entry in incident["tries"])
    assert slept == pytest.approx(MEASURED_SLEEP_MS)
    # 89% of what the client waited was MCC asleep.
    assert slept / incident["attempt"]["duration_ms"] > 0.88


@pytest.mark.asyncio
async def test_the_client_is_served_in_under_two_seconds_wall(recording) -> None:
    """The assertion this PR exists for. The measured shape took 57.3 seconds.

    Three keys, a limited model, and a healthy sibling one chain slot away --
    exactly attempt 0 and attempt 7 of the incident. Real sleeps: nothing is
    patched out, because "it does not sleep" is the claim.
    """
    pool = _pool(keys=3, limited=frozenset({KIMI}), routes_around_model=True)
    executor = _executor({"nvidia_nim": pool})

    started = time.monotonic()
    chunks = await _run(
        executor,
        RoutedMessagesPlan(
            (_routed("nvidia_nim", KIMI), _routed("nvidia_nim", NEMOTRON))
        ),
    )
    elapsed = time.monotonic() - started

    assert chunks == ["event: message_stop\ndata: {}\n\n"]
    assert elapsed < 2.0, f"served in {elapsed:.2f}s; the incident took 57.3s"
    payload = ladder_payload(recording.slot())
    assert payload["summary"]["time_sleeping_ms"] == 0.0


@pytest.mark.asyncio
async def test_only_one_upstream_try_is_spent_on_the_limited_model(
    recording,
) -> None:
    """One key, one try. The incident spent three keys and fifteen."""
    pool = _pool(keys=3, limited=frozenset({KIMI}), routes_around_model=True)
    executor = _executor({"nvidia_nim": pool})

    await _run(
        executor,
        RoutedMessagesPlan(
            (_routed("nvidia_nim", KIMI), _routed("nvidia_nim", NEMOTRON))
        ),
    )

    kimi_calls = sum(provider.calls.count(KIMI) for provider in _keys(pool))
    assert kimi_calls == 1
    # And the healthy sibling answered on a key that was never rotated away.
    assert sum(provider.calls.count(NEMOTRON) for provider in _keys(pool)) == 1


@pytest.mark.asyncio
async def test_the_ladder_still_records_every_try(recording) -> None:
    """Routing around a failure must never make it invisible.

    One upstream try, and one credential decision saying the pair was benched
    while the key stayed healthy -- which is the fact the operator needs in
    order to understand why the request went somewhere else.
    """
    pool = _pool(keys=3, limited=frozenset({KIMI}), routes_around_model=True)
    executor = _executor({"nvidia_nim": pool})

    await _run(
        executor,
        RoutedMessagesPlan(
            (_routed("nvidia_nim", KIMI), _routed("nvidia_nim", NEMOTRON))
        ),
    )

    payload = ladder_payload(recording.slot())
    # Two rows: the 429 that routed, and the try that worked. A ladder ending
    # in a success used to read as though the last 429 were the outcome.
    assert payload["summary"]["tries"] == 2
    assert payload["summary"]["statuses_by_code"] == {"429": 1}
    assert payload["summary"]["time_sleeping_ms"] == 0.0
    decisions = payload["credentials"]
    assert len(decisions) == 1
    assert decisions[0]["class"] == "rate_limit"
    assert decisions[0]["model"] == KIMI
    assert decisions[0]["model_benched_for_s"] == pytest.approx(60.0, abs=1.0)
    # The credential-wide bench is a different fact, and it did not happen.
    assert decisions[0]["benched_for_s"] is None


@pytest.mark.asyncio
async def test_a_single_key_pool_on_a_single_model_route_still_gets_the_ladder(
    recording,
) -> None:
    """The case the whole design must not break.

    With one key and one configured model there is nowhere to route, so the
    retry ladder is the only thing left that can serve the request, and
    ``PROVIDER_RETRY_ATTEMPTS`` still buys its three tries. Only the frame
    that spends them moved: the executor knows whether a chain exists, and
    the limiter never did.
    """
    limiter = ProviderRateLimiter(
        rate_limit=1000, rate_window=60, routes_around_model=True
    )
    only_key = _NimKey(limiter, frozenset({KIMI}))
    executor = _executor({"nvidia_nim": only_key}, rate_limit_attempts=3)

    with pytest.raises(ExecutionFailure) as excinfo:
        await _run(executor, RoutedMessagesPlan((_routed("nvidia_nim", KIMI),)))

    assert excinfo.value.kind is FailureKind.RATE_LIMIT
    assert only_key.calls == [KIMI, KIMI, KIMI]
    payload = ladder_payload(recording.slot())
    assert payload["summary"]["tries"] == 3


@pytest.mark.asyncio
async def test_the_toggle_off_reproduces_the_measured_shape(recording) -> None:
    """The documented off-switch, on the same fixture shape.

    Five tries per key across three keys is the incident's fifteen, and the
    sleeps between them are the 51 seconds. They are patched out here rather
    than waited, because asserting the schedule does not require enduring it.
    """
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    pool = _pool(
        keys=3, limited=frozenset({KIMI}), routes_around_model=False, retry_attempts=5
    )
    executor = _executor({"nvidia_nim": pool}, rate_limit_attempts=1)

    with (
        patch(
            "my_claude_code.providers.rate_limit.asyncio.sleep",
            new=AsyncMock(side_effect=_record),
        ),
        # A patched sleep does not make time pass, so the reactive block it
        # installs would never expire and ``_wait_for_reactive_block`` would
        # spin. That block is 6.19.0's second application of the same
        # cooldown, measured by its own unit tests; what is counted here is
        # the backoff ladder.
        patch.object(ProviderRateLimiter, "extend_reactive_block"),
        pytest.raises(ExecutionFailure),
    ):
        await _run(executor, RoutedMessagesPlan((_routed("nvidia_nim", KIMI),)))

    kimi_calls = sum(provider.calls.count(KIMI) for provider in _keys(pool))
    assert kimi_calls == MEASURED_TRIES
    # 2 + 4 + 8 + 10 per key, three keys: the ladder the incident walked, with
    # the shipped 10s ceiling rather than the template's mistaken 60. The
    # tolerance covers the fifteen scaled refusals, which share the patch.
    assert sum(slept) == pytest.approx(72.0, abs=0.05)
    payload = ladder_payload(recording.slot())
    assert payload["summary"]["tries"] == MEASURED_TRIES
    assert payload["summary"]["statuses_by_code"] == {"429": MEASURED_TRIES}


def test_the_shipped_defaults_no_longer_buy_that_ladder() -> None:
    """What an operator who changes nothing now gets.

    The install that produced the incident had already lowered the ladder to
    two tries and a five second ceiling; the shipped defaults would have been
    worse. Both numbers move, and the 429 stops walking the ladder at all.
    """
    from my_claude_code.config.constants import (
        PROVIDER_RETRY_ATTEMPTS_DEFAULT,
        PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
        RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT,
    )

    assert PROVIDER_RETRY_ATTEMPTS_DEFAULT == 3
    assert PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT == 10.0
    assert RATE_LIMIT_ROUTES_AROUND_MODEL_DEFAULT is True
    # And the limiter's own fallback is untouched: the factory always passes
    # ``max_retries`` explicitly, so this is not the number that moved.
    from my_claude_code.providers.rate_limit import UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS

    assert UPSTREAM_TRANSIENT_TOTAL_ATTEMPTS == 5


def test_the_replay_is_unchanged_by_the_pool_that_replaced_it(incident) -> None:
    """PR 1's observability replay and this one describe the same request."""
    assert (
        replace(
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="nvidia_nim",
                provider_model=KIMI,
                provider_model_ref=f"nvidia_nim/{KIMI}",
                reasoning_preference=ReasoningPreference.CLIENT,
            )
        ).provider_model
        == KIMI
    )
    assert {entry["key_index"] for entry in incident["decisions"]} == {0, 1, 2}
