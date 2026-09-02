"""Replay the request an empty wallet ended.

``req_df6a8ed49c6a46bb81765ba8039b8703`` (2026-09-02T11:52Z) asked for
``commandcode/z-ai/glm-5.3-flash``. The upstream answered HTTP 400 with::

    {"message": "You have insufficient credits to make this request. Please
     purchase more credits to continue using the service.",
     "type": "invalid_request_error", "code": "BAD_REQUEST"}

which classified as ``FailureKind.INVALID_REQUEST``. ``FALLBACK_SKIP_KINDS``
defaults to ``invalid_request``, so the six remaining entries on the chain were
recorded as "not tried: a invalid_request failure ends the route" and the
caller received a 400. The request was fine. The *account* was out of credits,
and every one of those six entries could have answered.

What is replayed here is that body, byte for byte, through the shipped
classifier and the shipped executor.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping

import httpx
import openai
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
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.providers.support import rotation_state

FLASH = "z-ai/glm-5.3-flash"
SIBLING = "z-ai/glm-5.3"

#: The stored response body, verbatim.
RECORDED_BODY = {
    "message": (
        "You have insufficient credits to make this request. Please purchase "
        "more credits to continue using the service."
    ),
    "type": "invalid_request_error",
    "code": "BAD_REQUEST",
}

#: The operator's existing RATE_LIMIT_COOLDOWN_SECONDS. No new number.
COOLDOWN = 45.0


def recorded_error() -> openai.BadRequestError:
    """The upstream rejection as the OpenAI SDK raises it."""
    request = httpx.Request("POST", "https://commandcode.test/v1/chat/completions")
    response = httpx.Response(400, request=request, json=RECORDED_BODY)
    return openai.BadRequestError(
        RECORDED_BODY["message"], response=response, body=RECORDED_BODY
    )


def _classified() -> ExecutionFailure:
    """What the shipped classifier makes of that body."""
    return classify_provider_failure(
        recorded_error(),
        provider_name="COMMANDCODE",
        read_timeout_s=None,
        request_id=None,
        mark_rate_limited=lambda _seconds: None,
        cooldown_seconds=COOLDOWN,
    )


class _CommandCodeKey(BaseProvider):
    """One credential on a Command Code-shaped gateway.

    A ``broke`` key answers the recorded body; the rest answer normally.
    """

    def __init__(self, *, broke: bool, broke_models: frozenset[str] | None) -> None:
        super().__init__(ProviderConfig(api_key="cc-x", base_url="http://cc"))
        self._broke = broke
        self._broke_models = broke_models
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
        return frozenset({FLASH, SIBLING})

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

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
        self.calls.append(request.model)
        await asyncio.sleep(0)
        refuses = self._broke and (
            self._broke_models is None or request.model in self._broke_models
        )
        if refuses:
            raise _classified()
        yield "event: message_stop\ndata: {}\n\n"


def _pool(
    *, broke: tuple[bool, ...], broke_models: frozenset[str] | None = None
) -> RotatingProvider:
    config = ProviderConfig(
        api_key="cc-x",
        base_url="http://cc",
        api_keys=tuple(f"cc-{index}" for index in range(len(broke))),
        credential_rotation="round_robin",
    )
    providers = [
        _CommandCodeKey(broke=is_broke, broke_models=broke_models) for is_broke in broke
    ]
    return RotatingProvider(
        config,
        providers,
        rotation_state(len(broke), "round_robin", rate_limit_seconds=COOLDOWN),
        key_labels=tuple(f"cc...{index}" for index in range(len(broke))),
        provider_id="commandcode",
    )


def _keys(pool: RotatingProvider) -> tuple[_CommandCodeKey, ...]:
    return tuple(
        provider
        for provider in pool._providers
        if isinstance(provider, _CommandCodeKey)
    )


def _routed(model: str) -> RoutedMessagesRequest:
    return RoutedMessagesRequest(
        request=MessagesRequest(
            model=model,
            messages=[Message(role="user", content="hello")],
            stream=True,
        ),
        resolved=ResolvedModel(
            original_model="claude-sonnet-4",
            provider_id="commandcode",
            provider_model=model,
            provider_model_ref=f"commandcode/{model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


def _executor(providers: Mapping[str, ProviderPort]) -> ProviderExecutor:
    return ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _messages, _system, _tools: 17,
        policy=RouteExecutionPolicy(
            first_token_timeout=0.0,
            total_timeout=0.0,
            stall_timeout=0.0,
            reasoning_answer_timeout=0.0,
        ),
    )


async def _run(executor: ProviderExecutor, plan: RoutedMessagesPlan) -> list[str]:
    stream = executor.stream(
        plan,
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_df6a_replay",
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


def test_the_recorded_body_no_longer_reads_as_a_malformed_request() -> None:
    """The stored 400, through the shipped classifier."""
    failure = _classified()

    assert failure.kind is FailureKind.QUOTA
    assert failure.kind is not FailureKind.INVALID_REQUEST
    # An exact phrase, so the pool may take the key out for the operator's
    # cooldown -- for exactly that, and no new number.
    assert failure.retry_after_seconds == COOLDOWN


def test_quota_never_ends_the_route() -> None:
    """Not merely absent from the documented default: absent from the policy."""
    policy = RouteExecutionPolicy()

    assert FailureKind.QUOTA not in policy.skip_kinds
    assert FailureKind.INVALID_REQUEST in policy.skip_kinds


@pytest.mark.asyncio
async def test_the_recorded_request_df6a_now_reaches_the_next_model(
    recording,
) -> None:
    """The assertion this release exists for.

    One key with no credits for the first model on the chain; the second model
    answers. In 6.33.1 the caller got the 400 and the second entry's row read
    "not tried: a invalid_request failure ends the route".
    """
    pool = _pool(broke=(True,), broke_models=frozenset({FLASH}))
    executor = _executor({"commandcode": pool})

    chunks = await _run(
        executor, RoutedMessagesPlan((_routed(FLASH), _routed(SIBLING)))
    )

    assert chunks == ["event: message_stop\ndata: {}\n\n"]
    assert _keys(pool)[0].calls == [FLASH, SIBLING]


@pytest.mark.asyncio
async def test_quota_rotates_to_a_funded_key_before_changing_model(
    recording,
) -> None:
    """Key 1 has credits, so the request never leaves the model it asked for."""
    pool = _pool(broke=(True, False))
    executor = _executor({"commandcode": pool})

    chunks = await _run(executor, RoutedMessagesPlan((_routed(FLASH),)))

    assert chunks == ["event: message_stop\ndata: {}\n\n"]
    assert _keys(pool)[0].calls == [FLASH]
    assert _keys(pool)[1].calls == [FLASH]
    # And the empty key is out of the pool for the operator's cooldown.
    health = pool.key_health()
    assert health[0]["state"] == "COOLDOWN"
    assert health[0]["cooldown_reason"] == "credits exhausted"
    assert health[1]["state"] == "HEALTHY"


@pytest.mark.asyncio
async def test_the_ladder_records_the_credits_decision_against_the_key(
    recording,
) -> None:
    """A sentence naming the key and the fix, not a status and a wait.

    (The per-try rows come from the provider rate limiter, which these
    doubles deliberately do not run; ``tests/core/test_upstream_ladder.py``
    owns the rendered root-cause sentence.)
    """
    pool = _pool(broke=(True, False))
    executor = _executor({"commandcode": pool})

    await _run(executor, RoutedMessagesPlan((_routed(FLASH),)))

    payload = ladder_payload(recording.slot())
    charged = [entry for entry in payload["credentials"] if entry["class"] == "quota"]
    assert len(charged) == 1
    assert charged[0]["reason"].startswith("credits exhausted on key ")
    assert charged[0]["benched_for_s"] == pytest.approx(COOLDOWN, abs=1.0)


@pytest.mark.asyncio
async def test_a_route_that_is_only_out_of_credits_says_so_and_points_at_providers(
    recording,
) -> None:
    """Every model failed for one reason, so the client is told that reason."""
    pool = _pool(broke=(True,))
    executor = _executor({"commandcode": pool})

    with pytest.raises(Exception) as caught:
        await _run(executor, RoutedMessagesPlan((_routed(FLASH), _routed(SIBLING))))

    message = str(caught.value)
    assert "all keys reported exhausted credits" in message
    assert "Providers" in message
