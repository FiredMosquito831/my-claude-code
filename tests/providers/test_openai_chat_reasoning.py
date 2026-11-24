from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.openai_chat.reasoning import (
    ChatTemplateReasoning,
    LlamaCppReasoning,
    NamedEffortReasoning,
    ReasoningObject,
    SplitReasoningOutput,
    ThinkingObjectReasoning,
)

_EFFORTS = (
    (ReasoningEffort.LOW, "low"),
    (ReasoningEffort.HIGH, "high"),
)


def test_named_effort_encoder_translates_only_documented_values() -> None:
    body: dict = {}
    encoder = NamedEffortReasoning(
        _EFFORTS,
        disabled_value="none",
        enabled_value="high",
    )

    encoder.encode(body, ReasoningPolicy.on(effort=ReasoningEffort.HIGH))

    assert body == {"reasoning_effort": "high"}


def test_named_effort_encoder_uses_exact_budget_only_with_budget_field() -> None:
    unsupported_body: dict = {}
    supported_body: dict = {}
    policy = ReasoningPolicy.on(budget_tokens=2048)

    NamedEffortReasoning(_EFFORTS, enabled_value="high").encode(
        unsupported_body, policy
    )
    NamedEffortReasoning(
        _EFFORTS,
        enabled_value="high",
        budget_field="reasoning_tokens",
    ).encode(supported_body, policy)

    assert unsupported_body == {"reasoning_effort": "high"}
    assert supported_body == {"reasoning_tokens": 2048}


def test_reasoning_object_keeps_effort_budget_and_disable_shapes_exclusive() -> None:
    effort_body: dict = {}
    budget_body: dict = {}
    off_body: dict = {}
    encoder = ReasoningObject(_EFFORTS)

    encoder.encode(
        effort_body,
        ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
    )
    encoder.encode(budget_body, ReasoningPolicy.on(budget_tokens=512))
    encoder.encode(off_body, ReasoningPolicy.off())

    assert effort_body == {"extra_body": {"reasoning": {"effort": "high"}}}
    assert budget_body == {"extra_body": {"reasoning": {"max_tokens": 512}}}
    assert off_body == {"extra_body": {"reasoning": {"enabled": False}}}


def test_thinking_object_leaves_provider_default_unmodified() -> None:
    body: dict = {}
    encoder = ThinkingObjectReasoning(
        enabled={"type": "enabled"},
        disabled={"type": "disabled"},
    )

    encoder.encode(body, ReasoningPolicy.provider_default())

    assert body == {}


def test_chat_template_encoder_maps_named_effort_to_boolean_capability() -> None:
    body: dict = {}

    ChatTemplateReasoning().encode(
        body,
        ReasoningPolicy(
            effort=ReasoningEffort.MEDIUM,
        ),
    )

    assert body == {"extra_body": {"chat_template_kwargs": {"thinking": True}}}


def test_llamacpp_encoder_maps_effort_preserves_exact_budget_and_disables() -> None:
    effort_body: dict = {}
    budget_body: dict = {}
    off_body: dict = {}
    encoder = LlamaCppReasoning()

    encoder.encode(effort_body, ReasoningPolicy.on(effort=ReasoningEffort.HIGH))
    encoder.encode(budget_body, ReasoningPolicy.on(budget_tokens=256))
    encoder.encode(off_body, ReasoningPolicy.off())

    assert effort_body == {"extra_body": {"thinking_budget_tokens": 2048}}
    assert budget_body == {"extra_body": {"thinking_budget_tokens": 256}}
    assert off_body == {"extra_body": {"thinking_budget_tokens": 0}}


def test_split_reasoning_output_does_not_invent_compute_control() -> None:
    off_body: dict = {}
    on_body: dict = {}

    SplitReasoningOutput().encode(off_body, ReasoningPolicy.off())
    SplitReasoningOutput().encode(on_body, ReasoningPolicy.on())

    assert off_body == {}
    assert on_body == {"extra_body": {"reasoning_split": True}}


def test_adaptive_off_prefers_enabled_value_before_high_clamp(monkeypatch) -> None:
    """Fix 3A: REASONING_ADAPTIVE='off' + ADAPTIVE(None) honors enabled_value != 'high'."""
    from my_claude_code.config.settings import get_settings
    from my_claude_code.providers.openai_chat import profiles as chat_profiles

    monkeypatch.setenv("REASONING_ADAPTIVE", "off")
    get_settings.cache_clear()
    try:
        encoder = NamedEffortReasoning(
            (
                (ReasoningEffort.LOW, "low"),
                (ReasoningEffort.MEDIUM, "medium"),
                (ReasoningEffort.HIGH, "high"),
                (ReasoningEffort.MAX, "max"),
            ),
            enabled_value="max",
        )
        body: dict = {}
        encoder.encode(body, ReasoningPolicy.adaptive())
        assert body == {"reasoning_effort": "max"}

        # Novita/Nebius/Together-style medium enabled_value when high is in efforts
        medium_encoder = NamedEffortReasoning(
            (
                (ReasoningEffort.LOW, "low"),
                (ReasoningEffort.MEDIUM, "medium"),
                (ReasoningEffort.HIGH, "high"),
            ),
            enabled_value="medium",
        )
        medium_body: dict = {}
        medium_encoder.encode(medium_body, ReasoningPolicy.adaptive())
        assert medium_body == {"reasoning_effort": "medium"}

        # Host with enabled_value=None still falls back to clamped HIGH
        generic_body: dict = {}
        chat_profiles.GENERIC_OPENAI_PROFILE.reasoning.encode(
            generic_body, ReasoningPolicy.adaptive()
        )
        assert generic_body == {"reasoning_effort": "high"}
    finally:
        monkeypatch.delenv("REASONING_ADAPTIVE", raising=False)
        get_settings.cache_clear()
