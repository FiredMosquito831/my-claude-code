"""``MEDIA_FALLBACK_ON_UNDOWNLOADABLE`` (7.68.0): a picture MCC cannot download.

A Gemini-shaped image request whose provider answers with a URL instead of the
picture: MCC has to download it before it can answer. Off (the default) that
happens after the answer committed, exactly as 7.67.0 did it, and a failed
download is the client's error. On, it happens inside the attempt, so a failed
download is that model's failure: it is charged and the next model answers.
Either way a key goes only to the provider's own host.

Fake upstreams only (``httpx.MockTransport``): no test here reaches a real host.
"""

import base64
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_log
from my_claude_code.providers.media.registry import MediaRegistry
from tests.api.support import create_test_app

XAI = "api.x.ai"
TOGETHER = "api.together.ai"
CDN = "cdn.example.test"
XAI_KEY = "xai-" + "a" * 40
TOGETHER_KEY = "tg-" + "b" * 40
PNG_OUT = b"\x89PNG\r\n\x1a\n" + b"\x02" * 64
PNG_NEXT = b"\x89PNG\r\n\x1a\n" + b"\x05" * 64
IMAGE_PATH = "/v1beta/models/mcc-image:generateContent"
STREAM_PATH = "/v1beta/models/mcc-image:streamGenerateContent"
GENERATIONS = "/v1/images/generations"
CDN_URL = f"https://{CDN}/out/kite.png"
SAME_HOST_URL = f"https://{XAI}/files/kite.png"

Call = tuple[str, str, str, bool]

# ----------------------------------------------------------------- goldens
# Captured on v7.67.0 (551ec650) with the same fake hosts, before this setting
# existed: status, content type, body bytes and every upstream call (method,
# host, path, whether it carried a key). The 404's request id is per request.
_INLINE = (
    '{"candidates":[{"content":{"role":"model","parts":[{"inlineData":'
    '{"mimeType":"image/png","data":"iVBORw0KGgoCAgICAgICAgICAgICAgICAgICAgIC'
    'AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC"}}]},'
    '"finishReason":"STOP","index":0}],"modelVersion":"mcc-image"}'
)
GOLDEN_7_67_0: dict[str, dict[str, Any]] = {
    "cdn_ok": {
        "status": 200,
        "content_type": "application/json",
        "body": _INLINE,
        "calls": [
            ("POST", XAI, GENERATIONS, True),
            ("GET", CDN, "/out/kite.png", False),
        ],
    },
    "cdn_404": {
        "status": 404,
        "content_type": "application/json",
        "body": (
            '{"error":{"code":404,"message":"Upstream provider XAI returned HTTP '
            "404.\\nCategory: upstream\\nMapped message: Provider API request "
            'failed.\\n\\nUpstream error:\\n{\\"error\\":{\\"message\\":\\"gone\\"}}'
            '\\n\\nRequest ID: req_<id>","status":"INTERNAL"}}'
        ),
        "calls": [
            ("POST", XAI, GENERATIONS, True),
            ("GET", CDN, "/out/kite.png", False),
        ],
    },
    "same_host": {
        "status": 200,
        "content_type": "application/json",
        "body": _INLINE,
        "calls": [
            ("POST", XAI, GENERATIONS, True),
            ("GET", XAI, "/files/kite.png", True),
        ],
    },
    "stream_cdn_ok": {
        "status": 200,
        "content_type": "text/event-stream; charset=utf-8",
        "body": (
            'data: {"candidates": [{"content": {"role": "model", "parts": '
            '[{"inlineData": {"mimeType": "image/png", "data": '
            '"iVBORw0KGgoCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC'
            'AgICAgICAgICAgICAgICAgICAgICAgIC"}}]}, "finishReason": "STOP", '
            '"index": 0}], "modelVersion": "mcc-image"}\n\n'
        ),
        "calls": [
            ("POST", XAI, GENERATIONS, True),
            ("GET", CDN, "/out/kite.png", False),
        ],
    },
}
_REQUEST_ID = re.compile(r"req_[0-9a-f]{32}")


class Upstream:
    """xAI answers with a link; Together (the next model) with the picture."""

    def __init__(
        self,
        url: str,
        fetch: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.url = url
        self.fetch = fetch
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.seen.append(request)
        host, path = request.url.host, request.url.path
        if host == XAI and path == GENERATIONS:
            return httpx.Response(
                200,
                json={"created": 1, "data": [{"url": self.url, "revised_prompt": "k"}]},
            )
        if host == TOGETHER and path == GENERATIONS:
            return httpx.Response(
                200,
                json={
                    "created": 2,
                    "data": [{"b64_json": base64.b64encode(PNG_NEXT).decode()}],
                },
            )
        if self.fetch is not None:
            return self.fetch(request)
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    def calls(self) -> list[Call]:
        return [
            (r.method, r.url.host, r.url.path, "authorization" in r.headers)
            for r in self.seen
        ]


def _png(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PNG_OUT, headers={"content-type": "image/png"})


def _gone(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"error": {"message": "gone"}})


