"""Shared primitives for the multi-account OAuth stores.

Both OAuth providers keep one file per provider holding an ``accounts`` list,
and both had to answer the same four questions to get there: how an account is
identified, how the legacy single-account document becomes a list without
losing the file, how the file is backed up exactly once, and how a refreshed
token is written back to the file it was imported from without ever writing an
older token over a newer one.

The two file *shapes* differ -- ``anthropic_oauth.json`` is a flat camelCase
object, ``auth/chatgpt-oauth.json`` is ``{version, tokens}`` -- so the shape
stays with each provider. What lives here is everything that is the same:

* :class:`StoredAccount`, the record both stores persist beside their tokens;
* :func:`backup_once`, the ``.bak-<epoch>`` taken before the first rewrite;
* :func:`storage_write_lock`, Claude Code's own ``~/.claude/.storage-write``
  lock, spoken with its own parameters so the other participant is not
  surprised by us;
* :func:`monotonic_write_allowed`, the race rule (C8) that makes write-back a
  protocol rather than a hope.

Nothing here imports either provider, so neither provider has to import the
other to share it.
"""

import contextlib
import os
import secrets
import shutil
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

#: Where an account came from. ``mcc`` means MCC signed it in itself and owns
#: no file but its own store; the other two name a file on this machine that
#: another client owns and that write-back may update.
ORIGIN_MCC = "mcc"
ORIGIN_CLAUDE_CODE = "claude-code"
ORIGIN_CODEX = "codex"

#: The suffix stem of the one-time migration backup. The epoch is appended so
#: the copy names the moment the shape changed; the "once" test is a glob over
#: this stem rather than a fixed name, because a fixed name would be silently
#: overwritten by a second migration.
BACKUP_STEM = ".bak-"

#: Claude Code 2.1.278 takes a ``proper-lockfile`` lock on this path before
#: every credential write (bundle offset 197903482), with
#: ``retries: {retries: 10, minTimeout: 100, maxTimeout: 1000}`` and
#: ``stale: 15000``. ``proper-lockfile`` implements a lock on ``X`` as a
#: **directory** at ``X.lock`` whose mtime is the liveness heartbeat, so
#: joining the protocol means creating and removing that directory with the
#: same staleness window -- not inventing a second convention beside it.
STORAGE_WRITE_LOCK_NAME = ".storage-write"
STORAGE_WRITE_LOCK_SUFFIX = ".lock"
STORAGE_LOCK_RETRIES = 10
STORAGE_LOCK_MIN_TIMEOUT_SECONDS = 0.1
STORAGE_LOCK_MAX_TIMEOUT_SECONDS = 1.0
STORAGE_LOCK_STALE_SECONDS = 15.0


class OAuthStorageLockUnavailable(Exception):
    """Raised when the credential-write lock could not be acquired.

    Deliberately not a failure of anything: the only correct response is to
    skip the write, which is what every caller does.
    """


@dataclass(frozen=True, slots=True)
class StoredAccount:
    """One OAuth account's non-token record, as persisted.

    ``data`` holds the provider-specific token block verbatim, so this module
    never has to know what a token looks like.
    """

    id: str
    origin: str = ORIGIN_MCC
    origin_path: str = ""
    write_back: bool = False
    added_at: str = ""
    #: The 1-based position the account held **when it was added**. Stored and
    #: never recomputed, so removing an earlier account does not renumber the
    #: default name of every account after it.
    ordinal: int = 1
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def owns_a_source_file(self) -> bool:
        """Whether write-back has a file to own at all.

        An account MCC signed in itself has no source but its own store, so
        write-back is not merely off for it -- there is nothing to write to.
        """

        return self.origin in (ORIGIN_CLAUDE_CODE, ORIGIN_CODEX) and bool(
            self.origin_path
        )


def now_iso() -> str:
    """The timestamp both stores write into ``added_at``."""

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


#: The prefix every minted id carries, so "did we make this up?" is a question
#: the record can answer about itself rather than one the caller has to track.
SYNTHETIC_ID_PREFIX = "local-"


def is_synthetic_account_id(account_id: str) -> bool:
    """Whether this id was minted here rather than reported by the provider."""

    return not account_id or account_id.startswith(SYNTHETIC_ID_PREFIX)


def synthetic_account_id(prefix: str = SYNTHETIC_ID_PREFIX) -> str:
    """Mint an account id for a credential that carries no identity.

    Minted once, persisted, and never recomputed. Fingerprinting the refresh
    token would be tempting and wrong: it rotates, and the name would detach
    from the account on the first refresh.
    """

    return f"{prefix}{secrets.token_hex(8)}"


def backup_once(path: Path, *, now: float | None = None) -> Path | None:
    """Copy ``path`` to ``<name>.bak-<epoch>``, the first time only.

    Returns the backup path (existing or new), or ``None`` when there was
    nothing to copy or the copy failed. A failed backup is not fatal: it must
    not cost the user a working credential, and the migration it guards is
    additive.
    """

    if not path.is_file():
        return None
    existing = sorted(path.parent.glob(f"{path.name}{BACKUP_STEM}*"))
    if existing:
        return existing[0]
    stamp = int(time.time() if now is None else now)
    target = path.with_name(f"{path.name}{BACKUP_STEM}{stamp}")
    try:
        shutil.copyfile(path, target)
    except OSError as error:  # pragma: no cover - defensive
        logger.warning("Could not back up {} before migrating it: {}", path, error)
        return None
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    logger.info("Backed up {} to {} before migrating it to a list.", path, target.name)
    return target


