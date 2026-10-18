"""``/admin/api/mcp-media``: the MCC media server card and each card's button.

7.99.0, decision 10 of 2026-10-18 and the 12:53 amendment: one explicit Add
MCP server / Remove MCP server button per agent card, writing one setting
(``MCP_MEDIA_APPS``) inside the config lock; the card shows whether
``mcc-mcp`` is installed, the tools the rails give, what they cost and a line
to copy. Nothing in either answer carries a token.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_mcp_media_routes
from my_claude_code.config import mcp_media
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

TOKEN = "proxy-secret-token"


@pytest.fixture
def command(tmp_path: Path):
    path = str(tmp_path / "Scripts" / "mcc-mcp.exe")
    with (
        patch.object(admin_mcp_media_routes, "mcp_media_command", return_value=path),
        patch.object(mcp_media, "mcp_media_command", return_value=path),
    ):
        yield path


def _client(monkeypatch, tmp_path: Path, **values: str) -> TestClient:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    settings = Settings.model_validate(
        {
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            "XAI_API_KEY": "xai-" + "a" * 40,
            "MODEL_IMAGE": "xai/grok-2-image",
            **values,
        }
    )
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _apps(payload: dict) -> dict[str, dict]:
    return {app["app_id"]: app for app in payload["apps"]}


def test_the_card_names_the_program_the_tools_and_their_cost(
    monkeypatch, tmp_path: Path, command: str
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        payload = client.get("/admin/api/mcp-media").json()

    assert payload["installed"] is True
    assert payload["command"] == command
    assert payload["tools"][:2] == ["media_status", "generate_image"]
    assert isinstance(payload["tool_bytes"], int) and payload["tool_bytes"] > 0
    image = next(rail for rail in payload["rails"] if rail["rail"] == "image")
    assert image["models"] == ["xai/grok-2-image"]
    assert "generate_image" in image["tools"]
    assert payload["copy_line"] == f"{json.dumps(command)} --app <your-app>"
    # Off for every app until a button is pressed.
    assert not any(app["on"] for app in payload["apps"])
    assert _apps(payload)["aider"]["support"] == "no_mcp"
    assert _apps(payload)["kimi_code"]["support"] == "not_measured"
    assert _apps(payload)["pi"]["support"] == "extension_later"
    assert TOKEN not in json.dumps(payload)


def test_add_then_remove_writes_one_setting_and_cleans_up(
    monkeypatch, tmp_path: Path, command: str
) -> None:
    claude_file = tmp_path / "mcp-claude.json"
    with _client(monkeypatch, tmp_path) as client:
        added = client.post(
            "/admin/api/mcp-media/apps", json={"app": "claude", "add": True}
        )
        assert added.status_code == 200, added.text
        assert added.json()["apps_value"] == "claude"
        assert _apps(added.json())["claude"]["on"] is True
        both = client.post(
            "/admin/api/mcp-media/apps", json={"app": "codex", "add": True}
        ).json()
        assert both["apps_value"] == "claude,codex"

        # The launcher wrote MCC's own --mcp-config file meanwhile.
        claude_file.write_text("{}", encoding="utf-8")
        removed = client.post(
            "/admin/api/mcp-media/apps", json={"app": "claude", "add": False}
        ).json()

    assert removed["apps_value"] == "codex"
    assert removed["message"].startswith("Removed from Claude Code.")
    assert not claude_file.exists()
    assert TOKEN not in json.dumps(removed)


def test_an_app_without_a_button_is_refused(monkeypatch, tmp_path: Path, command: str):
    with _client(monkeypatch, tmp_path) as client:
        for app in ("aider", "pi", "kimi_code", "not-an-app"):
            response = client.post(
                "/admin/api/mcp-media/apps", json={"app": app, "add": True}
            )
            assert response.status_code == 400, app


def test_add_without_mcc_mcp_installed_is_refused(monkeypatch, tmp_path: Path) -> None:
    with (
        patch.object(admin_mcp_media_routes, "mcp_media_command", return_value=None),
        _client(monkeypatch, tmp_path) as client,
    ):
        response = client.post(
            "/admin/api/mcp-media/apps", json={"app": "codex", "add": True}
        )
        status = client.get("/admin/api/mcp-media").json()

    assert response.status_code == 409
    assert "not installed" in response.json()["detail"]
    assert status["installed"] is False


def test_claude_desktop_unconfigured_says_what_is_left_to_do(
    monkeypatch, tmp_path: Path, command: str
) -> None:
    with _client(monkeypatch, tmp_path) as client:
        payload = client.post(
            "/admin/api/mcp-media/apps", json={"app": "claude_desktop", "add": True}
        ).json()

    assert "press Configure on the Claude Desktop card" in payload["message"]
    assert _apps(payload)["claude_desktop"]["pending"].startswith("Not written yet")


def test_the_routes_are_loopback_only(monkeypatch, tmp_path: Path, command: str):
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    app = create_test_app(Settings.model_validate({}))
    with TestClient(app, client=("203.0.113.9", 50000)) as client:
        assert client.get("/admin/api/mcp-media").status_code == 403
        assert (
            client.post(
                "/admin/api/mcp-media/apps", json={"app": "codex", "add": True}
            ).status_code
            == 403
        )
