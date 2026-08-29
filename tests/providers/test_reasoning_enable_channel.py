"""Every encoder that *can* express a level-less "on" must actually do so.

``ReasoningPolicy.on()`` -- control ON with the effort discarded -- is not an
edge case. It is precisely what per-model capability gating returns from
``_drop_controls`` and from its toggle-only branch, so it is the policy that
reaches the encoder on every route whose models.dev row publishes no effort
control. An encoder with no path for it is a silent hole: gating logs
"enabling thinking" and the body leaves carrying no reasoning instruction at
all.

These tests enumerate the profile registry rather than naming providers, so a
profile added later is covered the day it lands, and they name the deliberate
abstainers explicitly so that removing one of those justifications also fails a
test.

Deliberately *not* asserted here: anything about ``ReasoningControl.ADAPTIVE``.
Adaptive degrading to the provider's own default everywhere outside Anthropic
is a stated product decision, pinned by ``tests/application/test_adaptive_reasoning.py``.
"""

import pytest

from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.commandcode.client import _PROFILE as COMMANDCODE_PROFILE
from my_claude_code.providers.openai_chat.profiles import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
)
from my_claude_code.providers.openai_chat.reasoning import NoReasoning

_ALL_PROFILES = (
    *OPENAI_CHAT_PROFILES.items(),
    ("<generic>", GENERIC_OPENAI_PROFILE),
    ("commandcode", COMMANDCODE_PROFILE),
)

# Profiles that deliberately send nothing for a level-less "on", each for a
# reason recorded next to its encoder and backed by a live probe.
_NO_ENABLE_CHANNEL = {
    # 2026-08-29 probe: naming any rung *reduces* reasoning (3,000 -> 903
    # reasoning tokens on ``hy3-free``), and the gateway validates the enum but
    # forwards the field to the model, which answers HTTP 400 to every rung on
    # ``mimo-v2.5-free`` while a bare request returns 200 and reasons.
    "opencode",
    # A numeric thinking budget only; "on with no level" names no number, and
    # llama.cpp has no boolean to fall back on.
    "llamacpp",
}


def _encode(profile, policy: ReasoningPolicy) -> dict:
    body: dict = {}
    profile.reasoning.encode(body, policy)
    if body.get("extra_body") == {}:
        del body["extra_body"]
    return body


@pytest.mark.parametrize("name,profile", _ALL_PROFILES)
def test_level_less_on_reaches_the_wire(name: str, profile) -> None:
    """A control=ON policy with no effort must emit, or be a known abstainer."""

    body = _encode(profile, ReasoningPolicy.on())
    if isinstance(profile.reasoning, NoReasoning) or name in _NO_ENABLE_CHANNEL:
        assert body == {}, f"{name} unexpectedly gained an enable channel"
        return
    assert body, f"{name} silently drops a level-less reasoning request"


def test_commandcode_emits_its_probed_on_value() -> None:
    """The gateway reasons *more* when a rung is named, so ON must name one.

    Live A/B on 2026-08-29, identical prompt at ``max_tokens: 3000``:
    ``deepseek/deepseek-v4-flash`` returned 132 reasoning tokens bare against
    1,046 under ``reasoning_effort: "max"``, and ``xiaomi/mimo-v2.5`` returned
    7 against 17. Both HTTP 200. That refutes the 5.69.0 note this encoder was
    wired against, which assumed a bare request already reasons the most.
    """

    assert _encode(COMMANDCODE_PROFILE, ReasoningPolicy.on()) == {
        "reasoning_effort": "max"
    }
    # OFF still sends nothing: the enum has no "none" rung and 400s on one.
    assert _encode(COMMANDCODE_PROFILE, ReasoningPolicy.off()) == {}


def test_opencode_still_abstains_from_an_on_value() -> None:
    """Pinned separately from the registry sweep, with the reason in one place.

    The temptation after fixing Command Code is to give OpenCode the same
    treatment. It is the exact inverse: its gateway forwards
    ``reasoning_effort`` to the model, and ``mimo-v2.5-free`` rejects every
    rung with HTTP 400 while reasoning happily on a bare request. The field is
    safe there only behind the per-model capability gate that already stands in
    front of it.
    """

    profile = OPENAI_CHAT_PROFILES["opencode"]
    assert _encode(profile, ReasoningPolicy.on()) == {}
    # The effort channel itself is untouched, including its real OFF value.
    assert _encode(profile, ReasoningPolicy.off()) == {"reasoning_effort": "none"}
