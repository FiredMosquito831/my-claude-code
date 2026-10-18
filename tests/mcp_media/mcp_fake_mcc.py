"""A stand-in MCC for the ``mcc-mcp`` stdio tests (7.99.0).

A real HTTP server on ``127.0.0.1`` and a port the OS picks (never MCC's
own), answering the handful of routes ``mcc-mcp`` calls: ``/health``,
``/v1/media/models``, the image, speech and video doors. Every request is
recorded -- path, headers, body -- so a test can say exactly what the media
server sent, and with which token and app name. Nothing reaches a provider:
there is none.
"""

import base64
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PNG = b"\x89PNG\r\n\x1a\n" + b"\x07" * 48
MP3 = b"ID3\x04" + b"\x01" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x02" * 32
IMAGE_MODELS = ("fake_a/pic-one", "fake_b/pic-two")
VIDEO_MODEL = "fake_v/clip"
SPEECH_MODEL = "fake_s/voice"


def _field(**buckets: tuple[str, ...]) -> dict[str, list[str]]:
    names = ("accepted_by", "required_by", "refused_by", "not_forwarded_by")
    entry = {name: list(buckets.get(name, ())) for name in names}
    entry["not_known_for"] = list(buckets.get("not_known_for", ()))
    return entry


def catalogue() -> dict[str, Any]:
    """A ``GET /v1/media/models`` body: two image models, speech, video."""

    def schema(ref: str) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "model": {"type": "string", "const": ref},
                "prompt": {"type": "string"},
                "input": {"type": "string"},
                "size": {"type": "string"},
                "voice": {"type": ["string", "object"]},
                "mode": {"type": "string", "enum": ["std", "pro"]},
            },
        }

    def entry(ref: str, operation: str, endpoint: str) -> dict[str, Any]:
        return {
            "id": ref,
            "object": "media.model",
            "operations": {
                operation: {"endpoint": endpoint, "request_schema": schema(ref)}
            },
        }

    return {
        "object": "list",
        "data": [
            *(
                entry(ref, "image_generate", "/v1/images/generations")
                for ref in IMAGE_MODELS
            ),
            entry(SPEECH_MODEL, "speech", "/v1/audio/speech"),
            entry(VIDEO_MODEL, "video_create", "/v1/videos"),
        ],
        "rails": [
            {
                "rail": "image",
                "label": "Image",
                "models": list(IMAGE_MODELS),
                "operations": {
                    "image_generate": {
                        "endpoint": "/v1/images/generations",
                        "models": list(IMAGE_MODELS),
                        "not_served_by": [],
                        "fields": {
                            "prompt": _field(
                                accepted_by=IMAGE_MODELS, required_by=IMAGE_MODELS
                            ),
                            "size": _field(not_known_for=IMAGE_MODELS),
                        },
                    }
                },
            },
            {
                "rail": "tts",
                "label": "Speech",
                "models": [SPEECH_MODEL],
                "operations": {
                    "speech": {
                        "endpoint": "/v1/audio/speech",
                        "models": [SPEECH_MODEL],
                        "not_served_by": [],
                        "fields": {
                            "input": _field(
                                accepted_by=(SPEECH_MODEL,), required_by=(SPEECH_MODEL,)
                            ),
                            "voice": _field(not_known_for=(SPEECH_MODEL,)),
                        },
                    }
                },
            },
            {
                "rail": "asr",
                "label": "Transcription",
                "models": [],
                "operations": {
                    "transcribe": {
                        "endpoint": "/v1/audio/transcriptions",
                        "models": [],
                        "not_served_by": [],
                        "fields": {},
                    }
                },
            },
            {
                "rail": "video",
                "label": "Video",
                "models": [VIDEO_MODEL],
                "operations": {
                    "video_create": {
                        "endpoint": "/v1/videos",
                        "models": [VIDEO_MODEL],
                        "not_served_by": [],
                        "fields": {
                            "prompt": _field(
                                accepted_by=(VIDEO_MODEL,), required_by=(VIDEO_MODEL,)
                            ),
                            "mode": _field(
                                accepted_by=(VIDEO_MODEL,), required_by=(VIDEO_MODEL,)
                            ),
                        },
                    }
                },
            },
        ],
    }


@dataclass
class Seen:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes


@dataclass
class FakeMcc:
    """One stand-in MCC; ``start()`` then ``port``; ``stop()`` when done."""

    token: str = "test-token"
    seen: list[Seen] = field(default_factory=list)
    #: Held while set: the image door waits on it (an abandoned-call test).
    image_gate: threading.Event | None = None
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def start(self) -> FakeMcc:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _answer(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: object) -> None:
                self._answer(status, json.dumps(payload).encode(), "application/json")

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                owner.seen.append(Seen(method, self.path, headers, body))
                if self.path == "/health":
                    return self._json(200, {"status": "healthy"})
                if (
                    owner.token
                    and headers.get("authorization") != f"Bearer {owner.token}"
                ):
                    return self._json(401, {"detail": "Invalid API key"})
                if method == "GET" and self.path == "/v1/media/models":
                    return self._json(200, catalogue())
                if method == "POST" and self.path == "/v1/images/generations":
                    if owner.image_gate is not None:
                        owner.image_gate.wait(30)
                    return self._json(
                        200,
                        {
                            "created": 1,
                            "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                        },
                    )
                if method == "POST" and self.path == "/v1/audio/speech":
                    return self._answer(200, MP3, "audio/mpeg")
                if method == "POST" and self.path == "/v1/videos":
                    return self._json(200, {"id": "video_1", "status": "queued"})
                if method == "GET" and self.path == "/v1/videos/video_1":
                    return self._json(200, {"id": "video_1", "status": "completed"})
                if method == "GET" and self.path == "/v1/videos/video_1/content":
                    return self._answer(200, MP4, "video/mp4")
                return self._json(404, {"error": {"message": "no such route"}})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)

    def calls(self, path: str) -> list[Seen]:
        return [entry for entry in self.seen if entry.path == path]
