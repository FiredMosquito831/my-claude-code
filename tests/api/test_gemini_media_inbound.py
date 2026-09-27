"""Gemini-shaped media requests in: pictures, speech and Veo jobs on the media rails.

A ``generateContent`` whose ``responseModalities`` name ``IMAGE`` or ``AUDIO``
goes to the Image or Speech rail and is answered with ``inlineData``;
``:predictLongRunning`` goes to the Video rail and is read back through
``/v1beta/operations`` and ``/v1beta/files``. Fake upstreams only
(``httpx.MockTransport``): no test here reaches a real host.
"""

import asyncio
import base64
import io
import json
import sqlite3
import wave
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import gemini_media_routes, gemini_routes, media_routes
from my_claude_code.api.wire_surfaces import wire_api_for_path
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app, provider_manager_for_app

XAI = "api.x.ai"
TOGETHER = "api.together.ai"
GEMINI = "generativelanguage.googleapis.com"
OPENROUTER = "openrouter.ai"
CDN = "cdn.example.test"
IMAGES_GENERATIONS = "/v1/images/generations"
IMAGES_EDITS = "/v1/images/edits"
SPEECH = "/v1/audio/speech"
GEMINI_VIDEOS = "/v1beta/openai/videos"
OR_VIDEOS = "/api/v1/videos"
XAI_KEY = "xai-" + "a" * 40
TOGETHER_KEY = "tg-" + "b" * 40
GEMINI_KEY = "AIza" + "g" * 35
OR_KEY = "sk-or-v1-" + "c" * 48
PNG_IN = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
PNG_OUT = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
JPEG_OUT = b"\xff\xd8\xff\xe0" + b"\x03" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
IMAGE_PATH = "/v1beta/models/mcc-image:generateContent"
TTS_PATH = "/v1beta/models/mcc-tts:generateContent"
VEO_PATH = "/v1beta/models/veo-3.1-generate-preview:predictLongRunning"
CHAT_MODEL = "nvidia_nim/test-model"

Route = tuple[str, str, str]


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


