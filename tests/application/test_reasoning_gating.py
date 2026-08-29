"""Matrix tests for per-model reasoning gating.

The unit under test is :func:`adapt_reasoning_policy`, but the assertions that
matter are on the *emitted request body*: the policy is only interesting
because of what a provider encoder does with it. Every case therefore runs the
adapted policy through real provider encoders and asserts the exact body.
"""

from typing import Any

import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_budget import EFFORT_BUDGET_RATIOS
from my_claude_code.application.reasoning_gating import adapt_reasoning_policy
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import (
    MINIMUM_BUDGET_TOKENS,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningDialect,
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
# models.dev's ``reasoning: true`` with ``reasoning_options: []`` -- the model
# reasons, the caller gets no knob. 1,223 of its 5,230 reasoning models carry
# it. Every ``False`` here is a stated fact, which is what separates it from
# UNKNOWN.
NO_CONTROL = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=False,
    supports_toggle_control=False,
    supports_budget_control=False,
    supported_efforts=None,
)
ALL_CAPABILITIES = (
    UNKNOWN,
    CANNOT_REASON,
    EFFORT_ONLY_LOW_MEDIUM_HIGH,
    EFFORT_ONLY_HIGH_MAX,
    TOGGLE_ONLY,
    BUDGET_ONLY,
    NO_CONTROL,
)

# The HOST half of the two-fact rule: which reasoning fields a gateway parses.
# One per shape that exists in the fleet, so the matrix below covers every
# combination of "the model has this knob" x "the host has this field".
NO_DIALECT = None
EFFORT_ONLY_HOST = ReasoningDialect(
    effort_values=frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
    ),
    effort_field="reasoning_effort",
)
EFFORT_ALL_HOST = ReasoningDialect(
    effort_values=frozenset(ReasoningEffort),
    off=True,
    effort_field="reasoning_effort",
)
TOGGLE_HOST = ReasoningDialect(toggle=True, off=True, toggle_field="thinking")
BUDGET_HOST = ReasoningDialect(
    budget=True, off=True, budget_field="thinking.budget_tokens"
)
EFFORT_AND_TOGGLE_HOST = ReasoningDialect(
    effort_values=frozenset(ReasoningEffort),
    toggle=True,
    budget=True,
    off=True,
    effort_field="reasoning.effort",
    toggle_field="reasoning.enabled",
    budget_field="reasoning.max_tokens",
)
# Command Code's shape: an effort field whose "on" value is one of its own
# rungs. That is a default rung, not an on/off channel.
EFFORT_HOST_WITH_DEFAULT_RUNG = ReasoningDialect(
    effort_values=frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
    ),
    toggle=True,
    effort_field="reasoning_effort",
    toggle_field="reasoning_effort",
)
NO_CONTROL_HOST = ReasoningDialect()
ALL_DIALECTS = (
    NO_DIALECT,
    EFFORT_ONLY_HOST,
    EFFORT_ALL_HOST,
    TOGGLE_HOST,
    BUDGET_HOST,
    EFFORT_AND_TOGGLE_HOST,
    EFFORT_HOST_WITH_DEFAULT_RUNG,
    NO_CONTROL_HOST,
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
    adapted, _adaptation = adapt_reasoning_policy(
        policy,
        capability,
        max_tokens=max_tokens,
        output_limit=output_limit,
        model_ref="provider/a-model",
    )
    return encode(profile_id, adapted)


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

    assert adapt_reasoning_policy(policy, None)[0] is policy
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
    # On an allowance that small the answer floor -- half of it -- binds before
    # the 0.95 ratio does, so thinking and answer split it evenly.
    assert routed.reasoning.budget_tokens == 4096 // 2


# ---------------------------------------------------------------------------
# R2 -- an explicit OFF is never rewritten, whatever the model can do.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", ALL_CAPABILITIES)
@pytest.mark.parametrize("profile_id", REPRESENTATIVE_PROFILES)
def test_policy_off_is_unchanged_in_every_capability_shape(
    capability: ModelReasoningCapability | None, profile_id: str
) -> None:
    off = ReasoningPolicy.off()
    assert adapt_reasoning_policy(off, capability)[0] is off
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
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=9000), CANNOT_REASON
    )
    assert adapted == ReasoningPolicy.provider_default()


