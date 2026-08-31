"""The sentence a limit adds to the error Claude Code actually reads.

Two things are asserted here and nothing else is. First, that every message
MCC raises from one of its own limits names the env var that set it and the
dashboard card that edits it -- and names the limit that *actually* ended the
attempt, not merely the first plausible one. Second, that the shipped
all-zero policy really is no policy: a model that says nothing is not killed.

The second is the one worth a real executor rather than a formatter call. The
zeros are a product promise ("MCC never ends a silent or stalled upstream on
its own"), and a promise about what does *not* happen can only be proved by
letting it not happen.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from my_claude_code.application.deadline_hints import (
    LIMITS_PAGE_LABEL,
    card_for,
    limit_hint,
)
from my_claude_code.application.execution import (
    ProviderExecutor,
    RouteExecutionPolicy,
    _cooldown_failure,
    _timeout_env_var,
    _timeout_failure,
)
from my_claude_code.application.ports import ProviderPort
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)

DEADLINE_ENV_VARS = (
    "FALLBACK_FIRST_TOKEN_TIMEOUT",
    "FALLBACK_ATTEMPT_SHARE_FLOOR",
    "FALLBACK_TOTAL_TIMEOUT",
    "FALLBACK_STALL_TIMEOUT",
    "FALLBACK_REASONING_ANSWER_TIMEOUT",
)


# --- the wording ----------------------------------------------------------


@pytest.mark.parametrize("env_var", DEADLINE_ENV_VARS)
def test_every_deadline_hint_names_its_var_the_page_and_the_card(
    env_var: str,
) -> None:
    hint = limit_hint(env_var)

    assert env_var in hint
    assert LIMITS_PAGE_LABEL in hint
    assert "Deadlines" in hint


def test_the_cooldown_hint_points_at_credential_health_not_deadlines() -> None:
    """Different card, because it is a different question about a key."""
    hint = limit_hint("RATE_LIMIT_COOLDOWN_SECONDS")

    assert "RATE_LIMIT_COOLDOWN_SECONDS" in hint
    assert card_for("RATE_LIMIT_COOLDOWN_SECONDS") == "Credential health"
    assert "Credential health" in hint
    assert "Deadlines" not in hint


def test_the_hint_is_ascii_end_to_end() -> None:
    """It crosses an SSE frame, a JSON body, a log row and a terminal.

    An em-dash or an arrow that renders as a replacement character in one of
    those costs more than the typography buys, so the separator is ``--`` and
    the arrow is ``->``.
    """
    hint = limit_hint("FALLBACK_TOTAL_TIMEOUT")

    hint.encode("ascii")
    assert "->" in hint
    assert hint.startswith(" (") and hint.endswith(")")


# --- which limit is named -------------------------------------------------


def test_a_silent_model_names_the_first_token_deadline() -> None:
    assert (
        _timeout_env_var(
            first_token=True, stalled=False, reasoning_only=False, share_bound=False
        )
        == "FALLBACK_FIRST_TOKEN_TIMEOUT"
    )


def test_a_model_cut_by_its_budget_share_names_the_floor_instead() -> None:
    """Sending this reader to the first-token box wastes their afternoon.

    The attempt ended because its slice of the request budget ran out, not
    because the first-token deadline elapsed -- raising the first-token box
    would change nothing at all. ``_deadline_reached`` already decides which
    of the two bound; the hint has to follow that same decision.
    """
    assert (
        _timeout_env_var(
            first_token=True, stalled=False, reasoning_only=False, share_bound=True
        )
        == "FALLBACK_ATTEMPT_SHARE_FLOOR"
    )


def test_a_stalled_stream_names_the_stall_deadline() -> None:
    assert (
        _timeout_env_var(
            first_token=False, stalled=True, reasoning_only=False, share_bound=False
        )
        == "FALLBACK_STALL_TIMEOUT"
    )


def test_a_stream_that_outran_the_request_names_the_budget() -> None:
    assert (
        _timeout_env_var(
            first_token=False, stalled=False, reasoning_only=False, share_bound=False
        )
        == "FALLBACK_TOTAL_TIMEOUT"
    )


def test_a_model_that_only_thinks_names_the_thinking_allowance() -> None:
    """Reasoning wins over first-token: it is the more specific of the two."""
    assert (
        _timeout_env_var(
            first_token=True, stalled=False, reasoning_only=True, share_bound=True
        )
        == "FALLBACK_REASONING_ANSWER_TIMEOUT"
    )


# --- the failures themselves ----------------------------------------------


def test_the_no_output_failure_carries_the_hint() -> None:
    failure = _timeout_failure("prov/model", seconds=300, first_token=True)

    assert failure.message == (
        "Provider 'prov/model' produced no output within 300s. "
        "(FALLBACK_FIRST_TOKEN_TIMEOUT -- change it on the dashboard under "
        "Limits & Resilience -> Deadlines)"
    )
    assert failure.kind is FailureKind.TIMEOUT
    assert failure.status_code == 504


def test_the_stall_failure_carries_the_hint() -> None:
    """Also the wording the committed-stream truncation path records.

    Since 6.15.0 a stall past the commit point ends the message cleanly rather
    than raising, but the attempt row in the request log still stores this
    exact failure -- so the hint has to live on the failure, not on the
    raising branch.
    """
    failure = _timeout_failure(
        "prov/model", seconds=90, first_token=False, stalled=True
    )

    assert "stopped producing output for 90s." in failure.message
    assert (
        "(FALLBACK_STALL_TIMEOUT -- change it on the dashboard under "
        "Limits & Resilience -> Deadlines)" in failure.message
    )


def test_the_budget_failure_carries_the_hint() -> None:
    failure = _timeout_failure("prov/model", seconds=600, first_token=False)

    assert "exceeded the 600s request budget." in failure.message
    assert "FALLBACK_TOTAL_TIMEOUT" in failure.message


def test_the_reasoning_failure_carries_the_hint() -> None:
    failure = _timeout_failure(
        "prov/model", seconds=450, first_token=True, reasoning_only=True
    )

    assert "produced only reasoning for 450s without answering." in failure.message
    assert "FALLBACK_REASONING_ANSWER_TIMEOUT" in failure.message


def test_the_stepped_over_cooldown_verdict_carries_the_hint() -> None:
    failure = _cooldown_failure("prov/model", 42.0)

    assert "is in rate-limit cooldown for 42s." in failure.message
    assert "RATE_LIMIT_COOLDOWN_SECONDS" in failure.message
    assert "Credential health" in failure.message


def test_a_hint_survives_credential_redaction_untouched() -> None:
    """The hint is the one part of an error message that must not be eaten.

    ``redact_sensitive_error_text`` rewrites anything shaped like ``token=``
    or ``sk-...``; an env var name that happened to match would be replaced
    with ``<redacted>`` and the reader would be sent nowhere.
    """
    for env_var in (*DEADLINE_ENV_VARS, "RATE_LIMIT_COOLDOWN_SECONDS"):
        hint = limit_hint(env_var)
        assert redact_sensitive_error_text(hint) == hint


# --- the promise the zeros make -------------------------------------------


class _SilentProvider:
    """Accepts the request and then says nothing, forever."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def throttle_remaining(self) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        return None

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.entered.set()
        await asyncio.sleep(3600)
        yield "event: message_stop\ndata: {}\n\n"


