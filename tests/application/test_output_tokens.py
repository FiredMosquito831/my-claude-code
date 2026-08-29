"""Per-model output-token budgets (WORKING-NOTES 54).

    requested <= model maximum  ->  send what was requested
    requested >  model maximum  ->  send the MODEL'S MAXIMUM
    unknown                     ->  fall back, and say so

The numbers used here are real: they are the published output limits of models
this proxy actually routes to, so a regression shows up as a model the user
recognises rather than an abstract one.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.application.output_tokens import (
    OutputTokenLimits,
    resolve_max_output_tokens,
)
from my_claude_code.application.routing import (
    ModelRouter,
    apply_output_token_budget,
)
from my_claude_code.config.constants import (
    ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS_CEILING,
    MAX_OUTPUT_TOKENS_CONTEXT_FLOOR,
    MAX_OUTPUT_TOKENS_CONTEXT_MARGIN,
    MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT,
)
from my_claude_code.config.provider_catalog import GROQ_DEFAULT_BASE
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, profiled_provider

# Measured on the user's live routes, 2026-08.
MINIMAX_M3 = 16_384  # nvidia_nim/minimaxai/minimax-m3
HY3_FREE = 128_000  # nous_portal/tencent/hy3:free
LONGCAT = 131_072  # meituan/longcat-2.0:free
GLM_52_FREE = 230_400  # z-ai/glm-5.2:free
BIG_MODEL = 262_144


def resolve(
    requested: int | None,
    *,
    limit: int | None = None,
    context_length: int | None = None,
    unknown_default: int | None = MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT,
    ceiling: int | None = MAX_OUTPUT_TOKENS_CEILING,
    context_margin: int = MAX_OUTPUT_TOKENS_CONTEXT_MARGIN,
    context_floor: int = MAX_OUTPUT_TOKENS_CONTEXT_FLOOR,
    input_tokens: int = 0,
    for_reasoning: bool = False,
) -> int | None:
    """Resolve one budget against the shipped configuration defaults.

    ``ceiling`` carries the shipped 131,072 by default, exactly as a real
    install does. Cases about a capability *above* that number pass
    ``ceiling=None`` explicitly, so they keep testing the thing they are named
    for rather than passing because the head happened to land on the same
    value.
    """

    return resolve_max_output_tokens(
        requested,
        limits=OutputTokenLimits(
            limit=limit,
            context_length=context_length,
            unknown_default=unknown_default,
            ceiling=ceiling,
            context_margin=context_margin,
            context_floor=context_floor,
        ),
        input_tokens=input_tokens,
        model_ref="nvidia_nim/minimaxai/minimax-m3",
        for_reasoning=for_reasoning,
    )


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_client_value_below_the_limit_passes_through_untouched():
    assert resolve(4_096, limit=MINIMAX_M3) == 4_096


def test_client_value_above_the_limit_is_clamped_to_the_limit(caplog):
    with caplog.at_level("WARNING"):
        assert resolve(ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS, limit=MINIMAX_M3) == (
            MINIMAX_M3
        )

    assert "MAX TOKENS CLAMPED" in caplog.text
    assert "minimax-m3" in caplog.text
    assert "81920" in caplog.text
    assert "16384" in caplog.text


def test_client_value_exactly_at_the_limit_is_not_clamped():
    assert resolve(MINIMAX_M3, limit=MINIMAX_M3) == MINIMAX_M3


@pytest.mark.parametrize("limit", [MINIMAX_M3, HY3_FREE, LONGCAT, GLM_52_FREE])
def test_no_client_value_sends_the_models_full_capability(limit):
    """Not 81920. The whole point: under-using a model is as wrong as over-asking.

    ``ceiling=None`` because this is a statement about *capability*, and
    GLM_52_FREE publishes more than the shipped head. With the head in place
    the case would still pass, at 131,072, for a reason that has nothing to do
    with what it is checking.
    """

    assert resolve(None, limit=limit, ceiling=None) == limit
    assert resolve(None, limit=limit, ceiling=None) != (
        ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
    )


# --------------------------------------------------------------------------- #
# Unknown: a fallback, never a cap
# --------------------------------------------------------------------------- #


def test_unknown_model_without_a_client_value_uses_the_configured_fallback():
    assert resolve(None) == MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT


def test_unknown_model_never_clamps_an_explicit_client_value_below_itself():
    """A number nobody published has no standing to shrink an explicit ask."""

    requested = MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT * 4
    assert resolve(requested) == requested


def test_unknown_fallback_can_be_switched_off_entirely():
    """No fallback and no limit leaves max_tokens unset for the profile default."""

    assert resolve(None, unknown_default=None) is None


# --------------------------------------------------------------------------- #
# The ceiling: off by default, and it must not undercut a real capability
# --------------------------------------------------------------------------- #


def test_the_default_ceiling_is_the_head_a_262k_model_is_held_to():
    """The head ships set (6.8.0) because a thinking turn asks for the maximum.

    It is still not a per-model opinion: it never raises anything, and a model
    publishing less than it is untouched (the case below).
    """

    assert MAX_OUTPUT_TOKENS_CEILING == 131_072
    assert resolve(None, limit=BIG_MODEL) == 131_072
    assert resolve(None, limit=MINIMAX_M3) == MINIMAX_M3


def test_the_shipped_ceiling_can_be_lifted_so_a_262k_model_gets_262k():
    """``0`` in the environment, ``None`` here: the way back to no head."""

    assert resolve(None, limit=BIG_MODEL, ceiling=None) == BIG_MODEL


def test_ceiling_binds_when_the_operator_sets_one(caplog):
    with caplog.at_level("WARNING"):
        assert resolve(None, limit=BIG_MODEL, ceiling=32_000) == 32_000

    assert "MAX TOKENS CEILING" in caplog.text


def test_ceiling_above_the_models_limit_changes_nothing():
    assert resolve(None, limit=MINIMAX_M3, ceiling=BIG_MODEL) == MINIMAX_M3


# --------------------------------------------------------------------------- #
# Zero is a value, not an absence
# --------------------------------------------------------------------------- #


def test_client_zero_is_not_treated_as_unset():
    assert resolve(0, limit=LONGCAT) == 0


def test_client_zero_on_an_unknown_model_is_not_replaced_by_the_fallback():
    assert resolve(0) == 0


# --------------------------------------------------------------------------- #
# Context headroom
# --------------------------------------------------------------------------- #


def test_output_equal_to_context_is_bounded_by_what_the_prompt_left(caplog):
    """15% of the models.dev catalogue reports limit.output == limit.context."""

    with caplog.at_level("WARNING"):
        resolved = resolve(
            None,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=100_000,
        )

    assert resolved == LONGCAT - 100_000 - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN
    assert "MAX TOKENS BOUNDED BY CONTEXT" in caplog.text


def test_negative_headroom_leaves_the_request_unmodified():
    """Let the provider report the real error rather than send max_tokens<=0."""

    assert (
        resolve(
            None,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=LONGCAT,
        )
        == LONGCAT
    )


def test_headroom_exactly_zero_leaves_the_request_unmodified():
    assert (
        resolve(
            8_192,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=LONGCAT - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN,
        )
        == 8_192
    )


def test_a_small_output_limit_inside_a_large_context_is_never_touched():
    assert (
        resolve(
            None,
            limit=MINIMAX_M3,
            context_length=LONGCAT,
            input_tokens=90_000,
        )
        == MINIMAX_M3
    )


def test_context_margin_of_zero_reserves_nothing():
    assert (
        resolve(
            None,
            limit=HY3_FREE,
            context_length=HY3_FREE,
            input_tokens=8_000,
            context_margin=0,
        )
        == HY3_FREE - 8_000
    )


def test_headroom_above_the_floor_is_still_sent(caplog):
    """The live case: opencode/hy3-free style, 131,072 context, 100,000 prompt.

    Pinned separately from the bounding test above so the floor cannot quietly
    start rejecting the requests this path exists to rescue. 30,048 is far
    above the 4,096 floor, so nothing changes.
    """

    with caplog.at_level("WARNING"):
        resolved = resolve(
            None,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=100_000,
        )

    expected = LONGCAT - 100_000 - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN
    assert resolved == expected == 30_048
    assert expected > MAX_OUTPUT_TOKENS_CONTEXT_FLOOR
    assert "MAX TOKENS BOUNDED BY CONTEXT" in caplog.text
    assert "unchanged" not in caplog.text


def test_a_positive_headroom_below_the_floor_leaves_the_request_unmodified(caplog):
    """max_tokens: 3 technically succeeds, and a one-token answer is worse than
    a clear error. Below the floor, do what headroom <= 0 already does."""

    with caplog.at_level("WARNING"):
        resolved = resolve(
            None,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=LONGCAT - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN - 3,
        )

    assert resolved == LONGCAT
    assert "MAX TOKENS BOUNDED BY CONTEXT" in caplog.text
    assert "nvidia_nim/minimaxai/minimax-m3" in caplog.text
    assert str(LONGCAT) in caplog.text  # the context length
    assert "leaving only 3 output tokens" in caplog.text
    assert "MAX_OUTPUT_TOKENS_CONTEXT_FLOOR=4096" in caplog.text


def test_headroom_exactly_at_the_floor_is_sent():
    """The floor is inclusive: a budget equal to it is worth sending."""

    assert (
        resolve(
            None,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=LONGCAT
            - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN
            - MAX_OUTPUT_TOKENS_CONTEXT_FLOOR,
        )
        == MAX_OUTPUT_TOKENS_CONTEXT_FLOOR
    )


def test_a_floor_of_zero_restores_the_pre_floor_behaviour():
    """Operators who want any positive headroom sent can still have it."""

    assert (
        resolve(
            None,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=LONGCAT - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN - 3,
            context_floor=0,
        )
        == 3
    )


def test_the_floor_is_configurable_from_the_environment():
    # Env values arrive as strings and the model coerces them, which a
    # precisely-typed kwargs dict cannot express -- same shape as
    # tests/config/test_limit_bounds.py.
    overridden: dict[str, Any] = {
        "_env_file": None,
        "MAX_OUTPUT_TOKENS_CONTEXT_FLOOR": "8192",
    }
    shipped: dict[str, Any] = {"_env_file": None}
    assert Settings(**overridden).max_output_tokens_context_floor == 8_192
    assert (
        Settings(**shipped).max_output_tokens_context_floor
        == MAX_OUTPUT_TOKENS_CONTEXT_FLOOR
    )


# --------------------------------------------------------------------------- #
# Widening for a thinking turn
#
# One extra step, run first: a request that is going to think starts from the
# model's own published limit rather than from an ask the client sized for an
# answer it did not know would be sharing the allowance. Every clamp below it
# is unchanged, which is what most of this section asserts.
# --------------------------------------------------------------------------- #


def test_a_thinking_turn_starts_from_the_models_maximum_not_the_clients_ask(caplog):
    with caplog.at_level("INFO"):
        resolved = resolve(64_000, limit=BIG_MODEL, ceiling=None, for_reasoning=True)

    assert resolved == BIG_MODEL
    assert "MAX TOKENS WIDENED FOR REASONING" in caplog.text
    assert "64000" in caplog.text
    # INFO, not a warning: nothing was refused and nothing was invented.
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_widening_never_lowers_a_client_ask_above_the_limit():
    """A client above the limit still meets the ordinary clamp, not the widening."""

    assert resolve(BIG_MODEL, limit=MINIMAX_M3, for_reasoning=True) == MINIMAX_M3
    assert resolve(MINIMAX_M3, limit=MINIMAX_M3, for_reasoning=True) == MINIMAX_M3


def test_widening_is_clamped_by_the_ceiling_then_by_context_headroom():
    """The four steps still run, in the same order, on the widened value."""

    assert resolve(64_000, limit=BIG_MODEL, for_reasoning=True) == 131_072
    assert (
        resolve(
            64_000,
            limit=BIG_MODEL,
            context_length=200_000,
            input_tokens=120_000,
            for_reasoning=True,
        )
        == 200_000 - 120_000 - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN
    )


def test_an_unknown_output_limit_is_never_widened_by_the_unknown_fallback():
    """A number nobody published has no standing to raise an explicit request.

    The same rule that stops MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT lowering one.
    """

    assert resolve(4_096, limit=None, for_reasoning=True) == 4_096
    assert resolve(None, limit=None, for_reasoning=True) == (
        MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT
    )


@pytest.mark.parametrize(
    ("requested", "limit", "context_length", "input_tokens"),
    [
        (None, BIG_MODEL, None, 0),
        (64_000, BIG_MODEL, None, 0),
        (64_000, MINIMAX_M3, None, 0),
        (None, None, None, 0),
        (64_000, LONGCAT, LONGCAT, 100_000),
        (0, BIG_MODEL, None, 0),
    ],
)
def test_a_non_reasoning_request_is_byte_identical_to_6_7_0(
    requested, limit, context_length, input_tokens
):
    """Rule 1 of the contract: reasoning off changes nothing at all.

    ``ceiling=None`` throughout, because the 6.8.0 head is a separate
    behaviour change with its own tests; this one is about the widening not
    leaking into requests that never asked to think.
    """

    without = resolve(
        requested,
        limit=limit,
        context_length=context_length,
        input_tokens=input_tokens,
        ceiling=None,
    )
    with_flag_off = resolve(
        requested,
        limit=limit,
        context_length=context_length,
        input_tokens=input_tokens,
        ceiling=None,
        for_reasoning=False,
    )

    assert without == with_flag_off


def test_an_explicit_zero_is_not_widened():
    """An explicit zero is a statement, and a zero paired with a thinking
    request is the client contradicting itself, not ours to resolve."""

    assert resolve(0, limit=BIG_MODEL, for_reasoning=True) == 0


def test_widening_below_the_context_floor_still_leaves_the_request_unmodified(caplog):
    """The floor's rule is unchanged; it now guards a widened number too."""

    with caplog.at_level("WARNING"):
        resolved = resolve(
            8_192,
            limit=LONGCAT,
            context_length=LONGCAT,
            input_tokens=LONGCAT - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN - 3,
            for_reasoning=True,
        )

    # Widened to the model's limit, then left there: a headroom of 3 is below
    # the floor, so the provider gets to report the real context error.
    assert resolved == LONGCAT
    assert "leaving only 3 output tokens" in caplog.text