def _settings(monkeypatch, tmp_path: Path, **values: str) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    base = {
        "XAI_API_KEY": XAI_KEY,
        "TOGETHER_API_KEY": TOGETHER_KEY,
        "PROVIDER_RETRY_ATTEMPTS": "1",
        "MODEL_IMAGE": "xai/grok-2-image",
        "MODEL_IMAGE_FALLBACKS": "together/flux",
    }
    base.update(values)
    return Settings.model_validate(base)


def _image_request() -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": "a red kite"}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }


def _post(
    settings: Settings, upstream: Upstream, path: str = IMAGE_PATH
) -> httpx.Response:
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        return client.post(path, json=_image_request())


def _rows(tmp_path: Path) -> list[sqlite3.Row]:
    request_log.reset_request_log_stores()
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM requests ORDER BY ts_epoch"))
    finally:
        conn.close()


def _attempts(tmp_path: Path, request_id: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(tmp_path / "requests.db")
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT * FROM request_attempts WHERE request_id = ? ORDER BY attempt",
                (request_id,),
            )
        )
    finally:
        conn.close()


def _charged(monkeypatch) -> list[tuple[str, int | None]]:
    """Every failure the media route-health books record: (model ref, status)."""
    charged: list[tuple[str, int | None]] = []
    real = RouteHealthRegistry.record_failure

    def spy(self: RouteHealthRegistry, model_ref: str, **kwargs: Any) -> Any:
        charged.append((model_ref, kwargs.get("status_code")))
        return real(self, model_ref, **kwargs)

    monkeypatch.setattr(RouteHealthRegistry, "record_failure", spy)
    return charged


# --------------------------------------------------------------------- off

SCENARIOS: dict[str, tuple[str, Callable[[httpx.Request], httpx.Response], str]] = {
    "cdn_ok": (CDN_URL, _png, IMAGE_PATH),
    "cdn_404": (CDN_URL, _gone, IMAGE_PATH),
    "same_host": (SAME_HOST_URL, _png, IMAGE_PATH),
    "stream_cdn_ok": (CDN_URL, _png, STREAM_PATH),
}


@pytest.mark.parametrize("setting", [None, "false"], ids=["unset", "false"])
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_off_is_byte_identical_to_7_67_0(
    monkeypatch, tmp_path, scenario: str, setting: str | None
) -> None:
    """Off: the answer, byte for byte, and every upstream call are 7.67.0's.

    Including the failure: a picture that cannot be downloaded is the
    client's error, and the next model is never asked.
    """
    values = {} if setting is None else {"MEDIA_FALLBACK_ON_UNDOWNLOADABLE": setting}
    settings = _settings(monkeypatch, tmp_path, **values)
    assert settings.media_fallback_on_undownloadable is False
    url, fetch, path = SCENARIOS[scenario]
    upstream = Upstream(url, fetch)
    response = _post(settings, upstream, path)
    golden = GOLDEN_7_67_0[scenario]
    assert response.status_code == golden["status"]
    assert response.headers["content-type"] == golden["content_type"]
    body = _REQUEST_ID.sub("req_<id>", response.content.decode("utf-8"))
    assert body == golden["body"]
    assert upstream.calls() == golden["calls"]
    assert all(host != TOGETHER for _method, host, _path, _auth in upstream.calls())


