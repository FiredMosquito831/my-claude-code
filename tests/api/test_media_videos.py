"""``/v1/videos`` on the Video rail: a job accepted once, then read where it lives.

Fake upstreams only (``httpx.MockTransport``): no test here reaches a real host.
Hosts and shapes are the declared ones -- Gemini's OpenAI layer (multipart
create, ``processing``, the file at a ``url``), OpenRouter (JSON-only create,
``pending``, ``duration``, ``videos/{id}/content`` with the key).
"""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

from my_claude_code.api.wire_surfaces import wire_api_for_path
from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.media.key_pool import MediaKeyPool
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app, provider_manager_for_app

GEMINI = "generativelanguage.googleapis.com"
OPENROUTER = "openrouter.ai"
GEMINI_VIDEOS = "/v1beta/openai/videos"
OR_VIDEOS = "/api/v1/videos"
DEEPINFRA = "api.deepinfra.com"
DI_VIDEOS = "/v1/openai/videos"
DI_KEY = "di-" + "d" * 40
GEMINI_KEY = "AIza" + "g" * 35
OR_KEY = "sk-or-v1-" + "a" * 48
OR_KEY_2 = "sk-or-v1-" + "b" * 48
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4

Route = tuple[str, str, str]


class Upstream:
    """Scripted hosts. ``script`` is asked first, then the fixed ``routes``.

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


def _job(job_id: str, status: str, code: int = 200, **extra: Any) -> httpx.Response:
    return httpx.Response(code, json={"id": job_id, "status": status, **extra})


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "GEMINI_API_KEY": GEMINI_KEY,
        "OPENROUTER_API_KEY": OR_KEY,
        "DEEPINFRA_API_KEY": DI_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_VIDEO": "open_router/google/veo-3.1",
    }
    base.update(values)
    return Settings.model_validate(base)


def _client(settings: Settings, upstream: Upstream) -> tuple[TestClient, MediaRegistry]:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry)), registry


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


def _attempts(tmp_path: Path, request_id: str) -> list[sqlite3.Row]:
    conn = _db(tmp_path)
    try:
        return list(
            conn.execute(
                "SELECT * FROM request_attempts WHERE request_id = ? ORDER BY attempt",
                (request_id,),
            )
        )
    finally:
        conn.close()


def _form(fields: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    """A multipart body with text fields only -- what the OpenAI SDK sends."""
    boundary = "mcc-video-test"
    body = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
        for name, value in fields.items()
    )
    body += f"--{boundary}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


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


# ---------------------------------------------------------------- create


def test_submit_falls_back_before_acceptance(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_VIDEO="gemini/veo-3.1-generate-preview",
        MODEL_VIDEO_FALLBACKS="open_router/google/veo-3.1",
    )
    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [
                httpx.Response(500, json={"error": {"message": "boom"}})
            ],
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-1", "pending", 202)],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        response = client.post(
            "/v1/videos", json={"prompt": "a cat", "model": "sora-2", "seconds": "8"}
        )
    assert response.status_code == 200, response.text
    video = response.json()
    assert video["object"] == "video"
    assert video["id"].startswith("video_")
    assert video["status"] == "queued"
    assert video["model"] == "sora-2"
    assert video["prompt"] == "a cat"
    assert "or-job-1" not in response.text
    assert upstream.calls() == [
        ("POST", GEMINI, GEMINI_VIDEOS),
        ("POST", OPENROUTER, OR_VIDEOS),
    ]
    (job,) = _jobs(tmp_path)
    assert (job["provider"], job["model"], job["upstream_id"]) == (
        "open_router",
        "google/veo-3.1",
        "or-job-1",
    )
    (row,) = _requests(tmp_path)
    assert row["media_operation"] == "video_create"
    assert row["media_job_id"] == video["id"]
    assert row["provider"] == "open_router"
    assert row["output_image_count"] is None


def test_no_fallback_after_acceptance(monkeypatch, tmp_path) -> None:
    """Accepted by Gemini, then failed there: failed, never re-run elsewhere."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_VIDEO="gemini/veo-3.1-generate-preview",
        MODEL_VIDEO_FALLBACKS="open_router/google/veo-3.1",
    )
    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-1", "processing")],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-1"): [
                _job("g-1", "failed", error={"message": "blocked by safety"})
            ],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        polled = client.get(f"/v1/videos/{video['id']}")
    assert polled.status_code == 200, polled.text
    body = polled.json()
    assert body["status"] == "failed"
    assert body["error"] == {"code": "failed", "message": "blocked by safety"}
    assert all(host != OPENROUTER for _method, host, _path in upstream.calls())
    assert upstream.calls() == [
        ("POST", GEMINI, GEMINI_VIDEOS),
        ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-1"),
    ]