# ---------------------------------------------------------------------------
# R3 -- effort clamping.
# ---------------------------------------------------------------------------


def test_supported_effort_is_sent_unchanged() -> None:
    policy = ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    adapted, _adaptation = adapt_reasoning_policy(policy, EFFORT_ONLY_LOW_MEDIUM_HIGH)
    assert adapted is policy
    assert (
        gated_body("groq", policy, EFFORT_ONLY_LOW_MEDIUM_HIGH)["reasoning_effort"]
        == "medium"
    )


@pytest.mark.parametrize("requested", [ReasoningEffort.XHIGH, ReasoningEffort.MAX])
def test_effort_above_the_models_vocabulary_clamps_down_to_high(
    requested: ReasoningEffort,
) -> None:
    adapted, _adaptation = adapt_reasoning_policy(
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
    adapted, _adaptation = adapt_reasoning_policy(
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
    assert adapt_reasoning_policy(policy, capability)[0] is policy


# ---------------------------------------------------------------------------
# R4 -- toggle-only models.
# ---------------------------------------------------------------------------


def test_toggle_only_model_turns_thinking_on_and_loses_the_level() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
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


def test_a_toggle_only_model_on_an_effort_only_host_sends_nothing() -> None:
    """The defect this PR exists for, stated as a test.

    groq has no on/off field: its ``enabled_value`` is written into the very
    ``reasoning_effort`` field the model has no knob for. Sending it answers a
    request for one level with the host's own default level, on a model that
    cannot read either. Nothing at all is the honest wire, and the model's own
    default reasoning behaviour stands.
    """

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.LOW),
        TOGGLE_ONLY,
        dialect=EFFORT_HOST_WITH_DEFAULT_RUNG,
        model_ref="commandcode/a-model",
    )

    assert adapted == ReasoningPolicy.provider_default()
    assert adaptation.kind is ReasoningAdaptationKind.DROPPED
    assert adaptation.message is not None
    assert "no on/off field" in adaptation.message
    assert encode("groq", adapted) == {"model": "a-model", "messages": []}


def test_effort_model_on_effort_host_sends_the_users_rung_not_the_hosts_default() -> (
    None
):
    """And an effort the host cannot spell clamps to the nearest rung it can.

    ``max`` against ``low/medium/high`` is ``high`` -- the strongest rung at or
    below the ask -- never the encoder's ``enabled_value``, which is where a
    request for ``max`` used to leave as groq's ``medium``.
    """

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        EFFORT_ONLY_LOW_MEDIUM_HIGH,
        dialect=EFFORT_ONLY_HOST,
        model_ref="groq/a-model",
    )

    assert adapted == ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    assert adaptation.kind is ReasoningAdaptationKind.CLAMPED
    assert encode("groq", adapted)["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# The two-fact rule -- model capability x host dialect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", ALL_CAPABILITIES)
@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_unknown_dialect_leaves_gating_byte_identical(
    capability: ModelReasoningCapability | None, policy: ReasoningPolicy
) -> None:
    """An absent dialect must change nothing, for every cell of the matrix.

    Most providers declare none, so this is the guarantee that shipping the
    two-fact rule cannot regress them.
    """

    with_dialect, with_adaptation = adapt_reasoning_policy(
        policy, capability, dialect=None, model_ref="provider/a-model"
    )
    without = adapt_reasoning_policy(policy, capability, model_ref="provider/a-model")

    assert with_dialect == without[0]
    assert with_adaptation == without[1]


@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_unknown_capability_with_known_dialect_only_narrows(
    dialect: ReasoningDialect | None, policy: ReasoningPolicy
) -> None:
    """Nothing is known about the model, so only the host may narrow.

    In particular nothing is ever *added*: an unknown model never acquires a
    control it was not asked for, and an OFF is never rewritten, because only a
    stated ``mandatory`` may do that.
    """

    adapted, _adaptation = adapt_reasoning_policy(
        policy, None, dialect=dialect, model_ref="provider/a-model"
    )

    if policy.control is ReasoningControl.OFF:
        assert adapted == policy
    if not policy.requests_reasoning:
        assert adapted == policy
    if adapted.effort is not None and dialect is not None:
        assert dialect.effort_values is None or adapted.effort in dialect.effort_values


