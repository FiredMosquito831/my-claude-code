"""Matrix tests for per-model reasoning gating.

The unit under test is :func:`adapt_reasoning_policy`, but the assertions that
matter are on the *emitted request body*: the policy is only interesting
because of what a provider encoder does with it. Every case therefore runs the
adapted policy through real provider encoders and asserts the exact body.
"""

from typing import Any

import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_gating import (
    EFFORT_BUDGET_RATIOS,
    MINIMUM_BUDGET_TOKENS,
    adapt_reasoning_policy,
)
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

# One profile per distinct encoder shape in the codebase. If a new encoder
# shape appears, add it here: this tuple is what the R0 guarantee is proved
# over.
REPRESENTATIVE_PROFILES = (
    "groq",  # NamedEffortReasoning -> reasoning_effort
    "vercel",  # ReasoningObject -> extra_body.reasoning
    "zai",  # ThinkingObjectReasoning -> extra_body.thinking
    "fireworks",  # EffortOrThinkingBudgetReasoning
    "llamacpp",  # LlamaCppReasoning -> numeric budget
    "featherless",  # ChatTemplateReasoning -> chat_template_kwargs
    "minimax",  # SplitReasoningOutput -> unconditional
    "xai",  # NoReasoning -> never touches the body
)

UNKNOWN = None
CANNOT_REASON = ModelReasoningCapability(can_reason=False)
EFFORT_ONLY_LOW_MEDIUM_HIGH = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supports_toggle_control=False,
    supports_budget_control=False,
    supported_efforts=frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
    ),
)
EFFORT_ONLY_HIGH_MAX = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supports_toggle_control=False,
    supports_budget_control=False,
    supported_efforts=frozenset({ReasoningEffort.HIGH, ReasoningEffort.MAX}),
)
TOGGLE_ONLY = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=False,
    supports_toggle_control=True,
    supports_budget_control=False,
    supported_efforts=None,
)
BUDGET_ONLY = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=False,
    supports_toggle_control=False,
    supports_budget_control=True,
    supported_efforts=None,
)
ALL_CAPABILITIES = (
    UNKNOWN,
    CANNOT_REASON,
    EFFORT_ONLY_LOW_MEDIUM_HIGH,
    EFFORT_ONLY_HIGH_MAX,
    TOGGLE_ONLY,
    BUDGET_ONLY,
)

ALL_POLICIES = (
    ReasoningPolicy.provider_default(),
    ReasoningPolicy.off(),
    ReasoningPolicy.on(),
    *(ReasoningPolicy.on(effort=effort) for effort in ReasoningEffort),
    ReasoningPolicy.on(budget_tokens=4096),
    ReasoningPolicy.on(effort=ReasoningEffort.HIGH, budget_tokens=4096),
    ReasoningPolicy(control=ReasoningControl.DEFAULT, effort=ReasoningEffort.HIGH),
    ReasoningPolicy(control=ReasoningControl.OFF, effort=ReasoningEffort.HIGH),
)


def encode(profile_id: str, policy: ReasoningPolicy) -> dict[str, Any]:
    """Return the request body one provider builds for one policy."""

    body: dict[str, Any] = {"model": "a-model", "messages": []}
    OPENAI_CHAT_PROFILES[profile_id].reasoning.encode(body, policy)
    return body


def gated_body(
    profile_id: str,
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    *,
    max_tokens: int | None = 4096,
    output_limit: int | None = 8192,
) -> dict[str, Any]:
    return encode(
        profile_id,
        adapt_reasoning_policy(
            policy,
            capability,
            max_tokens=max_tokens,
            output_limit=output_limit,
            model_ref="provider/a-model",
        ),
    )


def field_paths(value: Any, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    paths: set[str] = set()
    for key, nested in value.items():
        paths |= field_paths(nested, f"{prefix}.{key}" if prefix else str(key))
    return paths


# ---------------------------------------------------------------------------
# R0 -- the guarantee this whole PR rests on.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_id", REPRESENTATIVE_PROFILES)
@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_unknown_capability_leaves_every_request_byte_identical(
    profile_id: str, policy: ReasoningPolicy
) -> None:
    """No models.dev row for a model means the request must not change at all."""

    assert adapt_reasoning_policy(policy, None) is policy
    assert gated_body(profile_id, policy, UNKNOWN) == encode(profile_id, policy)


def _router_settings() -> Settings:
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


def _router_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-3-opus",
        max_tokens=4096,
        messages=[Message(role="user", content="hello")],
    )


