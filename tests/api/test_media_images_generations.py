"""``POST /v1/images/generations`` end to end: routes, rail, fallback, log, store.

The server is the shipping app over a real provider manager; only the upstream
HTTP exchange is a MockTransport, which records every body it received so the
translated wire request is asserted, not assumed.
"""

import base64
import dataclasses
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.media_surfaces import image_generation_surface
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.core.media_store import media_file_path, media_root
from my_claude_code.providers.media.key_pool import MediaKeyPool
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import attempts_as_read, create_test_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_B64 = base64.b64encode(PNG).decode()


class Upstream:
    """Scripted upstream: answers per host, records every request."""

    def __init__(self, answers: dict[str, list[httpx.Response]] | None = None) -> None:
        self.answers = answers or {}
        self.seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append(
            (request.url.host, request.headers.get("authorization", ""), body)
        )
        queue = self.answers.get(request.url.host)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return _image_ok()


def _image_ok(**usage: int) -> httpx.Response:
    payload: dict[str, Any] = {"created": 1, "data": [{"b64_json": PNG_B64}]}
    if usage:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


def _error(status: int, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers)


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "XAI_API_KEY": "xai-" + "a" * 40,
        "TOGETHER_API_KEY": "tg-" + "b" * 40,
        "PROVIDER_RETRY_ATTEMPTS": "1",
    }
    base.update(values)
    return Settings.model_validate(base)


def _client(settings: Settings, upstream: Upstream) -> TestClient:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry))


def _rows(tmp_path: Path) -> list[sqlite3.Row]:
    # Closing the stores drains the writer thread, so every row is on disk.
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def _attempts(tmp_path: Path, request_id: str) -> list[Mapping[str, Any]]:
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        # 7.76.0: skipped attempts may be stored compactly; read them back.
        return attempts_as_read(conn, request_id)
    finally:
        conn.close()


def test_routes_to_rail_primary_and_logs_the_media_row(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    upstream = Upstream({"api.x.ai": [_image_ok(input_tokens=7, output_tokens=1000)]})
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations",
            json={"model": "gpt-image-2", "prompt": "a red cube", "size": "1024x1024"},
        )
    assert response.status_code == 200
    assert response.json()["data"][0]["b64_json"] == PNG_B64
    host, auth, body = upstream.seen[0]
    assert host == "api.x.ai"
    assert auth == "Bearer xai-" + "a" * 40
    # The client's own fields reach the host; only the model is the rail's.
    assert body == {
        "model": "grok-2-image",
        "prompt": "a red cube",
        "size": "1024x1024",
    }
    (row,) = _rows(tmp_path)
    assert row["endpoint"] == "/v1/images/generations"
    assert row["media_operation"] == "image_generate"
    assert row["provider"] == "xai"
    assert row["resolved_model"] == "grok-2-image"
    assert row["status"] == "success"
    assert row["output_image_count"] == 1
    assert row["media_bytes_out"] == len(PNG)
    assert row["media_sha_out"] is not None
    assert row["tokens_in"] == 7
    assert row["tokens_out"] == 1000
    assert row["input_chars"] == len("a red cube")


def test_falls_back_on_5xx_to_next_model(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="together/black-forest-labs/FLUX.1-schnell",
    )
    upstream = Upstream({"api.x.ai": [_error(500, "boom")]})
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200
    assert [seen[0] for seen in upstream.seen] == ["api.x.ai", "api.together.ai"]
    assert upstream.seen[1][2]["model"] == "black-forest-labs/FLUX.1-schnell"
    (row,) = _rows(tmp_path)
    attempts = _attempts(tmp_path, row["id"])
    assert [a["outcome"] for a in attempts] == ["failed", "succeeded"]
    assert row["route_attempt"] == 1


