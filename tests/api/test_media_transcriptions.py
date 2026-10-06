"""``POST /v1/audio/transcriptions`` and ``/translations`` on the Transcription rail."""

import io
import json
import sqlite3
import time
import wave
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import media_routes
from my_claude_code.config.media_surfaces import transcription_surface
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


WAV = _wav(2.0)


def _parts(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.content)
    parts: dict[str, tuple[str | None, bytes]] = {}
    for part in message.iter_parts():
        params = dict(part.get_params(header="content-disposition") or [])
        payload = part.get_payload(decode=True)
        parts[str(params.get("name"))] = (
            params.get("filename"),
            payload if isinstance(payload, bytes) else b"",
        )
    return parts


class Upstream:
    def __init__(self, answers: dict[str, list[httpx.Response]] | None = None) -> None:
        self.answers = answers or {}
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        queue = self.answers.get(request.url.host)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json={"text": "hello world"})


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "GROQ_API_KEY": "gsk_" + "c" * 40,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "MISTRAL_API_KEY": "mi-" + "e" * 40,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_ASR": "groq/whisper-large-v3",
    }
    base.update(values)
    return Settings.model_validate(base)


def _client(settings: Settings, upstream: Upstream) -> TestClient:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry))


def _row(tmp_path: Path) -> sqlite3.Row:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        (row,) = conn.execute("SELECT * FROM requests").fetchall()
        return row
    finally:
        conn.close()


def test_multipart_forwarded(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper-1", "language": "en", "response_format": "json"},
            files=[("file", ("clip.wav", WAV, "audio/wav"))],
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"text": "hello world"}
    (request,) = upstream.seen
    assert request.url.path == "/openai/v1/audio/transcriptions"
    parts = _parts(request)
    assert parts["file"] == ("clip.wav", WAV)
    assert parts["model"][1] == b"whisper-large-v3"
    assert parts["language"][1] == b"en"
    row = _row(tmp_path)
    assert row["media_operation"] == "transcribe"
    assert row["input_audio_seconds"] == 2.0
    assert row["output_chars"] == len("hello world")
    assert row["output_image_count"] is None


