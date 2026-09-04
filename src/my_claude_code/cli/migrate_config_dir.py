"""Opt-in migration of the legacy ``~/.fcc`` home to the new ``~/.mcc`` default.

The resolution rule (see ``config.paths.resolve_config_dir``) never moves
anything on its own: an existing ``~/.fcc`` keeps working as the legacy home
until the user asks for this. That ask is ``mcc-migrate`` (and its
``fcc-migrate`` alias) -- and only that. There is no dashboard button and no
write route: relocating a user's keys and history is not something a page they
happen to have open, or a stray local POST, should be able to trigger.

The move is a single same-volume ``os.rename(~/.fcc, ~/.mcc)``. On one volume
that is atomic and O(1) -- it either relocates the whole directory tree at once
or it raises before anything is moved, so there is no half-moved state to roll
back.

Two guards sit in front of it, because a migration under a running server ends
with that server writing its cached ``~/.fcc/logs`` path straight back into a
freshly recreated legacy home:

* **Every OS:** an explicit liveness probe -- the tray's ``desktop.lock`` and a
  ``/health`` request to the configured port -- refuses while MCC is running.
  This used to be a Windows-only guarantee that the docs stated unconditionally;
  on POSIX ``os.rename`` succeeds with open handles, so the probe is what makes
  the promise true there.
* **Windows, additionally:** the rename itself refuses with ``PermissionError``
  if *any* file inside the directory is held open by *any* process, and we
  report which MCC processes those are instead of moving anything.

The module is a thin, dependency-free command so it can run before the rest of
the application is composed; ``cli.entrypoints`` delegates to it.
"""

import datetime
import os
import platform
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from my_claude_code.config.paths import (
    DESKTOP_LOCK_FILENAME,
    FCC_ENV_FILENAME,
    legacy_config_dir_path,
    new_config_dir_path,
    retired_config_dir_path,
)

#: Seconds allowed for the ``/health`` liveness probe. The server is on
#: loopback, so anything that has not answered by now is not answering.
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0