WAV = _wav(0.5)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Upstream:
    """Scripted hosts: ``script`` first, then the fixed ``routes``.

    A route with several answers gives them in order and then repeats its
    last one. Every request is recorded, in order.
    """

    def __init__(
        self,
        routes: dict[Route, list[httpx.Response]] | None = None,
        script: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.script = script
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        if self.script is not None:
            answer = self.script(request)
            if answer is not None:
                return answer
        queue = self.routes.get((request.method, request.url.host, request.url.path))
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    def calls(self) -> list[Route]:
        return [(r.method, r.url.host, r.url.path) for r in self.seen]


def _images(*items: dict[str, Any], **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={"created": 1, "data": list(items), **extra})


def _job(job_id: str, status: str, code: int = 200, **extra: Any) -> httpx.Response:
    return httpx.Response(code, json={"id": job_id, "status": status, **extra})


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "XAI_API_KEY": XAI_KEY,
        "TOGETHER_API_KEY": TOGETHER_KEY,
        "GEMINI_API_KEY": GEMINI_KEY,
        "OPENROUTER_API_KEY": OR_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "xai/grok-2-image",
        "MODEL_TTS": "together/cartesia/sonic-2",
        "MODEL_VIDEO": "gemini/veo-3.1-generate-preview",
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


def _requests(tmp_path: Path) -> list[sqlite3.Row]:
    # Closing the stores drains the writer thread, so every row is on disk.
    request_log.reset_request_log_stores()
    conn = _db(tmp_path)
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def _jobs(tmp_path: Path) -> list[sqlite3.Row]:
    conn = _db(tmp_path)
    try:
        return list(conn.execute("SELECT * FROM media_jobs ORDER BY created_at"))
    finally:
        conn.close()


def _form_parts(request: httpx.Request) -> list[tuple[str, str | None, bytes]]:
    """(name, filename, payload) for every multipart part the host received."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.content)
    parts = []
    for part in message.iter_parts():
        params = dict(part.get_params(header="content-disposition") or [])
        payload = part.get_payload(decode=True)
        parts.append(
            (
                str(params.get("name")),
                params.get("filename"),
                payload if isinstance(payload, bytes) else b"",
            )
        )
    return parts


def _fields(request: httpx.Request) -> dict[str, bytes]:
    return {name: data for name, filename, data in _form_parts(request) if not filename}


def _image_request(**config: Any) -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": "a red kite over hills"}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], **config},
    }


def _inline(response: httpx.Response) -> list[dict[str, Any]]:
    body = response.json()
    (candidate,) = body["candidates"]
    assert candidate["finishReason"] == "STOP"
    assert candidate["content"]["role"] == "model"
    return [part["inlineData"] for part in candidate["content"]["parts"]]


# ------------------------------------------------------------- pictures


def test_response_modalities_image_routes_to_image_rail(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            ("POST", XAI, IMAGES_GENERATIONS): [
                _images(
                    {"b64_json": _b64(PNG_OUT)},
                    {"b64_json": _b64(JPEG_OUT)},
                    usage={"input_tokens": 12, "output_tokens": 1500},
                )
            ]
        }
    )
    payload = _image_request(
        candidateCount=2,
        temperature=0.4,
        imageConfig={"aspectRatio": "16:9", "imageSize": "2K"},
    )
    payload["systemInstruction"] = {"parts": [{"text": "flat vector style"}]}
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=payload)
    assert response.status_code == 200, response.text
    assert upstream.calls() == [("POST", XAI, IMAGES_GENERATIONS)]
    sent = json.loads(upstream.seen[0].content)
    assert sent == {
        "prompt": "flat vector style\na red kite over hills",
        "n": 2,
        "response_format": "b64_json",
        "model": "grok-2-image",
    }
    assert _inline(response) == [
        {"mimeType": "image/png", "data": _b64(PNG_OUT)},
        {"mimeType": "image/jpeg", "data": _b64(JPEG_OUT)},
    ]
    body = response.json()
    assert body["modelVersion"] == "mcc-image"
    assert body["usageMetadata"] == {
        "promptTokenCount": 12,
        "candidatesTokenCount": 1500,
        "totalTokenCount": 1512,
    }


def test_inline_image_becomes_an_edit_upload(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", XAI, IMAGES_EDITS): [_images({"b64_json": _b64(PNG_OUT)})]}
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": _b64(PNG_IN)}},
                    {"text": "make the sky purple"},
                ],
            }
        ],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=payload)
    assert response.status_code == 200, response.text
    assert upstream.calls() == [("POST", XAI, IMAGES_EDITS)]
    edit = upstream.seen[0]
    assert edit.headers["content-type"].startswith("multipart/form-data")
    parts = _form_parts(edit)
    files = [(name, data) for name, filename, data in parts if filename]
    assert files == [("image[]", PNG_IN)]
    assert _fields(edit) == {
        "model": b"grok-2-image",
        "prompt": b"make the sky purple",
        "response_format": b"b64_json",
    }
    assert _inline(response) == [{"mimeType": "image/png", "data": _b64(PNG_OUT)}]
    (row,) = _requests(tmp_path)
    assert row["media_operation"] == "image_edit"
    assert row["input_image_count"] == 1


def test_url_only_image_is_downloaded_and_inlined(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    url = f"https://{CDN}/out/kite.png"

    def script(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == CDN:
            return httpx.Response(
                200, content=PNG_OUT, headers={"content-type": "image/png"}
            )
        return None

    upstream = Upstream(
        {("POST", XAI, IMAGES_GENERATIONS): [_images({"url": url})]}, script=script
    )
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=_image_request())
    assert response.status_code == 200, response.text
    assert upstream.calls() == [
        ("POST", XAI, IMAGES_GENERATIONS),
        ("GET", CDN, "/out/kite.png"),
    ]
    # A key goes only to the provider's own host.
    assert "authorization" not in upstream.seen[1].headers
    assert _inline(response) == [{"mimeType": "image/png", "data": _b64(PNG_OUT)}]
    assert url not in response.text
    (row,) = _requests(tmp_path)
    assert row["output_image_count"] == 1
    assert row["media_bytes_out"] == len(PNG_OUT)


def test_undownloadable_image_url_is_an_error_to_the_client(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)

    def script(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == CDN:
            return httpx.Response(404, json={"error": {"message": "gone"}})
        return None

    upstream = Upstream(
        {
            ("POST", XAI, IMAGES_GENERATIONS): [
                _images({"url": f"https://{CDN}/out/kite.png"})
            ]
        },
        script=script,
    )
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=_image_request())
    # The fetch's own failure, classified like any upstream answer: the
    # host's status, never a success with the picture missing.
    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == 404
    assert error["status"] == "INTERNAL"
    (row,) = _requests(tmp_path)
    assert row["status"] == "error"


def test_empty_image_rail_404_names_model_image(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="")
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=_image_request())
    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["status"] == "NOT_FOUND"
    assert error["code"] == 404
    assert "MODEL_IMAGE" in error["message"]
    assert upstream.seen == []


def test_upstream_failure_is_googles_envelope_with_the_classified_status(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            ("POST", XAI, IMAGES_GENERATIONS): [
                httpx.Response(429, json={"error": {"message": "slow down"}})
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=_image_request())
    assert response.status_code == 429, response.text
    error = response.json()["error"]
    assert error["status"] == "RESOURCE_EXHAUSTED"
    assert error["code"] == 429


def test_both_modalities_is_400_gemini_envelope(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    payload = _image_request()
    payload["generationConfig"]["responseModalities"] = ["image", "audio"]
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=payload)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    assert "IMAGE" in error["message"] and "AUDIO" in error["message"]
    assert upstream.seen == []


def test_stream_generate_content_media_is_one_sse_event(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", XAI, IMAGES_GENERATIONS): [_images({"b64_json": _b64(PNG_OUT)})]}
    )
    stream_path = IMAGE_PATH.replace(":generateContent", ":streamGenerateContent")
    with _client(settings, upstream) as client:
        streamed = client.post(stream_path + "?alt=sse", json=_image_request())
        whole = client.post(IMAGE_PATH, json=_image_request())
    assert streamed.status_code == 200, streamed.text
    assert streamed.headers["content-type"].startswith("text/event-stream")
    text = streamed.text
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    assert text.count("data: ") == 1
    event = json.loads(text[len("data: ") :])
    assert event == whole.json()
    assert event["candidates"][0]["content"]["parts"] == [
        {"inlineData": {"mimeType": "image/png", "data": _b64(PNG_OUT)}}
    ]
    # The upstream call itself is never a stream.
    assert "stream" not in json.loads(upstream.seen[0].content)


# ---------------------------------------------------------------- speech


def test_audio_output_routes_to_tts_rail(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            ("POST", TOGETHER, SPEECH): [
                httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})
            ]
        }
    )
    payload = {
        "contents": [{"parts": [{"text": "Say cheerfully: have a wonderful day!"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}},
                "languageCode": "en-US",
            },
        },
    }
    with _client(settings, upstream) as client:
        response = client.post(TTS_PATH, json=payload)
    assert response.status_code == 200, response.text
    assert upstream.calls() == [("POST", TOGETHER, SPEECH)]
    sent = json.loads(upstream.seen[0].content)
    assert sent == {
        "input": "Say cheerfully: have a wonderful day!",
        "voice": "Kore",
        "model": "cartesia/sonic-2",
    }
    assert _inline(response) == [{"mimeType": "audio/wav", "data": _b64(WAV)}]
    (row,) = _requests(tmp_path)
    assert row["media_operation"] == "speech"
    assert row["output_audio_seconds"] == pytest.approx(0.5)
    params = json.loads(row["params"])
    assert (
        "generationConfig.speechConfig.languageCode"
        in (params["media"]["not_forwarded"])
    )


# ------------------------------------------------------------------ chat


class _ChatProvider:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.preflight_stream = MagicMock()

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data, **kwargs):
        self.requests.append(request_data)
        for event, data in (
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 3, "output_tokens": 0}},
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "just words"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ):
            yield format_sse_event(event, data)


def test_text_only_stays_chat(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()

    def no_media(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a TEXT-only request reached the media path")

    monkeypatch.setattr(media_routes, "_executor", no_media)
    monkeypatch.setattr(gemini_routes, "serve_media_generate", no_media)
    provider = _ChatProvider()
    with (
        patch(
            "my_claude_code.api.gemini_routes.resolve_provider", return_value=provider
        ),
        _client(settings, upstream) as client,
    ):
        texts = []
        for config in ({"responseModalities": ["TEXT"]}, {"maxOutputTokens": 16}):
            response = client.post(
                f"/v1beta/models/{CHAT_MODEL}:generateContent",
                json={
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                    "generationConfig": config,
                },
            )
            assert response.status_code == 200, response.text
            texts.append(response.json()["candidates"][0]["content"]["parts"])
    assert texts == [[{"text": "just words"}], [{"text": "just words"}]]
    assert len(provider.requests) == 2
    assert upstream.seen == []


# ------------------------------------------------------------ video jobs


def test_predict_long_running_routes_to_video_rail_and_pins(
    monkeypatch, tmp_path
) -> None:
    """OpenRouter refuses before accepting, Gemini accepts: the job stays there."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_VIDEO="open_router/google/veo-3.1",
        MODEL_VIDEO_FALLBACKS="gemini/veo-3.1-generate-preview",
    )
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [
                httpx.Response(500, json={"error": {"message": "boom"}})
            ],
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-1", "processing")],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-1"): [_job("g-1", "processing")],
        }
    )
    payload = {
        "instances": [{"prompt": "a lighthouse at dusk"}],
        "parameters": {
            "durationSeconds": 8,
            "aspectRatio": "16:9",
            "negativePrompt": "rain",
            "personGeneration": "allow_all",
            "sampleCount": 1,
        },
    }
    with _client(settings, upstream) as client:
        created = client.post(VEO_PATH, json=payload)
        assert created.status_code == 200, created.text
        name = created.json()["name"]
        polled = client.get(f"/v1beta/{name}")
    assert set(created.json()) == {"name"}
    assert name.startswith("operations/video_")
    assert "g-1" not in created.text
    assert polled.status_code == 200, polled.text
    assert upstream.calls() == [
        ("POST", OPENROUTER, OR_VIDEOS),
        ("POST", GEMINI, GEMINI_VIDEOS),
        ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-1"),
    ]
    create = upstream.seen[1]
    assert create.headers["content-type"].startswith("multipart/form-data")
    assert _fields(create) == {
        "model": b"veo-3.1-generate-preview",
        "prompt": b"a lighthouse at dusk",
        "seconds": b"8",
        "aspect_ratio": b"16:9",
        "negative_prompt": b"rain",
    }
    # The poll went out on the key that created the job.
    assert upstream.seen[2].headers["authorization"] == f"Bearer {GEMINI_KEY}"
    (job,) = _jobs(tmp_path)
    assert (job["provider"], job["upstream_id"]) == ("gemini", "g-1")
    assert name == f"operations/{job['job_id']}"


