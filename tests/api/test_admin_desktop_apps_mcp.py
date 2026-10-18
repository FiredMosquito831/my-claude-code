"""The Desktop apps routes for MCC's media server in the user's own files (7.100.0).

``POST /admin/api/desktop-apps/{id}/mcp/plan`` shows what *Add* or *Remove MCP
server* would write and writes nothing; ``POST /admin/api/desktop-apps/{id}/mcp``
writes it. Every test redirects the homes and MCC's config folder into
``tmp_path`` (the helpers of ``test_admin_desktop_apps``), and pins the path of
``mcc-mcp`` so the entry is the same on every machine.
"""

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api import admin_desktop_app_routes, admin_mcp_media_routes
from tests.api.support import create_test_app
from tests.api.test_admin_desktop_apps import (
    CODEX_DOCUMENT,
    _install_codex,
    _local_client,
    _remote_client,
    _scratch_home,
)

TOKEN = "sk-proxy-token-that-must-never-reach-a-file"
COMMAND = "C:\\MCC\\Scripts\\mcc-mcp.exe"


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    scratch = _scratch_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    for module in (admin_desktop_app_routes, admin_mcp_media_routes):
        monkeypatch.setattr(module, "mcp_media_command", lambda: COMMAND)
    return scratch


def _install_commandcode(home: Path) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    executable = home / "bin" / f"commandcode{suffix}"
    executable.write_bytes(b"")
    executable.chmod(0o755)
    return home / ".commandcode" / "mcp.json"


def _card(client: TestClient, app_id: str) -> dict:
    body = client.get("/admin/api/desktop-apps").json()
    return next(entry for entry in body["apps"] if entry["id"] == app_id)


def test_the_list_route_carries_each_cards_media_server_state(home):
    _install_codex(home)
    with _local_client(create_test_app()) as client:
        codex = _card(client, "codex_desktop")
        roo = _card(client, "roo_code")
        crush = _card(client, "crush_desktop")

    assert codex["mcp"]["state"] == "absent"
    assert codex["mcp"]["key"] == "mcp_servers.mcc_media"
    assert codex["mcp"]["display_path"] == "~/.codex/config.toml"
    assert codex["mcp"]["installed"] is True
    assert codex["mcp"]["fallback"] is False
    assert codex["mcp"]["asks"].startswith("Codex asks")
    # Not measured: a dated line and no block; no MCP file at all: nothing.
    assert "mcp" not in roo and roo["mcp_note"].startswith("MCP server: not measured")
    assert "mcp" not in crush and "mcp_note" not in crush


def test_plan_shows_only_the_entry_and_writes_nothing(home):
    path = _install_codex(home)
    with _local_client(create_test_app()) as client:
        plan = client.post(
            "/admin/api/desktop-apps/codex_desktop/mcp/plan", json={"add": True}
        ).json()

    assert path.read_text(encoding="utf-8") == CODEX_DOCUMENT
    assert not path.with_name("config.toml.mcc-backup").exists()
    assert plan["add"] is True and plan["no_op"] is False
    changed = [
        line
        for line in plan["diff"].splitlines()[2:]
        if line[:1] in "+-" and not line.startswith("@@")
    ]
    assert changed[0] == "+"
    assert changed[1] == "+[mcp_servers.mcc_media]"
    assert all(line.startswith("+") for line in changed)
    assert plan["backup_path"].endswith("config.toml.mcc-backup")
    assert plan["backup_exists"] is False
    assert TOKEN not in json.dumps(plan)


def test_add_then_remove_through_the_routes_is_byte_identical(home):
    path = _install_codex(home)
    original = path.read_bytes()
    with _local_client(create_test_app()) as client:
        added = client.post(
            "/admin/api/desktop-apps/codex_desktop/mcp", json={"add": True}
        ).json()
        assert added["changed"] is True
        assert added["backup_path"].endswith("config.toml.mcc-backup")
        assert added["mcp"]["state"] == "added"
        entry = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"][
            "mcc_media"
        ]
        # This server runs with a config folder of its own (the scratch one),
        # so the entry names it: mcc-mcp must read *this* MCC's settings.
        assert entry == {
            "command": COMMAND,
            "args": ["--app", "codex_desktop"],
            "env": {"MCC_CONFIG_DIR": os.environ["MCC_CONFIG_DIR"]},
        }
        assert TOKEN not in path.read_text(encoding="utf-8")

        removed = client.post(
            "/admin/api/desktop-apps/codex_desktop/mcp", json={"add": False}
        ).json()

    assert removed["changed"] is True and removed["mcp"]["state"] == "absent"
    assert path.read_bytes() == original
    assert path.with_name("config.toml.mcc-backup").read_bytes() == original


