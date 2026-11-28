"""Tests for PR 39 admin MCP mode switching and permanent config endpoints."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_mcp_media_routes
from my_claude_code.config import mcp_media, mcp_permanent_apply, paths
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

TOKEN = "proxy-secret-token"


@pytest.fixture
def env_and_cmd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[Path, str]]:
    home = tmp_path / "home"
    home.mkdir()
    mcc_dir = tmp_path / "mcc"
    mcc_dir.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(mcc_dir))
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    paths.reset_config_dir_cache()

    cmd = str(tmp_path / "bin" / "mcc-mcp")
    with (
        patch.object(admin_mcp_media_routes, "mcp_media_command", return_value=cmd),
        patch.object(mcp_media, "mcp_media_command", return_value=cmd),
        patch.object(mcp_permanent_apply, "mcp_media_command", return_value=cmd),
    ):
        yield home, cmd


def _client() -> TestClient:
    settings = Settings.model_validate(
        {
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            "XAI_API_KEY": "xai-" + "a" * 40,
            "MODEL_IMAGE": "xai/grok-2-image",
        }
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _app_map(payload: dict) -> dict[str, dict]:
    return {app["app_id"]: app for app in payload["apps"]}


def test_mcp_media_mode_switching_mutual_exclusion_and_permanent_routes(
    env_and_cmd: tuple[Path, str],
) -> None:
    home, cmd = env_and_cmd
    headers = {"origin": "http://127.0.0.1:8082"}

    with _client() as client:
        initial = client.get("/admin/api/mcp-media").json()
        apps = _app_map(initial)
        assert apps["claude"]["mode"] == "off"
        assert apps["claude"]["permanent_supported"] is True
        assert apps["gemini_cli"]["permanent_supported"] is True
        assert apps["kimi_code"]["permanent_supported"] is False
        assert any(row["target_id"] == "claude" for row in initial["permanent_targets"])

        # Switch claude -> launch.
        r1 = client.post(
            "/admin/api/mcp-media/mode",
            json={"app": "claude", "mode": "launch"},
            headers=headers,
        )
        assert r1.status_code == 200
        assert _app_map(r1.json())["claude"]["mode"] == "launch"
        assert r1.json()["apps_value"] == "claude"

        # Plan permanent first -> shows diff without mutating.
        plan = client.post(
            "/admin/api/mcp-media/permanent/claude/plan",
            json={"add": True},
            headers=headers,
        )
        assert plan.status_code == 200
        assert '"mcc_media"' in plan.json()["diff"]
        assert not (home / ".claude.json").exists()

        # Switch claude -> permanent: writes ~/.claude.json AND clears claude from MCP_MEDIA_APPS.
        r2 = client.post(
            "/admin/api/mcp-media/mode",
            json={"app": "claude", "mode": "permanent"},
            headers=headers,
        )
        assert r2.status_code == 200
        claude_after = _app_map(r2.json())["claude"]
        assert claude_after["mode"] == "permanent"
        assert claude_after["permanent_state"] == "configured"
        assert claude_after["on"] is False
        assert r2.json()["apps_value"] == ""
        written = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        assert written["mcpServers"]["mcc_media"]["command"] == cmd

        # Switch claude -> off (restore): removes ~/.claude.json entry cleanly.
        r3 = client.post(
            "/admin/api/mcp-media/mode",
            json={"app": "claude", "mode": "off", "undo_mode": "restore"},
            headers=headers,
        )
        assert r3.status_code == 200
        assert _app_map(r3.json())["claude"]["mode"] == "off"
        assert not (home / ".claude.json").exists()


def test_foreign_permanent_entry_returns_409_and_requires_loopback_origin(
    env_and_cmd: tuple[Path, str],
) -> None:
    home, _cmd = env_and_cmd
    qwen_cfg = home / ".qwen" / "settings.json"
    qwen_cfg.parent.mkdir(parents=True)
    raw = json.dumps({"mcpServers": {"mcc_media": {"command": "/other/mcp"}}})
    qwen_cfg.write_text(raw, encoding="utf-8")

    with _client() as client:
        # Cross-origin rejected by loopback admin guard.
        bad_origin = client.post(
            "/admin/api/mcp-media/mode",
            json={"app": "qwen_code", "mode": "permanent"},
            headers={"origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403

        conflict = client.post(
            "/admin/api/mcp-media/permanent/qwen_code/apply",
            json={"add": True},
            headers={"origin": "http://127.0.0.1:8082"},
        )
        assert conflict.status_code == 409

        mode_conflict = client.post(
            "/admin/api/mcp-media/mode",
            json={"app": "qwen_code", "mode": "permanent"},
            headers={"origin": "http://127.0.0.1:8082"},
        )
        assert mode_conflict.status_code == 409
        assert qwen_cfg.read_text(encoding="utf-8") == raw
