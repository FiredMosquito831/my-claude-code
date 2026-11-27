"""Off-loop multipart/JSON upload conversion and exact parameter bijection (8.62.0).

Pure helpers for:
* Exact lossless ``size <-> aspect_ratio`` (+ ``resolution``) bijection, with
  non-exact pixel rounding strictly gated behind ``approximate=True``
  (``MEDIA_APPROXIMATE_PARAM_MAPPING``);
* Off-loop spooled-upload <-> JSON ``data:`` URI conversion, intended to run
  inside ``asyncio.to_thread`` so the asyncio event loop never reads disk or
  base64-encodes/decodes megabytes of media bytes.
"""

import base64
import hashlib
import math
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from typing import IO, Any, cast

from my_claude_code.application.media.request import MediaUpload
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_VARIATION,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_EDIT,
)

#: Operations whose spooled file uploads can be encoded off-loop into JSON
#: ``data:`` URIs when the target surface declares ``encoding="json"``.
UPLOAD_TRANSLATABLE_OPERATIONS: frozenset[str] = frozenset(
    {
        MEDIA_OPERATION_IMAGE_EDIT,
        MEDIA_OPERATION_IMAGE_VARIATION,
        MEDIA_OPERATION_VIDEO_CREATE,
        MEDIA_OPERATION_VIDEO_EDIT,
        MEDIA_OPERATION_TRANSCRIBE,
        MEDIA_OPERATION_TRANSLATE,
        "ocr",
    }
)

#: Exact standard pixel dimensions -> aspect ratio (lossless bijection).
EXACT_SIZE_TO_ASPECT: dict[str, str] = {
    "1024x1024": "1:1",
    "1792x1024": "16:9",
    "1280x720": "16:9",
    "1920x1080": "16:9",
    "1024x1792": "9:16",
    "720x1280": "9:16",
    "1080x1920": "9:16",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
}

#: Exact standard pixel dimensions -> video/image resolution label.
EXACT_SIZE_TO_RESOLUTION: dict[str, str] = {
    "1280x720": "720p",
    "720x1280": "720p",
    "1920x1080": "1080p",
    "1080x1920": "1080p",
}

#: Reverse exact table for ``aspect_ratio`` -> default OpenAI ``size``.
ASPECT_TO_DEFAULT_SIZE: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}

_EXACT_ASPECT_RESOLUTION_TO_SIZE: dict[tuple[str, str], str] = {
    ("16:9", "720p"): "1280x720",
    ("9:16", "720p"): "720x1280",
    ("16:9", "1080p"): "1920x1080",
    ("9:16", "1080p"): "1080x1920",
}

_APPROX_ASPECT_CANDIDATES: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("16:9", 16.0 / 9.0),
    ("9:16", 9.0 / 16.0),
    ("3:2", 3.0 / 2.0),
    ("2:3", 2.0 / 3.0),
)

_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")
_ASPECT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")
_DATA_URI_RE = re.compile(
    r"^\s*data:(?P<mime>[A-Za-z0-9.+/-]+)?(?:;[A-Za-z0-9.=+-]+)*;base64,(?P<data>.+)\s*$",
    re.DOTALL,
)

_MIME_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "video/mp4": "mp4",
    "application/pdf": "pdf",
}


def _nearest_aspect_for_size(raw_size: str) -> str | None:
    match = _SIZE_RE.match(raw_size)
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    return min(
        _APPROX_ASPECT_CANDIDATES,
        key=lambda item: abs(math.log(ratio / item[1])),
    )[0]


def _nearest_aspect_from_ratio(raw_aspect: str) -> str | None:
    match = _ASPECT_RE.match(raw_aspect)
    if match is None:
        return None
    w, h = float(match.group(1)), float(match.group(2))
    if w <= 0 or h <= 0:
        return None
    ratio = w / h
    return min(
        _APPROX_ASPECT_CANDIDATES,
        key=lambda item: abs(math.log(ratio / item[1])),
    )[0]