def test_predict_long_running_image_is_the_input_reference(
    monkeypatch, tmp_path
) -> None:
    """The first frame goes up as ``input_reference``; a JSON-only host is skipped."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_VIDEO="open_router/google/veo-3.1",
        MODEL_VIDEO_FALLBACKS="gemini/veo-3.1-generate-preview",
    )
    upstream = Upstream({("POST", GEMINI, GEMINI_VIDEOS): [_job("g-2", "processing")]})
    payload = {
        "instances": [
            {
                "prompt": "the kite takes off",
                "image": {"bytesBase64Encoded": _b64(PNG_IN), "mimeType": "image/png"},
                "lastFrame": {"bytesBase64Encoded": _b64(PNG_IN)},
            }
        ]
    }
    with _client(settings, upstream) as client:
        created = client.post(VEO_PATH, json=payload)
    assert created.status_code == 200, created.text
    assert upstream.calls() == [("POST", GEMINI, GEMINI_VIDEOS)]
    files = [
        (name, filename, data)
        for name, filename, data in _form_parts(upstream.seen[0])
        if filename
    ]
    assert files == [("input_reference", "input_reference-0.png", PNG_IN)]
    (row,) = _requests(tmp_path)
    assert row["media_operation"] == "video_create"
    params = json.loads(row["params"])
    assert params["media"]["not_forwarded"] == ["instances[0].lastFrame"]


def test_operations_poll_gemini_shape(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    url = f"https://{GEMINI}/v1beta/files/abc123:download?alt=media"

    def script(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/v1beta/files/abc123:download":
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        return None

    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [
                _job("g-ok", "processing"),
                _job("g-bad", "processing"),
            ],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-ok"): [
                _job("g-ok", "processing", progress=40),
                _job("g-ok", "completed", url=url, duration_seconds=8),
            ],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-bad"): [
                _job("g-bad", "failed", error={"message": "blocked by safety"})
            ],
        },
        script=script,
    )
    veo = {"instances": [{"prompt": "a lighthouse"}]}
    with _client(settings, upstream) as client:
        ok = client.post(VEO_PATH, json=veo).json()["name"]
        bad = client.post(VEO_PATH, json=veo).json()["name"]
        running = client.get(f"/v1beta/{ok}")
        done = client.get(f"/v1beta/{ok}")
        failed = client.get(f"/v1beta/{bad}")
        uri = done.json()["response"]["generateVideoResponse"]["generatedSamples"][0][
            "video"
        ]["uri"]
        downloaded = client.get(uri)
        unknown = client.get("/v1beta/operations/video_" + "0" * 32)
    job_id = ok.removeprefix("operations/")
    assert running.json() == {"name": ok, "done": False, "metadata": {"progress": 40}}
    assert done.json() == {
        "name": ok,
        "done": True,
        "response": {
            "@type": (
                "type.googleapis.com/google.ai.generativelanguage.v1beta."
                "PredictLongRunningResponse"
            ),
            "generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]},
        },
    }
    assert uri == (
        "http://testserver/v1beta/files/"
        + job_id.replace("_", "")
        + ":download?alt=media"
    )
    assert url not in done.text
    assert failed.json() == {
        "name": bad,
        "done": True,
        "error": {"code": 13, "message": "blocked by safety"},
    }
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == MP4
    assert downloaded.headers["content-type"].startswith("video/mp4")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["status"] == "NOT_FOUND"


def test_the_sdks_http_form_of_the_download_uri_is_served(
    monkeypatch, tmp_path
) -> None:
    """Over ``http://`` google-genai requests ``files/<the whole uri>:download``."""
    settings = _settings(monkeypatch, tmp_path)
    url = f"https://{GEMINI}/v1beta/files/abc123:download?alt=media"

    def script(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/v1beta/files/abc123:download":
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        return None

    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-h", "processing")],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-h"): [
                _job("g-h", "completed", url=url)
            ],
        },
        script=script,
    )
    with _client(settings, upstream) as client:
        name = client.post(
            VEO_PATH, json={"instances": [{"prompt": "a lighthouse"}]}
        ).json()["name"]
        file_id = name.removeprefix("operations/").replace("_", "")
        nested = (
            f"/v1beta/files/http://testserver/v1beta/files/{file_id}:download"
            "?alt=media:download?alt=media"
        )
        downloaded = client.get(nested)
        not_a_download = client.get(f"/v1beta/files/{file_id}")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == MP4
    assert not_a_download.status_code == 404
    assert not_a_download.json()["error"]["status"] == "NOT_FOUND"


