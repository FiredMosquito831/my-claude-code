"""Per-model reasoning and output preferences, at the two seams they enter.

The whole design is that they enter where the *client's* own values enter and
nowhere else, so what these tests assert is mostly negative: the pipeline
downstream is untouched, and with no preference set every decision is the one
that was made before this feature existed.
"""

import pytest

from my_claude_code.application.routing import (
    ModelRouter,
    apply_output_token_budget,
)
from my_claude_code.config.model_overrides import (
    MAX_OUTPUT_TOKENS_OVERRIDE,
    REASONING_PREFERENCE_OVERRIDE,
    ModelParameterOverrides,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import (
    Message,
    MessagesRequest,
    ThinkingConfig,
)
from my_claude_code.core.reasoning import (
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningDialect,
    ReasoningEffort,
)

MODEL = "nvidia_nim/minimaxai/minimax-m3"
PROVIDER = "nvidia_nim"
MODEL_REF = MODEL
PUBLISHED_LIMIT = 200_000
# A second real provider, so the chain has two rungs with two different
# models and the per-attempt claim can actually be tested.
FALLBACK = "open_router/z-ai/glm-5"


@pytest.fixture
def settings():
    settings = Settings()
    settings.model = MODEL
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


def table(providers=None, models=None):
    return ModelParameterOverrides.from_document(
        {"providers": providers or {}, "models": models or {}}
    )


def router(settings, preferences=None, *, output_limit=None, dialect=None):
    return ModelRouter(
        settings,
        output_limit_lookup=lambda _p, _m: output_limit,
        reasoning_dialect_lookup=(
            None if dialect is None else (lambda _p, _m: dialect)
        ),
        model_preferences=(None if preferences is None else (lambda: preferences)),
    )


def request(**kwargs):
    return MessagesRequest(
        model="claude-sonnet-4",
        messages=[Message(role="user", content="hi")],
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Reasoning
# --------------------------------------------------------------------------- #


def test_a_per_model_reasoning_preference_replaces_the_requested_policy_before_gating(
    settings,
):
    """``REASONING_POLICY=off`` is overruled for this one model."""

    routed = router(
        settings, table(models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "high"}})
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.control is ReasoningControl.ON
    assert routed.requested_reasoning.effort is ReasoningEffort.HIGH
    assert routed.preference_sources == ((REASONING_PREFERENCE_OVERRIDE, "model"),)


def test_a_provider_preference_applies_to_every_model_under_it(settings):
    routed = router(
        settings, table(providers={PROVIDER: {REASONING_PREFERENCE_OVERRIDE: "low"}})
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.effort is ReasoningEffort.LOW
    assert routed.preference_sources == ((REASONING_PREFERENCE_OVERRIDE, "provider"),)


def test_a_model_preference_beats_the_provider_preference(settings):
    routed = router(
        settings,
        table(
            providers={PROVIDER: {REASONING_PREFERENCE_OVERRIDE: "low"}},
            models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "xhigh"}},
        ),
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.effort is ReasoningEffort.XHIGH
    assert routed.preference_sources == ((REASONING_PREFERENCE_OVERRIDE, "model"),)


def test_a_model_preference_beats_an_explicit_client_thinking_ask(settings):
    """Decision Q2: the operator's per-model word outranks the client's own."""

    settings.reasoning_policy = ReasoningPreference.CLIENT
    asked = request(thinking=ThinkingConfig(type="enabled", budget_tokens=20_000))

    routed = router(
        settings, table(models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "off"}})
    ).resolve_messages_request(asked)

    assert routed.requested_reasoning.control is ReasoningControl.OFF


def test_the_client_escape_hatch_hands_the_clients_own_ask_through(settings):
    """``client`` is the per-model way to say "not my business, on this one"."""

    asked = request(thinking=ThinkingConfig(type="enabled", budget_tokens=20_000))

    routed = router(
        settings, table(models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "client"}})
    ).resolve_messages_request(asked)

    assert routed.requested_reasoning.control is ReasoningControl.ON
    assert routed.requested_reasoning.budget_tokens == 20_000


def test_minimal_is_a_storable_preference(settings):
    routed = router(
        settings, table(models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "minimal"}})
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.effort is ReasoningEffort.MINIMAL


def test_an_unparsable_stored_word_leaves_the_routes_answer_in_charge(settings):
    """A typo must not be able to change what is sent."""

    routed = router(
        settings, table(models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "ultra"}})
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.control is ReasoningControl.OFF
    assert routed.preference_sources == ()


def test_a_null_preference_on_the_model_row_silences_the_provider_row(settings):
    routed = router(
        settings,
        table(
            providers={PROVIDER: {REASONING_PREFERENCE_OVERRIDE: "high"}},
            models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: None}},
        ),
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.control is ReasoningControl.OFF
    assert routed.preference_sources == ()


def test_no_preference_leaves_the_policy_byte_identical_to_today(settings):
    """The equality contract, at this seam: an empty table decides nothing."""

    bare = router(settings).resolve_messages_request(request())
    with_empty = router(settings, table()).resolve_messages_request(request())

    assert with_empty.requested_reasoning == bare.requested_reasoning
    assert with_empty.reasoning == bare.reasoning
    assert with_empty.output_limits == bare.output_limits
    assert with_empty.preference_sources == () == bare.preference_sources


def test_an_absent_preferences_callable_changes_nothing(settings):
    """A bare ``ModelRouter(settings)`` must decide what it always decided."""

    routed = ModelRouter(settings).resolve_messages_request(request())

    assert routed.preference_sources == ()
    assert routed.output_limits.limit is None