def test_answer_without_job_id_is_not_acceptance(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_VIDEO="gemini/veo-3.1-generate-preview",
        MODEL_VIDEO_FALLBACKS="open_router/google/veo-3.1",
    )
    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [
                httpx.Response(200, json={"status": "processing"})
            ],
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-2", "pending", 202)],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        response = client.post("/v1/videos", json={"prompt": "p"})
    assert response.status_code == 200, response.text
    assert [host for _m, host, _p in upstream.calls()] == [GEMINI, OPENROUTER]
    (job,) = _jobs(tmp_path)
    assert job["provider"] == "open_router"
    (row,) = _requests(tmp_path)
    first, second = _attempts(tmp_path, row["id"])
    assert first["outcome"] == "failed"
    assert "without a job id" in first["error_message"]
    assert second["outcome"] == "succeeded"


def test_sdk_multipart_reencoded_as_json_for_json_only_host(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-3", "pending", 202)]}
    )
    body, headers = _form(
        {"prompt": "a red cube", "model": "sora-2", "seconds": "8", "size": "1280x720"}
    )
    client, _registry = _client(settings, upstream)
    with client:
        response = client.post("/v1/videos", content=body, headers=headers)
    assert response.status_code == 200, response.text
    (request,) = upstream.seen
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "prompt": "a red cube",
        "duration": 8,
        "size": "1280x720",
        "model": "google/veo-3.1",
    }
    assert request.headers["authorization"] == f"Bearer {OR_KEY}"


