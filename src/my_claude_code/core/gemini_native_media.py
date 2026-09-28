"""Gemini's native ``generateContent`` as a media upstream: speech and transcripts.

Gemini's OpenAI-compatible layer has no ``audio/speech`` and no
``audio/transcriptions``, so a Gemini model serves the Speech and Transcription
rails only through native ``generateContent``. This module builds that body
from the OpenAI-shaped request the rail carries, and turns Google's answer
back into what an OpenAI client reads: the audio itself, or the transcript as
JSON or text.

MCC never transcodes (user decision 9). Putting a WAV header on Gemini's raw
PCM when the client named ``wav``, or taking one off when it named ``pcm``, is
framing: the samples are the host's own, byte for byte. Gemini documents its
raw audio (``audio/l16``) as 24 kHz mono 16-bit signed little-endian PCM,
which is exactly what a WAV ``data`` chunk holds.

A transcript is asked for with one fixed instruction (:data:`TRANSCRIBE_INSTRUCTION`,
approved in review 2026-09-26 03:38 #5), plus the language when the client
named one. Token counts come from the answer's ``usageMetadata`` only; nothing
is estimated.

Pure and synchronous. Everything here that touches the base64 audio or the
answer's JSON runs through ``asyncio.to_thread``.
"""

import base64
import binascii
import io
import json
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The instruction a transcription is asked with (documented in MEDIA.md).
TRANSCRIBE_INSTRUCTION = (
    "Transcribe the speech in this audio exactly as spoken. Answer with the "
    "transcript only, with no introduction or commentary."
)

#: Content types a host labels raw 16-bit PCM with.
_PCM_TYPES = frozenset({"audio/l16", "audio/pcm"})
#: Content types a host labels WAV with.
_WAV_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})
#: Bytes per sample of 16-bit PCM.
_SAMPLE_WIDTH = 2


class GeminiAnswerError(ValueError):
    """The answer holds nothing this request can be served with.

    The message completes "<provider> answered ...", e.g. "without audio".
    """


@dataclass(frozen=True, slots=True)
class GeminiMediaAnswer:
    """A native answer, translated into what the OpenAI client reads."""

    body: bytes
    content_type: str
    #: OpenAI-shaped token usage from ``usageMetadata``; ``None`` when absent.
    usage: dict[str, Any] | None = None
    #: The length of raw PCM, which states none in a header; ``None`` otherwise.
    audio_seconds: float | None = None


# ----------------------------------------------------------------- requests


def transcribe_instruction(language: str | None) -> str:
    """The instruction, with the client's ``language`` appended when it sent one."""

    if language:
        return f"{TRANSCRIBE_INSTRUCTION} The speech is in {language}."
    return TRANSCRIBE_INSTRUCTION


def speech_request(text: str, voice: str | None) -> dict[str, Any]:
    """The ``generateContent`` body that speaks ``text``.

    With no ``voice``, no ``speechConfig`` is sent and the host's default
    voice speaks.
    """

    generation: dict[str, Any] = {"responseModalities": ["AUDIO"]}
    if voice is not None:
        generation["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
        }
    return {"contents": [{"parts": [{"text": text}]}], "generationConfig": generation}


def transcribe_request(instruction: str, mime_type: str, audio: bytes) -> bytes:
    """The encoded ``generateContent`` body for a transcript. Run off the loop.

    The audio goes inline, base64, typed as the client's upload was typed.
    """

    body = {
        "contents": [
            {
                "parts": [
                    {"text": instruction},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64.b64encode(audio).decode("ascii"),
                        }
                    },
                ]
            }
        ]
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------ answers


def _mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _pick(mapping: Mapping[str, Any], *names: str) -> Any:
    """The first of ``names`` present: Google writes camelCase, SDKs snake_case."""

    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _payload(raw: bytes) -> dict[str, Any]:
    try:
        payload = _mapping(json.loads(raw))
    except ValueError, UnicodeDecodeError:
        payload = None
    if payload is None:
        raise GeminiAnswerError("with a body that is not a JSON object")
    return payload


def _parts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The first candidate's parts (one candidate is asked for)."""

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []
    candidate = _mapping(candidates[0])
    content = None if candidate is None else _mapping(candidate.get("content"))
    parts = None if content is None else content.get("parts")
    if not isinstance(parts, list):
        return []
    return [part for part in (_mapping(item) for item in parts) if part is not None]


