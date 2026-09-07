"""The outbound downscale, where it runs and what it leaves behind.

The point of every test here is *where*: the resize happens once per attempt,
on the router's own deep copy, so the client's request is never mutated and no
dialect converter ever learns about a setting.
"""

import base64
import io

import pytest
from PIL import Image

from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.image_downscale import resize_target
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.request_modalities import request_image_inputs


def png_b64(width: int, height: int, mode: str = "RGB") -> str:
    colour: str | tuple[int, int, int, int] = (
        "red" if mode == "RGB" else (0, 0, 255, 90)
    )
    image = Image.new(mode, (width, height))
    image.paste(colour, (0, 0, width, height))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def image_request(data: str, media_type: str = "image/png") -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "claude-3-opus",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        },
                    ],
                }
            ],
        }
    )


def dimensions_of(request: MessagesRequest) -> tuple[int, int]:
    image = request_image_inputs(request)[0]
    assert image.data is not None
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as decoded:
        return decoded.size


@pytest.fixture
def settings():
    settings = Settings()
    settings.model = "anthropic/claude-sonnet-4-5"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.reasoning_policy = ReasoningPreference.CLIENT
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def test_a_4k_screenshot_is_shrunk_to_the_anthropic_budget(settings):
    settings.image_max_long_edge = 1568
    request = image_request(png_b64(3840, 2160))
    routed = ModelRouter(settings).resolve_messages_request(request)

    assert routed.image_token_family == "anthropic"
    assert dimensions_of(routed.request) == (1456, 819)
    assert len(routed.image_resizes) == 1
    assert routed.image_resizes[0].before_width == 3840
    assert routed.image_resizes[0].after_width == 1456


def test_downscale_off_sends_the_original_bytes(settings):
    settings.image_max_long_edge = 0
    original = png_b64(3840, 2160)
    routed = ModelRouter(settings).resolve_messages_request(image_request(original))

    assert routed.image_resizes == ()
    assert request_image_inputs(routed.request)[0].data == original


def test_downscale_applies_once_per_attempt_on_the_copy(settings):
    """The client's own request is never mutated. This is the whole design."""
    settings.image_max_long_edge = 1568
    original = png_b64(3840, 2160)
    request = image_request(original)
    routed = ModelRouter(settings).resolve_messages_request(request)

    assert dimensions_of(request) == (3840, 2160)
    assert request_image_inputs(request)[0].data == original
    assert dimensions_of(routed.request) == (1456, 819)


def test_an_image_already_inside_the_budget_is_untouched(settings):
    settings.image_max_long_edge = 1568
    original = png_b64(1384, 835)
    routed = ModelRouter(settings).resolve_messages_request(image_request(original))

    assert routed.image_resizes == ()
    assert request_image_inputs(routed.request)[0].data == original


def test_unreadable_image_is_sent_untouched_and_the_request_still_routes(settings):
    settings.image_max_long_edge = 1568
    junk = base64.b64encode(b"this is not a picture").decode("ascii")
    routed = ModelRouter(settings).resolve_messages_request(image_request(junk))

    assert routed.image_resizes == ()
    assert request_image_inputs(routed.request)[0].data == junk


def test_alpha_survives_when_jpeg_reencoding_is_off(settings):
    settings.image_max_long_edge = 1568
    settings.image_jpeg_quality = 0
    routed = ModelRouter(settings).resolve_messages_request(
        image_request(png_b64(3000, 2000, mode="RGBA"))
    )

    image = request_image_inputs(routed.request)[0]
    assert image.media_type == "image/png"
    assert image.data is not None
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as decoded:
        assert decoded.mode == "RGBA"


def test_resize_target_respects_the_family_token_cap():
    """1456x819, not 1568x882, because 1568x882 would be 1792 tokens."""
    assert resize_target(1920, 1080, max_long_edge=1568, family="anthropic") == (
        1456,
        819,
    )
    # A family that publishes no resize-to-a-token-budget rule gets the pixel
    # cap and nothing else.
    assert resize_target(1920, 1080, max_long_edge=1568, family="openai_patch") == (
        1568,
        882,
    )


def test_resize_target_is_off_at_zero():
    assert resize_target(3840, 2160, max_long_edge=0, family="anthropic") == (
        3840,
        2160,
    )