def test_upload_skips_json_only_surface_uncharged(monkeypatch, tmp_path) -> None:
    """A file cannot go where only JSON is documented: skipped, never charged."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_VIDEO="open_router/google/veo-3.1",
        MODEL_VIDEO_FALLBACKS="gemini/veo-3.1-generate-preview",
    )
    upstream = Upstream({("POST", GEMINI, GEMINI_VIDEOS): [_job("g-2", "processing")]})
    client, _registry = _client(settings, upstream)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with client:
        response = client.post(
            "/v1/videos",
            data={"prompt": "animate this", "seconds": "4"},
            files=[("input_reference", ("ref.png", png, "image/png"))],
        )
    assert response.status_code == 200, response.text
    (request,) = upstream.seen
    assert request.url.host == GEMINI
    parts = _parts(request)
    assert parts["input_reference"] == ("ref.png", png)
    assert parts["model"][1] == b"veo-3.1-generate-preview"
    assert parts["seconds"][1] == b"4"
    (row,) = _requests(tmp_path)
    skipped, served = _attempts(tmp_path, row["id"])
    assert (skipped["outcome"], skipped["error_kind"]) == ("skipped", "unsupported")
    assert served["provider"] == "gemini"


def test_empty_rail_names_model_video(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_VIDEO="")
    client, _registry = _client(settings, Upstream())
    with client:
        response = client.post("/v1/videos", json={"prompt": "p"})
        missing = client.post("/v1/videos", json={"model": "sora-2"})
    assert response.status_code == 404
    assert "MODEL_VIDEO" in response.json()["error"]["message"]
    assert missing.status_code == 400


# ------------------------------------------------------------ pinned reads


@pytest.mark.parametrize(
    ("model", "created", "polled", "expected"),
    [
        (
            "gemini/veo-3.1-generate-preview",
            ("POST", GEMINI, GEMINI_VIDEOS),
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/job-n"),
            ("in_progress", "in_progress"),
        ),
        (
            "open_router/google/veo-3.1",
            ("POST", OPENROUTER, OR_VIDEOS),
            ("GET", OPENROUTER, f"{OR_VIDEOS}/job-n"),
            ("queued", "in_progress"),
        ),
    ],
)
def test_status_normalised_processing_to_in_progress(
    monkeypatch, tmp_path, model, created, polled, expected
) -> None:
    """Gemini's ``processing`` and OpenRouter's ``pending`` read as the SDK's words.

    The SDK's poll keeps waiting only on ``queued`` / ``in_progress``.
    """
    settings = _settings(monkeypatch, tmp_path, MODEL_VIDEO=model)
    first = "processing" if created[1] == GEMINI else "pending"
    later = "processing" if created[1] == GEMINI else "in_progress"
    upstream = Upstream(
        {created: [_job("job-n", first, 202)], polled: [_job("job-n", later)]}
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        polled_video = client.get(f"/v1/videos/{video['id']}").json()
    assert (video["status"], polled_video["status"]) == expected
    (job,) = _jobs(tmp_path)
    assert job["status_raw"] == later


def test_poll_uses_pinned_key(monkeypatch, tmp_path) -> None:
    """Key 0 refused the create, key 1 took it: the job is read with key 1 only."""
    monkeypatch.setenv("OPENROUTER_API_KEY_ROTATION", "failover")
    settings = _settings(
        monkeypatch, tmp_path, OPENROUTER_API_KEY=f"{OR_KEY},{OR_KEY_2}"
    )

    def script(request: httpx.Request) -> httpx.Response | None:
        auth = request.headers.get("authorization")
        if request.method == "POST":
            if auth == f"Bearer {OR_KEY}":
                return httpx.Response(401, json={"error": {"message": "invalid key"}})
            return _job("or-job-k", "pending", 202)
        return _job("or-job-k", "in_progress", progress=40)

    upstream = Upstream(script=script)
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        polled = client.get(f"/v1/videos/{video['id']}").json()
    assert polled["progress"] == 40
    auths = [(r.method, r.headers.get("authorization")) for r in upstream.seen]
    assert auths == [
        ("POST", f"Bearer {OR_KEY}"),
        ("POST", f"Bearer {OR_KEY_2}"),
        ("GET", f"Bearer {OR_KEY_2}"),
    ]
    (job,) = _jobs(tmp_path)
    assert job["key_index"] == 1
    assert job["key_fingerprint"] == credential_fingerprint(OR_KEY_2)


def test_poll_429_benches_key_like_chat_no_rotation(monkeypatch, tmp_path) -> None:
    """A 429 on the job's key is charged to that key's books; no other key is asked."""
    settings = _settings(
        monkeypatch, tmp_path, OPENROUTER_API_KEY=f"{OR_KEY},{OR_KEY_2}"
    )
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-r", "pending", 202)],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-r"): [
                httpx.Response(
                    429,
                    json={"error": {"message": "slow down"}},
                    headers={"retry-after": "30"},
                )
            ],
        }
    )
    client, registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        polled = client.get(f"/v1/videos/{video['id']}")
        assert polled.status_code == 429
        assert polled.json()["error"]["type"] == "rate_limit_error"
        pool = registry.node("open_router")
        assert isinstance(pool, MediaKeyPool)
        metrics = pool.key_health()
    assert [bench["model"] for bench in metrics[0]["model_benches"]] == [
        "google/veo-3.1"
    ]
    assert metrics[0]["rate_limits"] == 1
    assert metrics[1]["model_benches"] == []
    assert [r.headers.get("authorization") for r in upstream.seen] == [
        f"Bearer {OR_KEY}",
        f"Bearer {OR_KEY}",
    ]


def test_removed_key_409_names_provider(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-x", "pending", 202)]}
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
    rotated = _settings(monkeypatch, tmp_path, OPENROUTER_API_KEY=OR_KEY_2)
    client, _registry = _client(rotated, upstream)
    with client:
        response = client.get(f"/v1/videos/{video['id']}")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "open_router" in error["message"]
    assert video["id"] in error["message"]
    assert upstream.calls() == [("POST", OPENROUTER, OR_VIDEOS)]


def test_completed_poll_writes_output_video_seconds(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-s", "pending", 202)],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-s"): [
                _job("or-job-s", "completed", duration=8, usage={"cost": 0.2})
            ],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        # The create row is written by the batched writer: flush it first.
        request_log.reset_request_log_stores()
        polled = client.get(f"/v1/videos/{video['id']}").json()
    assert polled["status"] == "completed"
    assert polled["progress"] == 100
    assert polled["seconds"] == "8"
    (row,) = _requests(tmp_path)
    assert row["output_video_seconds"] == 8.0
    assert row["media_job_id"] == video["id"]
    (job,) = _jobs(tmp_path)
    assert job["row_seconds_written"] == 1
    assert json.loads(job["usage_json"]) == {"cost": 0.2}


def test_job_row_has_no_secret_or_url(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    url = "https://cdn.openrouter.example/signed/abc.mp4?sig=secret"
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [
                _job(
                    "or-job-u",
                    "pending",
                    202,
                    polling_url="https://openrouter.ai/api/v1/videos/or-job-u",
                )
            ],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-u"): [
                _job("or-job-u", "completed", unsigned_urls=[url])
            ],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        created = client.post("/v1/videos", json={"prompt": "p"})
        polled = client.get(f"/v1/videos/{created.json()['id']}")
    for text in (created.text, polled.text):
        assert "or-job-u" not in text
        assert "https://" not in text
    (job,) = _jobs(tmp_path)
    values = [str(value) for value in dict(job).values()]
    for value in values:
        assert OR_KEY not in value
        assert "https://" not in value
        assert "http://" not in value


# ---------------------------------------------------------------- content


def test_content_streamed_from_declared_endpoint(monkeypatch, tmp_path) -> None:
    """OpenRouter serves the file at videos/{id}/content with the key; it is kept."""
    settings = _settings(monkeypatch, tmp_path, MEDIA_STORE_ENABLED="true")
    content = ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-c/content")
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-c", "pending", 202)],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-c"): [
                _job("or-job-c", "completed", duration=4)
            ],
            content: [
                httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
            ],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        client.get(f"/v1/videos/{video['id']}")
        downloaded = client.get(f"/v1/videos/{video['id']}/content")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == MP4
        assert downloaded.headers["content-type"].startswith("video/mp4")
        # Kept in the media store: the second read never reaches the host.
        again = client.get(f"/v1/videos/{video['id']}/content")
        assert again.content == MP4
    download = [r for r in upstream.seen if r.url.path.endswith("/content")]
    assert len(download) == 1
    assert download[0].headers["authorization"] == f"Bearer {OR_KEY}"
    sha = hashlib.sha256(MP4).hexdigest()
    (job,) = _jobs(tmp_path)
    assert (job["content_sha"], job["content_bytes"], job["content_mime"]) == (
        sha,
        len(MP4),
        "video/mp4",
    )
    assert (tmp_path / "media" / sha[:2] / f"{sha}.mp4").read_bytes() == MP4
    conn = _db(tmp_path)
    try:
        links = conn.execute(
            "SELECT direction, idx, sha256 FROM request_media WHERE request_id = ?",
            (job["request_id"],),
        ).fetchall()
        stored = conn.execute(
            "SELECT stored FROM media_blobs WHERE sha256 = ?", (sha,)
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(link) for link in links] == [("out", 0, sha)]
    assert [tuple(row) for row in stored] == [(1,)]


@pytest.mark.parametrize(
    ("url", "authorized"),
    [
        ("https://storage.googleapis.example/veo/clip.mp4", False),
        (f"https://{GEMINI}/v1beta/files/clip:download?alt=media", True),
    ],
)
def test_content_from_result_url_auth_only_same_host(
    monkeypatch, tmp_path, url, authorized
) -> None:
    """Gemini declares no content path: the retrieve answer's URL is fetched.

    The key goes only to Gemini's own host, never to storage elsewhere.
    """
    settings = _settings(
        monkeypatch, tmp_path, MODEL_VIDEO="gemini/veo-3.1-generate-preview"
    )
    download_host = httpx.URL(url).host

    def script(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == download_host and "clip" in request.url.path:
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        return None

    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-c", "processing")],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-c"): [
                _job("g-c", "completed", url=url)
            ],
        },
        script=script,
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        downloaded = client.get(f"/v1/videos/{video['id']}/content")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == MP4
    fetch = upstream.seen[-1]
    assert "clip" in fetch.url.path
    assert ("authorization" in fetch.headers) is authorized
    assert upstream.seen[-2].headers["authorization"] == f"Bearer {GEMINI_KEY}"


def test_expired_upstream_content(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {
            ("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-e", "pending", 202)],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-e"): [
                _job("or-job-e", "completed")
            ],
            ("GET", OPENROUTER, f"{OR_VIDEOS}/or-job-e/content"): [
                httpx.Response(404, json={"error": {"message": "gone"}})
            ],
        }
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        client.get(f"/v1/videos/{video['id']}")
        response = client.get(f"/v1/videos/{video['id']}/content")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "video_expired"
    assert "no longer holds this video" in error["message"]


def test_deepinfra_gets_json_and_its_declared_variant(monkeypatch, tmp_path) -> None:
    """DeepInfra: the SDK's form as JSON (``seconds`` an integer); ``variant`` kept.

    Only the query parameters its content surface declares are forwarded.
    """
    settings = _settings(
        monkeypatch, tmp_path, MODEL_VIDEO="deepinfra/Wan-AI/Wan2.1-T2V-14B"
    )
    thumbnail = b"\x89PNG\r\n\x1a\n" + b"\x01" * 16
    upstream = Upstream(
        {
            ("POST", DEEPINFRA, DI_VIDEOS): [_job("di-1", "queued")],
            ("GET", DEEPINFRA, f"{DI_VIDEOS}/di-1"): [_job("di-1", "succeeded")],
            ("GET", DEEPINFRA, f"{DI_VIDEOS}/di-1/content"): [
                httpx.Response(
                    200, content=thumbnail, headers={"content-type": "image/png"}
                )
            ],
        }
    )
    body, headers = _form({"prompt": "waves", "seconds": "4"})
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", content=body, headers=headers).json()
        polled = client.get(f"/v1/videos/{video['id']}").json()
        response = client.get(
            f"/v1/videos/{video['id']}/content?variant=thumbnail&index=2"
        )
    assert json.loads(upstream.seen[0].content) == {
        "prompt": "waves",
        "seconds": 4,
        "model": "Wan-AI/Wan2.1-T2V-14B",
    }
    assert polled["status"] == "completed"
    assert response.status_code == 200, response.text
    assert response.content == thumbnail
    fetch = upstream.seen[-1]
    assert dict(fetch.url.params) == {"variant": "thumbnail"}
    assert fetch.headers["authorization"] == f"Bearer {DI_KEY}"
    # A thumbnail is not the video: nothing is recorded as the job's content.
    (job,) = _jobs(tmp_path)
    assert job["content_sha"] is None


def test_a_thumbnail_is_refused_where_only_the_video_is_served(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-t", "pending", 202)]}
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        response = client.get(f"/v1/videos/{video['id']}/content?variant=thumbnail")
    assert response.status_code == 400
    assert "serves only the video" in response.json()["error"]["message"]
    assert len(upstream.seen) == 1


# ------------------------------------------------------- list / delete / 404


def test_list_returns_mcc_jobs(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    answers = iter(["or-job-1", "or-job-2", "or-job-3"])

    def script(request: httpx.Request) -> httpx.Response | None:
        return _job(next(answers), "pending", 202) if request.method == "POST" else None

    upstream = Upstream(script=script)
    client, _registry = _client(settings, upstream)
    with client:
        ids = [
            client.post("/v1/videos", json={"prompt": f"p{n}"}).json()["id"]
            for n in range(3)
        ]
        before = len(upstream.seen)
        listed = client.get("/v1/videos").json()
        page = client.get("/v1/videos?limit=2").json()
        rest = client.get(f"/v1/videos?limit=2&after={page['last_id']}").json()
        oldest_first = client.get("/v1/videos?order=asc").json()
    assert len(upstream.seen) == before
    assert listed["object"] == "list"
    assert [video["id"] for video in listed["data"]] == ids[::-1]
    assert listed["has_more"] is False
    assert (listed["first_id"], listed["last_id"]) == (ids[2], ids[0])
    assert [video["id"] for video in page["data"]] == [ids[2], ids[1]]
    assert page["has_more"] is True
    assert [video["id"] for video in rest["data"]] == [ids[0]]
    assert rest["has_more"] is False
    assert [video["id"] for video in oldest_first["data"]] == ids
    assert "or-job" not in json.dumps(listed)


def test_delete_forgets_job(monkeypatch, tmp_path) -> None:
    """No declared host documents a delete: MCC forgets its record, calls nobody."""
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream(
        {("POST", OPENROUTER, OR_VIDEOS): [_job("or-job-d", "pending", 202)]}
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        deleted = client.delete(f"/v1/videos/{video['id']}")
        after = client.get(f"/v1/videos/{video['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": video["id"],
        "object": "video.deleted",
        "deleted": True,
    }
    assert after.status_code == 404
    assert upstream.calls() == [("POST", OPENROUTER, OR_VIDEOS)]
    assert _jobs(tmp_path) == []


def test_unknown_id_404(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    client, _registry = _client(settings, Upstream())
    with client:
        responses = [
            client.get("/v1/videos/video_nope"),
            client.get("/v1/videos/video_nope/content"),
            client.delete("/v1/videos/video_nope"),
        ]
    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "not_found_error"


def test_videos_probe(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    client, _registry = _client(settings, Upstream())
    with client:
        response = client.options("/v1/videos")
    assert response.status_code == 204
    assert "POST" in response.headers["allow"]


# -------------------------------------------------------------- the real SDK


def test_openai_sdk_videos_round_trip(monkeypatch, tmp_path) -> None:
    """``AsyncOpenAI`` against the app in-process: create_and_poll, then download.

    Gemini answers ``processing`` once: only because MCC normalises it to
    ``in_progress`` does the SDK keep polling until ``completed``.
    """
    settings = _settings(
        monkeypatch, tmp_path, MODEL_VIDEO="gemini/veo-3.1-generate-preview"
    )
    url = f"https://{GEMINI}/v1beta/files/sdk-clip:download?alt=media"

    def script(request: httpx.Request) -> httpx.Response | None:
        if "sdk-clip" in request.url.path:
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

    async def run() -> tuple[Any, Any, bytes]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mcc"
        ) as http:
            sdk = AsyncOpenAI(
                api_key="unused", base_url="http://mcc/v1", http_client=http
            )
            try:
                video = await sdk.videos.create_and_poll(
                    prompt="a lighthouse at dusk",
                    model="sora-2",
                    seconds="8",
                    poll_interval_ms=1,
                )
                again = await sdk.videos.retrieve(video.id)
                content = await sdk.videos.download_content(video.id)
                return video, again, content.content
            finally:
                await asyncio.wait_for(provider_manager_for_app(app).close(), 20)
                await registry.close()

    video, again, data = asyncio.run(run())
    assert video.status == "completed"
    assert video.id.startswith("video_")
    assert video.seconds == "8"
    assert again.status == "completed"
    assert data == MP4
    create = upstream.seen[0]
    assert create.headers["content-type"].startswith("multipart/form-data")
    parts = _parts(create)
    assert parts["prompt"][1] == b"a lighthouse at dusk"
    assert parts["model"][1] == b"veo-3.1-generate-preview"
    assert parts["seconds"][1] == b"8"


def test_video_job_paths_are_openai_shaped() -> None:
    """An error escaping a job path is answered in the OpenAI envelope."""

    assert wire_api_for_path("/v1/videos") == "chat_completions"
    assert wire_api_for_path("/v1/videos/video_abc") == "chat_completions"
    assert wire_api_for_path("/v1/videos/video_abc/content") == "chat_completions"


def test_content_redirect_to_storage_drops_the_key(monkeypatch, tmp_path) -> None:
    """A same-host download that redirects to storage: followed, key not carried."""
    settings = _settings(
        monkeypatch, tmp_path, MODEL_VIDEO="gemini/veo-3.1-generate-preview"
    )
    own = "https://generativelanguage.googleapis.com/v1beta/files/v1:download"
    storage = "https://storage.example.test/bucket/clip.mp4"

    def script(request: httpx.Request) -> httpx.Response | None:
        if "files/v1:download" in request.url.path:
            return httpx.Response(302, headers={"location": storage})
        if request.url.host == "storage.example.test":
            return httpx.Response(
                200, content=MP4, headers={"content-type": "video/mp4"}
            )
        return None

    upstream = Upstream(
        {
            ("POST", GEMINI, GEMINI_VIDEOS): [_job("g-r", "processing")],
            ("GET", GEMINI, f"{GEMINI_VIDEOS}/g-r"): [
                _job("g-r", "completed", url=own)
            ],
        },
        script=script,
    )
    client, _registry = _client(settings, upstream)
    with client:
        video = client.post("/v1/videos", json={"prompt": "p"}).json()
        downloaded = client.get(f"/v1/videos/{video['id']}/content")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == MP4
    assert upstream.seen[-2].headers["authorization"] == f"Bearer {GEMINI_KEY}"
    assert upstream.seen[-1].url.host == "storage.example.test"
    assert "authorization" not in upstream.seen[-1].headers
