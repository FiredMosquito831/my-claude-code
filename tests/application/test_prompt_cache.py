"""What an operator asks a prompt cache to live, and what a host gets (7.104.0).

The adaptation rule (user decisions 2026-10-20 Q1/Q2): the shortest lifetime
the host accepts that is at least the minutes asked for, else the host's
longest -- applied only when an MCC setting asked. The precedence (Q5): model
row, provider row, tier, global, client. The tier follows routing's own rules
for ``REASONING_<TIER>``, which this file pins against the router itself.
"""

from typing import Any

import pytest

from my_claude_code.application import prompt_cache
from my_claude_code.application.prompt_cache import (
    ROUTE_PROMPT_CACHE_TTL_SETTINGS,
    adapt_prompt_cache_minutes,
    choose_prompt_cache_lifetime,
    resolve_prompt_cache_ask,
    route_prompt_cache_ask,
    route_tier_prompt_cache_setting,
    stamp_prompt_cache_ask,
)
from my_claude_code.application.routing import _ROUTE_SETTINGS, ModelRouter
from my_claude_code.config.prompt_cache_lifetimes import (
    ANTHROPIC_MESSAGES_LIFETIMES,
    PROMPT_CACHE_LIFETIME_FAMILIES,
    CacheLifetime,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.core.tier_refs import (
    TIER_PROMPT_CACHE_TTL_SETTINGS,
    TIER_REASONING_SETTINGS,
)

ANTHROPIC = PROMPT_CACHE_LIFETIME_FAMILIES[ANTHROPIC_MESSAGES_LIFETIMES]


def _request(model: str = "claude-opus-5-5") -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )


# -- the adaptation rule ---------------------------------------------------


@pytest.mark.parametrize("minutes", range(1, 61))
def test_every_minute_maps_to_the_shortest_anthropic_lifetime_at_least_that_long(
    minutes: int,
) -> None:
    sent = adapt_prompt_cache_minutes(minutes, ANTHROPIC.lifetimes)

    assert sent is not None
    assert sent.wire == ("5m" if minutes <= 5 else "1h")


def test_a_request_longer_than_every_lifetime_gets_the_longest() -> None:
    lifetimes = (CacheLifetime(5, "5m", "5 min", "1.25x"),)

    sent = adapt_prompt_cache_minutes(30, lifetimes)

    assert sent is not None and sent.wire == "5m"


def test_the_rule_does_not_depend_on_the_order_the_table_lists_lifetimes() -> None:
    reversed_lifetimes = tuple(reversed(ANTHROPIC.lifetimes))
    short = adapt_prompt_cache_minutes(3, reversed_lifetimes)
    long = adapt_prompt_cache_minutes(6, reversed_lifetimes)

    assert short is not None and short.wire == "5m"
    assert long is not None and long.wire == "1h"


def test_a_host_with_no_lifetimes_gets_nothing() -> None:
    assert adapt_prompt_cache_minutes(30, ()) is None


# -- precedence ------------------------------------------------------------


def test_nothing_set_anywhere_asks_for_nothing() -> None:
    assert resolve_prompt_cache_ask() is None
    assert resolve_prompt_cache_ask(tier=("PROMPT_CACHE_TTL_OPUS", "inherit")) is None
    assert resolve_prompt_cache_ask(global_value="client") is None


def test_the_levels_win_in_the_documented_order() -> None:
    tier = ("PROMPT_CACHE_TTL_OPUS", "20")

    assert resolve_prompt_cache_ask(
        model_row="3", provider_row="10", tier=tier, global_value="40"
    ) == PromptCacheAsk(3, "model_override")
    assert resolve_prompt_cache_ask(
        provider_row="10", tier=tier, global_value="40"
    ) == PromptCacheAsk(10, "provider_override")
    assert resolve_prompt_cache_ask(tier=tier, global_value="40") == PromptCacheAsk(
        20, "PROMPT_CACHE_TTL_OPUS"
    )
    assert resolve_prompt_cache_ask(
        tier=("PROMPT_CACHE_TTL_OPUS", "inherit"), global_value="40"
    ) == PromptCacheAsk(40, "PROMPT_CACHE_TTL")


def test_client_at_a_level_stops_the_levels_below_it() -> None:
    assert (
        resolve_prompt_cache_ask(
            tier=("PROMPT_CACHE_TTL_HAIKU", "client"), global_value="30"
        )
        is None
    )
    assert resolve_prompt_cache_ask(model_row="client", global_value="30") is None


def test_a_value_no_validator_produced_never_changes_bytes() -> None:
    assert resolve_prompt_cache_ask(global_value="thirty") is None


# -- the route decides the tier, exactly as it does for reasoning ---------


def test_the_keyword_table_is_routings_own_in_routings_order() -> None:
    expected = [
        (keyword, reasoning_attr.replace("reasoning_", "prompt_cache_ttl_"))
        for keyword, _model, reasoning_attr, _fallbacks in _ROUTE_SETTINGS
    ]

    assert list(ROUTE_PROMPT_CACHE_TTL_SETTINGS) == expected


def test_the_tier_table_is_the_reasoning_tier_table_both_ways() -> None:
    assert set(TIER_PROMPT_CACHE_TTL_SETTINGS) == set(TIER_REASONING_SETTINGS)
    for tier, reasoning_attr in TIER_REASONING_SETTINGS.items():
        assert TIER_PROMPT_CACHE_TTL_SETTINGS[tier] == reasoning_attr.replace(
            "reasoning_", "prompt_cache_ttl_"
        )


