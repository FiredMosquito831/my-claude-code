"""Tests for Unified Session Discovery Scanner and /rc/api/sessions/import."""

import json
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.config.paths import request_log_path, reset_config_dir_cache
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from my_claude_code.runtime.remote import codex_driver
from my_claude_code.runtime.remote.discovery import discover_local_sessions
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub


def _seed_fake_stores(tmp_path: Path, allowed_proj: Path, outside_proj: Path) -> Path:
    fake_home = tmp_path / "fake_home"

    # 1. Claude Code transcripts (~/.claude/projects/*/*.jsonl)
    claude_dir = fake_home / ".claude" / "projects" / "proj-a"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "11111111-1111-1111-1111-111111111111",
                        "cwd": str(allowed_proj),
                        "timestamp": "2026-11-26T12:00:00Z",
                        "message": {"role": "user", "content": "Fix auth flow"},
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    claude_out = fake_home / ".claude" / "projects" / "outside"
    claude_out.mkdir(parents=True, exist_ok=True)
    (claude_out / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "22222222-2222-2222-2222-222222222222",
                "cwd": str(outside_proj),
                "message": {"role": "user", "content": "Secret outside repo"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Corrupt file resilience
    (claude_dir / "corrupt.jsonl").write_text("{not-json\n", encoding="utf-8")

    # 2. OpenCode SQLite (opencode.db) + Kilo JSON storage
    oc_dir = fake_home / ".local" / "share" / "opencode"
    oc_dir.mkdir(parents=True, exist_ok=True)
    oc_conn = sqlite3.connect(str(oc_dir / "opencode.db"))
    oc_conn.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_updated INTEGER)"
    )
    oc_conn.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?)",
        ("oc_sess_9", "OpenCode refactor", str(allowed_proj), int(time.time() * 1000)),
    )
    oc_conn.commit()
    oc_conn.close()

    kilo_store = fake_home / ".local" / "share" / "kilo" / "storage" / "session" / "p1"
    kilo_store.mkdir(parents=True, exist_ok=True)
    (kilo_store / "kilo_1.json").write_text(
        json.dumps(
            {
                "id": "kilo_1",
                "title": "Kilo lint fix",
                "directory": str(allowed_proj),
                "time": {"updated": time.time()},
            }
        ),
        encoding="utf-8",
    )

    # 3. Codex rollout JSONL
    codex_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "26"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "rollout-2026-11-26-abc.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "thr_codex_9",
                            "cwd": str(allowed_proj),
                            "model": "gpt-5.4",
                            "timestamp": "2026-11-26T12:05:00Z",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"message": "Write unit tests"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # 4. Hermes state.db
    hermes_dir = fake_home / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    h_conn = sqlite3.connect(str(hermes_dir / "state.db"))
    h_conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, model TEXT, updated_at REAL)"
    )
    h_conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        ("hermes_1", "Hermes task", str(allowed_proj), "mcc/best", time.time()),
    )
    h_conn.commit()
    h_conn.close()

    # 5. OpenClaw sessions.json
    claw_dir = fake_home / ".openclaw" / "agents" / "main" / "sessions"
    claw_dir.mkdir(parents=True, exist_ok=True)
    (claw_dir / "sessions.json").write_text(
        json.dumps(
            {
                "agent:main:main": {
                    "sessionId": "claw-uuid-1",
                    "displayName": "OpenClaw chat",
                    "cwd": str(allowed_proj),
                    "model": "mcc/good",
                    "updatedAt": int(time.time() * 1000),
                }
            }
        ),
        encoding="utf-8",
    )

    # 6. MCC requests.db correlation
    req_db = request_log_path()
    req_db.parent.mkdir(parents=True, exist_ok=True)
    r_conn = sqlite3.connect(str(req_db))
    r_conn.execute(
        "CREATE TABLE requests (id TEXT PRIMARY KEY, ts_epoch REAL, harness TEXT, session_id TEXT, project_dir TEXT, resolved_model TEXT)"
    )
    r_conn.execute(
        "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
        (
            "req_1",
            time.time(),
            "gemini_cli",
            "gem_sess_1",
            str(allowed_proj),
            "gemini-2.5-pro",
        ),
    )
    r_conn.commit()
    r_conn.close()

    return fake_home


@contextmanager
def _rc_client(
    monkeypatch, tmp_path: Path
) -> Iterator[tuple[TestClient, str, Path, Path, Path]]:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    allowed = tmp_path / "allowed"
    allowed_proj = allowed / "proj"
    allowed_proj.mkdir(parents=True, exist_ok=True)
    outside_proj = tmp_path / "outside"
    outside_proj.mkdir(parents=True, exist_ok=True)

    fake_home = _seed_fake_stores(tmp_path, allowed_proj, outside_proj)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(fake_home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "AppData" / "Local"))

    settings = Settings.model_validate(
        {
            "RC_ENABLED": True,
            "RC_ALLOWED_DIRS": str(allowed),
            "RC_SESSION_SECRET": "discovery-test-secret-0123456789",
        }
    )
    manager = ProviderRuntimeManager(settings)
    runtime = ApplicationRuntime(manager, transcriber=None)
    hub = RemoteRuntimeHub(settings=settings)
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
            yield client, pair["csrf_token"], allowed_proj, outside_proj, fake_home
    finally:
        hub._store.close()


