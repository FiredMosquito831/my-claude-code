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

from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
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


# ---------------------------------------------------------------------------
# The declaration and the encoder are two views of one fact.
# ---------------------------------------------------------------------------


def _declared_paths(dialect) -> set[str]:
    """Every wire object this dialect claims it can write.

    Compared at the top-level key: a dialect that names
    ``thinking.budget_tokens`` claims the ``thinking`` object, and the exact
    sub-keys it fills inside one object it owns are the encoder's business.
    What must not happen is an encoder writing into an object -- a whole
    reasoning channel -- that its dialect never mentions.
    """

    claimed = {
        dialect.effort_field if dialect.effort_values is not None else "",
        dialect.toggle_field if dialect.toggle or dialect.off else "",
        dialect.budget_field if dialect.budget else "",
    }
    return {path.split(".")[0] for path in claimed if path}


def _emitted_paths(body: dict, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for key, value in body.items():
        # ``extra_body`` is an SDK transport envelope, not a wire field: the
        # gateway sees its contents at the top level.
        path = prefix if key == "extra_body" else (f"{prefix}.{key}" if prefix else key)
        if isinstance(value, dict) and value:
            paths |= _emitted_paths(value, path)
        else:
            paths.add(path)
    return paths


@pytest.mark.parametrize("name,profile", _ALL_PROFILES)
def test_every_encoder_declares_a_dialect_consistent_with_what_it_emits(
    name: str, profile
) -> None:
    """No encoder may write a field its dialect does not claim.

    Gating decides what to send from the declaration alone, so a declaration
    that undersells its encoder is a control silently suppressed and one that
    oversells it is a control silently dropped. Both are invisible without
    this sweep, and both are exactly the class of defect this PR is fixing.
    """

    dialect = profile.reasoning.dialect
    declared = _declared_paths(dialect)

    policies = [
        ReasoningPolicy.on(),
        ReasoningPolicy.off(),
        ReasoningPolicy.on(budget_tokens=4096),
        *(ReasoningPolicy.on(effort=effort) for effort in ReasoningEffort),
    ]
    for policy in policies:
        emitted = _emitted_paths(_encode(profile, policy))
        # ``reasoning_split`` is an output-routing request, not a compute
        # control, and is emitted unconditionally by design.
        emitted = {path.split(".")[0] for path in emitted} - {"reasoning_split"}
        assert emitted <= declared, (name, policy, emitted, declared)


@pytest.mark.parametrize("name,profile", _ALL_PROFILES)
def test_a_declared_effort_vocabulary_is_one_the_encoder_can_reach(
    name: str, profile
) -> None:
    """Every rung the dialect advertises must actually change the body.

    An encoder that maps all six FCC efforts onto one wire word has a
    one-rung vocabulary, not a six-rung one, and saying otherwise tells gating
    a clamp is unnecessary -- which is how Cohere's flattening of low to
    "high" went unrecorded for so long.
    """

    dialect = profile.reasoning.dialect
    if dialect.effort_values is None:
        return
    emitted = {
        effort: _encode(profile, ReasoningPolicy.on(effort=effort))
        for effort in dialect.effort_values
    }
    distinct = {repr(sorted(body.items())) for body in emitted.values()}
    assert len(distinct) == len(dialect.effort_values), (name, emitted)
