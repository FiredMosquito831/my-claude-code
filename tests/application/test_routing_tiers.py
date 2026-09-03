"""The five coding-agent tier aliases, and how they resolve.

Every test here answers one question an operator can ask out loud: "if OpenCode
picks Best, what actually serves it, and would Claude Code have got the same
thing?" The answers are only useful if they cannot drift, which is what pins
them here rather than in a docstring.
"""

from typing import Any

import pytest

from my_claude_code.application.routing import ModelRouter
from my_claude_code.application.tier_chains import (
    TIER_SOURCE_GLOBAL,
    TIER_SOURCE_OVERRIDE,
)
from my_claude_code.config.harness_tiers import HarnessTierOverride, HarnessTiers
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import Message, MessagesRequest
from my_claude_code.core.tier_refs import (
    TIER_ORDER,
    ModelTier,
    parse_tier_ref,
    tier_ref,
)

PRIMARY = "nvidia_nim/primary"
PRIMARY_FALLBACK = "open_router/primary-fallback"
OPUS = "open_router/opus"
SONNET = "open_router/sonnet"
HAIKU = "open_router/haiku"
VISION = "open_router/vision"
OVERRIDE = "commandcode/override"
OVERRIDE_FALLBACK = "commandcode/override-fallback"


def _settings(**overrides: Any) -> Settings:
    # Aliased fields have to be named by their alias; ``model`` has none.
    values: dict[str, Any] = {
        "model": PRIMARY,
        "MODEL_FALLBACKS": PRIMARY_FALLBACK,
        "MODEL_OPUS": OPUS,
        "MODEL_SONNET": SONNET,
        "MODEL_HAIKU": HAIKU,
        "MODEL_VISION": VISION,
    }
    values.update(overrides)
    return Settings(**values)


def _bare_settings(**values: Any) -> Settings:
    """Settings with no tier routes at all, for the collapse-onto-MODEL case."""

    return Settings(**values)


def _router(settings: Settings, tiers: HarnessTiers | None = None) -> ModelRouter:
    table = tiers if tiers is not None else HarnessTiers()
    return ModelRouter(settings, harness_tiers=lambda: table)


def _refs(router: ModelRouter, name: str, harness: str | None = None) -> list[str]:
    return [
        resolved.provider_model_ref
        for resolved in router.resolve_chain(name, harness=harness)
    ]


def _request(model: str, content: Any = "hi") -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content=content)],
    )


def test_every_tier_alias_resolves_to_its_global_chain() -> None:
    """The default state of all five, and the whole point of pointer semantics.

    A tier is a name for one of MCC's own routes, so out of the box it answers
    exactly what Claude Code's alias for the same route answers.
    """

    router = _router(_settings())

    assert _refs(router, "mcc/best") == [PRIMARY, PRIMARY_FALLBACK]
    assert _refs(router, "mcc/good") == [OPUS]
    assert _refs(router, "mcc/medium") == [SONNET]
    assert _refs(router, "mcc/cheap") == [HAIKU]
    assert _refs(router, "mcc/vision") == [VISION]


def test_a_tier_alias_is_accepted_in_both_wire_spellings() -> None:
    """The fleet is genuinely split and the router must not care which half sent.

    Cline, Crush, Droid, Gemini CLI, Qwen and Aider put the gateway id on the
    wire; Codex, Command Code, OpenCode, Pi and Kimi put the bare ref.
    """

    router = _router(_settings())

    for tier in TIER_ORDER:
        bare = tier_ref(tier)
        assert _refs(router, bare) == _refs(router, f"anthropic/{bare}")
        assert parse_tier_ref(f"anthropic/{bare}") is tier


def test_an_unset_tier_falls_back_to_MODEL_like_the_claude_alias_does() -> None:
    """The collapse, pinned rather than hidden.

    On a default install every MODEL_<TIER> is blank, so all five tiers point at
    MODEL -- primary, fallbacks and pause list together. Inventing a different
    model for an unset tier would be MCC choosing one for the operator.
    """

    settings = _bare_settings(model=PRIMARY, MODEL_FALLBACKS=PRIMARY_FALLBACK)
    router = _router(settings)

    for tier in TIER_ORDER:
        assert _refs(router, tier_ref(tier)) == [PRIMARY, PRIMARY_FALLBACK]
    # And identically to what Claude Code already gets for the same route.
    assert _refs(router, "claude-opus-5") == [PRIMARY, PRIMARY_FALLBACK]


def test_a_harness_override_leads_the_chain_for_that_harness_only() -> None:
    """One agent's chain must never move another agent's, or Claude Code's."""

    tiers = HarnessTiers(
        harnesses={
            "opencode": {
                "best": HarnessTierOverride(
                    model=OVERRIDE, fallbacks=(OVERRIDE_FALLBACK,)
                )
            }
        }
    )
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/best", "opencode") == [OVERRIDE, OVERRIDE_FALLBACK]
    assert _refs(router, "mcc/best", "crush") == [PRIMARY, PRIMARY_FALLBACK]
    assert _refs(router, "claude-fable-5", "opencode") == [PRIMARY, PRIMARY_FALLBACK]