def test_a_foreign_entry_is_a_409_that_says_whose_it_is(home):
    path = _install_codex(
        home, CODEX_DOCUMENT + '\n[mcp_servers.mcc_media]\ncommand = "mine"\n'
    )
    before = path.read_bytes()
    with _local_client(create_test_app()) as client:
        assert _card(client, "codex_desktop")["mcp"]["state"] == "foreign"
        for route in ("mcp/plan", "mcp"):
            for add in (True, False):
                answer = client.post(
                    f"/admin/api/desktop-apps/codex_desktop/{route}",
                    json={"add": add},
                )
                assert answer.status_code == 409
                assert "did not add" in answer.json()["detail"]
                assert "It is yours" in answer.json()["detail"]
    assert path.read_bytes() == before


def test_undo_takes_the_entry_out_and_says_so(home):
    path = _install_codex(home)
    original = path.read_bytes()
    with _local_client(create_test_app()) as client:
        client.post("/admin/api/desktop-apps/codex_desktop/configure", json={})
        client.post("/admin/api/desktop-apps/codex_desktop/mcp", json={"add": True})
        undone = client.post(
            "/admin/api/desktop-apps/codex_desktop/undo", json={"mode": "restore"}
        ).json()
    assert undone["removed_mcp_server"] is True
    assert path.read_bytes() == original


def test_commandcode_gets_its_own_mcp_json_created_and_deleted(home, monkeypatch):
    # An MCC on its default config folder: the entry passes no environment.
    monkeypatch.delenv("MCC_CONFIG_DIR")
    path = _install_commandcode(home)
    assert not path.exists()
    with _local_client(create_test_app()) as client:
        plan = client.post(
            "/admin/api/desktop-apps/commandcode/mcp/plan", json={"add": True}
        ).json()
        assert plan["creates_file"] is True
        added = client.post(
            "/admin/api/desktop-apps/commandcode/mcp", json={"add": True}
        ).json()
        assert added["created_file"] is True
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "mcpServers": {
                "mcc_media": {
                    "transport": "stdio",
                    "enabled": True,
                    "command": COMMAND,
                    "args": ["--app", "commandcode"],
                }
            }
        }
        removed = client.post(
            "/admin/api/desktop-apps/commandcode/mcp", json={"add": False}
        ).json()
    assert removed["removed_file"] is True
    assert not path.exists()
    assert not path.parent.exists()


def test_add_without_mcc_mcp_is_refused(home, monkeypatch):
    path = _install_codex(home)
    monkeypatch.setattr(admin_desktop_app_routes, "mcp_media_command", lambda: None)
    with _local_client(create_test_app()) as client:
        assert _card(client, "codex_desktop")["mcp"]["installed"] is False
        answer = client.post(
            "/admin/api/desktop-apps/codex_desktop/mcp", json={"add": True}
        )
    assert answer.status_code == 409
    assert "mcc-mcp is not installed" in answer.json()["detail"]
    assert path.read_text(encoding="utf-8") == CODEX_DOCUMENT


def test_a_card_without_an_mcp_file_refuses_and_names_why(home):
    with _local_client(create_test_app()) as client:
        roo = client.post("/admin/api/desktop-apps/roo_code/mcp", json={"add": True})
        warp = client.post("/admin/api/desktop-apps/warp/mcp/plan", json={"add": True})
    assert roo.status_code == 409 and "not measured yet" in roo.json()["detail"]
    assert warp.status_code == 409


def test_the_mcp_routes_are_loopback_only(home):
    _install_codex(home)
    with _remote_client(create_test_app()) as client:
        for route in ("mcp/plan", "mcp"):
            answer = client.post(
                f"/admin/api/desktop-apps/codex_desktop/{route}", json={"add": True}
            )
            assert answer.status_code in (401, 403)


def test_the_media_server_card_lists_the_own_file_apps(home):
    _install_codex(home)
    with _local_client(create_test_app()) as client:
        client.post("/admin/api/desktop-apps/codex_desktop/mcp", json={"add": True})
        body = client.get("/admin/api/mcp-media").json()
    rows = {row["app_id"]: row for row in body["desktop_files"]}
    assert set(rows) == {
        "codex_desktop",
        "opencode_desktop",
        "commandcode",
        "claude_desktop",
    }
    assert rows["codex_desktop"]["state"] == "added"
    assert rows["claude_desktop"]["fallback"] is True
    # The setting the agent buttons write is not touched by a desktop card.
    assert body["apps_value"] in ("", None)
    command_code = next(
        app for app in body["apps"] if app["app_id"] == "commandcode_cli"
    )
    assert command_code["support"] == "desktop_card"
    assert "Desktop apps" in command_code["note"]
