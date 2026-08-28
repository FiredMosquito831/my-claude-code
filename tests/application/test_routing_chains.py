"""Fallback-chain and vision-adapter routing contracts."""

import pytest

from my_claude_code.application.routing import ModelRouter, RouteDiversion
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest

_IMAGE_BLOCK: dict[str, object] = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
}


@pytest.fixture
def settings() -> Settings:
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.model_fallbacks = None
    settings.model_fable_fallbacks = None
    settings.model_opus_fallbacks = None
    settings.model_sonnet_fallbacks = None
    settings.model_haiku_fallbacks = None
    settings.model_vision = None
    settings.reasoning_policy = ReasoningPreference.CLIENT
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def _request(*, image: bool = False, model: str = "claude-opus-4") -> MessagesRequest:
    content: list[dict[str, object]] = [{"type": "text", "text": "describe this"}]
    if image:
        content.append(_IMAGE_BLOCK)
    return MessagesRequest.model_validate(
        {"model": model, "messages": [{"role": "user", "content": content}]}
    )


def _refs(router: ModelRouter, request: MessagesRequest) -> tuple[str, ...]:
    return router.resolve_messages_plan(request).model_refs()


def test_a_route_without_a_chain_yields_a_single_attempt(settings):
    assert _refs(ModelRouter(settings), _request()) == ("nvidia_nim/fallback-model",)


def test_a_route_without_an_override_uses_the_root_chain(settings):
    settings.model_fallbacks = "cerebras/one,groq/two"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/fallback-model",
        "cerebras/one",
        "groq/two",
    )


def test_a_route_with_an_override_uses_its_own_chain_only(settings):
    settings.model_fallbacks = "cerebras/root-fallback"
    settings.model_opus = "open_router/opus-primary"
    settings.model_opus_fallbacks = "groq/opus-second"

    assert _refs(ModelRouter(settings), _request(model="claude-opus-4")) == (
        "open_router/opus-primary",
        "groq/opus-second",
    )
    # A route with no override of its own still falls back to the root chain.
    assert _refs(ModelRouter(settings), _request(model="claude-sonnet-4")) == (
        "nvidia_nim/fallback-model",
        "cerebras/root-fallback",
    )


def test_a_route_chain_is_orphaned_when_its_override_is_cleared(settings):
    """An override and the chain beside it stand or fall together.

    A route reads its own fallbacks only when it has its own primary, so
    clearing MODEL_OPUS silently retires MODEL_OPUS_FALLBACKS: the tier goes
    back to the root model *and* the root chain, with no error and nothing in
    the log to say the chain sitting next to it stopped being consulted.

    Pinned rather than fixed, because it is the behaviour the routing page's
    reorder arrows are built around -- the primary can only ever swap with
    fallback 1, never move down into an empty slot, so no button press can put
    a route into this state. Anyone who changes this should change that too.
    """
    settings.model_fallbacks = "cerebras/root-fallback"
    settings.model_opus = None
    settings.model_opus_fallbacks = "groq/opus-second"

    assert _refs(ModelRouter(settings), _request(model="claude-opus-4")) == (
        "nvidia_nim/fallback-model",
        "cerebras/root-fallback",
    )


def test_an_explicit_provider_model_request_is_never_overridden(settings):
    settings.model_fallbacks = "cerebras/one"

    assert _refs(ModelRouter(settings), _request(model="groq/exact/model")) == (
        "groq/exact/model",
    )


def test_a_chain_entry_naming_an_unknown_provider_is_skipped(settings):
    # Settings validation rejects these, but a custom provider can be removed
    # from the registry after the chain was persisted.
    settings.model_fallbacks = "not_a_provider/x,cerebras/real"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/fallback-model",
        "cerebras/real",
    )


def test_a_duplicate_of_the_primary_is_dropped_from_the_chain(settings):
    settings.model_fallbacks = "nvidia_nim/fallback-model,cerebras/real"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/fallback-model",
        "cerebras/real",
    )


def test_every_attempt_keeps_the_route_reasoning_preference(settings):
    settings.model_opus = "open_router/primary"
    settings.model_opus_fallbacks = "groq/second"
    settings.reasoning_opus = ReasoningPreference.OFF

    plan = ModelRouter(settings).resolve_messages_plan(_request(model="claude-opus-4"))

    assert [attempt.resolved.reasoning_preference for attempt in plan.attempts] == [
        ReasoningPreference.OFF,
        ReasoningPreference.OFF,
    ]


# ------------------------------------------------------------------- vision


def _blind(*blind_models: str):
    def lookup(provider_id: str, model_id: str) -> bool | None:
        if f"{provider_id}/{model_id}" in blind_models:
            return False
        return None

    return lookup