def token_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """``usageMetadata`` as OpenAI's token ``usage``; ``None`` when not stated."""

    metadata = _mapping(_pick(payload, "usageMetadata", "usage_metadata"))
    if metadata is None:
        return None
    prompt = _count(_pick(metadata, "promptTokenCount", "prompt_token_count"))
    output = _count(_pick(metadata, "candidatesTokenCount", "candidates_token_count"))
    total = _count(_pick(metadata, "totalTokenCount", "total_token_count"))
    if total is None and prompt is not None and output is not None:
        total = prompt + output
    if prompt is None and output is None and total is None:
        return None
    usage: dict[str, Any] = {"type": "tokens"}
    if prompt is not None:
        usage["input_tokens"] = prompt
    if output is not None:
        usage["output_tokens"] = output
    if total is not None:
        usage["total_tokens"] = total
    return usage


def _modality_tokens(metadata: Mapping[str, Any], *names: str) -> int | None:
    """The ``AUDIO`` tokens one ``*TokensDetails`` list states, or None."""

    details = _pick(metadata, *names)
    if not isinstance(details, list):
        return None
    found: int | None = None
    for item in details:
        entry = _mapping(item)
        if entry is None or str(entry.get("modality") or "").upper() != "AUDIO":
            continue
        count = _count(_pick(entry, "tokenCount", "token_count"))
        if count is not None:
            found = (found or 0) + count
    return found


def measured_usage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """:func:`token_usage` plus the audio part Gemini itemised (7.69.0).

    For the request log and its price only, never for a client: an OpenAI
    transcription answer keeps exactly the three counters :func:`token_usage`
    gives it. ``promptTokensDetails`` / ``candidatesTokensDetails`` state
    tokens per modality; the ``AUDIO`` ones are written the way OpenAI's own
    transcription usage spells them (``input_token_details.audio_tokens``,
    and ``output_token_details.audio_tokens`` for speech), so one reader
    prices both hosts. Counts the host stated; nothing is derived from seconds.
    """

    usage = token_usage(payload)
    metadata = _mapping(_pick(payload, "usageMetadata", "usage_metadata"))
    if usage is None or metadata is None:
        return usage
    audio_in = _modality_tokens(
        metadata, "promptTokensDetails", "prompt_tokens_details"
    )
    audio_out = _modality_tokens(
        metadata, "candidatesTokensDetails", "candidates_tokens_details"
    )
    if audio_in is not None:
        usage["input_token_details"] = {"audio_tokens": audio_in}
    if audio_out is not None:
        usage["output_token_details"] = {"audio_tokens": audio_out}
    return usage


def _media_type(mime: str) -> tuple[str, dict[str, str]]:
    """``audio/L16;codec=pcm;rate=24000`` -> ``("audio/l16", {codec, rate})``."""

    base, *params = mime.split(";")
    parsed: dict[str, str] = {}
    for param in params:
        name, _, value = param.partition("=")
        if name.strip():
            parsed[name.strip().lower()] = value.strip().strip('"')
    return base.strip().lower(), parsed