def test_a_router_without_lookups_never_gates() -> None:
    """A ModelRouter built without the lookups behaves exactly as before."""

    routed = ModelRouter(_router_settings()).resolve_messages_request(_router_request())
    assert routed.reasoning == ReasoningPolicy.on(effort=ReasoningEffort.MAX)


def test_the_router_applies_the_capability_it_is_given() -> None:
    """The wiring, end to end: a MAX tier lands on a model that tops out at high."""

    routed = ModelRouter(
        _router_settings(),
        reasoning_capability_lookup=lambda _p, _m: EFFORT_ONLY_LOW_MEDIUM_HIGH,
        output_limit_lookup=lambda _p, _m: 8192,
    ).resolve_messages_request(_router_request())
    assert routed.reasoning == ReasoningPolicy.on(effort=ReasoningEffort.HIGH)


def test_the_router_leaves_an_unknown_model_alone() -> None:
    routed = ModelRouter(
        _router_settings(),
        reasoning_capability_lookup=lambda _p, _m: None,
        output_limit_lookup=lambda _p, _m: None,
    ).resolve_messages_request(_router_request())
    assert routed.reasoning == ReasoningPolicy.on(effort=ReasoningEffort.MAX)


def test_the_router_feeds_request_max_tokens_into_a_synthesised_budget() -> None:
    routed = ModelRouter(
        _router_settings(),
        reasoning_capability_lookup=lambda _p, _m: BUDGET_ONLY,
        output_limit_lookup=lambda _p, _m: 65_536,
    ).resolve_messages_request(_router_request())
    # effective_max is the request's own 4096, not the model's 65536 limit.
    assert routed.reasoning.budget_tokens == int(4096 * 0.95)


# ---------------------------------------------------------------------------
# R2 -- an explicit OFF is never rewritten, whatever the model can do.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", ALL_CAPABILITIES)
@pytest.mark.parametrize("profile_id", REPRESENTATIVE_PROFILES)
def test_policy_off_is_unchanged_in_every_capability_shape(
    capability: ModelReasoningCapability | None, profile_id: str
) -> None:
    off = ReasoningPolicy.off()
    assert adapt_reasoning_policy(off, capability) is off
    assert gated_body(profile_id, off, capability) == encode(profile_id, off)


# ---------------------------------------------------------------------------
# R1 -- a model known not to reason gets no reasoning fields.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        ("groq", {"model": "a-model", "messages": []}),
        ("vercel", {"model": "a-model", "messages": []}),
        ("zai", {"model": "a-model", "messages": []}),
        ("fireworks", {"model": "a-model", "messages": []}),
        ("llamacpp", {"model": "a-model", "messages": []}),
        ("featherless", {"model": "a-model", "messages": []}),
        ("xai", {"model": "a-model", "messages": []}),
    ],
)
def test_model_known_not_to_reason_emits_no_reasoning_fields(
    profile_id: str, expected: dict[str, Any]
) -> None:
    body = gated_body(
        profile_id, ReasoningPolicy.on(effort=ReasoningEffort.HIGH), CANNOT_REASON
    )
    assert body == expected


def test_model_known_not_to_reason_drops_an_explicit_budget() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=9000), CANNOT_REASON
    )
    assert adapted == ReasoningPolicy.provider_default()


# ---------------------------------------------------------------------------
# R3 -- effort clamping.
# ---------------------------------------------------------------------------


def test_supported_effort_is_sent_unchanged() -> None:
    policy = ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    adapted = adapt_reasoning_policy(policy, EFFORT_ONLY_LOW_MEDIUM_HIGH)
    assert adapted is policy
    assert (
        gated_body("groq", policy, EFFORT_ONLY_LOW_MEDIUM_HIGH)["reasoning_effort"]
        == "medium"
    )


@pytest.mark.parametrize("requested", [ReasoningEffort.XHIGH, ReasoningEffort.MAX])
def test_effort_above_the_models_vocabulary_clamps_down_to_high(
    requested: ReasoningEffort,
) -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=requested), EFFORT_ONLY_LOW_MEDIUM_HIGH
    )
    assert adapted.effort is ReasoningEffort.HIGH
    body = gated_body(
        "groq", ReasoningPolicy.on(effort=requested), EFFORT_ONLY_LOW_MEDIUM_HIGH
    )
    assert body["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "requested", [ReasoningEffort.MINIMAL, ReasoningEffort.LOW, ReasoningEffort.MEDIUM]
)
def test_effort_below_the_models_vocabulary_clamps_up_to_its_lowest(
    requested: ReasoningEffort,
) -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=requested), EFFORT_ONLY_HIGH_MAX
    )
    assert adapted.effort is ReasoningEffort.HIGH
    body = gated_body(
        "groq", ReasoningPolicy.on(effort=requested), EFFORT_ONLY_HIGH_MAX
    )
    assert body["reasoning_effort"] == "high"


