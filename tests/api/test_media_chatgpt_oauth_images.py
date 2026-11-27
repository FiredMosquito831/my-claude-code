"""End-to-end, node, adapter, and contract tests for ``chatgpt_oauth`` image gen & edit."""

import asyncio
import base64
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
    MediaUpload,
)
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_SHAPE_CHATGPT_OAUTH_RESPONSES_IMAGE,
    chatgpt_oauth_image_edit_surface,
    chatgpt_oauth_image_generation_surface,
    surface_for,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.chatgpt_oauth.credentials import (
    CODEX_OAUTH_ORIGINATOR,
    ChatGPTOAuthCredentials,
    store_managed_chatgpt_oauth_tokens,
)
from my_claude_code.providers.chatgpt_oauth.response_headers import (
    OBSERVER as RESPONSE_HEADER_OBSERVER,
)
from my_claude_code.providers.media.adapters import build_wire_body
from my_claude_code.providers.media.chatgpt_oauth import ChatGPTOAuthMediaNode
from my_claude_code.providers.media.key_pool import MediaKeyPool
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from tests.api.support import attempts_as_read, create_test_app, requests_as_read

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
PNG_B64 = base64.b64encode(PNG).decode("ascii")
MASK_PNG = b"\x89PNG\r\n\x1a\n" + b"\xff" * 24
MASK_B64 = base64.b64encode(MASK_PNG).decode("ascii")
FAKE_TOKEN = "eyJhbGciOiJub25lIn0.eyJhY2NvdW50X2lkIjoiYWNjdF8xIn0.sig"
TOGETHER_KEY = "tg-" + "b" * 40


