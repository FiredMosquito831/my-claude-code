"""``mcc-desktop --ensure-shell``: the command that makes the pin reach a machine.

Until 6.60.0 the pinned desktop app was fetched by exactly one caller --
``ShellWindow.create()``, reached only when the *Python tray* started. A user
who launched ``MyClaudeCode.exe`` from the Start Menu, the taskbar, or the
Programs-folder install the native installer makes ran whatever shell they
first received and nothing would ever move it. One did: v6.43.0, for fifteen
releases, while the wheel updated itself every time (BUG-0).

This is that check as a command. What is asserted here is the *surface* -- the
argument shapes, the JSON on stdout, the exit code, and where a failure goes --
because that surface is a contract the Rust side parses.
``tests/config/test_desktop_shell.py`` covers the staging itself.
"""

import json
from pathlib import Path

import pytest

from my_claude_code.cli import desktop_entrypoint
from my_claude_code.config import desktop_shell
from my_claude_code.config.desktop_shell import DesktopShellError


@pytest.fixture
def staged(monkeypatch):
    """Record what ``--ensure-shell`` asked for, and answer with a report."""

    calls: list[Path | None] = []
    report: dict[str, object] = {
        "updated": True,
        "from_tag": "v6.43.0",
        "to_tag": "v6.60.0",
        "staged_path": "C:/apps/MyClaudeCode.exe.new",
        "restart_required": True,
    }

    def _stage(target: Path | None = None) -> dict[str, object]:
        calls.append(target)
        return report

    monkeypatch.setattr(desktop_shell, "stage_desktop_shell", _stage)
    return calls, report


def test_it_prints_the_report_as_json_and_nothing_else(staged, capsys) -> None:
    """The window parses stdout, so stdout is JSON and diagnostics are stderr."""

    calls, report = staged

    desktop_entrypoint.launch(["--ensure-shell"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == report
    assert captured.err == ""
    assert calls == [None], "no --target means the default install"


def test_a_target_names_the_binary_that_is_actually_running(staged, capsys) -> None:
    """The window passes its own executable: the Programs-folder case."""

    calls, _ = staged

    desktop_entrypoint.launch(
        ["--ensure-shell", "--target", r"C:\apps\My Claude Code\MyClaudeCode.exe"]
    )

    assert calls == [Path(r"C:\apps\My Claude Code\MyClaudeCode.exe")]
    json.loads(capsys.readouterr().out)


def test_a_failure_is_an_exit_code_and_a_sentence_on_stderr(
    monkeypatch, capsys
) -> None:
    """A window that gets no JSON must be able to tell that from an empty update."""

    def _stage(target: Path | None = None) -> dict[str, object]:
        raise DesktopShellError("the release feed could not be reached")

    monkeypatch.setattr(desktop_shell, "stage_desktop_shell", _stage)

    with pytest.raises(SystemExit) as exit_info:
        desktop_entrypoint.launch(["--ensure-shell"])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "release feed" in captured.err


@pytest.mark.parametrize(
    "args",
    [
        ["--ensure-shell", "--target"],
        ["--ensure-shell", "C:/apps/MyClaudeCode.exe"],
        ["--ensure-shell", "--target", "a", "b"],
        ["--ensure-shell", "--presence-v2"],
    ],
)
def test_a_shape_this_command_does_not_take_is_refused(args, capsys) -> None:
    """Two accepted shapes and no more; anything else prints usage and exits 2."""

    with pytest.raises(SystemExit) as exit_info:
        desktop_entrypoint.launch(args)

    assert exit_info.value.code == 2
    assert "--ensure-shell" in capsys.readouterr().err


def test_the_usage_line_documents_it() -> None:
    """A command a person cannot discover is a command that does not exist."""

    import io
    from contextlib import redirect_stderr

    buffer = io.StringIO()
    with redirect_stderr(buffer), pytest.raises(SystemExit):
        desktop_entrypoint.launch(["--nonsense"])

    assert "--ensure-shell [--target PATH]" in buffer.getvalue()
