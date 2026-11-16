"""Integration tests for ``POST /v1/websearch`` parity, auth, overrides, and telemetry."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools import (
    outbound,
    search_books,
    search_chain,
    streaming,
)
from my_claude_code.api.web_tools.search_providers import (
    reset_runtime_providers,
)
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.request_log import (
    get_request_log_store,
    set_request_log_path,
)
from my_claude_code.websearch.analytics import reset_analytics_state
from my_claude_code.websearch.errors import WebSearchRateLimitError
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
        "TAVILY_API_KEY": "tvly-test-key-2222",
        "REQUEST_LOG_ENABLED": False,
        "WEBSEARCH_LOG_ENABLED": False,
    }
    data.update(overrides)
    return Settings.model_validate(data)


@pytest.fixture(autouse=True)
def _clean_websearch_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
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
    *,
    recorded_requests: list[httpx.Request] | None = None,
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

        def _dispatch(req: httpx.Request) -> httpx.Response:
            if recorded_requests is not None:
                recorded_requests.append(req)
            return handler(req)

        prov.__dict__["_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(_dispatch)
        )
        providers[provider_id] = prov
        return prov

    monkeypatch.setattr(outbound, "runtime_provider", fake_runtime_provider)
    monkeypatch.setattr(search_chain, "runtime_provider", fake_runtime_provider)
    app = create_test_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    recorded_requests: list[httpx.Request] | None = None,
) -> TestClient:
    app = _build_app(
        monkeypatch,
        settings,
        handler,
        recorded_requests=recorded_requests,
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def test_probe_head_and_options_on_websearch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = _build_client(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: httpx.Response(200, json={"results": []}),
    )
    for method in ("HEAD", "OPTIONS"):
        resp = client.request(method, "/v1/websearch")
        assert resp.status_code == 204
        assert resp.headers["allow"] == "POST, HEAD, OPTIONS"


def test_auth_forms_and_401_openai_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _make_settings(tmp_path, ANTHROPIC_AUTH_TOKEN="secret-proxy-token")
    client = _build_client(
        monkeypatch,
        settings,
        lambda _req: httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Exa", "url": "https://example.org", "highlights": ["ok"]}
                ]
            },
        ),
    )

    unauth = client.post("/v1/websearch", json={"query": "hello"})
    assert unauth.status_code == 401
    assert unauth.headers.get("www-authenticate") == "Bearer"
    assert unauth.json()["error"]["type"] == "authentication_error"

    bad = client.post(
        "/v1/websearch",
        json={"query": "hello"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad.status_code == 401

    for headers, query_suffix in (
        ({"Authorization": "Bearer secret-proxy-token"}, ""),
        ({"x-api-key": "secret-proxy-token"}, ""),
        ({"x-goog-api-key": "secret-proxy-token"}, ""),
        ({}, "?key=secret-proxy-token"),
    ):
        ok = client.post(
            f"/v1/websearch{query_suffix}",
            json={"query": "hello"},
            headers=headers,
        )
        assert ok.status_code == 200
        assert ok.json()["provider"] == "exa"


def test_default_parity_digest_and_non_mutating_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: list[httpx.Request] = []
    monkeypatch.setenv("EXA_SEARCH_TYPE", "fast")
    settings = _make_settings(tmp_path)

    def upstream(req: httpx.Request) -> httpx.Response:
        if "tavily" in req.url.host:
            return httpx.Response(
                200,
                json={
                    "request_id": "tv-1",
                    "response_time": 0.8,
                    "usage": {"credits": 1},
                    "answer": "Tavily answer",
                    "results": [
                        {
                            "title": "Tavily Title",
                            "url": "https://tavily.example/p",
                            "content": "Tavily snippet",
                            "score": 0.9,
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "requestId": "exa-1",
                "searchTime": 210.0,
                "costDollars": {"total": 0.005},
                "results": [
                    {
                        "title": f"Exa {idx}",
                        "url": f"https://exa.example/{idx}",
                        "highlights": [f"hl {idx}"],
                        "publishedDate": "2026-01-05",
                    }
                    for idx in range(12)
                ],
            },
        )

    client = _build_client(monkeypatch, settings, upstream, recorded_requests=recorded)

    # P3: default format='mcc' leaves digest null and caps results at _MAX_SEARCH_RESULTS=10
    r_default = client.post("/v1/websearch", json={"query": "mcc search"})
    assert r_default.status_code == 200
    body_default = r_default.json()
    assert body_default["object"] == "web_search"
    assert body_default["id"].startswith("ws_")
    assert body_default["provider"] == "exa"
    assert body_default["fallback_used"] is False
    assert body_default["providers_tried"] == ["exa"]
    assert body_default["supports_domain_filters"] is True
    assert body_default["synthesis"] is None
    assert body_default["digest"] is None
    assert len(body_default["results"]) == 10
    assert body_default["usage"]["search_cost_usd"] == 0.005

    # P3 + PART 5.4: format='digest' matches streaming._search_summary character-for-character
    r_digest = client.post(
        "/v1/websearch",
        json={"query": "mcc search", "format": "digest", "max_results": 2},
    )
    assert r_digest.status_code == 200
    body_digest = r_digest.json()
    assert len(body_digest["results"]) == 2
    expected_items = [
        {
            "title": row["title"],
            "url": row["url"],
            "snippet": row["snippet"],
            "content": row["content"] or "",
            "published": row["published"] or "",
            "answer": "",
            "provider": "exa",
        }
        for row in body_digest["results"]
    ]
    assert body_digest["digest"] == streaming._search_summary(
        "mcc search", expected_items, settings
    )

    # M1 / Non-mutating override followed by default search
    r_override = client.post(
        "/v1/websearch",
        json={
            "query": "deep query",
            "search_depth": "deep",
            "country": "de",
            "include_content": True,
        },
    )
    assert r_override.status_code == 200
    r_after = client.post("/v1/websearch", json={"query": "plain after"})
    assert r_after.status_code == 200

    exa_bodies = [
        json.loads(req.content.decode()) for req in recorded if "exa" in req.url.host
    ]
    assert exa_bodies[2]["type"] == "deep"
    assert exa_bodies[2]["userLocation"] == "DE"
    assert exa_bodies[3]["type"] == "fast"
    assert "userLocation" not in exa_bodies[3]

    # Explicit provider override
    r_tavily = client.post(
        "/v1/websearch",
        json={"query": "tv query", "provider": "tavily", "include_answer": True},
    )
    assert r_tavily.status_code == 200
    tv_json = r_tavily.json()
    assert tv_json["provider"] == "tavily"
    assert tv_json["answer"] == "Tavily answer"
    assert tv_json["provider_response_time_ms"] == 800.0
    assert tv_json["usage"]["usage_credits"] == 1.0


def test_chain_fallback_and_domain_filter_reporting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _make_settings(
        tmp_path,
        WEB_SEARCH_CHAIN="exa,serper",
        SERPER_API_KEY="serper-key-1111",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if "exa" in req.url.host:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(
            200,
            json={
                "organic": [{"title": "S", "link": "https://serp.io", "snippet": "ok"}]
            },
        )

    client = _build_client(monkeypatch, settings, handler)
    resp = client.post("/v1/websearch", json={"query": "chain test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "serper"
    assert data["fallback_used"] is True
    assert data["providers_tried"] == ["exa", "serper"]
    assert data["supports_domain_filters"] is False


def test_dashboard_disabled_validation_and_error_status_mappings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # M3: ENABLE_WEB_SERVER_TOOLS=false -> 503
    disabled_tools = _build_client(
        monkeypatch,
        _make_settings(tmp_path, ENABLE_WEB_SERVER_TOOLS=False),
        lambda _req: httpx.Response(200, json={"results": []}),
    )
    r_tools_off = disabled_tools.post("/v1/websearch", json={"query": "q"})
    assert r_tools_off.status_code == 503
    assert r_tools_off.json()["error"]["type"] == "service_unavailable_error"

    # WEB_SEARCH_PROVIDER=disabled -> 503
    disabled_prov = _build_client(
        monkeypatch,
        _make_settings(tmp_path, WEB_SEARCH_PROVIDER="disabled"),
        lambda _req: httpx.Response(200, json={"results": []}),
    )
    assert disabled_prov.post("/v1/websearch", json={"query": "q"}).status_code == 503

    # Validation errors -> 400 OpenAI envelope (never 422)
    client = _build_client(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: httpx.Response(200, json={"results": []}),
    )
    bad_val = client.post("/v1/websearch", json={"query": "   "})
    assert bad_val.status_code == 400
    assert bad_val.json()["error"]["type"] == "invalid_request_error"

    # Unknown explicit provider -> 400
    bad_prov = client.post(
        "/v1/websearch", json={"query": "ok", "provider": "nonexistent"}
    )
    assert bad_prov.status_code == 400

    # M5: Upstream 401 -> WebSearchAuthError -> HTTP 503 (never 401)
    auth_client = _build_client(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: httpx.Response(401, json={"error": "bad key"}),
    )
    upstream_auth = auth_client.post("/v1/websearch", json={"query": "ok"})
    assert upstream_auth.status_code == 503

    # Upstream 502 error
    bad_gateway = _build_client(
        monkeypatch,
        _make_settings(tmp_path),
        lambda _req: httpx.Response(500, json={"error": "boom"}),
    )
    assert bad_gateway.post("/v1/websearch", json={"query": "ok"}).status_code == 502

    # M6: Rate limit with retry_after_seconds -> HTTP 429 + Retry-After header
    async def rate_limited_search(*_args: Any, **_kwargs: Any) -> Any:
        raise WebSearchRateLimitError("exa", "slow down", retry_after_seconds=12.2)

    monkeypatch.setattr(outbound, "_run_web_search_response", rate_limited_search)
    rl_resp = client.post("/v1/websearch", json={"query": "ok"})
    assert rl_resp.status_code == 429
    assert rl_resp.headers["retry-after"] == "13"


@pytest.mark.asyncio
async def test_fetch_missing_content_enriches_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def handle(request: web.Request) -> web.Response:
        if request.path == "/good":
            return web.Response(
                text="<html><title>Good</title><body><article><p>Fetched article body text.</p></article></body></html>",
                content_type="text/html",
            )
        return web.Response(status=500, text="server error")

    aio_app = web.Application()
    aio_app.router.add_get("/{tail:.*}", handle)
    server = TestServer(aio_app, host="127.0.0.1")
    await server.start_server()
    try:
        base = f"http://127.0.0.1:{server.port}"
        settings = _make_settings(tmp_path, WEB_FETCH_EXTRACTOR="basic")
        app = _build_app(
            monkeypatch,
            settings,
            lambda _req: httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "Good", "url": f"{base}/good", "highlights": ["s1"]},
                        {"title": "Bad", "url": f"{base}/bad", "highlights": ["s2"]},
                    ]
                },
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
            base_url="http://testserver",
        ) as async_client:
            resp = await async_client.post(
                "/v1/websearch",
                json={
                    "query": "enrich",
                    "fetch_missing_content": True,
                    "fetch_top_n": 2,
                },
            )
        assert resp.status_code == 200
        hits = resp.json()["results"]
        assert "Fetched article body text." in hits[0]["content"]
        assert hits[1]["content"] is None
        assert hits[1]["snippet"] == "s2"
    finally:
        await server.close()


def test_websearch_logs_local_row_in_requests_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M7 guard: logged with provider=None, resolved_model=None, optimization='web_search'."""
    settings = _make_settings(tmp_path, REQUEST_LOG_ENABLED=True)
    client = _build_client(
        monkeypatch,
        settings,
        lambda _req: httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Exa", "url": "https://example.org", "highlights": ["ok"]}
                ]
            },
        ),
    )
    resp = client.post("/v1/websearch", json={"query": "local row query"})
    assert resp.status_code == 200
    request_id = resp.headers["request-id"]

    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request(request_id)
    assert row is not None
    assert row["endpoint"] == "/v1/websearch"
    assert row["protocol"] == "rest_websearch"
    assert row["provider"] is None
    assert row["resolved_model"] is None
    assert row["cost_usd"] is None
    assert row["optimization"] == "web_search"
    assert row["is_local"] == 1
    assert row["input_text"] == "local row query"
