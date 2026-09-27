"""Gemini as the UPSTREAM of the Speech and Transcription rails (7.66.0).

Gemini's OpenAI-compatible layer has no ``audio/speech`` or
``audio/transcriptions``, so these rails reach a Gemini model through native
``generateContent``: the key in ``x-goog-api-key``, the model in the URL, the
answer translated back (raw PCM framed as the client named, the transcript as
JSON or text). Every upstream is an ``httpx.MockTransport`` fake.
"""

import asyncio
import base64
import io
import json
import sqlite3
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.media_surfaces import (
    GEMINI_TTS_VOICES,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    surface_for,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.core import gemini_native_media, request_log
from my_claude_code.core.gemini_native_media import TRANSCRIBE_INSTRUCTION
from my_claude_code.providers.media import adapters, leaf
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app

GEMINI_KEY = "AIza" + "g" * 35
GOOGLE = "generativelanguage.googleapis.com"
TOGETHER = "api.together.ai"
TTS_MODEL = "gemini-2.5-flash-preview-tts"
ASR_MODEL = "gemini-2.5-flash"
L16 = "audio/L16;codec=pcm;rate=24000"
#: Half a second of 24 kHz mono 16-bit PCM, not silence (so framing is visible).
PCM = bytes(range(256)) * 93 + bytes(range(192))
USAGE = {"promptTokenCount": 7, "candidatesTokenCount": 120, "totalTokenCount": 127}


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x00" * int(seconds * rate))
    return buffer.getvalue()


UPLOAD = _wav(2.0)
TOGETHER_WAV = _wav(1.0, rate=24000)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _answer(
    *parts: dict[str, Any], usage: dict[str, int] | None = USAGE
) -> httpx.Response:
    payload: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": list(parts)},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "modelVersion": TTS_MODEL,
    }
    if usage is not None:
        payload["usageMetadata"] = usage
    return httpx.Response(200, json=payload)


def _audio(data: bytes = PCM, mime: str = L16, **kwargs: Any) -> httpx.Response:
    return _answer({"inlineData": {"mimeType": mime, "data": _b64(data)}}, **kwargs)


def _text(*texts: str, **kwargs: Any) -> httpx.Response:
    return _answer(*({"text": text} for text in texts), **kwargs)


class Upstream:
    """Per-host scripted answers; a host's last answer repeats."""

    def __init__(self, answers: dict[str, list[httpx.Response]]) -> None:
        self.answers = answers
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        queue = self.answers.get(request.url.host)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(
            200, content=TOGETHER_WAV, headers={"content-type": "audio/wav"}
        )

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.seen]


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "GEMINI_API_KEY": GEMINI_KEY,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_TTS": f"gemini/{TTS_MODEL}",
        "MODEL_ASR": f"gemini/{ASR_MODEL}",
    }
    base.update(values)
    return Settings.model_validate(base)


def _client(settings: Settings, upstream: Upstream) -> TestClient:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry))


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    return conn


def _row(tmp_path: Path) -> sqlite3.Row:
    # Closing the stores drains the writer thread, so the row is on disk.
    request_log.reset_request_log_stores()
    conn = _db(tmp_path)
    try:
        (row,) = conn.execute("SELECT * FROM requests").fetchall()
        return row
    finally:
        conn.close()


def _attempt_kinds(tmp_path: Path, request_id: str) -> list[str | None]:
    conn = _db(tmp_path)
    try:
        return [
            kind
            for (kind,) in conn.execute(
                "SELECT error_kind FROM request_attempts WHERE request_id = ?"
                " ORDER BY attempt",
                (request_id,),
            )
        ]
    finally:
        conn.close()


def _not_forwarded(row: sqlite3.Row) -> list[str]:
    return json.loads(row["params"])["media"].get("not_forwarded", [])


def _wav_frames(data: bytes) -> tuple[int, int, int, bytes]:
    with wave.open(io.BytesIO(data)) as reader:
        return (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
            reader.readframes(reader.getnframes()),
        )


def _transcribe(client: TestClient, **fields: str) -> httpx.Response:
    return client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", UPLOAD, "audio/wav")},
        data={"model": "whisper-1", **fields},
    )