def test_non_stream_failure_is_invisible_to_the_client(monkeypatch, tmp_path) -> None:
    """A failed primary never reaches a non-streaming client: only the winner does."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="together/flux",
    )
    upstream = Upstream({"api.x.ai": [_error(503, "overloaded")]})
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200
    assert "error" not in response.json()


def test_malformed_request_ends_route(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="together/flux",
    )
    upstream = Upstream({"api.x.ai": [_error(400, "Malformed request: bad size")]})
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert [seen[0] for seen in upstream.seen] == ["api.x.ai"]


def test_429_benches_key_model_and_rotates_to_the_next_model(
    monkeypatch, tmp_path
) -> None:
    """RATE_LIMIT_ROUTES_AROUND_MODEL (default on): bench (key, model), move on."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        XAI_API_KEY="xai-" + "a" * 40 + ",xai-" + "c" * 40,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="together/flux",
    )
    upstream = Upstream(
        {"api.x.ai": [_error(429, "slow down", **{"retry-after": "30"})]}
    )
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
        assert response.status_code == 200
        # One try on the first key only: a 429 about the model is not
        # answered by spending the other key on the same model.
        assert [seen[0] for seen in upstream.seen] == ["api.x.ai", "api.together.ai"]
        pool = registry.node("xai")
        assert isinstance(pool, MediaKeyPool)
        metrics = pool.key_health()
    assert [bench["model"] for bench in metrics[0]["model_benches"]] == ["grok-2-image"]
    assert metrics[1]["model_benches"] == []