def test_an_image_reroutes_to_the_vision_adapter_when_the_route_is_blind(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=True)) == ("open_router/sees-images",)


def test_an_image_a_tool_returned_reroutes_the_same_way(settings):
    """A screenshot from Read or an MCP tool arrives inside its tool_result.

    Scanning only top-level blocks made these invisible, so the request went
    to a model documented not to accept images and analytics recorded an
    ordinary route.
    """
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))
    request = MessagesRequest.model_validate(
        {
            "model": "claude-opus-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [_IMAGE_BLOCK],
                        }
                    ],
                }
            ],
        }
    )

    plan = router.resolve_messages_plan(request)
    assert plan.model_refs() == ("open_router/sees-images",)
    assert plan.diversion is RouteDiversion.VISION
    assert plan.diverted_from == "nvidia_nim/fallback-model"


def test_a_pdf_document_reroutes_like_an_image(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))
    request = MessagesRequest.model_validate(
        {
            "model": "claude-opus-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "JVBERi0=",
                            },
                        }
                    ],
                }
            ],
        }
    )

    assert _refs(router, request) == ("open_router/sees-images",)


def test_a_text_only_request_never_reaches_the_vision_adapter(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=False)) == ("nvidia_nim/fallback-model",)


def test_unknown_vision_capability_leaves_the_route_alone(settings):
    """Most providers publish no modality metadata; silence is not a refusal."""
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind())

    assert _refs(router, _request(image=True)) == ("nvidia_nim/fallback-model",)


def test_no_vision_adapter_configured_leaves_the_route_alone(settings):
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=True)) == ("nvidia_nim/fallback-model",)


def test_an_image_with_nowhere_to_go_is_recorded_as_unavailable(settings):
    """Nothing moved, and that is exactly what has to be visible.

    An image sent to a route where every model is known blind used to look
    identical to an ordinary request in the log.
    """
    settings.model_fallbacks = "groq/also-blind"
    router = ModelRouter(
        settings,
        vision_lookup=_blind("nvidia_nim/fallback-model", "groq/also-blind"),
    )

    plan = router.resolve_messages_plan(_request(image=True))
    assert plan.model_refs() == ("nvidia_nim/fallback-model", "groq/also-blind")
    assert plan.diversion is RouteDiversion.VISION_UNAVAILABLE
    # Nothing was replaced, so there is nothing it was diverted from.
    assert plan.diverted_from is None


def test_a_text_request_on_a_blind_route_records_no_diversion(settings):
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    plan = router.resolve_messages_plan(_request(image=False))
    assert plan.diversion is None


def test_blind_fallbacks_are_dropped_from_a_diverted_chain(settings):
    settings.model_fallbacks = "groq/also-blind,cerebras/unknown"
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(
        settings, vision_lookup=_blind("nvidia_nim/fallback-model", "groq/also-blind")
    )

    assert _refs(router, _request(image=True)) == (
        "open_router/sees-images",
        "cerebras/unknown",
    )


def _sight(capability: dict[str, bool]):
    """Vision lookup with explicit per-ref answers; unlisted refs stay unknown."""

    def lookup(provider_id: str, model_id: str) -> bool | None:
        return capability.get(f"{provider_id}/{model_id}")

    return lookup


def test_a_sighted_fallback_leads_when_no_vision_adapter_is_configured(settings):
    """A chain member that can see beats a model documented to reject images.

    Without this the image went to the known-blind primary, which either fails
    or -- worse -- answers about an image it never received.
    """
    settings.model_fallbacks = "groq/also-blind,cerebras/sees-images"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {
                "nvidia_nim/fallback-model": False,
                "groq/also-blind": False,
                "cerebras/sees-images": True,
            }
        ),
    )

    assert _refs(router, _request(image=True)) == ("cerebras/sees-images",)


def test_a_text_request_keeps_the_blind_models_in_the_chain(settings):
    settings.model_fallbacks = "groq/also-blind,cerebras/sees-images"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {
                "nvidia_nim/fallback-model": False,
                "groq/also-blind": False,
                "cerebras/sees-images": True,
            }
        ),
    )

    assert _refs(router, _request(image=False)) == (
        "nvidia_nim/fallback-model",
        "groq/also-blind",
        "cerebras/sees-images",
    )


