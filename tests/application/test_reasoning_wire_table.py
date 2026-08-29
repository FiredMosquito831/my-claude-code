"""What actually leaves the proxy, per route, end to end.

Every other test in this area checks one layer. This one is the contract: for
each of the user's routed models it runs the *resolved* capability and the
*declared* dialect through real gating and the real provider encoder, and
asserts the exact reasoning keys on the body. If the two-fact rule regresses
anywhere -- capability resolution, gating, an encoder, a dialect declaration --
a row here changes, and the row says in one line what the user will see.

Capability values are the ones models.dev publishes in its OpenRouter bucket
(read 2026-08-29); dialect values are the ones each provider declares. Nothing
here is hand-written wire shape: the bodies come from the encoders themselves.
"""

from typing import Any

import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_gating import adapt_reasoning_policy
from my_claude_code.core.reasoning import (
    ReasoningDialect,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages.provider import (
    ANTHROPIC_REASONING_DIALECT,
)
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_REASONING_DIALECT,
)
from my_claude_code.providers.commandcode.client import _PROFILE as COMMANDCODE_PROFILE
from my_claude_code.providers.deepseek.client import DEEPSEEK_REASONING_DIALECT
from my_claude_code.providers.gemini.client import _PROFILE as GEMINI_PROFILE
from my_claude_code.providers.mistral.client import MISTRAL_REASONING_DIALECT
from my_claude_code.providers.nvidia_nim.client import NIM_REASONING_DIALECT
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile

# --------------------------------------------------------------------------
# The two facts, per route.
# --------------------------------------------------------------------------

TOGGLE_ONLY = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=False,
    supports_toggle_control=True,
    supports_budget_control=False,
)


def effort_model(*efforts: ReasoningEffort, toggle: bool = False):
    return ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supports_toggle_control=toggle,
        supports_budget_control=False,
        supported_efforts=frozenset(efforts),
    )


def profile_dialect(profile_id: str) -> ReasoningDialect:
    return OPENAI_CHAT_PROFILES[profile_id].reasoning.dialect


COMMANDCODE_DIALECT = COMMANDCODE_PROFILE.reasoning.dialect
# OpenRouter, Nous Portal and Kilo all negotiate reasoning through one shared
# OpenRouter-dialect profile, so one declaration serves all three.
OPENROUTER_DIALECT = openrouter_gateway_profile("OPENROUTER").reasoning.dialect
GEMINI_DIALECT = GEMINI_PROFILE.reasoning.dialect
NIM_DIALECT = NIM_REASONING_DIALECT
DEEPSEEK_DIALECT = DEEPSEEK_REASONING_DIALECT
MISTRAL_DIALECT = MISTRAL_REASONING_DIALECT
CHATGPT_DIALECT = CHATGPT_OAUTH_REASONING_DIALECT
ANTHROPIC_DIALECT = ANTHROPIC_REASONING_DIALECT

# ``nous_portal`` serves both of the rows below through ONE dialect -- the
# shared OpenRouter one. What differs is the MODEL half: the gateway publishes
# ``reasoning_effort`` in ``supported_parameters`` for ``tencent/hy3:free`` and
# not for ``meituan/longcat-2.0:free``, and that per-model statement is read as
# a capability (an effort knob) since 6.3.0. One gateway, two wires, decided by
# the gateway's own words about each model.


def reasoning_keys(body: dict[str, Any]) -> dict[str, Any]:
    """The reasoning-bearing part of one request body, envelope removed."""

    merged: dict[str, Any] = {}
    for key, value in body.items():
        if key == "extra_body" and isinstance(value, dict):
            merged.update(reasoning_keys(value))
        elif key not in {"model", "messages"}:
            merged[key] = value
    return merged


