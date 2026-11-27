"""Tests for PR-M6 (PR 30): NVIDIA NIM Riva gRPC ASR, Gemini translate, and Cloudflare/HF surfaces."""

import asyncio
import base64
import io
import json
import sqlite3
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_SHAPE_GEMINI_TRANSLATE,
    MEDIA_SHAPE_NIM_RIVA_GRPC,
    surface_for,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.core import gemini_native_media, request_log
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.gemini_native_media import (
    TRANSLATE_INSTRUCTION,
    translate_request,
)
from my_claude_code.providers.media import adapters, leaf
from my_claude_code.providers.media.leaf import MediaLeaf
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.nvidia_nim import voice as nim_voice
from my_claude_code.providers.nvidia_nim.voice import transcribe_riva_bytes
from tests.api.support import create_test_app, requests_as_read


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


WAV = _wav()
NIM_KEY = "nvapi-" + "n" * 32
GEMINI_KEY = "AIza" + "g" * 35
CF_TOKEN = "cf-" + "c" * 32
CF_ACCOUNT = "acct-42"
HF_KEY = "hf_" + "h" * 32


def _fake_riva_client(
    transcript: str,
) -> tuple[SimpleNamespace, SimpleNamespace, MagicMock, MagicMock]:
    response = SimpleNamespace(
        results=[
            SimpleNamespace(
                alternatives=[SimpleNamespace(transcript=transcript)],
            )
        ]
    )
    asr_service = MagicMock()
    asr_service.offline_recognize.return_value = response
    auth = MagicMock()
    client = SimpleNamespace(
        Auth=MagicMock(return_value=auth),
        ASRService=MagicMock(return_value=asr_service),
        RecognitionConfig=MagicMock(return_value=object()),
    )
    riva = SimpleNamespace(__path__=[], client=client)
    return riva, client, asr_service, auth


def _settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str
) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "NVIDIA_NIM_API_KEY": NIM_KEY,
        "GEMINI_API_KEY": GEMINI_KEY,
        "CLOUDFLARE_API_TOKEN": CF_TOKEN,
        "CLOUDFLARE_ACCOUNT_ID": CF_ACCOUNT,
        "HUGGINGFACE_API_KEY": HF_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
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


# ------------------------------------------------------------ NVIDIA NIM Riva


