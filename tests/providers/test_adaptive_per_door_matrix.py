"""The per-door matrix: what ADAPTIVE, a fixed rung and OFF each send.

This is the acceptance criterion for 8.13.0 ("adaptive as a whole should fully
function"). Every other test in this area checks one layer; this one is the
table the user asked for -- three doors times three policies times three
observations:

* **what goes out** -- the reasoning keys on the body the encoder built;
* **what is recorded** -- the adaptation kind the request log will carry, and
  the sentence the dashboard shows beside it;
* **what the badge says** -- the text ``formatReasoningEmitted`` renders from
  the attempt's own recorded wire snapshot.

The three doors are the three the fleet actually speaks:

* **Messages** -- ``build_anthropic_messages_body``, whose dialect has a real
  adaptive channel (``thinking: {"type": "adaptive"}``);
* **Responses** -- ``build_responses_request_body``, which has no adaptive
  channel and has always answered ADAPTIVE with the endpoint's own ``medium``;
* **Chat Completions** -- the ``opencode_go`` profile, whose only word for
  "reason" is one of its own effort rungs, so ADAPTIVE is answered with the
  highest rung it publishes.

Two invariants run through every cell:

1. **No fabricated rung.** Whatever leaves is a value the host itself declares
   -- its own adaptive type, its own level-less on, or a rung from its own
   published vocabulary. Never a word invented here.
2. **Nothing is silent.** A door that cannot express adaptive records what it
   sent instead; a door that can express it records nothing, because nothing
   was adapted. Either way the wire snapshot carries the field, so the badge
   says what went out.

The badge cells are asserted against the same rule ``admin.js`` implements,
read out of the page's own source rather than restated here: the badge is
``reasoning sent: <value>`` when a reasoning key went out, and "no reasoning
instruction sent (model default applies)" when none did. The jsdom half of the
same table lives in ``tests/api/test_admin_static_jsdom.py``.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ADAPTIVE_MEANING,
    AdaptiveExpression,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
    adaptive_expression,
)
from my_claude_code.core.wire_capture import (
    install_wire_trace,
    is_reasoning_key,
    reasoning_was_emitted,
)
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.reasoning import NamedEffortReasoning
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)

# The three doors, each with the encoder entry point the provider really uses.
DOORS = ("messages", "responses", "chat_completions")

# The three policies the user named. ``HIGH`` stands for "a fixed rung": it is
# a rung every door in this table can spell, so a difference between the
# ADAPTIVE and HIGH columns is a difference about adaptive, not about the rung.
POLICIES = {
    "adaptive": ReasoningPolicy.adaptive(),
    "fixed_rung": ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
    "off": ReasoningPolicy.off(),
}

REQUEST = MessagesRequest.model_validate(
    {
        "model": "deepseek-v4.1-flash",
        "max_tokens": 65_000,
        "messages": [{"role": "user", "content": "hello"}],
    }
)


def _trim(body: dict[str, Any]) -> dict[str, Any]:
    """The reasoning-bearing part of a body, envelope removed."""

    merged: dict[str, Any] = {}
    for key, value in body.items():
        if key == "extra_body" and isinstance(value, dict):
            merged.update(_trim(value))
        elif key not in {"model", "messages", "input", "stream", "store"}:
            merged[key] = value
    return merged


def _messages_body(policy: ReasoningPolicy) -> dict[str, Any]:
    return _trim(build_anthropic_messages_body(REQUEST, reasoning=policy))


def _responses_body(policy: ReasoningPolicy) -> dict[str, Any]:
    return _trim(
        build_responses_request_body(
            REQUEST,
            reasoning=policy,
            store=False,
            stream=True,
            parallel_tool_calls=False,
        )
    )


def _chat_body(policy: ReasoningPolicy) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "deepseek-v4.1-flash", "messages": []}
    OPENAI_CHAT_PROFILES["opencode_go"].reasoning.encode(body, policy)
    return _trim(body)


BUILDERS = {
    "messages": _messages_body,
    "responses": _responses_body,
    "chat_completions": _chat_body,
}


def _build(door: str, policy: ReasoningPolicy) -> dict[str, Any]:
    return BUILDERS[door](policy)


def _recorded(door: str, policy: ReasoningPolicy) -> tuple[str, str | None]:
    """The adaptation kind and message this door records, under a live trace.

    Run inside ``install_wire_trace`` so ``record_reasoning_adaptation`` is
    live, exactly as it is inside a real request.
    """

    trace = install_wire_trace()
    try:
        _build(door, policy)
        adaptations = trace.reasoning_adaptations
    finally:
        from my_claude_code.core.wire_capture import _WIRE_TRACE

        _WIRE_TRACE.set(None)
    if not adaptations:
        return ReasoningAdaptationKind.UNCHANGED.value, None
    assert len(adaptations) == 1, [item.message for item in adaptations]
    return adaptations[0].kind.value, adaptations[0].message


def _wire_value_text(value: Any) -> str:
    """``wireValueText`` in ``admin.js``: a flat object reads as ``k=v`` pairs."""

    if isinstance(value, str):
        return value
    if (
        isinstance(value, dict)
        and value
        and all(not isinstance(item, (dict, list)) for item in value.values())
    ):
        return ", ".join(f"{name}={item}" for name, item in value.items())
    return json.dumps(value)


def _badge(body: dict[str, Any]) -> str:
    """What ``formatReasoningEmitted`` renders for this body's wire snapshot.

    Mirrors the page's own rule, and the mirror is pinned against the page's
    source by :func:`test_the_badge_rule_mirrored_here_is_the_pages_own`.
    ``params.wire.reasoning`` is the map of every reasoning key on the body,
    which is what ``wireReasoningValue`` reads.
    """

    if not reasoning_was_emitted(body):
        return "no reasoning instruction sent (model default applies)"
    reasoning = {key: value for key, value in body.items() if is_reasoning_key(key)}
    if len(reasoning) == 1:
        (value,) = reasoning.values()
        return f"reasoning sent: {_wire_value_text(value)}"
    if reasoning:
        return f"reasoning sent: {', '.join(reasoning)}"
    return "reasoning sent"


# ---------------------------------------------------------------------------
# The table itself.
# ---------------------------------------------------------------------------


def test_the_adaptive_definition_is_stated_once() -> None:
    """Requirement 1: one sentence, in declared data, that every door reads."""

    assert ADAPTIVE_MEANING == "let the host think as much as it decides it needs"


@pytest.mark.parametrize("door", DOORS)
def test_the_door_declares_how_it_expresses_adaptive(door: str) -> None:
    """Every door's expression is derived from its dialect, never from a name."""

    if door == "messages":
        from my_claude_code.providers.anthropic_messages.provider import (
            ANTHROPIC_REASONING_DIALECT,
        )

        assert (
            adaptive_expression(ANTHROPIC_REASONING_DIALECT)
            is AdaptiveExpression.NATIVE
        )
    elif door == "chat_completions":
        dialect = OPENAI_CHAT_PROFILES["opencode_go"].reasoning.dialect
        assert adaptive_expression(dialect) is AdaptiveExpression.TOP_RUNG
    else:
        # The Responses door has no adaptive channel and no level-less on:
        # ``reasoning.effort`` takes a word and nothing else.
        from my_claude_code.providers.chatgpt_oauth.provider import (
            CHATGPT_OAUTH_REASONING_DIALECT,
        )

        assert (
            adaptive_expression(CHATGPT_OAUTH_REASONING_DIALECT)
            is AdaptiveExpression.TOP_RUNG
        )


