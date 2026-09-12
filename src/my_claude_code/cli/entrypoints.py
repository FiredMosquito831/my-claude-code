"""Lightweight entry points for installed commands.

The ``my-claude-code`` owner registers every console script against these
implementations. The pre-7.0.0 ``fcc-*`` family is retired: those names now
resolve to ``cli.legacy_stubs``, which prints the rename and exits 1.
"""

import sys
from collections.abc import Sequence

from my_claude_code.config.harnesses import harness_command_lines, harness_specs
from my_claude_code.core.identity import owner_for_invocation
from my_claude_code.core.version import package_version


def help_command(argv: Sequence[str] | None = None) -> None:
    """Print a short reference for every command and how to use it."""
    del argv  # --version is handled by the entry point; otherwise no options.
    print(_help_text())


def _harness_command_lines() -> str:
    """Render one help line per registered harness command.

    Generated rather than written out: a harness added to the registry appears
    here, in the installer summary and on the dashboard without three separate
    edits, which is how one of them used to be forgotten.
    """

    lines: list[str] = []
    for spec in harness_specs():
        for line in harness_command_lines(spec):
            if line.kind not in ("primary", "flag"):
                # The retired fcc- names are covered by the paragraph below,
                # and the RTK toggles belong under "Manage and inspect".
                continue
            lines.append(f"  {line.command:<23} {line.help_text}".rstrip())
    lines.append("  mcc-desktop             Open the system tray app (desktop)")
    return "\n".join(lines)


def _help_text() -> str:
    return f"""My Claude Code -- commands

The proxy runs on your machine and routes your coding agents to the models and
providers you configure. Everything is local: your keys stay in ~/.mcc.

Start the proxy:
  mcc-server              Start the local proxy and admin dashboard
  my-claude-code          Same as mcc-server (full command name)

Use a coding agent through the proxy:
{_harness_command_lines()}

Manage and inspect:
  mcc-init                Create or repair ~/.mcc/.env with the config template
  mcc-chatgpt-oauth-login Log in to ChatGPT/Codex via OAuth device flow
  mcc-anthropic-oauth-login Log in to a Claude subscription via OAuth
                          (not permitted by Anthropic -- read
                          docs/ANTHROPIC-SUBSCRIPTION.md first)
  mcc-compact-log         Compact the request log (deduplicate + compress)
  mcc-rtk                 Manage the RTK token optimizer
  mcc-apps                Point desktop apps here, and take it back out
                          (list | status | configure | undo)
  mcc-help                Show this command reference

The legacy fcc-* commands were retired in 7.0.0. They are still installed for
this major version, but each one now prints the mcc-* name that replaced it and
exits 1; they are removed entirely in 8.0.0.

Updates: install while the server is running. On Windows the update is staged
and completes after you stop and restart the app; on Linux/WSL it applies
immediately and is picked up on the next restart. Run mcc-server again after an
update to start the new version.
"""


def _bootstrap_config_paths() -> None:
    """Resolve the config directory and register the request-log path.

    The one place that ties the config-dir rule to ``core.request_log`` without
    violating the import-boundary contract: ``core`` may not import ``config``,
    so the resolved ``request_log_path`` is pushed into ``core`` here, before
    any store is opened. Runs once per process; later calls are a no-op.
    """

    from my_claude_code.config import paths
    from my_claude_code.core import request_log

    paths.config_dir_resolution()
    request_log.set_request_log_path(paths.request_log_path())


def serve(argv: Sequence[str] | None = None) -> None:
    """Start the FastAPI server."""
    if _print_version_if_requested(argv):
        return

    if _answer_installer_question(argv):
        return

    _bootstrap_config_paths()

    # Keep the server composition root off metadata-only command paths.
    from my_claude_code.cli.commands import serve as run_server

    run_server()


def init(argv: Sequence[str] | None = None) -> None:
    """Scaffold config at the resolved config directory's .env."""
    if _print_version_if_requested(argv):
        return

    _bootstrap_config_paths()

    # Config initialization shares command infrastructure with the server.
    from my_claude_code.cli.commands import init as initialize_config

    initialize_config()


def chatgpt_oauth_login(argv: Sequence[str] | None = None) -> None:
    """Log in to ChatGPT/Codex via OAuth device flow."""
    if _print_version_if_requested(argv):
        return

    from my_claude_code.cli.commands import chatgpt_oauth_login as run_login

    run_login()


def anthropic_oauth_login(argv: Sequence[str] | None = None) -> None:
    """Log in to a Claude subscription via OAuth (see docs/ANTHROPIC-SUBSCRIPTION.md)."""
    if _print_version_if_requested(argv):
        return

    from my_claude_code.cli.commands import anthropic_oauth_login as run_login

    run_login()


def _answer_installer_question(argv: Sequence[str] | None) -> bool:
    """Serve ``--report-holder``/``--stop-holder`` instead of starting a server.

    The official installer restarts exactly one server -- the one bound to the
    port of the configuration directory it installed for -- and it may not
    decide in PowerShell or in ``sh`` which processes MCC is allowed to stop.
    It asks the product instead, and the product answers here. See
    :mod:`my_claude_code.cli.installer_support`.

    A build that predates these flags exits non-zero from argument parsing
    instead, which is how a pinned ``--version`` install tells a newer
    installer to start nothing rather than to guess.
    """

    args = list(sys.argv[1:] if argv is None else argv)
    if not any(
        item == flag or item.startswith(f"{flag}=")
        for item in args
        for flag in ("--report-holder", "--stop-holder")
    ):
        return False

    from my_claude_code.cli.installer_support import run_installer_support

    status = run_installer_support(args)
    if status is None:
        return False
    if status != 0:
        raise SystemExit(status)
    return True


def _print_version_if_requested(argv: Sequence[str] | None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    if "--version" not in args:
        return False
    owner = owner_for_invocation()
    print(f"{owner.distribution} {package_version()}")
    return True


def compact_log(argv: Sequence[str] | None = None) -> None:
    """Compact the request log in place."""
    if _print_version_if_requested(argv):
        return

    _bootstrap_config_paths()

    from my_claude_code.cli.commands import compact_log as run_compaction

    run_compaction()


def rtk(argv: Sequence[str] | None = None) -> None:
    """Manage the RTK token optimizer."""
    from my_claude_code.cli.rtk_commands import rtk_command

    rtk_command(argv)


def apps(argv: Sequence[str] | None = None) -> None:
    """Configure desktop applications to route through this proxy."""
    from my_claude_code.cli.apps_command import apps_command

    apps_command(argv)


def migrate_config_dir(argv: Sequence[str] | None = None) -> int:
    """Move the legacy ``~/.mcc`` home to ``~/.mcc`` (opt-in)."""
    if _print_version_if_requested(argv):
        return 0

    from my_claude_code.cli.migrate_config_dir import main

    return main(list(argv) if argv is not None else None)