def test_tier_one_supported_parameters_lets_a_toggle_model_take_an_effort() -> None:
    """The gateway's own per-model statement outranks the cross-provider vote.

    ``nous_portal`` publishes ``reasoning_effort`` in ``supported_parameters``
    for ``tencent/hy3:free`` and not for ``meituan/longcat-2.0:free``. That is
    a tier-1 fact about one model, and it turns the model's capability record
    into one that has an effort knob -- so the effort is sent rather than
    discarded, even though the cross-provider vote calls the model toggle-only.
    """

    stated_by_the_gateway = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supports_toggle_control=True,
        supports_budget_control=None,
    )

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        stated_by_the_gateway,
        dialect=EFFORT_ONLY_HOST,
        model_ref="nous_portal/tencent/hy3:free",
    )

    assert adapted == ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    assert adaptation.kind is ReasoningAdaptationKind.UNCHANGED


def test_mandatory_off_floor_comes_from_the_intersection() -> None:
    """A floor only the model knows is no more sendable than the OFF was."""

    mandatory_low_to_max = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset(
            {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.MAX}
        ),
        mandatory=True,
    )
    host_without_low = ReasoningDialect(
        effort_values=frozenset({ReasoningEffort.MEDIUM, ReasoningEffort.MAX}),
        effort_field="reasoning_effort",
    )

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.off(),
        mandatory_low_to_max,
        dialect=host_without_low,
        model_ref="provider/a-model",
    )

    assert adapted == ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    assert adaptation.kind is ReasoningAdaptationKind.SUBSTITUTED


@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("capability", ALL_CAPABILITIES)
@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_every_adaptation_message_names_the_field_or_says_nothing_is_sent(
    dialect: ReasoningDialect | None,
    capability: ModelReasoningCapability | None,
    policy: ReasoningPolicy,
) -> None:
    """An operator reading the request log must learn what actually went out.

    Only checked where a dialect is known: with no dialect there is no field
    name to give, and the messages there are deliberately unchanged.
    """

    _adapted, adaptation = adapt_reasoning_policy(
        policy, capability, dialect=dialect, model_ref="provider/a-model"
    )
    if adaptation.kind is ReasoningAdaptationKind.UNCHANGED or dialect is None:
        return

    message = adaptation.message
    assert message is not None
    fields = {
        name
        for name in (dialect.effort_field, dialect.toggle_field, dialect.budget_field)
        if name
    }
    says_nothing = (
        "sending no reasoning instruction" in message
        or "dropping the requested reasoning controls" in message
        or "adaptive thinking" in message
        or "will be sent as a thinking budget" in message
    )
    assert says_nothing or any(f"`{name}`" in message for name in fields), message


# ---------------------------------------------------------------------------
# R5 -- budget-only models get the industry ratio formula.
# ---------------------------------------------------------------------------


def test_budget_only_model_synthesises_the_high_ratio_budget() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        BUDGET_ONLY,
        max_tokens=100_000,
        output_limit=200_000,
    )
    # effective_max = min(100_000, 200_000) = 100_000; 100_000 * 0.80 = 80_000,
    # which still leaves 20_000 for the answer -- more than the 16,384 floor --
    # so the published ratio is what binds here, not the reconciliation.
    assert adapted.budget_tokens == 80_000
    body = gated_body(
        "llamacpp",
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        BUDGET_ONLY,
        max_tokens=100_000,
        output_limit=200_000,
    )
    assert body["extra_body"]["thinking_budget_tokens"] == 80_000


def test_budget_only_model_uses_the_smaller_of_max_tokens_and_output_limit() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        BUDGET_ONLY,
        max_tokens=100_000,
        output_limit=8192,
    )
    assert adapted.budget_tokens == int(
        8192 * EFFORT_BUDGET_RATIOS[ReasoningEffort.MEDIUM]
    )


def test_a_synthesised_budget_below_the_floor_is_raised_to_1024() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MINIMAL),
        BUDGET_ONLY,
        max_tokens=2000,
        output_limit=8192,
    )
    # The literal floor, not the constant: a wrong constant must fail here.
    assert adapted.budget_tokens == 1024


