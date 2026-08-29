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


def _settings(*, ceiling: int | None = None) -> Settings:
    """Router settings for these cases, with the output head lifted by default.

    The shipped head is 131,072 and two of the live limits below are larger,
    so leaving it on would make those cases assert the head rather than the
    thing they are named for. The head has its own tests in
    ``tests/application/test_output_tokens.py``; one case here passes it
    explicitly to prove it still binds a widened allowance.
    """

    settings = Settings()
    settings.max_output_tokens_ceiling = ceiling
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


def _routed(
    output_limit: int,
    max_tokens: int | None = None,
    *,
    ceiling: int | None = None,
    reasoning: ReasoningPreference | None = None,
):
    settings = _settings(ceiling=ceiling)
    if reasoning is not None:
        settings.reasoning_policy = reasoning
    router = ModelRouter(
        settings,
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


def test_a_client_max_tokens_above_the_limit_lowers_the_budget_with_it() -> None:
    """The clamp direction is unchanged: an ask above the model still comes down."""

    routed = _routed(MINIMAX_M3, max_tokens=GLM_52_FREE)

    assert routed.request.max_tokens == MINIMAX_M3
    budget = routed.reasoning.numeric_budget_tokens
    assert budget is not None
    assert budget < MINIMAX_M3


def test_a_client_max_tokens_below_the_limit_no_longer_starves_a_thinking_turn():
    """6.7.0 priced a thinking budget from an answer-sized ask; 6.8.0 does not.

    The client sent 4,096 for an answer. It did not know the route was a
    thinking route, and it has no way to know how much this model can emit.
    """

    routed = _routed(GLM_52_FREE, max_tokens=4096)

    assert routed.request.max_tokens == GLM_52_FREE
    assert routed.output_widened_from == 4096
    budget = routed.reasoning.numeric_budget_tokens
    assert budget is not None
    assert budget == GLM_52_FREE - REASONING_ANSWER_FLOOR_MAX
    assert budget < routed.request.max_tokens


def test_the_rung_ratio_applies_to_the_widened_allowance() -> None:
    """The worked example, asserted: a 64,000 ask at ``max`` on a 262,144 model."""

    routed = _routed(262_144, max_tokens=64_000, ceiling=131_072)

    assert routed.request.max_tokens == 131_072
    assert routed.output_widened_from == 64_000
    assert answer_floor_tokens(131_072, REASONING_ANSWER_FLOOR_MAX) == 16_384
    assert routed.reasoning.numeric_budget_tokens == 114_688
    # 0.95 x 131,072 is 124,518, so the answer floor -- not the ratio -- is
    # what decides here, exactly as it did at 64,000.
    assert routed.reasoning.numeric_budget_tokens == 131_072 - 16_384


def test_the_answer_floor_is_unchanged_by_widening() -> None:
    """``min(16384, output // 2)`` on both sides of the widening: a cap, not a share."""

    narrow = _routed(262_144, max_tokens=64_000, reasoning=ReasoningPreference.OFF)
    assert narrow.request.max_tokens == 64_000

    for output in (64_000, 131_072, 262_144):
        assert answer_floor_tokens(output, REASONING_ANSWER_FLOOR_MAX) == 16_384
    # And it is still proportional where the halving bites.
    assert answer_floor_tokens(MINIMAX_M3, REASONING_ANSWER_FLOOR_MAX) == 8_192


def test_an_effort_only_host_keeps_its_rung_and_only_gains_answer_room() -> None:
    """The WORKING-NOTES 70(a) regression: the rung was right, the room was not.

    Nothing about the level moves. The allowance it is spent from does, so the
    answer stops being squeezed out by the thinking in front of it.
    """

    narrow = _routed(8_000, max_tokens=8_000, ceiling=131_072)
    wide = _routed(262_144, max_tokens=8_000, ceiling=131_072)

    # Same rung, asked for and applied, on both sides.
    assert wide.reasoning.effort is wide.requested_reasoning.effort
    assert wide.reasoning.effort is narrow.reasoning.effort is ReasoningEffort.MAX
    # Only the allowance moved -- and only because the model published more.
    assert narrow.request.max_tokens == 8_000
    assert narrow.output_widened_from is None
    assert wide.request.max_tokens == 131_072
    assert wide.output_widened_from == 8_000


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
