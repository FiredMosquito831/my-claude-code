"""7.104.0 moves no Anthropic Messages body unless a lifetime setting chose one.

The user's rule for the prompt-cache lifetime (decisions 2026-10-20 Q1/Q2):
by default MCC forwards the harness's own lifetime untouched, and changes one
only when an MCC setting asks. Over the whole corpus of
``test_anthropic_foreign_thinking_equality`` (432 bodies, digests recorded at
v7.102.0, with 7.102.4's one approved difference applied):

* with nothing chosen -- and with a request stamped by a setting but built
  where no lifetime is declared -- every body is byte-identical, and the
  builder never reaches the lifetime step at all (identity, not equality by
  copy);
* a stamped request dumps exactly as an unstamped one, so the request log,
  the other dialects and every body built from a dump are unchanged;
* with ``1h`` chosen, a body differs only by ``ttl`` on markers the client
  wrote -- no marker is added to a corpus body (every one carries a system
  marker) and none removed;
* with ``5m`` chosen over this corpus -- whose markers carry no ``ttl``, which
  is Anthropic's 5 minutes already -- no body moves.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.prompt_cache import (
    PromptCacheLifetimeChoice,
    choose_prompt_cache_lifetime,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    ANTHROPIC_MESSAGES_LIFETIMES,
    PROMPT_CACHE_LIFETIME_FAMILIES,
)
from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.anthropic_messages import request as request_module
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from tests.contracts.test_anthropic_foreign_thinking_equality import (
    BASELINE_PATH,
    corpus,
    digest,
)

ANTHROPIC = PROMPT_CACHE_LIFETIME_FAMILIES[ANTHROPIC_MESSAGES_LIFETIMES]
ASK = PromptCacheAsk(30, "PROMPT_CACHE_TTL")


def _expected() -> dict[str, str]:
    baseline: dict[str, Any] = json.loads(
        Path(BASELINE_PATH).read_text(encoding="utf-8")
    )
    return {**baseline["wire"], **baseline["wire_without_thinking_enabled"]}


def _stamped(request: MessagesRequest) -> MessagesRequest:
    stamped = request.model_copy(deep=True)
    stamped.set_prompt_cache_ask(ASK)
    return stamped


def _choice(minutes: int) -> PromptCacheLifetimeChoice:
    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(minutes, "PROMPT_CACHE_TTL"), ANTHROPIC
    )
    assert choice is not None
    return choice


def _build(
    request: MessagesRequest,
    policy: ReasoningPolicy,
    *,
    forward: bool = False,
    choice: PromptCacheLifetimeChoice | None = None,
) -> dict[str, Any]:
    return build_anthropic_messages_body(
        request,
        reasoning=policy,
        forward_thinking_display=forward,
        prompt_cache=choice,
    )


def _with_ttl_on_every_marker(node: Any, ttl: str) -> Any:
    if isinstance(node, dict):
        return {
            key: (
                {**value, "ttl": ttl}
                if key == "cache_control" and isinstance(value, dict)
                else _with_ttl_on_every_marker(value, ttl)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_with_ttl_on_every_marker(item, ttl) for item in node]
    return node


def _marker_count(node: Any) -> int:
    if isinstance(node, dict):
        return sum(
            1
            if key == "cache_control" and isinstance(value, dict)
            else _marker_count(value)
            for key, value in node.items()
        )
    if isinstance(node, list):
        return sum(_marker_count(item) for item in node)
    return 0


def test_the_corpus_is_the_432_bodies_it_was() -> None:
    assert len(corpus()) == 432
    assert sorted(_expected()) == sorted(corpus())


@pytest.mark.parametrize("forward", [False, True])
def test_nothing_chosen_moves_no_body_and_never_reaches_the_step(
    forward: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the lifetime step ran with nothing chosen")

    monkeypatch.setattr(request_module, "apply_prompt_cache_lifetime", refuse)
    expected = _expected()
    moved = [
        key
        for key, (request, policy) in sorted(corpus().items())
        for built in (
            _build(request, policy, forward=forward),
            # Stamped by a setting, built where no lifetime is declared.
            _build(_stamped(request), policy, forward=forward),
        )
        if digest(built) != expected[key]
    ]

    assert moved == []


def test_a_stamped_request_dumps_exactly_as_an_unstamped_one() -> None:
    for key, (request, _policy) in sorted(corpus().items()):
        stamped = _stamped(request)
        assert stamped.prompt_cache_ask == ASK, key
        assert json.dumps(stamped.model_dump()) == json.dumps(request.model_dump()), key
        assert json.dumps(stamped.model_dump(exclude_none=True)) == json.dumps(
            request.model_dump(exclude_none=True)
        ), key


def test_an_hour_moves_only_the_ttl_of_the_clients_markers() -> None:
    checked = 0
    for key, (request, policy) in sorted(corpus().items()):
        plain = _build(request, policy)
        sent = _build(request, policy, choice=_choice(30))
        assert _marker_count(plain) >= 1, key
        assert "cache_control" not in sent, key
        assert json.dumps(sent) == json.dumps(_with_ttl_on_every_marker(plain, "1h")), (
            key
        )
        checked += 1

    assert checked == 432


def test_five_minutes_over_ttl_less_markers_moves_no_body() -> None:
    expected = _expected()
    moved = [
        key
        for key, (request, policy) in sorted(corpus().items())
        if digest(_build(request, policy, choice=_choice(3))) != expected[key]
    ]

    assert moved == []
