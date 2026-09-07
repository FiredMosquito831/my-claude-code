"""Shrink an outbound image before it is sent, in place, once per attempt.

Where this runs matters more than what it does. It is called from
``application.routing`` on the per-attempt deep copy the router already takes,
beside ``_resolve_image_delivery`` and for the reason that function's docstring
already states: the converter "is handed a message list and nothing else, and
threading a capability lookup down into every dialect module would put model
metadata where none belongs". A resize is the same kind of decision -- it needs
the destination's billing family, which is model metadata -- so it belongs in
the same place. Doing it there also means all four dialect converters are
handed pre-shrunk data for free, and a chain that falls back from one host to
another re-derives the size for each rung, which one up-front mutation could
not.

Nothing here raises and nothing here can fail a request. An image the decoder
cannot read is left exactly as the client sent it.
"""

import base64
from dataclasses import dataclass
from typing import Any

from loguru import logger

from my_claude_code.core.image_geometry import decode_base64, downscale, raw_dimensions

from .image_tokens import (
    ANTHROPIC_MAX_TOKENS,
    ImageTokenFamily,
    anthropic_fit,
    resolve_family,
)
from .models import MessagesRequest
from .request_modalities import request_image_sources


@dataclass(frozen=True, slots=True)
class ImageResize:
    """One picture that was shrunk, before and after.

    Recorded rather than logged so the request detail can show
    ``1920x1080 -> 1456x819`` and the request row can carry the byte totals.
    Only images that actually changed produce one of these; an image already
    inside the budget is a no-op and says so by being absent.
    """

    index: int
    before_width: int
    before_height: int
    after_width: int
    after_height: int
    before_bytes: int
    after_bytes: int

    @property
    def summary(self) -> str:
        return (
            f"{self.before_width}x{self.before_height}"
            f" -> {self.after_width}x{self.after_height}"
        )


def resize_target(
    width: int,
    height: int,
    *,
    max_long_edge: int,
    family: str | ImageTokenFamily,
) -> tuple[int, int]:
    """Return the size this image should leave at, given the destination.

    ``max_long_edge`` of 0 is the operator saying "send what the client sent",
    and it wins over everything below it -- including the family's own token
    budget, which is why the off switch is genuinely off rather than merely
    looser.

    For the Anthropic family, and for a host that publishes no formula (which
    is charged Anthropic's), both the pixel cap *and* the token budget bind,
    and the token one is the tighter of the two on a wide image. For every
    other family the token budget is not a resize rule the host publishes, so
    only the pixel cap applies and the estimate reflects whatever arrives.
    """
    if max_long_edge <= 0 or width <= 0 or height <= 0:
        return (width, height)
    resolved = resolve_family(family)
    if resolved in (ImageTokenFamily.ANTHROPIC, ImageTokenFamily.UNKNOWN):
        return anthropic_fit(
            width, height, max_long_edge=max_long_edge, max_tokens=ANTHROPIC_MAX_TOKENS
        )
    longest = max(width, height)
    if longest <= max_long_edge:
        return (width, height)
    scale = max_long_edge / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def downscale_request_images(
    request: MessagesRequest,
    *,
    max_long_edge: int,
    family: str | ImageTokenFamily,
    jpeg_quality: int = 0,
) -> tuple[ImageResize, ...]:
    """Shrink every oversized image on *request*, in place, and report what moved.

    The request must already be the router's per-attempt deep copy: this writes
    through to the ``source`` dicts it finds, which is the whole point -- there
    is no second place an outbound image is materialised, so editing the source
    is what makes every dialect see the smaller picture.
    """
    if max_long_edge <= 0:
        return ()
    resizes: list[ImageResize] = []
    for index, (kind, source) in enumerate(request_image_sources(request)):
        if not isinstance(source, dict):
            continue
        # A document is a PDF, not a raster the geometry code can resize, and
        # a URL-referenced image has no bytes here to shrink.
        if kind != "image" or source.get("type") != "base64":
            continue
        data = source.get("data")
        if not isinstance(data, str) or not data:
            continue
        resize = _downscale_one(
            source,
            data,
            index=index,
            max_long_edge=max_long_edge,
            family=family,
            jpeg_quality=jpeg_quality,
        )
        if resize is not None:
            resizes.append(resize)
    return tuple(resizes)


def _downscale_one(
    source: dict[Any, Any],
    data: str,
    *,
    index: int,
    max_long_edge: int,
    family: str | ImageTokenFamily,
    jpeg_quality: int,
) -> ImageResize | None:
    raw = decode_base64(data)
    if raw is None:
        return None
    size = raw_dimensions(raw)
    if size is None:
        # Unreadable, oversized past the decode guard, or a codec Pillow does
        # not know. Sent untouched: a request must never fail because its
        # screenshot was odd.
        return None
    width, height = size
    target = resize_target(width, height, max_long_edge=max_long_edge, family=family)
    if target == (width, height):
        return None
    result = downscale(raw, target, jpeg_quality=jpeg_quality)
    if result is None:
        return None
    new_raw, media_type = result
    if len(new_raw) >= len(raw) and jpeg_quality <= 0:
        # A re-encoded PNG can come out larger than the original a better
        # encoder produced, even at fewer pixels. The smaller *picture* is
        # still the point when the destination bills per pixel, so this is not
        # a refusal -- but it is worth one debug line when it happens.
        logger.debug(
            "Image downscale grew the payload: {} -> {} bytes at {}x{}",
            len(raw),
            len(new_raw),
            target[0],
            target[1],
        )
    source["data"] = base64.b64encode(new_raw).decode("ascii")
    source["media_type"] = media_type
    return ImageResize(
        index=index,
        before_width=width,
        before_height=height,
        after_width=target[0],
        after_height=target[1],
        before_bytes=len(raw),
        after_bytes=len(new_raw),
    )
