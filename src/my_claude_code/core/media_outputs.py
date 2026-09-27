"""What a media answer produced, measured for the request log.

One shape for every media operation: how many items came back, the ones whose
bytes MCC actually saw (decoded only to be hashed, sized and -- when the store
is on -- written), the host's usage block, and the audio length when it can be
read from the container. Everything here is synchronous and is only ever
called through ``asyncio.to_thread``.
"""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GeneratedMedia:
    """One generated item, decoded only to be measured (and stored)."""

    sha256: str
    mime: str | None
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class MediaOutputs:
    """What one media answer produced.

    ``count`` is every item the host returned (URL-only items included);
    ``items`` holds only the ones whose bytes MCC saw. A URL-only answer
    therefore has a count and no bytes -- "not measured", never zero.
    ``audio_seconds`` is ``None`` unless the container states its length.
    """

    count: int = 0
    items: tuple[GeneratedMedia, ...] = ()
    usage: dict[str, Any] | None = None
    audio_seconds: float | None = None
    #: A text answer (a transcript), logged as the row's output text.
    text: str | None = None
    #: Seconds of audio the host says it heard (transcription usage).
    input_audio_seconds: float | None = None

    @property
    def bytes_total(self) -> int | None:
        if not self.items:
            return None
        return sum(len(item.data) for item in self.items)

    @property
    def first_sha(self) -> str | None:
        return self.items[0].sha256 if self.items else None


def sse_error_frame(message: str, error_type: str) -> bytes:
    """The SSE ``error`` event that ends a committed media stream."""

    payload = {"type": "error", "error": {"message": message, "type": error_type}}
    return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()