# --- what goes out ---------------------------------------------------------


def test_messages_adaptive_sends_the_adaptive_type() -> None:
    assert _build("messages", POLICIES["adaptive"])["thinking"] == {"type": "adaptive"}


def test_messages_fixed_rung_sends_a_budget_and_no_effort_word() -> None:
    body = _build("messages", POLICIES["fixed_rung"])
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] > 0
    # The door's dialect declares no effort field; adding one would be a guess.
    assert "reasoning_effort" not in body
    assert "reasoning" not in body


def test_messages_off_pops_the_thinking_field() -> None:
    assert "thinking" not in _build("messages", POLICIES["off"])


def test_responses_adaptive_keeps_the_endpoints_own_default_rung() -> None:
    """Kept, not changed: moving it would move every adaptive body ever sent."""

    assert _build("responses", POLICIES["adaptive"])["reasoning"] == {
        "effort": "medium",
        "summary": "auto",
    }


def test_responses_fixed_rung_sends_the_rung_verbatim() -> None:
    assert _build("responses", POLICIES["fixed_rung"])["reasoning"] == {
        "effort": "high",
        "summary": "auto",
    }


def test_responses_off_omits_the_block() -> None:
    assert "reasoning" not in _build("responses", POLICIES["off"])


def test_chat_adaptive_sends_the_hosts_top_published_rung() -> None:
    """The hole this PR closes: Go's Chat door used to send nothing at all."""

    assert _build("chat_completions", POLICIES["adaptive"])["reasoning_effort"] == "max"


