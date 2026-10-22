"""``mcc-codex`` and MCC's media MCP server (7.99.0, 7.107.0).

While Codex's Add MCP server button is on, the launch carries the server as
``-c mcp_servers.mcc_media.*`` settings -- for that launch only, so the user's
``config.toml`` is never rewritten -- placed after MCC's own provider settings
and before the user's arguments, and marked ``required`` (7.107.0). Off, the
command is what it always was.
"""

import tomllib
from pathlib import Path
from unittest.mock import patch

from my_claude_code.cli.launchers import codex
from my_claude_code.cli.launchers.codex import (
    build_codex_launcher_command,
    codex_mcp_config_args,
)
from my_claude_code.config.settings import Settings


def _settings(apps: str) -> Settings:
    return Settings.model_construct(
        host="127.0.0.1",
        port=18999,
        anthropic_auth_token="proxy-secret-token",
        model="nvidia_nim/test-model",
        mcp_media_apps=apps,
    )


def _assignments(args: list[str]) -> dict[str, object]:
    """Each ``-c key=value`` parsed the way Codex parses it: as TOML."""

    assert args[0::2] == ["-c"] * (len(args) // 2)
    parsed: dict[str, object] = {}
    for assignment in args[1::2]:
        key, _, value = assignment.partition("=")
        parsed[key] = tomllib.loads(f"value = {value}")["value"]
    return parsed


def test_the_server_is_passed_only_while_the_button_is_on(tmp_path: Path) -> None:
    command = str(tmp_path / "Scripts" / "mcc-mcp.exe")
    with patch.object(codex, "mcp_media_command", return_value=command):
        assert codex_mcp_config_args(_settings(""), {}) == []
        assert codex_mcp_config_args(_settings("claude,opencode"), {}) == []
        on = codex_mcp_config_args(
            _settings("codex"), {"MCC_CONFIG_DIR": str(tmp_path / "cfg")}
        )

    assert _assignments(on) == {
        "mcp_servers.mcc_media.command": command,
        "mcp_servers.mcc_media.args": ["--app", "codex"],
        "mcp_servers.mcc_media.env": {"MCC_CONFIG_DIR": str(tmp_path / "cfg")},
        # 7.107.0, decision 3: Codex waits for the server before the session
        # starts (its optional-server grace is ~1 s, mcc-mcp takes ~3 s).
        "mcp_servers.mcc_media.required": True,
    }
    assert "proxy-secret-token" not in " ".join(on)


def test_the_settings_go_before_the_users_own_arguments() -> None:
    settings = _settings("codex")
    mcp_args = ["-c", 'mcp_servers.mcc_media.command="x"']
    base = build_codex_launcher_command(
        binary_path="codex",
        argv=["exec", "hello"],
        settings=settings,
        proxy_root_url="http://127.0.0.1:18999",
    )
    with_server = build_codex_launcher_command(
        binary_path="codex",
        argv=["exec", "hello"],
        settings=settings,
        proxy_root_url="http://127.0.0.1:18999",
        mcp_config_args=mcp_args,
    )

    assert with_server == [*base[:-2], *mcp_args, "exec", "hello"]


def test_off_the_command_is_what_it_always_was() -> None:
    settings = _settings("")
    explicit = build_codex_launcher_command(
        binary_path="codex",
        argv=["resume", "--last"],
        settings=settings,
        proxy_root_url="http://127.0.0.1:18999",
        mcp_config_args=(),
    )
    default = build_codex_launcher_command(
        binary_path="codex",
        argv=["resume", "--last"],
        settings=settings,
        proxy_root_url="http://127.0.0.1:18999",
    )
    assert explicit == default
    assert not any("mcp_servers" in part for part in default)