def test_paused_ref_skipped(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="together/flux",
        MODEL_IMAGE_PAUSED="xai/grok-2-image",
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200
    assert [seen[0] for seen in upstream.seen] == ["api.together.ai"]
    (row,) = _rows(tmp_path)
    attempts = _attempts(tmp_path, row["id"])
    assert attempts[0]["error_kind"] == "paused"


def test_every_ref_paused_names_the_setting(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_PAUSED="xai/grok-2-image",
    )
    with _client(settings, Upstream()) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 503, response.text
    assert "MODEL_IMAGE_PAUSED" in response.json()["error"]["message"]


def test_direct_provider_ref_is_a_single_candidate(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MODEL_IMAGE_FALLBACKS="together/flux",
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations",
            json={"model": "together/some-other-model", "prompt": "p"},
        )
    assert response.status_code == 200
    assert [(seen[0], seen[2]["model"]) for seen in upstream.seen] == [
        ("api.together.ai", "some-other-model")
    ]


def test_empty_rail_answers_openai_error_naming_the_setting(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "not_found_error"
    assert "MODEL_IMAGE" in error["message"]
    assert upstream.seen == []


def test_provider_without_image_surface_is_skipped_not_charged(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        DEEPSEEK_API_KEY="sk-" + "d" * 40,
        MODEL_IMAGE="deepseek/deepseek-chat",
        MODEL_IMAGE_FALLBACKS="xai/grok-2-image",
    )
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 200
    assert [seen[0] for seen in upstream.seen] == ["api.x.ai"]
    (row,) = _rows(tmp_path)
    attempts = _attempts(tmp_path, row["id"])
    assert attempts[0]["error_kind"] == "unsupported"
    assert attempts[0]["outcome"] == "skipped"


def test_a_rail_nobody_can_serve_is_an_invalid_request(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        DEEPSEEK_API_KEY="sk-" + "d" * 40,
        MODEL_IMAGE="deepseek/deepseek-chat",
    )
    with _client(settings, Upstream()) as client:
        response = client.post("/v1/images/generations", json={"prompt": "p"})
    assert response.status_code == 400
    assert "image_generate" in response.json()["error"]["message"]


def test_prompt_is_required(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    with _client(settings, Upstream()) as client:
        response = client.post("/v1/images/generations", json={"size": "1024x1024"})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_store_is_off_by_default(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    with _client(settings, Upstream()) as client:
        assert (
            client.post("/v1/images/generations", json={"prompt": "p"}).status_code
            == 200
        )
    _rows(tmp_path)
    assert not media_root(tmp_path / "requests.db").exists()


def test_store_keeps_the_file_when_enabled(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MODEL_IMAGE="xai/grok-2-image",
        MEDIA_STORE_ENABLED="true",
    )
    with _client(settings, Upstream()) as client:
        assert (
            client.post("/v1/images/generations", json={"prompt": "p"}).status_code
            == 200
        )
    (row,) = _rows(tmp_path)
    path = media_file_path(
        media_root(tmp_path / "requests.db"), row["media_sha_out"], "image/png"
    )
    assert path.read_bytes() == PNG
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        assert conn.execute("SELECT stored FROM media_blobs").fetchall() == [(1,)]
        assert conn.execute("SELECT request_id FROM request_media").fetchall() == [
            (row["id"],)
        ]
    finally:
        conn.close()


@pytest.fixture
def streaming_xai(monkeypatch):
    """Declare xAI's images endpoint as streaming for the length of one test."""
    descriptor = PROVIDER_CATALOG["xai"]
    monkeypatch.setitem(
        PROVIDER_CATALOG,
        "xai",
        dataclasses.replace(
            descriptor, media_surfaces=(image_generation_surface(stream=True),)
        ),
    )


def _sse(*events: dict[str, Any]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )


def test_sse_partials_are_forwarded_and_the_completed_image_is_logged(
    monkeypatch, tmp_path, streaming_xai
) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    frames = _sse(
        {
            "type": "image_generation.partial_image",
            "b64_json": PNG_B64,
            "partial_image_index": 0,
        },
        {
            "type": "image_generation.completed",
            "b64_json": PNG_B64,
            "usage": {"output_tokens": 5},
        },
    )
    upstream = Upstream(
        {
            "api.x.ai": [
                httpx.Response(
                    200, content=frames, headers={"content-type": "text/event-stream"}
                )
            ]
        }
    )
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations", json={"prompt": "p", "stream": True}
        )
        assert response.status_code == 200
        assert response.content == frames
    assert upstream.seen[0][2]["stream"] is True
    (row,) = _rows(tmp_path)
    assert row["output_image_count"] == 1
    assert row["stream"] == 1


def test_a_stream_request_skips_a_surface_that_cannot_stream(
    monkeypatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    upstream = Upstream()
    with _client(settings, upstream) as client:
        response = client.post(
            "/v1/images/generations", json={"prompt": "p", "stream": True}
        )
    assert response.status_code == 400
    assert upstream.seen == []


def test_media_refs_never_reach_the_chat_model_list(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    with _client(settings, Upstream()) as client:
        listed = client.get("/v1/models").json()
    ids = {entry.get("id") for entry in listed.get("data", [])}
    assert "xai/grok-2-image" not in ids


def test_head_and_options_probe(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MODEL_IMAGE="xai/grok-2-image")
    with _client(settings, Upstream()) as client:
        assert client.options("/v1/images/generations").status_code == 204


def test_a_media_auth_failure_never_touches_the_chat_key_books(
    monkeypatch, tmp_path
) -> None:
    """Media owns its books (user decision 2026-09-26 03:38 #4).

    A 401 on an images endpoint locks the key out for MEDIA only: the chat
    pool over the same keys still reports the key healthy, and the chat route
    registry holds no record of the media ref.
    """
    import asyncio

    from my_claude_code.application.execution import route_health_registry
    from my_claude_code.providers.runtime.rotating import RotatingProvider
    from tests.api.support import provider_manager_for_app

    settings = _settings(
        monkeypatch,
        tmp_path,
        XAI_API_KEY="xai-" + "a" * 40 + ",xai-" + "c" * 40,
        MODEL_IMAGE="xai/grok-2-image",
        FALLBACK_BENCH_ENABLED="true",
    )
    # Read from the environment like every rotation policy (the default,
    # single, never leaves key 0).
    monkeypatch.setenv("XAI_API_KEY_ROTATION", "failover")
    upstream = Upstream({"api.x.ai": [_error(401, "invalid api key"), _image_ok()]})
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    app = create_test_app(settings, media=registry)
    with TestClient(app) as client:
        assert (
            client.post("/v1/images/generations", json={"prompt": "p"}).status_code
            == 200
        )
        media_pool = registry.node("xai")
        assert isinstance(media_pool, MediaKeyPool)
        assert media_pool.key_health()[0]["state"] != "HEALTHY"

        async def chat_books() -> list[dict[str, Any]]:
            lease = await provider_manager_for_app(app).acquire()
            try:
                chat = lease.resolve_provider("xai")
                assert isinstance(chat, RotatingProvider)
                return chat.key_health()
            finally:
                await lease.release()

        chat_health = asyncio.run(chat_books())
    assert [entry["state"] for entry in chat_health] == ["HEALTHY", "HEALTHY"]
    assert route_health_registry(settings).why("xai/grok-2-image") is None
