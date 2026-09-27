"""``POST /v1/audio/speech``: the Speech rail, the format gate, the audio back.

The upstream is a MockTransport recording what it received; the audio it
returns is a real WAV so the logged length can be checked against its header.
"""

import io
import json
import sqlite3
import wave
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.media_store import media_file_path, media_root
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


WAV = _wav(1.5)
MP3 = b"ID3\x03\x00\x00\x00" + b"\x11" * 200


class Upstream:
    def __init__(self, answers: dict[str, list[httpx.Response]] | None = None) -> None:
        self.answers = answers or {}
        self.seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(
            (request.url.host, request.url.path, json.loads(request.content))
        )
        queue = self.answers.get(request.url.host)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "GROQ_API_KEY": "gsk_" + "c" * 40,
        "OPENROUTER_API_KEY": "sk-or-" + "d" * 40,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_TTS": "together/cartesia/sonic-2",
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


def test_bytes_come_back_with_the_hosts_content_type(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"model": "gpt-4o-mini-tts", "input": "Hello there", "voice": "alloy"},
        )
    assert response.status_code == 200
    assert response.content == WAV
    assert response.headers["content-type"].startswith("audio/wav")
    host, path, body = upstream.seen[0]
    assert (host, path) == ("api.together.ai", "/v1/audio/speech")
    assert body == {
        "model": "cartesia/sonic-2",
        "input": "Hello there",
        "voice": "alloy",
    }
    row = _row(tmp_path)
    assert row["endpoint"] == "/v1/audio/speech"
    assert row["media_operation"] == "speech"
    assert row["input_chars"] == len("Hello there")
    assert row["output_audio_seconds"] == 1.5
    assert row["media_bytes_out"] == len(WAV)
    assert row["output_image_count"] is None


def test_named_format_unsupported_skips_candidate(monkeypatch, tmp_path) -> None:
    """OpenRouter documents mp3 and pcm only: a named wav skips it, uncharged."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_TTS="open_router/openai/gpt-4o-mini-tts",
        MODEL_TTS_FALLBACKS="together/cartesia/sonic-2",
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"input": "hi", "voice": "alloy", "response_format": "wav"},
        )
    assert response.status_code == 200
    assert [seen[0] for seen in upstream.seen] == ["api.together.ai"]
    row = _row(tmp_path)
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        kinds = [
            kind
            for (kind,) in conn.execute(
                "SELECT error_kind FROM request_attempts WHERE request_id = ?"
                " ORDER BY attempt",
                (row["id"],),
            )
        ]
    finally:
        conn.close()
    assert kinds == ["unsupported", None]


def test_a_supported_named_format_is_forwarded(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch, tmp_path, MODEL_TTS="open_router/openai/gpt-4o-mini-tts"
    )
    upstream = Upstream(
        {
            "openrouter.ai": [
                httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"input": "hi", "voice": "alloy", "response_format": "mp3"},
        )
    assert response.status_code == 200
    assert response.content == MP3
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert upstream.seen[0][1] == "/api/v1/audio/speech"
    assert upstream.seen[0][2]["response_format"] == "mp3"
    row = _row(tmp_path)
    # An MP3 does not state its length without decoding: not measured.
    assert row["output_audio_seconds"] is None
    assert row["media_bytes_out"] == len(MP3)


def test_omitted_format_returns_native_content_type(monkeypatch, tmp_path) -> None:
    """No format named: nothing is gated, and the host's own container comes back."""
    settings = _settings(
        monkeypatch, tmp_path, MODEL_TTS="groq/canopylabs/orpheus-v1-english"
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech", json={"input": "hi", "voice": "tara"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert "response_format" not in upstream.seen[0][2]
    assert upstream.seen[0][1] == "/openai/v1/audio/speech"


def test_falls_back_on_5xx(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_TTS="groq/canopylabs/orpheus-v1-english",
        MODEL_TTS_FALLBACKS="together/cartesia/sonic-2",
    )
    upstream = Upstream(
        {"api.groq.com": [httpx.Response(503, json={"error": {"message": "busy"}})]}
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech", json={"input": "hi", "voice": "tara"}
        )
    assert response.status_code == 200
    assert [seen[0] for seen in upstream.seen] == ["api.groq.com", "api.together.ai"]


def test_sse_request_skips_surfaces_that_do_not_declare_it(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"input": "hi", "voice": "alloy", "stream_format": "sse"},
        )
    assert response.status_code == 400
    assert upstream.seen == []


def test_empty_rail_names_model_tts(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_TTS="")
    with _client(settings, Upstream()) as client:
        response = client.post(
            "/v1/audio/speech", json={"input": "hi", "voice": "alloy"}
        )
    assert response.status_code == 404
    assert "MODEL_TTS" in response.json()["error"]["message"]


def test_input_is_required(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with _client(settings, Upstream()) as client:
        response = client.post("/v1/audio/speech", json={"voice": "alloy"})
    assert response.status_code == 400


def test_the_audio_is_stored_when_the_store_is_on(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true")
    with _client(settings, Upstream()) as client:
        assert (
            client.post(
                "/v1/audio/speech", json={"input": "hi", "voice": "alloy"}
            ).status_code
            == 200
        )
    row = _row(tmp_path)
    path = media_file_path(
        media_root(tmp_path / "requests.db"), row["media_sha_out"], "audio/wav"
    )
    assert path.suffix == ".wav"
    assert path.read_bytes() == WAV
