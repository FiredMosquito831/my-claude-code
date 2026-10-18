"""``mcc-mcp``'s calls, end to end through a real MCC app and a fake provider.

The media server's own code (``mcp_media.server``) drives the real FastAPI
app -- routing, the media executor, the request log -- whose provider is an
``httpx.MockTransport``: nothing reaches a real host. What is proven is what a
user would see: the file on disk, the picture handed back, the request row
naming the app, and that a picture a provider answered with only a link is
fetched by MCC through the provider's client and never by ``mcc-mcp``
(risk R8).
"""

import base64
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from anyio.to_thread import run_sync
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.mcp_media.client import MccAddress, MccClient
from my_claude_code.mcp_media.server import ServerState, call_tool, load_catalogue
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app

XAI = "api.x.ai"
CDN = "cdn.example.test"
OPENROUTER = "openrouter.ai"
OR_VIDEOS = "/api/v1/videos"
XAI_KEY = "xai-" + "a" * 40
OR_KEY = "sk-or-v1-" + "a" * 48
PNG = b"\x89PNG\r\n\x1a\n" + b"\x03" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x04" * 64
CDN_URL = f"https://{CDN}/out/kite.png"


class _ViaTestClient(httpx.AsyncBaseTransport):
    """Carry ``mcc-mcp``'s loopback requests into the in-process app."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        target = request.url.raw_path.decode("ascii")
        self.paths.append(request.url.path)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length"}
        }

        def send() -> httpx.Response:
            return self.client.request(
                request.method, target, content=body, headers=headers
            )

        answer = await run_sync(send)
        return httpx.Response(
            answer.status_code, headers=answer.headers, content=answer.content
        )


class Upstream:
    def __init__(self, *, url_only: bool = False) -> None:
        self.url_only = url_only
        self.seen: list[httpx.Request] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        host, path = request.url.host, request.url.path
        if host == XAI and path == "/v1/images/generations":
            self.entered.set()
            if self.gate is not None:
                self.gate.wait(30)
            item = (
                {"url": CDN_URL}
                if self.url_only
                else {"b64_json": base64.b64encode(PNG).decode()}
            )
            return httpx.Response(200, json={"created": 1, "data": [item]})
        if host == CDN:
            return httpx.Response(
                200, content=PNG, headers={"content-type": "image/png"}
            )
        if host == OPENROUTER and request.method == "POST" and path == OR_VIDEOS:
            return httpx.Response(202, json={"id": "or-job-1", "status": "pending"})
        if host == OPENROUTER and path == f"{OR_VIDEOS}/or-job-1":
            return httpx.Response(200, json={"id": "or-job-1", "status": "completed"})
        if host == OPENROUTER and path == f"{OR_VIDEOS}/or-job-1/content":
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        return httpx.Response(404, json={"error": {"message": "no such route"}})


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "XAI_API_KEY": XAI_KEY,
        "OPENROUTER_API_KEY": OR_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "xai/grok-2-image",
        "MODEL_VIDEO": "open_router/google/veo-3.1",
    }
    base.update(values)
    return Settings.model_validate(base)


class Rig:
    """One MCC app, one fake provider, one ``mcc-mcp`` state talking to them."""

    def __init__(self, settings: Settings, upstream: Upstream, out: Path) -> None:
        registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
        self.client = TestClient(create_test_app(settings, media=registry))
        self.upstream = upstream
        self.transport = _ViaTestClient(self.client)
        address = MccAddress(
            root="http://testserver", port_source="test", config_dir="", token_set=False
        )
        out.mkdir(parents=True, exist_ok=True)
        self.state = ServerState(
            app_id="opencode",
            address=address,
            client=MccClient(address, "", app_id="opencode", transport=self.transport),
            cwd=out,
            out_dir=out,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        async def run() -> Any:
            await load_catalogue(self.state)
            return await call_tool(self.state, name, arguments)

        return anyio.run(run)


@pytest.fixture
def rig(monkeypatch, tmp_path: Path) -> Iterator[Rig]:
    the_rig = Rig(_settings(monkeypatch, tmp_path), Upstream(), tmp_path / "work")
    with the_rig.client:
        yield the_rig


def _text(result: Any) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def _rows(tmp_path: Path) -> list[sqlite3.Row]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def test_generate_image_saves_file_via_fake_provider(rig: Rig, tmp_path: Path) -> None:
    result = rig.call("generate_image", {"prompt": "a red kite"})

    assert result.is_error is False, _text(result)
    (saved,) = [Path(p) for p in _text(result).splitlines()[1:2]]
    assert saved.read_bytes() == PNG
    assert saved.parent == (tmp_path / "work").resolve()
    # Decision 6: the picture also goes back to the model.
    (picture,) = [b for b in result.content if getattr(b, "type", "") == "image"]
    assert base64.b64decode(picture.data) == PNG
    assert picture.mime_type == "image/png"
    # A second call never overwrites the first file.
    again = rig.call("generate_image", {"prompt": "a red kite"})
    (second,) = [Path(p) for p in _text(again).splitlines()[1:2]]
    assert second != saved and saved.read_bytes() == PNG


def test_request_row_names_the_app(rig: Rig, tmp_path: Path) -> None:
    rig.call("generate_image", {"prompt": "a red kite"})

    rows = [
        row for row in _rows(tmp_path) if row["endpoint"] == "/v1/images/generations"
    ]
    assert [row["harness"] for row in rows] == ["opencode"]
    assert rows[0]["status"] == "success"


@pytest.mark.parametrize("fallback_on_undownloadable", ["false", "true"])
def test_url_only_picture_is_fetched_by_mcc_not_by_mcc_mcp(
    monkeypatch, tmp_path: Path, fallback_on_undownloadable: str
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MEDIA_FALLBACK_ON_UNDOWNLOADABLE=fallback_on_undownloadable,
    )
    upstream = Upstream(url_only=True)
    the_rig = Rig(settings, upstream, tmp_path / "work")
    with the_rig.client:
        result = the_rig.call("generate_image", {"prompt": "a red kite"})
        # Without the header the door answers exactly as before: the link.
        plain = the_rig.client.post(
            "/v1/images/generations", json={"prompt": "a red kite"}
        )

    assert result.is_error is False, _text(result)
    (saved,) = [Path(p) for p in _text(result).splitlines()[1:2]]
    assert saved.read_bytes() == PNG
    # MCC fetched the picture through the provider's own client (the mock
    # transport the provider leaf holds), without the key on a CDN host ...
    # One download, for mcc-mcp's call; the plain call fetches nothing.
    (cdn,) = [r for r in upstream.seen if r.url.host == CDN]
    assert "authorization" not in cdn.headers
    # ... and mcc-mcp itself only ever asked MCC.
    assert all(path.startswith("/v1/") for path in the_rig.transport.paths)
    assert plain.json()["data"] == [{"url": CDN_URL}]


def test_video_create_then_get_video_saves_content(rig: Rig, tmp_path: Path) -> None:
    started = rig.call("create_video", {"prompt": "a red kite", "mode": "std"})
    assert started.is_error is False, _text(started)
    (job_id,) = list(rig.state.log.video_jobs)
    assert job_id in _text(started)

    done = rig.call("get_video", {"video_id": job_id})

    assert done.is_error is False, _text(done)
    saved = Path(_text(done).removeprefix("Saved the video: ").strip())
    assert saved.read_bytes() == MP4
    assert saved.suffix == ".mp4"


def test_call_abandoned_by_the_app_still_saves(monkeypatch, tmp_path: Path) -> None:
    """Decision 7: an app that stops waiting gets its cancel; the file is saved."""

    upstream = Upstream()
    gate = threading.Event()
    upstream.gate = gate
    the_rig = Rig(_settings(monkeypatch, tmp_path), upstream, tmp_path / "work")
    state = the_rig.state

    async def run() -> str:
        await load_catalogue(state)
        async with anyio.create_task_group() as jobs:
            state.jobs = jobs
            async with anyio.create_task_group() as app:
                app.start_soon(call_tool, state, "generate_image", {"prompt": "kite"})
                await run_sync(upstream.entered.wait, 30)
                # The app gives up on the call (its own timeout, a cancel) ...
                app.cancel_scope.cancel()
            # ... and only then does the provider answer.
            gate.set()
        status = await call_tool(state, "media_status", {})
        return _text(status)

    with the_rig.client:
        status = anyio.run(run)

    (entry,) = state.log.snapshot()
    assert entry.after_app_stopped_waiting is True
    (saved,) = entry.paths
    assert Path(saved).read_bytes() == PNG
    assert "finished after the app stopped waiting" in status
    assert saved in status