def test_a_per_model_preference_is_resolved_per_attempt_in_a_fallback_chain(settings):
    """C2: each rung of a chain gets its OWN model's preference.

    The preference is resolved inside ``_route_for``, which the lazy chain
    calls once per rung, so a fallback to a differently-configured model
    re-resolves rather than inheriting the first rung's answer.
    """

    settings.model_sonnet = MODEL
    settings.model_sonnet_fallbacks = FALLBACK
    preferences = table(
        models={
            MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "low"},
            FALLBACK: {REASONING_PREFERENCE_OVERRIDE: "xhigh"},
        }
    )
    plan = router(settings, preferences).resolve_messages_plan(request())

    assert plan.attempts[0].requested_reasoning.effort is ReasoningEffort.LOW
    assert plan.attempts[1].requested_reasoning.effort is ReasoningEffort.XHIGH


def test_a_stored_effort_outside_the_capability_is_clamped_and_recorded_not_rejected(
    settings,
):
    """C4: out of vocabulary is an adaptation, never a 400."""

    dialect = ReasoningDialect(
        effort_values=frozenset({ReasoningEffort.LOW, ReasoningEffort.MEDIUM}),
        effort_field="reasoning_effort",
    )
    routed = router(
        settings,
        table(models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "xhigh"}}),
        dialect=dialect,
    ).resolve_messages_request(request())

    assert routed.requested_reasoning.effort is ReasoningEffort.XHIGH
    assert routed.reasoning.effort is ReasoningEffort.MEDIUM
    assert routed.reasoning_adaptation.kind is not ReasoningAdaptationKind.UNCHANGED


# --------------------------------------------------------------------------- #
# Output tokens
# --------------------------------------------------------------------------- #


def test_the_output_preference_lowers_the_effective_limit(settings):
    routed = router(
        settings,
        table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}}),
        output_limit=PUBLISHED_LIMIT,
    ).resolve_messages_request(request())

    assert routed.output_limits.limit == 4096
    assert routed.preference_sources == ((MAX_OUTPUT_TOKENS_OVERRIDE, "model"),)


def test_the_output_preference_never_raises_above_the_published_limit(settings):
    """User requirement 2, as arithmetic: ``min`` of the two, always."""

    routed = router(
        settings,
        table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 999_999}}),
        output_limit=16_384,
    ).resolve_messages_request(request())

    assert routed.output_limits.limit == 16_384


def test_the_output_preference_becomes_the_limit_when_nothing_published_one(settings):
    routed = router(
        settings,
        table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}}),
        output_limit=None,
    ).resolve_messages_request(request())

    assert routed.output_limits.limit == 4096


def test_a_client_asking_for_fewer_tokens_than_the_cap_still_gets_fewer(settings):
    """A cap, not a request: it can only ever lower.

    Asked above ``MAX_OUTPUT_TOKENS_FLOOR``, so what is being measured is the
    cap and not the floor, which raises anything below 8,192 on its own.
    """

    routed = apply_output_token_budget(
        router(
            settings,
            table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 32_768}}),
            output_limit=PUBLISHED_LIMIT,
        ).resolve_messages_request(request(max_tokens=12_000)),
        0,
    )

    assert routed.request.max_tokens == 12_000


def test_a_client_asking_for_more_than_the_cap_is_clamped_to_it(settings):
    routed = apply_output_token_budget(
        router(
            settings,
            table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}}),
            output_limit=PUBLISHED_LIMIT,
        ).resolve_messages_request(request(max_tokens=100_000)),
        0,
    )

    assert routed.request.max_tokens == 4096


def test_reasoning_widening_stops_at_the_user_cap_not_the_model_limit(settings):
    """C7, and the whole reason the cap enters as a LIMIT.

    Entered as the requested value instead, ``_widen_for_reasoning`` would
    raise a thinking turn straight back to the model's published 200,000.
    """

    settings.reasoning_policy = ReasoningPreference.HIGH
    routed = apply_output_token_budget(
        router(
            settings,
            table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}}),
            output_limit=PUBLISHED_LIMIT,
        ).resolve_messages_request(request(max_tokens=1024)),
        0,
    )

    assert routed.request.max_tokens == 4096


def test_output_widened_from_still_reports_the_clients_own_max_tokens(settings):
    """C8: the widening's "from" is the CLIENT's ask, never the cap."""

    settings.reasoning_policy = ReasoningPreference.HIGH
    routed = apply_output_token_budget(
        router(
            settings,
            table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}}),
            output_limit=PUBLISHED_LIMIT,
        ).resolve_messages_request(request(max_tokens=1024)),
        0,
    )

    assert routed.output_widened_from == 1024


def test_gating_and_the_output_budget_see_the_same_effective_limit(settings):
    """Residual risk 1: the second call site, which no other test covers.

    ``_gate_reasoning`` prices the thinking allowance against an output limit
    of its own. Reading the published limit there while the body carries the
    cap is a disagreement no wire byte would reveal, so it is asserted
    directly against the router's private seam.
    """

    built = router(
        settings,
        table(models={MODEL_REF: {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}}),
        output_limit=PUBLISHED_LIMIT,
    )
    resolved = built.resolve("claude-sonnet-4")

    assert built._effective_output_limit(resolved) == 4096
    assert built._output_limits(resolved).limit == 4096


def test_both_preferences_are_recorded_when_both_decided(settings):
    routed = router(
        settings,
        table(
            providers={PROVIDER: {MAX_OUTPUT_TOKENS_OVERRIDE: 8192}},
            models={MODEL_REF: {REASONING_PREFERENCE_OVERRIDE: "high"}},
        ),
        output_limit=PUBLISHED_LIMIT,
    ).resolve_messages_request(request())

    assert sorted(routed.preference_sources) == [
        (MAX_OUTPUT_TOKENS_OVERRIDE, "provider"),
        (REASONING_PREFERENCE_OVERRIDE, "model"),
    ]