def translate_size_and_aspect(
    body: Mapping[str, Any],
    *,
    target_fields: Iterable[str],
    approximate: bool = False,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Translate ``size <-> aspect_ratio`` (+ ``resolution``) for ``target_fields``.

    Exact standard ratios translate losslessly by default. Arbitrary dimensions
    remain untranslated and are recorded in ``not_forwarded`` unless
    ``approximate=True``.
    """

    targets = frozenset(target_fields)
    out = dict(body)
    adaptations: list[str] = []
    not_forwarded: list[str] = []

    raw_size = out.get("size")
    if (
        isinstance(raw_size, str)
        and raw_size.strip()
        and "size" not in targets
        and "aspect_ratio" in targets
    ):
        cleaned_size = out.pop("size").strip()
        norm_size = cleaned_size.lower()
        if norm_size in EXACT_SIZE_TO_ASPECT:
            aspect = EXACT_SIZE_TO_ASPECT[norm_size]
            if "aspect_ratio" not in out:
                out["aspect_ratio"] = aspect
                adaptations.append(f"size({cleaned_size})->aspect_ratio({aspect})")
            resolution = EXACT_SIZE_TO_RESOLUTION.get(norm_size)
            if (
                resolution is not None
                and "resolution" in targets
                and "resolution" not in out
            ):
                out["resolution"] = resolution
                adaptations.append(f"size({cleaned_size})->resolution({resolution})")
        elif approximate:
            nearest = _nearest_aspect_for_size(cleaned_size)
            if nearest is not None:
                if "aspect_ratio" not in out:
                    out["aspect_ratio"] = nearest
                    adaptations.append(
                        f"approx:size({cleaned_size})->aspect_ratio({nearest})"
                    )
            else:
                not_forwarded.append("size")
        else:
            not_forwarded.append("size")

    raw_aspect = out.get("aspect_ratio")
    if (
        isinstance(raw_aspect, str)
        and raw_aspect.strip()
        and "aspect_ratio" not in targets
        and "size" in targets
    ):
        cleaned_aspect = out.pop("aspect_ratio").strip()
        raw_res = out.pop("resolution", None) if "resolution" not in targets else None
        res_norm = (
            raw_res.strip().lower()
            if isinstance(raw_res, str) and raw_res.strip()
            else ""
        )
        mapped_size = (
            _EXACT_ASPECT_RESOLUTION_TO_SIZE.get((cleaned_aspect, res_norm))
            if res_norm
            else None
        ) or ASPECT_TO_DEFAULT_SIZE.get(cleaned_aspect)
        if mapped_size is not None:
            if "size" not in out:
                out["size"] = mapped_size
                adaptations.append(
                    f"aspect_ratio({cleaned_aspect})->size({mapped_size})"
                )
        elif approximate:
            nearest = _nearest_aspect_from_ratio(cleaned_aspect)
            if nearest is not None and nearest in ASPECT_TO_DEFAULT_SIZE:
                mapped = ASPECT_TO_DEFAULT_SIZE[nearest]
                if "size" not in out:
                    out["size"] = mapped
                    adaptations.append(
                        f"approx:aspect_ratio({cleaned_aspect})->size({mapped})"
                    )
            else:
                not_forwarded.append("aspect_ratio")
                if raw_res is not None:
                    not_forwarded.append("resolution")
        else:
            not_forwarded.append("aspect_ratio")
            if raw_res is not None:
                not_forwarded.append("resolution")

    return out, tuple(adaptations), tuple(not_forwarded)


def upload_to_data_uri(upload: MediaUpload) -> str:
    """Read one spooled upload and return a ``data:<mime>;base64,...`` URI.

    Synchronous and blocks on disk if spooled; always call via
    ``asyncio.to_thread`` from async code.
    """

    upload.file.seek(0)
    raw = upload.file.read()
    upload.file.seek(0)
    mime = upload.content_type or "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def uploads_to_data_uris(uploads: Sequence[MediaUpload]) -> list[dict[str, Any]]:
    """Encode spooled uploads into OpenAI JSON ``images`` entries off-loop."""

    return [
        {
            "type": "image_url",
            "image_url": {"url": upload_to_data_uri(upload)},
        }
        for upload in uploads
    ]


def extract_image_url(entry: Any) -> str | None:
    """Extract an image URL or data URI from a string or OpenAI-style entry dict."""
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, Mapping):
        return None
    image_url = entry.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, Mapping):
        url = image_url.get("url")
        if isinstance(url, str):
            return url
    url = entry.get("url")
    if isinstance(url, str):
        return url
    return None


def has_decodable_data_uris(body: Mapping[str, Any]) -> bool:
    """Whether ``body['images']`` carries at least one ``data:`` base64 URI."""

    images = body.get("images")
    if not isinstance(images, Sequence) or isinstance(images, str | bytes | bytearray):
        return False
    for entry in images:
        url = extract_image_url(entry)
        if url is not None and _DATA_URI_RE.match(url) is not None:
            return True
    return False


def data_uris_to_uploads(images_list: Sequence[Any]) -> tuple[MediaUpload, ...]:
    """Decode JSON ``images`` ``data:`` URIs into spooled ``MediaUpload`` objects.

    Synchronous and may spill large images to disk; always call via
    ``asyncio.to_thread`` from async code.
    """

    uploads: list[MediaUpload] = []
    for index, entry in enumerate(images_list):
        url = extract_image_url(entry)
        if url is None:
            continue
        match = _DATA_URI_RE.match(url)
        if match is None:
            continue
        mime = (match.group("mime") or "application/octet-stream").strip().lower()
        payload = re.sub(r"\s+", "", match.group("data"))
        try:
            raw = base64.b64decode(payload, validate=True)
        except ValueError:
            raw = base64.b64decode(payload)
        digest = hashlib.sha256(raw).hexdigest()
        # Returned open inside MediaUpload.file for downstream multipart streaming.
        spooled = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115
        spooled.write(raw)
        spooled.seek(0)
        ext = _MIME_EXTENSIONS.get(mime, "bin")
        uploads.append(
            MediaUpload(
                field="image",
                filename=f"image_{index}.{ext}",
                content_type=mime,
                size=len(raw),
                sha256=digest,
                file=cast(IO[bytes], spooled),
            )
        )
    return tuple(uploads)


__all__ = [
    "ASPECT_TO_DEFAULT_SIZE",
    "EXACT_SIZE_TO_ASPECT",
    "EXACT_SIZE_TO_RESOLUTION",
    "UPLOAD_TRANSLATABLE_OPERATIONS",
    "data_uris_to_uploads",
    "extract_image_url",
    "has_decodable_data_uris",
    "translate_size_and_aspect",
    "upload_to_data_uri",
    "uploads_to_data_uris",
]