def test_an_unknown_effort_vocabulary_is_left_alone() -> None:
    capability = ModelReasoningCapability(
        can_reason=True, supports_effort_control=True, supported_efforts=None
    )
    policy = ReasoningPolicy.on(effort=ReasoningEffort.MAX)
    assert adapt_reasoning_policy(policy, capability) is policy


# ---------------------------------------------------------------------------
# R4 -- toggle-only models.
# ---------------------------------------------------------------------------


def test_toggle_only_model_turns_thinking_on_and_loses_the_level() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH), TOGGLE_ONLY
    )
    assert adapted == ReasoningPolicy.on()
    body = gated_body(
        "zai", ReasoningPolicy.on(effort=ReasoningEffort.HIGH), TOGGLE_ONLY
    )
    assert (
        body["extra_body"]["thinking"]
        == encode("zai", ReasoningPolicy.on())["extra_body"]["thinking"]
    )


def test_toggle_only_model_sends_the_encoders_own_enabled_value() -> None:
    """An effort-only encoder expresses "thinking on" in its own vocabulary.

    groq has no toggle field, so the nearest thing it can express is its
    documented ``enabled_value``. The requested level is still discarded.
    """

    body = gated_body(
        "groq", ReasoningPolicy.on(effort=ReasoningEffort.HIGH), TOGGLE_ONLY
    )
    assert body == encode("groq", ReasoningPolicy.on())
    assert body["reasoning_effort"] != "high"


# ---------------------------------------------------------------------------
# R5 -- budget-only models get the industry ratio formula.
# ---------------------------------------------------------------------------


def test_budget_only_model_synthesises_the_high_ratio_budget() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        BUDGET_ONLY,
        max_tokens=4096,
        output_limit=8192,
    )
    # effective_max = min(4096, 8192) = 4096; 4096 * 0.80 = 3276.8 -> 3276
    assert adapted.budget_tokens == 3276
    body = gated_body(
        "llamacpp",
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        BUDGET_ONLY,
        max_tokens=4096,
        output_limit=8192,
    )
    assert body["extra_body"]["thinking_budget_tokens"] == 3276


def test_budget_only_model_uses_the_smaller_of_max_tokens_and_output_limit() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        BUDGET_ONLY,
        max_tokens=100_000,
        output_limit=8192,
    )
    assert adapted.budget_tokens == int(
        8192 * EFFORT_BUDGET_RATIOS[ReasoningEffort.MEDIUM]
    )


def test_a_synthesised_budget_below_the_floor_is_raised_to_1024() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MINIMAL),
        BUDGET_ONLY,
        max_tokens=2000,
        output_limit=8192,
    )
    # The literal floor, not the constant: a wrong constant must fail here.
    assert adapted.budget_tokens == 1024


def test_a_synthesised_budget_never_exceeds_the_models_output_limit() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        BUDGET_ONLY,
        max_tokens=512,
        output_limit=512,
    )
    assert adapted.budget_tokens == 512


# ---------------------------------------------------------------------------
# R6 -- an explicit client budget on a budget-capable model.
# ---------------------------------------------------------------------------


def test_explicit_budget_within_range_is_sent_unchanged() -> None:
    policy = ReasoningPolicy.on(budget_tokens=4096)
    assert adapt_reasoning_policy(policy, BUDGET_ONLY, output_limit=8192) is policy


def test_explicit_budget_above_the_output_limit_is_capped() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=999_999), BUDGET_ONLY, output_limit=8192
    )
    assert adapted.budget_tokens == 8192


def test_explicit_budget_below_the_floor_is_raised() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=100), BUDGET_ONLY, output_limit=8192
    )
    assert adapted.budget_tokens == 1024
    assert MINIMUM_BUDGET_TOKENS == 1024


def test_explicit_budget_is_untouched_when_the_output_limit_is_unknown() -> None:
    policy = ReasoningPolicy.on(budget_tokens=999_999)
    assert adapt_reasoning_policy(policy, BUDGET_ONLY, output_limit=None) is policy


# ---------------------------------------------------------------------------
# R7 -- explicit budget on an effort-only model.
# ---------------------------------------------------------------------------


