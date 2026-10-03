"""Read and replace the settings files a save writes, never mistaking busy for empty.

A save rebuilds the managed ``.env`` from what it reads there plus the edit.
Until 7.69.7 a read that failed was answered with an empty mapping, so on
Windows, where another process replacing or reading the file makes a read or a
replace fail for a moment, a one-key pause save could rewrite the whole file
with that one key. Measured on 2026-10-01 with a second process replacing the
file in a loop: **18 of 351 reads came back empty** (51 per 1,000), and with a
second process reading it, **91 of 15,727 replaces failed**.

So a read whose answer a save will write back goes through
:func:`read_settings_bytes`, and the replace through
:func:`replace_settings_file`. Both retry a short, fixed number of times and
then refuse the save with :class:`SettingsFileBusyError`, before anything is
written. "Does not exist yet" (a fresh install) is still an empty file: only a
file that exists, or that this process has seen holding settings, is refused.

Before every replace the file as it was is kept, once, beside it as
``.env.previous`` (:func:`keep_previous_copy`) -- one copy, overwritten by the
next save, never by an empty or unreadable read.
"""

import os
import time
from pathlib import Path

from loguru import logger

#: The waits between attempts, in seconds: seven attempts, 315 ms of waiting in
#: all. Measured on NTFS on 2026-10-02 with a second process looping on a 24 KB
#: file as fast as it could: a read stayed refused for 1.3 ms at the median,
#: 6.8 ms at the 99th percentile and 20.7 ms at worst; a replace for 2.7 ms,
#: 15.4 ms and 43.8 ms. The first wait (5 ms) is about twice the median, each
#: wait doubles, and the total is seven times the longest refusal measured --
#: still well under the time a save already spends holding the event loop.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16)

#: Suffix of the one previous copy kept beside the managed file.
PREVIOUS_COPY_SUFFIX = ".previous"

#: Paths (normalised) that this process has read holding settings. A path in
#: here that is suddenly missing or empty is retried and then refused, because
#: that is what another program replacing it looks like; a path never seen is
#: a fresh install and reads as empty, as it always has.
_SEEN_WITH_SETTINGS: set[str] = set()


class SettingsFileBusyError(Exception):
    """A settings save refused before anything was written.

    ``str()`` is the sentence the dashboard shows. ``path`` is the file that
    could not be read or replaced, ``reason`` the plain cause.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"Not saved: {reason}, so nothing was changed. Try again. "
            f"(Settings file: {path})"
        )


def _sleep(seconds: float) -> None:
    """The wait between attempts. A module function so a test can count it."""

    time.sleep(seconds)


def _attempt_read(path: Path) -> tuple[bytes, int]:
    """One read: the bytes, and the size the open handle reports after it."""

    with open(path, "rb") as handle:
        raw = handle.read()
        return raw, os.fstat(handle.fileno()).st_size


def _attempt_replace(source: Path, target: Path) -> None:
    """One replace. A module function so a test can make it fail."""

    os.replace(source, target)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _has_settings(raw: bytes | None) -> bool:
    return bool(raw and raw.strip())


def note_settings_read(path: Path, raw: bytes | None) -> None:
    """Remember that ``path`` held settings, after any successful read of it.

    Only ever adds. Forgetting happens in one place -- a save refused because
    the file went missing or empty -- so that saving again after deleting the
    file on purpose starts a new one instead of being refused for ever.
    """

    if _has_settings(raw):
        _SEEN_WITH_SETTINGS.add(_key(path))


def forget_seen_settings_files() -> None:
    """Forget every path seen holding settings. For tests."""

    _SEEN_WITH_SETTINGS.clear()


def _refuse(path: Path, reason: str) -> SettingsFileBusyError:
    # The path and the cause, never a byte of the file.
    logger.warning("SETTINGS SAVE REFUSED: {} ({}); nothing was changed.", path, reason)
    return SettingsFileBusyError(path, reason)


def read_settings_bytes(path: Path) -> bytes | None:
    """Return the file's bytes, or ``None`` when it does not exist.

    Raises :class:`SettingsFileBusyError` when the file exists but cannot be
    read, changes size while it is read, or -- having held settings when this
    process last read it -- is missing or empty, on every one of the attempts.
    """

    seen = _key(path) in _SEEN_WITH_SETTINGS
    reason = ""
    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            raw, size = _attempt_read(path)
        except FileNotFoundError:
            if not seen:
                return None
            reason = "the settings file was there a moment ago and is missing now"
        except OSError as exc:
            if os.path.isdir(path):
                # Not a settings file at all -- a virtualenv named ``.env`` in
                # the folder the server runs from, say. Opening a directory
                # fails, and the lenient read always skipped it (``is_file``),
                # so this one does too rather than refusing every save.
                return None
            seen = True
            reason = (
                "the settings file was busy (another program was using it)"
                if isinstance(exc, PermissionError)
                else f"the settings file could not be read ({type(exc).__name__})"
            )
        else:
            if len(raw) != size:
                seen = True
                reason = "the settings file was being rewritten by another program"
            elif seen and not _has_settings(raw):
                reason = "the settings file was emptied by another program"
            else:
                note_settings_read(path, raw)
                return raw
        if delay is None:
            break
        _sleep(delay)
    if "missing" in reason or "emptied" in reason:
        # Refused once. If it was deleted or emptied on purpose, the next save
        # starts afresh rather than being refused until a restart.
        _SEEN_WITH_SETTINGS.discard(_key(path))
    raise _refuse(path, reason)


def replace_settings_file(source: Path, target: Path) -> None:
    """``os.replace(source, target)``, retried while Windows refuses it.

    A sharing violation or "access denied" on a file another process has open
    is ``PermissionError``; it is retried on the same schedule as a read and
    then refused with :class:`SettingsFileBusyError`, leaving ``target``
    exactly as it was. Any other error is raised as it is.
    """

    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            _attempt_replace(source, target)
            return
        except PermissionError:
            if delay is None:
                break
            _sleep(delay)
    raise _refuse(target, "the settings file was busy (another program was using it)")


def remove_quietly(path: Path) -> None:
    """Delete a staging file, retried, and never raise.

    It runs on the failure path, where an exception would replace the clear
    refusal with an internal error. A staging file that cannot be removed is
    named in the log -- it holds a copy of the settings -- and the next save
    overwrites it before using it.
    """

    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            if delay is None:
                break
            _sleep(delay)
    logger.warning(
        "SETTINGS SAVE: could not remove the staging file {}; the next save "
        "overwrites it.",
        path,
    )


def previous_copy_path(path: Path) -> Path:
    """Where the copy of ``path`` from before the last save is kept."""

    return path.with_name(path.name + PREVIOUS_COPY_SUFFIX)


def keep_previous_copy(path: Path, current: bytes) -> Path:
    """Write ``current`` -- the file as it is before this save -- beside it.

    Atomic (a staging file in the same directory, then a replace), never wider
    than the settings file's own permission bits, and refused rather than
    skipped: a save that cannot keep the copy does not happen. The caller only
    passes bytes it read successfully and that hold settings, so the copy is
    never replaced by an empty or unreadable read.
    """

    backup = previous_copy_path(path)
    staging = backup.with_name(backup.name + ".tmp")
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    remove_quietly(staging)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(staging, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(current)
    except OSError as exc:
        remove_quietly(staging)
        raise _refuse(
            path,
            f"a copy of the current settings could not be kept at {backup.name} "
            f"({type(exc).__name__})",
        ) from exc
    try:
        replace_settings_file(staging, backup)
    finally:
        remove_quietly(staging)
    return backup