def _make_jwt(account_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload_bytes = json.dumps(
        {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    ).encode("utf-8")
    payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{header}.{payload}.sig"


def _codex_sse(
    *,
    b64: str | None = PNG_B64,
    revised_prompt: str | None = "A sunlit fox",
    usage: dict[str, int] | None = None,
    created_at: int = 1_700_000_777,
    headers: dict[str, str] | None = None,
    status_code: int = 200,
) -> httpx.Response:
    events: list[dict[str, Any]] = [
        {
            "type": "response.output_item.added",
            "item": {
                "id": "ig_1",
                "type": "image_generation_call",
                "status": "in_progress",
            },
        }
    ]
    if b64 is not None:
        done_item: dict[str, Any] = {
            "id": "ig_1",
            "type": "image_generation_call",
            "status": "completed",
            "result": b64,
            "output_format": "png",
        }
        if revised_prompt is not None:
            done_item["revised_prompt"] = revised_prompt
        events.append({"type": "response.output_item.done", "item": done_item})
    completed_output = (
        [
            {
                "id": "ig_1",
                "type": "image_generation_call",
                "status": "completed",
                "result": b64,
            }
        ]
        if b64 is not None
        else [
            {
                "id": "msg_1",
                "type": "message",
                "content": [{"type": "output_text", "text": "Refused."}],
            }
        ]
    )
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "created_at": created_at,
                "status": "completed",
                "output": completed_output,
                "usage": usage
                or {"input_tokens": 10, "output_tokens": 50, "total_tokens": 60},
            },
        }
    )
    body = (
        "".join(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n" for ev in events)
        + "data: [DONE]\n\n"
    )
    resp_headers = {"content-type": "text/event-stream"}
    if headers:
        resp_headers.update(headers)
    return httpx.Response(
        status_code, content=body.encode("utf-8"), headers=resp_headers
    )


class _Upstream:
    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.responses = list(responses or [])
        self.seen: list[tuple[httpx.Request, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        if request.headers.get("content-encoding") == "zstd":
            from compression import zstd

            raw = zstd.decompress(raw)
        payload = json.loads(raw) if raw else {}
        self.seen.append((request, payload))
        if self.responses:
            return (
                self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            )
        return _codex_sse()


def _settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str
) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    from my_claude_code.config.paths import reset_config_dir_cache

    reset_config_dir_cache()
    base = {
        "CHATGPT_OAUTH_ACCESS_TOKEN": FAKE_TOKEN,
        "CHATGPT_OAUTH_ACCOUNT_ID": "acct_override",
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "chatgpt_oauth/gpt-image-1",
    }
    base.update(extra)
    return Settings.model_validate(base)


def _client(settings: Settings, upstream: _Upstream) -> TestClient:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    return TestClient(create_test_app(settings, media=registry))


def _rows(tmp_path: Path) -> list[sqlite3.Row]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def _read_rows(tmp_path: Path) -> list[dict[str, Any]]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = requests_as_read(
            conn, conn.execute("SELECT * FROM requests ORDER BY ts_epoch").fetchall()
        )
    finally:
        conn.close()
    for row in rows:
        if isinstance(row.get("params"), str):
            row["params"] = json.loads(row["params"])
    return rows


def _attempts(tmp_path: Path, request_id: str) -> list[Mapping[str, Any]]:
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        return attempts_as_read(conn, request_id)
    finally:
        conn.close()


# ------------------------------------------------------- catalog & surfaces


def test_chatgpt_oauth_declares_image_surfaces_and_anthropic_oauth_has_none() -> None:
    chatgpt_desc = PROVIDER_CATALOG["chatgpt_oauth"]
    gen = surface_for(chatgpt_desc.media_surfaces, MEDIA_OPERATION_IMAGE_GENERATE)
    edit = surface_for(chatgpt_desc.media_surfaces, MEDIA_OPERATION_IMAGE_EDIT)
    assert gen == chatgpt_oauth_image_generation_surface()
    assert edit == chatgpt_oauth_image_edit_surface()
    assert gen is not None and gen.shape == MEDIA_SHAPE_CHATGPT_OAUTH_RESPONSES_IMAGE
    assert edit is not None and edit.shape == MEDIA_SHAPE_CHATGPT_OAUTH_RESPONSES_IMAGE
    assert gen.path == "codex/responses"
    assert edit.path == "codex/responses"

    anthropic_desc = PROVIDER_CATALOG["anthropic_oauth"]
    assert anthropic_desc.media_surfaces == ()
    assert (
        surface_for(anthropic_desc.media_surfaces, MEDIA_OPERATION_IMAGE_GENERATE)
        is None
    )
    assert (
        surface_for(anthropic_desc.media_surfaces, MEDIA_OPERATION_IMAGE_EDIT) is None
    )


def test_anthropic_oauth_on_image_rail_is_skipped_uncharged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        ANTHROPIC_OAUTH_ACCESS_TOKEN="sk-ant-oat01-" + "a" * 32,
        MODEL_IMAGE="anthropic_oauth/claude-sonnet-4-6",
    )
    upstream = _Upstream()
    with _client(settings, upstream) as client:
        resp = client.post("/v1/images/generations", json={"prompt": "draw a cat"})
    assert resp.status_code == 400
    assert upstream.seen == []


# -------------------------------------------------------- wire body & mask


def test_build_wire_body_forwards_valid_options_and_never_forwards_mask(
    tmp_path: Path,
) -> None:
    import io

    resolved = ResolvedModel(
        original_model="gpt-image-1",
        provider_id="chatgpt_oauth",
        provider_model="gpt-image-1",
        provider_model_ref="chatgpt_oauth/gpt-image-1",
        reasoning_preference=ReasoningPreference.INHERIT,
    )
    img_upload = MediaUpload(
        field="image",
        filename="base.png",
        content_type="image/png",
        size=len(PNG),
        sha256="abc",
        file=io.BytesIO(PNG),
    )
    mask_upload = MediaUpload(
        field="mask",
        filename="mask.png",
        content_type="image/png",
        size=len(MASK_PNG),
        sha256="def",
        file=io.BytesIO(MASK_PNG),
    )
    req = MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_EDIT,
        rail=MediaRail.IMAGE,
        model="chatgpt_oauth/gpt-image-1",
        body={
            "prompt": "replace sky with aurora",
            "size": "1024x1024",
            "quality": "high",
            "background": "opaque",
            "output_format": "png",
            "moderation": "low",
            "response_format": "b64_json",
            "user": "user-1",
            "n": 2,
        },
        uploads=(img_upload, mask_upload),
    )
    wire = asyncio.run(
        build_wire_body(
            chatgpt_oauth_image_edit_surface(),
            MediaAttempt(request=req, resolved=resolved),
        )
    )
    assert wire.json is not None
    assert wire.json["model"] == "gpt-image-1"
    assert wire.json["store"] is False
    assert wire.json["stream"] is True
    assert wire.json["tool_choice"] == {"type": "image_generation"}
    assert wire.json["tools"] == [
        {
            "type": "image_generation",
            "size": "1024x1024",
            "quality": "high",
            "background": "opaque",
            "output_format": "png",
            "moderation": "low",
        }
    ]
    content = wire.json["input"][0]["content"]
    assert content == [
        {"type": "input_text", "text": "replace sky with aurora"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{PNG_B64}"},
    ]
    assert MASK_B64 not in (wire.encoded or b"").decode("utf-8")
    assert set(wire.not_forwarded) == {"mask", "response_format", "user", "n"}
    assert wire.adaptations == ("encoding:multipart->json",)


# ------------------------------------------------------------ E2E generate & edit


def test_images_generations_and_edits_e2e_headers_and_mask_accounting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        CHATGPT_OAUTH_BASE_URL="https://codex.example.test/backend-api/codex",
    )
    upstream = _Upstream(
        [
            _codex_sse(
                headers={
                    "content-type": "text/event-stream",
                    "x-codex-primary-used-percent": "25",
                }
            ),
            _codex_sse(),
        ]
    )
    with _client(settings, upstream) as client:
        gen = client.post(
            "/v1/images/generations",
            json={
                "prompt": "minimalist red fox",
                "size": "1024x1024",
                "quality": "high",
                "response_format": "b64_json",
                "user": "tester",
            },
        )
        assert gen.status_code == 200, gen.text
        assert gen.json() == {
            "created": 1_700_000_777,
            "data": [{"b64_json": PNG_B64, "revised_prompt": "A sunlit fox"}],
            "output_format": "png",
            "usage": {"input_tokens": 10, "output_tokens": 50, "total_tokens": 60},
        }

        edit = client.post(
            "/v1/images/edits",
            data={"prompt": "make fox silver", "n": "1"},
            files=[
                ("image", ("fox.png", PNG, "image/png")),
                ("mask", ("mask.png", MASK_PNG, "image/png")),
            ],
        )
        assert edit.status_code == 200, edit.text
        assert edit.json()["data"][0]["b64_json"] == PNG_B64

    assert len(upstream.seen) == 2
    req0, payload0 = upstream.seen[0]
    assert req0.url.path == "/backend-api/codex/responses"
    assert req0.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert req0.headers["openai-beta"] == "responses=experimental"
    assert req0.headers["originator"] == CODEX_OAUTH_ORIGINATOR
    assert req0.headers["chatgpt-account-id"] == "acct_override"
    assert req0.headers["accept"] == "text/event-stream"
    assert "session-id" in req0.headers
    assert payload0["tools"] == [
        {"type": "image_generation", "size": "1024x1024", "quality": "high"}
    ]
    assert payload0["tool_choice"] == {"type": "image_generation"}
    assert payload0["store"] is False
    assert payload0["stream"] is True

    req1, payload1 = upstream.seen[1]
    assert req1.url.path == "/backend-api/codex/responses"
    assert payload1["input"][0]["content"] == [
        {"type": "input_text", "text": "make fox silver"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{PNG_B64}"},
    ]
    assert MASK_B64 not in json.dumps(payload1)

    rows = _read_rows(tmp_path)
    assert len(rows) == 2
    gen_media = rows[0]["params"]["media"]
    assert gen_media["surface"] == MEDIA_SHAPE_CHATGPT_OAUTH_RESPONSES_IMAGE
    assert set(gen_media["not_forwarded"]) == {"response_format", "user"}
    edit_media = rows[1]["params"]["media"]
    assert edit_media["surface"] == MEDIA_SHAPE_CHATGPT_OAUTH_RESPONSES_IMAGE
    assert set(edit_media["not_forwarded"]) == {"mask", "n"}
    assert "encoding:multipart->json" in edit_media["adapted"]

    snap = RESPONSE_HEADER_OBSERVER.latest
    assert snap is not None
    assert snap.status_code == 200
    assert snap.values.get("x-codex-primary-used-percent") == "25"


def test_streaming_client_folds_whole_codex_answer_into_images_sse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    upstream = _Upstream([_codex_sse()])
    with _client(settings, upstream) as client:
        resp = client.post(
            "/v1/images/generations",
            json={"prompt": "streamed fox", "stream": True},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "image_generation.completed" in resp.text
    assert PNG_B64 in resp.text


def test_empty_or_failed_codex_sse_returns_502_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        TOGETHER_API_KEY=TOGETHER_KEY,
        MODEL_IMAGE="chatgpt_oauth/gpt-image-1",
        MODEL_IMAGE_FALLBACKS="together/black-forest-labs/FLUX.1-schnell",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/codex/responses"):
            return _codex_sse(b64=None)
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": PNG_B64}]})

    registry = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        resp = client.post("/v1/images/generations", json={"prompt": "p"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["b64_json"] == PNG_B64
    (row,) = _rows(tmp_path)
    attempts = _attempts(tmp_path, row["id"])
    assert [a["outcome"] for a in attempts] == ["failed", "succeeded"]
    assert attempts[0]["error_kind"] == "upstream"
    assert "without an image" in (attempts[0]["error_message"] or "")

    # Without fallback -> 502
    settings_no_fb = _settings(
        monkeypatch, tmp_path, MODEL_IMAGE="chatgpt_oauth/gpt-image-1"
    )
    reg_no_fb = MediaRegistry(transport=httpx.MockTransport(handler))
    with TestClient(create_test_app(settings_no_fb, media=reg_no_fb)) as client:
        fail_resp = client.post("/v1/images/generations", json={"prompt": "p"})
    assert fail_resp.status_code == 502


# ----------------------------------------- OAuth 401 refresh & multi-account pool


def test_managed_oauth_401_refreshes_and_retries_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    from my_claude_code.config.paths import reset_config_dir_cache

    reset_config_dir_cache()
    old_jwt = _make_jwt("acct_managed_1")
    new_jwt = _make_jwt("acct_managed_1")
    store_managed_chatgpt_oauth_tokens(
        {
            "access_token": old_jwt,
            "refresh_token": "rt_old",
            "id_token": old_jwt,
            "account_id": "acct_managed_1",
            "expires_at": 9_999_999_999,
        }
    )
    settings = _settings(
        monkeypatch,
        tmp_path,
        CHATGPT_OAUTH_ACCESS_TOKEN="",
        CHATGPT_OAUTH_ACCOUNT_ID="",
    )

    refreshed_calls: list[str | None] = []

    def fake_refresh(account_id: str | None = None, *, purpose=None):
        refreshed_calls.append(account_id)
        return ChatGPTOAuthCredentials(
            access_token=new_jwt + "_fresh",
            account_id="acct_managed_1",
            refresh_token="rt_new",
            expires_at=9_999_999_999,
            source_name="fcc-managed",
        )

    monkeypatch.setattr(
        "my_claude_code.providers.media.chatgpt_oauth.force_refresh_managed_chatgpt_oauth_credentials",
        fake_refresh,
    )

    upstream = _Upstream(
        [
            httpx.Response(401, json={"error": {"message": "expired"}}),
            _codex_sse(),
        ]
    )
    with _client(settings, upstream) as client:
        resp = client.post("/v1/images/generations", json={"prompt": "refresh test"})
    assert resp.status_code == 200, resp.text
    assert refreshed_calls == ["acct_managed_1"]
    assert len(upstream.seen) == 2
    assert upstream.seen[0][0].headers["authorization"] == f"Bearer {old_jwt}"
    assert upstream.seen[1][0].headers["authorization"] == f"Bearer {new_jwt}_fresh"


def test_multi_account_pool_rotates_on_401_across_accounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_OAUTH_ACCESS_TOKEN_ROTATION", "failover")
    from my_claude_code.config.paths import reset_config_dir_cache

    reset_config_dir_cache()
    jwt1 = _make_jwt("acct_one")
    jwt2 = _make_jwt("acct_two")
    for acct, tok in (("acct_one", jwt1), ("acct_two", jwt2)):
        store_managed_chatgpt_oauth_tokens(
            {
                "access_token": tok,
                "refresh_token": f"rt_{acct}",
                "id_token": tok,
                "account_id": acct,
                "expires_at": 9_999_999_999,
            }
        )
    settings = _settings(
        monkeypatch,
        tmp_path,
        CHATGPT_OAUTH_ACCESS_TOKEN="",
        CHATGPT_OAUTH_ACCOUNT_ID="",
    )

    def failing_refresh(account_id: str | None = None, *, purpose=None):
        from my_claude_code.providers.chatgpt_oauth.credentials import ChatGPTOAuthError

        raise ChatGPTOAuthError("refresh token revoked")

    monkeypatch.setattr(
        "my_claude_code.providers.media.chatgpt_oauth.force_refresh_managed_chatgpt_oauth_credentials",
        failing_refresh,
    )

    upstream = _Upstream(
        [
            httpx.Response(401, json={"error": {"message": "unauthorized"}}),
            _codex_sse(),
        ]
    )
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        node = registry.resolve("chatgpt_oauth", settings)
        assert isinstance(node, MediaKeyPool)
        resp = client.post("/v1/images/generations", json={"prompt": "rotate account"})
    assert resp.status_code == 200, resp.text
    assert len(upstream.seen) == 2
    assert upstream.seen[0][0].headers["chatgpt-account-id"] == "acct_one"
    assert upstream.seen[1][0].headers["chatgpt-account-id"] == "acct_two"


def test_node_zstd_413_retry_and_429_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from my_claude_code.core.failures import ExecutionFailure, FailureKind
    from my_claude_code.providers.base import ProviderConfig

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    upstream = _Upstream(
        [
            httpx.Response(413, json={"error": {"message": "payload too large"}}),
            _codex_sse(),
            httpx.Response(
                429,
                json={"error": {"message": "rate limit"}},
                headers={"retry-after": "15"},
            ),
        ]
    )
    node = ChatGPTOAuthMediaNode(
        provider_id="chatgpt_oauth",
        config=ProviderConfig(
            api_key=FAKE_TOKEN,
            base_url="https://codex.example.test/backend-api",
            retry_attempts=1,
            routes_around_model=False,
        ),
        surfaces=(chatgpt_oauth_image_generation_surface(),),
        rate_limiter=ProviderRateLimiter(10, 60, max_concurrency=2, max_retries=0),
        transport=httpx.MockTransport(upstream.handler),
        account_id="acct_zstd",
    )
    attempt = MediaAttempt(
        request=MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="chatgpt_oauth/gpt-image-1",
            body={"prompt": "zstd test"},
        ),
        resolved=ResolvedModel(
            original_model="gpt-image-1",
            provider_id="chatgpt_oauth",
            provider_model="gpt-image-1",
            provider_model_ref="chatgpt_oauth/gpt-image-1",
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )

    async def run() -> None:
        try:
            chunks = [chunk async for chunk in node.execute(attempt)]
            assert len(chunks) == 1
            assert upstream.seen[0][0].headers.get("content-encoding") is None
            assert upstream.seen[1][0].headers.get("content-encoding") == "zstd"

            with pytest.raises(ExecutionFailure) as exc_info:
                async for _ in node.execute(attempt):
                    pass
            assert exc_info.value.kind is FailureKind.RATE_LIMIT
            assert exc_info.value.status_code == 429
            assert node.throttle_remaining() > 0
        finally:
            await node.cleanup()

    asyncio.run(run())