def test_explicit_budget_on_an_effort_only_model_becomes_the_nearest_effort() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=2048), EFFORT_ONLY_LOW_MEDIUM_HIGH
    )
    assert adapted.budget_tokens is None
    assert adapted.effort is ReasoningEffort.HIGH
    body = gated_body(
        "groq", ReasoningPolicy.on(budget_tokens=2048), EFFORT_ONLY_LOW_MEDIUM_HIGH
    )
    assert body["reasoning_effort"] == "high"


def test_a_tiny_explicit_budget_becomes_the_models_lowest_effort() -> None:
    adapted = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=16), EFFORT_ONLY_HIGH_MAX
    )
    # minimal is the cheapest effort FCC knows; the model's floor is high.
    assert adapted.effort is ReasoningEffort.HIGH


def test_explicit_budget_is_left_alone_when_budget_support_is_unknown() -> None:
    capability = ModelReasoningCapability(can_reason=True)
    policy = ReasoningPolicy.on(budget_tokens=4096)
    assert adapt_reasoning_policy(policy, capability) is policy


# ---------------------------------------------------------------------------
# R8 -- the encoder keeps ownership of the wire shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_id", REPRESENTATIVE_PROFILES)
def test_gating_never_makes_an_encoder_emit_a_new_field(profile_id: str) -> None:
    """No capability/policy pair may produce a field the encoder never had."""

    baseline: set[str] = set()
    for policy in ALL_POLICIES:
        baseline |= field_paths(encode(profile_id, policy))

    for capability in ALL_CAPABILITIES:
        for policy in ALL_POLICIES:
            for limits in ((4096, 8192), (None, None), (512, 512)):
                emitted = field_paths(
                    gated_body(
                        profile_id,
                        policy,
                        capability,
                        max_tokens=limits[0],
                        output_limit=limits[1],
                    )
                )
                assert emitted <= baseline, (profile_id, capability, policy)


def test_a_budget_only_model_does_not_gain_a_thinking_object_on_an_effort_provider() -> (
    None
):
    """groq speaks reasoning_effort only; a synthesised budget must not appear."""

    body = gated_body(
        "groq", ReasoningPolicy.on(effort=ReasoningEffort.HIGH), BUDGET_ONLY
    )
    assert "thinking" not in body
    assert "extra_body" not in body
    # The effort survives as the nearest expressible fallback.
    assert body["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# R9 -- warnings.
# ---------------------------------------------------------------------------


@pytest.fixture
def warnings_sink() -> Any:
    from loguru import logger

    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(message), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def test_a_clamp_emits_exactly_one_warning(warnings_sink: list[str]) -> None:
    adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        EFFORT_ONLY_LOW_MEDIUM_HIGH,
        model_ref="provider/a-model",
    )
    assert len(warnings_sink) == 1
    assert "REASONING EFFORT CLAMPED" in warnings_sink[0]
    assert "provider/a-model" in warnings_sink[0]


@pytest.mark.parametrize(
    ("policy", "capability"),
    [
        (ReasoningPolicy.on(effort=ReasoningEffort.MAX), UNKNOWN),
        (ReasoningPolicy.on(effort=ReasoningEffort.HIGH), EFFORT_ONLY_LOW_MEDIUM_HIGH),
        (ReasoningPolicy.off(), EFFORT_ONLY_HIGH_MAX),
        (ReasoningPolicy.off(), CANNOT_REASON),
        (ReasoningPolicy.provider_default(), CANNOT_REASON),
        (ReasoningPolicy.on(budget_tokens=4096), BUDGET_ONLY),
    ],
)
def test_nothing_altered_emits_no_warning(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    warnings_sink: list[str],
) -> None:
    adapt_reasoning_policy(policy, capability, output_limit=8192)
    assert warnings_sink == []


def test_a_suppression_warns(warnings_sink: list[str]) -> None:
    adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH), CANNOT_REASON
    )
    assert len(warnings_sink) == 1
    assert "REASONING SUPPRESSED" in warnings_sink[0]


# ---------------------------------------------------------------------------
# The ratio table is a single named constant.
# ---------------------------------------------------------------------------


def test_the_ratio_table_covers_every_effort_and_stays_below_one() -> None:
    assert set(EFFORT_BUDGET_RATIOS) == set(ReasoningEffort)
    assert all(0.0 < ratio < 1.0 for ratio in EFFORT_BUDGET_RATIOS.values())
    assert EFFORT_BUDGET_RATIOS[ReasoningEffort.HIGH] == 0.80
    assert EFFORT_BUDGET_RATIOS[ReasoningEffort.MAX] == 0.95