def test_a_synthesised_budget_never_exceeds_the_models_output_limit() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        BUDGET_ONLY,
        max_tokens=512,
        output_limit=512,
    )
    # Strictly less, never equal: a budget equal to max_tokens is a body
    # Anthropic rejects, and 512 was what this used to produce.
    assert adapted.budget_tokens == 511


# ---------------------------------------------------------------------------
# R6 -- an explicit client budget on a budget-capable model.
# ---------------------------------------------------------------------------


def test_explicit_budget_within_range_is_sent_unchanged() -> None:
    policy = ReasoningPolicy.on(budget_tokens=4096)
    assert adapt_reasoning_policy(policy, BUDGET_ONLY, output_limit=8192)[0] is policy


def test_explicit_budget_above_the_output_limit_is_capped() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=999_999), BUDGET_ONLY, output_limit=8192
    )
    # Capped at the allowance, not at the limit: the answer keeps half of an
    # 8,192-token output (the proportional floor), so 4,096 is left to think in.
    assert adapted.budget_tokens == 4096


def test_explicit_budget_below_the_floor_is_raised() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=100), BUDGET_ONLY, output_limit=8192
    )
    assert adapted.budget_tokens == 1024
    assert MINIMUM_BUDGET_TOKENS == 1024


def test_explicit_budget_is_untouched_when_the_output_limit_is_unknown() -> None:
    policy = ReasoningPolicy.on(budget_tokens=999_999)
    assert adapt_reasoning_policy(policy, BUDGET_ONLY, output_limit=None)[0] is policy


# ---------------------------------------------------------------------------
# R7 -- explicit budget on an effort-only model.
# ---------------------------------------------------------------------------


def test_explicit_budget_on_an_effort_only_model_becomes_the_nearest_effort() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=2048), EFFORT_ONLY_LOW_MEDIUM_HIGH
    )
    assert adapted.budget_tokens is None
    assert adapted.effort is ReasoningEffort.HIGH
    body = gated_body(
        "groq", ReasoningPolicy.on(budget_tokens=2048), EFFORT_ONLY_LOW_MEDIUM_HIGH
    )
    assert body["reasoning_effort"] == "high"


def test_a_tiny_explicit_budget_becomes_the_models_lowest_effort() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=16), EFFORT_ONLY_HIGH_MAX
    )
    # minimal is the cheapest effort FCC knows; the model's floor is high.
    assert adapted.effort is ReasoningEffort.HIGH


def test_explicit_budget_is_left_alone_when_budget_support_is_unknown() -> None:
    capability = ModelReasoningCapability(can_reason=True)
    policy = ReasoningPolicy.on(budget_tokens=4096)
    assert adapt_reasoning_policy(policy, capability)[0] is policy


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
# R10 -- models that cannot disable thinking (mandatory=True).
# ---------------------------------------------------------------------------


def test_mandatory_model_rewrites_an_off_request_to_its_lowest_effort() -> None:
    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.HIGH, ReasoningEffort.MAX}),
        mandatory=True,
    )
    adapted, adaptation = adapt_reasoning_policy(ReasoningPolicy.off(), capability)

    assert adapted == ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    assert adaptation.kind is ReasoningAdaptationKind.SUBSTITUTED
    assert adaptation.message is not None
    assert "cannot run with thinking" in adaptation.message


def test_mandatory_model_without_a_vocabulary_gets_adaptive() -> None:
    """No effort vocabulary known: adaptive lets the model pick its own floor."""

    capability = ModelReasoningCapability(can_reason=True, mandatory=True)
    adapted, adaptation = adapt_reasoning_policy(ReasoningPolicy.off(), capability)

    assert adapted.control is ReasoningControl.ADAPTIVE
    assert adaptation.kind is ReasoningAdaptationKind.SUBSTITUTED


def test_an_unknown_mandatory_flag_leaves_off_alone() -> None:
    """``mandatory=None`` must never rewrite an OFF request.

    Every capability in ALL_CAPABILITIES carries the default ``None``, so the
    existing OFF matrix above already proves the common shape; this asserts
    it explicitly for the effort-control capability.
    """

    off = ReasoningPolicy.off()
    assert adapt_reasoning_policy(off, EFFORT_ONLY_LOW_MEDIUM_HIGH)[0] is off