def _positive(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    number = int(value)
    return number if number > 0 else None


def _first_audio(parts: list[dict[str, Any]]) -> tuple[bytes, str] | None:
    """The first ``inlineData`` audio part: its bytes and its declared type."""

    for part in parts:
        inline = _mapping(_pick(part, "inlineData", "inline_data"))
        if inline is None:
            continue
        mime = _pick(inline, "mimeType", "mime_type")
        data = inline.get("data")
        if not isinstance(mime, str) or not mime.strip().lower().startswith("audio/"):
            continue
        if not isinstance(data, str) or not data:
            continue
        try:
            decoded = base64.b64decode(data, validate=False)
        except binascii.Error, ValueError:
            continue
        if decoded:
            return decoded, mime.strip()
    return None


def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    """Raw 16-bit little-endian PCM with a WAV header in front: framing only."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(_SAMPLE_WIDTH)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int, int]:
    """A 16-bit PCM WAV's samples, rate and channels: its header removed."""

    try:
        with wave.open(io.BytesIO(data)) as reader:
            width = reader.getsampwidth()
            rate = reader.getframerate()
            channels = reader.getnchannels()
            frames = reader.readframes(reader.getnframes())
    except wave.Error, EOFError:
        raise GeminiAnswerError("with a WAV MCC could not read") from None
    if width != _SAMPLE_WIDTH or rate <= 0:
        raise GeminiAnswerError(
            f"with {width * 8}-bit WAV audio, which is not the 16-bit PCM named"
        )
    return frames, rate, channels


def _pcm_type(rate: int, channels: int) -> str:
    """The type Gemini labels its own raw PCM with, for PCM taken out of a WAV."""

    kind = f"audio/L16;codec=pcm;rate={rate}"
    return kind if channels == 1 else f"{kind};channels={channels}"


def _pcm_seconds(size: int, rate: int, channels: int) -> float:
    return size / (rate * _SAMPLE_WIDTH * channels)


def speech_answer(raw: bytes, named: str | None) -> GeminiMediaAnswer:
    """A native TTS answer as the audio the client named. Run off the loop.

    ``named`` is the client's ``response_format``: ``wav`` gets raw PCM with a
    WAV header added (a WAV answer passes); ``pcm`` gets raw PCM (a WAV answer
    has its header removed); nothing named gets the host's bytes and type as
    they came. Raises :class:`GeminiAnswerError` when there is no audio, or
    when the audio cannot be framed as named without inventing a parameter.
    """

    payload = _payload(raw)
    # The body is the audio itself, so this usage reaches only the log.
    usage = measured_usage(payload)
    audio = _first_audio(_parts(payload))
    if audio is None:
        raise GeminiAnswerError("without audio")
    data, mime = audio
    base, params = _media_type(mime)
    pcm = base in _PCM_TYPES
    wav = base in _WAV_TYPES or data[:4] == b"RIFF"
    rate = _positive(params.get("rate"))
    channels = _positive(params.get("channels")) or 1
    seconds = (
        None if not pcm or rate is None else _pcm_seconds(len(data), rate, channels)
    )
    if named == "wav":
        if wav:
            return GeminiMediaAnswer(body=data, content_type="audio/wav", usage=usage)
        if not pcm:
            raise GeminiAnswerError(f"with {base} audio, not the wav asked for")
        if rate is None:
            raise GeminiAnswerError(
                "raw PCM without its sample rate; it cannot be framed as wav"
            )
        return GeminiMediaAnswer(
            body=pcm_to_wav(data, rate, channels), content_type="audio/wav", usage=usage
        )
    if named == "pcm":
        if pcm:
            return GeminiMediaAnswer(
                body=data, content_type=mime, usage=usage, audio_seconds=seconds
            )
        if not wav:
            raise GeminiAnswerError(f"with {base} audio, not the pcm asked for")
        samples, wav_rate, wav_channels = wav_to_pcm(data)
        return GeminiMediaAnswer(
            body=samples,
            content_type=_pcm_type(wav_rate, wav_channels),
            usage=usage,
            audio_seconds=_pcm_seconds(len(samples), wav_rate, wav_channels),
        )
    return GeminiMediaAnswer(
        body=data, content_type=mime, usage=usage, audio_seconds=seconds
    )


def transcript_answer(raw: bytes, named: str | None) -> GeminiMediaAnswer:
    """A native answer as an OpenAI transcription. Run off the loop.

    ``text`` gets the transcript as plain text; ``json`` (or nothing named)
    gets ``{"text", "usage"?}``. The text is the first candidate's text parts
    joined (a thought is not transcript). Raises :class:`GeminiAnswerError`
    when there is no text part at all.
    """

    payload = _payload(raw)
    usage = token_usage(payload)
    measured = measured_usage(payload)
    texts = [
        part["text"]
        for part in _parts(payload)
        if isinstance(part.get("text"), str) and not part.get("thought")
    ]
    if not texts:
        raise GeminiAnswerError("without a transcript")
    text = "".join(texts)
    if named == "text":
        return GeminiMediaAnswer(
            body=text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            usage=measured,
        )
    answer: dict[str, Any] = {"text": text}
    if usage is not None:
        answer["usage"] = usage
    return GeminiMediaAnswer(
        body=json.dumps(answer, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        usage=measured,
    )
