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
from typing import IO

#: File extension per MIME type. Anything else is stored as ``.bin``.
_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/opus": "opus",
    "audio/pcm": "pcm",
    "audio/l16": "pcm",
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


class MediaFileTee:
    """Hash -- and, when a root is given, store -- a file while it streams past.

    For a video served straight from the host to the client: the bytes are
    never held whole. Each batch is fed from a worker thread (``feed``), the
    temporary file sits in the media root, and ``finish`` renames it to its
    content address once the last byte has passed -- or ``abort`` removes it
    when the stream did not complete, so no partial file ever gets a name.
    """

    def __init__(self, root: Path | None) -> None:
        self._digest = hashlib.sha256()
        self.size = 0
        self._root = root
        self._temp: Path | None = None
        self._stream: IO[bytes] | None = None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(dir=root, suffix=".part")
            self._temp = Path(name)
            self._stream = os.fdopen(handle, "wb")

    def feed(self, data: bytes) -> None:
        self._digest.update(data)
        self.size += len(data)
        stream = self._stream
        if stream is not None:
            try:
                stream.write(data)
            except OSError:
                # Measuring goes on; only the copy is given up.
                self.abort()

    def finish(self, mime: str | None) -> tuple[str, bool]:
        """The content address, and whether the file is now stored under it."""
        sha = self._digest.hexdigest()
        stream, temp, root = self._stream, self._temp, self._root
        self._stream = None
        self._temp = None
        if stream is None or temp is None or root is None:
            return sha, False
        try:
            stream.close()
            target = media_file_path(root, sha, mime)
            if target.exists():
                temp.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp, target)
        except OSError:
            with contextlib.suppress(OSError):
                temp.unlink()
            return sha, False
        return sha, True

    def abort(self) -> None:
        """Drop the partial copy, if any. Safe to call more than once."""
        stream, temp = self._stream, self._temp
        self._stream = None
        self._temp = None
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink()


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
