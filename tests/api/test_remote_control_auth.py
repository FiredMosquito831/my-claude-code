"""Security, pairing, CSRF, WebSocket Origin, and forwarding-header fence tests."""

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.media.ports import MediaRuntimePort
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub


def _build_rc_app(
    monkeypatch, tmp_path: Path, *, rc_enabled: bool = True
) -> tuple[TestClient, RemoteRuntimeHub]:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    workspace = tmp_path / "work"
    workspace.mkdir(parents=True, exist_ok=True)
    settings = Settings.model_validate(
        {
            "RC_ENABLED": rc_enabled,
            "RC_ALLOWED_DIRS": str(workspace),
            "RC_SESSION_SECRET": "test-secret-for-unit-tests-only",
        }
    )
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    hub = RemoteRuntimeHub(settings=settings)
    media: MediaRuntimePort | None = None
    app = create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
            media=media,
            remote=hub,
        )
    )
    return TestClient(app, client=("127.0.0.1", 50000)), hub


def test_admin_rejects_reverse_proxy_forwarding_headers(
    monkeypatch, tmp_path: Path
) -> None:
    client, _hub = _build_rc_app(monkeypatch, tmp_path, rc_enabled=True)
    assert client.get("/admin/api/config").status_code == 200
    assert client.get("/admin/api/rc/status").status_code == 200

    for header in (
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "Forwarded",
        "X-Real-IP",
        "CF-Connecting-IP",
        "True-Client-IP",
    ):
        resp = client.get("/admin/api/config", headers={header: "203.0.113.9"})
        assert resp.status_code == 403, f"Expected 403 for {header}"
        rc_resp = client.get("/admin/api/rc/status", headers={header: "203.0.113.9"})
        assert rc_resp.status_code == 403, f"Expected 403 on /admin/api/rc for {header}"


def test_rc_disabled_returns_503(monkeypatch, tmp_path: Path) -> None:
    client, _hub = _build_rc_app(monkeypatch, tmp_path, rc_enabled=False)
    html_resp = client.get("/rc")
    assert html_resp.status_code == 503
    assert "Remote Control is disabled" in html_resp.text
    assert client.get("/rc/api/me").status_code == 503


def test_pairing_cookie_csrf_revocation_and_rate_limit(
    monkeypatch, tmp_path: Path
) -> None:
    client, hub = _build_rc_app(monkeypatch, tmp_path, rc_enabled=True)
    pair_data = client.post(
        "/admin/api/rc/pair-token", json={"label": "Pixel 9"}
    ).json()
    token = pair_data["token"]
    assert "?pair=" in pair_data["pair_url"]
    assert "<svg" in pair_data["qr_svg"]
    assert pair_data["expires_in_seconds"] == 60

    # Redeem single-use token
    redeem = client.post("/rc/api/pair", json={"token": token, "label": "My Phone"})
    assert redeem.status_code == 200
    body = redeem.json()
    assert body["authenticated"] is True
    device_id = body["device"]["device_id"]
    assert body["csrf_token"]
    assert "mcc_rc_session" in redeem.cookies
    assert "mcc_rc_csrf" in redeem.cookies

    # Replay of same single-use token must fail
    replay = client.post("/rc/api/pair", json={"token": token})
    assert replay.status_code == 401

    # Read succeeds with cookie
    assert client.get("/rc/api/sessions").status_code == 200

    # Write without CSRF fails 403
    no_csrf = client.post(
        "/rc/api/sessions",
        json={"harness": "terminal", "cwd": str(tmp_path / "work")},
    )
    assert no_csrf.status_code == 403

    # Write with wrong CSRF fails 403
    bad_csrf = client.post(
        "/rc/api/sessions",
        headers={"X-MCC-RC-CSRF": "wrong-csrf"},
        json={"harness": "terminal", "cwd": str(tmp_path / "work")},
    )
    assert bad_csrf.status_code == 403

    # Revoking device immediately invalidates cookie
    rev = client.post(f"/admin/api/rc/devices/{device_id}/revoke")
    assert rev.status_code == 200
    assert rev.json()["revoked"] is True
    assert client.get("/rc/api/sessions").status_code == 401

    # Expired token cannot be redeemed
    assert isinstance(client.app, FastAPI)
    auth = client.app.state.rc_auth
    expired = auth.issue_pairing_token(label="Expired")
    expired.expires_at_mono = time.monotonic() - 1.0
    assert client.post("/rc/api/pair", json={"token": expired.token}).status_code == 401

    # Brute-force rate limit triggers 429 once cumulative failures reach threshold (8)
    for _ in range(6):
        r = client.post("/rc/api/pair", json={"token": "bad-token-guess"})
        assert r.status_code == 401
    rate_limited = client.post("/rc/api/pair", json={"token": "bad-token-guess"})
    assert rate_limited.status_code == 429
    assert hub.list_devices() == []


def test_websocket_origin_rejects_cross_origin(monkeypatch, tmp_path: Path) -> None:
    client, _hub = _build_rc_app(monkeypatch, tmp_path, rc_enabled=True)
    tok = client.post("/admin/api/rc/pair-token", json={}).json()["token"]
    pair = client.post("/rc/api/pair", json={"token": tok}).json()
    csrf = pair["csrf_token"]
    sess = client.post(
        "/rc/api/sessions",
        headers={"X-MCC-RC-CSRF": csrf},
        json={"harness": "terminal", "cwd": str(tmp_path / "work")},
    ).json()["session"]

    try:
        with client.websocket_connect(
            f"/rc/ws/sessions/{sess['id']}",
            headers={"Origin": "https://evil.example"},
        ) as _ws:
            raise AssertionError("Cross-origin WebSocket should have been rejected")
    except Exception:
        pass
    finally:
        client.post(
            f"/rc/api/sessions/{sess['id']}/stop",
            headers={"X-MCC-RC-CSRF": csrf},
        )