def test_the_default_ceiling_can_be_lifted_with_zero():
    """The sentinel, at the Settings layer where an operator actually types it."""

    lifted: dict[str, Any] = {"_env_file": None, "MAX_OUTPUT_TOKENS_CEILING": "0"}
    shipped: dict[str, Any] = {"_env_file": None}
    blank: dict[str, Any] = {"_env_file": None, "MAX_OUTPUT_TOKENS_CEILING": ""}

    assert Settings(**lifted).max_output_tokens_ceiling is None
    assert Settings(**shipped).max_output_tokens_ceiling == 131_072
    # Blank means "use the default", not "no ceiling" -- the sharpest edge in
    # this change, pinned so nobody "fixes" it back.
    assert Settings(**blank).max_output_tokens_ceiling == 131_072


# --------------------------------------------------------------------------- #
# End to end through the router
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings():
    settings = Settings()
    settings.model = "nvidia_nim/minimaxai/minimax-m3"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.reasoning_policy = ReasoningPreference.OFF
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def route(settings, request, *, output_limit=None, context_length=None):
    router = ModelRouter(
        settings,
        output_limit_lookup=lambda _p, _m: output_limit,
        context_length_lookup=lambda _p, _m: context_length,
    )
    return apply_output_token_budget(router.resolve_messages_request(request), 0)