def test_discovery_scanner_parses_all_sources_and_flags_outside_dirs(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    reset_config_dir_cache()

    allowed = tmp_path / "allowed"
    allowed_proj = allowed / "proj"
    allowed_proj.mkdir(parents=True, exist_ok=True)
    outside_proj = tmp_path / "outside"
    outside_proj.mkdir(parents=True, exist_ok=True)
    fake_home = _seed_fake_stores(tmp_path, allowed_proj, outside_proj)

    items = discover_local_sessions(
        [allowed.resolve()],
        env={
            "XDG_DATA_HOME": str(fake_home / ".local" / "share"),
            "CODEX_HOME": str(fake_home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(fake_home / ".claude"),
            "HERMES_HOME": str(fake_home / ".hermes"),
            "OPENCLAW_HOME": str(fake_home),
        },
        home=fake_home,
    )
    by_id = {
        (item["source_harness"], item["external_session_id"]): item for item in items
    }

    assert ("claude", "11111111-1111-1111-1111-111111111111") in by_id
    assert (
        by_id[("claude", "11111111-1111-1111-1111-111111111111")]["within_allowed_dirs"]
        is True
    )
    assert (
        by_id[("claude", "11111111-1111-1111-1111-111111111111")]["title"]
        == "Fix auth flow"
    )

    assert ("claude", "22222222-2222-2222-2222-222222222222") in by_id
    assert (
        by_id[("claude", "22222222-2222-2222-2222-222222222222")]["within_allowed_dirs"]
        is False
    )

    assert ("opencode", "oc_sess_9") in by_id
    assert ("kilo", "kilo_1") in by_id
    assert ("codex", "thr_codex_9") in by_id
    assert by_id[("codex", "thr_codex_9")]["model"] == "gpt-5.4"
    assert ("hermes", "hermes_1") in by_id
    assert ("openclaw", "agent:main:main") in by_id
    assert ("gemini_cli", "gem_sess_1") in by_id


def test_discovered_route_harnesses_and_import_enforces_allowed_dirs(
    monkeypatch, tmp_path: Path
) -> None:
    with _rc_client(monkeypatch, tmp_path) as (
        client,
        csrf,
        allowed_proj,
        outside_proj,
        _fake_home,
    ):
        harnesses = {
            h["id"]: h for h in client.get("/rc/api/harnesses").json()["harnesses"]
        }
        for tid in ("opencode", "opencode2", "kilo", "codex", "openclaw"):
            assert harnesses[tid]["supports_structured"] is True
            assert harnesses[tid]["driver_tier"].startswith("tier_1a_")
        assert harnesses["claude"]["driver_tier"] == "tier_1b_claude"

        disc = client.get("/rc/api/discovered-sessions?harness=codex")
        assert disc.status_code == 200
        codex_items = disc.json()["sessions"]
        assert any(s["external_session_id"] == "thr_codex_9" for s in codex_items)

        # Importing outside RC_ALLOWED_DIRS MUST be rejected 403!
        blocked = client.post(
            "/rc/api/sessions/import",
            headers={"X-MCC-RC-CSRF": csrf},
            json={
                "external_session_id": "22222222-2222-2222-2222-222222222222",
                "harness": "claude",
                "cwd": str(outside_proj),
            },
        )
        assert blocked.status_code == 403

        # Importing an allowed Codex session resumes/forks cleanly with Tier 1A driver
        fake_codex = tmp_path / "fake_codex_import.py"
        fake_codex.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "while True:",
                    "    line = sys.stdin.readline()",
                    "    if not line: break",
                    "    msg = json.loads(line.strip())",
                    "    m = msg.get('method')",
                    "    rid = msg.get('id')",
                    "    if m == 'initialize':",
                    "        print(json.dumps({'id': rid, 'result': {}}), flush=True)",
                    "    elif m in ('thread/resume', 'thread/fork'):",
                    "        tid = msg['params']['threadId'] + ('-forked' if m == 'thread/fork' else '')",
                    "        print(json.dumps({'id': rid, 'result': {'thread': {'id': tid}}}), flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            codex_driver,
            "build_codex_app_server_argv",
            lambda **kwargs: [sys.executable, "-u", str(fake_codex)],
        )

        resumed = client.post(
            "/rc/api/sessions/import",
            headers={"X-MCC-RC-CSRF": csrf},
            json={
                "external_session_id": "thr_codex_9",
                "harness": "codex",
                "cwd": str(allowed_proj),
                "fork_session": True,
            },
        )
        assert resumed.status_code == 200
        sess = resumed.json()["session"]
        assert sess["driver_tier"] == "tier_1a_codex"
        assert sess["external_session_id"] == "thr_codex_9-forked"

        stopped = client.post(
            f"/rc/api/sessions/{sess['id']}/stop",
            headers={"X-MCC-RC-CSRF": csrf},
        )
        assert stopped.status_code == 200
