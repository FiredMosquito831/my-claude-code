"""A route chain is built rung by rung, as rungs are reached.

Until 6.62.0 ``resolve_messages_plan`` routed every rung of the chain before
the first attempt was made. Routing a rung deep-copies the request, re-derives
its image delivery, runs a PIL pass to shrink oversized pictures to that host's
billing family, and walks the models.dev ladder for reasoning -- so a
three-rung chain carrying an image paid three PIL passes and three ladder walks
to answer a request that rung one almost always answers.
"""

import base64
import io

import pytest
from PIL import Image

from my_claude_code.application.routing import LazyRouteChain, ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest


@pytest.fixture
def settings() -> Settings:
    settings = Settings()
    settings.model = "nvidia_nim/rung-one"
    settings.model_fallbacks = "nvidia_nim/rung-two,nvidia_nim/rung-three"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
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
    # The downscale is what makes an unused rung expensive; leave it on.
    settings.image_max_long_edge = 64
    return settings


def _image_request() -> MessagesRequest:
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (10, 20, 30)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": "what is this"},
                    ],
                }
            ],
        }
    )


def _count_downscales(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch
) -> list[int]:
    calls = [0]
    real = ModelRouter._downscale_images

    def counting(self, routed, family, delivery):
        calls[0] += 1
        return real(self, routed, family, delivery)

    monkeypatch.setattr(ModelRouter, "_downscale_images", counting)
    return calls


def test_only_first_rung_is_built_eagerly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A three-rung chain with an image performs exactly one downscale."""

    router = ModelRouter(settings)
    calls = _count_downscales(router, monkeypatch)

    plan = router.resolve_messages_plan(_image_request())

    assert len(plan.attempts) == 3
    assert calls[0] == 0

    assert plan.primary.resolved.provider_model == "rung-one"
    assert calls[0] == 1


def test_a_fallback_is_built_when_it_is_reached(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = ModelRouter(settings)
    calls = _count_downscales(router, monkeypatch)

    plan = router.resolve_messages_plan(_image_request())
    assert plan.attempts[0].resolved.provider_model == "rung-one"
    assert plan.attempts[1].resolved.provider_model == "rung-two"

    assert calls[0] == 2

    # And a rung is routed once, however often it is asked for.
    for _ in range(5):
        assert plan.attempts[1].resolved.provider_model == "rung-two"
    assert calls[0] == 2


def test_the_route_is_answerable_without_routing_anything(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the health registry, the pause list and the ledger read is free.

    This is the property that makes the laziness worth having: every reader on
    the hot path asks about the chain, not about the routed requests.
    """

    router = ModelRouter(settings)
    calls = _count_downscales(router, monkeypatch)

    plan = router.resolve_messages_plan(_image_request())

    assert plan.model_refs() == (
        "nvidia_nim/rung-one",
        "nvidia_nim/rung-two",
        "nvidia_nim/rung-three",
    )
    assert [resolved.provider_model for resolved in plan.resolved_models()] == [
        "rung-one",
        "rung-two",
        "rung-three",
    ]
    assert plan.has_fallbacks is True
    assert len(plan.attempts) == 3
    assert calls[0] == 0


def test_a_lazy_chain_iterates_and_slices_like_the_tuple_it_replaced(
    settings: Settings,
) -> None:
    router = ModelRouter(settings)
    plan = router.resolve_messages_plan(_image_request())
    attempts = plan.attempts
    assert isinstance(attempts, LazyRouteChain)

    assert [attempt.resolved.provider_model for attempt in attempts] == [
        "rung-one",
        "rung-two",
        "rung-three",
    ]
    assert attempts[-1].resolved.provider_model == "rung-three"
    assert [a.resolved.provider_model for a in attempts[1:]] == [
        "rung-two",
        "rung-three",
    ]
    with pytest.raises(IndexError):
        attempts[3]
