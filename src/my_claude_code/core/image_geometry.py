"""Pixel geometry for the images a request carries.

Three consumers need the same two facts about an image and none of them should
own the answer: the token estimator needs its width and height (every published
per-image billing formula is a function of those two numbers and of nothing
else), the outbound downscaler needs to produce a smaller copy, and the request
log's thumbnailer needs the same decompression-bomb guard both of them do.

Nothing here raises. An image the decoder cannot read is returned unchanged and
reported as unknown -- a request must never fail because its screenshot was
odd, which is the rule ``core/request_images.py`` already states for capture.

Dimensions are read header-only. ``Image.open`` parses the header and stops, so
reading ``.size`` never decodes pixels; the memo below then keeps even that
header parse off the second and every later turn of a conversation, which
matters because Claude Code re-sends the whole transcript -- and therefore the
same screenshot -- on every single request.
"""

import base64
import binascii
import hashlib
import io
import threading
from typing import Any

from loguru import logger

# Decoder guard, shared with ``core/request_images.py``. Pillow refuses images
# above this many pixels as a decompression bomb; the explicit number keeps the
# refusal ours and logged rather than an exception from inside the library.
MAX_SOURCE_PIXELS = 80_000_000

# Memo of digest -> (width, height) or None for "we looked and could not tell".
# Bounded because a long-running proxy sees an unbounded number of screenshots;
# the value is two small ints so the bound can be generous.
_MEMO_MAX_ENTRIES = 512
_memo: dict[str, tuple[int, int] | None] = {}
_memo_lock = threading.Lock()
_memo_hits = 0
_memo_misses = 0


def reset_image_geometry_cache() -> None:
    """Forget every memoised dimension. For tests and for a config reload."""
    global _memo_hits, _memo_misses
    with _memo_lock:
        _memo.clear()
        _memo_hits = 0
        _memo_misses = 0


def image_geometry_cache_info() -> tuple[int, int, int]:
    """Return ``(hits, misses, entries)`` for the dimension memo."""
    with _memo_lock:
        return (_memo_hits, _memo_misses, len(_memo))


def decode_base64(data: str) -> bytes | None:
    """Decode inlined base64, or ``None`` when it is not decodable."""
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError) as exc:
        logger.debug("Image base64 undecodable: {}", exc)
        return None


def _digest(data: str) -> str:
    return hashlib.blake2b(data.encode("utf-8", "ignore"), digest_size=16).hexdigest()


def image_dimensions(data: str) -> tuple[int, int] | None:
    """Return ``(width, height)`` of one inlined base64 image, or ``None``.

    ``None`` is "the decoder could not tell", which every caller must treat as
    a different fact from "the image is small": the estimator keeps its
    byte-based fallback and the downscaler sends the original bytes untouched.
    """
    global _memo_hits, _memo_misses
    if not data:
        return None
    key = _digest(data)
    with _memo_lock:
        if key in _memo:
            _memo_hits += 1
            return _memo[key]
    raw = decode_base64(data)
    size = None if raw is None else raw_dimensions(raw)
    with _memo_lock:
        _memo_misses += 1
        if len(_memo) >= _MEMO_MAX_ENTRIES:
            _memo.clear()
        _memo[key] = size
    return size


def raw_dimensions(raw: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` of decoded image bytes, header-only."""
    image = _open(raw)
    if image is None:
        return None
    with image:
        width, height = image.size
    if width <= 0 or height <= 0:
        return None
    if width * height > MAX_SOURCE_PIXELS:
        logger.warning(
            "Image geometry refused: {}x{} exceeds the decode guard", width, height
        )
        return None
    return (width, height)


def _open(raw: bytes) -> Any:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        logger.debug("Image geometry skipped: Pillow is not installed")
        return None
    try:
        return Image.open(io.BytesIO(raw))
    except Exception as exc:
        # A truncated upload, an unsupported codec, a PDF sent as a document.
        logger.debug("Image geometry skipped: {}", exc)
        return None


# Formats Pillow can write back in the shape it read them. Anything else --
# a PDF, an animated GIF, an ICO -- is left alone rather than re-encoded into
# something the destination may not accept.
_RESAVEABLE: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


def downscale(
    raw: bytes,
    target: tuple[int, int],
    *,
    jpeg_quality: int = 0,
) -> tuple[bytes, str] | None:
    """Return smaller bytes plus their media type, or ``None`` to send as-is.

    ``jpeg_quality`` of 0 means "never change the format", which is the default
    and the only setting under which this function is lossless about *what* the
    image is: a PNG comes back a PNG. A non-zero quality re-encodes to JPEG,
    and is skipped for any image carrying an alpha channel, because flattening
    transparency is a visible change the operator did not ask for when they
    asked for a smaller file.

    ``None`` on every failure path. A downscale that cannot be done is not an
    error; it is the original image.
    """
    image = _open(raw)
    if image is None:
        return None
    try:
        with image:
            width, height = image.size
            if width * height > MAX_SOURCE_PIXELS:
                return None
            source_format = str(image.format or "").upper()
            resized = image.convert("RGBA" if _has_alpha(image) else "RGB")
            resized = resized.resize(target, _resample())
            has_alpha = resized.mode == "RGBA"
        if jpeg_quality > 0 and not has_alpha:
            return (_encode(resized, "JPEG", quality=jpeg_quality), "image/jpeg")
        out_format = source_format if source_format in _RESAVEABLE else "PNG"
        if out_format == "JPEG" and has_alpha:
            out_format = "PNG"
        if out_format == "JPEG":
            # Re-encoding a JPEG at all is already lossy; 92 is the quality at
            # which a second generation is visually indistinguishable, and this
            # branch is only reached because the image was too big to send.
            return (_encode(resized, "JPEG", quality=92), _RESAVEABLE["JPEG"])
        return (_encode(resized, out_format), _RESAVEABLE[out_format])
    except Exception as exc:
        logger.debug("Image downscale skipped: {}", exc)
        return None


def _has_alpha(image: Any) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info


def _resample() -> Any:
    from PIL import Image

    return Image.Resampling.LANCZOS


def _encode(image: Any, image_format: str, *, quality: int | None = None) -> bytes:
    buffer = io.BytesIO()
    if quality is None:
        image.save(buffer, format=image_format)
    else:
        image.save(buffer, format=image_format, quality=quality)
    return buffer.getvalue()