def has_backup(path: Path) -> bool:
    """Whether the one-time migration backup of ``path`` already exists."""

    return any(path.parent.glob(f"{path.name}{BACKUP_STEM}*"))


# ---------------------------------------------------------------------------
# Claude Code's credential-write lock
# ---------------------------------------------------------------------------


def storage_write_lock_path(directory: Path) -> Path:
    """The directory ``proper-lockfile`` creates to lock ``.storage-write``."""

    return directory / (STORAGE_WRITE_LOCK_NAME + STORAGE_WRITE_LOCK_SUFFIX)


def _break_if_stale(lock_path: Path, *, stale_seconds: float) -> bool:
    """Remove a lock whose owner has stopped heartbeating. Its owner's rule."""

    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    if age < stale_seconds:
        return False
    try:
        lock_path.rmdir()
    except OSError:  # pragma: no cover - lost the race to break it
        return False
    logger.info(
        "Broke a stale Claude Code credential-write lock at {} ({:.0f}s old).",
        lock_path,
        age,
    )
    return True


@contextlib.contextmanager
def storage_write_lock(
    directory: Path,
    *,
    retries: int = STORAGE_LOCK_RETRIES,
    min_timeout: float = STORAGE_LOCK_MIN_TIMEOUT_SECONDS,
    max_timeout: float = STORAGE_LOCK_MAX_TIMEOUT_SECONDS,
    stale_seconds: float = STORAGE_LOCK_STALE_SECONDS,
    sleep: Any = time.sleep,
) -> Iterator[Path]:
    """Hold Claude Code's own credential-write lock, or refuse to write.

    Raises :class:`OAuthStorageLockUnavailable` when the retry budget runs out.
    A lock MCC cannot acquire is a **skipped** write -- never a forced one:
    the whole point of joining an existing protocol is that the other
    participant's write is as real as ours.

    The lock is always released, including when the body raises, because a
    lock left behind would block the user's real client for ``stale`` seconds
    on every subsequent write.
    """

    lock_path = storage_write_lock_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    delay = min_timeout
    # ``waited`` counts only the times this actually queued behind a *live*
    # owner. Breaking a stale lock must not spend a retry: with ``retries=0``
    # it would otherwise refuse a lock that was already abandoned, which is
    # the opposite of what the stale window is for. Two stale-breaks in a row
    # are bounded anyway -- each removes the directory, so the very next
    # ``mkdir`` either wins or finds a genuinely new owner.
    waited = 0
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            if _break_if_stale(lock_path, stale_seconds=stale_seconds):
                continue
            if waited >= retries:
                raise OAuthStorageLockUnavailable(
                    f"Could not acquire {lock_path} after {retries} retries"
                ) from None
            waited += 1
            sleep(delay)
            delay = min(delay * 2, max_timeout)
        except OSError as error:  # pragma: no cover - defensive
            raise OAuthStorageLockUnavailable(str(error)) from error
    try:
        yield lock_path
    finally:
        with contextlib.suppress(OSError):
            lock_path.rmdir()


# ---------------------------------------------------------------------------
# The race rule (C8)
# ---------------------------------------------------------------------------


def monotonic_write_allowed(
    *,
    target_expires_at: int | None,
    target_refresh_expires_at: int | None,
    ours_expires_at: int | None,
    ours_refresh_expires_at: int | None,
) -> bool:
    """Whether writing our token over the target's would move time forwards.

    Last-writer-wins with a monotonicity guard. The target is re-read inside
    the lock immediately before the write; if its stored expiry is **greater
    than or equal to** ours, the other client refreshed more recently and its
    token is the live one, so the write is skipped. Ties break on the refresh
    token's expiry, because two refreshes seconds apart routinely land on the
    same access-token expiry while the refresh token still moved.

    A target that states no expiry at all cannot be compared, so it is
    overwritten: it is either a file we wrote before this release or one
    another client left half-written, and in both cases a token whose expiry
    we know is better than one we do not.
    """

    if target_expires_at is None:
        return True
    if ours_expires_at is None:
        # Ours says nothing and theirs does. Refuse: never trade a known
        # expiry for an unknown one.
        return False
    if ours_expires_at > target_expires_at:
        return True
    if ours_expires_at < target_expires_at:
        return False
    if target_refresh_expires_at is None or ours_refresh_expires_at is None:
        return False
    return ours_refresh_expires_at > target_refresh_expires_at


def normalise_epoch_seconds(value: object) -> int | None:
    """Read an epoch stamp in whichever unit it happened to be written.

    Claude Code writes milliseconds; the token endpoints answer seconds. The
    same threshold both providers already use (anything past year ~2286 in
    seconds is really milliseconds) keeps one rule in one place.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value / 1000) if value > 10_000_000_000 else int(value)


def account_record_fields(
    entry: Mapping[str, Any],
    *,
    id_key: str = "id",
    origin_key: str = "origin",
    origin_path_key: str = "originPath",
    write_back_key: str = "writeBack",
    added_at_key: str = "addedAt",
    ordinal_key: str = "ordinal",
) -> dict[str, Any]:
    """Read the record fields shared by both stores out of one entry."""

    origin = entry.get(origin_key)
    ordinal = entry.get(ordinal_key)
    return {
        "id": str(entry.get(id_key) or "").strip(),
        "origin": origin if isinstance(origin, str) and origin else ORIGIN_MCC,
        "origin_path": str(entry.get(origin_path_key) or ""),
        "write_back": bool(entry.get(write_back_key, False)),
        "added_at": str(entry.get(added_at_key) or ""),
        "ordinal": int(ordinal) if isinstance(ordinal, int) and ordinal > 0 else 1,
    }
