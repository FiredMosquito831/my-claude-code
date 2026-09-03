"""Opt-in migration of the legacy ``~/.fcc`` home to the new ``~/.mcc`` default.

The resolution rule (see ``config.paths.resolve_config_dir``) never moves
anything on its own: an existing ``~/.fcc`` keeps working as the legacy home
until the user asks for this. That ask is ``mcc-migrate`` (and its ``fcc-migrate``
alias), and the ``POST /admin/api/migrate-config-dir`` dashboard button.

The move is a single same-volume ``os.rename(~/.fcc, ~/.mcc)``. On one volume
that is atomic and O(1) -- it either relocates the whole directory tree at once
or it raises before anything is moved, so there is no half-moved state to roll
back. On Windows the rename refuses with ``PermissionError`` if *any* file
inside the directory is held open by *any* process (the spec proved this
empirically), which is the safety check: a refusal means an MCC process is
still using the legacy home, and we report which processes those are instead of
moving anything.

The module is a thin, dependency-free command so it can run before the rest of
the application is composed; ``cli.entrypoints`` delegates to it.
"""

import datetime
import os
import platform
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from my_claude_code.config.paths import (
    MCC_CONFIG_DIRNAME,
    legacy_config_dir_path,
    retired_config_dir_path,
)

# Process names (image bases) that legitimately hold files inside the legacy
# config directory while they run. The tray holds ``desktop.lock`` for its whole
# lifetime; the server writes the request log and the current server log; a
# ``mcc-*`` launcher session reads its harness catalogue at startup; the
# deferred Windows updater stages into ``updates/``. We surface these on a
# refusal so the user knows exactly what to close. Matched case-insensitively
# against the command line / image name.
_MCC_PROCESS_HINTS = (
    "mcc-desktop",
    "mcc-server",
    "my-claude-code",
    "mcc-claude",
    "mcc-codex",
    "mcc-pi",
    "mcc-opencode",
    "mcc-kimi",
    "mcc-qwen",
    "mcc-crush",
    "mcc-cline",
    "mcc-aider",
    "mcc-droid",
    "mcc-gemini",
    "mcc-goose",
    "mcc-kilo",
    "mcc-opencode2",
    "mcc-commandcode",
    "python",
    "pythonw",
    "uv",
)


class MigrationError(RuntimeError):
    """The migration could not run; the message is suitable for the console."""


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _restore_text(new_home: Path, legacy_home: Path) -> str:
    date = _now_iso()
    if platform.system() == "Windows":
        move_back = f'Move-Item -Force "{new_home}" "{legacy_home}"'
    else:
        move_back = f'mv "{new_home}" "{legacy_home}"'
    return textwrap.dedent(
        f"""\
        My Claude Code moved its config directory on {date}.

            {legacy_home.name}  ->  {new_home.name}

        Nothing was copied and nothing was deleted. This directory holds only this
        note. To move the data back, close every MCC process (the tray, the server,
        and any coding agent) and run:

            {move_back}

        Version 6.40.0 and later will simply use the directory wherever it finds
        it; to pin the directory instead, set MCC_CONFIG_DIR to the path you want.
        """
    )


def _running_mcc_processes() -> list[str]:
    """Best-effort list of MCC processes that may hold the legacy home.

    Windows: ``tasklist`` gives us image names and PIDs; ``netstat -ano`` would
    tell us which PID holds a port but not which holds a file, and ``handle.exe``
    is not installed on this machine (the spec confirmed it). POSIX: we avoid
    ``lsof`` (not guaranteed present) and reason from ``/proc``-style process
    names instead. Either way this is a hint for the user, never a kill target.
    """

    if platform.system() == "Windows":
        return _running_mcc_processes_windows()
    return _running_mcc_processes_posix()


def _running_mcc_processes_windows() -> list[str]:
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"(could not list processes: {exc})"]
    if completed.returncode != 0:
        return [f"(tasklist exited {completed.returncode})"]
    hints: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        # CSV: "image","pid","session","session#","mem"
        parts = line.split('",')
        if not parts:
            continue
        image = parts[0].strip('"').lower()
        pid = parts[1].strip('"').strip() if len(parts) > 1 else "?"
        for hint in _MCC_PROCESS_HINTS:
            if image == hint.lower() or image.startswith(hint.lower()):
                hints.append(f"{image} (PID {pid})")
                break
    return hints