def test_chat_fixed_rung_sends_the_rung() -> None:
    assert (
        _build("chat_completions", POLICIES["fixed_rung"])["reasoning_effort"] == "high"
    )


def test_chat_off_sends_nothing() -> None:
    assert _build("chat_completions", POLICIES["off"]) == {}


@pytest.mark.parametrize("door", DOORS)
def test_no_door_invents_a_rung_it_did_not_declare(door: str) -> None:
    """IV.1: whatever leaves is a value the host itself publishes."""

    body = _build(door, POLICIES["adaptive"])
    if door == "messages":
        assert body["thinking"]["type"] == "adaptive"
    elif door == "responses":
        assert body["reasoning"]["effort"] == "medium"
    else:
        encoder = OPENAI_CHAT_PROFILES["opencode_go"].reasoning
        assert isinstance(encoder, NamedEffortReasoning)
        declared = {word for _effort, word in encoder.efforts}
        assert body["reasoning_effort"] in declared


# --- what is recorded ------------------------------------------------------


def test_messages_records_no_adaptation_because_it_expressed_the_request() -> None:
    kind, message = _recorded("messages", POLICIES["adaptive"])
    assert kind == ReasoningAdaptationKind.UNCHANGED.value
    assert message is None


def test_responses_records_the_substitution_it_has_always_made() -> None:
    """The 8.13.0 change on this door: the same bytes, now stated."""

    kind, message = _recorded("responses", POLICIES["adaptive"])
    assert kind == ReasoningAdaptationKind.SUBSTITUTED.value
    assert message is not None
    assert "medium" in message
    assert "adaptive channel" in message


def test_chat_records_the_rung_it_sent_instead() -> None:
    kind, message = _recorded("chat_completions", POLICIES["adaptive"])
    assert kind == ReasoningAdaptationKind.SUBSTITUTED.value
    assert message is not None
    assert "'max'" in message
    assert "reasoning_effort" in message


@pytest.mark.parametrize("door", DOORS)
def test_a_fixed_rung_and_off_record_nothing_on_every_door(door: str) -> None:
    """The equality boundary: only an adaptive request can be adapted."""

    for name in ("fixed_rung", "off"):
        kind, message = _recorded(door, POLICIES[name])
        assert kind == ReasoningAdaptationKind.UNCHANGED.value, (door, name)
        assert message is None, (door, name)


# --- what the badge says ---------------------------------------------------


def test_the_badge_names_the_adaptive_type_on_the_messages_door() -> None:
    assert _badge(_build("messages", POLICIES["adaptive"])) == (
        "reasoning sent: type=adaptive"
    )


def test_the_badge_names_the_effort_on_the_responses_door() -> None:
    assert _badge(_build("responses", POLICIES["adaptive"])) == (
        "reasoning sent: effort=medium, summary=auto"
    )


def test_the_badge_names_the_rung_on_the_chat_door() -> None:
    assert _badge(_build("chat_completions", POLICIES["adaptive"])) == (
        "reasoning sent: max"
    )


@pytest.mark.parametrize("door", DOORS)
def test_the_badge_never_reads_as_nothing_sent_on_an_adaptive_request(
    door: str,
) -> None:
    """Requirement 4: a user must never see "adaptive" and no way to tell."""

    assert _badge(_build(door, POLICIES["adaptive"])) != (
        "no reasoning instruction sent (model default applies)"
    )


def test_the_badge_reads_as_nothing_sent_only_where_nothing_went_out() -> None:
    for door in DOORS:
        body = _build(door, POLICIES["off"])
        assert _badge(body) == "no reasoning instruction sent (model default applies)"


def test_the_badge_rule_mirrored_here_is_the_pages_own() -> None:
    """The mirror above is pinned to ``admin.js``, not restated from memory."""

    source = ADMIN_JS.read_text(encoding="utf-8")
    assert "no reasoning instruction sent (model default applies)" in source
    assert "reasoning sent: ${value}" in source
    # ``wireValueText`` renders a flat object as ``key=value`` pairs, which is
    # what the two object-shaped doors' badge cells above assume.
    assert re.search(
        r"function wireValueText\(value\) \{.*?parts\.join\(\", \"\)", source, re.S
    ), "wireValueText no longer renders flat objects as key=value pairs"


