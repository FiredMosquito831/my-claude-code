"""Tests for PR-M6 (PR 30): ``GET /v1/audio/voices`` and chunked audio speech streaming."""

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.media_surfaces import (
    GEMINI_TTS_VOICES,
    OPENAI_TTS_VOICES,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app, requests_as_read

TOKEN = "proxy-" + "v" * 24
OPENAI_KEY = "sk-test-" + "o" * 32
GEMINI_KEY = "AIza" + "g" * 35
TOGETHER_KEY = "tg-" + "t" * 32


def _settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str
) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "CHATGPT_OAUTH_ACCESS_TOKEN": OPENAI_KEY,
        "GEMINI_API_KEY": GEMINI_KEY,
        "TOGETHER_API_KEY": TOGETHER_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_TTS": "gemini/gemini-2.5-flash-preview-tts",
        "MODEL_TTS_FALLBACKS": "openai/gpt-4o-mini-tts",
    }
    base.update(extra)
    return Settings.model_validate(base)


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM requests ORDER BY ts_epoch").fetchall()
        return [dict(r) for r in requests_as_read(conn, rows)]
    finally:
        conn.close()


# ----------------------------------------------------------- GET /v1/audio/voices


def test_audio_voices_endpoint_auth_probes_and_filters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with TestClient(create_test_app(settings)) as client:
        assert client.get("/v1/audio/voices").status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.head("/v1/audio/voices", headers=headers).status_code == 204
        assert client.options("/v1/audio/voices", headers=headers).status_code == 204

        all_resp = client.get("/v1/audio/voices", headers=headers)
        assert all_resp.status_code == 200, all_resp.text
        payload = all_resp.json()
        assert payload["object"] == "list"
        assert set(payload["by_model"]) == {
            "gemini/gemini-2.5-flash-preview-tts",
            "openai/gpt-4o-mini-tts",
        }
        gemini_voices = payload["by_model"]["gemini/gemini-2.5-flash-preview-tts"]
        openai_voices = payload["by_model"]["openai/gpt-4o-mini-tts"]
        assert [v["id"] for v in gemini_voices] == list(GEMINI_TTS_VOICES)
        assert [v["id"] for v in openai_voices] == list(OPENAI_TTS_VOICES)
        for v in payload["voices"]:
            assert set(v) == {"id", "name", "provider", "model_ref", "source"}
            assert v["name"] == v["id"]

        # Provider filter
        filtered_prov = client.get(
            "/v1/audio/voices?provider=openai", headers=headers
        ).json()
        assert list(filtered_prov["by_model"]) == ["openai/gpt-4o-mini-tts"]
        assert [v["id"] for v in filtered_prov["voices"]] == list(OPENAI_TTS_VOICES)

        # Model filter (bare model name or full ref)
        filtered_model = client.get(
            "/v1/audio/voices?model=gemini-2.5-flash-preview-tts", headers=headers
        ).json()
        assert list(filtered_model["by_model"]) == [
            "gemini/gemini-2.5-flash-preview-tts"
        ]


def test_audio_voices_reads_cached_capability_metadata_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_TTS="together/cartesia/sonic-2",
        MODEL_TTS_FALLBACKS="",
    )
    app = create_test_app(settings)
    monkeypatch.setattr(
        app.state.services.requests,
        "cached_prefixed_model_infos",
        lambda: (
            ProviderModelInfo(
                model_id="together/cartesia/sonic-2",
                published_row=json.dumps(
                    {"supported_voices": ["sonic-en", "sonic-es"]}
                ),
            ),
        ),
    )
    with TestClient(app) as client:
        resp = client.get(
            "/v1/audio/voices", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["by_model"]["together/cartesia/sonic-2"] == [
        {
            "id": "sonic-en",
            "name": "sonic-en",
            "provider": "together",
            "model_ref": "together/cartesia/sonic-2",
            "source": "provider_list",
        },
        {
            "id": "sonic-es",
            "name": "sonic-es",
            "provider": "together",
            "model_ref": "together/cartesia/sonic-2",
            "source": "provider_list",
        },
    ]


def test_audio_voices_empty_tts_rail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_TTS="", MODEL_TTS_FALLBACKS="")
    with TestClient(create_test_app(settings)) as client:
        resp = client.get(
            "/v1/audio/voices", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"object": "list", "voices": [], "by_model": {}}


# ------------------------------------------------ Chunked audio speech streaming


class _ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def test_speech_streaming_sse_and_raw_audio_chunked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chunks_raw = [b"ID3\x03\x00\x00", b"\x11\x22\x33", b"\x44\x55\x66"]
    sse_frames = [
        b'event: speech.audio.delta\ndata: {"type":"speech.audio.delta","audio":"AAAA"}\n\n',
        b'event: speech.audio.done\ndata: {"type":"speech.audio.done","usage":{"input_tokens":4,"output_tokens":9}}\n\n',
    ]
    seen_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_bodies.append(body)
        if body.get("stream_format") == "sse":
            return httpx.Response(
                200,
                stream=_ChunkedByteStream(sse_frames),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            stream=_ChunkedByteStream(chunks_raw),
            headers={"content-type": "audio/mpeg"},
        )

    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_TTS="openai/gpt-4o-mini-tts",
        MODEL_TTS_FALLBACKS="",
    )
    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(create_test_app(settings, media=registry)) as client:
        # 1) SSE stream pass-through
        sse_resp = client.post(
            "/v1/audio/speech",
            headers=headers,
            json={"input": "hello", "voice": "alloy", "stream_format": "sse"},
        )
        assert sse_resp.status_code == 200, sse_resp.text
        assert sse_resp.headers["content-type"].startswith("text/event-stream")
        assert sse_resp.content == b"".join(sse_frames)

        # 2) Raw audio chunked transfer pass-through (stream=True)
        raw_resp = client.post(
            "/v1/audio/speech",
            headers=headers,
            json={"input": "hello raw", "voice": "nova", "stream": True},
        )
        assert raw_resp.status_code == 200, raw_resp.text
        assert raw_resp.headers["content-type"].startswith("audio/mpeg")
        assert raw_resp.content == b"".join(chunks_raw)

    assert len(seen_bodies) == 2
    assert seen_bodies[0]["stream_format"] == "sse"
    assert seen_bodies[1]["stream"] is True

    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert rows[0]["status"] == "success"
    assert rows[0]["stream"] == 1
    assert (rows[0]["tokens_in"], rows[0]["tokens_out"]) == (4, 9)
    assert rows[1]["status"] == "success"
    assert rows[1]["stream"] == 1
    assert rows[1]["media_bytes_out"] == sum(len(c) for c in chunks_raw)