def test_an_override_naming_a_model_owns_its_whole_chain() -> None:
    """Appending the global fallbacks would attach models nobody listed.

    The rail on the dashboard says "this agent's own chain"; quietly extending
    it with entries from another route would make that sentence false.
    """

    tiers = HarnessTiers(
        harnesses={"opencode": {"best": HarnessTierOverride(model=OVERRIDE)}}
    )
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/best", "opencode") == [OVERRIDE]


def test_an_override_without_a_model_keeps_the_global_primary_and_its_own_fallbacks() -> (
    None
):
    """The middle state, and the reason the file exists at all.

    "Keep whatever MODEL_SONNET points at, but if it fails, try mine" cannot be
    said by a settings key, because a settings key is global by construction.
    """

    tiers = HarnessTiers(
        harnesses={
            "opencode": {"medium": HarnessTierOverride(fallbacks=(OVERRIDE_FALLBACK,))}
        }
    )
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/medium", "opencode") == [SONNET, OVERRIDE_FALLBACK]
    assert _refs(router, "mcc/medium", "crush") == [SONNET]


def test_an_empty_override_entry_still_means_same_as_global() -> None:
    """The dashboard writes one the moment Override is pressed.

    A store that treated "present but says nothing" as an override would move
    the agent onto whatever the empty rail resolved to, which is not what the
    operator asked for by opening an editor.
    """

    tiers = HarnessTiers(harnesses={"opencode": {"best": HarnessTierOverride()}})
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/best", "opencode") == [PRIMARY, PRIMARY_FALLBACK]


def test_an_unknown_harness_resolves_the_global_chain() -> None:
    """No identity is not an error. A raw curl still gets a working tier."""

    tiers = HarnessTiers(
        harnesses={"opencode": {"best": HarnessTierOverride(model=OVERRIDE)}}
    )
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/best", None) == [PRIMARY, PRIMARY_FALLBACK]
    assert _refs(router, "mcc/best", "unknown") == [PRIMARY, PRIMARY_FALLBACK]


def test_a_bad_override_entry_is_skipped_not_fatal() -> None:
    """One unusable entry must not take a whole agent's tier down.

    Exactly what ``resolve_chain`` already does for a fallback naming a provider
    this install has never heard of.
    """

    tiers = HarnessTiers(
        harnesses={
            "opencode": {
                "best": HarnessTierOverride(
                    model="no_such_provider/model", fallbacks=(OVERRIDE,)
                )
            }
        }
    )
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/best", "opencode") == [OVERRIDE]


def test_a_tier_whose_every_entry_is_unusable_still_answers() -> None:
    """An alias that raises is worse than one that answers with MODEL."""

    tiers = HarnessTiers(
        harnesses={
            "opencode": {"best": HarnessTierOverride(model="no_such_provider/model")}
        }
    )
    router = _router(_settings(), tiers)

    assert _refs(router, "mcc/best", "opencode") == [PRIMARY]


def test_a_paused_ref_on_mcc_cheap_names_MODEL_HAIKU_PAUSED() -> None:
    """Pause is resolved by tier name, not by matching the model string.

    The router's ``_matched_route`` is a substring match over the Claude alias
    names; ``cheap`` contains none of them, so without an explicit mapping the
    Haiku pause list would never be consulted and the all-paused message would
    name the wrong setting.
    """

    settings = _settings(MODEL_HAIKU_PAUSED=HAIKU)
    plan = _router(settings).resolve_messages_plan(_request("mcc/cheap"))

    assert plan.paused_refs == frozenset({HAIKU})
    assert plan.paused_env_var == "MODEL_HAIKU_PAUSED"


def test_a_paused_ref_on_an_overridden_tier_names_the_file_not_an_env_var() -> None:
    """A per-agent pause has no env var, and must not be told to change one.

    Writing MODEL_HAIKU_PAUSED would switch the ref off for every other agent
    too, which is the opposite of what a per-agent override means.
    """

    tiers = HarnessTiers(
        harnesses={
            "opencode": {
                "cheap": HarnessTierOverride(
                    model=OVERRIDE, fallbacks=(OVERRIDE_FALLBACK,), paused=(OVERRIDE,)
                )
            }
        }
    )
    plan = _router(_settings(), tiers).resolve_messages_plan(
        _request("mcc/cheap"), harness="opencode"
    )

    assert plan.paused_refs == frozenset({OVERRIDE})
    assert plan.paused_env_var == "harness_tiers.json:opencode.cheap.paused"
    # The paused entry keeps its place in the chain: it is skipped, not hidden.
    assert plan.model_refs() == (OVERRIDE, OVERRIDE_FALLBACK)


