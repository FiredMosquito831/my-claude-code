"""Tests for the ``mcc-rtk`` CLI entrypoint and its subcommands."""

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from my_claude_code.cli import entrypoints, rtk_commands
from my_claude_code.config.rtk import RtkError, RtkState


def _run_rtk(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    apply = MagicMock()
    monkeypatch.setattr(rtk_commands, "apply_rtk_state", apply)
    monkeypatch.setattr(
        rtk_commands,
        "rtk_status",
        MagicMock(
            return_value={
                "installed": True,
                "claude": False,
                "codex": False,
                "pi": False,
                "binary_path": "/x/rtk",
                "version": "rtk 0.44.2",
            }
        ),
    )
    monkeypatch.setattr(rtk_commands, "save_rtk_state", MagicMock())
    monkeypatch.setattr(rtk_commands, "load_rtk_state", lambda: RtkState())
    return apply


def test_console_scripts_are_registered() -> None:
    manifest = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    scripts = manifest["project"]["scripts"]
    assert scripts["mcc-rtk"] == "my_claude_code.cli.entrypoints:rtk"
    assert scripts["fcc-rtk"] == "my_claude_code.cli.legacy_stubs:main"


def test_rtk_entrypoint_is_callable() -> None:
    assert callable(entrypoints.rtk)


def test_status_prints_status_dict(capsys, monkeypatch, tmp_path):
    _run_rtk(["status"], monkeypatch, tmp_path)

    rtk_commands.rtk_command(["status"])

    out = capsys.readouterr().out
    assert "installed:" in out
    assert "claude" in out
    assert "codex" in out
    assert "pi" in out
    assert "binary_path:" in out
    assert "version:" in out


def test_enable_sets_agents_and_reconciles(monkeypatch, tmp_path):
    apply = _run_rtk(["enable", "claude,codex"], monkeypatch, tmp_path)

    rtk_commands.rtk_command(["enable", "claude,codex"])

    state = apply.call_args.args[0]
    assert state.claude is True
    assert state.codex is True
    assert state.pi is False
    assert apply.call_args.kwargs == {"uninstall": False}


def test_enable_space_separated_agents(monkeypatch, tmp_path):
    apply = _run_rtk(["enable", "claude", "pi"], monkeypatch, tmp_path)

    rtk_commands.rtk_command(["enable", "claude", "pi"])

    state = apply.call_args.args[0]
    assert state.claude is True
    assert state.pi is True
    assert state.codex is False


def test_disable_sets_agents_false_and_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        rtk_commands,
        "load_rtk_state",
        lambda: RtkState(claude=True, codex=True, pi=True),
    )
    apply = MagicMock()
    monkeypatch.setattr(rtk_commands, "apply_rtk_state", apply)
    monkeypatch.setattr(rtk_commands, "save_rtk_state", MagicMock())

    rtk_commands.rtk_command(["disable", "claude"])

    state = apply.call_args.args[0]
    assert state.claude is False
    assert state.codex is True
    assert state.pi is True


def test_uninstall_disables_all_and_removes_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rtk_commands, "load_rtk_state", lambda: RtkState(claude=True))
    apply = MagicMock()
    monkeypatch.setattr(rtk_commands, "apply_rtk_state", apply)
    monkeypatch.setattr(rtk_commands, "save_rtk_state", MagicMock())

    rtk_commands.rtk_command(["uninstall"])

    state = apply.call_args.args[0]
    assert state.claude is False
    assert state.codex is False
    assert state.pi is False
    assert apply.call_args.kwargs == {"uninstall": True}


def test_apply_reconciles_stored_state(monkeypatch, tmp_path):
    apply = _run_rtk(["apply"], monkeypatch, tmp_path)

    rtk_commands.rtk_command(["apply"])

    apply.assert_called_once_with(RtkState())


def test_unknown_agent_errors(monkeypatch, tmp_path, capsys):
    _run_rtk(["enable", "bogus"], monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        rtk_commands.rtk_command(["enable", "bogus"])

    assert exc_info.value.code == 1
    assert "unknown agent" in capsys.readouterr().err


def test_unknown_subcommand_errors(capsys, monkeypatch, tmp_path):
    _run_rtk(["frobnicate"], monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        rtk_commands.rtk_command(["frobnicate"])

    assert exc_info.value.code == 1
    assert "unknown subcommand" in capsys.readouterr().err


def test_no_arguments_prints_usage(capsys, monkeypatch, tmp_path):
    _run_rtk([], monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        rtk_commands.rtk_command([])

    assert exc_info.value.code == 1
    assert "Usage: mcc-rtk" in capsys.readouterr().out


def test_help_prints_usage(capsys, monkeypatch, tmp_path):
    _run_rtk(["help"], monkeypatch, tmp_path)

    rtk_commands.rtk_command(["help"])

    out = capsys.readouterr().out
    assert "enable" in out
    assert "uninstall" in out
    assert "apply" in out


def test_rtk_error_is_reported_on_stderr(monkeypatch, tmp_path, capsys):
    apply = _run_rtk(["apply"], monkeypatch, tmp_path)
    apply.side_effect = RtkError("boom")

    with pytest.raises(SystemExit) as exc_info:
        rtk_commands.rtk_command(["apply"])

    assert exc_info.value.code == 1
    assert "boom" in capsys.readouterr().err


def test_rtk_entrypoint_delegates_to_command(monkeypatch):
    command = MagicMock()
    monkeypatch.setattr(rtk_commands, "rtk_command", command)

    entrypoints.rtk(["status"])

    command.assert_called_once_with(["status"])


def test_rtk_entrypoint_passes_none_when_no_argv(monkeypatch):
    command = MagicMock()
    monkeypatch.setattr(rtk_commands, "rtk_command", command)

    entrypoints.rtk(None)

    command.assert_called_once_with(None)
