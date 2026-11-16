"""Integration tests for drop-in dialect endpoints and POST /v1/websearch format dispatch."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools import outbound, search_books, search_chain
from my_claude_code.api.web_tools.search_providers import reset_runtime_providers
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.websearch.analytics import reset_analytics_state
from my_claude_code.websearch.registry import build_provider
from tests.api.support import create_test_app
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        **NO_WEB_SEARCH_CREDENTIALS,
        "HOME": str(tmp_path),
        "ENABLE_WEB_SERVER_TOOLS": True,
        "WEB_SEARCH_PROVIDER": "exa",
        "EXA_API_KEY": "exa-test-key-1111",
        "WEB_FETCH_EXTRACTOR": "basic",
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
    set_request_log_path(tmp_path / "requests.db")
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()
    search_books.reset_search_books()
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(reset_runtime_providers())
        loop.close()
    except Exception:
        pass


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    handler: Callable[[httpx.Request], httpx.Response],
) -> Any:
    providers: dict[str, Any] = {}

    async def fake_runtime_provider(cfg: Settings, provider_id: str) -> Any:
        cached = providers.get(provider_id)
        if cached is not None:
            return cached
        prov = build_provider(cfg, provider_id)
        original = prov.__dict__.get("_client")
        if original is not None:
            await original.aclose()
        prov.__dict__["_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        providers[provider_id] = prov
        return prov

    monkeypatch.setattr(outbound, "runtime_provider", fake_runtime_provider)
    monkeypatch.setattr(search_chain, "runtime_provider", fake_runtime_provider)
    app = create_test_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _exa_rich_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "requestId": "exa-req-9",
            "searchTime": 310.0,
            "costDollars": {"total": 0.004},
            "results": [
                {
                    "title": "Python 3.14",
                    "url": "https://docs.python.org/3.14/",
                    "highlights": ["PEP 758 except syntax"],
                    "highlightScores": [0.91],
                    "text": "Full release page text",
                    "publishedDate": "2026-10-05",
                    "author": "Core Team",
                    "score": 0.87,
                    "favicon": "https://docs.python.org/favicon.ico",
                }
            ],
        },
    )


def _exa_sparse_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "title": "Sparse Hit",
                    "url": "https://sparse.example.org/",
                    "highlights": ["Minimal snippet"],
                }
            ]
        },
    )


def test_dialect_probes_and_auth_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _make_settings(tmp_path, ANTHROPIC_AUTH_TOKEN="tok-dialect-secret")
    app = _build_app(monkeypatch, settings, lambda _req: _exa_sparse_response())
    client = TestClient(app, client=("127.0.0.1", 50000))

    for post_path in (
        "/v1/websearch/tavily",
        "/v1/websearch/tavily/search",
        "/v1/websearch/tavily/extract",
        "/v1/websearch/exa",
        "/v1/websearch/exa/search",
        "/v1/websearch/exa/contents",
        "/v1/websearch/deepseek",
        "/v1/websearch/deepseek/tools/search",
        "/v1/websearch/deepseek/v1/tools/search",
        "/v1/websearch/deepseek/tools/fetch",
        "/v1/websearch/deepseek/v1/tools/fetch",
    ):
        for method in ("HEAD", "OPTIONS"):
            probe = client.request(method, post_path)
            assert probe.status_code == 204
            assert probe.headers["allow"] == "POST, HEAD, OPTIONS"

    for get_path in (
        "/v1/websearch/brave",
        "/res/v1/web/search",
        "/v1/websearch/searxng",
        "/search",
    ):
        for method in ("HEAD", "OPTIONS"):
            probe = client.request(method, get_path)
            assert probe.status_code == 204
            assert probe.headers["allow"] == "GET, HEAD, OPTIONS"

    # M4: Brave X-Subscription-Token must reject missing/invalid token with 401
    for brave_path in ("/v1/websearch/brave", "/res/v1/web/search"):
        missing = client.get(f"{brave_path}?q=test")
        assert missing.status_code == 401
        assert missing.headers.get("www-authenticate") == "Bearer"
        assert missing.json()["error"]["type"] == "authentication_error"

        wrong = client.get(
            f"{brave_path}?q=test",
            headers={"X-Subscription-Token": "wrong-token"},
        )
        assert wrong.status_code == 401

        # Wrong Bearer must NOT be rescued by valid X-Subscription-Token
        mixed = client.get(
            f"{brave_path}?q=test",
            headers={
                "Authorization": "Bearer wrong",
                "X-Subscription-Token": "tok-dialect-secret",
            },
        )
        assert mixed.status_code == 401

        ok = client.get(
            f"{brave_path}?q=test",
            headers={"X-Subscription-Token": "tok-dialect-secret"},
        )
        assert ok.status_code == 200
        assert ok.json()["type"] == "search"

    # Tavily auth accepts JSON body api_key AND rejects missing/wrong with 401
    assert client.post("/v1/websearch/tavily", json={"query": "q"}).status_code == 401
    assert (
        client.post(
            "/v1/websearch/tavily",
            json={"query": "q", "api_key": "wrong"},
        ).status_code
        == 401
    )
    tavily_ok = client.post(
        "/v1/websearch/tavily",
        json={"query": "q", "api_key": "tok-dialect-secret"},
    )
    assert tavily_ok.status_code == 200
    assert tavily_ok.json()["query"] == "q"

    # Exa, DeepSeek, and SearXNG ?key= auth
    assert client.post("/v1/websearch/exa", json={"query": "q"}).status_code == 401
    assert (
        client.post(
            "/v1/websearch/exa",
            json={"query": "q"},
            headers={"x-api-key": "tok-dialect-secret"},
        ).status_code
        == 200
    )
    assert (
        client.get("/search?q=test&format=json&key=tok-dialect-secret").status_code
        == 200
    )
    assert client.get("/search?q=test&format=json&key=wrong").status_code == 401


def test_post_websearch_format_dispatch_and_dialect_search_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _build_app(
        monkeypatch, _make_settings(tmp_path), lambda _req: _exa_rich_response()
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    # M7 guard: format='tavily' on POST /v1/websearch returns Tavily payload, not canonical wrapper
    fmt_tavily = client.post(
        "/v1/websearch",
        json={"query": "python 3.14", "format": "tavily"},
    )
    assert fmt_tavily.status_code == 200
    tv = fmt_tavily.json()
    assert "object" not in tv
    assert "providers_tried" not in tv
    assert tv["query"] == "python 3.14"
    assert tv["results"][0]["published_date"] == "2026-10-05"
    assert tv["results"][0]["score"] == 0.87

    for fmt, expected_top_key in (
        ("exa", "requestId"),
        ("brave", "web"),
        ("deepseek", "results"),
        ("searxng", "number_of_results"),
    ):
        resp = client.post(
            "/v1/websearch", json={"query": "python 3.14", "format": fmt}
        )
        assert resp.status_code == 200
        assert expected_top_key in resp.json()

    # Canonical format='mcc' remains unchanged and excludes internal highlight_scores/images
    canonical = client.post("/v1/websearch", json={"query": "python 3.14"}).json()
    assert canonical["object"] == "web_search"
    assert canonical["digest"] is None
    assert "images" not in canonical
    assert "highlight_scores" not in canonical["results"][0]

    # Direct dialect endpoints
    exa = client.post(
        "/v1/websearch/exa/search",
        json={"query": "python 3.14", "type": "lite", "contents": {"text": True}},
    )
    assert exa.status_code == 200
    exa_json = exa.json()
    assert exa_json["resolvedSearchType"] == "lite"
    assert exa_json["costDollars"] == {"total": 0.004}
    assert exa_json["results"][0]["highlightScores"] == [0.91]

    ds = client.post(
        "/v1/websearch/deepseek/v1/tools/search",
        json={"query": "python 3.14", "need_content": True},
    )
    assert ds.status_code == 200
    assert ds.json()["results"][0]["content"] == "Full release page text"
    assert ds.json()["results"][0]["rerank_score"] == 0.87

    # M3: GET /search?format=html returns HTTP 400 OpenAI JSON error
    bad_fmt = client.get("/search?q=test&format=html")
    assert bad_fmt.status_code == 400
    assert bad_fmt.json()["error"]["type"] == "invalid_request_error"
    assert "format=json" in bad_fmt.json()["error"]["message"]

    # Missing/blank q & invalid freshness return 400 OpenAI JSON
    assert client.get("/search").status_code == 400
    assert client.get("/res/v1/web/search?q=%20%20").status_code == 400
    assert client.get("/v1/websearch/brave?q=ok&freshness=bogus").status_code == 400

    # SearXNG ignores browser engine strings cleanly
    sx_ok = client.get(
        "/v1/websearch/searxng?q=ok&engines=google,bing&categories=science"
    )
    assert sx_ok.status_code == 200
    assert sx_ok.json()["results"][0]["category"] == "science"


def test_http_iv16_nullability_preserves_none_across_dialects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _build_app(
        monkeypatch, _make_settings(tmp_path), lambda _req: _exa_sparse_response()
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    tv = client.post("/v1/websearch/tavily", json={"query": "sparse"}).json()
    assert tv["results"][0]["score"] is None
    assert tv["results"][0]["published_date"] is None

    exa = client.post("/v1/websearch/exa", json={"query": "sparse"}).json()
    assert "costDollars" not in exa
    assert exa["results"][0]["score"] is None
    assert exa["results"][0]["publishedDate"] is None

    ds = client.post("/v1/websearch/deepseek", json={"query": "sparse"}).json()
    assert ds["results"][0]["rerank_score"] is None
    assert ds["results"][0]["date"] is None

    sx = client.get("/search?q=sparse&format=json").json()
    assert sx["results"][0]["score"] is None
    assert sx["results"][0]["publishedDate"] is None


@pytest.mark.asyncio
async def test_batch_extract_contents_partial_tolerance_and_deepseek_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def handle(request: web.Request) -> web.Response:
        if request.path == "/good":
            return web.Response(
                text="<html><title>Good Page</title><body><p>Healthy article text</p></body></html>",
                content_type="text/html",
            )
        return web.Response(status=500, text="boom")

    aio_app = web.Application()
    aio_app.router.add_get("/{tail:.*}", handle)
    server = TestServer(aio_app, host="127.0.0.1")
    await server.start_server()
    try:
        base = f"http://127.0.0.1:{server.port}"
        settings = _make_settings(tmp_path, REQUEST_LOG_ENABLED=True)
        app = _build_app(monkeypatch, settings, lambda _req: _exa_sparse_response())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://testserver",
        ) as async_client:
            # M5: /tavily/extract tolerates failing URLs inside failed_results[] without aborting batch
            tv_ext = await async_client.post(
                "/v1/websearch/tavily/extract",
                json={"urls": [f"{base}/good", f"{base}/bad", "file:///etc/passwd"]},
            )
            assert tv_ext.status_code == 200
            tv_data = tv_ext.json()
            assert len(tv_data["results"]) == 1
            assert tv_data["results"][0]["url"] == f"{base}/good"
            assert "Healthy article text" in tv_data["results"][0]["raw_content"]
            assert len(tv_data["failed_results"]) == 2
            failed_urls = {entry["url"] for entry in tv_data["failed_results"]}
            assert failed_urls == {f"{base}/bad", "file:///etc/passwd"}

            # Exa /contents also preserves per-URL status split
            exa_c = await async_client.post(
                "/v1/websearch/exa/contents",
                json={"ids": [f"{base}/good", f"{base}/bad"]},
            )
            assert exa_c.status_code == 200
            exa_data = exa_c.json()
            assert len(exa_data["results"]) == 1
            assert exa_data["statuses"][0]["status"] == "success"
            assert exa_data["statuses"][1]["status"] == "error"

            # DeepSeek single fetch succeeds on good URL and returns 400 on SSRF/disallowed scheme
            ds_ok = await async_client.post(
                "/v1/websearch/deepseek/v1/tools/fetch",
                json={"url": f"{base}/good"},
            )
            assert ds_ok.status_code == 200
            assert ds_ok.json()["title"] == "Good Page"
            assert "Healthy article text" in ds_ok.json()["content"]

            ds_bad = await async_client.post(
                "/v1/websearch/deepseek/tools/fetch",
                json={"url": "file:///etc/passwd"},
            )
            assert ds_bad.status_code == 400
    finally:
        await server.close()


def test_disabled_web_tools_returns_503_across_dialects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _build_app(
        monkeypatch,
        _make_settings(tmp_path, ENABLE_WEB_SERVER_TOOLS=False),
        lambda _req: _exa_sparse_response(),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.post("/v1/websearch/tavily", json={"query": "q"}).status_code == 503
    assert client.get("/search?q=q&format=json").status_code == 503
    assert (
        client.post(
            "/v1/websearch/tavily/extract",
            json={"urls": ["https://example.org"]},
        ).status_code
        == 503
    )
