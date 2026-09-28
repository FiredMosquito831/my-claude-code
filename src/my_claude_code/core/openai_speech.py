"""The OpenAI speech (TTS) wire shape: the answer is the audio itself.

``POST /audio/speech`` answers with raw audio bytes in the requested (or the
host's own) container. MCC never transcodes (user decision 9); it measures what
came back -- SHA-256, size, type, and the length when the container states it
(WAV does, in its header; MP3/Opus/AAC do not without decoding, so those stay
"not measured"). Synchronous: called through ``asyncio.to_thread``.
"""

import io
import json
import wave
from typing import Any

from my_claude_code.core.media_outputs import GeneratedMedia, MediaOutputs
from my_claude_code.core.media_store import sha256_hex

#: The content types a host labels WAV with.
_WAV_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})


def _base_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";")[0].strip().lower() or None


def wav_seconds(data: bytes) -> float | None:
    """The length a WAV header states, or ``None`` if it is not a readable WAV."""

    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(data)) as reader:
            rate = reader.getframerate()
            frames = reader.getnframes()
    except wave.Error, EOFError:
        return None
    if rate <= 0:
        return None
    return frames / rate


def parse_speech_response(body: bytes, content_type: str | None) -> MediaOutputs:
    """Measure one synthesized audio answer."""

    if not body:
        return MediaOutputs()
    mime = _base_type(content_type)
    seconds = wav_seconds(body) if (mime in _WAV_TYPES or body[:4] == b"RIFF") else None
    return MediaOutputs(
        count=1,
        items=(GeneratedMedia(sha256=sha256_hex(body), mime=mime, data=body),),
        audio_seconds=seconds,
    )


def parse_speech_stream(frames: bytes) -> MediaOutputs:
    """Measure a ``stream_format: "sse"`` answer from its ``*.done`` event (7.69.0).

    The audio arrives as base64 deltas, which are passed through and not
    decoded here; the done event carries the host's token ``usage``, the one
    thing the request log (and its price) reads from a streamed speech answer.
    """

    usage: dict[str, Any] | None = None
    done = False
    for line in frames.splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError, UnicodeDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if not str(event.get("type") or "").endswith(".done"):
            continue
        done = True
        found = event.get("usage")
        if isinstance(found, dict):
            usage = {str(key): value for key, value in found.items()}
    return MediaOutputs(count=1 if done else 0, usage=usage)