def test_predict_is_still_unsupported(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1beta/models/imagen-4:predict",
            json={"instances": [{"prompt": "p"}]},
        )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["status"] == "NOT_FOUND"
    assert "Unsupported method: predict." in error["message"]
    assert "predictLongRunning" in error["message"]
    assert upstream.seen == []


def test_rows_are_logged_with_protocol_gemini(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            ("POST", XAI, IMAGES_GENERATIONS): [_images({"b64_json": _b64(PNG_OUT)})],
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-log", "processing")],
        }
    )
    with _client(settings, upstream) as client:
        image = client.post(
            IMAGE_PATH,
            json=_image_request(temperature=0.4, imageConfig={"aspectRatio": "16:9"}),
        )
        video = client.post(
            VEO_PATH,
            json={
                "instances": [{"prompt": "a lighthouse"}],
                "parameters": {"personGeneration": "allow_all"},
            },
        )
    assert image.status_code == 200, image.text
    assert video.status_code == 200, video.text
    first, second = _requests(tmp_path)
    assert (first["protocol"], first["endpoint"]) == ("gemini", IMAGE_PATH)
    assert first["media_operation"] == "image_generate"
    assert first["provider"] == "xai"
    assert first["input_chars"] == len("a red kite over hills")
    params = json.loads(first["params"])
    assert "n" not in params
    assert params["response_format"] == "b64_json"
    assert params["media"]["not_forwarded"] == [
        "generationConfig.temperature",
        "generationConfig.imageConfig.aspectRatio",
    ]
    assert (second["protocol"], second["endpoint"]) == ("gemini", VEO_PATH)
    assert second["media_operation"] == "video_create"
    assert json.loads(second["params"])["media"]["not_forwarded"] == [
        "parameters.personGeneration"
    ]


