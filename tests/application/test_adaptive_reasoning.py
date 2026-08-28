"""Explicit adaptive extended thinking as a per-tier reasoning choice.

``adaptive`` is Anthropic's own "let the model decide how hard to think" mode.
This module proves three things: selecting it reaches the Anthropic Messages
wire as ``thinking {"type": "adaptive"}``; every other provider degrades to its
own default instead of inventing a field; and *not* selecting it changes
nothing at all for anybody.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning import resolve_reasoning_policy
from my_claude_code.application.reasoning_gating import adapt_reasoning_policy
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.admin.spec import ConfigOptionSpec
from my_claude_code.config.reasoning import (
    ROOT_REASONING_PREFERENCES,
    ROUTE_REASONING_PREFERENCES,
    ReasoningPreference,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.google_openai.reasoning import (
    VertexReasoningEncoder,
)
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

# One profile per distinct encoder shape, mirroring the gating matrix.
REPRESENTATIVE_PROFILES = (
    "groq",
    "vercel",
    "zai",
    "fireworks",
    "llamacpp",
    "featherless",
    "minimax",
    "xai",
)

# Every preference that existed before adaptive was added. The regression
# guard below is pinned to exactly these.
PRE_ADAPTIVE_PREFERENCES = ("off", "client", "low", "medium", "high", "xhigh", "max")

CLIENT_REQUESTS = {
    "bare": {},
    "client_effort_high": {"output_config": {"effort": "high"}},
    "client_thinking_budget": {"thinking": {"type": "enabled", "budget_tokens": 4096}},
    "client_thinking_adaptive": {"thinking": {"type": "adaptive"}},
    "client_thinking_disabled": {"thinking": {"type": "disabled"}},
}

BASELINE_PATH = Path(__file__).with_name("reasoning_request_baseline.json")
BASELINE: dict[str, dict[str, Any]] = json.loads(
    BASELINE_PATH.read_text(encoding="utf-8")
)

CANNOT_REASON = ModelReasoningCapability(can_reason=False)
EFFORT_ONLY = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supports_toggle_control=False,
    supports_budget_control=False,
    supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
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


def _request(**overrides: Any) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "provider/model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return MessagesRequest.model_validate(payload)


def _trim(body: dict[str, Any]) -> dict[str, Any]:
    """Drop the fields that carry no reasoning intent."""

    return {
        key: value
        for key, value in body.items()
        if key not in {"model", "messages", "stream"}
    }


def _openai_body(profile_id: str, policy: ReasoningPolicy) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "a-model", "messages": []}
    OPENAI_CHAT_PROFILES[profile_id].reasoning.encode(body, policy)
    return body


def _google_body(label: str, policy: ReasoningPolicy) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "a-model", "messages": []}
    VertexReasoningEncoder().encode(body, policy)
    return body


NON_ANTHROPIC_ENCODERS = (
    *((profile_id, _openai_body) for profile_id in REPRESENTATIVE_PROFILES),
    ("vertex", _google_body),
)


# ---------------------------------------------------------------------------
# Settings -> resolved policy
# ---------------------------------------------------------------------------


def _router_settings(**overrides: Any) -> Settings:
    settings = Settings()
    settings.model = "anthropic/claude-x"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.reasoning_policy = ReasoningPreference.CLIENT
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def test_a_tier_set_to_adaptive_resolves_to_an_adaptive_policy() -> None:
    """End to end: a route override of "adaptive" reaches the resolved policy."""

    settings = _router_settings(reasoning_opus=ReasoningPreference.ADAPTIVE)
    router = ModelRouter(settings)

    resolved = router.resolve("claude-3-opus")
    assert resolved.reasoning_preference is ReasoningPreference.ADAPTIVE

    policy = resolve_reasoning_policy(_request(), resolved.reasoning_preference)
    assert policy == ReasoningPolicy.adaptive()
    assert policy.control is ReasoningControl.ADAPTIVE
    assert policy.effort is None
    assert policy.budget_tokens is None


def test_the_root_policy_may_also_be_set_to_adaptive() -> None:
    settings = _router_settings(reasoning_policy=ReasoningPreference.ADAPTIVE)

    assert (
        ModelRouter(settings).resolve("claude-3-haiku").reasoning_preference
        is ReasoningPreference.ADAPTIVE
    )


def test_adaptive_is_offered_at_both_the_root_and_the_route_level() -> None:
    assert ReasoningPreference.ADAPTIVE in ROOT_REASONING_PREFERENCES
    assert ReasoningPreference.ADAPTIVE in ROUTE_REASONING_PREFERENCES


# ---------------------------------------------------------------------------
# Provider translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(CLIENT_REQUESTS))
def test_anthropic_messages_sends_adaptive_thinking(shape: str) -> None:
    """The one provider that has an adaptive channel uses it, whatever the client sent."""

    request = _request(**CLIENT_REQUESTS[shape])
    body = build_anthropic_messages_body(request, reasoning=ReasoningPolicy.adaptive())

    assert body["thinking"] == {"type": "adaptive"}
    # Adaptive carries no budget, so max_tokens is never bumped for it.
    assert body["max_tokens"] == 4096


@pytest.mark.parametrize("encoder", NON_ANTHROPIC_ENCODERS, ids=lambda item: item[0])
def test_non_anthropic_providers_emit_no_new_field_for_adaptive(
    encoder: tuple[str, Any],
) -> None:
    """Adaptive is an Anthropic concept: everyone else sends their own default."""

    label, build = encoder
    adaptive = build(label, ReasoningPolicy.adaptive())
    provider_default = build(label, ReasoningPolicy.provider_default())

    assert adaptive == provider_default


# ---------------------------------------------------------------------------
# The regression guard: nothing changes for anybody who does not select it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preference", PRE_ADAPTIVE_PREFERENCES)
@pytest.mark.parametrize("shape", sorted(CLIENT_REQUESTS))
def test_every_pre_adaptive_preference_is_byte_identical(
    preference: str, shape: str
) -> None:
    """Snapshot captured from the commit before adaptive existed.

    ``reasoning_request_baseline.json`` was generated by running the *previous*
    source tree over this exact matrix. If any cell moves, this PR changed
    behaviour for a configuration that did not ask for adaptive.
    """

    overrides = CLIENT_REQUESTS[shape]
    request = _request(**overrides)
    policy = resolve_reasoning_policy(request, ReasoningPreference(preference))

    produced: dict[str, dict[str, Any]] = {
        "anthropic_messages": _trim(
            build_anthropic_messages_body(request, reasoning=policy)
        )
    }
    for profile_id in REPRESENTATIVE_PROFILES:
        produced[profile_id] = _trim(_openai_body(profile_id, policy))
    for label in ("vertex",):
        produced[label] = _trim(_google_body(label, policy))

    for label, body in produced.items():
        assert body == BASELINE[f"{label}|{shape}|{preference}"], label


def test_anthropic_still_defaults_to_adaptive_without_an_adaptive_tier() -> None:
    """The pre-existing implicit default must survive untouched."""

    body = build_anthropic_messages_body(
        _request(), reasoning=ReasoningPolicy.on(effort=None)
    )

    assert body["thinking"] == {"type": "adaptive"}


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


def test_a_model_known_not_to_reason_suppresses_an_adaptive_tier() -> None:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.adaptive(), CANNOT_REASON, model_ref="provider/no-thinking"
    )

    assert adapted == ReasoningPolicy.provider_default()
    assert (
        build_anthropic_messages_body(_request(), reasoning=adapted).get("thinking")
        is None
    )


def test_unknown_capability_leaves_an_adaptive_policy_untouched() -> None:
    policy = ReasoningPolicy.adaptive()

    assert adapt_reasoning_policy(policy, None)[0] is policy


@pytest.mark.parametrize("capability", [EFFORT_ONLY, TOGGLE_ONLY, BUDGET_ONLY])
def test_adaptive_is_never_translated_into_a_fabricated_control(
    capability: ModelReasoningCapability,
) -> None:
    """An effort-, toggle-, or budget-only model gets the provider default.

    Adaptive names no level, so there is nothing to clamp and nothing honest to
    substitute; inventing an effort here would send a request the user never
    asked for.
    """

    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.adaptive(),
        capability,
        max_tokens=4096,
        output_limit=8192,
        model_ref="provider/a-model",
    )

    assert adapted == ReasoningPolicy.adaptive()
    for profile_id in REPRESENTATIVE_PROFILES:
        assert _openai_body(profile_id, adapted) == _openai_body(
            profile_id, ReasoningPolicy.provider_default()
        )


# ---------------------------------------------------------------------------
# The admin surface
#
# The request log's own rendering of an adaptive policy is asserted end to end
# in tests/api/test_reasoning_recording.py.
# ---------------------------------------------------------------------------


def test_the_manifest_exposes_adaptive_with_a_label() -> None:
    for key, expected in (
        ("REASONING_POLICY", ROOT_REASONING_PREFERENCES),
        ("REASONING_FABLE", ROUTE_REASONING_PREFERENCES),
        ("REASONING_OPUS", ROUTE_REASONING_PREFERENCES),
        ("REASONING_SONNET", ROUTE_REASONING_PREFERENCES),
        ("REASONING_HAIKU", ROUTE_REASONING_PREFERENCES),
    ):
        field = next(item for item in FIELDS if item.key == key)
        options = [
            option for option in field.options if isinstance(option, ConfigOptionSpec)
        ]
        assert len(options) == len(field.options)
        # The dropdown must match the enum exactly -- no dropped members.
        assert tuple(option.value for option in options) == tuple(
            preference.value for preference in expected
        )
        adaptive = next(option for option in options if option.value == "adaptive")
        assert adaptive.label == "Adaptive"
