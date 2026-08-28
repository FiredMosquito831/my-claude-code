"""Thinking budget and answer allowance come out of one ``max_tokens``.

Nothing reconciled them before: a budget could take 100% of the output, and the
Anthropic encoder raised ``max_tokens`` to ``budget + 1`` without ever
consulting the model's real limit. These tests pin the split, its proportional
answer floor, and the ``budget < max_tokens`` invariant at both the place the
number is decided and the place the body is serialised.

Output limits used here are live values: ``nvidia_nim/minimaxai/minimax-m3``
16,384; ``tencent/hy3:free`` 128,000; ``longcat-2.0:free`` 131,072;
``z-ai/glm-5.2:free`` 230,400.
"""

import pytest

from my_claude_code.application.output_tokens import OutputTokenLimits
from my_claude_code.application.reasoning_budget import (
    EFFORT_BUDGET_RATIOS,
    answer_floor_tokens,
    bound_budget,
    budget_for_effort,
    effort_for_budget,
    reconcile_reasoning_budget,
)
from my_claude_code.application.routing import (
    ModelRouter,
    apply_output_token_budget,
    apply_reasoning_budget,
)
from my_claude_code.config.constants import REASONING_ANSWER_FLOOR_MAX
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import (
    MINIMUM_BUDGET_TOKENS,
    ReasoningAdaptationKind,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)

# Live output limits, smallest first.
MINIMAX_M3 = 16_384
HY3_FREE = 128_000
LONGCAT = 131_072
GLM_52_FREE = 230_400
LIVE_LIMITS = (MINIMAX_M3, HY3_FREE, LONGCAT, GLM_52_FREE)


# ---------------------------------------------------------------------------
# The answer floor is proportional, never flat.
# ---------------------------------------------------------------------------


def test_the_answer_floor_is_capped_on_a_large_model() -> None:
    assert answer_floor_tokens(GLM_52_FREE, REASONING_ANSWER_FLOOR_MAX) == 16_384


def test_the_answer_floor_halves_a_small_model_instead_of_starving_it() -> None:
    """A flat 16,384 on a 16,384-output model leaves a budget of zero.

    That is the bug this proportional floor exists to avoid: reasoning would be
    silently disabled on the user's own MODEL_HAIKU.
    """

    assert answer_floor_tokens(MINIMAX_M3, REASONING_ANSWER_FLOOR_MAX) == 8192
    assert REASONING_ANSWER_FLOOR_MAX == 16_384