# ---------------------------------------------------------------------------
# The whole table, as one object, so a reader can see it in one place.
# ---------------------------------------------------------------------------

MATRIX: dict[tuple[str, str], dict[str, str]] = {
    ("messages", "adaptive"): {
        "wire": 'thinking: {"type": "adaptive"}',
        "recorded": "unchanged (the door expressed it)",
        "badge": "reasoning sent: type=adaptive",
    },
    ("messages", "fixed_rung"): {
        # The budget is derived from the model's output allowance; this
        # request names none, so the flat last-resort table supplies 2,048 for
        # ``high``. The cell asserts the shape and the budget that actually
        # left, not a placeholder.
        "wire": "thinking: {type: enabled, budget_tokens: 2048}",
        "recorded": "unchanged",
        "badge": "reasoning sent: type=enabled, budget_tokens=2048",
    },
    ("messages", "off"): {
        "wire": "nothing (thinking popped)",
        "recorded": "unchanged",
        "badge": "no reasoning instruction sent (model default applies)",
    },
    ("responses", "adaptive"): {
        "wire": 'reasoning: {"effort": "medium", "summary": "auto"}',
        "recorded": "substituted (medium, the endpoint default)",
        "badge": "reasoning sent: effort=medium, summary=auto",
    },
    ("responses", "fixed_rung"): {
        "wire": 'reasoning: {"effort": "high", "summary": "auto"}',
        "recorded": "unchanged",
        "badge": "reasoning sent: effort=high, summary=auto",
    },
    ("responses", "off"): {
        "wire": "nothing (block omitted)",
        "recorded": "unchanged",
        "badge": "no reasoning instruction sent (model default applies)",
    },
    ("chat_completions", "adaptive"): {
        "wire": 'reasoning_effort: "max"',
        "recorded": "substituted (the host's top published rung)",
        "badge": "reasoning sent: max",
    },
    ("chat_completions", "fixed_rung"): {
        "wire": 'reasoning_effort: "high"',
        "recorded": "unchanged",
        "badge": "reasoning sent: high",
    },
    ("chat_completions", "off"): {
        "wire": "nothing",
        "recorded": "unchanged",
        "badge": "no reasoning instruction sent (model default applies)",
    },
}


@pytest.mark.parametrize(("door", "policy"), sorted(MATRIX))
def test_every_cell_of_the_matrix_is_what_the_code_does(door: str, policy: str) -> None:
    """Nine cells, each asserted against the real encoder and the real rule."""

    expected = MATRIX[(door, policy)]
    body = _build(door, POLICIES[policy])
    kind, _message = _recorded(door, POLICIES[policy])

    assert _badge(body) == expected["badge"], (door, policy)
    if expected["recorded"].startswith("unchanged"):
        assert kind == ReasoningAdaptationKind.UNCHANGED.value
    else:
        assert kind == ReasoningAdaptationKind.SUBSTITUTED.value
    # The wire column is prose; the machine-checkable half is that an adaptive
    # request's body carries a reasoning key on every door and an OFF body
    # carries none.
    if policy == "off":
        assert not any(is_reasoning_key(str(key)) for key in body)
    else:
        assert reasoning_was_emitted(body)


def test_the_matrix_is_the_whole_table() -> None:
    """Three doors times three policies, and no cell left out."""

    assert set(MATRIX) == {(door, policy) for door in DOORS for policy in POLICIES}
    assert len(MATRIX) == 9


def test_the_matrix_documents_every_control_value_it_claims() -> None:
    """``ReasoningControl`` has four members; the matrix covers three intents.

    ``DEFAULT`` is deliberately absent: it is "no opinion was expressed", not a
    policy an operator can select, and it is covered by the equality baselines.
    """

    assert {policy.control for policy in POLICIES.values()} == {
        ReasoningControl.ADAPTIVE,
        ReasoningControl.ON,
        ReasoningControl.OFF,
    }


def test_the_matrix_json_round_trips_so_it_can_be_diffed() -> None:
    """A table a reviewer can diff, not prose in a docstring."""

    rendered = json.dumps(
        {f"{door}|{policy}": cell for (door, policy), cell in sorted(MATRIX.items())},
        indent=2,
        sort_keys=True,
    )
    assert json.loads(rendered)["chat_completions|adaptive"]["badge"] == (
        "reasoning sent: max"
    )
