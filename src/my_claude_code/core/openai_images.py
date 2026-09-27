"""The OpenAI Images wire shape: what a generation or edit answer carries.

Reading an answer means decoding base64 and hashing multi-megabyte images, so
every parser here is synchronous and is only ever called through
``asyncio.to_thread`` (``tests/contracts/test_media_loop_offload.py``).
"""

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from my_claude_code.core.media_outputs import GeneratedMedia, MediaOutputs
from my_claude_code.core.media_store import sha256_hex


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


def _decode(item: Any) -> GeneratedMedia | None:
    if not isinstance(item, dict):
        return None
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        data = base64.b64decode(encoded, validate=False)
    except binascii.Error, ValueError:
        return None
    return GeneratedMedia(
        sha256=sha256_hex(data), mime=sniff_image_mime(data), data=data
    )


def _usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage = payload.get("usage")
    return (
        {str(key): value for key, value in usage.items()}
        if isinstance(usage, dict)
        else None
    )


def parse_images_response(body: bytes) -> MediaOutputs:
    """Measure a non-streaming ``{created, data: [...], usage}`` answer."""

    try:
        payload = json.loads(body)
    except ValueError, UnicodeDecodeError:
        return MediaOutputs()
    if not isinstance(payload, dict):
        return MediaOutputs()
    items = payload.get("data")
    if not isinstance(items, list):
        return MediaOutputs(usage=_usage(payload))
    images = tuple(image for image in (_decode(item) for item in items) if image)
    return MediaOutputs(count=len(items), items=images, usage=_usage(payload))


def url_only_images(body: bytes) -> tuple[tuple[int, str], ...]:
    """Every answer item that names a URL and carries no ``b64_json``.

    ``(position in data, URL)``, in order: the pictures MCC must download
    itself before it can hand them on as bytes. Empty for anything that is
    not an Images answer.
    """

    try:
        payload = json.loads(body)
    except ValueError, UnicodeDecodeError:
        return ()
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ()
    found: list[tuple[int, str]] = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            found.append((position, url))
    return tuple(found)


def inline_image_urls(body: bytes, fetched: Mapping[int, bytes]) -> bytes:
    """The answer with each downloaded item's ``url`` replaced by ``b64_json``.

    ``fetched`` maps an item's position in ``data`` to its downloaded bytes;
    every other field of the answer and of the item is kept as the host sent
    it. Run off the loop: this base64-encodes whole pictures.
    """

    payload = json.loads(body)
    items = payload["data"]
    for position, data in fetched.items():
        item = dict(items[position])
        item.pop("url", None)
        item["b64_json"] = base64.b64encode(data).decode("ascii")
        items[position] = item
    return json.dumps(payload).encode()


def parse_images_stream(frames: bytes) -> MediaOutputs:
    """Measure a streamed answer from its ``*.completed`` events.

    Partial images are previews of the same picture and are not counted; the
    completed event carries the final image and the usage.
    """

    images: list[GeneratedMedia] = []
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
    return MediaOutputs(count=count, items=tuple(images), usage=usage)