# ------------------------------------------------------------------ speech


def test_tts_request_is_native_generate_content(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio()]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"model": "gpt-4o-mini-tts", "input": "Hello there", "voice": "Kore"},
        )
    assert response.status_code == 200, response.text
    (sent,) = upstream.seen
    assert str(sent.url) == (
        f"https://{GOOGLE}/v1beta/models/{TTS_MODEL}:generateContent"
    )
    assert sent.headers["x-goog-api-key"] == GEMINI_KEY
    assert "authorization" not in sent.headers
    assert json.loads(sent.content) == {
        "contents": [{"parts": [{"text": "Hello there"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}
            },
        },
    }
    row = _row(tmp_path)
    assert row["provider"] == "gemini"
    assert row["resolved_model"] == TTS_MODEL
    assert _not_forwarded(row) == []


def test_unknown_voice_not_forwarded_and_recorded(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio()]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={
                "input": "hi",
                "voice": "alloy",
                "instructions": "Speak slowly.",
                "speed": 1.25,
            },
        )
    assert response.status_code == 200, response.text
    sent = json.loads(upstream.seen[0].content)
    # No speechConfig: the host's default voice speaks.
    assert sent["generationConfig"] == {"responseModalities": ["AUDIO"]}
    assert _not_forwarded(_row(tmp_path)) == ["voice", "instructions", "speed"]


def test_tts_pcm_wrapped_as_wav_when_wav_named(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio()]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"input": "hi", "voice": "Puck", "response_format": "wav"},
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/wav")
    # Framing only: the samples are the host's own, byte for byte.
    assert _wav_frames(response.content) == (1, 2, 24000, PCM)
    # response_format is honoured by the framing, never sent to Gemini.
    assert "response_format" not in json.loads(upstream.seen[0].content)
    row = _row(tmp_path)
    assert row["output_audio_seconds"] == pytest.approx(len(PCM) / 48000)
    assert row["media_bytes_out"] == len(response.content)
    assert _not_forwarded(row) == []


def test_tts_pcm_passthrough_when_omitted(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio()]})
    with _client(settings, upstream) as client:
        response = client.post("/v1/audio/speech", json={"input": "hi"})
    assert response.status_code == 200, response.text
    assert response.content == PCM
    assert response.headers["content-type"] == L16
    row = _row(tmp_path)
    assert row["output_audio_seconds"] == pytest.approx(len(PCM) / 48000)


def test_tts_wav_answer_stripped_to_pcm_when_pcm_named(monkeypatch, tmp_path) -> None:
    """A host that answers WAV gives a ``pcm`` client the data chunk only."""
    settings = _settings(monkeypatch, tmp_path)
    wav = gemini_native_media.pcm_to_wav(PCM, 24000)
    upstream = Upstream({GOOGLE: [_audio(wav, "audio/wav")]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech", json={"input": "hi", "response_format": "pcm"}
        )
    assert response.status_code == 200, response.text
    assert response.content == PCM
    assert response.headers["content-type"] == L16


def test_tts_mp3_named_skips_gemini_uncharged(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch, tmp_path, MODEL_TTS_FALLBACKS="together/cartesia/sonic-2"
    )
    upstream = Upstream({GOOGLE: [_audio()]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech", json={"input": "hi", "response_format": "mp3"}
        )
    assert response.status_code == 200, response.text
    assert upstream.hosts() == [TOGETHER]
    row = _row(tmp_path)
    assert _attempt_kinds(tmp_path, row["id"]) == ["unsupported", None]


def test_tts_answer_without_audio_falls_back(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch, tmp_path, MODEL_TTS_FALLBACKS="together/cartesia/sonic-2"
    )
    upstream = Upstream({GOOGLE: [_text("I cannot say that.")]})
    with _client(settings, upstream) as client:
        response = client.post("/v1/audio/speech", json={"input": "hi"})
    assert response.status_code == 200, response.text
    assert response.content == TOGETHER_WAV
    assert upstream.hosts() == [GOOGLE, TOGETHER]
    row = _row(tmp_path)
    kinds = _attempt_kinds(tmp_path, row["id"])
    assert len(kinds) == 2 and kinds[0] is not None and kinds[1] is None
    conn = _db(tmp_path)
    try:
        (message,) = conn.execute(
            "SELECT error_message FROM request_attempts WHERE request_id = ?"
            " AND attempt = 0",
            (row["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert "gemini answered without audio" in message


def test_tts_pcm_without_rate_is_not_framed_as_wav(monkeypatch, tmp_path) -> None:
    """No rate on the PCM: a WAV header would be a guess, so the chain moves on."""
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio(PCM, "audio/L16;codec=pcm")]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech", json={"input": "hi", "response_format": "wav"}
        )
    assert response.status_code == 502
    assert "sample rate" in response.json()["error"]["message"]


def test_usage_metadata_to_tokens(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio()]})
    with _client(settings, upstream) as client:
        assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 200
    row = _row(tmp_path)
    assert (row["tokens_in"], row["tokens_out"]) == (7, 120)


def test_no_usage_metadata_logs_no_tokens(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio(usage=None)]})
    with _client(settings, upstream) as client:
        assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 200
    row = _row(tmp_path)
    assert (row["tokens_in"], row["tokens_out"]) == (None, None)


