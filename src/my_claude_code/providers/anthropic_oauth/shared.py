"""A Claude credential MCC shares with Claude Code: follow the file, never race it.

"If we share the credential with official Claude Code we shouldn't play with
it." (the user, 2026-09-27.) A credential read from Claude Code's
``.credentials.json`` -- by an Import or by the automatic fallback -- is
**shared**, and for a shared credential Claude Code's file is the truth:

* every use compares the file's ``(mtime_ns, size)`` with the stamp MCC last
  read it at -- Claude Code's own lazy rule -- and re-reads and adopts the
  file's token when it moved (``shared:adopted``), checking
  ``~/.claude.json`` ``oauthAccount.accountUuid`` for an account switch
  (``shared:identity-changed``);
* a half-written or unreadable file keeps the cached token for that call and
  is never read as a logout;
* nothing refreshes it early -- no 120 s leeway, no background task, no
  dashboard refresh while it is valid;
* it is refreshed only when the access token has expired (or a 401 came back
  within 120 s of expiry) **and** a real client request needs it **and**
  write-back is possible, under Claude Code's own refresh lock, with the new
  token written back by compare-and-swap before the lock is released;
* a refresh token that is past its stated expiry, or that Anthropic already
  definitively rejected, is never posted again (``shared:sign-in-again``).

Claude Code's protocol (2.1.283; see ``docs/ANTHROPIC-SUBSCRIPTION.md``):
``<dir>/.oauth_refresh.lock`` then the legacy ``<realpath(dir)>.lock``, both
``proper-lockfile`` directories with a heartbeat; re-read before and after
locking and stop if the access token changed; POST; take
``<dir>/.storage-write.lock`` and write only if the file still holds the
refresh token that was posted; release everything only after the write.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.core.credential_refresh_scope import RefreshPurpose
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CLAUDE_CODE,
    ORIGIN_MCC,
    STORAGE_WRITE_LOCK_NAME,
    is_synthetic_account_id,
    monotonic_write_allowed,
    normalise_epoch_seconds,
)
from my_claude_code.providers.oauth_file_lock import (
    REFRESH_LOCK_TIMING,
    STORAGE_WRITE_LOCK_TIMING,
    HeldLock,
    LockBusy,
    LockTiming,
    hold,
    try_acquire,
)
from my_claude_code.providers.oauth_ownership import (
    clear_known_dead,
    file_stamp,
    fingerprint,
    is_known_dead,
    mark_known_dead,
    mark_migration,
    migration_marker,
    read_only_reason,
    record_decision,
)

from . import credentials as creds
from .credentials import (
    CLAUDE_OAUTH_KEY,
    AccountRecord,
    AnthropicOAuthRefreshError,
    AnthropicOAuthRefreshRejected,
    AnthropicOAuthRefreshUnavailable,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
)

PROVIDER = "anthropic_oauth"
#: The card's and the log's name for the fallback credential, which has no
#: account record of its own.
FALLBACK_SLOT = "claude-code"

#: ``<dir>/.oauth_refresh.lock`` -- Claude Code 2.1.283's refresh lock.
REFRESH_LOCK_DIRNAME = ".oauth_refresh.lock"
#: Claude Code keeps a pid record beside it. MCC never writes it.
OWNER_RECORD_NAME = ".oauth_refresh.lock.owner"
#: ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` relocates Claude Code's lock directory.
SECURE_STORAGE_DIR_ENV = "CLAUDE_SECURESTORAGE_CONFIG_DIR"

#: A 401 within this many seconds of the stated expiry may refresh (rule 4).
SHARED_401_WINDOW_SECONDS = 120.0
#: Claude Code's save retries three times, ``100 * i`` ms apart (F2).
WRITE_TRIES = 3
WRITE_RETRY_STEP_SECONDS = 0.1

#: The legacy lock has no documented heartbeat of its own; MCC touches it on
#: the refresh lock's cadence and, like every Claude lock, never breaks one
#: that moved in the last minute.
LEGACY_LOCK_TIMING = REFRESH_LOCK_TIMING


class SharedCredentialWaiting(AnthropicOAuthUnavailableError):
    """A shared credential expired and only Claude Code may renew it now."""


class SharedRefreshRejected(AnthropicOAuthRefreshRejected):
    """A shared refresh token is finished: sign in again in Claude Code.

    Definitive, so it maps exactly as :class:`AnthropicOAuthRefreshRejected`
    always has (``AUTHENTICATION``). Only the words differ: MCC set nothing
    aside -- the credential belongs to Claude Code.
    """

    def __init__(self, status_code: int, *, response: Any = None) -> None:
        AnthropicOAuthRefreshError.__init__(
            self,
            status_code,
            f"Anthropic will not renew the Claude Code credential MCC shares "
            f"(HTTP {status_code}). Sign in again in Claude Code "
            "(`claude /login`); MCC will follow its file.",
            response=response,
        )


def _platform() -> str:
    """``sys.platform``, behind a seam the tests can pin (III.3)."""

    return sys.platform


def current_platform() -> str:
    """The platform the read-only rule sees; the card asks the same seam."""

    return _platform()


def claude_lock_directory() -> Path:
    """Where Claude Code keeps its refresh locks.

    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` when set, else its config home -- the
    directory ``.credentials.json`` lives in.
    """

    override = os.environ.get(SECURE_STORAGE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return creds.claude_credentials_path().parent


def refresh_lock_path() -> Path:
    return claude_lock_directory() / REFRESH_LOCK_DIRNAME


def legacy_lock_path() -> Path:
    directory = claude_lock_directory()
    try:
        resolved = Path(os.path.realpath(directory))
    except OSError:
        resolved = directory
    return resolved.with_name(resolved.name + ".lock")


def storage_lock_path(target: Path) -> Path:
    return target.parent / (STORAGE_WRITE_LOCK_NAME + ".lock")


# ---------------------------------------------------------------------------
# Reading Claude Code's file
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileRead:
    """One read of Claude Code's credential file."""

    #: ``ok`` / ``absent`` (no file, or no credential in it) / ``unreadable``
    #: (present but not parseable -- mid-write).
    state: str
    tokens: OAuthTokens | None
    stamp: tuple[int, int] | None


def read_claude_file(path: Path) -> FileRead:
    stamp = file_stamp(path)
    if stamp is None:
        return FileRead("absent", None, None)
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError, ValueError:
        return FileRead("unreadable", None, stamp)
    if not isinstance(document, dict):
        return FileRead("unreadable", None, stamp)
    block = document.get(CLAUDE_OAUTH_KEY)
    if not isinstance(block, dict):
        return FileRead("absent", None, stamp)
    tokens = creds._tokens_from_payload(block, source="claude-code")
    if tokens is None:
        return FileRead("absent", None, stamp)
    return FileRead("ok", tokens, stamp)


def _identity_uuid() -> str:
    """``oauthAccount.accountUuid`` of ``~/.claude.json`` (Q1), or ``""``."""

    return creds.claude_code_oauth_account().get("accountUuid", "")


def _same_token(a: OAuthTokens | None, b: OAuthTokens | None) -> bool:
    if a is None or b is None:
        return a is b
    return a.access_token == b.access_token and a.refresh_token == b.refresh_token


def _file_is_newer(file_tokens: OAuthTokens, mirror: OAuthTokens) -> bool:
    return monotonic_write_allowed(
        target_expires_at=mirror.expires_at,
        target_refresh_expires_at=mirror.refresh_token_expires_at,
        ours_expires_at=file_tokens.expires_at,
        ours_refresh_expires_at=file_tokens.refresh_token_expires_at,
    )


# ---------------------------------------------------------------------------
# The slot one auth instance serves
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SharedSlot:
    """What an auth instance knows about the shared credential it serves."""

    slot: str
    path: Path
    #: The tokens as MCC last read (or wrote) them.
    seen: OAuthTokens
    #: The file's stamp at that read, or ``None`` when the file was absent.
    stamp: tuple[int, int] | None
    #: The account record, for an imported credential; ``None`` for the
    #: fallback, which has no record.
    record: AccountRecord | None = None
    #: ``oauthAccount.accountUuid`` the last time the file changed.
    identity: str = ""

    @property
    def write_back_flag(self) -> bool:
        return True if self.record is None else self.record.write_back


_ONCE: set[tuple[str, str, str]] = set()


def _once(slot: str, code: str, tokens: OAuthTokens | None, **kwargs: Any) -> None:
    """Emit ``code`` once per token, for the decisions a busy loop repeats."""

    key = (slot, code, fingerprint(tokens.access_token if tokens else ""))
    if key in _ONCE:
        return
    _ONCE.add(key)
    record_decision(PROVIDER, slot, code, **kwargs)


def reset_once_cache() -> None:
    """Forget which once-per-token decisions were emitted. Tests only."""

    _ONCE.clear()


def _mirror(
    record: AccountRecord, tokens: OAuthTokens, **changes: Any
) -> AccountRecord:
    """Update an imported record's copy of the file's token."""

    identified = replace(
        tokens,
        account_uuid=tokens.account_uuid or record.tokens.account_uuid,
        account_email=tokens.account_email or record.tokens.account_email,
        organization_name=tokens.organization_name or record.tokens.organization_name,
        source="mcc",
    )
    try:
        updated = creds.update_account_record(record.id, tokens=identified, **changes)
    except OSError as error:
        # The mirror is a cache (rule 2): Claude Code's file is the truth, so
        # a mirror write that loses a race with another MCC server costs
        # nothing but a re-read next time. The answer is still served.
        logger.debug("Could not update the shared credential's mirror: {}", error)
        return replace(record, tokens=identified, **changes)
    return updated if updated is not None else record


def resolve_record(record: AccountRecord, previous: SharedSlot | None) -> SharedSlot:
    """Resolve an imported (shared) record against Claude Code's file."""

    path = (
        Path(record.origin_path)
        if record.origin_path
        else creds.claude_credentials_path()
    )
    prior = previous if previous is not None and previous.slot == record.id else None
    read = read_claude_file(path)
    if read.state == "unreadable":
        logger.debug(
            "Claude Code's credential file is mid-write; keeping the cached "
            "credential for this call (account {}).",
            record.id,
        )
        seen = prior.seen if prior is not None else record.tokens
        return SharedSlot(record.id, path, seen, prior.stamp if prior else None, record)
    if read.state == "absent":
        _once(record.id, "shared:no-file", record.tokens)
        return SharedSlot(record.id, path, record.tokens, None, record)
    assert read.tokens is not None
    file_tokens = read.tokens
    mirror = record.tokens
    if _same_token(file_tokens, mirror):
        if record.source_stamp != read.stamp:
            record = _mirror(record, record.tokens, source_stamp=read.stamp)
        return SharedSlot(record.id, path, record.tokens, read.stamp, record)
    if record.pending_write_back and fingerprint(file_tokens.refresh_token) == (
        record.pending_posted_fp
    ):
        # The file still holds the token MCC already spent: ours is the live
        # one until the write-back lands (rule 11).
        return SharedSlot(record.id, path, record.tokens, read.stamp, record)
    if record.source_stamp is not None and read.stamp == record.source_stamp:
        # The file has not moved since MCC last read or wrote it, so a
        # difference is MCC's own copy being the newer one (a baseline kept
        # below, or a token MCC rotated) -- never a Claude Code rotation.
        return SharedSlot(record.id, path, record.tokens, read.stamp, record)
    if record.source_stamp is None and not _file_is_newer(file_tokens, mirror):
        # A record stored before 7.69.1 has no baseline. Keep MCC's copy (it
        # may be a token MCC rotated) and start watching from here.
        record = _mirror(record, record.tokens, source_stamp=read.stamp)
        return SharedSlot(record.id, path, record.tokens, read.stamp, record)
    return _adopt_changed_file(record, path, file_tokens, read.stamp)


def _adopt_changed_file(
    record: AccountRecord,
    path: Path,
    file_tokens: OAuthTokens,
    stamp: tuple[int, int] | None,
) -> SharedSlot:
    """S2 / S3: the file moved under an imported record."""

    identity = _identity_uuid()
    known = "" if is_synthetic_account_id(record.id) else record.id
    if identity and known and identity != known:
        return _identity_changed(record, path, file_tokens, stamp, identity)
    clear_known_dead(PROVIDER, record.tokens.refresh_token)
    updated = _mirror(
        record,
        file_tokens,
        pending_write_back=False,
        pending_posted_fp="",
        source_stamp=stamp,
    )
    record_decision(PROVIDER, record.id, "shared:adopted")
    return SharedSlot(record.id, path, updated.tokens, stamp, updated, identity)


def _identity_changed(
    record: AccountRecord,
    path: Path,
    file_tokens: OAuthTokens,
    stamp: tuple[int, int] | None,
    identity: str,
) -> SharedSlot:
    """Rule 9: the user switched accounts in Claude Code."""

    creds.remove_account(record.id, keep_name=True)
    native = creds.account_for(identity)
    record_decision(
        PROVIDER,
        record.id,
        "shared:identity-changed",
        detail=f"now {identity}",
    )
    if native is not None and not native.is_shared:
        # MCC already holds the new account itself: the shared slot would be
        # a duplicate of it. "One id, one record."
        return SharedSlot(identity, path, native.tokens, stamp, native, identity)
    account = creds.claude_code_oauth_account()
    identified = replace(
        file_tokens,
        account_uuid=identity,
        account_email=account.get("emailAddress") or None,
        source="mcc",
    )
    added = creds.add_or_update_account(
        identified,
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(path),
        write_back=record.write_back,
        default_email=account.get("emailAddress", ""),
        account_id=identity,
    )
    added = creds.update_account_record(added.id, source_stamp=stamp) or added
    return SharedSlot(added.id, path, added.tokens, stamp, added, identity)


def resolve_fallback(tokens: OAuthTokens, previous: SharedSlot | None) -> SharedSlot:
    """The automatic fallback: a shared credential with no record."""

    path = creds.claude_credentials_path()
    stamp = file_stamp(path)
    prior = (
        previous if previous is not None and previous.slot == FALLBACK_SLOT else None
    )
    if prior is None:
        return SharedSlot(FALLBACK_SLOT, path, tokens, stamp, None, _identity_uuid())
    if _same_token(prior.seen, tokens):
        return SharedSlot(FALLBACK_SLOT, path, tokens, stamp, None, prior.identity)
    identity = _identity_uuid()
    clear_known_dead(PROVIDER, prior.seen.refresh_token)
    if identity and prior.identity and identity != prior.identity:
        record_decision(
            PROVIDER, FALLBACK_SLOT, "shared:identity-changed", detail=f"now {identity}"
        )
    else:
        record_decision(PROVIDER, FALLBACK_SLOT, "shared:adopted")
    return SharedSlot(
        FALLBACK_SLOT, path, tokens, stamp, None, identity or prior.identity
    )


def fallback_is_mid_write(previous: SharedSlot | None) -> bool:
    """S4 for the fallback: the file exists but cannot be parsed right now."""

    if previous is None or previous.slot != FALLBACK_SLOT:
        return False
    return read_claude_file(previous.path).state == "unreadable"


# ---------------------------------------------------------------------------
# Using it
# ---------------------------------------------------------------------------


def _read_only(slot: SharedSlot) -> str | None:
    return read_only_reason(
        target=slot.path,
        write_back_on=creds.write_back_enabled(),
        account_write_back=slot.write_back_flag,
        platform=_platform(),
    )


def _dead(slot: SharedSlot, tokens: OAuthTokens) -> bool:
    remaining = tokens.refresh_token_seconds_remaining()
    if remaining is not None and remaining <= 0:
        return True
    if not tokens.has_refresh_token:
        return True
    return is_known_dead(PROVIDER, tokens.refresh_token)


def _sign_in_again(slot: SharedSlot, tokens: OAuthTokens) -> Exception:
    _once(slot.slot, "shared:sign-in-again", tokens)
    return SharedRefreshRejected(400)


async def use(
    slot: SharedSlot,
    purpose: RefreshPurpose,
    *,
    after_401: bool = False,
    post: Callable[[str], Awaitable[Any]] | None = None,
) -> OAuthTokens:
    """Serve a shared credential, refreshing it only when rule 4 allows.

    Returns the token to send. Raises one of the existing refresh/unavailable
    errors, so the provider's existing failure mapping applies unchanged.
    """

    tokens = slot.seen
    if slot.record is not None and slot.record.pending_write_back:
        tokens = await retry_pending_write_back(slot)
    needs = tokens.is_expired() or (
        after_401 and (tokens.seconds_remaining() or 0.0) <= SHARED_401_WINDOW_SECONDS
    )
    if not needs:
        return tokens
    if _dead(slot, tokens):
        raise _sign_in_again(slot, tokens)
    if purpose == "background":
        _once(slot.slot, "shared:expired-waiting", tokens)
        raise SharedCredentialWaiting(
            "The shared Claude Code credential has expired; waiting for Claude "
            "Code to renew it (MCC refreshes a shared credential only for a "
            "real request)."
        )
    if slot.record is not None and not slot.path.is_file():
        _once(slot.slot, "shared:no-file", tokens)
        raise AnthropicOAuthUnavailableError(
            "The shared Claude Code credential has expired and Claude Code's "
            "credential file is gone; MCC serves its copy only while it is valid."
        )
    reason = _read_only(slot)
    if reason is not None:
        _once(slot.slot, f"shared:read-only:{reason}", tokens)
        raise AnthropicOAuthUnavailableError(
            "The shared Claude Code credential has expired and MCC may not "
            f"renew it ({reason}); waiting for Claude Code."
        )
    # The in-process lock stays the first layer (rule 8): a burst of requests
    # in this server waits here rather than on the file lock's 1-2 s retries,
    # and the one that goes second re-reads and adopts the winner's token.
    async with _inprocess_lock(slot.path):
        return await locked_refresh(slot, tokens, post=post)


_INPROCESS_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


def _inprocess_lock(path: Path) -> asyncio.Lock:
    # Keyed on the running loop too: an ``asyncio.Lock`` that was ever
    # contended is bound to its loop and would refuse a second one.
    key = (str(path), id(asyncio.get_running_loop()))
    lock = _INPROCESS_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _INPROCESS_LOCKS[key] = lock
    return lock


# ---------------------------------------------------------------------------
# The locked refresh (rule 5)
# ---------------------------------------------------------------------------


async def _acquire_claude_locks(
    timing: LockTiming,
) -> tuple[HeldLock, HeldLock]:
    """Take the refresh lock, then the legacy lock, Claude Code's way.

    If the legacy lock is busy, Claude Code releases the new one and treats
    the attempt as ELOCKED; so does MCC. The waiting budget is the refresh
    lock's.
    """

    refresh = refresh_lock_path()
    legacy = legacy_lock_path()
    refresh.parent.mkdir(parents=True, exist_ok=True)
    waited = False
    deadline: float | None = None
    attempt = 0
    while True:
        held = try_acquire(refresh, timing)
        if held is not None:
            legacy_held = try_acquire(legacy, LEGACY_LOCK_TIMING)
            if legacy_held is not None:
                held.waited = waited
                legacy_held.waited = waited
                return held, legacy_held
            held.release()
        waited = True
        if attempt < timing.retries:
            attempt += 1
            await asyncio.sleep(timing.retry_delay(attempt))
            continue
        if deadline is None:
            deadline = time.monotonic() + timing.liveness_seconds
        if time.monotonic() >= deadline:
            raise LockBusy(f"{refresh} is held by a live owner")
        await asyncio.sleep(timing.liveness_poll_seconds)


async def locked_refresh(
    slot: SharedSlot,
    tokens: OAuthTokens,
    *,
    post: Callable[[str], Awaitable[Any]] | None = None,
) -> OAuthTokens:
    """S6-S12: the one path that may POST a shared refresh token."""

    poster = post if post is not None else creds._post_refresh
    before = read_claude_file(slot.path)
    if (
        before.state == "ok"
        and before.tokens is not None
        and (before.tokens.access_token != tokens.access_token)
    ):
        return _adopt_now(slot, before, "shared:adopted")
    try:
        refresh_lock, legacy_lock = await _acquire_claude_locks(REFRESH_LOCK_TIMING)
    except LockBusy as error:
        _once(slot.slot, "shared:lock-busy", tokens)
        raise AnthropicOAuthRefreshUnavailable(
            503, detail="Claude Code's refresh lock is busy"
        ) from error
    heartbeat = asyncio.get_running_loop().create_task(_beat(refresh_lock, legacy_lock))
    try:
        under = read_claude_file(slot.path)
        if (
            under.state == "ok"
            and under.tokens is not None
            and (under.tokens.access_token != tokens.access_token)
        ):
            return _adopt_now(slot, under, "shared:waited")
        assert tokens.refresh_token is not None
        posted = tokens.refresh_token
        response = await poster(posted)
        if response.status_code >= 400:
            failure = creds.classify_refresh_failure(response)
            logger.warning(
                "Shared Claude subscription refresh failed: status={} definitive={}",
                failure.status_code,
                failure.definitive,
            )
            if failure.definitive:
                mark_known_dead(PROVIDER, posted)
                record_decision(PROVIDER, slot.slot, "shared:rejected")
                raise SharedRefreshRejected(
                    failure.status_code, response=failure.response
                ) from failure
            raise failure
        refreshed = creds._tokens_from_refresh(response.json(), previous=tokens)
        compromised = refresh_lock.compromised or legacy_lock.compromised
        outcome, file_tokens = (
            ("failed", None)
            if compromised
            else await cas_write(slot.path, refreshed, posted)
        )
        if outcome == "wrote":
            _after_write(slot, refreshed, "shared:refreshed+wrote-back")
            return slot.seen
        if outcome == "superseded" and file_tokens is not None:
            return _adopt_now(
                slot,
                FileRead("ok", file_tokens, file_stamp(slot.path)),
                "shared:superseded",
            )
        _keep_pending(slot, refreshed, posted)
        return refreshed
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat
        legacy_lock.release()
        refresh_lock.release()


async def _beat(*locks: HeldLock) -> None:
    seconds = REFRESH_LOCK_TIMING.heartbeat_seconds
    while True:
        await asyncio.sleep(seconds)
        for held in locks:
            held.touch()


def _adopt_now(slot: SharedSlot, read: FileRead, code: str) -> OAuthTokens:
    assert read.tokens is not None
    clear_known_dead(PROVIDER, slot.seen.refresh_token)
    if slot.record is not None:
        slot.record = _mirror(
            slot.record,
            read.tokens,
            pending_write_back=False,
            pending_posted_fp="",
            source_stamp=read.stamp,
        )
        slot.seen = slot.record.tokens
    else:
        slot.seen = read.tokens
    slot.stamp = read.stamp
    record_decision(PROVIDER, slot.slot, code)
    return slot.seen


def _after_write(slot: SharedSlot, refreshed: OAuthTokens, code: str) -> None:
    stamp = file_stamp(slot.path)
    if slot.record is not None:
        slot.record = _mirror(
            slot.record,
            refreshed,
            pending_write_back=False,
            pending_posted_fp="",
            source_stamp=stamp,
        )
        slot.seen = slot.record.tokens
    else:
        slot.seen = replace(refreshed, source="claude-code")
    slot.stamp = stamp
    record_decision(PROVIDER, slot.slot, code)


def _keep_pending(slot: SharedSlot, refreshed: OAuthTokens, posted: str) -> None:
    """Rule 11: MCC never drops a token it rotated."""

    stamp = file_stamp(slot.path)
    if slot.record is not None:
        slot.record = _mirror(
            slot.record,
            refreshed,
            pending_write_back=True,
            pending_posted_fp=fingerprint(posted),
            source_stamp=stamp,
        )
    else:
        identity = slot.identity or refreshed.account_uuid or ""
        identified = replace(
            refreshed,
            account_uuid=refreshed.account_uuid or identity or None,
            source="mcc",
        )
        record = creds.add_or_update_account(
            identified,
            origin=ORIGIN_CLAUDE_CODE,
            origin_path=str(slot.path),
            write_back=True,
            account_id=identified.account_uuid or None,
        )
        slot.record = (
            creds.update_account_record(
                record.id,
                pending_write_back=True,
                pending_posted_fp=fingerprint(posted),
                source_stamp=stamp,
            )
            or record
        )
        slot.slot = slot.record.id
    slot.seen = slot.record.tokens
    slot.stamp = stamp
    record_decision(PROVIDER, slot.slot, "shared:writeback-pending")


async def cas_write(
    target: Path, refreshed: OAuthTokens, posted: str
) -> tuple[str, OAuthTokens | None]:
    """Compare-and-swap ``claudeAiOauth`` under ``.storage-write``.

    ``("wrote", None)``, ``("superseded", <the file's token>)`` or
    ``("failed", None)`` after :data:`WRITE_TRIES` attempts.
    """

    for attempt in range(1, WRITE_TRIES + 1):
        try:
            async with hold(storage_lock_path(target), STORAGE_WRITE_LOCK_TIMING):
                document = creds._load_json(target)
                existing = document.get(CLAUDE_OAUTH_KEY)
                if not isinstance(existing, dict):
                    raise OSError("the credential file holds no claudeAiOauth block")
                if not creds.refresh_token_unchanged(existing, posted) or not (
                    monotonic_write_allowed(
                        target_expires_at=normalise_epoch_seconds(
                            existing.get("expiresAt")
                        ),
                        target_refresh_expires_at=normalise_epoch_seconds(
                            existing.get("refreshTokenExpiresAt")
                        ),
                        ours_expires_at=refreshed.expires_at,
                        ours_refresh_expires_at=refreshed.refresh_token_expires_at,
                    )
                ):
                    return "superseded", creds._tokens_from_payload(
                        existing, source="claude-code"
                    )
                creds.write_claude_block(document, existing, refreshed, target)
                return "wrote", None
        except (LockBusy, OSError) as error:
            logger.warning(
                "Write-back to {} failed (try {} of {}): {}",
                target.name,
                attempt,
                WRITE_TRIES,
                error,
            )
            await asyncio.sleep(WRITE_RETRY_STEP_SECONDS * attempt)
    return "failed", None


async def retry_pending_write_back(slot: SharedSlot) -> OAuthTokens:
    """S9 on a later use: retry the write, same locks, same CAS, no POST."""

    record = slot.record
    assert record is not None
    read = read_claude_file(slot.path)
    if read.state != "ok" or read.tokens is None:
        return slot.seen
    if fingerprint(read.tokens.refresh_token) != record.pending_posted_fp and (
        read.tokens.refresh_token
    ):
        # The file changed: adopt it (rule 11 stops here).
        return _adopt_now(slot, read, "shared:adopted")
    try:
        refresh_lock, legacy_lock = await _acquire_claude_locks(
            replace(REFRESH_LOCK_TIMING, retries=0, liveness_seconds=0.0)
        )
    except LockBusy:
        return slot.seen
    try:
        outcome, file_tokens = await _cas_write_fp(slot.path, slot.seen, record)
    finally:
        legacy_lock.release()
        refresh_lock.release()
    if outcome == "wrote":
        _after_write(slot, slot.seen, "shared:refreshed+wrote-back")
    elif outcome == "superseded" and file_tokens is not None:
        return _adopt_now(
            slot,
            FileRead("ok", file_tokens, file_stamp(slot.path)),
            "shared:superseded",
        )
    return slot.seen


async def _cas_write_fp(
    target: Path, ours: OAuthTokens, record: AccountRecord
) -> tuple[str, OAuthTokens | None]:
    try:
        async with hold(storage_lock_path(target), STORAGE_WRITE_LOCK_TIMING):
            document = creds._load_json(target)
            existing = document.get(CLAUDE_OAUTH_KEY)
            if not isinstance(existing, dict):
                return "failed", None
            current = existing.get("refreshToken")
            current = current if isinstance(current, str) else ""
            if current and fingerprint(current) != record.pending_posted_fp:
                return "superseded", creds._tokens_from_payload(
                    existing, source="claude-code"
                )
            creds.write_claude_block(document, existing, ours, target)
            return "wrote", None
    except (LockBusy, OSError) as error:
        logger.debug("Pending write-back retry did not land: {}", error)
        return "failed", None


# ---------------------------------------------------------------------------
# The one-time migration (rule 14)
# ---------------------------------------------------------------------------


_MIGRATION_DONE = False


async def migrate_once() -> str:
    """Write a viable MCC-signed token over Claude Code's expired one, once.

    Returns the decision code. Runs at most once per install (the marker in
    ``oauth_ownership.json``) and at most once per process.
    """

    global _MIGRATION_DONE
    if _MIGRATION_DONE:
        return "migration:already-ran"
    _MIGRATION_DONE = True
    if migration_marker(PROVIDER) is not None:
        return "migration:already-ran"
    try:
        code = await _migrate()
    except Exception as error:  # pragma: no cover - never fatal
        logger.warning("Claude credential ownership migration skipped: {}", error)
        code = "migration:skipped:error"
    mark_migration(PROVIDER, code)
    record_decision(PROVIDER, FALLBACK_SLOT, code, persist=False)
    return code


def reset_migration_flag() -> None:
    """Tests only: let :func:`migrate_once` run again in this process."""

    global _MIGRATION_DONE
    _MIGRATION_DONE = False


async def _migrate() -> str:
    store = creds.managed_store_path()
    store_stamp = file_stamp(store)
    if store_stamp is None:
        return "migration:skipped:no-store"
    records = creds.load_accounts(migrate=False)
    if not records:
        return "migration:skipped:no-store"
    primary = records[0]
    if primary.origin != ORIGIN_MCC:
        return "migration:skipped:not-mcc"
    viable, _ = creds.credential_viability(primary.tokens)
    if not viable or primary.tokens.is_expired():
        return "migration:skipped:not-viable"
    target = creds.claude_credentials_path()
    if _platform() == "darwin":
        return "migration:skipped:macos"
    if not creds.write_back_enabled():
        return "migration:skipped:write-back-off"
    before = read_claude_file(target)
    if before.state != "ok" or before.tokens is None or before.stamp is None:
        return "migration:skipped:no-file"
    identity = _identity_uuid()
    ours_id = primary.tokens.account_uuid or (
        "" if is_synthetic_account_id(primary.id) else primary.id
    )
    if not identity or not ours_id or identity != ours_id:
        return "migration:skipped:other-identity"
    if not before.tokens.is_expired():
        return "migration:skipped:file-valid"
    if before.stamp[0] >= store_stamp[0]:
        return "migration:skipped:file-newer"
    if (primary.tokens.expires_at or 0) <= (before.tokens.expires_at or 0):
        return "migration:skipped:not-later"
    posted = before.tokens.refresh_token or ""
    try:
        refresh_lock, legacy_lock = await _acquire_claude_locks(REFRESH_LOCK_TIMING)
    except LockBusy:
        return "migration:skipped:lock-busy"
    try:
        outcome, _ = await cas_write(target, primary.tokens, posted)
    finally:
        legacy_lock.release()
        refresh_lock.release()
    if outcome != "wrote":
        return f"migration:skipped:{outcome}"
    creds.update_account_record(
        primary.id,
        origin=ORIGIN_CLAUDE_CODE,
        origin_path=str(target),
        write_back=True,
        source_stamp=file_stamp(target),
    )
    return "migration:wrote-back"