def _running_mcc_processes_posix() -> list[str]:
    hints: list[str] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        return [f"(could not read /proc: {exc})"]
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not cmdline:
            continue
        argv = cmdline.replace(b"\0", b" ").decode("utf-8", errors="replace")
        needle = argv.lower()
        if any(hint in needle for hint in _MCC_PROCESS_HINTS):
            hints.append(f"PID {entry.name}: {argv.strip()[:120]}")
    return hints


def _describe_holders() -> str:
    """Return a human-readable description of what is holding the legacy home."""

    hints = _running_mcc_processes()
    if not hints:
        return (
            "No MCC process was found holding it, but a file inside the legacy "
            "home is still open. Close the tray, the server, and any running "
            "coding agent, then re-run mcc-migrate."
        )
    listed = "\n".join(f"  - {hint}" for hint in hints[:20])
    return (
        "These MCC processes are likely holding files in the legacy home:\n"
        f"{listed}\n"
        "Close them and re-run mcc-migrate."
    )


def migrate_config_dir(*, force: bool = False) -> str:
    """Rename ``~/.fcc`` to ``~/.mcc`` atomically, or explain why not.

    Returns a short human-readable summary of the outcome (printed by the
    command). The rename only happens when the new home does not already exist
    and the rename succeeds; on any error nothing is moved and the message
    names the likely holders. After a success an empty ``~/.fcc-old/`` is
    created holding only ``RESTORE.txt``.
    """

    legacy_home = legacy_config_dir_path()
    new_home = Path.home() / MCC_CONFIG_DIRNAME
    retired_home = retired_config_dir_path()

    if not legacy_home.is_dir():
        if new_home.is_dir():
            return (
                f"Nothing to do: the legacy {legacy_home} is already gone and "
                f"{new_home} exists."
            )
        return (
            f"Nothing to do: neither {legacy_home} nor {new_home} exists. Run "
            f"mcc-init to create a fresh config, then mcc-server."
        )

    if new_home.exists():
        raise MigrationError(
            f"Refusing to migrate: {new_home} already exists. Both "
            f"{legacy_home} and {new_home} are present, so this is not a "
            f"first-time migration. Move or rename {new_home} out of the way "
            f"first, or just keep using it."
        )

    try:
        os.replace(legacy_home, new_home)
    except PermissionError as exc:
        # Windows: any open handle inside the directory makes the rename fail.
        # This is the safety check -- nothing was moved.
        logger.warning(
            "Config-dir migration refused: {} (a process still holds files in {})",
            exc,
            legacy_home,
        )
        return (
            f"Could not move {legacy_home} to {new_home}: a file inside the "
            f"legacy home is still open.\n\n{_describe_holders()}\n\nNothing was "
            f"moved. MCC keeps running from {legacy_home} for now. After closing "
            f"the processes above, re-run mcc-migrate."
        )
    except OSError as exc:
        logger.warning("Config-dir migration failed: {}", exc)
        return (
            f"Could not move {legacy_home} to {new_home}: {exc}.\n"
            f"Nothing was moved. Close every MCC process and re-run mcc-migrate."
        )

    if retired_home.exists():
        logger.info(
            "{} already exists; leaving it as-is and writing RESTORE.txt "
            "next to it would be redundant.",
            retired_home,
        )
        restore_note = (
            f"{legacy_home} -> {new_home} on {_now_iso()}. "
            f"{retired_home} already exists; see it for the original rollback note."
        )
    else:
        retired_home.mkdir(parents=True, exist_ok=True)
        (retired_home / "RESTORE.txt").write_text(
            _restore_text(new_home, legacy_home), encoding="utf-8"
        )
        restore_note = f"Rollback note written to {retired_home / 'RESTORE.txt'}."

    return (
        f"Moved {legacy_home} to {new_home}. Nothing was copied and nothing was "
        f"deleted.\n\n{restore_note}\n\nRestart the server yourself afterwards: "
        f"the running server is still using the old directory until you do."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """``mcc-migrate`` / ``fcc-migrate`` console entry point."""

    force = "--force" in (argv or ())
    try:
        summary = migrate_config_dir(force=force)
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
