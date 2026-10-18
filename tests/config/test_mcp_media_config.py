"""The media MCP server's one table and its setting (7.99.0)."""

import os
import tomllib
from pathlib import Path

import pytest

from my_claude_code.api.wire_surfaces import MEDIA_MODELS_ENDPOINT
from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.desktop_apps import desktop_app
from my_claude_code.config.harnesses import harness_specs
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_APP_TABLE,
    MCP_MEDIA_FREE_TOOLS,
    MCP_MEDIA_SPENDING_TOOLS,
    McpMediaShape,
    McpMediaSupport,
    button_app_ids,
    format_mcp_media_apps,
    mcp_media_command,
    mcp_media_on,
    parse_mcp_media_apps,
    server_environment,
    with_app,
)
from my_claude_code.config.onboarding import build_state
from my_claude_code.config.settings import Settings
from my_claude_code.mcp_media.client import MEDIA_CATALOGUE_PATH

REPO = Path(__file__).resolve().parents[2]


def test_the_buttons_are_the_measured_apps() -> None:
    """Decision 10: these nine now; the rest say why not."""

    assert button_app_ids() == (
        "claude",
        "codex",
        "opencode",
        "opencode2",
        "kilo",
        "crush",
        "qwen_code",
        "gemini_cli",
        "claude_desktop",
    )
    support = {app.app_id: app.support for app in MCP_MEDIA_APP_TABLE}
    for app_id in ("kimi_code", "goose", "cline_cli", "droid"):
        assert support[app_id] is McpMediaSupport.NOT_MEASURED
    assert support["pi"] is McpMediaSupport.EXTENSION_LATER
    assert support["aider"] is McpMediaSupport.NO_MCP


def test_every_row_names_a_real_app_and_every_button_says_who_asks() -> None:
    harness_ids = {spec.id for spec in harness_specs()}
    for app in MCP_MEDIA_APP_TABLE:
        if app.shape is McpMediaShape.CLAUDE_DESKTOP_MANAGED:
            sidecar = desktop_app(app.app_id).sidecar
            assert sidecar is not None
            assert sidecar.mcp_servers_key == "managedMcpServers"
        else:
            assert app.app_id in harness_ids, app.app_id
        if app.support is McpMediaSupport.BUTTON:
            assert app.shape is not None and app.asks, app.app_id
    desktop = [
        app
        for app in MCP_MEDIA_APP_TABLE
        if app.shape is McpMediaShape.CLAUDE_DESKTOP_MANAGED
    ]
    assert len(desktop) == 1
    assert set(MCP_MEDIA_SPENDING_TOOLS).isdisjoint(MCP_MEDIA_FREE_TOOLS)


def test_the_setting_is_read_and_written_in_one_order() -> None:
    assert parse_mcp_media_apps("") == ()
    assert parse_mcp_media_apps(None) == ()
    assert parse_mcp_media_apps(" Codex , claude\nnot-an-app,aider ") == (
        "claude",
        "codex",
    )
    assert format_mcp_media_apps(["gemini_cli", "claude"]) == "claude,gemini_cli"
    value = with_app("", "codex", on=True)
    value = with_app(value, "claude", on=True)
    assert value == "claude,codex"
    assert with_app(value, "codex", on=False) == "claude"
    assert with_app("claude", "claude", on=False) == ""
    assert mcp_media_on("claude,codex", "codex") is True
    assert mcp_media_on("claude", "codex") is False


def test_the_program_beside_the_interpreter_wins_then_path(tmp_path: Path) -> None:
    scripts = tmp_path / "env" / "Scripts"
    scripts.mkdir(parents=True)
    interpreter = scripts / "python.exe"
    interpreter.write_bytes(b"")
    name = "mcc-mcp.exe" if os.name == "nt" else "mcc-mcp"
    beside = scripts / name

    elsewhere = str(tmp_path / "bin" / name)
    assert mcp_media_command(
        executable=str(interpreter), which=lambda _name: elsewhere
    ) == os.path.abspath(elsewhere)
    beside.write_bytes(b"")
    assert mcp_media_command(
        executable=str(interpreter), which=lambda _name: elsewhere
    ) == str(beside)
    beside.unlink()
    assert mcp_media_command(executable=str(interpreter), which=lambda _n: None) is None


def test_only_a_non_default_config_folder_is_passed_on() -> None:
    assert server_environment({}) == {}
    assert server_environment({"MCC_CONFIG_DIR": "  "}) == {}
    assert server_environment(
        {"MCC_CONFIG_DIR": "/cfg", "ANTHROPIC_AUTH_TOKEN": "secret", "PORT": "1"}
    ) == {"MCC_CONFIG_DIR": "/cfg"}


def test_the_setting_is_on_the_coding_agents_page_and_hot() -> None:
    field = FIELD_BY_KEY["MCP_MEDIA_APPS"]
    assert field.section_id == "mcp_media"
    assert field.settings_attr == "mcp_media_apps"
    assert field.affects_providers is False
    assert field.restart_required is False
    assert len(field.description) >= 20
    assert Settings.model_validate({}).mcp_media_apps == ""


def test_get_started_step_is_done_once_an_app_has_the_server(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))

    def step():
        state = build_state(claude_settings_configured=False, has_requests=False)
        return next(s for s in state.steps if s.id == "media_tools")

    first = step()
    assert first.done is False
    assert first.optional is True
    assert first.view == "coding_agents"
    assert first.guide_anchors == ("guide-media-mcp",)
    monkeypatch.setenv("MCP_MEDIA_APPS", "codex")
    assert step().done is True


def test_the_catalogue_path_is_the_one_the_api_serves() -> None:
    assert MEDIA_CATALOGUE_PATH == MEDIA_MODELS_ENDPOINT


@pytest.mark.parametrize("prefix", ["opentelemetry-sdk", "opentelemetry-exporter"])
def test_the_sdk_brings_no_telemetry_exporter(prefix: str) -> None:
    """R12: ``mcp`` pulls ``opentelemetry-api`` only, which exports nothing."""

    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    names = {package["name"] for package in lock["package"]}
    assert "opentelemetry-api" in names
    assert not any(name.startswith(prefix) for name in names)


def test_mcc_mcp_is_a_console_script() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["mcc-mcp"] == (
        "my_claude_code.mcp_media.server:main"
    )
    assert any(
        dep.startswith("mcp>=2.3,<3") for dep in pyproject["project"]["dependencies"]
    )
