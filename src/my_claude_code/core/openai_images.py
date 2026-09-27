"""The OpenAI Images wire shape: what a generation answer carries, and its errors.

Reading an answer means decoding base64 and hashing multi-megabyte images, so
every parser here is synchronous and is only ever called through
``asyncio.to_thread`` (``tests/contracts/test_media_loop_offload.py``).
"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.core.media_store import sha256_hex


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """One image an answer carried, decoded only to be measured (and stored)."""

    sha256: str
    mime: str | None
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ImageOutputs:
    """What one images answer produced, for the request log.

    ``count`` is every item in ``data`` (URL-only items included); ``images``
    holds only the base64 ones, the only ones whose bytes MCC ever sees. A
    URL-only answer therefore has a count and no bytes -- "not measured", not
    zero.
    """

    count: int = 0
    images: tuple[GeneratedImage, ...] = ()
    usage: dict[str, Any] | None = None

    @property
    def bytes_total(self) -> int | None:
        if not self.images:
            return None
        return sum(len(image.data) for image in self.images)

    @property
    def first_sha(self) -> str | None:
        return self.images[0].sha256 if self.images else None


def sniff_image_mime(data: bytes) -> str | None:
    """The image type from its magic bytes; ``None`` when unrecognised."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _decode(item: Any) -> GeneratedImage | None:
    if not isinstance(item, dict):
        return None
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        data = base64.b64decode(encoded, validate=False)
    except binascii.Error, ValueError:
        return None
    return GeneratedImage(
        sha256=sha256_hex(data), mime=sniff_image_mime(data), data=data
    )


def _usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage = payload.get("usage")
    return (
        {str(key): value for key, value in usage.items()}
        if isinstance(usage, dict)
        else None
    )


def parse_images_response(body: bytes) -> ImageOutputs:
    """Measure a non-streaming ``{created, data: [...], usage}`` answer."""

    try:
        payload = json.loads(body)
    except ValueError, UnicodeDecodeError:
        return ImageOutputs()
    if not isinstance(payload, dict):
        return ImageOutputs()
    items = payload.get("data")
    if not isinstance(items, list):
        return ImageOutputs(usage=_usage(payload))
    images = tuple(image for image in (_decode(item) for item in items) if image)
    return ImageOutputs(count=len(items), images=images, usage=_usage(payload))


def parse_images_stream(frames: bytes) -> ImageOutputs:
    """Measure a streamed answer from its ``*.completed`` events.

    Partial images are previews of the same picture and are not counted; the
    completed event carries the final image and the usage.
    """

    images: list[GeneratedImage] = []
    count = 0
    usage: dict[str, Any] | None = None
    for line in frames.splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError, UnicodeDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if not kind.endswith(".completed"):
            continue
        count += 1
        image = _decode(event)
        if image is not None:
            images.append(image)
        usage = _usage(event) or usage
    return ImageOutputs(count=count, images=tuple(images), usage=usage)


def images_error_frame(message: str, error_type: str) -> bytes:
    """The SSE ``error`` event that ends a committed images stream."""

    payload = {"type": "error", "error": {"message": message, "type": error_type}}
    return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()
