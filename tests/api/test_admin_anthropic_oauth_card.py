"""The Claude subscription card reports facts, and says so when it has none.

The card used to print a masked token and nothing else, while the endpoint
behind it already returned the plan and the expiry. What it must never do is
invent a usage window: "you hit your 5-hour limit" is a claim MCC may only
repeat from a header Anthropic actually sent.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.admin_routes import NOT_YET_OBSERVED
from my_claude_code.providers.anthropic_oauth import rate_limit_headers as rlh
from tests.api.support import create_test_app

LIVE_CREDENTIAL = {
    "accessToken": "sk-ant-oat01-super-secret-value",
    "refreshToken": "sk-ant-ort01-refresh-secret-value",
    "expiresAt": 9_999_999_999_000,
    "refreshTokenExpiresAt": 9_999_999_999_999,
    "rateLimitTier": "default_claude_max_5x",
    "scopes": [
        "user:file_upload",
        "user:inference",
        "user:mcp_servers",
        "user:profile",
        "user:sessions:claude_code",
    ],
    "subscriptionType": "max",
}


@pytest.fixture(autouse=True)
def _clean_observer():
    rlh.OBSERVER._latest = None
    yield
    rlh.OBSERVER._latest = None


def _app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    return create_test_app()


def _write_claude_code_credentials(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": LIVE_CREDENTIAL}), encoding="utf-8"
    )


def _get(app):
    return TestClient(app, client=("127.0.0.1", 50000)).get(
        "/admin/api/anthropic-oauth/sources"
    )


def test_card_reports_plan_expiry_scopes_and_windows(monkeypatch, tmp_path):
    _write_claude_code_credentials(tmp_path)
    rlh.OBSERVER.observe(
        {
            "anthropic-ratelimit-unified-status": "session-limit-reached",
            "anthropic-ratelimit-unified-5h-utilization": "1.0",
            "anthropic-ratelimit-unified-5h-reset": "2026-09-02T22:00:00Z",
            "anthropic-ratelimit-unified-7d-utilization": "0.31",
        },
        status_code=429,
        now=1_788_400_000.0,
    )
    app = _app(monkeypatch, tmp_path)

    response = _get(app)

    assert response.status_code == 200
    assert LIVE_CREDENTIAL["accessToken"] not in response.text
    assert LIVE_CREDENTIAL["refreshToken"] not in response.text
    data = response.json()
    source = data["claude_code"]
    assert source["available"] is True
    assert source["subscription_type"] == "max"
    assert source["rate_limit_tier"] == "default_claude_max_5x"
    assert source["refresh_token_expires_at"] == 9_999_999_999
    assert source["has_inference_scope"] is True
    assert "user:sessions:claude_code" in source["scopes"]
    assert source["source"] == "claude-code"

    windows = data["windows"]
    assert windows["observed"] is True
    assert windows["status"] == "session-limit-reached"
    assert windows["five_hour_reset"] == "2026-09-02T22:00:00Z"
    assert windows["weekly_utilization"] == "0.31"
    # Anthropic sent no overage header, so the card must not invent one.
    assert windows["overage_status"] == NOT_YET_OBSERVED


def test_windows_read_not_yet_observed_before_any_response(monkeypatch, tmp_path):
    _write_claude_code_credentials(tmp_path)
    app = _app(monkeypatch, tmp_path)

    data = _get(app).json()

    assert data["windows"]["observed"] is False
    for field in (
        "status",
        "five_hour_utilization",
        "five_hour_reset",
        "weekly_utilization",
        "weekly_reset",
        "overage_status",
        "usage_limit",
    ):
        assert data["windows"][field] == NOT_YET_OBSERVED


def test_a_missing_inference_scope_is_reported(monkeypatch, tmp_path):
    """Offset 183645139: without this scope the credential cannot answer."""
    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    payload = dict(LIVE_CREDENTIAL, scopes=["user:profile"])
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": payload}), encoding="utf-8"
    )
    app = _app(monkeypatch, tmp_path)

    data = _get(app).json()

    assert data["claude_code"]["has_inference_scope"] is False