# What actually holds files inside the legacy config directory: the tray holds
# ``desktop.lock`` for its whole lifetime; the server writes the request log and
# the current server log; an ``mcc-*``/``fcc-*`` launcher session reads its
# harness catalogue at startup; the deferred Windows updater stages into
# ``updates/``. Every one of those runs as a console script whose image name (or
# POSIX command line) starts with ``mcc-``/``fcc-``, or names the package.
#
# 6.40.0 also listed ``python``, ``pythonw`` and ``uv`` here, which made the
# refusal name every unrelated interpreter on the machine -- and a list a user
# is told to "close and re-run" is a list they may act on, so it has to be
# right. A bare interpreter running MCC in-process is still matched by
# ``my_claude_code`` on POSIX (where we see the full command line); on Windows
# ``tasklist`` gives only the image name, so such a process goes unnamed rather
# than being wrongly accused. The rename's own refusal is the guarantee; this
# list is only the explanation of it.
_MCC_PROCESS_PREFIXES = ("mcc-", "fcc-", "my-claude-code", "free-claude-code")
_MCC_PROCESS_SUBSTRINGS = (
    "mcc-",
    "fcc-",
    "my_claude_code",
    "my-claude-code",
    "free_claude_code",
    "free-claude-code",
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
    """Return the rollback note left in ``~/.fcc-old``.

    The command in it must **fail** if the legacy home has come back, and it
    comes back easily: a server still running from before the migration holds a
    cached ``~/.fcc/logs`` path and recreates the directory the moment it next
    configures logging. Both of the obvious commands do the wrong thing there --
    ``Move-Item -Force "<new>" "<legacy>"`` and ``mv "<new>" "<legacy>"`` move
    the source *inside* an existing target, silently nesting the data as
    ``~/.fcc/.mcc`` and leaving two half-configs and no error message. So the
    note tests for the target first and refuses out loud instead.
    """

    date = _now_iso()
    if platform.system() == "Windows":
        move_back = (
            f'if (Test-Path "{legacy_home}") '
            f'{{ throw "{legacy_home} exists again - inspect it first" }} '
            f'else {{ Move-Item "{new_home}" "{legacy_home}" }}'
        )
    else:
        # ``-T`` treats the destination as the name to create rather than a
        # directory to move into, so an existing target is an error, not a nest.
        move_back = f'[ ! -e "{legacy_home}" ] && mv -T "{new_home}" "{legacy_home}"'
    return textwrap.dedent(
        f"""\
        My Claude Code moved its config directory on {date}.

            {legacy_home.name}  ->  {new_home.name}

        Nothing was copied and nothing was deleted. This directory holds only this
        note. To move the data back, close every MCC process (the tray, the server,
        and any coding agent) and run:

            {move_back}

        That command deliberately refuses if {legacy_home} exists again. A server
        left running across the migration recreates it, because it caches its log
        path at startup. If that happened: stop the server, look at what is inside
        the recreated directory, remove it if it holds nothing but an empty
        ``logs/``, and run the command again.

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
        if image.startswith(_MCC_PROCESS_PREFIXES):
            hints.append(f"{image} (PID {pid})")
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
        if any(hint in needle for hint in _MCC_PROCESS_SUBSTRINGS):
            hints.append(f"PID {entry.name}: {argv.strip()[:120]}")
    return hints


def _read_env_setting(env_path: Path, name: str) -> str | None:
    """Return one ``KEY=value`` from a ``.env``, or ``None``.

    A deliberately tiny reader. Building a real ``Settings`` here would load the
    directory we are about to rename, and the only thing we need is the port to
    knock on. The last assignment wins, matching dotenv semantics.
    """

    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    value: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, candidate = line.partition("=")
        if key.strip().upper() != name:
            continue
        value = candidate.split(" #")[0].strip().strip('"').strip("'")
    return value or None


def _configured_port(legacy_home: Path) -> int | None:
    """The port a server started from ``legacy_home`` would be listening on."""

    raw = _read_env_setting(legacy_home / FCC_ENV_FILENAME, "PORT")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            logger.debug("Ignoring unparseable PORT={} in {}", raw, legacy_home)
    try:
        from my_claude_code.config.settings import Settings

        default = Settings.model_fields["port"].default
    except Exception:  # pragma: no cover - only if Settings stops importing
        return None
    return default if isinstance(default, int) else None


def _mcc_is_running(legacy_home: Path) -> str:
    """Return why MCC still looks alive, or ``""`` when it does not.

    Two independent signals, both cheap and both read-only:

    * the tray's ``desktop.lock`` -- if it exists and we cannot take the lock,
      a tray owns it. We never create the file, because creating anything
      inside a directory we are about to rename is exactly the wrong move;
    * a ``/health`` request to the port the legacy ``.env`` configures. The
      body has to say ``healthy``, so an unrelated program squatting on the
      port does not block a migration.

    On Windows the rename would refuse anyway; on POSIX ``os.rename`` succeeds
    with every handle in the directory still open, and the running server then
    recreates ``~/.fcc/logs`` from its cached path. This probe is what makes
    "it refuses while MCC is running" true on both.
    """

    lock_path = legacy_home / DESKTOP_LOCK_FILENAME
    if lock_path.is_file():
        from my_claude_code.core.interprocess_lock import InterprocessFileLock

        lock = InterprocessFileLock(lock_path)
        if not lock.acquire():
            return f"the desktop tray still holds {lock_path}"
        lock.release()

    port = _configured_port(legacy_home)
    if port is None:
        return ""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health",
            timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
        ) as response:
            body = response.read(256).decode("utf-8", errors="replace")
    except urllib.error.URLError, OSError, ValueError:
        return ""
    if "healthy" not in body:
        return ""
    return f"an MCC server is answering on http://127.0.0.1:{port}/health"


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


def migrate_config_dir() -> str:
    """Rename ``~/.fcc`` to ``~/.mcc`` atomically, or explain why not.

    Returns a short human-readable summary of the outcome (printed by the
    command). The rename only happens when the new home does not already exist,
    no MCC process is running, and the rename succeeds; on any error nothing is
    moved and the message names the likely holders. After a success an empty
    ``~/.fcc-old/`` is created holding only ``RESTORE.txt``.

    There is no ``force``. 6.40.0 accepted ``--force`` on the command line and
    in the dashboard payload and read it nowhere -- a user told "``~/.mcc``
    already exists, refusing" was offered an escape hatch that did nothing.
    There is no safe automatic behaviour to hang on such a flag either: when
    both directories exist, only the user knows which one holds the data they
    want, and merging is the one thing this command promises never to do.
    """

    legacy_home = legacy_config_dir_path()
    new_home = new_config_dir_path()
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

    blocker = _mcc_is_running(legacy_home)
    if blocker:
        raise MigrationError(
            f"Refusing to migrate: {blocker}. Stop the server and the tray "
            f"first, then re-run mcc-migrate. Nothing was moved -- a rename "
            f"under a running server leaves it writing to a path that no "
            f"longer exists and recreating {legacy_home} behind you."
        )

    try:
        # Re-checked here, in the same step as the rename, rather than only at
        # the top of the function: POSIX ``rename`` silently replaces an
        # *empty* target directory, so a ``~/.mcc`` created between the two
        # would vanish without a word. There is no portable atomic
        # rename-if-absent (``RENAME_NOREPLACE`` is Linux-only), and reserving
        # the name with ``mkdir(exist_ok=False)`` would be worse: a crash
        # between the reservation and the rename leaves an empty ``~/.mcc``,
        # which then wins resolution forever. A non-empty target is refused by
        # the rename itself on every platform, so what remains uncovered is an
        # empty directory created inside a few microseconds -- and swallowing
        # an empty ``~/.mcc`` produces exactly the state the user asked for.
        if new_home.exists():
            raise MigrationError(
                f"Refusing to migrate: {new_home} appeared while this command "
                f"was running. Nothing was moved."
            )
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

    try:
        summary = migrate_config_dir()
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
