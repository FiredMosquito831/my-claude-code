"""``POST /v1/images/edits``: multipart and JSON, streamed uploads, the rail.

The upstream is a MockTransport that parses what it received, so the test
asserts the multipart the host actually got -- every file, the mask, and the
text fields -- and that the files went out as a stream, not as one buffer.
"""

import base64
import json
import sqlite3
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.media import multipart as media_multipart
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 64
OUT = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64


def _parts(request: httpx.Request) -> list[tuple[str, str | None, str | None, bytes]]:
    """(name, filename, content type, payload) for every part the host received."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.content)
    parts = []
    for part in message.iter_parts():
        disposition = part.get("content-disposition", "")
        params = dict(part.get_params(header="content-disposition") or [])
        payload = part.get_payload(decode=True)
        parts.append(
            (
                str(params.get("name")),
                params.get("filename"),
                part.get_content_type() if "filename" in disposition else None,
                payload if isinstance(payload, bytes) else b"",
            )
        )
    return parts


class Upstream:
    def __init__(self, answers: dict[str, list[httpx.Response]] | None = None) -> None:
        self.answers = answers or {}
        self.seen: list[httpx.Request] = []
        self.streamed: list[bool] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.streamed.append(isinstance(request.stream, httpx.AsyncByteStream))
        self.seen.append(request)
        queue = self.answers.get(request.url.host)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": base64.b64encode(OUT).decode()}]},
        )


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "XAI_API_KEY": "xai-" + "a" * 40,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "xai/grok-2-image",
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


def _media_links(tmp_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        return conn.execute(
            "SELECT direction, idx, bytes, stored FROM request_media"
            " JOIN media_blobs USING (sha256) ORDER BY direction, idx"
        ).fetchall()
    finally:
        conn.close()


def test_multipart_multiple_images_streamed_not_buffered(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(media_multipart, "READ_CHUNK_BYTES", 16)
    chunks: list[int] = []
    original = media_multipart.MultipartBody.stream

    async def counting(self):
        async for chunk in original(self):
            chunks.append(len(chunk))
            yield chunk

    monkeypatch.setattr(media_multipart.MultipartBody, "stream", counting)
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/edits",
            data={
                "model": "gpt-image-2",
                "prompt": "make it blue",
                "size": "1024x1024",
            },
            files=[
                ("image[]", ("a.png", PNG, "image/png")),
                ("image[]", ("b.png", PNG[::-1], "image/png")),
            ],
        )
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()["data"][0]["b64_json"]) == OUT
    (request,) = upstream.seen
    assert request.url.path == "/v1/images/edits"
    assert upstream.streamed == [True]
    parts = _parts(request)
    names = [(name, filename) for name, filename, _type, _body in parts]
    assert ("model", None) in names
    assert names.count(("image[]", "a.png")) == 1
    assert names.count(("image[]", "b.png")) == 1
    bodies = {filename: body for _n, filename, _t, body in parts if filename}
    assert bodies == {"a.png": PNG, "b.png": PNG[::-1]}
    fields = {name: body.decode() for name, filename, _t, body in parts if not filename}
    assert fields["model"] == "grok-2-image"
    assert fields["prompt"] == "make it blue"
    assert fields["size"] == "1024x1024"
    assert int(request.headers["content-length"]) == len(request.content)
    # Each file went out in 16-byte reads, never as one buffer.
    assert chunks.count(16) >= 2 * (len(PNG) // 16)


def test_mask_forwarded(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/edits",
            data={"prompt": "remove the cube"},
            files=[
                ("image", ("scene.png", PNG, "image/png")),
                ("mask", ("mask.png", PNG[:20], "image/png")),
            ],
        )
    assert response.status_code == 200, response.text
    parts = _parts(upstream.seen[0])
    files = {name: (filename, body) for name, filename, _t, body in parts if filename}
    assert files["mask"] == ("mask.png", PNG[:20])
    assert files["image"] == ("scene.png", PNG)


def test_json_variant(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    body = {
        "model": "gpt-image-2",
        "prompt": "p",
        "images": [{"image_url": "https://example.test/a.png"}],
    }
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/edits", json=body)
    assert response.status_code == 200, response.text
    sent = json.loads(upstream.seen[0].content)
    assert sent == {
        "model": "grok-2-image",
        "prompt": "p",
        "images": [{"image_url": "https://example.test/a.png"}],
    }


def test_a_retried_upload_is_sent_whole_again(monkeypatch, tmp_path) -> None:
    """The fallback re-reads the spooled files from the start."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="xai/grok-2-image-b",
    )
    upstream = Upstream(
        {
            "api.x.ai": [
                httpx.Response(500, json={"error": {"message": "boom"}}),
                httpx.Response(200, json={"data": []}),
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/edits",
            data={"prompt": "p"},
            files=[("image", ("a.png", PNG, "image/png"))],
        )
    assert response.status_code == 200, response.text
    assert len(upstream.seen) == 2
    for request in upstream.seen:
        files = {name: body for name, filename, _t, body in _parts(request) if filename}
        assert files == {"image": PNG}


def test_a_provider_without_an_edit_surface_is_skipped_uncharged(
    monkeypatch, tmp_path
) -> None:
    """Together declares generation but not edits: it is not tried."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="together/flux",
        MODEL_IMAGE_FALLBACKS="xai/grok-2-image",
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/edits",
            data={"prompt": "p"},
            files=[("image", ("a.png", PNG, "image/png"))],
        )
    assert response.status_code == 200, response.text
    assert [request.url.host for request in upstream.seen] == ["api.x.ai"]


def test_inputs_are_recorded_as_metadata_only_by_default(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    with _client(settings, Upstream()) as client:
        response = client.post(
            "/v1/images/edits",
            data={"prompt": "p"},
            files=[("image", ("a.png", PNG, "image/png"))],
        )
    assert response.status_code == 200
    row = _row(tmp_path)
    assert row["endpoint"] == "/v1/images/edits"
    assert row["media_operation"] == "image_edit"
    assert row["input_image_count"] == 1
    assert row["output_image_count"] == 1
    assert _media_links(tmp_path) == [("in", 0, len(PNG), 0), ("out", 0, len(OUT), 0)]
    params = json.loads(row["params"]) if isinstance(row["params"], str) else None
    if params is not None:
        assert params["media"]["uploads"][0]["filename"] == "a.png"


@pytest.mark.parametrize(
    ("data", "files", "message"),
    [
        ({"size": "1024x1024"}, [("image", ("a.png", PNG, "image/png"))], "prompt"),
        ({"prompt": "p"}, [("mask", ("m.png", PNG, "image/png"))], "image"),
    ],
)
def test_missing_prompt_or_image_is_a_400(
    monkeypatch, tmp_path, data, files, message
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/edits", data=data, files=files)
    assert response.status_code == 400
    assert message in response.json()["error"]["message"]
    assert upstream.seen == []


def test_input_thumbnails_are_kept_when_media_storage_is_on(
    monkeypatch, tmp_path
) -> None:
    """User decision 7: inputs get metadata, plus a thumbnail when storage is on."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (200, 10, 10)).save(buffer, format="PNG")
    real_png = buffer.getvalue()
    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true")
    with _client(settings, Upstream()) as client:
        response = client.post(
            "/v1/images/edits",
            data={"prompt": "p"},
            files=[("image", ("a.png", real_png, "image/png"))],
        )
    assert response.status_code == 200
    row = _row(tmp_path)
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        thumbs = conn.execute(
            "SELECT i.width, i.height, i.thumbnail IS NOT NULL FROM request_images r"
            " JOIN image_blobs i ON i.sha = r.sha WHERE r.request_id = ?",
            (row["id"],),
        ).fetchall()
    finally:
        conn.close()
    assert thumbs == [(64, 32, 1)]
