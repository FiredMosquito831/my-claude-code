"""Integration tests for ``POST /v1/webfetch`` parity, SSRF/P1, proxy, and telemetry."""

import sqlite3
from pathlib import Path
from typing import Any, Self

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools import outbound, search_books
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.websearch.analytics import (
    default_websearch_db_path,
    reset_analytics_state,
)
from tests.api.support import create_test_app
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS

ARTICLE_HTML = (
    "<html><head><title>Article Title</title></head><body>"
    "<nav>Nav | Skip</nav>"
    "<article><h1>Main Heading</h1>"
    + "".join(
        f"<p>Paragraph {i} contains meaningful long extraction words for trafilatura.</p>"
        for i in range(15)
    )
    + "</article><footer>Copyright</footer></body></html>"
)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        **NO_WEB_SEARCH_CREDENTIALS,
        "HOME": str(tmp_path),
        "ENABLE_WEB_SERVER_TOOLS": True,
        "WEB_FETCH_EXTRACTOR": "trafilatura",
        "WEB_FETCH_ALLOWED_SCHEMES": "http,https",
        "WEB_FETCH_ALLOW_PRIVATE_NETWORKS": True,
        "REQUEST_LOG_ENABLED": False,
        "WEBSEARCH_LOG_ENABLED": False,
    }
    data.update(overrides)
    return Settings.model_validate(data)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("WEBSEARCH_PROXY", raising=False)
    set_request_log_path(tmp_path / "requests.db")
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()
    search_books.reset_search_books()


def _client(
    settings: Settings, *, client_addr: tuple[str, int] = ("127.0.0.1", 50000)
) -> TestClient:
    app = create_test_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, client=client_addr)


@pytest.mark.asyncio
async def test_probe_and_webfetch_extractors_and_truncation(tmp_path: Path) -> None:
    async def handle(request: web.Request) -> web.Response:
        if request.path == "/plain":
            return web.Response(text="plain text body", content_type="text/plain")
        if request.path == "/fail":
            return web.Response(status=500, text="boom")
        return web.Response(
            text=ARTICLE_HTML, content_type="text/html", charset="utf-8"
        )

    aio_app = web.Application()
    aio_app.router.add_get("/{tail:.*}", handle)
    server = TestServer(aio_app, host="127.0.0.1")
    await server.start_server()
    try:
        base = f"http://127.0.0.1:{server.port}"
        settings = _settings(tmp_path)
        app = create_test_app(settings)
        app.dependency_overrides[get_settings] = lambda: settings

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://testserver",
        ) as client:
            for method in ("HEAD", "OPTIONS"):
                probe = await client.request(method, "/v1/webfetch")
                assert probe.status_code == 204
                assert probe.headers["allow"] == "POST, HEAD, OPTIONS"

            traf = await client.post("/v1/webfetch", json={"url": f"{base}/article"})
            assert traf.status_code == 200
            traf_json = traf.json()
            assert traf_json["object"] == "web_fetch"
            assert traf_json["id"].startswith("wf_")
            assert traf_json["title"] == "Article Title"
            assert traf_json["extractor_used"] == "trafilatura"
            assert traf_json["truncated"] is False
            assert "Main Heading" in traf_json["content"]

            basic = await client.post(
                "/v1/webfetch",
                json={"url": f"{base}/article", "extractor": "basic", "max_chars": 220},
            )
            assert basic.status_code == 200
            basic_json = basic.json()
            assert basic_json["extractor_used"] == "basic"
            assert basic_json["truncated"] is True
            assert len(basic_json["content"]) <= 220

            plain = await client.post("/v1/webfetch", json={"url": f"{base}/plain"})
            assert plain.status_code == 200
            assert plain.json()["extractor_used"] == "plain"
            assert plain.json()["content"] == "plain text body"

            err_502 = await client.post("/v1/webfetch", json={"url": f"{base}/fail"})
            assert err_502.status_code == 502
            assert err_502.json()["error"]["type"] == "upstream_error"
    finally:
        await server.close()