# ----------------------------------------------------------- transcription


def test_transcribe_via_generate_content_with_instruction(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_text("Bonjour ", "tout le monde")]})
    with _client(settings, upstream) as client:
        response = _transcribe(
            client, language="fr", prompt="Names: Zoé", temperature="0"
        )
    assert response.status_code == 200, response.text
    (sent,) = upstream.seen
    assert str(sent.url) == (
        f"https://{GOOGLE}/v1beta/models/{ASR_MODEL}:generateContent"
    )
    assert sent.headers["x-goog-api-key"] == GEMINI_KEY
    assert "authorization" not in sent.headers
    assert sent.headers["content-type"] == "application/json"
    assert json.loads(sent.content) == {
        "contents": [
            {
                "parts": [
                    {"text": f"{TRANSCRIBE_INSTRUCTION} The speech is in fr."},
                    {"inlineData": {"mimeType": "audio/wav", "data": _b64(UPLOAD)}},
                ]
            }
        ]
    }
    assert response.json()["text"] == "Bonjour tout le monde"
    row = _row(tmp_path)
    assert row["media_operation"] == "transcribe"
    assert row["output_chars"] == len("Bonjour tout le monde")
    assert row["input_audio_seconds"] == pytest.approx(2.0)
    assert _not_forwarded(row) == ["prompt", "temperature"]


def test_transcribe_instruction_without_language(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_text("hello")]})
    with _client(settings, upstream) as client:
        assert _transcribe(client).status_code == 200
    parts = json.loads(upstream.seen[0].content)["contents"][0]["parts"]
    assert parts[0] == {"text": TRANSCRIBE_INSTRUCTION}


def test_transcribe_json_and_text_formats(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_text("hello world")]})
    with _client(settings, upstream) as client:
        as_json = _transcribe(client, response_format="json")
        as_text = _transcribe(client, response_format="text")
    assert as_json.status_code == 200, as_json.text
    assert as_json.headers["content-type"].startswith("application/json")
    assert as_json.json() == {
        "text": "hello world",
        "usage": {
            "type": "tokens",
            "input_tokens": 7,
            "output_tokens": 120,
            "total_tokens": 127,
        },
    }
    assert as_text.status_code == 200, as_text.text
    assert as_text.headers["content-type"].startswith("text/plain")
    assert as_text.text == "hello world"
    for sent in upstream.seen:
        assert "response_format" not in sent.content.decode()
    request_log.reset_request_log_stores()
    conn = _db(tmp_path)
    try:
        rows = conn.execute(
            "SELECT tokens_in, tokens_out, output_chars FROM requests ORDER BY ts_epoch"
        ).fetchall()
    finally:
        conn.close()
    # The text answer cannot carry usage; the log still has the host's counts.
    assert [tuple(row) for row in rows] == [
        (7, 120, len("hello world")),
        (7, 120, len("hello world")),
    ]


