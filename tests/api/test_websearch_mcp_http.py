"""Tests for the pure-FastAPI Streamable HTTP MCP endpoint (/v1/mcp/websearch)."""

import ast
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools.egress import WebFetchEgressViolation
from my_claude_code.api.web_tools.outbound import WebSearchRouteMeta
from my_claude_code.api.wire_surfaces import WEBSEARCH_MCP_HTTP_PATH
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import get_request_log_store, set_request_log_path
from my_claude_code.core.websearch.models import WebSearchResponse, WebSearchResultItem
from my_claude_code.websearch.analytics import (
    default_websearch_db_path,
    reset_analytics_state,
)
from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _isolated_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / ".mcc"))
    set_request_log_path(tmp_path / "requests.db")
    reset_analytics_state()
    yield
    store = get_request_log_store()
    if store is not None:
        store.close()
    set_request_log_path(None)
    reset_analytics_state()


def _fake_search_response(query: str, *_args: Any, **_kw: Any) -> Any:
    item = WebSearchResultItem(
        title="Context7 / Exa Result",
        url="https://docs.python.org/3/",
        snippet="Documentation excerpt",
        content=None,
        published=None,
    )
    resp = WebSearchResponse(
        provider="exa", query=query, results=(item,), key_index=0, cost_usd=None
    )
    items = [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "content": "",
            "published": "",
            "answer": "",
            "provider": "exa",
        }
    ]
    meta = WebSearchRouteMeta(
        route_id="route_1",
        primary_provider="exa",
        terminal_provider="exa",
        providers_tried=("exa",),
        fallback_used=False,
        supports_domain_filters=True,
    )
    return resp, items, meta


def test_mcp_routes_module_has_zero_mcp_sdk_imports() -> None:
    """Owner boundary guard matching tests/contracts/test_import_boundaries.py."""
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "my_claude_code"
        / "api"
        / "websearch_mcp_routes.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for mod in imported:
        assert mod != "mcp" and not mod.startswith("mcp.")
        assert mod != "mcp_types" and not mod.startswith("mcp_types.")
        assert not mod.startswith("my_claude_code.mcp_media.server")


def test_mcp_auth_probes_toggle_and_protocol_errors() -> None:
    # Auth required across Bearer / x-api-key / ?key=.
    app_auth = create_test_app(
        Settings.model_validate({"ANTHROPIC_AUTH_TOKEN": "secret-tok"}),
        providers={},
    )
    with TestClient(app_auth) as client:
        assert (
            client.post(
                WEBSEARCH_MCP_HTTP_PATH,
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"{WEBSEARCH_MCP_HTTP_PATH}?key=secret-tok",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            ).status_code
            == 200
        )
        head = client.head(
            WEBSEARCH_MCP_HTTP_PATH, headers={"Authorization": "Bearer secret-tok"}
        )
        assert head.status_code == 204
        assert head.headers["allow"] == "POST, HEAD, OPTIONS"

    # Toggle WEB_SEARCH_MCP_HTTP_ENABLED=false -> 503.
    app_off = create_test_app(
        Settings.model_validate({"WEB_SEARCH_MCP_HTTP_ENABLED": False}),
        providers={},
    )
    with TestClient(app_off) as client:
        off_resp = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert off_resp.status_code == 503
        assert client.options(WEBSEARCH_MCP_HTTP_PATH).status_code == 503

    # JSON-RPC parse error -32700, invalid req -32600, unknown method -32601, notification 202.
    app = create_test_app(providers={})
    with TestClient(app) as client:
        bad_json = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            content="{not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert bad_json.status_code == 400
        assert bad_json.json()["error"]["code"] == -32700

        bad_req = client.post(WEBSEARCH_MCP_HTTP_PATH, json={"foo": "bar"})
        assert bad_req.status_code == 400
        assert bad_req.json()["error"]["code"] == -32600

        notif = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notif.status_code == 202
        assert notif.content == b""

        unknown = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
        )
        assert unknown.status_code == 200
        assert unknown.json()["error"]["code"] == -32601