def test_a_mandatory_off_rewrite_warns(warnings_sink: list[str]) -> None:
    capability = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.HIGH}),
        mandatory=True,
    )
    adapt_reasoning_policy(ReasoningPolicy.off(), capability)

    assert len(warnings_sink) == 1
    assert "REASONING OFF SUBSTITUTED" in warnings_sink[0]


# ---------------------------------------------------------------------------
# The ratio table is a single named constant.
# ---------------------------------------------------------------------------


def test_the_ratio_table_covers_every_effort_and_stays_below_one() -> None:
    assert set(EFFORT_BUDGET_RATIOS) == set(ReasoningEffort)
    assert all(0.0 < ratio < 1.0 for ratio in EFFORT_BUDGET_RATIOS.values())
    assert EFFORT_BUDGET_RATIOS[ReasoningEffort.HIGH] == 0.80
    assert EFFORT_BUDGET_RATIOS[ReasoningEffort.MAX] == 0.95


# ---------------------------------------------------------------------------
# R8 -- ``reasoning_options: []``: the model reasons and takes no control.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", tuple(ReasoningEffort))
def test_a_model_with_no_control_gets_thinking_on_and_no_level(
    effort: ReasoningEffort,
) -> None:
    """Every effort collapses to a bare "on"; none of them reaches the wire.

    Before this branch existed, all three guards below were skipped -- effort
    control False, budget control False, toggle control False -- and the raw
    effort fell through the final ``UNCHANGED`` return into a field the model
    does not have.
    """

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=effort), NO_CONTROL, model_ref="provider/a-model"
    )

    assert adapted == ReasoningPolicy.on()
    assert adapted.effort is None
    assert adapted.budget_tokens is None
    assert adapted.numeric_budget_tokens is None
    assert adaptation.kind is ReasoningAdaptationKind.DROPPED
    assert adaptation.message is not None
    assert effort.value in adaptation.message


def test_a_client_budget_on_a_no_control_model_is_dropped_too() -> None:
    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=4096),
        NO_CONTROL,
        output_limit=230_400,
        model_ref="provider/a-model",
    )

    assert adapted == ReasoningPolicy.on()
    assert adaptation.kind is ReasoningAdaptationKind.DROPPED


def test_a_bare_on_against_a_no_control_model_is_not_an_adaptation() -> None:
    on = ReasoningPolicy.on()
    adapted, adaptation = adapt_reasoning_policy(on, NO_CONTROL)

    assert adapted == on
    assert adaptation.kind is ReasoningAdaptationKind.UNCHANGED


@pytest.mark.parametrize("effort", tuple(ReasoningEffort))
def test_no_control_and_unknown_do_not_collapse_into_each_other(
    effort: ReasoningEffort,
) -> None:
    """The three-state distinction the parser preserves has to survive gating.

    ``None`` on all three flags means nobody said anything and must change
    nothing; ``False`` on all three is a statement and must drop the level.
    """

    policy = ReasoningPolicy.on(effort=effort)
    unknown_flags = ModelReasoningCapability(can_reason=True)

    assert adapt_reasoning_policy(policy, unknown_flags)[0] == policy
    assert adapt_reasoning_policy(policy, NO_CONTROL)[0] == ReasoningPolicy.on()


@pytest.mark.parametrize("profile_id", REPRESENTATIVE_PROFILES)
def test_a_no_control_model_never_receives_an_effort_level(profile_id: str) -> None:
    body = gated_body(
        profile_id, ReasoningPolicy.on(effort=ReasoningEffort.MINIMAL), NO_CONTROL
    )
    assert body == encode(profile_id, ReasoningPolicy.on())


# ---------------------------------------------------------------------------
# R9 -- ``mandatory``: a model that cannot run with thinking disabled.
# ---------------------------------------------------------------------------


MANDATORY_WITH_VOCABULARY = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supports_toggle_control=False,
    supports_budget_control=False,
    supported_efforts=frozenset(
        {ReasoningEffort.MEDIUM, ReasoningEffort.HIGH, ReasoningEffort.MAX}
    ),
    mandatory=True,
)
MANDATORY_WITHOUT_VOCABULARY = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=False,
    supports_toggle_control=True,
    supports_budget_control=False,
    supported_efforts=None,
    mandatory=True,
)