def test_router_defaults_are_unset_so_nothing_changes_without_lookups(settings):
    """An absent catalogue must leave the shipped fallback in charge, not 81920."""

    request = MessagesRequest(
        model="claude-sonnet-4",
        messages=[Message(role="user", content="hi")],
    )
    routed = route(settings, request)

    assert routed.request.max_tokens == MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT


def test_router_sends_the_full_published_limit_when_the_client_omits_one(settings):
    # Lifted, because this is a statement about the model's capability and
    # GLM_52_FREE publishes more than the shipped head.
    settings.max_output_tokens_ceiling = None
    request = MessagesRequest(
        model="claude-sonnet-4",
        messages=[Message(role="user", content="hi")],
    )
    routed = route(settings, request, output_limit=GLM_52_FREE)

    assert routed.request.max_tokens == GLM_52_FREE
    assert routed.output_widened_from is None


def test_router_holds_a_230k_model_to_the_shipped_head(settings):
    """The other half of the case above: what a default install actually sends."""

    request = MessagesRequest(
        model="claude-sonnet-4",
        messages=[Message(role="user", content="hi")],
    )
    routed = route(settings, request, output_limit=GLM_52_FREE)

    assert routed.request.max_tokens == MAX_OUTPUT_TOKENS_CEILING == 131_072


