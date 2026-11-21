"""8.34.0 Web Search Analytics API, Export, Telemetry Capture, and UI Field Parity tests."""

import csv
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_websearch_routes import get_websearch_log_store
from my_claude_code.api.web_tools import outbound, search_books
from my_claude_code.api.web_tools.search_providers import reset_runtime_providers
from my_claude_code.config.settings import Settings
from my_claude_code.core import export as export_engine
from my_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from my_claude_code.websearch.adapters.tavily import TavilyWebSearchProvider
from my_claude_code.websearch.analytics import WebSearchLogStore
from my_claude_code.websearch.base import WebSearchProviderConfig
from my_claude_code.websearch.registry import (
    SearchOutcome,
    SearchRouteOutcome,
    websearch_telemetry_context,
)
from tests.api.support import create_test_app
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS

_TS = datetime(2026, 11, 20, 12, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def log_store(tmp_path: Path):
    store = WebSearchLogStore(tmp_path / "logs" / "websearch.db")
    yield store
    store.close()


@pytest.fixture
def client(
    log_store: WebSearchLogStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    app = create_test_app()
    app.dependency_overrides[get_websearch_log_store] = lambda: log_store
    yield TestClient(app, client=("127.0.0.1", 50000))
    app.dependency_overrides.clear()


def _seed_v834(store: WebSearchLogStore) -> None:
    tries_k0_402 = json.dumps(
        [
            {
                "key_index": 0,
                "key_label": "exak…0000",
                "try": 1,
                "status": 402,
                "kind": "quota",
                "ms": 20.0,
                "ok": False,
                "charged": "quota",
                "benched_for_s": 1800.0,
                "decision": "sk-secret-abc12345678901234567890 exceeded limit",
                "rotated": True,
            },
            {
                "key_index": 1,
                "key_label": "exak…1111",
                "try": 1,
                "status": None,
                "kind": None,
                "ms": 30.0,
                "ok": True,
            },
        ],
        separators=(",", ":"),
    )
    store.record(
        SearchOutcome(
            ts_epoch=_TS,
            ts_iso="2026-11-20T12:00:00+00:00",
            provider="exa",
            key_index=1,
            key_label="exak…1111",
            query="=SUM(1,2) formula",
            results_count=2,
            duration_ms=50.0,
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=0.002,
            route_id="route-chain-1",
            attempt_number=1,
            chain_position=0,
            outcome="succeeded",
            tries_json=tries_k0_402,
            tool_kind="web_search",
            door="rest",
            http_status=200,
            empty_results=False,
        )
    )
    store.record(
        SearchOutcome(
            ts_epoch=_TS + 1,
            ts_iso="2026-11-20T12:00:01+00:00",
            provider="exa",
            key_index=0,
            key_label="exak…0000",
            query="empty 200 query",
            results_count=0,
            duration_ms=40.0,
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=None,
            route_id="route-chain-2",
            attempt_number=1,
            chain_position=0,
            outcome="empty",
            tries_json=None,
            tool_kind="web_search",
            door="anthropic_stream",
            http_status=200,
            empty_results=True,
        )
    )
    store.record(
        SearchOutcome(
            ts_epoch=_TS + 2,
            ts_iso="2026-11-20T12:00:02+00:00",
            provider="context7",
            key_index=0,
            key_label="",
            query="skipped docs",
            results_count=0,
            duration_ms=0.0,
            status="skipped",
            error_kind=None,
            error_message="skipped: paused",
            cost_usd=None,
            route_id="route-chain-2",
            attempt_number=2,
            chain_position=1,
            outcome="skipped",
            tool_kind="search_docs",
            door="mcp",
            http_status=None,
            empty_results=False,
        )
    )
    store.record(
        SearchOutcome(
            ts_epoch=_TS + 3,
            ts_iso="2026-11-20T12:00:03+00:00",
            provider="tavily",
            key_index=0,
            key_label="tvly…1111",
            query="empty 200 query",
            results_count=3,
            duration_ms=65.0,
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=0.001,
            route_id="route-chain-2",
            attempt_number=2,
            chain_position=2,
            outcome="succeeded",
            tool_kind="web_search",
            door="anthropic_stream",
            http_status=200,
            empty_results=False,
        )
    )
    store.record(
        SearchOutcome(
            ts_epoch=_TS + 4,
            ts_iso="2026-11-20T12:00:04+00:00",
            provider="web_fetch",
            key_index=0,
            key_label="",
            query="https://example.invalid/page",
            results_count=1,
            duration_ms=22.0,
            status="success",
            error_kind=None,
            error_message=None,
            cost_usd=None,
            route_id=None,
            attempt_number=1,
            tool_kind="web_fetch",
            door="rest",
            http_status=200,
            empty_results=False,
        )
    )
    store.record_route(
        SearchRouteOutcome(
            route_id="route-chain-1",
            ts_epoch=_TS,
            ts_iso="2026-11-20T12:00:00+00:00",
            query="=SUM(1,2) formula",
            primary_provider="exa",
            terminal_provider="exa",
            provider_path=("exa",),
            attempt_count=1,
            fallback_used=False,
            duration_ms=50.0,
            status="success",
            results_count=2,
            cost_usd=0.002,
            error_kind=None,
            error_message=None,
            door="rest",
            skipped_count=0,
            key_rotations=1,
            empty_fallback_count=0,
        )
    )
    store.record_route(
        SearchRouteOutcome(
            route_id="route-chain-2",
            ts_epoch=_TS + 3,
            ts_iso="2026-11-20T12:00:03+00:00",
            query="empty 200 query",
            primary_provider="exa",
            terminal_provider="tavily",
            provider_path=("exa", "tavily"),
            attempt_count=2,
            fallback_used=True,
            duration_ms=105.0,
            status="success",
            results_count=3,
            cost_usd=0.001,
            error_kind=None,
            error_message=None,
            door="anthropic_stream",
            skipped_count=1,
            key_rotations=0,
            empty_fallback_count=1,
        )
    )
    store.flush()


class TestAdminWebSearchV834Endpoints:
    def test_routes_endpoint_requires_loopback_and_filters(
        self, client: TestClient, log_store: WebSearchLogStore
    ) -> None:
        _seed_v834(log_store)
        remote = TestClient(client.app, client=("203.0.113.10", 50000))
        assert remote.get("/admin/api/websearch/routes").status_code == 403

        all_routes = client.get("/admin/api/websearch/routes").json()
        assert all_routes["total"] == 2
        newest = all_routes["items"][0]
        assert newest["route_id"] == "route-chain-2"
        assert newest["door"] == "anthropic_stream"
        assert newest["skipped_count"] == 1
        assert newest["empty_fallback_count"] == 1
        assert newest["providers"] == ["exa", "tavily"]

        fallback_only = client.get(
            "/admin/api/websearch/routes", params={"fallback_only": "true"}
        ).json()
        assert fallback_only["total"] == 1
        assert fallback_only["items"][0]["route_id"] == "route-chain-2"

        rest_only = client.get(
            "/admin/api/websearch/routes", params={"door": "rest"}
        ).json()
        assert rest_only["total"] == 1
        assert rest_only["items"][0]["key_rotations"] == 1

    def test_status_skipped_door_tool_kind_route_id_and_empty_only_filters(
        self, client: TestClient, log_store: WebSearchLogStore
    ) -> None:
        _seed_v834(log_store)

        # Default stats exclude web_fetch and skipped rows (3 search attempts)
        stats_default = client.get("/admin/api/websearch/stats").json()
        assert stats_default["totals"]["requests"] == 3
        by_key = {(r["provider"], r["key_label"]): r for r in stats_default["by_key"]}
        assert by_key[("exa", "exak…0000")]["tries_failed"] == 1
        assert by_key[("exa", "exak…0000")]["key_rotations"] == 1

        # status="skipped" works on both /requests and /stats
        skipped = client.get(
            "/admin/api/websearch/requests", params={"status": "skipped"}
        )
        assert skipped.status_code == 200
        assert skipped.json()["total"] == 1
        assert skipped.json()["items"][0]["provider"] == "context7"

        skipped_stats = client.get(
            "/admin/api/websearch/stats", params={"status": "skipped"}
        )
        assert skipped_stats.status_code == 200
        assert skipped_stats.json()["totals"]["requests"] == 1

        # tool_kind="web_fetch" isolates web_fetch; tool_kind="all" includes it
        fetch_reqs = client.get(
            "/admin/api/websearch/requests", params={"tool_kind": "web_fetch"}
        ).json()
        assert fetch_reqs["total"] == 1
        assert fetch_reqs["items"][0]["provider"] == "web_fetch"

        docs_reqs = client.get(
            "/admin/api/websearch/requests", params={"tool_kind": "search_docs"}
        ).json()
        assert docs_reqs["total"] == 1
        assert docs_reqs["items"][0]["door"] == "mcp"

        # empty_only returns only empty-200 row
        empty_reqs = client.get(
            "/admin/api/websearch/requests", params={"empty_only": "true"}
        ).json()
        assert empty_reqs["total"] == 1
        assert empty_reqs["items"][0]["http_status"] == 200
        assert empty_reqs["items"][0]["empty_results"] is True

        # route_id returns all sibling attempts for that route
        siblings = client.get(
            "/admin/api/websearch/requests", params={"route_id": "route-chain-2"}
        ).json()
        assert siblings["total"] == 3
        assert {item["provider"] for item in siblings["items"]} == {
            "exa",
            "context7",
            "tavily",
        }


class TestWebSearchExportV834:
    def test_export_includes_chain_columns_sanitizes_and_matches_admin_js(
        self, client: TestClient, log_store: WebSearchLogStore
    ) -> None:
        _seed_v834(log_store)

        fields = (
            "provider,query,route_id,tool_kind,door,http_status,empty_results,"
            "chain_position,outcome,failure_kind,tries_json"
        )
        json_rows = client.get(
            "/admin/api/export",
            params={"format": "json", "scope": "websearch", "fields": fields},
        ).json()
        # Default export excludes web_fetch, keeps 4 search rows (3 counted + 1 skipped)
        assert len(json_rows) == 4
        by_route = {(r["route_id"], r["provider"]): r for r in json_rows}
        r1 = by_route[("route-chain-1", "exa")]
        assert r1["door"] == "rest"
        assert r1["tool_kind"] == "web_search"
        assert r1["http_status"] == 200
        assert r1["empty_results"] is False
        assert r1["chain_position"] == 0
        assert r1["outcome"] == "succeeded"
        assert r1["tries_summary"] == "k0:402 -> k1:200"
        assert "sk-secret-abc12345678901234567890" not in r1["tries_json"]

        # CSV neutralizes leading '=' formula injection in query cell
        csv_resp = client.get(
            "/admin/api/export",
            params={"format": "csv", "scope": "websearch", "fields": fields},
        )
        assert csv_resp.status_code == 200
        csv_rows = list(csv.reader(io.StringIO(csv_resp.content.decode("utf-8-sig"))))
        headers = csv_rows[0]
        assert "Key tries" in headers
        assert "Key tries summary" in headers
        assert "Door" in headers
        assert "HTTP status" in headers
        query_idx = headers.index("Query")
        assert any(row[query_idx].startswith("'=SUM(1,2)") for row in csv_rows[1:])

        # tool_kind="web_fetch" exports only web_fetch
        fetch_rows = client.get(
            "/admin/api/export",
            params={
                "format": "json",
                "scope": "websearch",
                "fields": "provider,tool_kind,door",
                "tool_kind": "web_fetch",
            },
        ).json()
        assert len(fetch_rows) == 1
        assert fetch_rows[0]["provider"] == "web_fetch"

    def test_admin_js_export_fields_websearch_matches_core_export(self) -> None:
        admin_js = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "my_claude_code"
            / "api"
            / "admin_static"
            / "admin.js"
        ).read_text(encoding="utf-8")
        ws_block = admin_js.split("websearch: [", 1)[1].split("],", 1)[0]
        js_pairs = re.findall(
            r'\{\s*id:\s*"([^"]+)",\s*label:\s*"([^"]+)"\s*\}', ws_block
        )
        py_pairs = export_engine.websearch_field_labels()
        assert js_pairs == py_pairs


@pytest.mark.asyncio
async def test_telemetry_capture_door_http_status_empty_and_rotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake HTTP adapters via MockTransport capture door, http_status, empty_results, rotations."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    await reset_runtime_providers()
    search_books.reset_search_books()

    store = WebSearchLogStore(tmp_path / "logs" / "websearch.db")
    monkeypatch.setattr(
        "my_claude_code.websearch.analytics.get_shared_store", lambda: store
    )

    def exa_handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") == "exa-k0-dead":
            return httpx.Response(
                402,
                json={"error": "You have exceeded your credits limit."},
            )
        return httpx.Response(200, json={"results": []})

    def tavily_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Tavily Hit",
                        "url": "https://example.invalid/hit",
                        "content": "snippet",
                    }
                ]
            },
        )

    exa = ExaWebSearchProvider(
        WebSearchProviderConfig(
            api_keys=("exa-k0-dead", "exa-k1-empty"),
            credential_rotation="failover",
            base_url="https://exa.invalid",
            proxy=None,
            http_timeout=5.0,
        )
    )
    exa._client = httpx.AsyncClient(transport=httpx.MockTransport(exa_handler))
    tavily = TavilyWebSearchProvider(
        WebSearchProviderConfig(
            api_keys=("tvly-k0-live",),
            credential_rotation="failover",
            base_url="https://tavily.invalid",
            proxy=None,
            http_timeout=5.0,
        )
    )
    tavily._client = httpx.AsyncClient(transport=httpx.MockTransport(tavily_handler))

    async def fake_runtime(_settings: Settings, provider_id: str):
        if provider_id == "exa":
            return exa
        if provider_id == "tavily":
            return tavily
        raise AssertionError(f"unexpected provider {provider_id}")

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.search_chain.runtime_provider", fake_runtime
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider", fake_runtime
    )

    settings = Settings.model_validate(
        {
            **NO_WEB_SEARCH_CREDENTIALS,
            "EXA_API_KEY": "exa-k0-dead,exa-k1-empty",
            "TAVILY_API_KEY": "tvly-k0-live",
            "WEB_SEARCH_CHAIN": "exa,tavily",
            "WEB_SEARCH_FALLBACK_ON_EMPTY": "true",
        }
    )

    try:
        with websearch_telemetry_context(door="rest", tool_kind="web_search"):
            _resp, items, meta = await outbound._run_web_search_response(
                "probe query", settings
            )
        assert len(items) == 1
        assert meta.terminal_provider == "tavily"
        store.flush()

        reqs = store.requests(route_id=meta.route_id)["items"]
        assert len(reqs) == 2
        by_prov = {item["provider"]: item for item in reqs}
        assert by_prov["exa"]["door"] == "rest"
        assert by_prov["exa"]["tool_kind"] == "web_search"
        assert by_prov["exa"]["http_status"] == 200
        assert by_prov["exa"]["empty_results"] is True
        assert by_prov["exa"]["outcome"] == "empty"
        assert by_prov["tavily"]["http_status"] == 200
        assert by_prov["tavily"]["empty_results"] is False
        assert by_prov["tavily"]["door"] == "rest"

        routes = store.routes()["items"]
        assert len(routes) == 1
        assert routes[0]["door"] == "rest"
        assert routes[0]["key_rotations"] == 1
        assert routes[0]["empty_fallback_count"] == 1
    finally:
        await exa.close()
        await tavily.close()
        store.close()
        search_books.reset_search_books()
        await reset_runtime_providers()