def test_transcribe_srt_named_skips_gemini_uncharged(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_ASR_FALLBACKS="together/openai/whisper-large-v3",
    )
    upstream = Upstream(
        {TOGETHER: [httpx.Response(200, text="1\n00:00:00,000 --> 00:00:01,000\nhi\n")]}
    )
    with _client(settings, upstream) as client:
        response = _transcribe(client, response_format="srt")
    assert response.status_code == 200, response.text
    assert upstream.hosts() == [TOGETHER]
    row = _row(tmp_path)
    assert _attempt_kinds(tmp_path, row["id"]) == ["unsupported", None]


def test_transcribe_answer_without_text_falls_back(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_ASR_FALLBACKS="together/openai/whisper-large-v3",
    )
    blocked = httpx.Response(
        200, json={"candidates": [{"finishReason": "SAFETY", "index": 0}]}
    )
    upstream = Upstream(
        {GOOGLE: [blocked], TOGETHER: [httpx.Response(200, json={"text": "hi"})]}
    )
    with _client(settings, upstream) as client:
        response = _transcribe(client)
    assert response.status_code == 200, response.text
    assert response.json() == {"text": "hi"}
    assert upstream.hosts() == [GOOGLE, TOGETHER]


def test_translation_is_not_declared_for_gemini(monkeypatch, tmp_path) -> None:
    surfaces = PROVIDER_CATALOG["gemini"].media_surfaces
    assert surface_for(surfaces, MEDIA_OPERATION_TRANSLATE) is None
    speech = surface_for(surfaces, MEDIA_OPERATION_SPEECH)
    transcribe = surface_for(surfaces, MEDIA_OPERATION_TRANSCRIBE)
    assert speech is not None and speech.voices == GEMINI_TTS_VOICES
    assert len(GEMINI_TTS_VOICES) == 30
    assert transcribe is not None and transcribe.formats == ("json", "text")
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/translations",
            files={"file": ("clip.wav", UPLOAD, "audio/wav")},
            data={"model": "whisper-1"},
        )
    assert response.status_code == 400
    assert upstream.seen == []


# ------------------------------------------------------ Gemini-shaped client


def test_gemini_speech_from_gemini_shaped_client(monkeypatch, tmp_path) -> None:
    """M6 inbound AUDIO -> the Speech rail -> Gemini native -> inlineData back."""
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_audio()]})
    payload = {
        "contents": [{"parts": [{"text": "Say cheerfully: have a wonderful day!"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}
            },
        },
    }
    with _client(settings, upstream) as client:
        response = client.post("/v1beta/models/mcc-tts:generateContent", json=payload)
    assert response.status_code == 200, response.text
    sent = json.loads(upstream.seen[0].content)
    assert sent["contents"] == payload["contents"]
    assert sent["generationConfig"] == payload["generationConfig"]
    body = response.json()
    (part,) = body["candidates"][0]["content"]["parts"]
    assert part == {"inlineData": {"mimeType": L16, "data": _b64(PCM)}}
    assert body["usageMetadata"] == USAGE
    row = _row(tmp_path)
    assert row["protocol"] == "gemini"
    assert (row["tokens_in"], row["tokens_out"]) == (7, 120)


# --------------------------------------------------------------- the loop


def _off_loop(name: str, calls: list[str], real):
    def sentinel(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)
            return real(*args, **kwargs)
        raise AssertionError(f"{name} ran on the event loop thread")

    return sentinel


def test_base64_and_translation_run_off_the_loop(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        adapters,
        "transcribe_request",
        _off_loop("encode", calls, gemini_native_media.transcribe_request),
    )
    monkeypatch.setattr(
        leaf,
        "transcript_answer",
        _off_loop("transcript", calls, gemini_native_media.transcript_answer),
    )
    monkeypatch.setattr(
        leaf,
        "speech_answer",
        _off_loop("speech", calls, gemini_native_media.speech_answer),
    )
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream({GOOGLE: [_text("hello"), _audio()]})
    with _client(settings, upstream) as client:
        assert _transcribe(client).status_code == 200
        assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 200
    assert calls == ["encode", "transcript", "speech"]
