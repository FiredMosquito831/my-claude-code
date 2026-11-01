"""Every installed command answers ``--help`` / ``-h`` without doing its work.

Until 7.113.1 three commands ignored the flag and ran: ``mcc-init --help``
wrote ``.env``, ``mcc-compact-log --help`` rewrote and vacuumed the request
log, and ``mcc-migrate --help`` moved the config home. ``mcc-server --help``
had started a real server until 7.111.5 (#283). They now print the command
reference and return before any work, the same check ``serve()`` uses.

Two guards, so the next command cannot regress quietly:

* every console script and gui script in ``pyproject.toml`` is either in
  ``HELP_CASES`` (its ``--help`` is run here with its work mocked, and the work
  must not be called) or in ``PASS_THROUGH`` with the reason -- the coding-agent
  launchers, whose ``--help`` belongs to the agent;
* the four MCC entry points that do real work read the help flag as their
  second statement, straight after ``--version`` (read from the source, so the
  order is pinned even where no runtime test can reach it).
"""

import ast
import json
import subprocess
import sys
import tomllib
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import pytest

from my_claude_code.cli import entrypoints

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS_SOURCE = REPO_ROOT / "src" / "my_claude_code" / "cli" / "entrypoints.py"
REFERENCE_HEADLINE = "My Claude Code -- commands"
LAUNCHER_PACKAGE = "my_claude_code.cli.launchers."


@dataclass(frozen=True, slots=True)
class HelpCase:
    """How one command answers ``--help``, and what it must not do first."""

    target: str
    #: Patch targets that do the command's work; none may be called.
    work: tuple[str, ...]
    exit_code: int
    text: str
    stream: str = "out"


_SERVE = HelpCase(
    "my_claude_code.cli.entrypoints:serve",
    work=(
        "my_claude_code.cli.commands.serve",
        "my_claude_code.cli.entrypoints._bootstrap_config_paths",
        "my_claude_code.cli.entrypoints._answer_installer_question",
    ),
    exit_code=0,
    text=REFERENCE_HEADLINE,
)

#: Every MCC command that is not a coding-agent launcher. Each one is run with
#: ``--help`` and ``-h`` below, its work mocked; the work must not be called.
HELP_CASES: dict[str, HelpCase] = {
    "mcc-server": _SERVE,
    "my-claude-code": _SERVE,
    "mcc-init": HelpCase(
        "my_claude_code.cli.entrypoints:init",
        work=(
            "my_claude_code.cli.commands.init",
            "my_claude_code.cli.entrypoints._bootstrap_config_paths",
        ),
        exit_code=0,
        text=REFERENCE_HEADLINE,
    ),
    "mcc-compact-log": HelpCase(
        "my_claude_code.cli.entrypoints:compact_log",
        work=(
            "my_claude_code.cli.commands.compact_log",
            "my_claude_code.core.request_log.compact_request_log",
            "my_claude_code.cli.entrypoints._bootstrap_config_paths",
        ),
        exit_code=0,
        text=REFERENCE_HEADLINE,
    ),
    "mcc-migrate": HelpCase(
        "my_claude_code.cli.entrypoints:migrate_config_dir",
        work=(
            "my_claude_code.cli.migrate_config_dir.main",
            "my_claude_code.cli.migrate_config_dir.migrate_config_dir",
        ),
        exit_code=0,
        text=REFERENCE_HEADLINE,
    ),
    "mcc-help": HelpCase(
        "my_claude_code.cli.entrypoints:help_command",
        work=(),
        exit_code=0,
        text=REFERENCE_HEADLINE,
    ),
    "mcc-chatgpt-oauth-login": HelpCase(
        "my_claude_code.cli.entrypoints:chatgpt_oauth_login",
        work=(
            "my_claude_code.providers.chatgpt_oauth.browser_login.perform_browser_login",
            "my_claude_code.providers.chatgpt_oauth.browser_login.perform_chatgpt_oauth_login",
            "my_claude_code.providers.chatgpt_oauth.browser_login._print_chatgpt_accounts",
        ),
        exit_code=0,
        text="mcc-chatgpt-oauth-login --",
    ),
    "mcc-anthropic-oauth-login": HelpCase(
        "my_claude_code.cli.entrypoints:anthropic_oauth_login",
        work=(
            "my_claude_code.providers.anthropic_oauth.cli._run_loopback_flow",
            "my_claude_code.providers.anthropic_oauth.cli._run_paste_flow",
            "my_claude_code.providers.anthropic_oauth.cli._describe_existing",
            "my_claude_code.providers.anthropic_oauth.cli._list_accounts",
        ),
        exit_code=0,
        text="mcc-anthropic-oauth-login --",
    ),
    "mcc-rtk": HelpCase(
        "my_claude_code.cli.entrypoints:rtk",
        work=(
            "my_claude_code.cli.rtk_commands.rtk_status",
            "my_claude_code.cli.rtk_commands._reconcile",
            "my_claude_code.cli.rtk_commands.apply_rtk_state",
        ),
        exit_code=0,
        text="Usage: mcc-rtk",
    ),
    "mcc-apps": HelpCase(
        "my_claude_code.cli.entrypoints:apps",
        work=(
            "my_claude_code.cli.apps_command._print_list",
            "my_claude_code.cli.apps_command._print_status",
            "my_claude_code.cli.apps_command._configure",
            "my_claude_code.cli.apps_command._undo",
        ),
        exit_code=0,
        text="usage: mcc-apps",
    ),
    "mcc-mcp": HelpCase(
        "my_claude_code.mcp_media.server:main",
        work=(
            "my_claude_code.mcp_media.server.build_state",
            "my_claude_code.mcp_media.server.serve",
        ),
        exit_code=0,
        text="usage: mcc-mcp",
    ),
    # A GUI script: the usage goes to stderr with exit 2, the answer every
    # mcc-desktop gives an argument it does not know. Nothing is started.
    "mcc-desktop": HelpCase(
        "my_claude_code.cli.desktop_entrypoint:launch",
        work=(
            "my_claude_code.cli.desktop_entrypoint._launch_host",
            "my_claude_code.cli.desktop_entrypoint.set_start_at_login",
            "my_claude_code.cli.desktop_entrypoint.set_server_mode",
        ),
        exit_code=2,
        text="Usage: mcc-desktop",
        stream="err",
    ),
}

