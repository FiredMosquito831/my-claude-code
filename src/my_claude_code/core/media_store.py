"""The opt-in, content-addressed store for media a media endpoint generated.

Metadata is always recorded (hash, bytes, MIME type, counts, on the request
row). The bytes themselves are kept only when ``MEDIA_STORE_ENABLED`` is on,
as plain files beside the request log -- ``<logs>/media/<sha[:2]>/<sha>.<ext>``
-- never inside ``requests.db``: a few multi-megabyte images or a minute of
audio would bloat the single database everything else shares.

Everything here is synchronous file work and is only ever called off the
event loop: from ``asyncio.to_thread`` on the request path, and from the
request log's own writer thread when pruning.
"""

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: File extension per MIME type. Anything else is stored as ``.bin``.
_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/aac": "aac",
    "video/mp4": "mp4",
    "video/webm": "webm",
}


@dataclass(frozen=True, slots=True)
class MediaOutputRecord:
    """One generated output, linked to its request in ``request_media``."""

    sha256: str
    mime: str | None
    bytes: int
    stored: bool
    idx: int = 0
    direction: str = "out"


def media_root(db_path: Path) -> Path:
    """Where stored media for the request log at ``db_path`` lives."""

    return Path(db_path).parent / "media"


def extension_for(mime: str | None) -> str:
    return _EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), "bin")


def media_file_path(root: Path, sha256: str, mime: str | None) -> Path:
    return root / sha256[:2] / f"{sha256}.{extension_for(mime)}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_media_file(root: Path, sha256: str, mime: str | None, data: bytes) -> bool:
    """Store ``data`` under its content address; ``True`` once it is on disk.

    Written to a temporary file in the same directory and renamed into place,
    so a crash never leaves a truncated file under a valid name. An address
    already on disk is not rewritten -- the same bytes have the same name.
    """

    target = media_file_path(root, sha256, mime)
    if target.exists():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=target.parent, suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        return False
    return True


def delete_media_files(root: Path, shas: Iterable[str]) -> int:
    """Delete every stored file for these addresses. Returns how many went."""

    removed = 0
    for sha in shas:
        folder = root / sha[:2]
        if not folder.is_dir():
            continue
        for path in folder.glob(f"{sha}.*"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
    return removed
