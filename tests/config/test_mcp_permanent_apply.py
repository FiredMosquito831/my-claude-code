"""Tests for permanent user-config MCP targets and safe apply/undo."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from my_claude_code.cli.launchers import claude as claude_launcher
from my_claude_code.cli.launchers import codex as codex_launcher
from my_claude_code.config import mcp_media, mcp_permanent_apply, paths
from my_claude_code.config.desktop_apply import probe_mcp
from my_claude_code.config.desktop_apps import DESKTOP_APPS_BY_ID
from my_claude_code.config.document_codecs import DocumentFormat
from my_claude_code.config.mcp_media import (
    PermanentMcpPathRung,
    PermanentMcpTargetSpec,
    has_permanent_mcp_entry,
    mcp_media_document,
    permanent_mcp_target,
    resolve_permanent_mcp_path,
)
from my_claude_code.config.mcp_permanent_apply import (
    PermanentMcpError,
    PermanentMcpState,
    apply_permanent_mcp,
    permanent_entry_for,
    plan_permanent_mcp,
    probe_permanent_mcp,
)
from my_claude_code.config.restore_record import UndoMode
from my_claude_code.config.settings import Settings
from tests.config.test_desktop_apply import make_marker


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    mcc_dir = tmp_path / "mcc"
    mcc_dir.mkdir()
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "MCC_CONFIG_DIR": str(mcc_dir),
        "PATH": str(home / "bin"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    paths.reset_config_dir_cache()
    return env


@pytest.fixture
def fake_mcp_cmd(tmp_path: Path) -> Iterator[str]:
    cmd = str(tmp_path / "bin" / "mcc-mcp")
    with (
        patch.object(mcp_media, "mcp_media_command", return_value=cmd),
        patch.object(mcp_permanent_apply, "mcp_media_command", return_value=cmd),
    ):
        yield cmd


def test_claude_json_plan_apply_restore_and_launch_guard(
    isolated_env: dict[str, str], fake_mcp_cmd: str
) -> None:
    home = Path(isolated_env["HOME"])
    claude_cfg = home / ".claude.json"
    original_bytes = '{\n  "theme": "dark",\n  "mcpServers": {\n    "other": {"command": "echo"}\n  }\n}\n'
    claude_cfg.write_text(original_bytes, encoding="utf-8")

    spec = permanent_mcp_target("claude")
    assert spec is not None
    probe0 = probe_permanent_mcp(spec, env=isolated_env)
    assert probe0.state is PermanentMcpState.ABSENT
    assert probe0.document_path == str(claude_cfg)

    plan = plan_permanent_mcp("claude", add=True, env=isolated_env)
    assert not plan.no_op
    assert '"mcc_media"' in plan.diff

    res = apply_permanent_mcp("claude", add=True, env=isolated_env)
    assert res.changed
    assert Path(res.backup_path).read_text(encoding="utf-8") == original_bytes

    data = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["mcpServers"]["other"] == {"command": "echo"}
    assert data["mcpServers"]["mcc_media"]["command"] == fake_mcp_cmd
    assert data["mcpServers"]["mcc_media"]["args"] == ["--app", "claude"]

    settings = Settings.model_validate({"MCP_MEDIA_APPS": "claude"})
    assert has_permanent_mcp_entry("claude", environ=isolated_env) is True
    assert claude_launcher.claude_mcp_config(settings, isolated_env) is None

    undo_res = apply_permanent_mcp(
        "claude", add=False, mode=UndoMode.RESTORE, env=isolated_env
    )
    assert undo_res.changed
    assert claude_cfg.read_text(encoding="utf-8") == original_bytes
    assert has_permanent_mcp_entry("claude", environ=isolated_env) is False
    assert claude_launcher.claude_mcp_config(settings, isolated_env) is not None


def test_codex_toml_shared_desktop_sync_tool_search_and_launch_guard(
    isolated_env: dict[str, str], fake_mcp_cmd: str
) -> None:
    home = Path(isolated_env["HOME"])
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    codex_toml = codex_dir / "config.toml"
    original_toml = (
        '# user header\nmodel = "o3"\n\n[mcp_servers.existing]\ncommand = "foo"\n'
    )
    codex_toml.write_text(original_toml, encoding="utf-8")

    # Even with empty MCP_MEDIA_APPS, writing permanent codex config keeps
    # supports_search_tool: false so Codex's model sees the tools, while
    # mcc-codex stops injecting duplicate -c args.
    apply_permanent_mcp("codex", add=True, env=isolated_env)
    assert has_permanent_mcp_entry("codex", environ=isolated_env) is True
    assert "# user header" in codex_toml.read_text(encoding="utf-8")

    codex_spec = permanent_mcp_target("codex")
    assert codex_spec is not None
    make_marker(Path(isolated_env["HOME"]), DESKTOP_APPS_BY_ID["codex_desktop"])
    expected = permanent_entry_for(codex_spec, env=isolated_env, command=fake_mcp_cmd)
    desktop_probe = probe_mcp(
        DESKTOP_APPS_BY_ID["codex_desktop"],
        env=isolated_env,
        expected_entry=expected,
    )
    assert desktop_probe.state.value == "added"

    sample_catalogue: dict[str, object] = {
        "models": [{"slug": "o3", "supports_search_tool": True}]
    }
    doc = mcp_media_document(
        "codex", sample_catalogue, apps_value="", environ=isolated_env
    )
    assert isinstance(doc, dict)
    assert doc["models"][0]["supports_search_tool"] is False
    settings = Settings.model_validate({"MCP_MEDIA_APPS": "codex"})
    assert codex_launcher.codex_mcp_config_args(settings, isolated_env) == []

    apply_permanent_mcp("codex", add=False, mode=UndoMode.KEYS_ONLY, env=isolated_env)
    after_toml = codex_toml.read_text(encoding="utf-8")
    assert "mcc_media" not in after_toml
    assert "[mcp_servers.existing]" in after_toml
    doc_after = mcp_media_document(
        "codex", sample_catalogue, apps_value="", environ=isolated_env
    )
    assert doc_after is sample_catalogue
    assert len(codex_launcher.codex_mcp_config_args(settings, isolated_env)) > 0


def test_opencode_qwen_and_openclaw_shapes(
    isolated_env: dict[str, str], fake_mcp_cmd: str
) -> None:
    apply_permanent_mcp("opencode", add=True, env=isolated_env)
    oc_spec = permanent_mcp_target("opencode")
    assert oc_spec is not None
    oc_path = resolve_permanent_mcp_path(oc_spec, isolated_env)
    assert oc_path is not None and oc_path.is_file()
    oc_data = json.loads(oc_path.read_text(encoding="utf-8"))
    assert oc_data["mcp"]["mcc_media"]["type"] == "local"
    assert oc_data["mcp"]["mcc_media"]["command"][0] == fake_mcp_cmd
    assert oc_data["permission"]["mcc_media_generate_image"] == "ask"
    assert has_permanent_mcp_entry("opencode2", environ=isolated_env) is True

    apply_permanent_mcp("qwen_code", add=True, env=isolated_env)
    qw_path = Path(isolated_env["HOME"]) / ".qwen" / "settings.json"
    qw_data = json.loads(qw_path.read_text(encoding="utf-8"))
    assert qw_data["mcpServers"]["mcc_media"]["args"] == ["--app", "qwen_code"]

    apply_permanent_mcp("openclaw", add=True, env=isolated_env)
    claw_path = Path(isolated_env["HOME"]) / ".openclaw" / "openclaw.json"
    claw_data = json.loads(claw_path.read_text(encoding="utf-8"))
    assert claw_data["mcp"]["servers"]["mcc_media"]["command"] == fake_mcp_cmd


def test_foreign_entry_is_never_overwritten(
    isolated_env: dict[str, str], fake_mcp_cmd: str
) -> None:
    assert fake_mcp_cmd
    gemini_cfg = Path(isolated_env["HOME"]) / ".gemini" / "settings.json"
    gemini_cfg.parent.mkdir(parents=True)
    raw = json.dumps(
        {"mcpServers": {"mcc_media": {"command": "/usr/local/bin/custom-mcp"}}}
    )
    gemini_cfg.write_text(raw, encoding="utf-8")

    spec = permanent_mcp_target("gemini_cli")
    assert spec is not None
    probe = probe_permanent_mcp(spec, env=isolated_env)
    assert probe.state is PermanentMcpState.FOREIGN

    with pytest.raises(PermanentMcpError, match="did not add"):
        plan_permanent_mcp("gemini_cli", add=True, env=isolated_env)

    with pytest.raises(PermanentMcpError, match="did not add"):
        apply_permanent_mcp("gemini_cli", add=True, env=isolated_env)

    assert gemini_cfg.read_text(encoding="utf-8") == raw


def test_yaml_codec_roundtrip_and_keys_only_removal(
    isolated_env: dict[str, str], fake_mcp_cmd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_spec = PermanentMcpTargetSpec(
        target_id="yaml_demo",
        display_name="YAML Demo",
        document_format=DocumentFormat.YAML,
        rungs=(
            PermanentMcpPathRung(
                env_vars=("HOME",), parts=(".yaml_demo", "config.yaml")
            ),
        ),
        display_path="~/.yaml_demo/config.yaml",
        servers_key=("mcpServers",),
        shape=mcp_media.McpMediaShape.MCP_SERVERS,
    )
    monkeypatch.setitem(mcp_media._PERMANENT_BY_ID, "yaml_demo", yaml_spec)
    cfg = Path(isolated_env["HOME"]) / ".yaml_demo" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    original_yaml = (
        "# keep comment\ntheme: light\nmcpServers:\n  existing:\n    command: echo\n"
    )
    cfg.write_text(original_yaml, encoding="utf-8")

    yaml_cmd = Path(fake_mcp_cmd).as_posix()
    apply_permanent_mcp(
        "yaml_demo",
        add=True,
        env=isolated_env,
        entry={"command": yaml_cmd},
    )
    written = cfg.read_text(encoding="utf-8")
    assert "# keep comment" in written
    assert "mcc_media:" in written
    assert yaml_cmd in written

    apply_permanent_mcp(
        "yaml_demo", add=False, mode=UndoMode.KEYS_ONLY, env=isolated_env
    )
    cleaned = cfg.read_text(encoding="utf-8")
    assert "mcc_media" not in cleaned
    assert "existing:" in cleaned