@pytest.mark.parametrize("output_limit", LIVE_LIMITS)
@pytest.mark.parametrize("effort", tuple(ReasoningEffort))
def test_every_effort_on_every_live_model_leaves_room_for_an_answer(
    output_limit: int, effort: ReasoningEffort
) -> None:
    budget = budget_for_effort(effort, output_limit)

    assert budget > 0
    assert budget < output_limit
    assert output_limit - budget >= min(REASONING_ANSWER_FLOOR_MAX, output_limit // 2)


# ---------------------------------------------------------------------------
# An effort is priced against the model, not against a flat table.
# ---------------------------------------------------------------------------


def test_high_effort_costs_more_on_a_bigger_model() -> None:
    """The flat table answered 2,048 for both of these."""

    small = budget_for_effort(ReasoningEffort.HIGH, MINIMAX_M3)
    large = budget_for_effort(ReasoningEffort.HIGH, GLM_52_FREE)

    assert small != large
    assert large > small
    assert 2048 not in {small, large}


def test_the_published_ratio_binds_where_the_answer_floor_does_not() -> None:
    assert budget_for_effort(ReasoningEffort.LOW, GLM_52_FREE) == int(
        GLM_52_FREE * EFFORT_BUDGET_RATIOS[ReasoningEffort.LOW]
    )


def test_efforts_stay_ordered_on_every_live_model() -> None:
    for output_limit in LIVE_LIMITS:
        budgets = [
            budget_for_effort(effort, output_limit) for effort in ReasoningEffort
        ]
        assert budgets == sorted(budgets)


def test_the_dialect_minimum_wins_over_the_floor_when_both_fit() -> None:
    """A 1,000-token budget is one Anthropic rejects; 1,024/976 is legal."""

    assert bound_budget(200, 2000) == MINIMUM_BUDGET_TOKENS


def test_the_invariant_holds_even_on_an_absurdly_small_allowance() -> None:
    assert bound_budget(999_999, 10) == 9


def test_the_inverse_mapping_uses_the_same_model_sized_numbers() -> None:
    """Inverting a flat table would answer ``max`` for a budget of 8,192."""

    # 50,000 tokens buys "low" (46,080) but not "medium" (115,200) on a
    # 230,400-output model. The flat table would have called it "max".
    assert effort_for_budget(50_000, GLM_52_FREE) is ReasoningEffort.LOW
    assert effort_for_budget(50_000, None) is ReasoningEffort.MAX


# ---------------------------------------------------------------------------
# Reconciliation against the resolved max_tokens.
# ---------------------------------------------------------------------------


def test_an_unknown_allowance_changes_nothing() -> None:
    policy = ReasoningPolicy.on(effort=ReasoningEffort.MAX)
    reconciled, adaptation = reconcile_reasoning_budget(policy, effective_output=None)

    assert reconciled is policy
    assert adaptation.kind is ReasoningAdaptationKind.UNCHANGED
    # The flat last-resort table is what answers when nothing is published.
    assert reconciled.numeric_budget_tokens == 8192


def test_a_client_budget_is_clamped_into_the_allowance() -> None:
    reconciled, adaptation = reconcile_reasoning_budget(
        ReasoningPolicy.on(budget_tokens=999_999),
        effective_output=MINIMAX_M3,
        model_ref="nvidia_nim/minimaxai/minimax-m3",
    )

    assert reconciled.budget_tokens == 8192
    assert adaptation.kind is ReasoningAdaptationKind.CLAMPED
    assert adaptation.message is not None
    assert "REASONING BUDGET CLAMPED" in adaptation.message


def test_an_effort_gains_a_model_sized_translation_without_an_adaptation() -> None:
    """The effort asked for is still the effort sent; only its price changed."""

    reconciled, adaptation = reconcile_reasoning_budget(
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        effective_output=HY3_FREE,
    )

    assert reconciled.effort is ReasoningEffort.HIGH
    assert reconciled.budget_tokens is None
    assert reconciled.effort_budget_tokens == budget_for_effort(
        ReasoningEffort.HIGH, HY3_FREE
    )
    assert reconciled.numeric_budget_tokens == reconciled.effort_budget_tokens
    assert adaptation.kind is ReasoningAdaptationKind.UNCHANGED


def test_a_derived_budget_never_reaches_the_caller_budget_field() -> None:
    """Encoders that can send both check ``budget_tokens`` first.

    Writing a derived value there would flip every effort-capable provider onto
    its budget channel, which is why the derived number has its own field.
    """

    reconciled, _ = reconcile_reasoning_budget(
        ReasoningPolicy.on(effort=ReasoningEffort.LOW), effective_output=LONGCAT
    )
    assert reconciled.budget_tokens is None


def test_an_off_policy_is_never_given_a_budget() -> None:
    off = ReasoningPolicy.off()
    reconciled, adaptation = reconcile_reasoning_budget(
        off, effective_output=GLM_52_FREE
    )

    assert reconciled is off
    assert adaptation.kind is ReasoningAdaptationKind.UNCHANGED


# ---------------------------------------------------------------------------
# End to end through the router, on the real ordering.
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    settings = Settings()
    settings.model = "nvidia_nim/a-model"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.reasoning_policy = ReasoningPreference.MAX
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def _request(max_tokens: int | None = None) -> MessagesRequest:
    payload = {
        "model": "claude-3-opus",
        "messages": [{"role": "user", "content": "hello"}],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return MessagesRequest.model_validate(payload)


def _routed(output_limit: int, max_tokens: int | None = None):
    router = ModelRouter(
        _settings(),
        reasoning_capability_lookup=lambda _p, _m: None,
        output_limit_lookup=lambda _p, _m: output_limit,
    )
    routed = router.resolve_messages_request(_request(max_tokens))
    return apply_reasoning_budget(apply_output_token_budget(routed, input_tokens=10))


@pytest.mark.parametrize("output_limit", LIVE_LIMITS)
def test_the_routed_budget_is_reconciled_against_the_sent_max_tokens(
    output_limit: int,
) -> None:
    """The two numbers have to come from the same allowance or they disagree."""

    routed = _routed(output_limit)
    budget = routed.reasoning.numeric_budget_tokens
    max_tokens = routed.request.max_tokens

    assert max_tokens == output_limit
    assert budget is not None
    assert 0 < budget < max_tokens


def test_a_small_model_still_gets_a_working_budget() -> None:
    """The flat-floor bug would give exactly zero here."""

    routed = _routed(MINIMAX_M3)
    assert routed.reasoning.numeric_budget_tokens == 8192


def test_a_client_max_tokens_lowers_the_budget_with_it() -> None:
    routed = _routed(GLM_52_FREE, max_tokens=4096)

    assert routed.request.max_tokens == 4096
    budget = routed.reasoning.numeric_budget_tokens
    assert budget is not None
    assert budget < 4096


def test_the_answer_floor_travels_with_the_output_limits() -> None:
    assert OutputTokenLimits().answer_floor_max == REASONING_ANSWER_FLOOR_MAX
    router = ModelRouter(_settings())
    routed = router.resolve_messages_request(_request())
    assert routed.output_limits.answer_floor_max == REASONING_ANSWER_FLOOR_MAX


# ---------------------------------------------------------------------------
# The invariant at the serialisation boundary.
# ---------------------------------------------------------------------------


def _body(max_tokens: int, policy: ReasoningPolicy) -> dict:
    return build_anthropic_messages_body(
        MessagesRequest(
            model="claude-sonnet-5",
            max_tokens=max_tokens,
            messages=[Message(role="user", content="hello")],
        ),
        reasoning=policy,
    )


@pytest.mark.parametrize("output_limit", LIVE_LIMITS)
@pytest.mark.parametrize("effort", tuple(ReasoningEffort))
def test_the_serialised_body_always_keeps_the_budget_under_max_tokens(
    output_limit: int, effort: ReasoningEffort
) -> None:
    reconciled, _ = reconcile_reasoning_budget(
        ReasoningPolicy.on(effort=effort), effective_output=output_limit
    )
    body = _body(output_limit, reconciled)

    assert body["thinking"]["budget_tokens"] < body["max_tokens"]
    assert body["max_tokens"] == output_limit


def test_a_client_supplied_budget_and_max_tokens_still_satisfy_the_invariant() -> None:
    """A client may send both, and configuration may move ``max_tokens`` after
    gating -- so the boundary is where this has to hold, not gating alone."""

    body = _body(4096, ReasoningPolicy.on(budget_tokens=999_999))

    assert body["thinking"]["budget_tokens"] == 4095
    assert body["thinking"]["budget_tokens"] < body["max_tokens"]
    # The old encoder answered max_tokens=1_000_000 here, unchecked against
    # anything the model publishes.
    assert body["max_tokens"] == 4096