#: One distinct reasoning word per route setting, so the word routing picks
#: names the setting it read.
_WORD_BY_REASONING_ATTR: dict[str, str] = {
    "reasoning_mythos": "minimal",
    "reasoning_fable": "low",
    "reasoning_opus": "medium",
    "reasoning_sonnet": "high",
    "reasoning_haiku": "max",
}
_GLOBAL_WORD = "adaptive"

NAMES = (
    "claude-opus-5-5",
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-fable-5-1",
    "claude-fable-5.1",
    "claude-mythos-5-1",
    "CLAUDE-OPUS-4-8",
    "opus-haiku-blend",
    "mcc/cyber",
    "mcc/best",
    "mcc/good",
    "mcc/medium",
    "mcc/cheap",
    "mcc/vision",
    "anthropic_oauth/claude-opus-5-5",
    "open_router/anthropic/claude-sonnet-5",
    "gpt-6",
    "some-model",
)


@pytest.mark.parametrize("name", NAMES)
def test_a_name_reads_the_lifetime_setting_whose_reasoning_setting_routing_reads(
    name: str,
) -> None:
    settings = Settings.model_validate(
        {
            "REASONING_POLICY": _GLOBAL_WORD,
            **{attr.upper(): word for attr, word in _WORD_BY_REASONING_ATTR.items()},
        }
    )
    chosen = ModelRouter(settings).resolve(name).reasoning_preference
    by_word = {word: attr for attr, word in _WORD_BY_REASONING_ATTR.items()}
    reasoning_attr = by_word.get(chosen.value)
    expected = (
        None
        if reasoning_attr is None
        else reasoning_attr.replace("reasoning_", "prompt_cache_ttl_")
    )

    assert chosen in {
        ReasoningPreference(_GLOBAL_WORD),
        *map(ReasoningPreference, by_word),
    }
    assert route_tier_prompt_cache_setting(name) == expected


def test_a_tier_setting_wins_over_the_global_one_on_its_route() -> None:
    settings = Settings.model_validate(
        {"PROMPT_CACHE_TTL": "40", "PROMPT_CACHE_TTL_OPUS": "3"}
    )

    assert route_prompt_cache_ask(settings, "claude-opus-5-5") == PromptCacheAsk(
        3, "PROMPT_CACHE_TTL_OPUS"
    )
    assert route_prompt_cache_ask(settings, "claude-haiku-5-5") == PromptCacheAsk(
        40, "PROMPT_CACHE_TTL"
    )
    assert route_prompt_cache_ask(settings, "mcc/good") == PromptCacheAsk(
        3, "PROMPT_CACHE_TTL_OPUS"
    )
    assert route_prompt_cache_ask(
        settings, "anthropic_oauth/claude-opus-5-5"
    ) == PromptCacheAsk(40, "PROMPT_CACHE_TTL")


def test_nothing_set_answers_before_the_name_is_looked_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("looked up a route with nothing set")

    monkeypatch.setattr(prompt_cache, "route_tier_prompt_cache_setting", refuse)

    assert (
        route_prompt_cache_ask(Settings.model_validate({}), "claude-opus-5-5") is None
    )


# -- the stamp -------------------------------------------------------------


def test_nothing_set_stamps_nothing() -> None:
    request = _request()
    before = dict(request.__pydantic_private__ or {})

    stamp_prompt_cache_ask(Settings.model_validate({}), request)

    assert request.prompt_cache_ask is None
    assert dict(request.__pydantic_private__ or {}) == before


def test_the_stamp_rides_every_routed_copy_and_no_dump() -> None:
    request = _request()
    plain = request.model_dump()
    stamp_prompt_cache_ask(Settings.model_validate({"PROMPT_CACHE_TTL": "30"}), request)

    copied = request.model_copy(deep=True)

    assert copied.prompt_cache_ask == PromptCacheAsk(30, "PROMPT_CACHE_TTL")
    assert request.model_dump() == plain
    assert copied.model_dump(exclude_none=True) == request.model_dump(exclude_none=True)


def test_a_client_body_cannot_set_the_stamp() -> None:
    request = MessagesRequest.model_validate(
        {
            "model": "claude-opus-5-5",
            "messages": [{"role": "user", "content": "hi"}],
            "_prompt_cache_ask": {"minutes": 60, "source": "client"},
            "prompt_cache_ask": {"minutes": 60, "source": "client"},
        }
    )

    assert request.prompt_cache_ask is None


# -- one host's choice -----------------------------------------------------


def test_no_ask_or_no_family_is_no_choice() -> None:
    assert choose_prompt_cache_lifetime(None, ANTHROPIC) is None
    assert (
        choose_prompt_cache_lifetime(PromptCacheAsk(30, "PROMPT_CACHE_TTL"), None)
        is None
    )


def test_a_refused_lifetime_is_never_chosen_and_the_rest_adapt() -> None:
    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(30, "PROMPT_CACHE_TTL"), ANTHROPIC, refused={"1h"}
    )

    assert choice is not None
    assert choice.sent is not None and choice.sent.wire == "5m"
    assert choice.refused == ("1h",)
    assert choice.note() == {
        "requested": 30,
        "from": "PROMPT_CACHE_TTL",
        "sent": "5m",
        "host_lifetimes": ["5m", "1h"],
        "declared_for": "Anthropic Messages",
        "refused_by_host": ["1h"],
    }


def test_a_host_that_refused_every_lifetime_gets_none_and_says_why() -> None:
    choice = choose_prompt_cache_lifetime(
        PromptCacheAsk(3, "PROMPT_CACHE_TTL"), ANTHROPIC, refused={"5m", "1h"}
    )

    assert choice is not None
    assert choice.sent is None
    assert choice.note()["sent"] is None
