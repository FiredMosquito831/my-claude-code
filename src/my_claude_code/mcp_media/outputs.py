"""Where ``mcc-mcp`` saves what it makes, and what it remembers (7.99.0).

User decision 5 of 2026-10-18: results are saved in the folder the app started
the media server in -- a coding CLI starts it in the project it is working on
-- or, for Claude Desktop, which has no such folder, in ``mcp-media`` under
MCC's configuration folder (its registration passes ``--out``). The agent may
name another folder, or a file name, with ``save_to``. **A file is never
overwritten**: every save creates a new file exclusively, and a name already
taken gets ``-1``, ``-2``... before its extension.

Decision 7: a call the app stops waiting for keeps running; its result is
saved all the same and :class:`SessionLog` keeps it, marked as finished after
the app stopped waiting, so ``media_status`` can show where it went -- the
provider bills it either way.
"""

import mimetypes
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Extensions by the content type MCC answered with. Only the media types a
#: media door returns; anything else falls back to ``mimetypes`` and then to
#: ``.bin`` -- a name, not a claim about the bytes.
_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/pcm": ".pcm",
    "audio/l16": ".pcm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

#: Magic numbers of the picture formats an images answer carries as base64,
#: which says nothing about its own type.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_type(data: bytes) -> str:
    """The picture's MIME type from its first bytes (``image/png`` if unknown)."""

    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def extension_for(content_type: str) -> str:
    """A file extension for a content type, ``.bin`` when none is known."""

    base = content_type.split(";", 1)[0].strip().lower()
    if base in _EXTENSIONS:
        return _EXTENSIONS[base]
    guessed = mimetypes.guess_extension(base) if base else None
    return guessed or ".bin"


def resolve_target(
    save_to: str | None, *, default_dir: Path, cwd: Path
) -> tuple[Path, str | None]:
    """The folder a result goes in, and the file name asked for, if any.

    ``save_to`` empty: the default folder. A path that is an existing folder,
    ends with a separator or has no extension: that folder. Anything else: a
    file name, in its folder. Relative paths are resolved against ``cwd`` --
    the folder the app started the media server in.
    """

    if not save_to or not save_to.strip():
        return default_dir, None
    text = save_to.strip()
    path = Path(text)
    if not path.is_absolute():
        path = cwd / path
    if path.is_dir() or text.endswith(("/", "\\")) or not path.suffix:
        return path, None
    return path.parent, path.name


def _numbered(name: str, number: int) -> str:
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return f"{name}-{number}"
    return f"{stem}-{number}.{suffix}"


def save_new_file(data: bytes, folder: Path, name: str) -> Path:
    """Write ``data`` to a file that did not exist before; return its path.

    Exclusive creation, so a file already there -- whoever made it, whenever
    -- is never opened for writing: the next free ``-N`` name is taken
    instead (decision 5, never overwrite).
    """

    folder.mkdir(parents=True, exist_ok=True)
    number = 0
    while True:
        candidate = folder / (name if number == 0 else _numbered(name, number))
        try:
            with open(candidate, "xb") as handle:
                handle.write(data)
        except FileExistsError:
            number += 1
            continue
        return candidate.resolve()


def default_name(kind: str, extension: str, index: int = 0) -> str:
    """``mcc-image-20261018-130502.png`` (``-2`` for a second picture)."""

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    suffix = f"-{index + 1}" if index else ""
    return f"mcc-{kind}-{stamp}{suffix}{extension}"


def named(asked: str | None, extension: str, kind: str, index: int) -> str:
    """The asked file name (numbered after the first), or a default one."""

    if asked is None:
        return default_name(kind, extension, index)
    if index == 0:
        return asked
    return _numbered(asked, index)


@dataclass(slots=True)
class SessionResult:
    """One finished call, as ``media_status`` lists it."""

    tool: str
    finished_at: str
    summary: str
    paths: tuple[str, ...] = ()
    error: bool = False
    #: The app stopped waiting (it cancelled the call) before it finished.
    after_app_stopped_waiting: bool = False


@dataclass(slots=True)
class SessionLog:
    """Every result of this ``mcc-mcp`` process, oldest first."""

    results: list[SessionResult] = field(default_factory=list)
    #: Video jobs this session started, by id, with the rail they went to.
    video_jobs: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(
        self,
        tool: str,
        summary: str,
        *,
        paths: Sequence[str] = (),
        error: bool = False,
        after_app_stopped_waiting: bool = False,
    ) -> SessionResult:
        result = SessionResult(
            tool=tool,
            finished_at=time.strftime("%H:%M:%S", time.localtime()),
            summary=summary,
            paths=tuple(paths),
            error=error,
            after_app_stopped_waiting=after_app_stopped_waiting,
        )
        with self._lock:
            self.results.append(result)
        return result

    def snapshot(self) -> tuple[SessionResult, ...]:
        with self._lock:
            return tuple(self.results)


def describe_folder(path: Path) -> str:
    """A folder as the reader should see it: absolute, native separators."""

    return os.path.normpath(str(path.resolve()))
