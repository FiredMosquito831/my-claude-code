"""What ``mcc-server`` does to its own configuration before it starts.

Until 6.65.0 a machine that had never run ``mcc-init`` had no ``.env`` at all,
and the server met the 6.30.0 refusal on its very first start: ``HOST`` was
``0.0.0.0``, the code default for ``ANTHROPIC_AUTH_TOKEN`` was ``""``, and
nothing on any install path -- not ``install.ps1``, not ``install.sh``, not the
npm hook, not the desktop shell -- ever ran the one command that would have
written the file. The product asked for a one-time user step that nobody was
told about, and the desktop app turned the resulting exit into a spinner.

So the server initialises its own configuration, in one place, before anything
reads it:

1. ``MCC_CONFIG_DIR`` outranks everything. When it is set, that directory is
   the home; it is created if absent and given a ``.env`` if it has none, and
   no migration is ever considered.
2. ``~/.mcc`` absent and ``~/.fcc`` present -> the one-time move, exactly as
   ``mcc-migrate`` performs it (a single atomic ``os.replace``, the
   ``~/.fcc-migrated.txt`` pointer, the ``~/.fcc-old/RESTORE.txt`` note).
   ``~/.fcc`` held open by anything -- the desktop app's ``desktop.lock``,
   another server, an editor -- refuses the start and names the holders rather
   than half-moving a directory holding the user's keys and history.
3. Both present -> ``~/.mcc`` wins, ``~/.fcc`` is named as an orphan and left
   exactly where it is. Nothing is ever merged and nothing is ever deleted.
4. Neither present -> ``~/.mcc`` is created.

and then, whichever home was chosen, a missing ``.env`` is written from the
shipped template with a token generated on this machine. An existing ``.env``
is never touched, so the 6.30.0 refusal still fires for the one case it was
built for: somebody who emptied their token while listening to the network.

This supersedes the 6.41.1 rule "``~/.fcc`` becomes ``~/.mcc`` only when the
user runs ``mcc-migrate``" (user decision, 2026-09-09 00:06). ``mcc-migrate``
remains, as the manual trigger of the same code.
"""

import os
import sys
from pathlib import Path

from loguru import logger

from my_claude_code.config.env_template import render_default_env
from my_claude_code.config.logging_config import append_to_server_log
from my_claude_code.config.paths import (
    CONFIG_DIR_ENV,
    FCC_ENV_FILENAME,
    display_path,
    legacy_config_dir_path,
    migrated_pointer_path,
    new_config_dir_path,
    reset_config_dir_cache,
    server_log_path,
    set_first_start_notice,
)
from my_claude_code.config.proxy_auth import (
    RUNTIME_PAGE_LABEL,
    RUNTIME_SECTION_LABEL,
)

from . import migrate_config_dir as migration


class ConfigHomeLocked(RuntimeError):
    """``~/.fcc`` could not be moved because something is holding it open."""


def _resolve_target(env: dict[str, str] | None = None) -> Path:
    """Return the directory that will hold the configuration, before any I/O."""

    source = os.environ if env is None else env
    override = source.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return new_config_dir_path()


def _migrate_legacy_home() -> str:
    """Perform the one-time ``~/.fcc`` -> ``~/.mcc`` move.

    Returns the notice to log and show on the dashboard. Raises
    ``ConfigHomeLocked`` when the move did not happen, with the holders named:
    the alternative is starting a server on a fresh empty home while the user's
    keys and 3 GB of history sit in a directory nothing will look at again.
    """

    legacy = legacy_config_dir_path()
    new_home = new_config_dir_path()

    try:
        summary = migration.migrate_config_dir()
    except migration.MigrationError as exc:
        summary = str(exc)

    # Did it actually move? The command reports a held directory by returning a
    # message rather than raising, so the filesystem is the verdict and the
    # message is the explanation.
    if legacy.exists() or not new_home.is_dir():
        raise ConfigHomeLocked(summary)

    return (
        f"Moved your configuration from {display_path(legacy)} to "
        f"{display_path(new_home)} (one atomic rename; nothing was copied, "
        f"merged or deleted). A note saying how to move it back is in "
        f"{display_path(migrated_pointer_path())}."
    )


def _write_default_env(env_path: Path) -> str:
    """Write the shipped template with a token generated for this machine."""

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(render_default_env(), encoding="utf-8")
    return (
        f"Wrote a default configuration to {display_path(env_path)} with a "
        f"proxy token generated for this machine, HOST=127.0.0.1 (this machine "
        f"only) and PORT=8082. Read the token on the dashboard under "
        f"{RUNTIME_PAGE_LABEL} -> {RUNTIME_SECTION_LABEL}."
    )


def ensure_config_home() -> str:
    """Make the config home exist and hold a ``.env``, before anything reads it.

    Returns the notice describing what was done, or an empty string when there
    was nothing to do (which is every start after the first). Raises
    ``ConfigHomeLocked`` when a legacy home could not be moved.
    """

    notices: list[str] = []
    target = _resolve_target()

    if target == new_config_dir_path():
        legacy = legacy_config_dir_path()
        if not target.is_dir() and legacy.is_dir():
            notices.append(_migrate_legacy_home())
        elif target.is_dir() and legacy.is_dir():
            # Not a failure and not a merge: say which one is in use and leave
            # the other alone. ``resolve_config_dir`` phrases the same fact for
            # the log; this is the one a first start puts on the dashboard.
            notices.append(
                f"Two config directories exist: {display_path(target)} is in "
                f"use and {display_path(legacy)} is ignored and left untouched. "
                f"Nothing is ever merged."
            )

    if not target.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        notices.append(f"Created the config directory {display_path(target)}.")

    env_path = target / FCC_ENV_FILENAME
    if not env_path.exists():
        notices.append(_write_default_env(env_path))

    notice = " ".join(notices)
    if notice:
        # The directory decision is cached for the life of the process, and it
        # was possibly cached before any of the above happened.
        reset_config_dir_cache()
        set_first_start_notice(notice)
        logger.info(notice)
    return notice


def ensure_config_home_or_exit() -> str:
    """``ensure_config_home``, turning a locked legacy home into a clean exit.

    The message goes to stderr as well as the log, because the desktop app
    reads the child's stderr and this is precisely the case where a user is
    owed a sentence instead of a spinner.
    """

    try:
        return ensure_config_home()
    except ConfigHomeLocked as exc:
        message = (
            f"Refusing to start: your configuration still lives in "
            f"{display_path(legacy_config_dir_path())} and it could not be "
            f"moved to {display_path(new_config_dir_path())}.\n\n{exc}\n\n"
            f"Nothing was moved. Close the processes named above (the desktop "
            f"app holds desktop.lock for its whole lifetime) and start the "
            f"server again."
        )
        logger.error(message)
        print(message, file=sys.stderr)
        # ...and into the file the desktop app's error page names. Nothing has
        # configured a file sink at this point in a start, so without this the
        # one refusal a user cannot act on without help is written nowhere.
        append_to_server_log(os.getenv("LOG_FILE", server_log_path()), "ERROR", message)
        raise SystemExit(1) from exc
