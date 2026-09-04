"""The dashboard's Refresh now / Disconnect controls, and their routes.

Before 6.43.0 the Anthropic subscription card could report that the access
token had expired 65 hours ago and offer nothing to do about it: the route
surface was ``sources``, ``import-claude-code``, ``initiate`` and ``complete``,
and there was no supported way to renew a credential or to clear a dead store
short of a shell.
"""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.constants import (
    ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from tests.api.support import create_test_app

_TOKEN = "sk-ant-oat01-not-a-real-token-value"

LIVE_CREDENTIAL = {
    "accessToken": _TOKEN,
    "refreshToken": "sk-ant-ort01-not-a-real-refresh-value",
    "expiresAt": 9_999_999_999_000,
    "refreshTokenExpiresAt": 9_999_999_999_999,
    "rateLimitTier": "default_claude_max_5x",
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max",
}


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    creds._REFRESH_LOCKS.clear()
    return create_test_app()


def _local(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _remote(app) -> TestClient:
    return TestClient(app, client=("203.0.113.10", 50000))


def _managed_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    store = tmp_path / "anthropic_oauth.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.quarantine_anthropic_oauth_store",
        creds.quarantine_managed_store,
    )
    return store


def _mock_token_endpoint(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(creds.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["refresh", "disconnect"])
def test_the_new_routes_are_loopback_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    app = _app(monkeypatch, tmp_path)
    response = _remote(app).post(f"/admin/api/anthropic-oauth/{route}", json={})
    assert response.status_code == 403


def test_the_loopback_login_routes_are_loopback_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    for route in ("loopback/initiate", "loopback/status"):
        response = _remote(app).post(f"/admin/api/anthropic-oauth/{route}", json={})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_admin_refresh_route_returns_the_new_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    store = _managed_store(monkeypatch, tmp_path)
    store.write_text(json.dumps(LIVE_CREDENTIAL), encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "fresh-access", "expires_in": 3600}
        )

    _mock_token_endpoint(monkeypatch, handler)

    response = _local(app).post("/admin/api/anthropic-oauth/refresh", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["credential_reference"] == ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE
    assert body["expires_at"] > time.time()
    # Never a token, on any path.
    assert _TOKEN not in response.text
    assert "fresh-access" not in response.text


def test_admin_refresh_route_reports_a_429_as_transient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """503, not 401: the credential is fine and must not be re-authenticated."""
    app = _app(monkeypatch, tmp_path)
    store = _managed_store(monkeypatch, tmp_path)
    store.write_text(json.dumps(LIVE_CREDENTIAL), encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"type": "rate_limit_error"}})

    _mock_token_endpoint(monkeypatch, handler)

    response = _local(app).post("/admin/api/anthropic-oauth/refresh", json={})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "rate-limiting" in detail
    assert "sign in again" not in detail.lower()
    # The store survives a rate limit untouched.
    assert store.exists()
    assert json.loads(store.read_text())["accessToken"] == _TOKEN


def test_admin_refresh_route_reports_a_definitive_rejection_as_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    store = _managed_store(monkeypatch, tmp_path)
    store.write_text(json.dumps(LIVE_CREDENTIAL), encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _mock_token_endpoint(monkeypatch, handler)

    response = _local(app).post("/admin/api/anthropic-oauth/refresh", json={})

    assert response.status_code == 401
    assert "sign in again" in response.json()["detail"].lower()
    assert not store.exists()
    assert len(list(tmp_path.glob("anthropic_oauth.json.dead-*"))) == 1


def test_admin_refresh_route_404s_when_there_is_no_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    _managed_store(monkeypatch, tmp_path)

    response = _local(app).post("/admin/api/anthropic-oauth/refresh", json={})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


def test_admin_disconnect_route_quarantines_rather_than_deletes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    store = _managed_store(monkeypatch, tmp_path)
    store.write_text(json.dumps(LIVE_CREDENTIAL), encoding="utf-8")

    response = _local(app).post("/admin/api/anthropic-oauth/disconnect", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["quarantined_as"].startswith("anthropic_oauth.json.dead-")
    assert not store.exists()

    dead = list(tmp_path.glob("anthropic_oauth.json.dead-*"))
    assert len(dead) == 1
    # The evidence survives, and the token is still not in the response.
    assert json.loads(dead[0].read_text())["accessToken"] == _TOKEN
    assert _TOKEN not in response.text


def test_admin_disconnect_route_never_touches_claude_codes_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    store = _managed_store(monkeypatch, tmp_path)
    store.write_text(json.dumps(LIVE_CREDENTIAL), encoding="utf-8")

    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    credentials = claude / ".credentials.json"
    credentials.write_text(
        json.dumps({"claudeAiOauth": LIVE_CREDENTIAL}), encoding="utf-8"
    )
    before = credentials.read_text(encoding="utf-8")

    assert (
        _local(app).post("/admin/api/anthropic-oauth/disconnect", json={}).status_code
        == 200
    )

    assert credentials.read_text(encoding="utf-8") == before


def test_admin_disconnect_route_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _app(monkeypatch, tmp_path)
    _managed_store(monkeypatch, tmp_path)

    response = _local(app).post("/admin/api/anthropic-oauth/disconnect", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "complete"
    assert "no MCC-owned credential" in response.json()["message"]


# ---------------------------------------------------------------------------
# the dashboard script
# ---------------------------------------------------------------------------


def test_admin_static_wires_the_new_anthropic_controls() -> None:
    script = Path("src/my_claude_code/api/admin_static/admin.js").read_text(
        encoding="utf-8"
    )

    # B5 -- the two controls exist and call the two routes.
    assert '"Refresh now"' in script
    assert '"Disconnect"' in script
    assert "/admin/api/anthropic-oauth/refresh" in script
    assert "/admin/api/anthropic-oauth/disconnect" in script

    # B4 -- credential_reference is used, and both success paths say what to do
    # next, in the exact wording the working ChatGPT path already uses.
    assert "function fillAnthropicOAuthFields(" in script
    assert '[data-key="ANTHROPIC_OAUTH_ACCESS_TOKEN"] input' in script
    assert script.count("Apply settings to activate the provider.") >= 4

    # The loopback sign-in, with the same-host guard the ChatGPT flow uses.
    assert (
        "/admin/api/anthropic-oauth/loopback/initiate?same_host_confirmed=true"
        in script
    )
    assert "/admin/api/anthropic-oauth/loopback/status" in script

    # B10 -- an expired refresh token says so, and says Refresh cannot help.
    assert "Refresh token expired" in script
