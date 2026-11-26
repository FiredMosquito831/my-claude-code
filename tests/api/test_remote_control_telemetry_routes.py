"""API route tests for /rc telemetry, proxy quick-controls, and Web Push (PR 19)."""

import base64
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.config.model_overrides import current_model_overrides
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from my_claude_code.runtime.remote import claude_driver
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub


def _seed_requests_db(db_path: Path, *, session_id: str, project_dir: str) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                ts_epoch REAL NOT NULL,
                endpoint TEXT,
                protocol TEXT,
                requested_model TEXT,
                provider TEXT,
                resolved_model TEXT,
                route_attempt INTEGER,
                tokens_in INTEGER,
                tokens_out INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                ttft_ms REAL,
                duration_ms REAL,
                status TEXT,
                status_code INTEGER,
                error_kind TEXT,
                error_message TEXT,
                cost_usd REAL,
                estimated_cost_usd REAL,
                cost_source TEXT,
                session_id TEXT,
                project_dir TEXT,
                params TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO requests (
                id, ts_epoch, endpoint, protocol, requested_model, provider,
                resolved_model, route_attempt, tokens_in, tokens_out,
                cache_read_tokens, cache_write_tokens, reasoning_tokens,
                ttft_ms, duration_ms, status, status_code, cost_usd,
                estimated_cost_usd, cost_source, session_id, project_dir, params
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "req_route_1",
                5000.0,
                "/v1/messages",
                "anthropic",
                "claude-sonnet-4-6",
                "openai",
                "openai/gpt-5.4",
                0,
                300,
                75,
                20,
                0,
                0,
                142.0,
                400.0,
                "success",
                200,
                0.009,
                None,
                "provider",
                session_id,
                project_dir,
                json.dumps({"wire": {"surface": "responses"}}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _setup_app(
    monkeypatch, tmp_path: Path
) -> Iterator[tuple[TestClient, str, Path, Path, RemoteRuntimeHub]]:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    allowed = tmp_path / "allowed"
    (allowed / "proj").mkdir(parents=True, exist_ok=True)

    settings = Settings.model_validate(
        {
            "RC_ENABLED": True,
            "RC_ALLOWED_DIRS": str(allowed),
            "RC_SESSION_SECRET": "telemetry-route-secret-0123456789",
        }
    )
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    hub = RemoteRuntimeHub(
        settings=settings, messaging_target_provider=runtime.rc_messaging_targets
    )
    app = create_app(
        ApiServices(
            requests=manager,
            admin=runtime,
            tasks=runtime,
            media=None,
            remote=hub,
        )
    )
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            tok = client.post("/admin/api/rc/pair-token", json={}).json()["token"]
            pair = client.post("/rc/api/pair", json={"token": tok}).json()
            yield client, pair["csrf_token"], cfg_dir, allowed, hub
    finally:
        hub._store.close()


def test_telemetry_catalog_proxy_controls_and_push_routes(
    monkeypatch, tmp_path: Path
) -> None:
    fake_script = tmp_path / "fake_claude.py"
    fake_script.write_text(
        "\n".join(
            [
                "import json, sys",
                "print(json.dumps({'type':'system','subtype':'init','session_id':'ext-tel-1','model':'claude-sonnet-4-6'}), flush=True)",
                "while True:",
                "    raw = sys.stdin.readline()",
                "    if not raw: break",
                "    line = raw.strip()",
                "    if not line: continue",
                "    msg = json.loads(line)",
                "    if msg.get('type') == 'user':",
                "        print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'ok'}]}}), flush=True)",
                "        print(json.dumps({'type':'result','subtype':'success','result':'ok'}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_driver,
        "build_claude_stream_argv",
        lambda **kwargs: [sys.executable, "-u", str(fake_script)],
    )
    with _setup_app(monkeypatch, tmp_path) as (
        client,
        csrf,
        cfg_dir,
        allowed,
        hub,
    ):
        headers = {"X-MCC-RC-CSRF": csrf}
        created = client.post(
            "/rc/api/sessions",
            json={
                "harness": "claude",
                "cwd": str(allowed / "proj"),
                "permission_mode": "ask",
            },
            headers=headers,
        )
        assert created.status_code == 200
        sess_id = created.json()["session"]["id"]
        try:
            _seed_requests_db(
                cfg_dir / "logs" / "requests.db",
                session_id=sess_id,
                project_dir=str(allowed / "proj"),
            )

            # Telemetry route
            tel_resp = client.get(f"/rc/api/sessions/{sess_id}/telemetry")
            assert tel_resp.status_code == 200
            tel = tel_resp.json()
            assert tel["session_id"] == sess_id
            assert tel["request_count"] == 1
            assert abs(tel["reported_cost_usd"] - 0.009) < 1e-9
            assert tel["input_tokens"] == 300
            assert tel["last_request"]["provider_id"] == "openai"

            assert (
                client.get("/rc/api/sessions/does_not_exist/telemetry").status_code
                == 404
            )

            # Catalog route
            cat_resp = client.get("/rc/api/catalog")
            assert cat_resp.status_code == 200
            cat = cat_resp.json()
            assert isinstance(cat["tiers"], list) and len(cat["tiers"]) >= 1
            assert isinstance(cat["multi_door_providers"], list)
            assert "high" in cat["reasoning_efforts"]

            # Proxy quick-controls: CSRF enforcement + session + global preferred surface
            no_csrf = client.post(
                f"/rc/api/sessions/{sess_id}/proxy-controls",
                json={"model": "sonnet", "reasoning_effort": "high"},
            )
            assert no_csrf.status_code == 403

            ctrl_resp = client.post(
                f"/rc/api/sessions/{sess_id}/proxy-controls",
                json={
                    "model": "sonnet",
                    "reasoning_effort": "high",
                    "preferred_surface": "responses",
                    "preferred_surface_scope": "provider",
                    "preferred_surface_key": "opencode",
                },
                headers=headers,
            )
            assert ctrl_resp.status_code == 200
            ctrl_body = ctrl_resp.json()
            assert ctrl_body["session"]["model"] == "sonnet"
            assert ctrl_body["session"]["reasoning_effort"] == "high"
            assert ctrl_body["preferred_surface"]["global"] is True
            assert ctrl_body["preferred_surface"]["surface"] == "responses"
            assert (
                current_model_overrides().preferred_surface_detail(
                    "opencode", "opencode"
                )[0]
                == "responses"
            )

            bad_surface = client.post(
                f"/rc/api/sessions/{sess_id}/proxy-controls",
                json={
                    "preferred_surface": "bogus_wire_surface",
                    "preferred_surface_scope": "provider",
                    "preferred_surface_key": "opencode",
                },
                headers=headers,
            )
            assert bad_surface.status_code == 422

            # Web Push VAPID public key + subscribe/unsubscribe/test
            vapid_resp = client.get("/rc/api/push/vapid-public-key")
            assert vapid_resp.status_code == 200
            pub_key = vapid_resp.json()["public_key"]
            raw_pub = base64.urlsafe_b64decode(pub_key + "=" * (-len(pub_key) % 4))
            assert len(raw_pub) == 65 and raw_pub[0] == 0x04

            sub_payload = {
                "endpoint": "https://push.example/sub/device-1",
                "keys": {
                    "p256dh": pub_key,
                    "auth": base64.urlsafe_b64encode(os.urandom(16))
                    .rstrip(b"=")
                    .decode("ascii"),
                },
            }
            assert (
                client.post("/rc/api/push/subscribe", json=sub_payload).status_code
                == 403
            )
            sub_ok = client.post(
                "/rc/api/push/subscribe", json=sub_payload, headers=headers
            )
            assert sub_ok.status_code == 200
            assert sub_ok.json()["subscribed"] is True
            assert len(hub._store.list_push_subscriptions()) == 1

            # Deleting subscription works via JSON body or query parameter
            del_ok = client.delete(
                "/rc/api/push/subscribe",
                params={"endpoint": "https://push.example/sub/device-1"},
                headers=headers,
            )
            assert del_ok.status_code == 200
            assert del_ok.json()["unsubscribed"] is True
            assert hub._store.list_push_subscriptions() == []

            test_push = client.post("/rc/api/push/test", headers=headers)
            assert test_push.status_code == 200
            assert test_push.json()["subscriptions"] == 0
        finally:
            stopped = client.post(f"/rc/api/sessions/{sess_id}/stop", headers=headers)
            assert stopped.status_code == 200
            deleted = client.delete(f"/rc/api/sessions/{sess_id}", headers=headers)
            assert deleted.status_code == 200