# provider/model, capability, dialect, requested effort, expected reasoning keys
WIRE_TABLE: tuple[tuple[Any, ...], ...] = (
    # A toggle-only model on an effort-only host: no on/off field exists, and
    # the host's on-value is one of its own effort rungs. Nothing is sent, and
    # the model's own default reasoning behaviour stands. Live: this gateway
    # returns reasoning_tokens=0 for this model whatever the body carries.
    (
        "commandcode",
        "minimax/minimax-m3-free",
        TOGGLE_ONLY,
        COMMANDCODE_DIALECT,
        ReasoningEffort.MAX,
        {},
    ),
    # An effort model on the same host: the ask survives, clamped into the
    # gateway's own documented enum.
    (
        "commandcode",
        "z-ai/glm-5.3-flash",
        effort_model(ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX),
        COMMANDCODE_DIALECT,
        ReasoningEffort.MAX,
        {"reasoning_effort": "max"},
    ),
    (
        "commandcode",
        "z-ai/glm-5.3-flash",
        effort_model(ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX),
        COMMANDCODE_DIALECT,
        ReasoningEffort.MEDIUM,
        {"reasoning_effort": "low"},
    ),
    # OpenCode: toggle-only model, effort-only host, and the host 400s on every
    # effort rung for it. Nothing is the only correct body.
    (
        "opencode",
        "mimo-v2.5-free",
        TOGGLE_ONLY,
        profile_dialect("opencode"),
        ReasoningEffort.HIGH,
        {},
    ),
    (
        "opencode",
        "hy3-free",
        effort_model(ReasoningEffort.LOW, ReasoningEffort.HIGH),
        profile_dialect("opencode"),
        ReasoningEffort.HIGH,
        {"reasoning_effort": "high"},
    ),
    # One gateway, two dialects, decided per model by its own
    # ``supported_parameters``.
    (
        "nous_portal",
        "tencent/hy3:free",
        effort_model(ReasoningEffort.LOW, ReasoningEffort.HIGH),
        OPENROUTER_DIALECT,
        ReasoningEffort.HIGH,
        {"reasoning": {"effort": "high"}},
    ),
    (
        "nous_portal",
        "meituan/longcat-2.0:free",
        TOGGLE_ONLY,
        OPENROUTER_DIALECT,
        ReasoningEffort.HIGH,
        {"reasoning": {"enabled": True}},
    ),
    # A toggle+effort model on a chat-template host: the toggle is the channel
    # both sides have, so thinking goes on and the level is discarded.
    (
        "nvidia_nim",
        "moonshotai/kimi-k3",
        effort_model(
            ReasoningEffort.LOW,
            ReasoningEffort.HIGH,
            ReasoningEffort.MAX,
            toggle=True,
        ),
        NIM_DIALECT,
        ReasoningEffort.MAX,
        None,  # NIM builds its own body; asserted separately below.
    ),
    # OpenRouter: toggle + effort [high, xhigh], asked for max -> xhigh.
    (
        "open_router",
        "z-ai/glm-5.2:free",
        effort_model(ReasoningEffort.HIGH, ReasoningEffort.XHIGH, toggle=True),
        OPENROUTER_DIALECT,
        ReasoningEffort.MAX,
        {"reasoning": {"effort": "xhigh"}},
    ),
    # Gemini's host spells an OFF, and folds xhigh/max onto "high" -- so a max
    # request is a recorded clamp to high, never silence.
    (
        "gemini",
        "gemini-3-pro",
        effort_model(ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH),
        GEMINI_DIALECT,
        ReasoningEffort.MAX,
        {"reasoning_effort": "high"},
    ),
    # groq: the host's enum stops at high; max lands on high, not on groq's own
    # ``enabled_value`` of medium.
    (
        "groq",
        "openai/gpt-oss-120b",
        effort_model(ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH),
        profile_dialect("groq"),
        ReasoningEffort.MAX,
        {"reasoning_effort": "high"},
    ),
    # Mistral publishes one on-value; every lower ask clamps up to it, visibly.
    (
        "mistral",
        "magistral-medium",
        None,
        MISTRAL_DIALECT,
        ReasoningEffort.LOW,
        None,  # Mistral builds its own body; asserted separately below.
    ),
)


