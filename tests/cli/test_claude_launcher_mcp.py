"""``mcc-claude`` and MCC's media MCP server (7.99.0).

Claude Code's ``--mcp-config`` takes several files, so ``--mcp-config <file>
mcp list`` reads ``mcp`` and ``list`` as two more config files (measured on
2.1.302, risk R4). The launcher therefore only ever writes the ``=`` form, in
front of the user's own arguments, and only while Claude Code's Add MCP
server button is on; off, the command is what it always was.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from my_claude_code.cli.launchers import claude
from my_claude_code.cli.launchers.claude import (
    build_claude_launcher_command,
    claude_mcp_config,
)
from my_claude_code.config.settings import Settings

CONFIG = "/mcc/mcp-claude.json"


@pytest.mark.parametrize(
    "argv",
    [["fix it"], ["-p", "fix it"], ["--resume"], ["mcp", "list"], []],
    ids=["prompt", "print", "resume", "subcommand", "bare"],
)
def test_equals_form_keeps_user_arguments(argv: list[str]) -> None:
    command = build_claude_launcher_command(
        binary_path="claude", argv=argv, mcp_config=CONFIG
    )

    assert command == ["claude", f"--mcp-config={CONFIG}", *argv]
    # Never the spaced form, which would swallow what follows it.
    assert "--mcp-config" not in command


@pytest.mark.parametrize("argv", [["fix it"], ["-p", "x"], ["--resume"], []])
def test_off_the_command_is_what_it_always_was(argv: list[str]) -> None:
    assert build_claude_launcher_command(binary_path="claude", argv=argv) == [
        "claude",
        *argv,
    ]
    assert build_claude_launcher_command(
        binary_path="claude", argv=argv, mcp_config=None
    ) == ["claude", *argv]


def _settings(apps: str, tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    return Settings.model_validate(
        {"MCP_MEDIA_APPS": apps, "ANTHROPIC_AUTH_TOKEN": "proxy-secret-token"}
    )


def test_mccs_file_is_written_only_while_the_button_is_on(
    tmp_path: Path, monkeypatch
) -> None:
    command = str(tmp_path / "Scripts" / "mcc-mcp.exe")
    with patch.object(claude, "mcp_media_command", return_value=command):
        off = claude_mcp_config(_settings("codex", tmp_path, monkeypatch), {})
        assert off is None
        assert not (tmp_path / "mcp-claude.json").exists()

        on = claude_mcp_config(
            _settings("claude", tmp_path, monkeypatch),
            {
                "MCC_CONFIG_DIR": str(tmp_path),
                "ANTHROPIC_AUTH_TOKEN": "proxy-secret-token",
            },
        )

    assert on == str(tmp_path / "mcp-claude.json")
    text = (tmp_path / "mcp-claude.json").read_text(encoding="utf-8")
    assert json.loads(text) == {
        "mcpServers": {
            "mcc_media": {
                "type": "stdio",
                "command": command,
                "args": ["--app", "claude"],
                "env": {"MCC_CONFIG_DIR": str(tmp_path)},
            }
        }
    }
    # The token is read by mcc-mcp at run time; it is never in the file.
    assert "proxy-secret-token" not in text


def test_a_missing_mcc_mcp_launches_without_it_and_says_so(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    with patch.object(claude, "mcp_media_command", return_value=None):
        result = claude_mcp_config(_settings("claude", tmp_path, monkeypatch), {})

    assert result is None
    assert "mcc-mcp" in capsys.readouterr().err
