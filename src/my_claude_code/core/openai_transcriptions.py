"""The OpenAI transcription wire shape: text back, audio length when stated.

``response_format`` json / verbose_json answer JSON (``text``, and for
verbose_json a ``duration``); text / srt / vtt answer plain text; a stream is
SSE ending in ``transcript.text.done``. The usage block, when present, may be
``{type: "duration", seconds}``. MCC passes the answer through unchanged and
only measures it. Synchronous: called through ``asyncio.to_thread``.
"""

import io
import json
import wave
from typing import Any

from my_claude_code.core.media_outputs import MediaOutputs


def _usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage = payload.get("usage")
    return (
        {str(key): value for key, value in usage.items()}
        if isinstance(usage, dict)
        else None
    )


def _seconds(payload: dict[str, Any], usage: dict[str, Any] | None) -> float | None:
    """The audio length the host stated, from usage or a verbose ``duration``."""
    if usage is not None and usage.get("type") == "duration":
        value = usage.get("seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    duration = payload.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        return float(duration)
    return None


def parse_transcription_response(body: bytes, content_type: str | None) -> MediaOutputs:
    """Measure a non-streaming transcription answer."""
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind == "application/json" or body[:1] == b"{":
        try:
            payload = json.loads(body)
        except ValueError, UnicodeDecodeError:
            payload = None
        if isinstance(payload, dict):
            usage = _usage(payload)
            text = payload.get("text")
            return MediaOutputs(
                count=1,
                usage=usage,
                text=text if isinstance(text, str) else None,
                input_audio_seconds=_seconds(payload, usage),
            )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return MediaOutputs(count=1, text=text)


def parse_transcription_stream(frames: bytes) -> MediaOutputs:
    """Measure a streamed answer: the ``*.done`` event carries text and usage."""
    text: str | None = None
    usage: dict[str, Any] | None = None
    deltas: list[str] = []
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
        if kind.endswith(".delta") and isinstance(event.get("delta"), str):
            deltas.append(event["delta"])
        if kind.endswith(".done"):
            if isinstance(event.get("text"), str):
                text = event["text"]
            usage = _usage(event) or usage
    if text is None and deltas:
        text = "".join(deltas)
    return MediaOutputs(
        count=1 if text is not None else 0,
        usage=usage,
        text=text,
        input_audio_seconds=_seconds({}, usage),
    )


def wav_file_seconds(header: bytes) -> float | None:
    """The length a WAV header states, from the first bytes of an upload."""
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(header)) as reader:
            rate = reader.getframerate()
            frames = reader.getnframes()
    except wave.Error, EOFError:
        return None
    return frames / rate if rate > 0 else None