def test_router_clamps_the_client_value_and_leaves_the_caller_request_alone(settings):
    request = MessagesRequest(
        model="claude-sonnet-4",
        max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        messages=[Message(role="user", content="hi")],
    )
    routed = route(settings, request, output_limit=MINIMAX_M3)

    assert routed.request.max_tokens == MINIMAX_M3
    assert request.max_tokens == ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS


def test_router_applies_the_operator_ceiling_from_settings(settings):
    settings.max_output_tokens_ceiling = 8_000
    request = MessagesRequest(
        model="claude-sonnet-4",
        messages=[Message(role="user", content="hi")],
    )
    routed = route(settings, request, output_limit=BIG_MODEL)

    assert routed.request.max_tokens == 8_000

    # ...and the unset path still gives the model everything it published.
    settings.max_output_tokens_ceiling = None
    assert route(settings, request, output_limit=BIG_MODEL).request.max_tokens == (
        BIG_MODEL
    )


def test_router_bounds_by_context_using_the_prompts_token_count(settings):
    router = ModelRouter(
        settings,
        output_limit_lookup=lambda _p, _m: HY3_FREE,
        context_length_lookup=lambda _p, _m: HY3_FREE,
    )
    request = MessagesRequest(
        model="claude-sonnet-4",
        messages=[Message(role="user", content="hi")],
    )
    routed = apply_output_token_budget(
        router.resolve_messages_request(request), 120_000
    )

    assert routed.request.max_tokens == (
        HY3_FREE - 120_000 - MAX_OUTPUT_TOKENS_CONTEXT_MARGIN
    )