def test_a_global_pause_does_not_reach_an_agent_that_overrides_the_tier() -> None:
    """The override owns its chain, so it owns which of it is switched off."""

    tiers = HarnessTiers(
        harnesses={"opencode": {"cheap": HarnessTierOverride(model=HAIKU)}}
    )
    plan = _router(_settings(MODEL_HAIKU_PAUSED=HAIKU), tiers).resolve_messages_plan(
        _request("mcc/cheap"), harness="opencode"
    )

    assert plan.paused_refs == frozenset()


def test_mcc_vision_is_not_double_diverted_by_the_vision_adapter() -> None:
    """A client naming the vision tier is already on the vision rail.

    The adapter diverts only when the head of the chain is *known* to reject
    images, and the head here is MODEL_VISION's own -- so the guard returns
    early and nothing moves. Without this test the adapter could start
    rewriting a route the client asked for by name.
    """

    image = [
        {"type": "text", "text": "what is this"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
        },
    ]
    router = ModelRouter(
        _settings(),
        vision_lookup=lambda provider, model: model == "vision",
        harness_tiers=HarnessTiers,
    )

    plan = router.resolve_messages_plan(_request("mcc/vision", image))

    assert plan.model_refs() == (VISION,)
    assert plan.diverted_from is None
    assert plan.diversion is None


def test_a_tier_alias_can_never_point_at_another_tier() -> None:
    """A tier resolving to a tier is a loop the file that caused it cannot show."""

    from my_claude_code.config.harness_tiers import is_valid_tier_override_ref

    assert not is_valid_tier_override_ref("mcc/best")
    assert not is_valid_tier_override_ref("MCC/cheap")
    assert is_valid_tier_override_ref("open_router/mcc-thing")


def test_tier_ids_do_not_collide_with_the_substring_matcher() -> None:
    """``_matched_route`` matches any name *containing* a route word.

    ``best``/``good``/``medium``/``cheap``/``vision`` share no substring with
    ``fable``/``opus``/``haiku``/``sonnet``, so the old matcher cannot capture a
    tier and the new parser cannot capture a Claude alias. Both directions,
    because only one of the two failures would be loud.
    """

    for tier in TIER_ORDER:
        assert ModelRouter._matched_route(tier_ref(tier)) is None
    for alias in (
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "nvidia_nim/some/model",
        "anthropic/nvidia_nim/some/model",
    ):
        assert parse_tier_ref(alias) is None


def test_the_plan_records_which_chain_a_tier_request_used() -> None:
    """``requested_model`` and ``resolved_model`` cannot answer this between them.

    An override naming the same ref as the global chain is indistinguishable
    from no override at all, and "did my override fire?" is the question the
    request log has to be able to answer.
    """

    tiers = HarnessTiers(
        harnesses={"opencode": {"best": HarnessTierOverride(model=OVERRIDE)}}
    )
    router = _router(_settings(), tiers)

    overridden = router.resolve_messages_plan(_request("mcc/best"), harness="opencode")
    assert overridden.tier_route is not None
    assert overridden.tier_route.tier is ModelTier.BEST
    assert overridden.tier_route.harness == "opencode"
    assert overridden.tier_route.source == TIER_SOURCE_OVERRIDE

    inherited = router.resolve_messages_plan(_request("mcc/best"), harness="crush")
    assert inherited.tier_route is not None
    assert inherited.tier_route.source == TIER_SOURCE_GLOBAL

    assert router.resolve_messages_plan(_request("claude-opus-5")).tier_route is None


def test_the_request_log_row_names_the_alias_and_the_ref_it_served() -> None:
    """``original_model`` stays what the client sent.

    The row has to be findable by the name the operator configured in the agent,
    not by whatever that name happened to resolve to on the day.
    """

    router = _router(_settings())
    resolved = router.resolve("mcc/medium")

    assert resolved.original_model == "mcc/medium"
    assert resolved.provider_model_ref == SONNET
    assert resolved.provider_id == "open_router"


@pytest.mark.parametrize(
    ("tier", "reasoning_key", "expected"),
    [
        ("mcc/good", "REASONING_OPUS", "off"),
        ("mcc/medium", "REASONING_SONNET", "off"),
        ("mcc/cheap", "REASONING_HAIKU", "off"),
    ],
)
def test_a_tier_inherits_the_reasoning_of_the_route_it_names(
    tier: str, reasoning_key: str, expected: str
) -> None:
    """The tier is the route, so it carries the route's reasoning preference."""

    router = _router(_settings(**{reasoning_key: expected}))

    assert router.resolve(tier).reasoning_preference.value == expected