def test_operation_and_file_paths_are_gemini_shaped() -> None:
    """An error escaping a job path is answered in Google's envelope."""

    assert wire_api_for_path("/v1beta/operations/video_abc") == "gemini"
    assert wire_api_for_path("/v1beta/files/videoabc:download") == "gemini"


def _off_loop(name: str, calls: list[str], real: Callable[..., Any]):
    def sentinel(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)
            return real(*args, **kwargs)
        raise AssertionError(f"{name} ran on the event loop thread")

    return sentinel


def test_gemini_media_base64_and_json_run_off_the_loop(monkeypatch, tmp_path) -> None:
    """Decoding the inline image, reading the answer and encoding the reply."""
    calls: list[str] = []
    for name in ("_spool", "openai_image_parts", "encode_media_answer"):
        monkeypatch.setattr(
            gemini_media_routes,
            name,
            _off_loop(name, calls, getattr(gemini_media_routes, name)),
        )
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", XAI, IMAGES_EDITS): [_images({"b64_json": _b64(PNG_OUT)})]}
    )
    payload = _image_request()
    payload["contents"][0]["parts"].append(
        {"inlineData": {"mimeType": "image/png", "data": _b64(PNG_IN)}}
    )
    with _client(settings, upstream) as client:
        response = client.post(IMAGE_PATH, json=payload)
    assert response.status_code == 200, response.text
    assert calls == ["_spool", "openai_image_parts", "encode_media_answer"]