def test_off_against_a_mandatory_model_becomes_its_lowest_effort() -> None:
    """Live shape: z-ai/glm-5.3-flash publishes ``reasoning.mandatory: true``.

    OFF would be refused by the model, so the floor is the nearest honest
    thing -- never left OFF, and never dropped to "no opinion" either.
    """

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.off(),
        MANDATORY_WITH_VOCABULARY,
        model_ref="nous_portal/z-ai/glm-5.3-flash",
    )

    assert adapted == ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM)
    assert adapted.control is not ReasoningControl.OFF
    assert adaptation.kind is ReasoningAdaptationKind.SUBSTITUTED
    assert adaptation.message is not None
    assert "cannot run with thinking disabled" in adaptation.message


def test_off_against_a_mandatory_model_without_a_vocabulary_goes_adaptive() -> None:
    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.off(), MANDATORY_WITHOUT_VOCABULARY, model_ref="p/m"
    )

    assert adapted == ReasoningPolicy.adaptive()
    assert adapted.control is ReasoningControl.ADAPTIVE
    assert adaptation.kind is ReasoningAdaptationKind.SUBSTITUTED


@pytest.mark.parametrize("profile_id", REPRESENTATIVE_PROFILES)
def test_a_mandatory_model_never_sees_a_disabled_wire_field(profile_id: str) -> None:
    """The point of the rewrite: no encoder may emit its "off" shape."""

    gated = gated_body(profile_id, ReasoningPolicy.off(), MANDATORY_WITH_VOCABULARY)
    off_body = encode(profile_id, ReasoningPolicy.off())
    floor_body = encode(profile_id, ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM))

    assert gated == floor_body
    # ``minimax`` (a constant body) and ``xai`` (no reasoning field at all)
    # have no disabled shape to have emitted; every other encoder here does.
    if off_body != floor_body:
        assert gated != off_body


def test_mandatory_is_ignored_when_it_is_unknown() -> None:
    """``None`` is not ``False``; an unstated flag must not rewrite anything."""

    off = ReasoningPolicy.off()
    unstated = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.HIGH}),
    )
    assert adapt_reasoning_policy(off, unstated)[0] is off


# ---------------------------------------------------------------------------
# R10 -- a client budget is clamped wherever the output limit is known.
# ---------------------------------------------------------------------------


def test_a_client_budget_is_clamped_when_budget_control_is_unknown() -> None:
    """``supports_budget_control`` is ``None`` for most models.

    Knowing what the model can emit is already enough to know the budget cannot
    exceed it, so the clamp must not wait for the control flag.
    """

    unknown_control = ModelReasoningCapability(can_reason=True)
    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(budget_tokens=999_999),
        unknown_control,
        output_limit=16_384,
        model_ref="nvidia_nim/minimaxai/minimax-m3",
    )

    assert adapted.budget_tokens == 8192
    assert adaptation.kind is ReasoningAdaptationKind.CLAMPED


def test_a_client_budget_stays_untouched_when_the_output_limit_is_unknown() -> None:
    """Unknown on both counts still has to change nothing at all."""

    policy = ReasoningPolicy.on(budget_tokens=999_999)
    unknown_control = ModelReasoningCapability(can_reason=True)
    assert (
        adapt_reasoning_policy(policy, unknown_control, output_limit=None)[0] is policy
    )


# ---------------------------------------------------------------------------
# R11 -- an effort is priced against the model, not against a flat table.
# ---------------------------------------------------------------------------


def test_the_same_effort_costs_different_tokens_on_different_models() -> None:
    """Live numbers: glm-5.2:free 230,400 vs minimax-m3 16,384."""

    def budget(output_limit: int) -> int | None:
        adapted, _ = adapt_reasoning_policy(
            ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
            BUDGET_ONLY,
            max_tokens=None,
            output_limit=output_limit,
        )
        return adapted.budget_tokens

    large = budget(230_400)
    small = budget(16_384)

    assert large is not None and small is not None
    assert large != small
    assert large > small
    # The flat table would have answered 2,048 for both.
    assert 2048 not in {large, small}