def test_an_image_keeps_the_whole_route_when_nothing_is_known_to_see(settings):
    """Every candidate is blind: leave the route intact rather than route nowhere.

    Dropping the blind entries here would leave an empty plan, which is worse
    than letting the request fail against the model the user actually chose.
    """
    settings.model_fallbacks = "groq/also-blind"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {"nvidia_nim/fallback-model": False, "groq/also-blind": False}
        ),
    )

    assert _refs(router, _request(image=True)) == (
        "nvidia_nim/fallback-model",
        "groq/also-blind",
    )


def test_a_plan_records_the_vision_diversion_it_made(settings):
    """Without this the log cannot tell a diversion from an ordinary route."""
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    plan = router.resolve_messages_plan(_request(image=True))

    assert plan.diversion is RouteDiversion.VISION
    assert plan.diverted_from == "nvidia_nim/fallback-model"
    assert plan.model_refs() == ("open_router/sees-images",)


def test_an_undiverted_plan_records_no_diversion(settings):
    settings.model_vision = "open_router/sees-images"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    plan = router.resolve_messages_plan(_request(image=False))

    assert plan.diversion is None
    assert plan.diverted_from is None


def test_a_sighted_chain_promotion_is_recorded_as_a_diversion(settings):
    """Promoting a sighted fallback also replaces the head of the chain."""
    settings.model_fallbacks = "cerebras/sees-images"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {"nvidia_nim/fallback-model": False, "cerebras/sees-images": True}
        ),
    )

    plan = router.resolve_messages_plan(_request(image=True))

    assert plan.diversion is RouteDiversion.VISION
    assert plan.diverted_from == "nvidia_nim/fallback-model"


def test_the_vision_adapter_has_its_own_fallback_chain(settings):
    """One unreachable vision model must not lose every image on the machine."""
    settings.model_vision = "open_router/sees-images"
    settings.model_vision_fallbacks = "groq/backup-eyes,cerebras/last-eyes"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=True)) == (
        "open_router/sees-images",
        "groq/backup-eyes",
        "cerebras/last-eyes",
    )


def test_the_vision_chain_leads_the_routes_own_sighted_fallbacks(settings):
    settings.model_fallbacks = "cerebras/route-eyes"
    settings.model_vision = "open_router/sees-images"
    settings.model_vision_fallbacks = "groq/backup-eyes"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=True)) == (
        "open_router/sees-images",
        "groq/backup-eyes",
        "cerebras/route-eyes",
    )


def test_a_blind_entry_is_dropped_from_the_vision_chain(settings):
    """A blind model in a *vision* chain is a mistake, not a preference."""
    settings.model_vision = "open_router/sees-images"
    settings.model_vision_fallbacks = "groq/also-blind,cerebras/last-eyes"
    router = ModelRouter(
        settings,
        vision_lookup=_sight(
            {
                "nvidia_nim/fallback-model": False,
                "groq/also-blind": False,
                "open_router/sees-images": True,
            }
        ),
    )

    assert _refs(router, _request(image=True)) == (
        "open_router/sees-images",
        "cerebras/last-eyes",
    )


def test_a_text_request_never_sees_the_vision_chain(settings):
    settings.model_vision = "open_router/sees-images"
    settings.model_vision_fallbacks = "groq/backup-eyes"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    assert _refs(router, _request(image=False)) == ("nvidia_nim/fallback-model",)


def test_the_vision_chain_is_recorded_as_one_diversion(settings):
    settings.model_vision = "open_router/sees-images"
    settings.model_vision_fallbacks = "groq/backup-eyes"
    router = ModelRouter(settings, vision_lookup=_blind("nvidia_nim/fallback-model"))

    plan = router.resolve_messages_plan(_request(image=True))

    assert plan.diversion is RouteDiversion.VISION
    assert plan.diverted_from == "nvidia_nim/fallback-model"
    assert plan.has_fallbacks


def test_visibility_patterns_never_change_a_resolved_route(settings):
    """Hide-only: the visibility lists are a presentation filter, not a block.

    A model can be hidden from `/v1/models` and from the Admin pickers and
    still be named by MODEL or by a fallback chain. It must keep routing. The
    alternative -- refusing to route a hidden model -- would let one glob
    silently break a working chain, and the breakage would surface as an
    outage far away from the setting that caused it.
    """
    settings.model = "nvidia_nim/thinkingmachines/inkling"
    settings.model_fallbacks = (
        "nous_portal/tencent/hy3:free,commandcode/minimax/minimax-m3-free"
    )
    settings.model_visibility_allow = "nothing-matches-this/*"
    settings.model_visibility_deny = "*"

    assert _refs(ModelRouter(settings), _request()) == (
        "nvidia_nim/thinkingmachines/inkling",
        "nous_portal/tencent/hy3:free",
        "commandcode/minimax/minimax-m3-free",
    )
