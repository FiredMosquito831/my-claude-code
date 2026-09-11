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
#:
#: 6.72.0 added ``staging``, ``swapping`` and ``rolling-back``. An update no
#: longer replaces the live environment in place: it builds the new one BESIDE
#: it (``staging``), runs it once to prove it works (``verifying``), and only
#: then exchanges the two directories (``swapping``). ``rolling-back`` is the
#: stage between a cutover whose ``/health`` never answered and the
#: ``recovered`` that follows it.
#:
#: 6.73.0 added ``episode``. It is not a stage of the work: it is the marker
#: record every writer appends FIRST, naming itself and its pid, because from
#: 6.73.0 nobody truncates this file. Until then every writer opened it with a
#: truncate, so at 15:04 on 2026-09-11 a hand-run ``install.ps1`` erased the
#: entire record of the update helper that had finished two minutes earlier --
#: while a window was supposed to be reading it. The marker is what lets a
#: reader that arrives a minute late tell where the current episode begins.
UPDATE_PROGRESS_STAGES: tuple[str, ...] = (
    "episode",
    "waiting-for-parent",
    "staging",
    "stopping",
    "installing",
    "verifying",
    "swapping",
    "starting",
    "handing-off",
    "rolling-back",
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
    # Rank 0 on purpose: the marker is written before any work, and a writer's
    # monotonic guard treats rank 0 as "keep the rank you had", so a marker
    # never blocks the stage that follows it and never moves an episode
    # backwards.
    "episode": 0,
    "waiting-for-parent": 1,
    "staging": 2,
    "stopping": 3,
    "installing": 4,
    "verifying": 5,
    "swapping": 6,
    "starting": 7,
    "handing-off": 7,
    "rolling-back": 8,
    "done": 9,
    "failed": 9,
    "recovered": 9,
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

#: Where an update builds the new environment, and where it keeps the old one.
#:
#: Both are SIBLINGS of uv's tools root, never children of it. That is a
#: measured requirement, not a preference: a directory inside the tools root
#: whose name does not normalise to a valid package name makes ``uv tool list``
#: fail outright --
#:
#:     error: Not a valid package or extra name: ".mcc-previous".
#:
#: -- and list NOTHING, which is strictly worse than the malformed-tool
#: warnings the existing ``my-claude-code.old-<stamp>`` directories produce
#: (those normalise to ``my-claude-code-old-<stamp>``, which is valid, so uv
#: merely skips them). A sibling is invisible to uv, is on the same volume as
#: the tools root, and so keeps the swap a rename. Measured on uv 0.11.21,
#: 2026-09-11.
STAGING_ENV_DIRNAME = ".mcc-staging"
PREVIOUS_ENV_DIRNAME = ".mcc-previous"

#: How many previous environments to keep. Exactly one: it is the rollback, and
#: a second one is only disk (decision Q5). Swept after ``/health`` answers, so
#: the copy being kept is never the one the running server came from.
PREVIOUS_ENVS_KEPT = 1

#: How many installer transcripts to keep. They are the only record of what an
#: update did, and they are small, but one per update is unbounded.
INSTALL_TRANSCRIPTS_KEPT = 5

#: How long the cutover waits for the new server to answer ``/health`` before
#: it puts the previous environment back.
#:
#: The desktop shell's own start budget is 15 s x 3 attempts (6.58.1's
#: ``server_start_retries``), and under ``--no-restart`` it is the shell, not
#: the helper, that starts the server -- so the gate has to outlast the shell's
#: whole ladder plus the ten-second tick that begins it, or a slow first start
#: would be rolled back as a failure. 90 s covers 10 + 45 and leaves margin for
#: a cold interpreter (measured cold ``--print-status``: 18 s).
UPDATE_HEALTH_GATE_SECONDS = 90.0

#: How often the cutover asks.
UPDATE_HEALTH_POLL_SECONDS = 1.0


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
#: The marker record that opens an episode (6.73.0). Every writer appends one
#: before it does anything else, and no writer truncates the receipt any more.
EPISODE_MARKER_STAGE = "episode"
EPISODE_MARKER_MESSAGE = "An update started."

#: The one lock both update paths take before they write the receipt or the
#: tool environment. ``<config dir>/updates/update.lock``, holding the owner's
#: pid, the second it started and which script it is, so a dead owner's lock is
#: reclaimed rather than waited on until the end of the day.
#:
#: Until 6.73.0 there was none. The hand-run installer and the dashboard's
#: helper wrote the same two files and installed into the same uv tool
#: directory with no coordination beyond an advisory pid check, which is how
#: two installs wrote over each other's environment on 2026-09-09 and how the
#: helper's whole record was erased on 2026-09-11.
UPDATE_LOCK_FILENAME = "update.lock"

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


def update_lock_path() -> Path:
    """Where both update paths take the one exclusive lock."""

    return config_dir_path() / UPDATE_STAGE_DIRNAME / UPDATE_LOCK_FILENAME


def read_update_lock(path: Path | None = None) -> dict[str, Any] | None:
    """The lock owner's record, or ``None`` when the lock is not held.

    A lock file that cannot be parsed is reported as held by an unknown owner
    rather than as absent: "I could not read it" must never be the reading that
    licenses a second installer.
    """

    target = update_lock_path() if path is None else path
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        parsed = json.loads(raw.strip() or "{}")
    except ValueError:
        return {"pid": 0, "source": "an earlier installer", "started_display": ""}
    return parsed if isinstance(parsed, dict) else None


def lock_owner_is_alive(record: dict[str, Any] | None) -> bool:
    """Whether the lock's owner is still running.

    ``None`` from :func:`_pid_is_running` -- the pid could not be checked at all
    -- counts as alive, for the same reason it does for the helper gate: an
    unknown pid read as "gone" is what starts a second installer.
    """

    if not record:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        # No usable pid. Judge by age, exactly as a receipt from an older
        # build is judged, so a lock left by a crashed writer is reclaimable.
        started = record.get("started_at")
        if isinstance(started, int | float):
            return (time.time() - float(started)) < STALE_HELPER_SECONDS
        return False
    return _pid_is_running(pid) is not False


def describe_lock_owner(record: dict[str, Any] | None) -> str:
    """The sentence a second installer prints when it finds the lock held."""

    if not record:
        return "an update is already running"
    pid = record.get("pid")
    started = record.get("started_display") or ""
    source = record.get("source") or "an update"
    if isinstance(pid, int) and pid > 0 and started:
        return f"an update is already running (pid {pid}, started {started})"
    if isinstance(pid, int) and pid > 0:
        return f"an update is already running (pid {pid})"
    return f"an update is already running ({source})"


def read_update_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Every parseable record in the receipt, oldest first.

    Torn lines are skipped rather than ending the read: the file is appended to
    by a detached process while this runs.
    """

    target = update_progress_path() if path is None else path
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def last_episode_records(path: Path | None = None) -> list[dict[str, Any]]:
    """The records of the MOST RECENT episode only.

    From 6.73.0 the receipt is appended to and never truncated, so it holds
    every episode this machine has run. A reader that wants "what is happening
    now" wants the records after the last ``episode`` marker; a receipt written
    by an older build has no marker at all, and then the whole file is the one
    episode it recorded, which is exactly what that build meant.
    """

    records = read_update_records(path)
    for index in range(len(records) - 1, -1, -1):
        if str(records[index].get("stage") or "") == EPISODE_MARKER_STAGE:
            return records[index:]
    return records


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
