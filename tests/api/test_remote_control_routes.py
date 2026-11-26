"""REST, static asset hygiene, sandbox, and WebSocket replay tests for /rc."""

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.app import create_app
from my_claude_code.api.ports import ApiServices
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from my_claude_code.runtime.remote import claude_driver
from my_claude_code.runtime.remote.hub import RemoteRuntimeHub


@contextmanager
def _pair_client(
    monkeypatch, tmp_path: Path, *, max_sessions: int = 8
) -> Iterator[tuple[TestClient, str, Path, RemoteRuntimeHub]]:
    cfg_dir = tmp_path / "mcc_home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(cfg_dir))
    allowed = tmp_path / "allowed"
    (allowed / "proj").mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    settings = Settings.model_validate(
        {
            "RC_ENABLED": True,
            "RC_ALLOWED_DIRS": str(allowed),
            "RC_MAX_SESSIONS": max_sessions,
            "RC_SESSION_SECRET": "route-test-secret-0123456789",
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
            yield client, pair["csrf_token"], allowed, hub
    finally:
        hub._store.close()


def test_rc_static_and_vendored_assets_have_zero_cdns(
    monkeypatch, tmp_path: Path
) -> None:
    with _pair_client(monkeypatch, tmp_path) as (client, _csrf, _allowed, _hub):
        for asset_path in (
            "/rc",
            "/rc/manifest.webmanifest",
            "/rc/sw.js",
            "/rc/assets/remote.css",
            "/rc/assets/remote.js",
            "/rc/assets/vendor/xterm.js",
            "/rc/assets/vendor/xterm.css",
            "/rc/assets/vendor/addon-fit.js",
        ):
            resp = client.get(asset_path)
            assert resp.status_code == 200, asset_path
            if asset_path == "/rc":
                lower = resp.text.lower()
                assert "https://" not in lower
                assert "unpkg.com" not in lower
                assert "jsdelivr.net" not in lower


def test_allowed_dirs_blocks_traversal_and_symlinks(
    monkeypatch, tmp_path: Path
) -> None:
    with _pair_client(monkeypatch, tmp_path) as (client, csrf, allowed, _hub):
        outside = tmp_path / "outside"

        dirs = client.get("/rc/api/fs/dirs")
        assert dirs.status_code == 200
        assert dirs.json()["roots"] == [str(allowed.resolve())]

        assert client.get(f"/rc/api/fs/dirs?path={outside}").status_code == 403

        escape_attempt = allowed / "proj" / ".." / ".." / "outside"
        resp = client.post(
            "/rc/api/sessions",
            headers={"X-MCC-RC-CSRF": csrf},
            json={"harness": "terminal", "cwd": str(escape_attempt)},
        )
        assert resp.status_code == 403

        link_path = allowed / "proj" / "link_outside"
        try:
            os.symlink(outside, link_path, target_is_directory=True)
        except OSError:
            pytest.skip("Symlink creation requires developer privilege on this host")
        link_resp = client.post(
            "/rc/api/sessions",
            headers={"X-MCC-RC-CSRF": csrf},
            json={"harness": "terminal", "cwd": str(link_path)},
        )
        assert link_resp.status_code == 403


def test_session_lifecycle_git_worktree_and_max_limit(
    monkeypatch, tmp_path: Path
) -> None:
    with _pair_client(monkeypatch, tmp_path, max_sessions=1) as (
        client,
        csrf,
        allowed,
        _hub,
    ):
        repo = allowed / "proj"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "rc@test.local"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "RC Test"],
            check=True,
        )
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        fake_script = tmp_path / "fake_claude.py"
        fake_script.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "print(json.dumps({'type':'system','subtype':'init','session_id':'ext-1','model':'fake-sonnet'}), flush=True)",
                    "while True:",
                    "    raw = sys.stdin.readline()",
                    "    if not raw: break",
                    "    line = raw.strip()",
                    "    if not line: continue",
                    "    msg = json.loads(line)",
                    "    if msg.get('type') == 'user':",
                    "        txt = msg['message']['content']",
                    "        print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'echo:' + txt}]}}), flush=True)",
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

        created = client.post(
            "/rc/api/sessions",
            headers={"X-MCC-RC-CSRF": csrf},
            json={
                "harness": "claude",
                "cwd": str(repo),
                "create_worktree": True,
                "prompt": "ping",
            },
        )
        assert created.status_code == 200
        sess = created.json()["session"]
        assert sess["branch_name"].startswith("rc/")
        assert Path(sess["worktree_path"]).is_dir()

        # Second active session exceeds max_sessions=1
        second = client.post(
            "/rc/api/sessions",
            headers={"X-MCC-RC-CSRF": csrf},
            json={"harness": "terminal", "cwd": str(repo)},
        )
        assert second.status_code == 429

        # WebSocket replays persisted events since_seq=0
        with client.websocket_connect(
            f"/rc/ws/sessions/{sess['id']}?since_seq=0"
        ) as ws:
            seen = []
            for _ in range(10):
                frame = json.loads(ws.receive_text())
                seen.append(frame["kind"])
                if frame["kind"] == "turn.completed":
                    break
            assert "message.user" in seen
            assert "turn.completed" in seen

        diff = client.get(f"/rc/api/sessions/{sess['id']}/diff").json()
        assert diff["is_git"] is True
        assert diff["branch"] == sess["branch_name"]

        stopped = client.post(
            f"/rc/api/sessions/{sess['id']}/stop",
            headers={"X-MCC-RC-CSRF": csrf},
        )
        assert stopped.status_code == 200
        assert stopped.json()["session"]["status"] == "stopped"

        deleted = client.delete(
            f"/rc/api/sessions/{sess['id']}",
            headers={"X-MCC-RC-CSRF": csrf},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
