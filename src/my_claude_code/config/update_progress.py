"""The update helper's liveness receipt, read by everyone who must not race it.

On Windows an update is applied by a detached PowerShell helper that outlives
the server it replaces (``application/release_updates.py`` generates it). While
that helper runs, ``uv`` is rewriting the very launcher shims the desktop shell
calls -- so for a few seconds ``mcc-desktop`` is genuinely not there, the
shell's status ladder reads ``NotInstalled``, and the shell, by design, starts
an install *of its own*. Two installers, one tool directory. Measured on
2026-09-07: the helper failed all five of its attempts while the shell's
install succeeded at 23:25:43, and the helper's "start the server again" step
never ran because it had already written a failure.

The fix needs one fact, available to three different readers (the helper's own
Python parent, ``mcc-desktop --print-status``, and the Rust shell): *is an
update helper alive right now?* This module is that fact for the Python side.

It lives in ``config`` because ``cli`` may not import ``application``
(``tests/contracts/test_import_boundaries.py``) and ``cli.desktop_status`` has
to publish the answer. ``application.release_updates`` re-exports the names it
used to own, so nothing outside had to learn a new import.

The receipt is JSON *lines*, appended by a detached process that may be killed
at any point, so the last parseable line wins and a torn trailing line costs
one stale record rather than the whole file.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from my_claude_code.config.paths import config_dir_path

#: Where the helper stages an update, inside the configuration directory.
UPDATE_STAGE_DIRNAME = "updates"

#: One JSON object per line. See the module docstring for why it is lines.
UPDATE_PROGRESS_FILENAME = "progress.json"

#: The stages the helper reports, in the order it reports them. Readers show
#: whatever string they find rather than switching on this tuple -- a second
#: copy of the list in the desktop shell would be a second source of truth --
#: but the sequence is pinned by a test so a stage cannot silently stop being
#: written. ``recovered`` is 6.58.3's: an install that failed and put the
#: previously installed server back.
#:
#: 6.71.0 added ``stopping``, ``verifying`` and ``handing-off`` so the window
#: can draw a timeline rather than a single sentence. ``handing-off`` is the
#: honest name for what the helper does under ``--no-restart``: until 6.71.0 it
#: wrote ``starting`` there, from a ``$noRestart`` it read one line before the
#: line that assigns it, so a receipt claimed the helper was starting a server
#: it had been told not to start (spec F6, seen in the live 6.66.1 receipt).
UPDATE_PROGRESS_STAGES: tuple[str, ...] = (
    "waiting-for-parent",
    "stopping",
    "installing",
    "verifying",
    "starting",
    "handing-off",
    "done",
    "failed",
    "recovered",
)

#: How far through an episode each stage is. Stages are **monotonic**: a writer
#: never goes back to an earlier one, so a window can draw the sequence as a
#: timeline and a reader can tell "still installing" from "installed, starting"
#: without guessing. Terminal stages share the last rank, because an episode
#: ends exactly once and ``failed`` may be followed by ``recovered``.
UPDATE_PROGRESS_STAGE_ORDER: dict[str, int] = {
    "waiting-for-parent": 1,
    "stopping": 2,
    "installing": 3,
    "verifying": 4,
    "starting": 5,
    "handing-off": 5,
    "done": 6,
    "failed": 6,
    "recovered": 6,
}

#: The stages that end an episode. ``handing-off`` is deliberately absent: the
#: helper is still running when it writes one, and reading it as terminal would
#: reopen the "one installer at a time" gate a beat too early.
UPDATE_TERMINAL_STAGES: frozenset[str] = frozenset({"done", "failed", "recovered"})

#: Basename prefix of the installer transcript the helper tees ``uv`` into,
#: beside the receipt: ``<config>/updates/install-<stamp>.log``. Every progress
#: record names the file in its ``log`` field, so a reader never has to guess
#: the stamp -- and the desktop window tails it while the install happens,
#: which is the whole of "see everything happening" (decision Q2).
INSTALL_LOG_PREFIX = "install-"
INSTALL_LOG_SUFFIX = ".log"

#: Set to an existing transcript to make ``scripts/install.ps1`` and
#: ``scripts/install.sh`` append to it rather than open one of their own.
#:
#: One episode, one transcript. A caller that already owns one -- the deferred
#: helper, whose recovery ladder can run the hand-run installer -- would
#: otherwise leave the window tailing whichever of two files it happened to be
#: told about, with half the story in the other. Declared here rather than only
#: in the scripts because this module is where every reader of the receipt
#: looks, and because a name documented in USAGE.md has to exist somewhere the
#: docs-drift guard can find it.
INSTALL_LOG_ENV = "MCC_INSTALL_LOG"


def stage_rank(stage: str) -> int:
    """How far through an episode ``stage`` is; ``0`` for one we do not know.

    An unknown stage ranks below every known one so that a reader written
    before a stage existed still orders the stages it does know, and a writer's
    monotonic guard never *drops* a record it cannot place.
    """

    return UPDATE_PROGRESS_STAGE_ORDER.get(stage.strip(), 0)


def install_log_path(stamp: str) -> Path:
    """The installer transcript for the episode identified by ``stamp``."""

    return (
        config_dir_path()
        / UPDATE_STAGE_DIRNAME
        / f"{INSTALL_LOG_PREFIX}{stamp}{INSTALL_LOG_SUFFIX}"
    )


#: The exact sentence each writer of this receipt uses for a stage, so the two
#: writers cannot drift. ``application/release_updates.py`` generates the
#: deferred helper; ``scripts/install.ps1`` and ``scripts/install.sh`` are the
#: hand-run one-liner. Until 6.59.0 only the helper wrote a receipt at all, so
#: a user who ran the installer themselves -- which the reporter does, and did
#: at 01:10 on the day of the incident -- was invisible to every reader of this
#: file: the desktop shell saw no helper, decided nothing was in flight, and
#: was free to start an install of its own into the tool directory the
#: one-liner was writing. A contract test pins these strings against all three
#: scripts.
INSTALLING_MESSAGE = "Installing the new version."
INSTALL_DONE_MESSAGE = "The new version is installed."
INSTALL_FAILED_MESSAGE = "The install failed."

#: How far past its last receipt a helper is still believed to be working when
#: its process id cannot be checked at all. ``uv`` can spend minutes inside one
#: stage, and the helper is single-threaded PowerShell that cannot heartbeat
#: while it waits on that, so this has to cover a whole slow install. It is
#: only ever the fallback: when the pid can be checked, the pid decides.
STALE_HELPER_SECONDS = 900.0


def update_progress_path() -> Path:
    """Where the deferred helper appends its stage receipts."""

    return config_dir_path() / UPDATE_STAGE_DIRNAME / UPDATE_PROGRESS_FILENAME


def read_update_progress() -> dict[str, Any] | None:
    """The most recent stage the deferred helper reported, if any.

    The last parseable line wins. A trailing line that is still being written
    is skipped rather than treated as the end of the story, because the stage
    before it is still true.
    """

    try:
        raw = update_progress_path().read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in reversed(raw.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _pid_is_running(pid: int) -> bool | None:
    """Whether ``pid`` names a live process. ``None`` when it cannot be told.

    ``None`` is a real answer and not a failure: an unknown pid must never be
    read as "the helper is gone", because that is the reading that starts a
    second installer.
    """

    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return None
        if completed.returncode != 0:
            return None
        # tasklist answers a filter that matched nothing with a sentence on
        # stdout ("INFO: No tasks are running...") rather than a non-zero
        # status, so the pid has to be looked for in the row it would print.
        return f'"{pid}"' in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def helper_is_alive(record: dict[str, Any] | None) -> bool:
    """Whether ``record`` describes an update helper that is still working.

    Two independent conditions, and both must hold. ``helper_done`` is the
    helper's own word for "I have finished" -- written on the terminal stages
    -- and the process id is the check that survives a helper killed before it
    could write one. A record from a build that wrote neither is judged by its
    age alone, which is how a 6.58.2 helper mid-install is still respected by a
    6.58.3 reader.
    """

    if not record:
        return False
    if record.get("helper_done") is True:
        return False
    pid = record.get("helper_pid")
    if isinstance(pid, int):
        running = _pid_is_running(pid)
        if running is False:
            return False
        if running is True:
            return True
    started = record.get("started_at")
    if isinstance(started, int | float):
        return (time.time() - float(started)) < STALE_HELPER_SECONDS
    # No pid, no start time: a pre-6.58.3 helper. Anything but a terminal stage
    # is treated as in flight, which is the safe reading.
    stage = str(record.get("stage") or "")
    return stage not in UPDATE_TERMINAL_STAGES


def active_update() -> dict[str, Any] | None:
    """The record of an update helper that is running right now, or ``None``."""

    record = read_update_progress()
    return record if helper_is_alive(record) else None


def update_report() -> dict[str, Any] | None:
    """What ``--print-status`` publishes about an in-flight update.

    ``None`` when no helper is running, which is the ordinary case and the one
    a reader must handle first. Otherwise a small document a window can render
    without knowing anything about PowerShell: which stage, the helper's own
    sentence, the version it is installing, how long it has been at it, and --
    since 6.71.0 -- where the installer transcript it is writing right now
    lives, so a reader can show the thing itself rather than a summary of it.

    Frozen as informational and never required by the status contract (C9,
    decision Q7): the only transport for it is ``mcc-desktop --print-status``,
    which is the very command that cannot answer while an installer is
    replacing the environment. See ``cli/desktop_status.py``.
    """

    record = active_update()
    if record is None:
        return None
    started = record.get("started_at")
    elapsed = (
        max(0.0, time.time() - float(started))
        if isinstance(started, int | float)
        else None
    )
    recorded = record.get("elapsed_seconds")
    if elapsed is None and isinstance(recorded, int | float):
        elapsed = max(0.0, float(recorded))
    return {
        "stage": str(record.get("stage") or ""),
        "log": record.get("log") if isinstance(record.get("log"), str) else None,
        "message": record.get("message")
        if isinstance(record.get("message"), str)
        else None,
        "version": record.get("version")
        if isinstance(record.get("version"), str)
        else None,
        "helper_pid": record.get("helper_pid")
        if isinstance(record.get("helper_pid"), int)
        else None,
        "elapsed_seconds": elapsed,
    }
