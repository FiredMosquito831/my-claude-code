"""Tests for ``mcc-apps mcp-*`` CLI subcommands."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_mcp_media_routes
from my_claude_code.cli import apps_command as module
from my_claude_code.config import mcp_media, mcp_permanent_apply, paths
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app


@pytest.fixture
def mcp_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
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
    settings = Settings.model_validate(
        {
            "ANTHROPIC_AUTH_TOKEN": "proxy-secret-token",
            "XAI_API_KEY": "xai-" + "a" * 40,
            "MODEL_IMAGE": "xai/grok-2-image",
        }
    )

    with (
        patch.object(admin_mcp_media_routes, "mcp_media_command", return_value=cmd),
        patch.object(mcp_media, "mcp_media_command", return_value=cmd),
        patch.object(mcp_permanent_apply, "mcp_media_command", return_value=cmd),
    ):
        app = create_test_app(settings)
        client = TestClient(app, client=("127.0.0.1", 50000))

        def call(method: str, path: str, payload=None):
            headers = {"origin": "http://127.0.0.1:8082"}
            if method == "GET":
                response = client.get(path, headers=headers)
            else:
                response = client.post(path, json=payload or {}, headers=headers)
            if response.status_code >= 400:
                raise module.AppsCommandError(response.json().get("detail", ""))
            return response.json()

        monkeypatch.setattr(module, "_call", call)
        with client:
            yield home


def test_mcp_list_status_mode_preview_apply_and_remove_restore(
    mcp_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module.apps_command(["mcp-list"])
    listed = capsys.readouterr().out
    assert "claude" in listed
    assert "codex" in listed

    module.apps_command(["mcp-status", "claude"])
    status = capsys.readouterr().out
    assert "Claude Code (claude)" in status
    assert "mode" in status and "off" in status

    # Preview permanent writes nothing.
    module.apps_command(["mcp-mode", "claude", "permanent", "--preview"])
    preview = capsys.readouterr().out
    assert '"mcc_media"' in preview
    assert not (mcp_cli / ".claude.json").exists()

    # Apply permanent writes ~/.claude.json.
    module.apps_command(["mcp-mode", "claude", "permanent"])
    applied = capsys.readouterr().out
    assert "permanent" in applied.lower()
    assert json.loads((mcp_cli / ".claude.json").read_text(encoding="utf-8"))[
        "mcpServers"
    ]["mcc_media"]

    # Preview remove then remove & restore.
    module.apps_command(["mcp-remove", "claude", "--preview", "--restore"])
    rem_preview = capsys.readouterr().out
    assert "mcc_media" in rem_preview
    assert (mcp_cli / ".claude.json").exists()

    module.apps_command(["mcp-remove", "claude", "--restore"])
    assert not (mcp_cli / ".claude.json").exists()