def _routed(provider_id: str) -> RoutedMessagesRequest:
    request = MessagesRequest(
        model="model",
        messages=[Message(role="user", content="hello")],
        stream=True,
    )
    return RoutedMessagesRequest(
        request=request,
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id=provider_id,
            provider_model="model",
            provider_model_ref=f"{provider_id}/model",
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
        token_counter=lambda _m, _s, _t: 1,
        policy=RouteExecutionPolicy(),
    )


@pytest.mark.asyncio
async def test_the_shipped_policy_never_ends_a_silent_model_itself() -> None:
    """The whole product decision, asserted as an absence.

    Four silent models on the chain and the shipped defaults, which are all
    zero. Nothing MCC owns may end this request: no first-token deadline, no
    share of a budget that does not exist, no stall clock, no thinking
    allowance. It runs until the client goes away -- here, until this test
    cancels it -- and the chain never moves, because no provider returned an
    error for it to move on.

    A deadline that has crept back in would surface as an ExecutionFailure
    inside the wait, so the assertion is that the wait times out with the task
    still pending.
    """
    providers = {f"p{i}": _SilentProvider() for i in range(4)}
    plan = RoutedMessagesPlan(tuple(_routed(name) for name in providers))
    stream = _executor(providers).stream(
        plan,
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_all_zero",
    )

    async def drain() -> list[str]:
        return [chunk async for chunk in stream]

    task = asyncio.ensure_future(drain())
    done, pending = await asyncio.wait({task}, timeout=1.5)

    assert not done, "the all-zero policy killed a silent model"
    assert pending == {task}
    # Only the first model was ever reached: with nothing ending the attempt,
    # the chain has no reason to advance.
    assert providers["p0"].entered.is_set()
    assert not providers["p1"].entered.is_set()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_one_deadline_set_is_enough_to_get_the_failover_back() -> None:
    """The other half of the promise: the zeros are a default, not a wall.

    Same four silent models, same executor, one setting changed. The chain
    moves, and the error names the setting that moved it.
    """
    providers = {f"p{i}": _SilentProvider() for i in range(4)}
    plan = RoutedMessagesPlan(tuple(_routed(name) for name in providers))
    executor = ProviderExecutor(
        lambda provider_id: providers[provider_id],
        token_counter=lambda _m, _s, _t: 1,
        policy=RouteExecutionPolicy(first_token_timeout=0.05),
    )

    stream = executor.stream(
        plan,
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_one_deadline",
    )

    with pytest.raises(ExecutionFailure) as caught:
        async for _chunk in stream:
            pass

    assert all(provider.entered.is_set() for provider in providers.values())
    assert "produced no output within" in caught.value.message
    assert "FALLBACK_FIRST_TOKEN_TIMEOUT" in caught.value.message
    assert f"{LIMITS_PAGE_LABEL} -> Deadlines" in caught.value.message
