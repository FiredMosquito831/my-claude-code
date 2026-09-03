"""Admin web search credential key management and provider test endpoints."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)
from my_claude_code.websearch.errors import (
    WebSearchConfigError,
    WebSearchRateLimitError,
)
from tests.api.support import create_test_app

_WEBSEARCH_ENV_KEYS = (
    "WEB_SEARCH_PROVIDER",
    "OLLAMA_SEARCH_API_KEY",
    "EXA_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "JINA_API_KEY",
    "SERPER_API_KEY",
    "FIRECRAWL_API_KEY",
    "LINKUP_API_KEY",
    "PERPLEXITY_SEARCH_API_KEY",
    "PARALLEL_API_KEY",
    "SEARCHAPI_API_KEY",
    "SERPAPI_API_KEY",
    "SEARXNG_BASE_URL",
    "FCC_ENV_FILE",
)


def _local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _set_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in _WEBSEARCH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _managed_env_text(tmp_path: Path) -> str:
    env_file = tmp_path / ".mcc" / ".env"
    return env_file.read_text(encoding="utf-8")


def test_websearch_key_add_appends_and_persists_comma_separated(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    client = _local_client(app)

    first = client.post(
        "/admin/api/websearch/credentials/EXA_API_KEY/keys",
        json={"key": "k1-aaaa1111bbbb"},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["applied"] is True
    assert body["provider_id"] == "exa"
    assert body["keys"] == [{"index": 0, "key_label": "k1-a…bbbb"}]
    assert "EXA_API_KEY=k1-aaaa1111bbbb" in _managed_env_text(tmp_path)

    second = client.post(
        "/admin/api/websearch/credentials/EXA_API_KEY/keys",
        json={"key": "k2-cccc2222dddd"},
    )
    assert second.status_code == 200
    assert second.json()["keys"] == [
        {"index": 0, "key_label": "k1-a…bbbb"},
        {"index": 1, "key_label": "k2-c…dddd"},
    ]
    assert "EXA_API_KEY=k1-aaaa1111bbbb,k2-cccc2222dddd" in _managed_env_text(tmp_path)


def test_websearch_key_list_masks_keys_and_reports_no_health_when_unused(
    monkeypatch, tmp_path
):
    _set_home(monkeypatch, tmp_path)
    env_file = tmp_path / ".mcc" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("EXA_API_KEY=k1-aaaa1111bbbb,k2-cccc2222dddd\n", "utf-8")
    app = create_test_app()

    response = _local_client(app).get(
        "/admin/api/websearch/credentials/EXA_API_KEY/keys"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "exa"
    assert body["env_key"] == "EXA_API_KEY"
    assert body["locked"] is False
    assert body["keys"] == [
        {"index": 0, "key_label": "k1-a…bbbb"},
        {"index": 1, "key_label": "k2-c…dddd"},
    ]
    assert body["health"] is None
    assert "k1-aaaa1111bbbb" not in response.text
    assert "k2-cccc2222dddd" not in response.text


def test_websearch_key_delete_removes_index_and_persists(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    env_file = tmp_path / ".mcc" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("EXA_API_KEY=k1-aaaa1111bbbb,k2-cccc2222dddd\n", "utf-8")
    app = create_test_app()

    response = _local_client(app).delete(
        "/admin/api/websearch/credentials/EXA_API_KEY/keys/0"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["keys"] == [{"index": 0, "key_label": "k2-c…dddd"}]
    assert "EXA_API_KEY=k2-cccc2222dddd" in _managed_env_text(tmp_path)
    assert "k1-aaaa1111bbbb" not in _managed_env_text(tmp_path)


def test_websearch_key_delete_out_of_range_is_404(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()

    response = _local_client(app).delete(
        "/admin/api/websearch/credentials/EXA_API_KEY/keys/3"
    )

    assert response.status_code == 404


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_websearch_unknown_credential_env_is_404(monkeypatch, tmp_path, method):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    if method == "get":
        response = client.get("/admin/api/websearch/credentials/NOPE_API_KEY/keys")
    elif method == "post":
        response = client.post(
            "/admin/api/websearch/credentials/NOPE_API_KEY/keys", json={"key": "x"}
        )
    else:
        response = client.delete("/admin/api/websearch/credentials/NOPE_API_KEY/keys/0")

    assert response.status_code == 404


@pytest.mark.parametrize("bad_key", ["", "   ", "k1,k2"])
def test_websearch_key_add_rejects_empty_or_comma_keys(monkeypatch, tmp_path, bad_key):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    response = client.post(
        "/admin/api/websearch/credentials/EXA_API_KEY/keys", json={"key": bad_key}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/admin/api/websearch/credentials/EXA_API_KEY/keys", "get"),
        ("/admin/api/websearch/providers/exa/test", "post"),
    ],
)
def test_websearch_endpoints_are_loopback_only(monkeypatch, tmp_path, path, method):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    remote = TestClient(app, client=("203.0.113.10", 50000))

    response = remote.get(path) if method == "get" else remote.post(path)

    assert response.status_code == 403


def _test_response(*titles: str) -> WebSearchResponse:
    return WebSearchResponse(
        provider="exa",
        query="web search",
        results=tuple(
            WebSearchResultItem(
                title=title,
                url=f"https://example.com/{index}",
                snippet="",
                content=None,
                published=None,
            )
            for index, title in enumerate(titles)
        ),
        key_index=0,
        cost_usd=None,
    )


def test_websearch_provider_test_reports_latency_and_titles(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    provider = MagicMock()
    runtime_provider = AsyncMock(return_value=provider)
    search_with_logging = AsyncMock(
        return_value=_test_response("Alpha", "Beta", "Gamma")
    )
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.runtime_provider", runtime_provider
    )
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.search_with_logging", search_with_logging
    )

    response = _local_client(app).post("/admin/api/websearch/providers/exa/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["provider_id"] == "exa"
    assert body["result_count"] == 3
    assert body["titles"] == ["Alpha", "Beta", "Gamma"]
    assert body["latency_ms"] >= 0
    search_with_logging.assert_awaited_once_with(provider, "web search", max_results=3)


def test_websearch_provider_test_reports_structured_error(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.runtime_provider",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.search_with_logging",
        AsyncMock(
            side_effect=WebSearchRateLimitError("exa", "slow down", status_code=429)
        ),
    )

    response = _local_client(app).post("/admin/api/websearch/providers/exa/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == {
        "kind": "rate_limit",
        "message": "slow down",
        "status_code": 429,
    }
    assert body["latency_ms"] >= 0


def test_websearch_provider_test_reports_config_error(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    app = create_test_app()
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.runtime_provider",
        AsyncMock(
            side_effect=WebSearchConfigError("exa", "EXA_API_KEY is not configured")
        ),
    )

    response = _local_client(app).post("/admin/api/websearch/providers/exa/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["kind"] == "config"


def test_websearch_provider_test_unknown_provider_is_404(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    client = _local_client(create_test_app())

    response = client.post("/admin/api/websearch/providers/nope/test")

    assert response.status_code == 404