def test_the_hosts_stated_duration_wins(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            "api.groq.com": [
                httpx.Response(
                    200,
                    json={"text": "hi", "usage": {"type": "duration", "seconds": 7}},
                )
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper-1"},
            files=[("file", ("clip.mp3", b"ID3" + b"\x00" * 50, "audio/mpeg"))],
        )
    assert response.status_code == 200
    assert _row(tmp_path)["input_audio_seconds"] == 7.0


@pytest.mark.parametrize(
    ("fmt", "body", "content_type"),
    [
        ("text", b"hello world", "text/plain"),
        ("srt", b"1\n00:00:00,000 --> 00:00:01,000\nhello world\n", "text/plain"),
    ],
)
def test_text_and_srt_formats_passthrough(
    monkeypatch, tmp_path, fmt, body, content_type
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            "api.groq.com": [
                httpx.Response(
                    200, content=body, headers={"content-type": content_type}
                )
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"response_format": fmt},
            files=[("file", ("clip.wav", WAV, "audio/wav"))],
        )
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"].startswith(content_type)
    assert _row(tmp_path)["output_chars"] == len(body.decode())


def test_translation_requires_translation_surface(monkeypatch, tmp_path) -> None:
    """Together transcribes but does not translate: it is skipped, uncharged."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_ASR="together/openai/whisper-large-v3",
        MODEL_ASR_FALLBACKS="groq/whisper-large-v3",
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/translations",
            files=[("file", ("clip.wav", WAV, "audio/wav"))],
        )
    assert response.status_code == 200, response.text
    assert [(r.url.host, r.url.path) for r in upstream.seen] == [
        ("api.groq.com", "/openai/v1/audio/translations")
    ]
    assert _row(tmp_path)["media_operation"] == "translate"


def test_falls_back_and_resends_the_whole_file(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_ASR="groq/whisper-large-v3",
        MODEL_ASR_FALLBACKS="together/openai/whisper-large-v3",
    )
    upstream = Upstream(
        {"api.groq.com": [httpx.Response(500, json={"error": {"message": "x"}})]}
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions", files=[("file", ("clip.wav", WAV, "audio/wav"))]
        )
    assert response.status_code == 200
    assert [r.url.host for r in upstream.seen] == ["api.groq.com", "api.together.ai"]
    for request in upstream.seen:
        assert _parts(request)["file"] == ("clip.wav", WAV)


def _sse(*events: dict) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


def test_stream_deltas(monkeypatch, tmp_path) -> None:
    """Mistral declares SSE on transcriptions: deltas are forwarded as they come."""
    settings = _settings(monkeypatch, tmp_path, MODEL_ASR="mistral/voxtral-mini-latest")
    frames = _sse(
        {"type": "transcript.text.delta", "delta": "hello "},
        {"type": "transcript.text.delta", "delta": "world"},
        {
            "type": "transcript.text.done",
            "text": "hello world",
            "usage": {"type": "duration", "seconds": 3},
        },
    )
    upstream = Upstream(
        {
            "api.mistral.ai": [
                httpx.Response(
                    200, content=frames, headers={"content-type": "text/event-stream"}
                )
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"stream": "true"},
            files=[("file", ("clip.wav", WAV, "audio/wav"))],
        )
        assert response.status_code == 200
        assert response.content == frames
    assert _parts(upstream.seen[0])["stream"][1] == b"true"
    row = _row(tmp_path)
    assert row["stream"] == 1
    assert row["output_chars"] == len("hello world")
    assert row["input_audio_seconds"] == 3.0


_ANSWERED = _sse(
    {"type": "transcript.text.delta", "delta": "hello "},
    {
        "type": "transcript.text.done",
        "text": "hello world",
        "usage": {"type": "duration", "seconds": 3, "cost": 0.0042, "is_byok": False},
    },
)
_ERRORED = _sse({"type": "transcript.text.delta", "delta": "hello "}) + (
    b'event: error\ndata: {"error": {"message": "upstream gave up"}}\n\n'
)


@pytest.mark.parametrize(
    ("frames", "status", "cost"),
    [
        (_ANSWERED, "success", (0.0042, "provider")),
        (_ERRORED, "error", (None, "unpriced")),
    ],
    ids=["answered", "errored"],
)
def test_a_finished_stream_is_logged_before_the_store_closes(
    monkeypatch, tmp_path, frames, status, cost
) -> None:
    """A stop closes the log store as soon as the last request has ended.

    The row of a streamed answer used to be written by a task left running
    after the response had ended (7.60.0 to 7.78.3): a stop, restart or update
    that closed the store -- or ended the loop -- first lost the whole row,
    cost and attempts included, though the client had its answer. The parser
    is held 0.3 s on its worker thread here, as a loaded machine can hold it;
    the row was lost on every run.
    """
    parse = media_routes.parse_transcription_stream

    def slow_parse(seen: bytes):
        time.sleep(0.3)
        return parse(seen)

    monkeypatch.setattr(media_routes, "parse_transcription_stream", slow_parse)
    settings = _settings(monkeypatch, tmp_path, MODEL_ASR="mistral/voxtral-mini-latest")
    upstream = Upstream(
        {
            "api.mistral.ai": [
                httpx.Response(
                    200, content=frames, headers={"content-type": "text/event-stream"}
                )
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"stream": "true"},
            files=[("file", ("clip.wav", WAV, "audio/wav"))],
        )
        # What the server's own stop does once no request is left: close the
        # stores (runtime/application.py, _close_owned_resources).
        request_log.reset_request_log_stores()
        assert response.status_code == 200
        assert response.content == frames
    row = _row(tmp_path)
    assert row["status"] == status
    assert row["stream"] == 1
    assert row["media_operation"] == "transcribe"
    assert (row["cost_usd"], row["cost_source"]) == cost


def test_a_stream_skips_a_surface_that_cannot_stream(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"stream": "true"},
            files=[("file", ("clip.wav", WAV, "audio/wav"))],
        )
    assert response.status_code == 400
    assert upstream.seen == []


def test_file_is_required(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with _client(settings, Upstream()) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            data={"model": "whisper-1"},
            files=[("other", ("x.bin", b"x", "application/octet-stream"))],
        )
        assert response.status_code == 400
        assert (
            client.post(
                "/v1/audio/transcriptions", json={"model": "whisper-1"}
            ).status_code
            == 400
        )


def test_empty_rail_names_model_asr(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_ASR="")
    with _client(settings, Upstream()) as client:
        response = client.post(
            "/v1/audio/transcriptions", files=[("file", ("clip.wav", WAV, "audio/wav"))]
        )
    assert response.status_code == 404
    assert "MODEL_ASR" in response.json()["error"]["message"]


def test_mistral_declares_streaming_transcription() -> None:
    assert (
        transcription_surface(stream=True) in PROVIDER_CATALOG["mistral"].media_surfaces
    )