# ------------------------------------------------------ the real SDK wire


@pytest.mark.parametrize("base", ["https://mcc", "http://mcc"])
def test_genai_sdk_generate_videos_round_trip(monkeypatch, tmp_path, base) -> None:
    """``google-genai`` against the app in-process: generate, poll, download.

    Skipped where the SDK is not installed (it is not a dependency of this
    repository). Over https the SDK names the file by its id; over http it
    nests the whole uri in the path -- both reach the same video.
    """
    genai = pytest.importorskip("google.genai")
    genai_types = pytest.importorskip("google.genai.types")
    settings = _settings(monkeypatch, tmp_path)
    url = f"https://{GEMINI}/v1beta/files/abc123:download?alt=media"

    def script(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/v1beta/files/abc123:download":
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        return None

    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-sdk", "processing")],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-sdk"): [
                _job("g-sdk", "processing"),
                _job("g-sdk", "completed", url=url, duration_seconds=8),
            ],
        },
        script=script,
    )
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    app = create_test_app(settings, media=registry)

    async def run() -> tuple[Any, bytes]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base) as http:
            client = genai.Client(
                api_key="unused",
                http_options=genai_types.HttpOptions(
                    base_url=base, httpx_async_client=http
                ),
            )
            try:
                operation = await client.aio.models.generate_videos(
                    model="veo-3.1-generate-preview",
                    prompt="a lighthouse at dusk",
                    config=genai_types.GenerateVideosConfig(
                        duration_seconds=8, aspect_ratio="16:9"
                    ),
                )
                for _poll in range(5):
                    if operation.done:
                        break
                    operation = await client.aio.operations.get(operation)
                video = operation.response.generated_videos[0].video
                data = await client.aio.files.download(file=video)
                return operation, data
            finally:
                await asyncio.wait_for(provider_manager_for_app(app).close(), 20)
                await registry.close()

    operation, data = asyncio.run(run())
    assert operation.done is True
    assert operation.name.startswith("operations/video_")
    assert data == MP4
    create = upstream.seen[0]
    assert _fields(create) == {
        "model": b"veo-3.1-generate-preview",
        "prompt": b"a lighthouse at dusk",
        "seconds": b"8",
        "aspect_ratio": b"16:9",
    }