# --------------------------------------------------------------------------- #
# Cooperation with the reactive cap learned from a provider 400
# --------------------------------------------------------------------------- #


@pytest.fixture
def groq_provider():
    return profiled_provider(
        "groq",
        ProviderConfig(
            api_key="test_groq_key",
            base_url=GROQ_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


@pytest.mark.asyncio
async def test_a_learned_cap_beats_the_catalogue_limit(groq_provider):
    """Ground truth from the provider outranks a published claim about it."""

    body = groq_provider._build_request_body(
        make_messages_request(
            "llama-3.3-70b-versatile",
            # What the catalogue said this model can emit, already applied by
            # routing before the provider ever saw the request.
            max_tokens=LONGCAT,
            thinking={"enabled": False},
        )
    )
    assert body["max_completion_tokens"] == LONGCAT
    groq_provider._model_output_caps[body["model"]] = 40_960

    create = AsyncMock(return_value=object())
    with patch.object(groq_provider._client.chat.completions, "create", create):
        _stream, used_body = await groq_provider._create_stream(body)

    assert used_body["max_completion_tokens"] == 40_960


@pytest.mark.asyncio
async def test_a_learned_cap_never_raises_a_smaller_catalogue_limit(groq_provider):
    """An "at most N" does not contradict a catalogue value below N."""

    body = groq_provider._build_request_body(
        make_messages_request(
            "llama-3.3-70b-versatile",
            max_tokens=MINIMAX_M3,
            thinking={"enabled": False},
        )
    )
    groq_provider._model_output_caps[body["model"]] = 40_960

    create = AsyncMock(return_value=object())
    with patch.object(groq_provider._client.chat.completions, "create", create):
        _stream, used_body = await groq_provider._create_stream(body)

    assert used_body["max_completion_tokens"] == MINIMAX_M3
