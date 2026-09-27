"""Media hashing, base64 and store writes never run on the event loop.

The Windows listener dies when the loop stalls (WORKING-NOTES V.14), and a
generated image is megabytes of base64. Every decode, hash and file write on
the media path goes through ``asyncio.to_thread``; this contract replaces each
with a sentinel that fails if it finds a running loop on its own thread.
"""

import asyncio
import base64

import httpx
from fastapi.testclient import TestClient

from my_claude_code.api import media_capture, media_routes
from my_claude_code.config.settings import Settings
from my_claude_code.core import openai_images
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _off_loop(name: str, calls: list[str], real):
    def sentinel(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)
            return real(*args, **kwargs)
        raise AssertionError(f"{name} ran on the event loop thread")

    return sentinel


def test_parsing_hashing_and_storing_run_off_the_loop(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(
        media_routes,
        "parse_images_response",
        _off_loop("parse", calls, openai_images.parse_images_response),
    )
    monkeypatch.setattr(
        media_capture,
        "write_media_file",
        _off_loop("store", calls, media_capture.write_media_file),
    )
    settings = Settings.model_validate(
        {
            "XAI_API_KEY": "xai-" + "a" * 40,
            "MODEL_IMAGE": "xai/grok-2-image",
            "MEDIA_STORE_ENABLED": "true",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        )

    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200
    assert calls == ["parse", "store"]
