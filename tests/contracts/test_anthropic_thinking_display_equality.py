"""7.102.5 moves no Anthropic body unless the client chose a ``thinking.display``.

7.102.5 lets Claude Code's ``thinking.display`` reach Anthropic on the Claude
subscription, and nowhere else. Over the whole corpus of
``test_anthropic_foreign_thinking_equality`` (432 bodies, digests recorded at
v7.102.0, with 7.102.4's one approved difference -- MCC's ``enabled`` key
gone -- applied):

* with forwarding on, every body whose client sent no ``display`` is
  byte-identical;
* with a ``display`` added to every client ``thinking`` object, every build
  without forwarding -- the Console API-key provider, Command Code, the
  custom and OpenCode Messages doors -- is byte-identical too;
* with forwarding on, such a body differs by exactly one key, ``display``,
  appended to a ``thinking`` that asks for thinking, and in no other body.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from tests.contracts.test_anthropic_foreign_thinking_equality import (
    BASELINE_PATH,
    corpus,
    digest,
)

DISPLAY = "summarized"


def _expected() -> dict[str, str]:
    baseline: dict[str, Any] = json.loads(
        Path(BASELINE_PATH).read_text(encoding="utf-8")
    )
    return {**baseline["wire"], **baseline["wire_without_thinking_enabled"]}


def _with_display(request: MessagesRequest) -> MessagesRequest | None:
    if request.thinking is None:
        return None
    thinking = request.thinking.model_copy(update={"display": DISPLAY})
    return request.model_copy(update={"thinking": thinking})


def _build(
    request: MessagesRequest, policy: ReasoningPolicy, *, forward: bool
) -> dict[str, Any]:
    return build_anthropic_messages_body(
        request, reasoning=policy, forward_thinking_display=forward
    )


def test_forwarding_moves_no_body_whose_client_chose_no_display() -> None:
    expected = _expected()
    moved = [
        key
        for key, (request, policy) in sorted(corpus().items())
        if digest(_build(request, policy, forward=True)) != expected[key]
    ]

    assert moved == []


def test_a_display_reaches_no_path_that_does_not_forward_it() -> None:
    expected = _expected()
    moved: list[str] = []
    checked = 0
    for key, (request, policy) in sorted(corpus().items()):
        chosen = _with_display(request)
        if chosen is None:
            continue
        checked += 1
        if digest(_build(chosen, policy, forward=False)) != expected[key]:
            moved.append(key)

    assert moved == []
    assert checked == 324


def test_on_the_subscription_a_display_adds_exactly_that_key() -> None:
    carried = 0
    for key, (request, policy) in sorted(corpus().items()):
        chosen = _with_display(request)
        if chosen is None:
            continue
        plain = _build(request, policy, forward=False)
        sent = _build(chosen, policy, forward=True)
        thinking = plain.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") in {
            "adaptive",
            "enabled",
        }:
            plain = {**plain, "thinking": {**thinking, "display": DISPLAY}}
            carried += 1
        assert json.dumps(sent) == json.dumps(plain), key

    assert carried > 0
