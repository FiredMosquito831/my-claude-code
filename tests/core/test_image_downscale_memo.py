"""How the shrink memo is reused, and the byte bound it never passes.

The memo's bound is not a configured number: it is the largest amount any
single request it served needed at once. These tests pin what that means for
the shapes of traffic MCC really sees -- the rungs of one chain, the turns of
one conversation that re-sends its screenshots, many unrelated requests in a
row -- and that the bytes held never pass the bound after a call returns.
"""

import base64
import io
from collections.abc import Callable
from typing import Any

import pytest
from PIL import Image, ImageDraw

from my_claude_code.core.anthropic import image_downscale
from my_claude_code.core.anthropic.image_downscale import (
    downscale_request_images,
    image_downscale_cache_info,
)
from my_claude_code.core.anthropic.models import MessagesRequest


def _picture(seed: int, width: int = 320, height: int = 240) -> str:
    image = Image.new("RGB", (width, height), (seed % 251, 40, 90))
    draw = ImageDraw.Draw(image)
    for row in range(0, height, 6):
        draw.line(
            (0, row, (row * seed) % width, row), fill=(200, seed % 199, row % 255)
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _request(*datas: str) -> MessagesRequest:
    """A fresh request, as a new turn arrives: new objects, same text."""
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        *(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "".join(list(data)),
                                },
                            }
                            for data in datas
                        ),
                    ],
                }
            ],
        }
    )


@pytest.fixture
def resize_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Every real resize the module asks for, by target size."""
    calls: list[tuple[int, int]] = []
    real: Callable[..., Any] = image_downscale.downscale

    def counting(raw: bytes, target: tuple[int, int], **kwargs: Any) -> Any:
        calls.append(target)
        return real(raw, target, **kwargs)

    monkeypatch.setattr(image_downscale, "downscale", counting)
    return calls


def _chain(request_datas: tuple[str, ...], families: tuple[str, ...]) -> None:
    """Build one request's rungs the way the router does: a copy per rung."""
    original = _request(*request_datas)
    for family in families:
        downscale_request_images(
            original.model_copy(deep=True),
            max_long_edge=200,
            family=family,
            jpeg_quality=0,
        )


def test_every_rung_after_the_first_is_a_hit(resize_calls) -> None:
    images = tuple(_picture(seed) for seed in range(6))
    families = ("anthropic", "unknown", "openai_patch", "gemini") * 3
    _chain(images, families)

    # Two resize policies in the chain (Anthropic's budget binds for anthropic
    # and unknown; only the pixel cap for the other two): six images each.
    assert len(resize_calls) == 6 * 2
    info = image_downscale_cache_info()
    assert info.misses == 6 * 2
    assert info.hits == 6 * (len(families) - 2)


def test_the_next_turn_resizes_only_the_new_screenshot(resize_calls) -> None:
    images = [_picture(seed) for seed in range(5)]
    families = ("anthropic",) * 4
    for turn in range(1, len(images) + 1):
        before = len(resize_calls)
        _chain(tuple(images[:turn]), families)
        assert len(resize_calls) - before == 1, f"turn {turn}"


def test_bytes_held_never_pass_the_bound_and_the_bound_is_one_request() -> None:
    needed_per_request: list[int] = []
    for request_number in range(40):
        datas = tuple(_picture(1000 + request_number * 5 + i) for i in range(5))
        _chain(datas, ("anthropic", "openai_patch"))
        info = image_downscale_cache_info()
        assert info.held_bytes <= info.bound_bytes
        # Every entry of the request just served is still there.
        before = info.misses
        _chain(datas, ("anthropic", "openai_patch"))
        assert image_downscale_cache_info().misses == before
        needed_per_request.append(info.held_bytes)

    info = image_downscale_cache_info()
    # 400 shrinks were made; the memo holds about one request's worth of them.
    assert info.misses == 40 * 5 * 2
    assert info.entries <= 2 * 5 * 2
    assert info.bound_bytes <= max(needed_per_request)


def test_a_conversation_nobody_uses_any_more_ages_out() -> None:
    old = tuple(_picture(seed) for seed in range(200, 204))
    new = tuple(_picture(seed) for seed in range(300, 306))
    _chain(old, ("anthropic",))
    _chain(new, ("anthropic",))
    _chain(new, ("anthropic",))
    misses = image_downscale_cache_info().misses
    _chain(new, ("anthropic",))
    assert image_downscale_cache_info().misses == misses
    # The old conversation is evicted once the new one needs the room.
    _chain(old, ("anthropic",))
    assert image_downscale_cache_info().misses > misses


def test_text_only_requests_and_the_off_switch_leave_the_memo_alone() -> None:
    images = tuple(_picture(seed) for seed in range(3))
    _chain(images, ("anthropic",))
    settled = image_downscale_cache_info()

    for _ in range(5):
        downscale_request_images(
            _request(), max_long_edge=200, family="anthropic", jpeg_quality=0
        )
        downscale_request_images(
            _request(*images), max_long_edge=0, family="anthropic", jpeg_quality=0
        )
    assert image_downscale_cache_info() == settled


def test_an_image_already_inside_the_budget_is_remembered_without_bytes() -> None:
    small = _picture(7, width=120, height=90)
    _chain((small,), ("anthropic",) * 3)
    info = image_downscale_cache_info()
    assert info.entries == 1
    assert info.misses == 1
    assert info.hits == 2
    # No pixels are kept for an image that leaves as it came.
    assert info.held_bytes < len(small)