def test_p1_ssrf_tightening_and_non_loopback_guards(tmp_path: Path) -> None:
    # M4: allow_private_networks=True cannot override WEB_FETCH_ALLOW_PRIVATE_NETWORKS=false
    strict_client = _client(_settings(tmp_path, WEB_FETCH_ALLOW_PRIVATE_NETWORKS=False))
    widen = strict_client.post(
        "/v1/webfetch",
        json={"url": "http://127.0.0.1:18531/", "allow_private_networks": True},
    )
    assert widen.status_code == 400
    assert widen.json()["error"]["param"] == "allow_private_networks"

    # Tightening True -> False is allowed and blocks loopback
    tighten_client = _client(_settings(tmp_path, WEB_FETCH_ALLOW_PRIVATE_NETWORKS=True))
    tight = tighten_client.post(
        "/v1/webfetch",
        json={"url": "http://127.0.0.1:18531/", "allow_private_networks": False},
    )
    assert tight.status_code == 400

    # Disallowed scheme -> 400
    bad_scheme = tighten_client.post("/v1/webfetch", json={"url": "file:///etc/passwd"})
    assert bad_scheme.status_code == 400

    # P1: Non-loopback client WITHOUT PROXY_API_KEY cannot fetch private targets
    remote_unauth = _client(
        _settings(tmp_path, WEB_FETCH_ALLOW_PRIVATE_NETWORKS=True, HOST="0.0.0.0"),
        client_addr=("198.51.100.10", 50000),
    )
    blocked_remote = remote_unauth.post(
        "/v1/webfetch", json={"url": "http://127.0.0.1:18531/"}
    )
    assert blocked_remote.status_code == 400
    assert "PROXY_API_KEY" in blocked_remote.json()["error"]["message"]

    # Validation error -> 400 OpenAI envelope (never 422)
    bad_val = tighten_client.post("/v1/webfetch", json={"url": "   "})
    assert bad_val.status_code == 400
    assert bad_val.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_webfetch_proxy_and_telemetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_proxy_kwarg: list[str | None] = []

    class _FakeStreamResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"content-type": "text/plain; charset=utf-8"}
            self.url = httpx.URL("http://127.0.0.1:18531/via-proxy?secret=123")

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self, chunk_size: int = 65_536) -> Any:
            _ = chunk_size
            yield b"proxied response body"

    class _FakeAsyncClient:
        def __init__(self, *args: Any, proxy: str | None = None, **kwargs: Any) -> None:
            _ = args, kwargs
            captured_proxy_kwarg.append(proxy)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def stream(self, method: str, url: str) -> _FakeStreamResponse:
            _ = method, url
            return _FakeStreamResponse()

    monkeypatch.setenv("WEBSEARCH_PROXY", "http://proxy.internal:8888")
    monkeypatch.setattr(outbound.httpx, "AsyncClient", _FakeAsyncClient)

    settings = _settings(
        tmp_path,
        REQUEST_LOG_ENABLED=True,
        WEBSEARCH_LOG_ENABLED=True,
        ANTHROPIC_AUTH_TOKEN="tok-1",
    )
    client = _client(settings, client_addr=("198.51.100.10", 50000))
    resp = client.post(
        "/v1/webfetch",
        json={"url": "http://127.0.0.1:18531/via-proxy?secret=123"},
        headers={"Authorization": "Bearer tok-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "proxied response body"
    assert captured_proxy_kwarg == ["http://proxy.internal:8888"]

    # Verify requests.db local row and websearch.db web_fetch row
    req_id = resp.headers["request-id"]
    store = get_request_log_store()
    assert store is not None
    store.close()
    req_row = store.get_request(req_id)
    assert req_row is not None
    assert req_row["endpoint"] == "/v1/webfetch"
    assert req_row["protocol"] == "rest_webfetch"
    assert req_row["provider"] is None
    assert req_row["resolved_model"] is None
    assert req_row["optimization"] == "web_fetch"
    assert req_row["is_local"] == 1

    reset_analytics_state()
    db_file = default_websearch_db_path()
    assert db_file.exists()
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT provider, status, query, input_json FROM search_log"
        ).fetchall()
    assert len(rows) >= 1
    last = dict(rows[-1])
    assert last["provider"] == "web_fetch"
    assert last["status"] == "success"
    # Secret query string stripped before persistence
    assert "secret=123" not in (last["query"] or "")
    assert "secret=123" not in (last["input_json"] or "")
