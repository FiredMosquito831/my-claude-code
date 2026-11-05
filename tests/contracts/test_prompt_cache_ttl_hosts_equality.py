"""7.106.0 moves no OpenAI-shaped body unless a lifetime is set for its host.

The user's rule (decisions 2026-10-20 Q1/Q2): MCC forwards the harness's own
lifetime untouched and changes one only when a setting or an override row
asks. Over the 432 requests of ``test_anthropic_foreign_thinking_equality``,
built as Chat Completions bodies for every host 7.106.0 declares something
for (``chat_prompt_cache_corpus.HOSTS``), against digests recorded at
v7.105.2 (``prompt_cache_ttl_hosts_baseline.json``, written by
``chat_prompt_cache_corpus.snapshot`` before this change existed):

* with nothing set, no attempt chooses a lifetime and the lifetime step hands
  back the very body it was given -- identity, not equality by copy -- so
  every body is byte-identical to 7.105.2's;
* with a lifetime stamped by Model Config, OpenRouter's non-Claude model and
  Agnes (which caches automatically) still choose nothing and move no byte;
* with 30 min stamped, an OpenRouter Claude body differs from 7.105.2's only
  by the one top-level marker (``extra_body.cache_control``, ``1h``) and an
  Azure body only by ``prompt_cache_retention: "24h"``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.providers.openai_chat.prompt_cache import (
    apply_chat_prompt_cache_lifetime,
    chat_prompt_cache_choice,
)
from tests.contracts.adaptive_expectations import expected_digest, moved
from tests.contracts.chat_prompt_cache_corpus import (
    HOSTS,
    build,
    chat_provider,
    host_request,
    snapshot,
)
from tests.contracts.test_anthropic_foreign_thinking_equality import corpus, digest

BASELINE_PATH = Path(__file__).with_name("prompt_cache_ttl_hosts_baseline.json")
ASK = PromptCacheAsk(30, "PROMPT_CACHE_TTL_OPUS")
#: One provider per id: building a body never changes the provider.
_PROVIDERS: dict[str, Any] = {}


def _baseline() -> dict[str, str]:
    document: dict[str, Any] = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert document["count"] == len(document["digests"])
    return document["digests"]


def _attempt(
    host: str, request: MessagesRequest, policy: Any, ask: PromptCacheAsk | None
) -> tuple[Any, Any, Any]:
    """What the stream runner does: build, choose, then the lifetime step."""

    provider_id, model = HOSTS[host]
    provider = _PROVIDERS.get(provider_id)
    if provider is None:
        provider = _PROVIDERS[provider_id] = chat_provider(provider_id)
    routed = host_request(request, model)
    if ask is not None:
        routed.set_prompt_cache_ask(ask)
    choice = chat_prompt_cache_choice(routed, provider_id=provider_id)
    built = build(provider, routed, policy)
    if isinstance(built, str):
        return built, built, choice
    return built, apply_chat_prompt_cache_lifetime(built, choice, {}), choice


def test_the_baseline_is_the_corpus_on_every_host() -> None:
    expected = _baseline()
    assert len(corpus()) == 432
    assert len(expected) == 432 * len(HOSTS)
    assert sorted(expected) == sorted(snapshot())


def test_nothing_set_moves_no_body_on_any_host() -> None:
    """Every call, against the committed baseline plus 8.13.0's recorded moves.

    8.13.0 makes the Chat door express an ADAPTIVE policy instead of silently
    sending nothing, which moves the body of every ``*/adaptive/*`` call. Those
    are recorded before/after in ``adaptive_every_door_expectations.json`` and
    nothing else is: a call whose policy is not ADAPTIVE must still equal the
    v7.105.2 digest exactly, and the recorded ``before`` of every listed call
    must be that same digest, so the historical baseline cannot drift.
    """

    expected = _baseline()
    recorded = moved("prompt_cache_ttl")
    differing: list[str] = []
    for host in sorted(HOSTS):
        for key, (request, policy) in sorted(corpus().items()):
            built, sent, choice = _attempt(host, request, policy, None)
            assert choice is None, (host, key)
            assert sent is built, (host, key)
            name = f"{host}/{key}"
            if digest(sent) != expected_digest(
                "prompt_cache_ttl", name, expected[name], recorded
            ):
                differing.append(name)

    assert differing == []
    # Anti-vacuity: the recorded set is exactly the adaptive calls, and every
    # one of them is a call this corpus really makes.
    assert recorded, "the recorded moves would prove nothing if empty"
    assert all("/adaptive/" in name for name in recorded), sorted(recorded)[:5]
    assert set(recorded) <= {f"{host}/{key}" for host in HOSTS for key in corpus()}


@pytest.mark.parametrize("host", ["open_router/open", "agnes"])
def test_a_stamped_lifetime_moves_nothing_where_the_host_takes_none(host: str) -> None:
    expected = _baseline()
    recorded = moved("prompt_cache_ttl")
    differing = []
    for key, (request, policy) in sorted(corpus().items()):
        built, sent, choice = _attempt(host, request, policy, ASK)
        assert choice is None, key
        assert sent is built, key
        name = f"{host}/{key}"
        if digest(sent) != expected_digest(
            "prompt_cache_ttl", name, expected[name], recorded
        ):
            differing.append(key)

    assert differing == []


def _strip_marker(body: dict[str, Any]) -> dict[str, Any]:
    extra = dict(body["extra_body"])
    assert extra.pop("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    out = dict(body)
    if extra:
        out["extra_body"] = extra
    else:
        del out["extra_body"]
    return out


def test_thirty_minutes_on_a_claude_model_adds_only_the_top_level_marker() -> None:
    expected = _baseline()
    recorded = moved("prompt_cache_ttl")
    checked = 0
    for key, (request, policy) in sorted(corpus().items()):
        _built, sent, choice = _attempt("open_router/claude", request, policy, ASK)
        assert choice is not None and choice.sent is not None, key
        name = f"open_router/claude/{key}"
        want = expected_digest("prompt_cache_ttl", name, expected[name], recorded)
        if isinstance(sent, str):
            assert digest(sent) == want, key
            continue
        assert digest(_strip_marker(sent)) == want, key
        checked += 1

    assert checked > 0


def test_thirty_minutes_on_azure_adds_only_the_retention_word() -> None:
    expected = _baseline()
    recorded = moved("prompt_cache_ttl")
    checked = 0
    for key, (request, policy) in sorted(corpus().items()):
        _built, sent, choice = _attempt("azure_openai", request, policy, ASK)
        assert choice is not None and choice.sent is not None, key
        name = f"azure_openai/{key}"
        want = expected_digest("prompt_cache_ttl", name, expected[name], recorded)
        if isinstance(sent, str):
            assert digest(sent) == want, key
            continue
        assert sent.pop("prompt_cache_retention") == "24h", key
        assert digest(sent) == want, key
        checked += 1

    assert checked > 0