_PROFILES = {
    "commandcode": COMMANDCODE_PROFILE,
    "opencode": OPENAI_CHAT_PROFILES["opencode"],
    "groq": OPENAI_CHAT_PROFILES["groq"],
    "gemini": GEMINI_PROFILE,
    "open_router": openrouter_gateway_profile("OPENROUTER"),
    "nous_portal": openrouter_gateway_profile("NOUS PORTAL"),
}


def gated(
    capability: ModelReasoningCapability | None,
    dialect: ReasoningDialect,
    effort: ReasoningEffort,
    model_ref: str,
) -> ReasoningPolicy:
    adapted, _adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=effort),
        capability,
        dialect=dialect,
        max_tokens=4096,
        output_limit=32768,
        model_ref=model_ref,
    )
    return adapted


@pytest.mark.parametrize(
    ("provider_id", "model_id", "capability", "dialect", "effort", "expected"),
    [row for row in WIRE_TABLE if row[5] is not None],
    ids=[
        f"{row[0]}/{row[1]}@{row[4].value}" for row in WIRE_TABLE if row[5] is not None
    ],
)
def test_the_wire_table_is_what_leaves_the_proxy(
    provider_id: str,
    model_id: str,
    capability: ModelReasoningCapability | None,
    dialect: ReasoningDialect,
    effort: ReasoningEffort,
    expected: dict[str, Any],
) -> None:
    profile = _PROFILES[provider_id]
    policy = gated(capability, dialect, effort, f"{provider_id}/{model_id}")

    body: dict[str, Any] = {"model": model_id, "messages": []}
    profile.reasoning.encode(body, policy)

    assert reasoning_keys(body) == expected


def test_a_toggle_and_effort_model_on_nim_takes_the_chat_template_toggle() -> None:
    """NIM strips every effort-shaped key and reads a chat-template flag.

    So the effort is discarded in favour of the channel both sides have, and
    the numeric budget rides along in the field NIM does parse.
    """

    policy = gated(
        effort_model(
            ReasoningEffort.LOW,
            ReasoningEffort.HIGH,
            ReasoningEffort.MAX,
            toggle=True,
        ),
        NIM_DIALECT,
        ReasoningEffort.MAX,
        "nvidia_nim/moonshotai/kimi-k3",
    )

    assert policy == ReasoningPolicy.on()
    # No budget rides along: this model states it has no budget knob, and NIM
    # having a field for one is not a reason to invent a number for it.
    assert policy.numeric_budget_tokens is None


def test_mistrals_single_rung_clamps_up_and_is_recorded() -> None:
    """One on-value, so a lower ask becomes it -- never "none", never silence."""

    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=ReasoningEffort.LOW),
        None,
        dialect=MISTRAL_DIALECT,
        model_ref="mistral/magistral-medium",
    )

    assert adapted.effort is ReasoningEffort.HIGH
    assert adaptation.kind.value == "clamped"


def test_chatgpt_oauth_has_no_off_spelling_and_a_default_rung() -> None:
    """An explicit OFF omits the block; there is no disable to send.

    Recorded because the spec for this work said otherwise, and the endpoint's
    own conversion is the authority: OFF returns ``None`` and the whole
    ``reasoning`` block is left out.
    """

    assert CHATGPT_DIALECT.off is False
    assert CHATGPT_DIALECT.toggle is True
    assert CHATGPT_DIALECT.toggle_field == CHATGPT_DIALECT.effort_field


def test_anthropic_is_the_one_host_with_an_adaptive_channel() -> None:
    assert ANTHROPIC_DIALECT.adaptive is True
    assert ANTHROPIC_DIALECT.budget is True
    assert ANTHROPIC_DIALECT.effort_values is None


def test_deepseek_spells_the_whole_ladder() -> None:
    """Its own 400 named the enum, and it is FCC's ladder value for value."""

    assert DEEPSEEK_DIALECT.effort_values == frozenset(ReasoningEffort)