def test_mcp_initialize_tools_list_calls_sse_and_schema_on_error(
    tmp_path: Path,
) -> None:
    app = create_test_app(providers={})
    with (
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_search_response",
            side_effect=_fake_search_response,
        ) as mocked_search,
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
            return_value=(
                {
                    "url": "https://docs.python.org/3/",
                    "title": "Python Docs",
                    "media_type": "text/plain",
                    "data": "Welcome to Python 3 docs.",
                },
                "trafilatura",
                False,
            ),
        ),
        TestClient(app) as client,
    ):
        init = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            },
        )
        assert init.status_code == 200
        init_res = init.json()["result"]
        assert init_res["protocolVersion"] == "2025-03-26"
        assert "tools" in init_res["capabilities"]

        # Accept: text/event-stream negotiation
        listed = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            headers={"Accept": "text/event-stream"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200
        assert listed.headers["content-type"].startswith("text/event-stream")
        assert "event: message" in listed.text
        data_line = next(
            line for line in listed.text.splitlines() if line.startswith("data:")
        )
        tools_payload = json.loads(data_line[len("data:") :].strip())["result"]["tools"]
        assert [t["name"] for t in tools_payload] == [
            "web_search",
            "web_fetch",
            "search_docs",
        ]
        tools_by_name = {t["name"]: t for t in tools_payload}

        # Invalid arguments -> CallToolResult(isError=True) embedding full inputSchema!
        bad_search = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"bad_param": 1}},
            },
        )
        assert bad_search.status_code == 200
        bad_res = bad_search.json()["result"]
        assert bad_res["isError"] is True
        assert (
            bad_res["structuredContent"]["inputSchema"]
            == tools_by_name["web_search"]["inputSchema"]
        )
        assert (
            json.dumps(tools_by_name["web_search"]["inputSchema"], sort_keys=True)
            in (bad_res["content"][0]["text"])
        )

        # Unknown tool -> JSON-RPC -32602 with available_tools schemas in error.data!
        unknown_tool = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
            },
        )
        assert unknown_tool.json()["error"]["code"] == -32602
        assert "web_search" in unknown_tool.json()["error"]["data"]["available_tools"]

        # Valid web_search, search_docs (forces provider_override='context7'), and web_fetch.
        ok_search = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"query": "python 3.14"}},
            },
        )
        assert ok_search.json()["result"]["isError"] is False

        ok_docs = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"library": "fastapi", "query": "depends"},
                },
            },
        )
        assert ok_docs.json()["result"]["isError"] is False
        assert (
            mocked_search.call_args_list[-1].kwargs["provider_override"] == "context7"
        )

        ok_fetch = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "web_fetch",
                    "arguments": {"url": "https://docs.python.org/3/"},
                },
            },
        )
        assert ok_fetch.json()["result"]["isError"] is False
        assert (
            ok_fetch.json()["result"]["structuredContent"]["extractor"] == "trafilatura"
        )

    # SSRF private IP blocked cleanly.
    with (
        patch(
            "my_claude_code.api.web_tools.outbound._run_web_fetch_detail",
            side_effect=WebFetchEgressViolation("private network blocked"),
        ),
        TestClient(app) as client,
    ):
        ssrf = client.post(
            WEBSEARCH_MCP_HTTP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "web_fetch",
                    "arguments": {"url": "http://127.0.0.1/secret"},
                },
            },
        )
        assert ssrf.json()["result"]["isError"] is True

    # Verify requests.db logged MCP local rows + websearch.db logged web_fetch.
    store = get_request_log_store()
    assert store is not None
    store.close()
    with sqlite3.connect(tmp_path / "requests.db") as conn:
        mcp_rows = conn.execute(
            "SELECT protocol, is_local, optimization FROM requests"
        ).fetchall()
    assert len(mcp_rows) >= 3
    assert all(row[0] == "mcp" and row[1] == 1 for row in mcp_rows)
    reset_analytics_state()
    ws_db = default_websearch_db_path()
    assert ws_db.exists()
    with sqlite3.connect(ws_db) as ws_conn:
        fetch_rows = ws_conn.execute(
            "SELECT provider, status FROM search_log WHERE provider='web_fetch'"
        ).fetchall()
    assert ("web_fetch", "success") in fetch_rows
