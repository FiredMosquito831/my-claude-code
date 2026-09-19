"""The card lists every account, and each row's controls act on that one.

Everything here runs against a scratch HOME and a scratch config directory.
No real credential file is read and no token endpoint is contacted: the
refresh route is exercised against a fixture exchange.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.credential_names import (
    oauth_credential_id,
    oauth_pool_id,
    pool_names,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth import rate_limit_headers as rlh
from my_claude_code.providers.chatgpt_oauth import credentials as chat
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from tests.api.support import create_test_app


@pytest.fixture(autouse=True)
def _clean_observer():
    rlh.OBSERVER._latest = None
    rlh.OBSERVER._by_account.clear()
    yield
    rlh.OBSERVER._latest = None
    rlh.OBSERVER._by_account.clear()


def _app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "mcc"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "my_claude_code.config.credential_names.credential_names_path",
        lambda: tmp_path / "credential_names.json",
    )
    monkeypatch.setattr(
        creds, "managed_store_path", lambda: tmp_path / "anthropic_oauth.json"
    )
    creds._REFRESH_LOCKS.clear()
    return create_test_app()


def _client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def _seed(count: int = 2) -> list[str]:
    ids = []
    for index in range(count):
        record = creds.add_or_update_account(
            creds.OAuthTokens(
                access_token=f"sk-ant-oat01-secret-{index}",
                refresh_token=f"sk-ant-ort01-secret-{index}",
                expires_at=9_999_999_999,
                account_uuid=f"uuid-{index}",
                account_email=f"person{index}@example.test",
                subscription_type="max",
                scopes=("user:inference",),
            ),
            origin=ORIGIN_MCC,
        )
        ids.append(record.id)
    return ids


def test_the_sources_payload_lists_every_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    account_ids = _seed(2)

    response = _client(app).get("/admin/api/anthropic-oauth/sources")

    assert response.status_code == 200
    # Not one token, on any row, in any field.
    assert "sk-ant-oat01-secret-0" not in response.text
    assert "sk-ant-ort01-secret-1" not in response.text
    data = response.json()
    assert [row["account_id"] for row in data["accounts"]] == account_ids
    assert data["accounts"][0]["name"] == "person0@example.test"
    assert data["accounts"][1]["subscription_type"] == "max"


def test_the_sources_payload_keeps_mcc_as_an_alias_for_the_primary_account(
    monkeypatch, tmp_path
):
    """One release of grace, so a cached older bundle does not blank the card."""
    app = _app(monkeypatch, tmp_path)
    _seed(2)

    data = _client(app).get("/admin/api/anthropic-oauth/sources").json()

    assert data["mcc"]["available"] is True
    assert data["mcc"]["masked_token"] == data["accounts"][0]["masked_token"]


def test_the_windows_are_attributed_to_the_account_that_observed_them(
    monkeypatch, tmp_path
):
    app = _app(monkeypatch, tmp_path)
    _seed(2)
    rlh.OBSERVER.observe(
        {"anthropic-ratelimit-unified-5h-utilization": "0.91"},
        status_code=200,
        now=1_788_400_000.0,
        account_id="uuid-1",
    )

    data = _client(app).get("/admin/api/anthropic-oauth/sources").json()

    # A 5-hour utilisation belongs to the subscription that observed it.
    # Reporting it on the other account's row is a wrong answer, not a
    # rounding error.
    assert data["accounts"][0]["windows"]["observed"] is False
    assert data["accounts"][1]["windows"]["five_hour_utilization"] == "0.91"


def test_the_per_account_disconnect_route_leaves_the_others(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    account_ids = _seed(2)

    response = _client(app).post(
        f"/admin/api/anthropic-oauth/accounts/{account_ids[0]}/disconnect",
        json={},
    )

    assert response.status_code == 200
    assert [record.id for record in creds.load_accounts()] == [account_ids[1]]
    assert list(tmp_path.glob("anthropic_oauth.json.dead-*"))


def test_disconnecting_an_account_that_is_not_there_is_a_404(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _seed(1)

    response = _client(app).post(
        "/admin/api/anthropic-oauth/accounts/nope/disconnect", json={}
    )

    assert response.status_code == 404


def test_the_per_account_refresh_route_refreshes_only_that_account(
    monkeypatch, tmp_path
):
    import httpx

    app = _app(monkeypatch, tmp_path)
    account_ids = _seed(2)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["refresh_token"])
        return httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "rotated",
                "expires_in": 3600,
                "account": {"uuid": "uuid-1"},
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(creds.httpx, "AsyncClient", factory)

    response = _client(app).post(
        f"/admin/api/anthropic-oauth/accounts/{account_ids[1]}/refresh", json={}
    )

    assert response.status_code == 200
    assert seen == ["sk-ant-ort01-secret-1"]
    records = creds.load_accounts()
    assert records[0].tokens.access_token == "sk-ant-oat01-secret-0"
    assert records[1].tokens.access_token == "fresh"


def test_refreshing_an_account_that_is_not_there_is_a_404(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    _seed(1)

    response = _client(app).post(
        "/admin/api/anthropic-oauth/accounts/nope/refresh", json={}
    )

    assert response.status_code == 404


def test_naming_an_account_stores_the_name_and_nothing_else(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    account_ids = _seed(1)

    response = _client(app).put(
        f"/admin/api/anthropic-oauth/accounts/{account_ids[0]}/name",
        json={"name": "the work one"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "the work one"
    names = pool_names(oauth_pool_id("anthropic_oauth"))
    assert names[oauth_credential_id(account_ids[0])] == "the work one"


def test_naming_an_account_does_not_rebuild_the_provider_generation(
    monkeypatch, tmp_path
):
    """Renaming a credential must not cost every in-flight request its provider."""
    app = _app(monkeypatch, tmp_path)
    account_ids = _seed(1)
    applied: list[object] = []
    monkeypatch.setattr(
        "my_claude_code.api.admin_routes.apply_admin_config",
        lambda *args, **kwargs: applied.append(args),
    )

    _client(app).put(
        f"/admin/api/anthropic-oauth/accounts/{account_ids[0]}/name",
        json={"name": "x"},
    )

    assert applied == []


def test_the_account_routes_are_loopback_only(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    account_ids = _seed(1)
    remote = TestClient(app, client=("10.0.0.7", 50000))

    for path, method in (
        (f"/admin/api/anthropic-oauth/accounts/{account_ids[0]}/refresh", "post"),
        (f"/admin/api/anthropic-oauth/accounts/{account_ids[0]}/disconnect", "post"),
        ("/admin/api/chatgpt-oauth/accounts/acct-1/refresh", "post"),
        ("/admin/api/chatgpt-oauth/accounts/acct-1/disconnect", "post"),
    ):
        response = getattr(remote, method)(path, json={})
        assert response.status_code in (401, 403), path


def test_the_chatgpt_card_now_has_refresh_and_disconnect(monkeypatch, tmp_path):
    """Both routes are new in 7.30.0: this card had neither."""
    app = _app(monkeypatch, tmp_path)
    path = tmp_path / "mcc" / "auth" / "chatgpt-oauth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    chat.add_or_update_chatgpt_account(
        {"access_token": "a", "refresh_token": "r", "account_id": "acct-1"},
        auth_path=path,
    )
    chat.add_or_update_chatgpt_account(
        {"access_token": "b", "refresh_token": "r", "account_id": "acct-2"},
        auth_path=path,
    )

    status = _client(app).get("/admin/api/chatgpt-oauth/status").json()
    assert [row["account_id"] for row in status["accounts"]] == ["acct-1", "acct-2"]

    response = _client(app).post(
        "/admin/api/chatgpt-oauth/accounts/acct-1/disconnect", json={}
    )

    assert response.status_code == 200
    assert [r.id for r in chat.load_chatgpt_accounts(auth_path=path)] == ["acct-2"]


def test_the_chatgpt_status_payload_never_carries_a_token(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    path = tmp_path / "mcc" / "auth" / "chatgpt-oauth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    chat.add_or_update_chatgpt_account(
        {
            "access_token": "super-secret-access",
            "refresh_token": "super-secret-refresh",
            "account_id": "acct-1",
        },
        auth_path=path,
    )

    text = _client(app).get("/admin/api/chatgpt-oauth/status").text

    assert "super-secret-access" not in text
    assert "super-secret-refresh" not in text