def test_transcribe_riva_bytes_unit_and_errors() -> None:
    _riva, client, asr_service, auth = _fake_riva_client("hello from riva")
    body, content_type = transcribe_riva_bytes(
        WAV,
        model="nvidia/parakeet-ctc-1.1b-asr",
        api_key=NIM_KEY,
        riva_client_module=client,
        response_format="json",
    )
    assert content_type == "application/json"
    assert json.loads(body) == {"text": "hello from riva"}
    client.Auth.assert_called_once_with(
        use_ssl=True,
        uri="grpc.nvcf.nvidia.com:443",
        metadata_args=[
            ["function-id", "1598d209-5e27-4d3c-8079-4751568b1081"],
            ["authorization", f"Bearer {NIM_KEY}"],
        ],
    )
    client.RecognitionConfig.assert_called_once_with(
        language_code="en-US",
        max_alternatives=1,
        verbatim_transcripts=True,
    )
    asr_service.offline_recognize.assert_called_once()
    auth.channel.close.assert_called_once_with()

    # Custom/unknown model passes through as function_id and text format works.
    _riva2, client2, _asr2, auth2 = _fake_riva_client("custom text")
    text_body, text_ct = transcribe_riva_bytes(
        WAV,
        model="custom-fn-id-123",
        api_key=NIM_KEY,
        language="fr-FR",
        riva_client_module=client2,
        response_format="text",
    )
    assert text_ct == "text/plain; charset=utf-8"
    assert text_body == b"custom text"
    client2.Auth.assert_called_once_with(
        use_ssl=True,
        uri="grpc.nvcf.nvidia.com:443",
        metadata_args=[
            ["function-id", "custom-fn-id-123"],
            ["authorization", f"Bearer {NIM_KEY}"],
        ],
    )
    client2.RecognitionConfig.assert_called_once_with(
        language_code="fr-FR",
        max_alternatives=1,
        verbatim_transcripts=True,
    )
    auth2.channel.close.assert_called_once_with()

    # Missing key -> ExecutionFailure(INVALID_REQUEST)
    with pytest.raises(ExecutionFailure) as missing_key:
        transcribe_riva_bytes(WAV, model="nvidia/parakeet-ctc-1.1b-asr", api_key="  ")
    assert missing_key.value.kind == FailureKind.INVALID_REQUEST

    # Missing riva.client -> ExecutionFailure(UPSTREAM)
    real_import = __import__

    def no_riva(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "riva.client" or name.startswith("riva"):
            raise ImportError("No module named 'riva'")
        return real_import(name, *args, **kwargs)

    with (
        patch.dict(sys.modules, {"riva": None, "riva.client": None}),
        pytest.raises(ExecutionFailure) as missing_pkg,
    ):
        transcribe_riva_bytes(
            WAV, model="nvidia/parakeet-ctc-1.1b-asr", api_key=NIM_KEY
        )
    assert missing_pkg.value.kind == FailureKind.UPSTREAM
    assert "voice" in missing_pkg.value.message
    del no_riva


def test_nvidia_nim_model_asr_route_off_loop_and_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    surface = surface_for(
        PROVIDER_CATALOG["nvidia_nim"].media_surfaces, MEDIA_OPERATION_TRANSCRIBE
    )
    assert surface is not None
    assert surface.shape == MEDIA_SHAPE_NIM_RIVA_GRPC

    riva, client, _asr_service, auth = _fake_riva_client("riva e2e transcript")
    calls: list[str] = []
    real_transcribe = nim_voice.transcribe_riva_bytes

    def off_loop_transcribe(*args: Any, **kwargs: Any) -> tuple[bytes, str]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("transcribe_riva_bytes")
            return real_transcribe(*args, **kwargs)
        raise AssertionError("transcribe_riva_bytes ran on the event loop thread")

    monkeypatch.setattr(leaf, "transcribe_riva_bytes", off_loop_transcribe)

    settings = _settings(
        monkeypatch, tmp_path, MODEL_ASR="nvidia_nim/nvidia/parakeet-ctc-1.1b-asr"
    )
    seen_http: list[httpx.Request] = []

    def fail_if_http(request: httpx.Request) -> httpx.Response:
        seen_http.append(request)
        raise AssertionError(f"Unexpected HTTP call for Riva gRPC: {request.url}")

    registry = MediaRegistry(transport=httpx.MockTransport(fail_if_http))
    with (
        patch.dict(sys.modules, {"riva": riva, "riva.client": client}),
        TestClient(create_test_app(settings, media=registry)) as http_client,
    ):
        resp = http_client.post(
            "/v1/audio/transcriptions",
            files={"file": ("clip.wav", WAV, "audio/wav")},
            data={"prompt": "ignored hint"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"text": "riva e2e transcript"}
    assert seen_http == []
    assert calls == ["transcribe_riva_bytes"]
    auth.channel.close.assert_called_once_with()

    rows = _rows(tmp_path)
    assert len(rows) == 1
    params = json.loads(rows[0]["params"])
    assert params["media"]["surface"] == MEDIA_SHAPE_NIM_RIVA_GRPC
    assert "prompt" in params["media"]["not_forwarded"]


# ------------------------------------------------------------ Gemini translate


def test_gemini_translate_request_and_off_loop_e2e(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    surface = surface_for(
        PROVIDER_CATALOG["gemini"].media_surfaces, MEDIA_OPERATION_TRANSLATE
    )
    assert surface is not None
    assert surface.shape == MEDIA_SHAPE_GEMINI_TRANSLATE

    raw_req = json.loads(translate_request("audio/wav", WAV))
    parts = raw_req["contents"][0]["parts"]
    assert parts[0] == {"text": TRANSLATE_INSTRUCTION}
    assert parts[1] == {
        "inlineData": {
            "mimeType": "audio/wav",
            "data": base64.b64encode(WAV).decode("ascii"),
        }
    }

    calls: list[str] = []
    real_translate_req = gemini_native_media.translate_request
    real_transcript_ans = gemini_native_media.transcript_answer

    def off_loop_req(*args: Any, **kwargs: Any) -> bytes:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("translate_request")
            return real_translate_req(*args, **kwargs)
        raise AssertionError("translate_request ran on the event loop thread")

    def off_loop_ans(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append("transcript_answer")
            return real_transcript_ans(*args, **kwargs)
        raise AssertionError("transcript_answer ran on the event loop thread")

    monkeypatch.setattr(adapters, "translate_request", off_loop_req)
    monkeypatch.setattr(leaf, "transcript_answer", off_loop_ans)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "Good morning!"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 13,
                },
            },
        )

    settings = _settings(monkeypatch, tmp_path, MODEL_ASR="gemini/gemini-2.5-flash")
    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as http_client:
        resp_json = http_client.post(
            "/v1/audio/translations",
            files={"file": ("clip.wav", WAV, "audio/wav")},
            data={"temperature": "0.2"},
        )
        resp_text = http_client.post(
            "/v1/audio/translations",
            files={"file": ("clip.wav", WAV, "audio/wav")},
            data={"response_format": "text"},
        )

    assert resp_json.status_code == 200, resp_json.text
    assert resp_json.json()["text"] == "Good morning!"
    assert resp_text.status_code == 200, resp_text.text
    assert resp_text.text == "Good morning!"
    assert resp_text.headers["content-type"].startswith("text/plain")
    assert calls == [
        "translate_request",
        "transcript_answer",
        "translate_request",
        "transcript_answer",
    ]
    assert len(seen) == 2
    assert seen[0].url.path == "/v1beta/models/gemini-2.5-flash:generateContent"
    assert seen[0].headers["x-goog-api-key"] == GEMINI_KEY


# -------------------------------------------- Cloudflare & HuggingFace surfaces


def test_cloudflare_and_huggingface_surfaces_and_account_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for provider_id in ("cloudflare", "huggingface"):
        surfaces = PROVIDER_CATALOG[provider_id].media_surfaces
        assert surface_for(surfaces, MEDIA_OPERATION_IMAGE_GENERATE) is not None
        assert surface_for(surfaces, MEDIA_OPERATION_SPEECH) is not None
        assert surface_for(surfaces, MEDIA_OPERATION_TRANSCRIBE) is not None

    # Cloudflare account-scoped URL resolution in MediaRegistry.resolve
    settings_cf = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="cloudflare/@cf/black-forest-labs/flux-1-schnell",
        MODEL_TTS="cloudflare/@cf/myshell-ai/melotts",
        MODEL_ASR="cloudflare/@cf/openai/whisper",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"b64_json": "AAAA"}]})
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(
                200, content=WAV, headers={"content-type": "audio/wav"}
            )
        return httpx.Response(200, json={"text": "cf transcript"})

    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    node = registry.resolve("cloudflare", settings_cf)
    assert isinstance(node, MediaLeaf)
    assert (
        node.config.base_url
        == f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/v1"
    )

    with TestClient(create_test_app(settings_cf, media=registry)) as client:
        img = client.post("/v1/images/generations", json={"prompt": "lighthouse"})
        tts = client.post("/v1/audio/speech", json={"input": "hello"})
        asr = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("clip.wav", WAV, "audio/wav")},
        )

    assert img.status_code == 200
    assert tts.status_code == 200
    assert asr.status_code == 200
    assert [req.url.path for req in seen] == [
        f"/client/v4/accounts/{CF_ACCOUNT}/ai/v1/images/generations",
        f"/client/v4/accounts/{CF_ACCOUNT}/ai/v1/audio/speech",
        f"/client/v4/accounts/{CF_ACCOUNT}/ai/v1/audio/transcriptions",
    ]

    # HuggingFace routing
    seen.clear()
    settings_hf = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="huggingface/black-forest-labs/FLUX.1-schnell",
        MODEL_TTS="huggingface/hexgrad/Kokoro-82M",
        MODEL_ASR="huggingface/openai/whisper-large-v3",
    )
    hf_registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings_hf, media=hf_registry)) as client:
        assert (
            client.post("/v1/images/generations", json={"prompt": "cat"}).status_code
            == 200
        )
        assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 200
        assert (
            client.post(
                "/v1/audio/transcriptions",
                files={"file": ("clip.wav", WAV, "audio/wav")},
            ).status_code
            == 200
        )
    assert [req.url.path for req in seen] == [
        "/v1/images/generations",
        "/v1/audio/speech",
        "/v1/audio/transcriptions",
    ]