def test_off_failure_is_logged_as_the_clients_error(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    response = _post(settings, Upstream(CDN_URL, _gone))
    assert response.status_code == 404
    (row,) = _rows(tmp_path)
    assert row["status"] == "error"
    assert row["provider"] == "xai"
    # xAI answered (the download failed after the commit); Together, the
    # next model, was never asked.
    attempts = _attempts(tmp_path, row["id"])
    assert [a["outcome"] for a in attempts] == ["succeeded", "skipped"]


# ---------------------------------------------------------------------- on


def test_on_download_failure_charges_model_one_and_model_two_answers(
    monkeypatch, tmp_path
) -> None:
    charged = _charged(monkeypatch)
    settings = _settings(monkeypatch, tmp_path, MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true")
    upstream = Upstream(CDN_URL, _gone)
    response = _post(settings, upstream)

    assert response.status_code == 200, response.text
    (part,) = response.json()["candidates"][0]["content"]["parts"]
    assert part["inlineData"] == {
        "mimeType": "image/png",
        "data": base64.b64encode(PNG_NEXT).decode(),
    }
    assert upstream.calls() == [
        ("POST", XAI, GENERATIONS, True),
        ("GET", CDN, "/out/kite.png", False),
        ("POST", TOGETHER, GENERATIONS, True),
    ]
    assert charged == [("xai/grok-2-image", 502)]
    (row,) = _rows(tmp_path)
    assert row["status"] == "success"
    assert row["provider"] == "together"
    assert row["route_attempt"] == 1
    assert row["output_image_count"] == 1
    assert row["media_bytes_out"] == len(PNG_NEXT)
    first, second = _attempts(tmp_path, row["id"])
    assert (first["outcome"], second["outcome"]) == ("failed", "succeeded")
    assert first["error_message"] == (
        "xai produced the image but it could not be downloaded: HTTP 404"
    )


def test_on_downloaded_answer_is_the_same_bytes_as_off(monkeypatch, tmp_path) -> None:
    """On and fetched: the client gets exactly what 7.67.0 answered."""
    settings = _settings(monkeypatch, tmp_path, MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true")
    upstream = Upstream(CDN_URL, _png)
    response = _post(settings, upstream)
    assert response.status_code == 200
    assert response.text == GOLDEN_7_67_0["cdn_ok"]["body"]
    assert upstream.calls() == GOLDEN_7_67_0["cdn_ok"]["calls"]
    (row,) = _rows(tmp_path)
    assert row["output_image_count"] == 1
    assert row["media_bytes_out"] == len(PNG_OUT)


@pytest.mark.parametrize(
    ("url", "keyed"),
    [(CDN_URL, False), (SAME_HOST_URL, True)],
    ids=["cdn", "provider-host"],
)
def test_on_the_key_never_goes_off_host(monkeypatch, tmp_path, url, keyed) -> None:
    settings = _settings(monkeypatch, tmp_path, MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true")
    upstream = Upstream(url, _png)
    assert _post(settings, upstream).status_code == 200
    fetch = upstream.seen[1]
    assert fetch.method == "GET"
    if keyed:
        assert fetch.headers["authorization"] == f"Bearer {XAI_KEY}"
    else:
        assert "authorization" not in fetch.headers
    for request in upstream.seen:
        if request.url.host not in {XAI, TOGETHER}:
            assert XAI_KEY not in str(request.headers)


def test_on_a_redirect_off_host_drops_the_key(monkeypatch, tmp_path) -> None:
    """The provider's own address may send the picture elsewhere: bare there."""
    settings = _settings(monkeypatch, tmp_path, MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true")

    def fetch(request: httpx.Request) -> httpx.Response:
        if request.url.host == XAI:
            return httpx.Response(302, headers={"location": CDN_URL})
        return _png(request)

    upstream = Upstream(SAME_HOST_URL, fetch)
    assert _post(settings, upstream).status_code == 200
    assert upstream.calls() == [
        ("POST", XAI, GENERATIONS, True),
        ("GET", XAI, "/files/kite.png", True),
        ("GET", CDN, "/out/kite.png", False),
    ]


def test_on_an_answer_with_no_http_address_falls_back(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path, MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true")
    upstream = Upstream("ftp://files.example.test/kite.png")
    response = _post(settings, upstream)
    assert response.status_code == 200
    assert [call[1] for call in upstream.calls()] == [XAI, TOGETHER]
    (row,) = _rows(tmp_path)
    first, _second = _attempts(tmp_path, row["id"])
    assert first["error_message"] == (
        "xai produced the image but it could not be downloaded: the answer gave "
        "no http(s) address"
    )


def test_on_the_last_model_failing_is_the_clients_error(monkeypatch, tmp_path) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true",
        MODEL_IMAGE_FALLBACKS="",
    )
    upstream = Upstream(CDN_URL, _gone)
    response = _post(settings, upstream)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == 502
    assert "produced the image but it could not be downloaded" in error["message"]
    assert [call[1] for call in upstream.calls()] == [XAI, CDN]


def test_on_leaves_the_openai_images_route_alone(monkeypatch, tmp_path) -> None:
    """Scope: only the Gemini IMAGE branch downloads; an OpenAI client gets its URL."""
    settings = _settings(monkeypatch, tmp_path, MEDIA_FALLBACK_ON_UNDOWNLOADABLE="true")
    upstream = Upstream(CDN_URL, _gone)
    registry = MediaRegistry(transport=httpx.MockTransport(upstream.handler))
    with TestClient(create_test_app(settings, media=registry)) as client:
        response = client.post(GENERATIONS, json={"prompt": "a red kite"})
    assert response.status_code == 200
    assert response.json()["data"] == [{"url": CDN_URL, "revised_prompt": "k"}]
    assert upstream.calls() == [("POST", XAI, GENERATIONS, True)]