#: 8.0.0 removed the fourteen retired ``fcc-*`` names, so there is no tombstone
#: case left to run: every installed command is now an MCC command or a coding
#: agent launcher. ``test_no_legacy_fcc_surface`` asserts none of the retired
#: names is installed again.

_AGENT_HELP = (
    "a coding-agent launcher: --help, -h and --version belong to the agent, so "
    "they are handed to it (with the proxy configured, or untouched where the "
    "registry lists them as pass-through flags) and the agent prints its own "
    "help; the command reference says so"
)

#: Commands whose ``--help`` is NOT MCC's, on purpose. Each must be a launcher.
PASS_THROUGH: dict[str, str] = dict.fromkeys(
    (
        "mcc-claude",
        "mcc-claude-old",
        "mcc-codex",
        "mcc-pi",
        "mcc-opencode",
        "mcc-opencode2",
        "mcc-kilo",
        "mcc-commandcode",
        "mcc-openclaw",
        "mcc-hermes",
        "mcc-kimi",
        "mcc-qwen",
        "mcc-crush",
        "mcc-cline",
        "mcc-goose",
        "mcc-aider",
        "mcc-droid",
        "mcc-gemini",
    ),
    _AGENT_HELP,
)


def _installed_commands() -> dict[str, str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    commands = dict(project["scripts"])
    commands.update(project.get("gui-scripts", {}))
    return commands


def _resolve(target: str) -> Callable[[], object]:
    module_name, _, attribute = target.partition(":")
    return getattr(import_module(module_name), attribute)


def test_every_installed_command_has_a_help_test_or_a_reason() -> None:
    installed = _installed_commands()
    unclassified = sorted(set(installed) - set(HELP_CASES) - set(PASS_THROUGH))
    assert unclassified == [], (
        "These commands have no --help test and no PASS_THROUGH reason; add a "
        f"HELP_CASES entry (its work mocked) for each: {unclassified}"
    )
    assert sorted(set(HELP_CASES) & set(PASS_THROUGH)) == []
    assert sorted((set(HELP_CASES) | set(PASS_THROUGH)) - set(installed)) == []


def test_every_help_case_names_the_installed_entry_point() -> None:
    installed = _installed_commands()
    wrong = {
        name: (case.target, installed[name])
        for name, case in HELP_CASES.items()
        if installed[name] != case.target
    }
    assert wrong == {}


def test_only_launchers_may_pass_help_through() -> None:
    installed = _installed_commands()
    not_launchers = sorted(
        name
        for name in PASS_THROUGH
        if not installed[name].startswith(LAUNCHER_PACKAGE)
    )
    assert not_launchers == []
    assert all(len(reason) >= 40 for reason in PASS_THROUGH.values())


def test_the_reference_tells_the_reader_where_agent_help_goes() -> None:
    text = entrypoints._help_text()
    assert "mcc-claude --help" in text
    assert "go to the agent itself" in text
    # The reference printed by ``mcc-migrate --help`` names mcc-migrate.
    assert "mcc-migrate" in text


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("command", sorted(HELP_CASES))
def test_help_answers_before_any_work(
    command: str,
    flag: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run the command as its console script does: ``sys.exit(entry())``."""

    case = HELP_CASES[command]
    entry = _resolve(case.target)
    monkeypatch.setattr(sys, "argv", [command, flag])
    with ExitStack() as stack:
        mocks = [(target, stack.enter_context(patch(target))) for target in case.work]
        try:
            returned = entry()
            code = 0 if returned is None else returned
        except SystemExit as exc:
            code = 0 if exc.code is None else exc.code

    called = [target for target, mock in mocks if mock.called]
    assert called == [], f"{command} {flag} did its work first: {called}"
    assert code == case.exit_code
    captured = capsys.readouterr()
    shown = captured.out if case.stream == "out" else captured.err
    assert case.text in shown


#: The MCC entry points that do real work after their flag checks.
WORKING_ENTRY_POINTS = ("serve", "init", "compact_log", "migrate_config_dir")


def _flag_check_name(statement: ast.stmt) -> str | None:
    """``if _check(argv): return ...`` -> ``_check``; anything else -> None."""

    if not isinstance(statement, ast.If) or statement.orelse:
        return None
    test = statement.test
    if not (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and [ast.unparse(arg) for arg in test.args] == ["argv"]
    ):
        return None
    if len(statement.body) != 1 or not isinstance(statement.body[0], ast.Return):
        return None
    return test.func.id


@pytest.mark.parametrize("name", WORKING_ENTRY_POINTS)
def test_the_help_check_is_read_straight_after_the_version_check(name: str) -> None:
    """Static: nothing runs between ``--version`` and ``--help`` and the work."""

    tree = ast.parse(ENTRYPOINTS_SOURCE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    body = list(function.body)
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring
    assert [_flag_check_name(statement) for statement in body[:2]] == [
        "_print_version_if_requested",
        "_print_help_if_requested",
    ]


@pytest.mark.parametrize("name", ["init", "compact_log", "migrate_config_dir"])
@pytest.mark.parametrize("argv", [("--help", "--version"), ("--version", "-h")])
def test_version_still_wins_over_help(
    name: str, argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    """The order ``serve()`` has had since 7.111.5, kept for the other three."""

    with patch.object(entrypoints, "package_version", return_value="9.8.7"):
        returned = getattr(entrypoints, name)(argv)

    assert returned in (None, 0)
    out = capsys.readouterr().out
    assert out.endswith(" 9.8.7\n")
    assert REFERENCE_HEADLINE not in out


def test_compact_log_without_a_flag_still_compacts() -> None:
    from my_claude_code.cli import commands

    with (
        patch.object(entrypoints, "_bootstrap_config_paths") as bootstrap,
        patch.object(commands, "compact_log") as run,
    ):
        entrypoints.compact_log(())

    bootstrap.assert_called_once_with()
    run.assert_called_once_with()


def test_migrate_without_a_flag_still_migrates() -> None:
    from my_claude_code.cli import migrate_config_dir

    with patch.object(migrate_config_dir, "main", return_value=0) as run:
        assert entrypoints.migrate_config_dir(()) == 0

    run.assert_called_once_with([])


def test_init_without_a_flag_still_initialises() -> None:
    from my_claude_code.cli import commands

    with (
        patch.object(entrypoints, "_bootstrap_config_paths") as bootstrap,
        patch.object(commands, "init") as run,
    ):
        entrypoints.init(())

    bootstrap.assert_called_once_with()
    run.assert_called_once_with()


@pytest.mark.local_serial
@pytest.mark.parametrize("name", ["init", "compact_log", "migrate_config_dir"])
def test_help_loads_none_of_the_work(name: str) -> None:
    """In a fresh interpreter, ``--help`` imports no module that does the work."""

    script = "\n".join(
        (
            "import json",
            "import sys",
            f"from my_claude_code.cli.entrypoints import {name}",
            f"{name}(['--help'])",
            "forbidden = ('my_claude_code.cli.commands', "
            "'my_claude_code.cli.migrate_config_dir', "
            "'my_claude_code.core.request_log', 'uvicorn', 'fastapi')",
            "print(json.dumps([m for m in forbidden if m in sys.modules]))",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert REFERENCE_HEADLINE in completed.stdout
    assert json.loads(completed.stdout.splitlines()[-1]) == []
